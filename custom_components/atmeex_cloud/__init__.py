from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import timedelta
from typing import Any, Coroutine

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
from .command_executor import AtmeexCommandExecutor, PendingCommand
from .compat import async_create_background_task
from .state_store import AtmeexStateStore

_LOGGER = logging.getLogger(__name__)

# Upper-bound for awaiting WS task cancellation during entry unload.
# asyncio.wait returns after this limit even if the task is stuck in finally.
_UNLOAD_TASK_TIMEOUT_SEC: float = 5.0

# Upper-bound for awaiting a coalesced in-flight refresh_device task.
# Sized to cover all retries (RETRY_MAX_ATTEMPTS=3 × 20 s timeout + headroom).
# Module-level so tests can monkeypatch it without touching the closure.
_REFRESH_TASK_TIMEOUT_SEC: float = 65.0

from .runtime import AtmeexRuntimeData

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


async def _async_cleanup_runtime(runtime: AtmeexRuntimeData) -> None:
    """Stop one entry runtime and bound cancellation of all owned work."""
    runtime.stopping = True
    current = asyncio.current_task()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _UNLOAD_TASK_TIMEOUT_SEC
    timed_out: set[asyncio.Task[Any]] = set()

    async def cancel_and_wait(
        tasks: set[asyncio.Task[Any]],
    ) -> set[asyncio.Task[Any]]:
        for task in tasks:
            task.cancel()
        remaining = deadline - loop.time()
        if remaining <= 0:
            return tasks
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in done:
            if not task.cancelled():
                task.exception()
        runtime.tasks.difference_update(done)
        return pending

    try:
        startup = runtime.websocket_start_task
        if (
            startup is not None
            and startup is not current
            and not startup.done()
        ):
            timed_out.update(await cancel_and_wait({startup}))

        manager = runtime.websocket_manager
        if manager is not None:
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                async with asyncio.timeout(remaining):
                    await manager.disconnect()
            except TimeoutError:
                _LOGGER.warning(
                    "Atmeex WebSocket cleanup exceeded the unload timeout"
                )
            except Exception as err:
                _LOGGER.warning(
                    "Atmeex WebSocket cleanup failed: %s",
                    type(err).__name__,
                )

        while True:
            tasks = {
                task
                for task in runtime.tasks
                if task is not current and not task.done()
            }
            if not tasks:
                break
            pending = await cancel_and_wait(tasks)
            if pending:
                timed_out.update(pending)
                break
            # Let task callbacks and cancellation-finally children publish
            # their ownership before taking the next snapshot.
            await asyncio.sleep(0)

        if timed_out:
            _LOGGER.warning(
                "%d Atmeex tasks exceeded the unload timeout",
                len(timed_out),
            )
    finally:
        completed_tasks = {
            task for task in runtime.tasks if task.done()
        }
        runtime.tasks.difference_update(completed_tasks)
        runtime.refresh_tasks.clear()
        runtime.websocket_start_task = None
        runtime.websocket_message_task = None
        runtime.websocket_resync_task = None
        runtime.inventory_watchdog_task = None


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
        api=api,
        state_store=state_store,
        config_entry_id=entry.entry_id,
        config_entry=entry,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=update_interval_seconds),
        fire_logbook_event=_fire_logbook_event,
    )

    await coordinator.async_config_entry_first_refresh()

    websocket_messages: deque[dict[str, Any]] = deque()
    websocket_limit = 500
    overflow_resync_running = False
    overflow_epoch = 0
    ws_logbook_last_event_ts: float = float("-inf")
    ws_logbook_suppressed_updates = 0
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

    async def _recover_after_targeted_failure(
        device_id: int | str,
    ) -> None:
        if (
            runtime_data is None
            or runtime_data.stopping
            or runtime_data.command_executor is None
        ):
            return
        async with runtime_data.get_device_lock(device_id):
            if runtime_data.stopping:
                return
            inventory_success_before = coordinator.last_inventory_success_mono
            await coordinator.async_request_refresh()
            if runtime_data.stopping:
                return
            inventory_success_after = coordinator.last_inventory_success_mono
            if (
                coordinator.last_update_success is not True
                or coordinator.last_api_error is not None
                or inventory_success_after is None
                or (
                    inventory_success_before is not None
                    and inventory_success_after <= inventory_success_before
                )
            ):
                return
            runtime_data.command_executor.allow_recovery_confirmation(device_id)
            # The coordinator published before confirmation tickets were enabled;
            # notify once more without replacing the comparable snapshot.
            coordinator.async_update_listeners()

    def _report_targeted_failure(
        device_id: int | str,
        err: AtmeexApiError,
    ) -> None:
        """Publish one safe failure and schedule authoritative recovery."""
        _LOGGER.warning("Failed to refresh device %s: %s", device_id, err)
        coordinator._fire_api_error_event(
            {
                "message": str(err),
                "operation": err.operation,
                "status": err.status,
                "source": "refresh_device",
                "device_id": str(device_id),
            }
        )
        _create_entry_task(
            _recover_after_targeted_failure(device_id),
            "atmeex targeted-refresh recovery",
        )

    async def _refresh_device_once(device_id: int | str) -> None:
        """Fetch one device and merge it into coordinator state."""
        if runtime_data is None or runtime_data.stopping:
            return
        baseline = runtime_data.state_store.capture_device(device_id)
        unexpected_failure: AtmeexProtocolError | None = None
        try:
            full: AtmeexDevice = await api.get_device(device_id)
        except AtmeexApiError as err:
            _report_targeted_failure(device_id, err)
            raise
        except Exception:
            unexpected_failure = AtmeexProtocolError(
                "get_device",
                "unexpected client failure",
            )
        if unexpected_failure is not None:
            # Report outside the active exception handler so neither this
            # error nor a scheduler failure retains private client context.
            _report_targeted_failure(device_id, unexpected_failure)
            raise unexpected_failure
        if runtime_data.stopping:
            return
        update = runtime_data.state_store.apply_refresh(full, baseline)
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
        if runtime_data is None or runtime_data.stopping:
            return
        key = normalize_device_id(device_id)
        owner = runtime_data.refresh_tasks.get(key)
        if owner is None or owner.done():
            owner = _create_entry_task(
                _refresh_device_once(device_id),
                f"atmeex refresh {key}",
            )
            runtime_data.refresh_tasks[key] = owner

            def remove_owner(done: asyncio.Task[None]) -> None:
                if runtime_data.refresh_tasks.get(key) is done:
                    runtime_data.refresh_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            owner.add_done_callback(remove_owner)
        await asyncio.wait_for(
            asyncio.shield(owner),
            timeout=_REFRESH_TASK_TIMEOUT_SEC,
        )

    command_executor = AtmeexCommandExecutor(refresh_device)
    runtime_data = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh_device,
        state_store=state_store,
        command_executor=command_executor,
    )
    entry.runtime_data = runtime_data

    def _create_entry_task(
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        if runtime_data.stopping:
            coro.close()
            raise RuntimeError("Atmeex runtime is stopping")
        try:
            task = async_create_background_task(hass, coro, name)
        except BaseException:
            coro.close()
            raise
        return runtime_data.track_task(task)

    async def _async_rollback_setup(*, unload_platforms: bool) -> None:
        """Rollback setup without replacing the original setup failure."""
        if unload_platforms:
            try:
                unload_ok = await hass.config_entries.async_unload_platforms(
                    entry,
                    PLATFORMS,
                )
                if not unload_ok:
                    _LOGGER.warning(
                        "Atmeex platform rollback returned false"
                    )
            except BaseException as err:
                _LOGGER.warning(
                    "Atmeex platform rollback failed: %s",
                    type(err).__name__,
                )
        try:
            await _async_cleanup_runtime(runtime_data)
        except BaseException as err:
            _LOGGER.warning(
                "Atmeex runtime rollback failed: %s",
                type(err).__name__,
            )
        finally:
            entry.runtime_data = None

    websocket_manager = None
    websocket_reauth_started = False
    enable_websocket = bool(options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET))

    if enable_websocket:
        try:
            from .websocket import WebSocketManager
        except BaseException:
            await _async_rollback_setup(unload_platforms=False)
            raise
        else:
            async def _overflow_resync() -> None:
                """Run one authoritative refresh after queue overflow."""
                nonlocal overflow_resync_running
                try:
                    # Keep owner publication deterministic for eager task APIs.
                    await asyncio.sleep(0)
                    while not runtime_data.stopping:
                        refresh_epoch = overflow_epoch
                        await coordinator.async_request_refresh()
                        if (
                            runtime_data.stopping
                            or overflow_epoch == refresh_epoch
                        ):
                            return
                finally:
                    overflow_resync_running = False
                    runtime_data.websocket_resync_task = None

            def _schedule_overflow_resync() -> None:
                """Schedule at most one entry-owned overflow recovery."""
                nonlocal overflow_resync_running
                if overflow_resync_running or runtime_data.stopping:
                    return
                overflow_resync_running = True
                try:
                    task = _create_entry_task(
                        _overflow_resync(),
                        "atmeex websocket overflow resync",
                    )
                except BaseException:
                    overflow_resync_running = False
                    runtime_data.websocket_resync_task = None
                    raise
                runtime_data.websocket_resync_task = task

            async def _drain_websocket_messages() -> None:
                """Merge one same-turn message batch and publish at most once."""
                try:
                    # Home Assistant may eagerly start background tasks. Yield
                    # before snapshotting so a synchronous burst can coalesce.
                    await asyncio.sleep(0)
                    if runtime_data.stopping:
                        return

                    batch = list(websocket_messages)
                    websocket_messages.clear()
                    working = {
                        key: dict(value)
                        for key, value in runtime_data.state_store.data[
                            "states"
                        ].items()
                    }
                    state_deltas: dict[str, dict[str, Any]] = {}
                    device_deltas: dict[str, dict[str, Any]] = {}
                    message_types_by_device: dict[str, set[str]] = {}

                    def merge_device_delta(
                        target: dict[str, Any],
                        incoming: dict[str, Any],
                    ) -> None:
                        for field, value in incoming.items():
                            if field in ("condition", "settings") and isinstance(
                                value,
                                dict,
                            ):
                                target.setdefault(field, {}).update(value)
                            else:
                                target[field] = value

                    for message in batch:
                        message_type = message["type"]
                        source_name = (
                            "condition"
                            if message_type == "condition"
                            else "settings"
                        )
                        for item in message["data"]:
                            if not isinstance(item, dict) or item.get("id") is None:
                                continue
                            source = item.get(source_name)
                            if not isinstance(source, dict) or not source:
                                continue
                            try:
                                key = normalize_device_id(item["id"])
                            except ValueError:
                                continue
                            if key not in working:
                                continue
                            state_delta, device_delta = (
                                normalize_condition_delta(source)
                                if message_type == "condition"
                                else normalize_settings_delta(source, working[key])
                            )
                            # Keep accepted same-value fields so their store
                            # revisions still defeat an older HTTP snapshot.
                            state_deltas.setdefault(key, {}).update(state_delta)
                            merge_device_delta(
                                device_deltas.setdefault(key, {}),
                                device_delta,
                            )
                            working[key].update(state_delta)
                            message_types_by_device.setdefault(key, set()).add(
                                message_type
                            )

                    changed_device_ids: list[str] = []
                    for key, state_delta in state_deltas.items():
                        update = runtime_data.state_store.apply_websocket_delta(
                            key,
                            state_delta=state_delta,
                            device_delta=device_deltas.get(key),
                        )
                        if update.changed:
                            changed_device_ids.append(key)

                    if changed_device_ids and not runtime_data.stopping:
                        coordinator.async_set_updated_data(
                            runtime_data.state_store.data
                        )
                        represented_types: set[str] = set()
                        for key in changed_device_ids:
                            represented_types.update(
                                message_types_by_device.get(key, set())
                            )
                        event_message_type = (
                            next(iter(represented_types))
                            if len(represented_types) == 1
                            else "mixed"
                        )
                        _fire_websocket_device_updated(
                            changed_device_ids,
                            event_message_type,
                        )
                finally:
                    runtime_data.websocket_message_task = None
                    if websocket_messages and not runtime_data.stopping:
                        runtime_data.websocket_message_task = _create_entry_task(
                            _drain_websocket_messages(),
                            "atmeex websocket drain",
                        )

            def on_websocket_message(message: dict[str, Any]) -> bool:
                """Admit one valid message without silently evicting work."""
                nonlocal overflow_epoch
                if runtime_data.stopping:
                    return False
                if message.get("type") not in ("condition", "settings"):
                    return False
                if not isinstance(message.get("data"), list):
                    return False
                if len(websocket_messages) >= websocket_limit:
                    runtime_data.websocket_overflow_count += 1
                    overflow_epoch += 1
                    _schedule_overflow_resync()
                    return False
                websocket_messages.append(message)
                task = runtime_data.websocket_message_task
                if task is None or task.done():
                    runtime_data.websocket_message_task = _create_entry_task(
                        _drain_websocket_messages(),
                        "atmeex websocket drain",
                    )
                return True

            def _on_ws_auth_failure() -> None:
                """Start config-entry reauth at most once while runtime is active."""
                nonlocal websocket_reauth_started
                if websocket_reauth_started or runtime_data.stopping:
                    return
                websocket_reauth_started = True
                entry.async_start_reauth(hass)

            try:
                if not hasattr(session, "ws_connect"):
                    _LOGGER.warning(
                        "WebSocket skipped: HTTP session has no ws_connect()"
                    )
                    websocket_manager = None
                elif not api.token:
                    _LOGGER.warning("WebSocket skipped: API token is unavailable")
                    websocket_manager = None
                else:
                    websocket_manager = WebSocketManager(
                        session=session,
                        token_getter=lambda: api.token,
                        on_message=on_websocket_message,
                        task_factory=_create_entry_task,
                        on_auth_failure=_on_ws_auth_failure,
                        on_token_refresh=api.async_refresh_access_token,
                    )
            except BaseException:
                await _async_rollback_setup(unload_platforms=False)
                raise
    else:
        _LOGGER.info("WebSocket disabled in options, using HTTP polling only")

    runtime_data.websocket_manager = websocket_manager

    platform_forward_attempted = False
    remove_update_listener = None
    try:
        platform_forward_attempted = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        manager = runtime_data.websocket_manager
        if manager is not None:
            async def _start_websocket() -> None:
                if runtime_data.stopping:
                    return
                success = await manager.connect()
                if runtime_data.stopping:
                    try:
                        await manager.disconnect()
                    except Exception as err:
                        _LOGGER.warning(
                            "Late Atmeex WebSocket cleanup failed: %s",
                            type(err).__name__,
                        )
                    return
                if success:
                    _LOGGER.info("WebSocket connected for real-time updates")
                else:
                    _LOGGER.warning(
                        "WebSocket bootstrap failed; reconnect remains active"
                    )

            runtime_data.websocket_start_task = _create_entry_task(
                _start_websocket(),
                "atmeex websocket startup",
            )

        watchdog = getattr(coordinator, "async_inventory_watchdog", None)
        if callable(watchdog):
            runtime_data.inventory_watchdog_task = _create_entry_task(
                watchdog(),
                "atmeex inventory watchdog",
            )

        remove_update_listener = entry.add_update_listener(_update_listener)
        entry.async_on_unload(remove_update_listener)
    except BaseException:
        if remove_update_listener is not None:
            try:
                remove_update_listener()
            except BaseException as err:
                _LOGGER.warning(
                    "Atmeex update-listener rollback failed: %s",
                    type(err).__name__,
                )
        await _async_rollback_setup(
            unload_platforms=platform_forward_attempted,
        )
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Atmeex Cloud config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )
    if not unloaded:
        return False
    try:
        await _async_cleanup_runtime(runtime)
    finally:
        entry.runtime_data = None
    return True


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
            if runtime.command_executor is not None:
                runtime.command_executor.remove_device(ident)

    # The device will reappear on next poll if it's still connected to the account.
    return True
