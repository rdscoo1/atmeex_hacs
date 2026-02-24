from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypedDict, Callable, Awaitable

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import AtmeexApi, ApiError, AtmeexDevice, AtmeexState
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_ENABLE_WEBSOCKET,
    DEFAULT_ENABLE_WEBSOCKET,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
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
class PendingCommand:
    """Tracks a pending command to prevent stale state overwrites."""
    value: Any
    timestamp: float
    attribute: str  # e.g., "fan_speed", "pwr_on"


@dataclass
class AtmeexRuntimeData:
    """Единый runtime-объект для записи конфигурации."""
    api: AtmeexApi
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData]
    refresh_device: Callable[[int | str], Awaitable[None]]
    # Per-device locks to serialize set+refresh operations
    device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Per-device pending commands: device_id -> {attribute -> PendingCommand}
    pending_commands: dict[str, dict[str, PendingCommand]] = field(default_factory=dict)
    # WebSocket manager for real-time updates (optional, can be None for HTTP-only mode)
    websocket_manager: Any = None  # WebSocketManager | None
    # Task that performs initial WebSocket startup/retry bootstrap.
    websocket_start_task: asyncio.Task[None] | None = None

    def get_device_lock(self, device_id: int | str) -> asyncio.Lock:
        """Get or create a lock for the given device."""
        key = str(device_id)
        if key not in self.device_locks:
            self.device_locks[key] = asyncio.Lock()
        return self.device_locks[key]

    def set_pending(self, device_id: int | str, attribute: str, value: Any) -> float:
        """Record a pending command. Returns the timestamp."""
        key = str(device_id)
        ts = time.monotonic()
        if key not in self.pending_commands:
            self.pending_commands[key] = {}
        self.pending_commands[key][attribute] = PendingCommand(
            value=value, timestamp=ts, attribute=attribute
        )
        _LOGGER.debug(
            "Pending command set: device=%s attr=%s value=%s ts=%.3f",
            device_id, attribute, value, ts
        )
        return ts

    def get_pending(self, device_id: int | str, attribute: str) -> PendingCommand | None:
        """Get pending command if exists."""
        key = str(device_id)
        return self.pending_commands.get(key, {}).get(attribute)

    def clear_pending(self, device_id: int | str, attribute: str) -> None:
        """Clear a pending command after confirmation."""
        key = str(device_id)
        if key in self.pending_commands and attribute in self.pending_commands[key]:
            del self.pending_commands[key][attribute]
            _LOGGER.debug("Pending command cleared: device=%s attr=%s", device_id, attribute)

    def clear_pending_if_confirmed(
        self, device_id: int | str, attribute: str, confirmed_value: Any, tolerance: float = 5.0
    ) -> bool:
        """Clear pending if device confirmed the value or TTL expired.
        
        Returns True if the confirmed_value should be used (no stale pending).
        Returns False if there's a newer pending command that should take precedence.
        """
        pending = self.get_pending(device_id, attribute)
        if pending is None:
            return True  # No pending, use confirmed value
        
        age = time.monotonic() - pending.timestamp
        
        # If pending command is too old, clear it and use confirmed
        if age > tolerance:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command expired: device=%s attr=%s age=%.1fs",
                device_id, attribute, age
            )
            return True
        
        # If device confirmed our pending value, clear it
        if pending.value == confirmed_value:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command confirmed: device=%s attr=%s value=%s",
                device_id, attribute, confirmed_value
            )
            return True
        
        # Pending command is newer than this response - ignore stale data
        _LOGGER.debug(
            "Ignoring stale value: device=%s attr=%s confirmed=%s pending=%s age=%.1fs",
            device_id, attribute, confirmed_value, pending.value, age
        )
        return False


__all__ = [
    "async_setup_entry",
    "async_unload_entry",
    "async_get_options_flow",
    "AtmeexCoordinatorData",
    "AtmeexRuntimeData",
    "PendingCommand",
]


def _normalize_device_state(item: dict[str, Any]) -> dict[str, Any]:
    """Склеить condition + settings → нормализованное состояние (+ online)"""
    cond = dict(item.get("condition") or {})
    st = dict(item.get("settings") or {})

    # Питание
    pwr_cond = cond.get("pwr_on")
    pwr_settings = st.get("u_pwr_on")
    
    # Prefer condition.pwr_on if available, fallback to settings.u_pwr_on
    if pwr_cond is not None:
        pwr = to_bool(pwr_cond)
    elif pwr_settings is not None:
        pwr = to_bool(pwr_settings)
    else:
        pwr = False  # Default to off if no data available
    
    _LOGGER.debug(
        "Normalize pwr_on: condition=%s, settings=%s, result=%s",
        pwr_cond, pwr_settings, pwr
    )

    # Скорость вентилятора
    # API uses 0-6, we need to convert to HA 1-7
    from .helpers import api_to_fan_speed
    
    fan_raw = cond.get("fan_speed")
    u_fan_raw = st.get("u_fan_speed")
    
    _LOGGER.debug(
        "Normalize fan_speed: condition.fan_speed=%s, settings.u_fan_speed=%s, pwr=%s",
        fan_raw, u_fan_raw, pwr
    )
    
    # Prefer settings.u_fan_speed if condition.fan_speed is missing/None or 0 when device is on
    # This handles cases where condition data is stale but settings has the target speed
    if (
        (fan_raw is None or (fan_raw == 0 and pwr))
        and u_fan_raw is not None
        and pwr
    ):
        # Use settings speed
        fan = api_to_fan_speed(u_fan_raw)
    else:
        # Use condition speed (or None if missing)
        fan = api_to_fan_speed(fan_raw) if fan_raw is not None else None

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

    # meta - если API не вернул online, считаем что устройство оффлайн
    # это важно для корректного отображения состояния при физическом включении
    online = item.get("online")
    if online is not None:
        out["online"] = bool(online)
    else:
        # Если поле отсутствует, проверяем наличие свежих данных condition
        # Если condition есть и свежий - устройство онлайн
        has_condition = bool(cond and cond.get("time"))
        out["online"] = has_condition
    
    return out


def _resolve_update_interval_seconds(options: dict[str, Any]) -> int:
    """Normalize update interval from options with a safe bounded range."""
    try:
        raw_interval = int(options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    except (TypeError, ValueError):
        raw_interval = DEFAULT_UPDATE_INTERVAL
    return max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, raw_interval))


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
    update_interval_seconds = _resolve_update_interval_seconds(options)

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

    refresh_tasks: dict[str, asyncio.Task[None]] = {}

    async def _refresh_device_once(device_id: int | str) -> None:
        """Fetch one device and merge it into coordinator state."""
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

    async def refresh_device(device_id: int | str) -> None:
        """Refresh one device with per-device request coalescing."""
        key = str(device_id)

        in_flight = refresh_tasks.get(key)
        if in_flight and not in_flight.done():
            _LOGGER.debug(
                "Refresh for device %s is already running; awaiting existing task",
                device_id,
            )
            await in_flight
            return

        task = asyncio.create_task(_refresh_device_once(device_id))
        refresh_tasks[key] = task
        try:
            await task
        finally:
            if refresh_tasks.get(key) is task:
                refresh_tasks.pop(key, None)

    websocket_manager = None
    websocket_start_task: asyncio.Task[None] | None = None
    enable_websocket = bool(options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET))

    def _to_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _apply_condition_update(
        state: dict[str, Any], condition_data: dict[str, Any]
    ) -> dict[str, Any]:
        from .helpers import api_to_fan_speed

        updated = dict(state)

        if "pwr_on" in condition_data:
            updated["pwr_on"] = to_bool(condition_data["pwr_on"])

        if "fan_speed" in condition_data:
            updated["fan_speed"] = api_to_fan_speed(condition_data["fan_speed"])

        for field in ("temp_room", "temp_in", "hum_room", "co2_ppm", "damp_pos", "hum_stg"):
            if field in condition_data:
                parsed = _to_int(condition_data[field])
                if parsed is not None:
                    updated[field] = parsed

        if "no_water" in condition_data:
            updated["no_water"] = to_bool(condition_data["no_water"])

        if "time" in condition_data:
            updated["time"] = condition_data["time"]

        # Any received WS condition means device connection is alive right now.
        updated["online"] = True
        return updated

    def _apply_settings_update(
        state: dict[str, Any], settings_data: dict[str, Any]
    ) -> dict[str, Any]:
        from .helpers import api_to_fan_speed

        updated = dict(state)

        if "u_fan_speed" in settings_data:
            updated["u_fan_speed"] = api_to_fan_speed(settings_data["u_fan_speed"])

        if "u_pwr_on" in settings_data:
            updated["pwr_on"] = to_bool(settings_data["u_pwr_on"])

        if "u_temp_room" in settings_data:
            parsed = _to_int(settings_data["u_temp_room"])
            if parsed is not None:
                updated["u_temp_room"] = parsed

        if "u_hum_stg" in settings_data:
            parsed = _to_int(settings_data["u_hum_stg"])
            if parsed is not None:
                updated["hum_stg"] = parsed

        if "u_damp_pos" in settings_data:
            parsed = _to_int(settings_data["u_damp_pos"])
            if parsed is not None:
                updated["damp_pos"] = parsed

        updated["online"] = True
        return updated

    def _apply_websocket_message(message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type not in ("condition", "settings"):
            _LOGGER.debug("WebSocket message type '%s' ignored", msg_type)
            return

        payload = message.get("data")
        if not isinstance(payload, list):
            _LOGGER.warning("WebSocket message has unexpected data format: %s", payload)
            return

        cur: AtmeexCoordinatorData = coordinator.data or {
            "devices": [],
            "device_map": {},
            "states": {},
            "last_success_ts": None,
            "avg_latency_ms": None,
            "request_retries": 0,
        }
        device_map = cur.get("device_map", {}) or {}
        if not device_map:
            return

        states: dict[str, dict[str, Any]] = dict(cur.get("states", {}))
        changed = False

        for device_data in payload:
            if not isinstance(device_data, dict):
                continue

            device_id = device_data.get("id")
            if device_id is None:
                continue

            key = str(device_id)
            if key not in device_map:
                _LOGGER.debug(
                    "WebSocket: device %s not in current map, skipping message",
                    device_id,
                )
                continue

            if msg_type == "condition":
                source = device_data.get("condition")
                if not isinstance(source, dict) or not source:
                    continue
                updated_state = _apply_condition_update(states.get(key, {}), source)
            else:
                source = device_data.get("settings")
                if not isinstance(source, dict) or not source:
                    continue
                updated_state = _apply_settings_update(states.get(key, {}), source)

            if updated_state != states.get(key, {}):
                states[key] = updated_state
                changed = True

        if not changed:
            return

        coordinator.async_set_updated_data(
            {
                "devices": cur.get("devices", []),
                "device_map": dict(device_map),
                "states": states,
                "last_success_ts": cur.get("last_success_ts"),
                "avg_latency_ms": cur.get("avg_latency_ms"),
                "request_retries": cur.get("request_retries", 0),
            }
        )

    if enable_websocket:
        try:
            from .websocket import WebSocketManager

            def on_websocket_message(message: dict[str, Any]) -> None:
                """Handle WebSocket message and update coordinator state."""
                try:
                    _apply_websocket_message(message)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("Error processing WebSocket message: %s", err)

            if not hasattr(session, "ws_connect"):
                _LOGGER.warning("WebSocket skipped: HTTP session has no ws_connect()")
                websocket_manager = None
            elif not getattr(api, "_token", None):
                _LOGGER.warning("WebSocket skipped: API token is unavailable")
                websocket_manager = None
            else:
                websocket_manager = WebSocketManager(
                    session=session,
                    token=api._token,
                    on_message=on_websocket_message,
                )

                async def _start_websocket() -> None:
                    try:
                        success = await websocket_manager.connect()
                        if success:
                            _LOGGER.info("WebSocket connected for real-time updates")
                        else:
                            _LOGGER.warning(
                                "WebSocket bootstrap failed, reconnect loop will continue in background"
                            )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.warning(
                            "Failed to start WebSocket: %s. Using HTTP polling only.",
                            err,
                        )

                create_task = getattr(hass, "async_create_task", asyncio.create_task)
                start_task = create_task(_start_websocket())
                if isinstance(start_task, asyncio.Task):
                    websocket_start_task = start_task

        except ImportError:
            _LOGGER.warning("WebSocket module not available, using HTTP polling only")
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to initialize WebSocket: %s. Using HTTP polling only.", err)
    else:
        _LOGGER.info("WebSocket disabled in options, using HTTP polling only")
    
    runtime_data = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh_device,
        websocket_manager=websocket_manager,
        websocket_start_task=websocket_start_task,
    )
    entry.runtime_data = runtime_data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Atmeex Cloud config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    start_task = getattr(runtime, "websocket_start_task", None)
    if start_task and not start_task.done():
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            pass

    # Disconnect WebSocket if active
    if runtime and runtime.websocket_manager:
        try:
            await runtime.websocket_manager.disconnect()
            _LOGGER.info("WebSocket disconnected during unload")
        except Exception as err:
            _LOGGER.warning("Error disconnecting WebSocket: %s", err)
    
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


async def async_get_options_flow(config_entry: ConfigEntry):
    """Hook для options flow."""
    return AtmeexOptionsFlowHandler(config_entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    """Remove a device from the integration.
    
    This allows users to remove individual devices that are no longer
    connected or needed.
    """
    # We allow removal of any device - the device will reappear
    # on next poll if it's still connected to the account
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry to new version.
    
    This function handles config entry version upgrades when the
    integration schema changes.
    """
    _LOGGER.debug("Migrating from version %s", config_entry.version)

    if config_entry.version == 1:
        # Current version, no migration needed
        pass

    # Future migrations would go here:
    # if config_entry.version == 1:
    #     new_data = {**config_entry.data}
    #     # ... modify new_data ...
    #     hass.config_entries.async_update_entry(
    #         config_entry, data=new_data, version=2
    #     )

    _LOGGER.debug("Migration to version %s successful", config_entry.version)
    return True
