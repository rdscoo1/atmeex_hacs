from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypedDict, Callable, Awaitable

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import AtmeexApi, ApiError, AtmeexDevice, AtmeexState
from .const import (
    PLATFORMS,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL
)
from .config_flow import AtmeexOptionsFlowHandler
from .helpers import to_bool

_LOGGER = logging.getLogger(__name__)

# События, которые будут попадать в Logbook
EVENT_API_ERROR = "atmeex_cloud_api_error"
EVENT_DEVICE_UPDATED = "atmeex_cloud_device_updated"


class AtmeexCoordinatorData(TypedDict, total=False):
    """Структура данных, хранимая координатором."""
    devices: list[dict[str, Any]
                  ]  # "сырой" payload для обратной совместимости / диагностики
    device_map: dict[str, AtmeexDevice]
    states: dict[str, dict[str, Any]]
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


def _normalize_device_state(item: dict[str, Any]) -> dict[str, Any]:
    """Склеить condition + settings → нормализованное состояние (+ online)"""
    cond = dict(item.get("condition") or {})
    st = dict(item.get("settings") or {})

    # Питание
    pwr_cond = cond.get("pwr_on")
    pwr = to_bool(pwr_cond) if pwr_cond is not None else to_bool(
        st.get("u_pwr_on"))

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

    async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    async def _fetch_devices_safely() -> list[AtmeexDevice]:
        """Получить список устройств с fallback и дочитыванием по id

        Важные моменты:
        * 401/403 не скрываем — они должны привести к re-auth;
        * сетевые/прочие ошибки → пытаемся fallback=True;
        * для каждого устройства по возможности вызываем get_device(id),
          но auth-ошибки опять же не глотаем.
        """
        devices: list[AtmeexDevice] = []

        # 1. Основной вызов без fallback
        try:
            primary = await api.get_devices(fallback=False)
            if isinstance(primary, list) and primary:
                devices = primary
        except ApiError as err:
            status = getattr(err, "status", None)
            if status in (401, 403):
                # Пусть разберётся верхний уровень — он превратит это в ConfigEntryAuthFailed
                raise
            _LOGGER.debug("Primary get_devices failed: %s", err)
        except Exception as err:
            _LOGGER.debug("Unexpected error in primary get_devices: %s", err)

        # 2. Если ничего не получили — пробуем fallback=True
        if not devices:
            try:
                fb = await api.get_devices(fallback=True)
                if isinstance(fb, list):
                    devices = fb
            except ApiError as err:
                if getattr(err, "status", None) in (401, 403):
                    raise
                _LOGGER.warning("Fallback get_devices failed: %s", err)
                devices = []
            except (asyncio.TimeoutError, aiohttp.ClientError) as err:
                _LOGGER.warning("Fallback get_devices network error: %s", err)
                devices = []
            except Exception as err:
                _LOGGER.exception("Unexpected error in fallback get_devices: %s", err)
                devices = []


        # 3. Дочитываем по одному устройству
        hydrated: list[AtmeexDevice] = []
        for dev in devices:
            did = dev.id
            try:
                full = await api.get_device(did)
                hydrated.append(full)
            except ApiError as err:
                status = getattr(err, "status", None)
                if status in (401, 403):
                    raise
                _LOGGER.debug("get_device(%s) failed: %s", did, err)
                hydrated.append(dev)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "Unexpected error in get_device(%s): %s", did, err)
                hydrated.append(dev)

        return hydrated

    async def _async_update_data() -> AtmeexCoordinatorData:
        """Плановый опрос: тянем устройства, при ошибке кидаем UpdateFailed / AuthFailed."""

        start_ts = time.perf_counter()
        try:
            device_objs = await _fetch_devices_safely()
        except ApiError as err:
            setattr(coordinator, "last_api_error", err)
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
            setattr(coordinator, "last_api_error", None)
            raise UpdateFailed(
                f"Unexpected error while updating Atmeex data: {err}"
            ) from err

        elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

        if not isinstance(device_objs, list):
            raise UpdateFailed("Atmeex API returned non-list devices payload")

        # Строим карту id -> AtmeexDevice
        device_map: dict[str, AtmeexDevice] = {
            str(d.id): d for d in device_objs}

        # Для обратной совместимости (диагностика, тесты) храним ещё и "плоские" dict’ы
        devices_raw: list[dict[str, Any]] = [d.to_ha_dict()
                                             for d in device_objs]

        # Мержим с предыдущими устройствами, чтобы не терять оффлайн-девайсы
        if getattr(coordinator, "last_update_success", False) and isinstance(
            getattr(coordinator, "data", None), dict
        ):
            # type: ignore[assignment]
            prev: AtmeexCoordinatorData = coordinator.data
            for d_raw in prev.get("devices", []):
                did = d_raw.get("id")
                if did is None:
                    continue
                key = str(did)
                if key not in device_map:
                    # Восстанавливаем AtmeexDevice из старого dict — best-effort
                    try:
                        device_map[key] = AtmeexDevice.from_raw(d_raw)
                        devices_raw.append(d_raw)
                    except Exception:
                        # если совсем всё плохо — хотя бы dict сохраним
                        devices_raw.append(d_raw)

        # --- строим нормализованные состояния через AtmeexState ---
        states: dict[str, dict[str, Any]] = {}
        for did, dev in device_map.items():
            try:
                ha_dict = dev.to_ha_dict()
                st = AtmeexState.from_device_dict(ha_dict)
            except Exception as e:
                _LOGGER.warning("Failed to normalize state for device %s: %s", did, e)
                continue
            states[did] = st.to_ha_dict()


        retry_count = getattr(api, "_retry_count", 0)

        data: AtmeexCoordinatorData = {
            "devices": devices_raw,
            "device_map": device_map,
            "states": states,
            "last_success_ts": time.time(),
            "avg_latency_ms": round(elapsed_ms, 1),
            "request_retries": retry_count,
        }

        # успех — сохраняем timestamp и сбрасываем ошибку
        setattr(coordinator, "last_success_ts", data["last_success_ts"])
        setattr(coordinator, "last_api_error", None)

        # успешный апдейт — обнуляем last error
        return data

    options = getattr(entry, "options", {}) or {}
    update_interval_seconds = int(options.get("update_interval", 30))

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_method=_async_update_data,
        update_interval=timedelta(seconds=update_interval_seconds),
    )

    setattr(coordinator, "last_api_error", None)
    setattr(coordinator, "last_success_ts", None)

    await coordinator.async_config_entry_first_refresh()

     # ВАЖНО: если пользователь поменял options — перезагрузить entry
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    async def refresh_device(device_id: int | str) -> None:
        """Дочитать одно устройство и обновить координатор (device_map + devices + states)."""
        try:
            full: AtmeexDevice = await api.get_device(device_id)
        except ApiError as e:
            _LOGGER.warning("Failed to refresh device %s: %s", device_id, e)
            return
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "Unexpected error in refresh_device(%s): %s", device_id, e)
            return

        # Текущее состояние координатора (fallback на пустую структуру)
        cur: AtmeexCoordinatorData = coordinator.data or {
            "devices": [],
            "device_map": {},
            "states": {},
            "last_success_ts": None,
            "avg_latency_ms": None,
            "request_retries": 0,
        }

        devices_raw: list[dict[str, Any]] = list(cur.get("devices", []))
        device_map: dict[str, AtmeexDevice] = dict(cur.get("device_map", {}))
        states: dict[str, dict[str, Any]] = dict(cur.get("states", {}))

        # Ключ по id устройства
        key = str(full.id)
        payload = full.to_ha_dict()

        # Обновляем device_map
        device_map[key] = full

        # Обновляем/добавляем запись в devices_raw (для обратной совместимости/диагностики)
        for idx, d in enumerate(devices_raw):
            if d.get("id") == full.id:
                devices_raw[idx] = payload
                break
        else:
            devices_raw.append(payload)

        # Пересчитываем нормализованное состояние
        try:
            st = AtmeexState.from_device_dict(payload)
            states[key] = st.to_ha_dict()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Failed to normalize refreshed state for %s: %s", device_id, e)

        # Применяем обновление к координатору, диагностические поля не трогаем
        coordinator.async_set_updated_data(
            {
                "devices": devices_raw,
                "device_map": device_map,
                "states": states,
                "last_success_ts": cur.get("last_success_ts"),
                "avg_latency_ms": cur.get("avg_latency_ms"),
                "request_retries": cur.get("request_retries", 0),
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
