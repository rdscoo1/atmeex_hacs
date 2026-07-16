from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import Any, Awaitable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import (
    AtmeexApi,
    AtmeexApiError,
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
    AtmeexRateLimitError,
)
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
    CONF_AUTH_METHOD,
    CONF_PHONE,
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_PHONE,
)
from .helpers import (
    normalize_condition_delta,
    normalize_device_id,
    normalize_settings_delta,
)
from .state_store import AtmeexStateStore

_LOGGER = logging.getLogger(__name__)

# Upper-bound for awaiting WS task cancellation during entry unload.
# asyncio.wait returns after this limit even if the task is stuck in finally.
_UNLOAD_TASK_TIMEOUT_SEC: float = 5.0

# Upper-bound for awaiting a coalesced in-flight refresh_device task.
# Sized to cover all retries (RETRY_MAX_ATTEMPTS=3 × 20 s timeout + headroom).
# Module-level so tests can monkeypatch it without touching the closure.
_REFRESH_TASK_TIMEOUT_SEC: float = 65.0

from .runtime import PendingCommand, AtmeexRuntimeData

__all__ = [
    "async_setup_entry",
    "async_unload_entry",
    "AtmeexCoordinator",
    "AtmeexCoordinatorData",
    "AtmeexRuntimeData",
    "PendingCommand",  # re-exported: tests import from here for convenience
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

    # Default to email for entries created before phone login was supported.
    auth_method = entry.data.get(CONF_AUTH_METHOD, AUTH_METHOD_EMAIL)

    stored_refresh_token = entry.data.get("refresh_token")

    def _persist_refresh_token(refresh_token: str) -> None:
        if entry.data.get("refresh_token") == refresh_token:
            return
        new_data = {**entry.data, "refresh_token": refresh_token}
        try:
            hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception:
            _LOGGER.warning("Failed to persist rotated Atmeex refresh token")

    api = AtmeexApi(
        session,
        on_refresh_token_changed=_persist_refresh_token,
    )
    await api.async_init()

    # Restore refresh token from previous session if available.
    if stored_refresh_token:
        api.restore_refresh_token(stored_refresh_token)

    try:
        if auth_method == AUTH_METHOD_PHONE:
            # Phone accounts cannot replay sign-in; refresh_token is the
            # only path back to a valid access token across restarts.
            if not stored_refresh_token:
                raise ConfigEntryAuthFailed(
                    "Phone account requires re-verification (no refresh token stored)"
                )
            await api.authenticate_phone()
        else:
            email = entry.data.get(CONF_EMAIL)
            password = entry.data.get(CONF_PASSWORD)
            if not email or not password:
                raise ConfigEntryAuthFailed(
                    "Email account is missing credentials in config entry"
                )
            await api.login(email, password)
    except AtmeexAuthenticationError as err:
        raise ConfigEntryAuthFailed("Atmeex authentication failed") from err
    except (
        AtmeexConnectionError,
        AtmeexRateLimitError,
        AtmeexProtocolError,
    ) as err:
        raise ConfigEntryNotReady("Cannot connect to Atmeex Cloud") from err

    options = getattr(entry, "options", {}) or {}
    update_interval_seconds = _resolve_update_interval_seconds(options)
    runtime_data: AtmeexRuntimeData | None = None

    def _fire_logbook_event(event_type: str, data: dict[str, Any]) -> None:
        """Fire integration event for logbook if HA bus is available."""
        bus = getattr(hass, "bus", None)
        if bus is None or not hasattr(bus, "async_fire"):
            return
        bus.async_fire(event_type, data)

    coordinator: AtmeexCoordinator

    options_snapshot = dict(entry.options)

    async def _update_listener(
        hass: HomeAssistant,
        updated_entry: ConfigEntry,
    ) -> None:
        nonlocal options_snapshot
        current_options = dict(updated_entry.options)
        if current_options == options_snapshot:
            return
        options_snapshot = current_options
        await hass.config_entries.async_reload(updated_entry.entry_id)

    state_store = AtmeexStateStore()
    coordinator = AtmeexCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=update_interval_seconds),
    )
    coordinator.setup_update(
        api=api,
        state_store=state_store,
        fire_logbook_event=_fire_logbook_event,
    )

    await coordinator.async_config_entry_first_refresh()

    # ВАЖНО: если пользователь поменял options — перезагрузить entry
    entry.async_on_unload(entry.add_update_listener(_update_listener))

    refresh_tasks: dict[str, asyncio.Task[None]] = {}
    websocket_message_queue: deque[dict[str, Any]] = deque(maxlen=500)
    # Mutable container avoids nonlocal and keeps runtime_data in sync automatically.
    _ws_task_ref: dict[str, asyncio.Task[None] | None] = {"task": None}
    ws_logbook_last_event_ts: float = float("-inf")
    ws_logbook_suppressed_updates = 0
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
        baseline = state_store.capture_device(device_id)
        try:
            full: AtmeexDevice = await api.get_device(device_id)
            update = state_store.apply_refresh(full, baseline)
        except AtmeexApiError as err:
            _LOGGER.warning("Failed to refresh device %s: %s", device_id, err)
            coordinator._fire_api_error_event(
                {
                    "message": str(err),
                    "status": err.status,
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            recovery_coro = coordinator.async_request_refresh()
            try:
                hass.async_create_task(
                    recovery_coro,
                    name="atmeex targeted-refresh recovery",
                )
            except BaseException:
                recovery_coro.close()
                raise
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Unexpected error in refresh_device(%s): %s", device_id, err
            )
            coordinator._fire_api_error_event(
                {
                    "message": str(err),
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            return
        if not update.changed:
            return
        coordinator.async_set_updated_data(update.data)
        _fire_logbook_event(
            EVENT_DEVICE_UPDATED,
            {
                "device_id": full.id,
                "device_name": full.name,
                "source": "refresh_device",
            },
        )

    async def refresh_device(device_id: int | str) -> None:
        """Refresh one device with per-device request coalescing."""
        key = normalize_device_id(device_id)

        in_flight = refresh_tasks.get(key)
        if in_flight and not in_flight.done():
            _LOGGER.debug(
                "Refresh for device %s is already running; awaiting existing task",
                device_id,
            )
            try:
                await asyncio.wait_for(in_flight, timeout=_REFRESH_TASK_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "In-flight refresh for device %s timed out; evicting stuck task",
                    device_id,
                )
                refresh_tasks.pop(key, None)
                raise
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

        changed_device_ids: list[str] = []
        for device_data in payload:
            if not isinstance(device_data, dict) or "id" not in device_data:
                continue
            try:
                key = normalize_device_id(device_data["id"])
            except ValueError:
                continue
            current_state = state_store.data.get("states", {}).get(key, {})
            if msg_type == "condition":
                source = device_data.get("condition")
                if not isinstance(source, dict) or not source:
                    continue
                state_delta, device_delta = normalize_condition_delta(source)
            else:
                source = device_data.get("settings")
                if not isinstance(source, dict) or not source:
                    continue
                state_delta, device_delta = normalize_settings_delta(
                    source,
                    current_state,
                )
            update = state_store.apply_websocket_delta(
                key,
                state_delta=state_delta,
                device_delta=device_delta,
            )
            if update.changed:
                changed_device_ids.append(key)

        if changed_device_ids:
            coordinator.async_set_updated_data(state_store.data)
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

            # Throttle WS reauth prompts: at most once per 5 minutes so that a
            # successful reconnect followed by another auth failure still shows the prompt.
            _WS_REAUTH_COOLDOWN_SEC = 300.0
            ws_reauth_last_ts: float = float("-inf")

            def _on_ws_auth_failure() -> None:
                """Start config-entry reauth when WS token becomes invalid."""
                nonlocal ws_reauth_last_ts
                now = time.monotonic()
                if now - ws_reauth_last_ts < _WS_REAUTH_COOLDOWN_SEC:
                    return
                ws_reauth_last_ts = now

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
                    on_token_refresh=coordinator.async_request_refresh,
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
        state_store=state_store,
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
        _, pending = await asyncio.wait({message_task}, timeout=_UNLOAD_TASK_TIMEOUT_SEC)
        if pending:
            _LOGGER.warning("WS message task did not finish within %.1fs; abandoning", _UNLOAD_TASK_TIMEOUT_SEC)

    start_task = getattr(runtime, "websocket_start_task", None)
    if start_task and not start_task.done():
        start_task.cancel()
        _, pending = await asyncio.wait({start_task}, timeout=_UNLOAD_TASK_TIMEOUT_SEC)
        if pending:
            _LOGGER.warning("WS start task did not finish within %.1fs; abandoning", _UNLOAD_TASK_TIMEOUT_SEC)

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
    connected or needed. Per-device runtime state (locks, pending commands)
    is dropped here so it doesn't accumulate across add/remove cycles.
    """
    runtime: AtmeexRuntimeData | None = getattr(config_entry, "runtime_data", None)
    if runtime is not None:
        for domain, ident in device_entry.identifiers:
            if domain != DOMAIN:
                continue
            key = str(ident)
            runtime.pending_commands.pop(key, None)
            runtime.device_locks.pop(key, None)

    # The device will reappear on next poll if it's still connected to the account.
    return True
