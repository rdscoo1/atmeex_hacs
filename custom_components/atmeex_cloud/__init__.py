from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, List, TypedDict, Callable, Awaitable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import AtmeexApi, ApiError
from .const import DOMAIN, PLATFORMS
from .config_flow import AtmeexOptionsFlowHandler

_LOGGER = logging.getLogger(__name__)

_LAST_API_ERROR: ApiError | None = None

# События, которые будут попадать в Logbook
EVENT_API_ERROR = "atmeex_cloud_api_error"
EVENT_DEVICE_UPDATED = "atmeex_cloud_device_updated"


class AtmeexCoordinatorData(TypedDict, total=False):
    """Структура данных, хранимая координатором."""
    devices: list[dict[str, Any]]
    states: dict[str, dict[str, Any]]
    # Доп. диагностические поля
    last_success_ts: float | None
    avg_latency_ms: float | None
    request_retries: int


@dataclass
class AtmeexRuntimeData:
    """Единый runtime-объект для записи конфигурации."""
    api: AtmeexApi
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData]
    refresh_device: Callable[[int | str], Awaitable[None]]


__all__ = [
    "async_setup_entry",
    "async_unload_entry",
    "async_get_options_flow",
    "AtmeexCoordinatorData",
    "AtmeexRuntimeData",
]


def _to_bool(v: Any) -> bool:
    """Приведение к bool"""
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return bool(v)


def _normalize_device_state(item: dict[str, Any]) -> dict[str, Any]:
    """Склеить condition + settings → нормализованное состояние (+ online)"""
    cond = dict(item.get("condition") or {})
    st = dict(item.get("settings") or {})

    # Питание
    pwr_cond = cond.get("pwr_on")
    pwr = _to_bool(pwr_cond) if pwr_cond is not None else _to_bool(st.get("u_pwr_on"))

    # Скорость вентилятора
    fan = cond.get("fan_speed")
    u_fan = st.get("u_fan_speed")
    if (
        (fan is None or int(fan) == 0)
        and pwr
        and isinstance(u_fan, (int, float))
        and int(u_fan) > 0
    ):
        fan = int(u_fan)

    # Заслонка
    damp = cond.get("damp_pos")
    if damp is None and "u_damp_pos" in st:
        damp = st.get("u_damp_pos")

    # Цель температуры (деци-°C)
    u_temp = cond.get("u_temp_room")
    if u_temp is None and "u_temp_room" in st:
        u_temp = st.get("u_temp_room")

    # Увлажнение (ступень)
    hum_stg = cond.get("hum_stg")
    if hum_stg is None and "u_hum_stg" in st:
        hum_stg = st.get("u_hum_stg")

    # Текущие показания
    hum_room = cond.get("hum_room")
    temp_room = cond.get("temp_room")

    out = dict(cond) if cond else {}
    if pwr is not None:
        out["pwr_on"] = bool(pwr)
    if fan is not None:
        try:
            out["fan_speed"] = int(fan)
        except (TypeError, ValueError):
            pass

    if damp is not None:
        try:
            out["damp_pos"] = int(damp)
        except (TypeError, ValueError):
            pass

    if hum_stg is not None:
        try:
            out["hum_stg"] = int(hum_stg)
        except (TypeError, ValueError):
            pass

    if u_temp is not None:
        try:
            out["u_temp_room"] = int(u_temp)
        except (TypeError, ValueError):
            pass

    if isinstance(hum_room, (int, float)):
        out["hum_room"] = int(hum_room)
    if isinstance(temp_room, (int, float)):
        out["temp_room"] = int(temp_room)

    # meta
    out["online"] = bool(item.get("online", True))
    return out


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Atmeex Cloud from a config entry."""
    session = async_get_clientsession(hass)

    # Поддерживаем оба варианта ключей: CONF_EMAIL/CONF_PASSWORD и "email"/"password"
    email = entry.data.get(CONF_EMAIL) or entry.data.get("email")
    password = entry.data.get(CONF_PASSWORD) or entry.data.get("password")

    api = AtmeexApi(session)
    await api.async_init()

    # Логин: различаем неверные креды и временные сетевые проблемы
    try:
        await api.login(email, password)
    except ApiError as err:
        status = getattr(err, "status", None)
        if status in (401, 403):
            # неправильный логин/пароль → запускаем re-auth flow
            raise ConfigEntryAuthFailed(
                f"Invalid Atmeex credentials: {err}"
            ) from err
        # остальное — проблемы соединения / бэкенда → NotReady
        raise ConfigEntryNotReady(
            f"Cannot connect to Atmeex Cloud: {err}"
        ) from err

    options = getattr(entry, "options", {}) or {}
    raw_interval = int(options.get("update_interval", 30))
    update_interval_seconds = max(10, min(300, raw_interval))

    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData]

    async def _async_update_data() -> AtmeexCoordinatorData:
        """Плановый опрос: тянем устройства, при ошибке кидаем UpdateFailed / AuthFailed."""
        global _LAST_API_ERROR

        start_ts = time.perf_counter()
        try:
            devices = await api.get_devices(fallback=False)
        except ApiError as err:
            _LAST_API_ERROR = err
            status = getattr(err, "status", None)
            if status in (401, 403):
                # токен протух / креды поменяли → re-auth
                raise ConfigEntryAuthFailed(
                    f"Authentication with Atmeex failed during update: {err}"
                ) from err
            raise UpdateFailed(
                f"Error communicating with Atmeex API: {err}"
            ) from err
        except Exception as err:
            _LAST_API_ERROR = None
            raise UpdateFailed(
                f"Unexpected error while updating Atmeex data: {err}"
            ) from err

        elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

        if not isinstance(devices, list):
            raise UpdateFailed("Atmeex API returned non-list devices payload")

        # мержим новые устройства с прошлым снимком, чтобы не терять оффлайн-девайсы
        devices_by_id: Dict[str, dict[str, Any]] = {
            str(d.get("id")): d for d in devices if d.get("id") is not None
        }

        # Используем предыдущие данные координатора, если они были успешны
        if getattr(coordinator, "last_update_success", False) and isinstance(
            getattr(coordinator, "data", None), dict
        ):
            prev: AtmeexCoordinatorData = coordinator.data  # type: ignore[assignment]
            for d in prev.get("devices", []):
                did = d.get("id")
                if did is None:
                    continue
                key = str(did)
                if key not in devices_by_id:
                    devices_by_id[key] = d

        merged_devices: List[dict[str, Any]] = list(devices_by_id.values())

        states: dict[str, dict[str, Any]] = {}
        for dev in merged_devices:
            did = dev.get("id")
            if did is None:
                continue
            states[str(did)] = _normalize_device_state(dev)

        # Обновляем last_success_ts и счётчик ретраев (если он есть у api)
        retry_count = getattr(api, "_retry_count", 0)

        data: AtmeexCoordinatorData = {
            "devices": merged_devices,
            "states": states,
            "last_success_ts": time.time(),
            "avg_latency_ms": round(elapsed_ms, 1),
            "request_retries": retry_count,
        }

        # привяжем диагностические поля к самому координатору
        setattr(coordinator, "last_success_ts", data["last_success_ts"])
        setattr(coordinator, "last_api_error", _LAST_API_ERROR)

        # успешный апдейт — обнуляем last error
        _LAST_API_ERROR = None

        return data


    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_method=_async_update_data,
        update_interval=timedelta(seconds=update_interval_seconds),
    )

    await coordinator.async_config_entry_first_refresh()

    async def refresh_device(device_id: int | str) -> None:
        """Дочитать одно устройство и обновить координатор."""
        try:
            full = await api.get_device(device_id)
        except ApiError as e:
            _LOGGER.warning("Failed to refresh device %s: %s", device_id, e)
            return
        except Exception as e:
            _LOGGER.warning("Unexpected error in refresh_device(%s): %s", device_id, e)
            return

        cond_norm = _normalize_device_state(full)
        cur = coordinator.data or {"devices": [], "states": {}, "last_success_ts": None}
        devices = list(cur.get("devices", []))
        states = dict(cur.get("states", {}))

        inserted = True
        for i, d in enumerate(devices):
            if d.get("id") == full.get("id"):
                devices[i] = full
                inserted = False
                break
        if inserted:
            devices.append(full)

        states[str(full.get("id"))] = cond_norm
        coordinator.async_set_updated_data(
            {
                "devices": devices,
                "states": states,
                "last_success_ts": cur.get("last_success_ts"),
            }
        )

    runtime_data = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh_device,
    )
    entry.runtime_data = runtime_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Atmeex Cloud config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


async def async_get_options_flow(config_entry: ConfigEntry):
    """Hook для options flow."""
    return AtmeexOptionsFlowHandler(config_entry)
