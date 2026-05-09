import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from custom_components.atmeex_cloud.helpers import to_bool, _normalize_device_state
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


async def test_async_unload_entry_clears_data(monkeypatch):
    hass = SimpleNamespace(
        data={DOMAIN: {"entry1": {"some": "data"}}},
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=SimpleNamespace(websocket_manager=None)
    )

    result = await atmeex_init.async_unload_entry(hass, entry)
    assert result is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)

async def test_unload_entry_hung_task_does_not_block_unload(monkeypatch):
    """Cancellation of WS tasks during unload must have a timeout.

    If await message_task or await start_task hangs (e.g. slow TLS teardown
    in a finally block), async_unload_entry would also block HA's reload
    watchdog indefinitely.  The fix wraps each await in asyncio.wait_for.
    """
    gate = asyncio.Event()  # open in cleanup to unblock hanging task

    async def _hung_task():
        try:
            await asyncio.sleep(9999)
        finally:
            await gate.wait()  # simulate slow cleanup

    task = asyncio.create_task(_hung_task())
    await asyncio.sleep(0)  # let task start

    # Patch the timeout to something tiny so the test runs fast
    monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.05)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=SimpleNamespace(
            websocket_manager=None,
            websocket_message_task=task,
            websocket_start_task=None,
        ),
    )

    # Must complete quickly rather than hanging on await task
    result = await asyncio.wait_for(atmeex_init.async_unload_entry(hass, entry), timeout=5.0)
    assert result is True

    # Cleanup: open the gate so the hung task can exit cleanly
    gate.set()
    await asyncio.gather(task, return_exceptions=True)


async def test_unload_entry_disconnect_error_is_logged_not_raised(caplog):
    websocket_manager = SimpleNamespace(
        disconnect=AsyncMock(side_effect=RuntimeError("boom")),
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=SimpleNamespace(
            websocket_manager=websocket_manager,
            websocket_message_task=None,
            websocket_start_task=None,
        ),
    )

    with caplog.at_level(logging.WARNING):
        result = await atmeex_init.async_unload_entry(hass, entry)

    assert result is True
    assert "Error disconnecting WebSocket: boom" in caplog.text

async def test_async_remove_config_entry_device_drops_per_device_state():
    """Device removal must evict per-device locks and pending commands.

    Without this cleanup, runtime.device_locks / runtime.pending_commands grow
    unboundedly across add/remove cycles for the lifetime of the loaded entry.
    """
    from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData, PendingCommand

    runtime = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    runtime.device_locks["42"] = asyncio.Lock()
    runtime.device_locks["99"] = asyncio.Lock()
    runtime.pending_commands["42"] = {"pwr_on": PendingCommand(value=True, timestamp=0.0, attribute="pwr_on")}
    runtime.pending_commands["99"] = {"pwr_on": PendingCommand(value=False, timestamp=0.0, attribute="pwr_on")}

    entry = SimpleNamespace(runtime_data=runtime)
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "42"), ("other_domain", "ignored")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )

    assert result is True
    assert "42" not in runtime.device_locks
    assert "42" not in runtime.pending_commands
    # Other devices and unrelated identifiers are untouched.
    assert "99" in runtime.device_locks
    assert "99" in runtime.pending_commands

async def test_async_remove_config_entry_device_handles_missing_runtime():
    """Removal must not crash when runtime_data is unset (e.g. failed setup)."""
    entry = SimpleNamespace()  # no runtime_data attribute
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "42")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )
    assert result is True

async def test_async_remove_config_entry_device_unknown_device_is_noop():
    """Removing a device that was never tracked must not raise."""
    from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData

    runtime = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    entry = SimpleNamespace(runtime_data=runtime)
    device_entry = SimpleNamespace(identifiers={(DOMAIN, "never_seen")})

    result = await atmeex_init.async_remove_config_entry_device(
        hass=SimpleNamespace(), config_entry=entry, device_entry=device_entry
    )
    assert result is True
    assert runtime.device_locks == {}
    assert runtime.pending_commands == {}
