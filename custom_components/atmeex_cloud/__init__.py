from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from time import time  # NEW: for last_success_ts
from typing import Any, Dict, List, TypedDict, Callable, Awaitable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import AtmeexApi, ApiError
from .const import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

# NEW: track last API error for diagnostics
_LAST_API_ERROR: str | None = None


class AtmeexCoordinatorData(TypedDict):
    """Структура данных, хранимая координатором."""

    devices: list[dict[str, Any]]
    states: dict[str, dict[str, Any]]
    # NEW: timestamp of last successful coordinator update (UTC, time.time())
    last_success_ts: float | None


@dataclass
class AtmeexRuntimeData:
    """Единый runtime-объект для записи конфигурации."""

    api: AtmeexApi
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData]
    refresh_device: Callable[[int | str], Awaitable[None]]


__all__ = [
    "async_setup_entry",
    "async_unload_entry",
    "AtmeexCoordinatorData",
    "AtmeexRuntimeData",
    "get_diagnostics_snapshot",  # NEW: helper for diagnostic entities
]


def _to_bool(v: Any) -> bool:
    """Best-effort приведение к bool."""
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except Exception:
        return bool(v)


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Склеить condition + settings → нормализованное состояние (+ online)."""
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
        except Exception:
            pass
    if damp is not None:
        try:
            out["damp_pos"] = int(damp)
        except Exception:
            pass
    if hum_stg is not None:
        try:
            out["hum_stg"] = int(hum_stg)
        except Exception:
            pass
    if u_temp is not None:
        try:
            out["u_temp_room"] = int(u_temp)  # деци-°C
        except Exception:
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
    api = AtmeexApi(session)
    await api.async_init()

    # Поддерживаем оба варианта ключей: CONF_EMAIL/CONF_PASSWORD и "email"/"password"
    email = entry.data.get(CONF_EMAIL) or entry.data.get("email")
    password = entry.data.get(CONF_PASSWORD) or entry.data.get("password")

    # Логин: при ошибке — ConfigEntryNotReady, интеграция не стартует.
    try:
        await api.login(email, password)
    except ApiError as err:
        raise ConfigEntryNotReady(f"Cannot connect to Atmeex Cloud: {err}") from err

    # Объявляем coordinator в замыкании, чтобы в update иметь доступ
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData]

    async def _async_update_data() -> AtmeexCoordinatorData:
        """Плановый опрос: тянем устройства, при ошибке кидаем UpdateFailed.

        DataUpdateCoordinator сам:
        - на первом запуске превратит UpdateFailed → ConfigEntryNotReady;
        - после первого успешного опроса будет сохранять прошлые данные при ошибках.
        """
        global _LAST_API_ERROR  # NEW: record most recent API error for diagnostics

        try:
            devices = await api.get_devices(fallback=False)
        except ApiError as err:
            _LAST_API_ERROR = f"API error in get_devices: {err}"
            raise UpdateFailed(f"Error communicating with Atmeex API: {err}") from err
        except Exception as err:
            _LAST_API_ERROR = f"Unexpected error in get_devices: {err}"
            raise UpdateFailed(
                f"Unexpected error while updating Atmeex data: {err}"
            ) from err

        if not isinstance(devices, list):
            _LAST_API_ERROR = "Atmeex API returned non-list devices payload"
            raise UpdateFailed(_LAST_API_ERROR)

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
            states[str(did)] = _normalize_item(dev)

        # NEW: successful update – clear last error and record timestamp
        _LAST_API_ERROR = None
        return {
            "devices": merged_devices,
            "states": states,
            "last_success_ts": time(),
        }

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_method=_async_update_data,
        update_interval=timedelta(seconds=30),
    )

    await coordinator.async_config_entry_first_refresh()

    async def refresh_device(device_id: int | str) -> None:
        """Дочитать одно устройство и обновить координатор."""
        global _LAST_API_ERROR  # NEW: record errors coming from single-device refresh

        try:
            full = await api.get_device(device_id)
        except ApiError as e:
            _LAST_API_ERROR = f"Failed to refresh device {device_id}: {e}"
            _LOGGER.warning(_LAST_API_ERROR)
            return
        except Exception as e:
            _LAST_API_ERROR = f"Unexpected error in refresh_device({device_id}): {e}"
            _LOGGER.warning(_LAST_API_ERROR)
            return

        # Успешный запрос по одному устройству не обязательно означает
        # успешный полный опрос, но можно считать это "локальным" success.
        cond_norm = _normalize_item(full)

        cur: AtmeexCoordinatorData = coordinator.data or {
            "devices": [],
            "states": {},
            "last_success_ts": None,
        }
        devices = list(cur.get("devices", []))
        states = dict(cur.get("states", {}))
        last_success_ts = cur.get("last_success_ts")

        inserted = True
        full_id = full.get("id")
        # кешируем строковый id для ключей.
        full_id_str = str(full_id) if full_id is not None else None

        for i, d in enumerate(devices):
            if d.get("id") == full_id:
                devices[i] = full
                inserted = False
                break
        if inserted:
            devices.append(full)

        if full_id_str is not None:
            states[full_id_str] = cond_norm

        if not isinstance(last_success_ts, (int, float)):
            last_success_ts = time()

        coordinator.async_set_updated_data(
            {
                "devices": devices,
                "states": states,
                "last_success_ts": last_success_ts,
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


# small helper for diagnostic entities / diagnostics.py
def get_diagnostics_snapshot(
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData],
) -> dict[str, Any]:
    """Return a compact diagnostics snapshot for entities/UI."""
    data: AtmeexCoordinatorData = coordinator.data or {
        "devices": [],
        "states": {},
        "last_success_ts": None,
    }
    devices = data.get("devices") or []
    last_ts = data.get("last_success_ts")

    # Build ISO string for readability; keep raw ts as well
    last_success_utc: str | None = None
    if isinstance(last_ts, (int, float)):
        from datetime import datetime, timezone

        try:
            last_success_utc = (
                datetime.fromtimestamp(last_ts, tz=timezone.utc).isoformat()
            )
        except Exception:  # pragma: no cover - very defensive
            last_success_utc = None

    return {
        "device_count": len(devices),
        "last_success_ts": last_ts,
        "last_success_utc": last_success_utc,
        "last_api_error": _LAST_API_ERROR,
    }
