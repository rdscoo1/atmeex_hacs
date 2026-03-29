from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Awaitable

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import AtmeexApi, ApiError, AtmeexDevice, AtmeexState
from .coordinator import AtmeexCoordinator, AtmeexCoordinatorData
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_ENABLE_WEBSOCKET,
    DEFAULT_ENABLE_WEBSOCKET,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    EVENT_API_ERROR,
    EVENT_DEVICE_UPDATED,
    WS_LOGBOOK_MIN_INTERVAL_SEC,
)
from .helpers import apply_condition_update, apply_settings_update

_LOGGER = logging.getLogger(__name__)


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
    coordinator: AtmeexCoordinator
    refresh_device: Callable[[int | str], Awaitable[None]]
    # Per-device locks to serialize set+refresh operations
    device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Per-device pending commands: device_id -> {attribute -> PendingCommand}
    pending_commands: dict[str, dict[str, PendingCommand]] = field(default_factory=dict)
    # WebSocket manager for real-time updates (optional, can be None for HTTP-only mode)
    websocket_manager: Any = None  # WebSocketManager | None
    # Task that performs initial WebSocket startup/retry bootstrap.
    websocket_start_task: asyncio.Task[None] | None = None
    # Serialized task for queued websocket state updates.
    websocket_message_task: asyncio.Task[None] | None = None

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
    "AtmeexCoordinator",
    "AtmeexCoordinatorData",
    "AtmeexRuntimeData",
    "PendingCommand",
    "EVENT_API_ERROR",
    "EVENT_DEVICE_UPDATED",
]


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

    # Restore refresh token from previous session if available
    stored_refresh_token = entry.data.get("refresh_token")
    if stored_refresh_token:
        api._refresh_token = stored_refresh_token

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

    # Persist refresh token if the API returned one
    if api.refresh_token and api.refresh_token != stored_refresh_token:
        new_data = {**entry.data, "refresh_token": api.refresh_token}
        hass.config_entries.async_update_entry(entry, data=new_data)

    options = getattr(entry, "options", {}) or {}
    update_interval_seconds = _resolve_update_interval_seconds(options)
    runtime_data: AtmeexRuntimeData | None = None

    def _fire_logbook_event(event_type: str, data: dict[str, Any]) -> None:
        """Fire integration event for logbook if HA bus is available."""
        bus = getattr(hass, "bus", None)
        if bus is None or not hasattr(bus, "async_fire"):
            return
        bus.async_fire(event_type, data)

    # Throttle API-error logbook events to avoid flooding during outages.
    _api_error_last_ts: float = float("-inf")
    _api_error_suppressed: int = 0

    def _fire_api_error_event(data: dict[str, Any]) -> None:
        nonlocal _api_error_last_ts, _api_error_suppressed
        now = time.monotonic()
        if now - _api_error_last_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            _api_error_suppressed += 1
            return
        if _api_error_suppressed:
            data = {**data, "suppressed_errors": _api_error_suppressed}
            _api_error_suppressed = 0
        _fire_logbook_event(EVENT_API_ERROR, data)
        _api_error_last_ts = now

    coordinator: AtmeexCoordinator

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

        # Record monotonic time *before* the network round-trip so we can
        # detect WS updates that arrived while the poll was in-flight.
        poll_start_mono = time.monotonic()
        start_ts = time.perf_counter()
        try:
            device_objs = await _fetch_devices_safely()
        except ApiError as err:
            coordinator.last_api_error = err
            status = getattr(err, "status", None)
            _fire_api_error_event(
                {
                    "message": str(err),
                    "status": status,
                    "source": "coordinator_update",
                }
            )
            if status in (401, 403):
                # токен протух / креды поменяли → re-auth
                raise ConfigEntryAuthFailed(
                    f"Authentication with Atmeex failed during update: {err}"
                ) from err
            raise UpdateFailed(
                f"Error communicating with Atmeex API: {err}"
            ) from err
        except Exception as err:
            coordinator.last_api_error = None
            _fire_api_error_event(
                {"message": str(err), "source": "coordinator_update"}
            )
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

        # Preserve WS state for devices that received a fresher WebSocket
        # update while this poll was in-flight.  Without this, the poll
        # result (started *before* the WS message) would overwrite the
        # newer WS-pushed state.
        cur_data = coordinator.data
        if cur_data and isinstance(cur_data, dict):
            cur_states = cur_data.get("states") or {}
            for did, ws_ts in _ws_device_update_ts.items():
                if ws_ts >= poll_start_mono and did in cur_states and did in states:
                    _LOGGER.debug(
                        "Preserving fresher WS state for device %s "
                        "(ws_ts=%.3f >= poll_start=%.3f)",
                        did, ws_ts, poll_start_mono,
                    )
                    states[did] = cur_states[did]

        data: AtmeexCoordinatorData = {
            "devices": devices_raw,
            "device_map": device_map,
            "states": states,
            "last_success_ts": time.time(),
            "avg_latency_ms": round(elapsed_ms, 1),
            "request_retries": retry_count,
        }

        # успех — сохраняем timestamp и сбрасываем ошибку
        coordinator.last_success_ts = data["last_success_ts"]
        coordinator.last_api_error = None

        return data

    coordinator = AtmeexCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_method=_async_update_data,
        update_interval=timedelta(seconds=update_interval_seconds),
    )

    await coordinator.async_config_entry_first_refresh()

    # ВАЖНО: если пользователь поменял options — перезагрузить entry
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    refresh_tasks: dict[str, asyncio.Task[None]] = {}
    state_update_lock = asyncio.Lock()
    websocket_message_queue: deque[dict[str, Any]] = deque(maxlen=500)
    # Mutable container avoids nonlocal and keeps runtime_data in sync automatically.
    _ws_task_ref: dict[str, asyncio.Task[None] | None] = {"task": None}
    ws_logbook_last_event_ts: float = float("-inf")
    ws_logbook_suppressed_updates = 0
    # Per-device monotonic timestamp of last WS state update — used to prevent
    # polling from overwriting fresher WS data.
    _ws_device_update_ts: dict[str, float] = {}

    def _create_background_task(coro: Awaitable[None]) -> asyncio.Task[None]:
        """Create background task via HA scheduler."""
        return hass.async_create_task(coro)

    def _fire_websocket_device_updated(
        changed_device_ids: list[str],
        msg_type: Any,
    ) -> None:
        """Fire throttled websocket device-updated event for logbook."""
        nonlocal ws_logbook_last_event_ts, ws_logbook_suppressed_updates

        if not changed_device_ids:
            return

        now = time.monotonic()
        if now - ws_logbook_last_event_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            ws_logbook_suppressed_updates += len(changed_device_ids)
            return

        cur_map = (coordinator.data or {}).get("device_map", {})
        device_names = [
            cur_map[did].name
            for did in changed_device_ids
            if did in cur_map and hasattr(cur_map[did], "name")
        ]
        payload: dict[str, Any] = {
            "device_ids": changed_device_ids,
            "device_names": device_names or None,
            "source": "websocket",
            "message_type": msg_type,
        }
        if ws_logbook_suppressed_updates:
            payload["suppressed_updates"] = ws_logbook_suppressed_updates
            ws_logbook_suppressed_updates = 0

        _fire_logbook_event(EVENT_DEVICE_UPDATED, payload)
        ws_logbook_last_event_ts = now

    async def _refresh_device_once(device_id: int | str) -> None:
        """Fetch one device and merge it into coordinator state."""
        try:
            full: AtmeexDevice = await api.get_device(device_id)
        except ApiError as e:
            _LOGGER.warning("Failed to refresh device %s: %s", device_id, e)
            _fire_api_error_event(
                {
                    "message": str(e),
                    "status": getattr(e, "status", None),
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            return
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning(
                "Unexpected error in refresh_device(%s): %s", device_id, e)
            _fire_api_error_event(
                {
                    "message": str(e),
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            return

        # Ключ по id устройства
        key = str(full.id)
        payload = full.to_ha_dict()

        # Пересчитываем нормализованное состояние
        normalized_state: dict[str, Any] | None = None
        try:
            st = AtmeexState.from_device_dict(payload)
            normalized_state = st.to_ha_dict()
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Failed to normalize refreshed state for %s: %s", device_id, e)

        # Serialize coordinator writes to avoid lost updates vs websocket updates.
        async with state_update_lock:
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

            # Обновляем device_map
            device_map[key] = full

            # Обновляем/добавляем запись в devices_raw (для обратной совместимости/диагностики)
            for idx, d in enumerate(devices_raw):
                if d.get("id") == full.id:
                    devices_raw[idx] = payload
                    break
            else:
                devices_raw.append(payload)

            if normalized_state is not None:
                states[key] = normalized_state

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
            device_name = full.name if hasattr(full, "name") else None
            _fire_logbook_event(
                EVENT_DEVICE_UPDATED,
                {"device_id": key, "device_name": device_name, "source": "refresh_device"},
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

    async def _apply_websocket_message(message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type not in ("condition", "settings"):
            _LOGGER.debug("WebSocket message type '%s' ignored", msg_type)
            return

        payload = message.get("data")
        if not isinstance(payload, list):
            _LOGGER.warning("WebSocket message has unexpected data format: %s", payload)
            return

        async with state_update_lock:
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
            changed_device_ids: list[str] = []

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
                    updated_state = apply_condition_update(states.get(key, {}), source)
                else:
                    source = device_data.get("settings")
                    if not isinstance(source, dict) or not source:
                        continue
                    updated_state = apply_settings_update(states.get(key, {}), source)

                if updated_state != states.get(key, {}):
                    states[key] = updated_state
                    changed = True
                    changed_device_ids.append(key)

            if not changed:
                return

            # Record per-device WS update timestamp so polling knows not to
            # overwrite this fresher state.
            ws_now = time.monotonic()
            for did in changed_device_ids:
                _ws_device_update_ts[did] = ws_now

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
            _fire_websocket_device_updated(changed_device_ids, msg_type)

    if enable_websocket:
        try:
            from .websocket import WebSocketManager

            async def _drain_websocket_messages() -> None:
                """Process queued websocket messages in order using one task."""
                try:
                    while websocket_message_queue:
                        message = websocket_message_queue.popleft()
                        try:
                            await _apply_websocket_message(message)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.error("Error processing WebSocket message: %s", err)
                finally:
                    _ws_task_ref["task"] = None
                    if runtime_data is not None:
                        runtime_data.websocket_message_task = None

            def on_websocket_message(message: dict[str, Any]) -> None:
                """Queue websocket message for serialized coordinator updates."""
                websocket_message_queue.append(message)
                task = _ws_task_ref["task"]
                if task and not task.done():
                    return
                new_task = _create_background_task(_drain_websocket_messages())
                _ws_task_ref["task"] = new_task
                if runtime_data is not None:
                    runtime_data.websocket_message_task = new_task

            ws_reauth_started = False

            def _on_ws_auth_failure() -> None:
                """Start config-entry reauth when WS token becomes invalid."""
                nonlocal ws_reauth_started
                if ws_reauth_started:
                    return
                ws_reauth_started = True

                _LOGGER.warning(
                    "WebSocket auth rejected; starting config-entry reauth flow"
                )

                start_reauth = getattr(entry, "async_start_reauth", None)
                if not callable(start_reauth):
                    _LOGGER.error(
                        "Config entry has no async_start_reauth; WS auth failure cannot trigger reauth"
                    )
                    return

                try:
                    start_reauth(hass)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Failed to start reauth after WebSocket auth failure: %s",
                        err,
                    )

            if not hasattr(session, "ws_connect"):
                _LOGGER.warning("WebSocket skipped: HTTP session has no ws_connect()")
                websocket_manager = None
            elif not api.token:
                _LOGGER.warning("WebSocket skipped: API token is unavailable")
                websocket_manager = None
            else:
                websocket_manager = WebSocketManager(
                    session=session,
                    token_getter=lambda: api.token,
                    on_message=on_websocket_message,
                    on_auth_failure=_on_ws_auth_failure,
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

                websocket_start_task = _create_background_task(_start_websocket())

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

    message_task = getattr(runtime, "websocket_message_task", None)
    if message_task and not message_task.done():
        message_task.cancel()
        try:
            await message_task
        except asyncio.CancelledError:
            pass

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
