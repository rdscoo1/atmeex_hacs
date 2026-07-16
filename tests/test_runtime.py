"""Unit tests for entry-owned Atmeex runtime dependencies."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud.command_executor as command_executor_module
from custom_components.atmeex_cloud.command_executor import (
    AtmeexCommandExecutor,
    PendingCommand,
)
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData


def _runtime() -> AtmeexRuntimeData:
    return AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=AsyncMock(),
    )


def test_pending_command_fields():
    pending = PendingCommand(value=5, generation=1, expires_at=10.0)

    assert pending.value == 5
    assert pending.generation == 1
    assert pending.expires_at == 10.0


def test_runtime_owns_one_command_executor():
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=refresh_device,
        state_store=None,
        command_executor=executor,
    )

    assert runtime.command_executor is executor


def test_runtime_auto_constructs_one_command_executor():
    refresh_device = AsyncMock()
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=refresh_device,
    )

    assert isinstance(runtime.command_executor, AtmeexCommandExecutor)
    assert runtime.command_executor is runtime.command_executor


def test_runtime_set_get_clear_pending():
    runtime = _runtime()
    generation = runtime.set_pending(1, "fan_speed", 7)
    later_generation = runtime.set_pending(1, "pwr_on", True)

    assert isinstance(generation, int)
    assert isinstance(later_generation, int)
    assert later_generation > generation
    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None and pending.value == 7

    runtime.clear_pending(1, "fan_speed")
    assert runtime.get_pending(1, "fan_speed") is None


def test_runtime_clear_pending_if_confirmed_matching():
    runtime = _runtime()
    runtime.set_pending(1, "pwr_on", True)

    assert runtime.clear_pending_if_confirmed(1, "pwr_on", True) is True
    assert runtime.get_pending(1, "pwr_on") is None


def test_runtime_clear_pending_if_confirmed_stale():
    runtime = _runtime()
    runtime.set_pending(1, "fan_speed", 7)

    assert runtime.clear_pending_if_confirmed(1, "fan_speed", 3) is False
    assert runtime.get_pending(1, "fan_speed") is not None


def test_runtime_clear_pending_if_confirmed_expired(monkeypatch):
    now = 100.0
    monkeypatch.setattr(command_executor_module.time, "monotonic", lambda: now)
    runtime = _runtime()
    runtime.set_pending(1, "fan_speed", 7)

    now = 120.0

    assert runtime.clear_pending_if_confirmed(
        1,
        "fan_speed",
        3,
        tolerance=5.0,
    ) is True
    assert runtime.get_pending(1, "fan_speed") is None


def test_device_lock_identity_and_canonical_aliases():
    runtime = _runtime()
    lock_a = runtime.get_device_lock(1)
    lock_b = runtime.get_device_lock("001")
    lock_c = runtime.get_device_lock(2)

    assert lock_a is lock_b
    assert lock_a is not lock_c
    assert isinstance(lock_a, asyncio.Lock)


def test_runtime_compatibility_views_do_not_expose_mutable_mappings():
    runtime = _runtime()
    runtime.set_pending(1, "fan_speed", {"value": 7})
    original_lock = runtime.get_device_lock(1)

    pending_view = runtime.pending_commands
    pending_view["1"].clear()
    pending_view.clear()
    lock_view = runtime.device_locks
    lock_view["1"] = asyncio.Lock()
    lock_view.clear()

    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None
    assert pending.value == {"value": 7}
    assert runtime.get_device_lock(1) is original_lock


def test_cancel_legacy_generation_preserves_newer_pending_value():
    runtime = _runtime()
    older_generation = runtime.set_pending(1, "fan_speed", 3)
    newer_generation = runtime.set_pending(1, "fan_speed", 5)

    runtime.cancel_legacy_generation(1, older_generation)

    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None
    assert pending.value == 5
    assert pending.generation == newer_generation


def test_runtime_without_refresh_keeps_executor_optional():
    runtime = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)

    assert runtime.command_executor is None
    assert runtime.pending_commands == {}
    assert runtime.device_locks == {}
    assert runtime.get_pending(1, "fan_speed") is None
    assert runtime.set_pending(1, "fan_speed", 5) is None
    assert runtime.clear_pending_if_confirmed(1, "fan_speed", 3) is True
    with pytest.raises(RuntimeError, match="Command executor is unavailable"):
        runtime.get_device_lock(1)


@pytest.mark.asyncio
async def test_runtime_tracks_and_discards_completed_tasks():
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        state_store=None,
        command_executor=None,
        refresh_device=None,
    )
    candidate = asyncio.create_task(asyncio.sleep(0))
    try:
        task = runtime.track_task(candidate)
        assert task in runtime.tasks
        await task
        await asyncio.sleep(0)
        assert runtime.tasks == set()
    finally:
        if not candidate.done():
            candidate.cancel()
        await asyncio.gather(candidate, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_logs_failed_task_type_without_exception_text(caplog):
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        state_store=None,
        command_executor=None,
        refresh_device=None,
    )

    async def fail() -> None:
        raise RuntimeError("secret cloud response")

    candidate = asyncio.create_task(fail())
    try:
        task = runtime.track_task(candidate)
        with pytest.raises(RuntimeError, match="secret cloud response"):
            await task
        await asyncio.sleep(0)

        assert "RuntimeError" in caplog.text
        assert "secret cloud response" not in caplog.text
        assert runtime.tasks == set()
    finally:
        if not candidate.done():
            candidate.cancel()
        await asyncio.gather(candidate, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_discards_cancelled_task_without_warning(caplog):
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        state_store=None,
        command_executor=None,
        refresh_device=None,
    )
    candidate = asyncio.create_task(asyncio.sleep(60))
    try:
        task = runtime.track_task(candidate)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)

        assert runtime.tasks == set()
        assert "Atmeex background task failed" not in caplog.text
    finally:
        if not candidate.done():
            candidate.cancel()
        await asyncio.gather(candidate, return_exceptions=True)


def test_runtime_ownership_defaults_are_isolated():
    first = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=None,
    )
    second = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=None,
    )

    first.refresh_tasks["1"] = MagicMock()
    first.websocket_overflow_count = 2

    assert first.stopping is False
    assert first.inventory_watchdog_task is None
    assert first.websocket_resync_task is None
    assert second.tasks == set()
    assert second.refresh_tasks == {}
    assert second.websocket_overflow_count == 0


def test_runtime_preserves_legacy_websocket_task_positional_arguments():
    api = MagicMock()
    coordinator = MagicMock()
    refresh_device = AsyncMock()
    state_store = MagicMock()
    command_executor = MagicMock()
    websocket_manager = MagicMock()
    websocket_start_task = MagicMock()
    websocket_message_task = MagicMock()

    runtime = AtmeexRuntimeData(
        api,
        coordinator,
        refresh_device,
        state_store,
        command_executor,
        websocket_manager,
        websocket_start_task,
        websocket_message_task,
    )

    assert runtime.websocket_start_task is websocket_start_task
    assert runtime.websocket_message_task is websocket_message_task
    assert runtime.stopping is False
    assert runtime.tasks == set()
    assert runtime.refresh_tasks == {}
