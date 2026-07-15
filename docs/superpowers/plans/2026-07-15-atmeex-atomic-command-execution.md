# Atmeex Atomic Command Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize every logical Atmeex device command atomically, preserve the newest optimistic field values through races and cancellation, and perform exactly one targeted confirmation refresh per successful logical action.

**Architecture:** Add an AtmeexCommandExecutor owned by AtmeexRuntimeData. Entities submit a zero-argument CommandCoroutineFactory plus expected pending fields; the executor allocates generations before lock acquisition, invokes the factory only after acquiring the per-device lock, and keeps the final RecoveryRefresh inside that lock. Platform entities remain responsible only for validation and composing logical API operations, while the executor owns locking, optimistic values, translated execution errors, cancellation cleanup, and confirmation.

**Tech Stack:** Python 3.12+, asyncio, Home Assistant 2024.8+ entity APIs and translated exceptions, pytest, pytest-asyncio, unittest.mock

---

## Exact file map

- Create custom_components/atmeex_cloud/command_executor.py — command type aliases, generation-tagged pending values, per-device locks, translated failure mapping, cancellation cleanup, and the single confirmation refresh.
- Modify custom_components/atmeex_cloud/runtime.py — make the executor an entry-owned dependency and retain narrow compatibility accessors during the migration.
- Modify custom_components/atmeex_cloud/__init__.py — construct one executor per config entry and keep the existing PendingCommand re-export.
- Modify custom_components/atmeex_cloud/entity_base.py — replace eager coroutine execution with the factory-based executor interface and delegate optimistic reads to the executor.
- Modify custom_components/atmeex_cloud/climate.py — express each HVAC, temperature, humidity, fan, damper, service, and preset action as one validated logical command.
- Modify custom_components/atmeex_cloud/fan.py — combine speed-plus-power actions into one command and one refresh.
- Modify custom_components/atmeex_cloud/select.py — route both selects through the shared executor and reject invalid or unsupported input.
- Modify custom_components/atmeex_cloud/switch.py — route all switch writes through the shared executor.
- Modify custom_components/atmeex_cloud/strings.json — add translated execution and validation exception keys.
- Modify custom_components/atmeex_cloud/translations/en.json — add English exception messages.
- Modify custom_components/atmeex_cloud/translations/ru.json — add Russian exception messages.
- Create tests/test_command_executor.py — deterministic executor generation, ordering, failure, refresh, and cancellation tests.
- Modify tests/test_runtime.py — assert runtime ownership and compatibility delegation.
- Modify tests/test_entity_base.py — assert lazy factory invocation and translated exception properties.
- Modify tests/test_climate.py — assert one-refresh compound commands, pending maps, validation, and atomic presets.
- Modify tests/test_fan.py — assert speed-plus-power is one logical action.
- Modify tests/test_select.py — assert executor use and validation.
- Modify tests/test_switch.py — assert executor use for every switch.
- Modify tests/test_unload.py — migrate direct PendingCommand construction and
  verify removal through the executor.
- Modify tests/test_race_protection.py — replace sleep-based ordering with Event barriers and generation assertions.
- Modify tests/test_refresh_device.py — assert a command performs one targeted refresh through the entry-owned callback.

## Locked predecessor and public interfaces

Plan 2 is complete before this plan begins. Use its names exactly:

Plan 2's frozen FieldRevisionBaseline contains device_id: str and revisions: Mapping[str, int]. Its frozen StateStoreUpdate contains data: AtmeexCoordinatorData, changed: bool, and removed_device_ids: frozenset[str] with an empty default.

AtmeexStateStore(initial: AtmeexCoordinatorData | None = None) exposes data, capture_device(device_id), capture_all(), apply_websocket_delta(device_id, state_delta, device_delta), apply_refresh(device, baseline), and apply_inventory(devices, baselines). Use the exact argument and return types in the file map's approved design: capture_device returns FieldRevisionBaseline, capture_all returns dict[str, FieldRevisionBaseline], and every apply method returns StateStoreUpdate.

This plan defines CommandCoroutineFactory as Callable[[], Awaitable[None]] and RecoveryRefresh as Callable[[int | str], Awaitable[None]]. The executable implementation in Task 1 fixes the Plan 4 contract: AtmeexCommandExecutor(refresh_device, pending_ttl=10.0), async_execute(device_id, operation, pending, translation_key, translation_placeholders), value_with_pending(device_id, attribute, confirmed), confirm(device_id, attribute, confirmed), and allow_recovery_confirmation(device_id). A generation becomes equality-confirmable only after its own successful targeted refresh, or after Plan 4 reports a successful authoritative recovery.

## Execution gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require all
tests to pass. The only warning allowed before Plan 4 is the already-documented
un-awaited WebSocket startup coroutine; the pytest-asyncio configuration warning
remains assigned to Plan 6. No task may add another warning.

### Task 1: Build the generation-safe command executor

**Files:**

- Create: custom_components/atmeex_cloud/command_executor.py
- Create: tests/test_command_executor.py

- [ ] **Step 1: Write deterministic RED tests for ordering, generations, refresh failure, cancellation, and cross-device concurrency**

Create tests/test_command_executor.py with:

~~~python
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.command_executor import AtmeexCommandExecutor


@pytest.mark.asyncio
async def test_same_device_factory_runs_only_after_lock_and_refresh_is_inside_lock():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def refresh_device(device_id: int | str) -> None:
        order.append(f"refresh-{device_id}")

    executor = AtmeexCommandExecutor(refresh_device)

    async def first_operation() -> None:
        order.append("first-start")
        first_started.set()
        await release_first.wait()
        order.append("first-end")

    second_factory_called = False

    def second_factory():
        nonlocal second_factory_called
        second_factory_called = True

        async def second_operation() -> None:
            order.append("second")

        return second_operation()

    first = asyncio.create_task(
        executor.async_execute(
            1,
            first_operation,
            pending={"fan_speed": 4},
            translation_key="command_failed",
            translation_placeholders={"action": "set fan speed"},
        )
    )
    await first_started.wait()
    second = asyncio.create_task(
        executor.async_execute(
            "1",
            second_factory,
            pending={"fan_speed": 7},
            translation_key="command_failed",
            translation_placeholders={"action": "set fan speed"},
        )
    )
    await asyncio.sleep(0)

    assert second_factory_called is False
    assert executor.value_with_pending(1, "fan_speed", 2) == 7

    release_first.set()
    await asyncio.gather(first, second)

    assert second_factory_called is True
    assert order == [
        "first-start",
        "first-end",
        "refresh-1",
        "second",
        "refresh-1",
    ]


@pytest.mark.asyncio
async def test_older_failure_cannot_clear_newer_pending_generation():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    async def failing_operation() -> None:
        first_started.set()
        await release_first.wait()
        raise ApiError("test_command", "write failed", status=503)

    async def newer_operation() -> None:
        return

    older = asyncio.create_task(
        executor.async_execute(
            1,
            failing_operation,
            pending={"fan_speed": 4},
            translation_key="command_failed",
            translation_placeholders={"action": "set fan speed"},
        )
    )
    await first_started.wait()
    newer = asyncio.create_task(
        executor.async_execute(
            1,
            newer_operation,
            pending={"fan_speed": 7},
            translation_key="command_failed",
            translation_placeholders={"action": "set fan speed"},
        )
    )
    await asyncio.sleep(0)
    release_first.set()

    with pytest.raises(HomeAssistantError) as raised:
        await older
    await newer

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "command_failed"
    assert executor.value_with_pending(1, "fan_speed", 3) == 7
    assert refresh_device.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_never_creates_operation_coroutine():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    async def first_operation() -> None:
        first_started.set()
        await release_first.wait()

    factory_called = False

    def cancelled_factory():
        nonlocal factory_called
        factory_called = True

        async def operation() -> None:
            return

        return operation()

    owner = asyncio.create_task(
        executor.async_execute(
            1,
            first_operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await first_started.wait()
    waiter = asyncio.create_task(
        executor.async_execute(
            1,
            cancelled_factory,
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert factory_called is False
    # Cancelling the newer generation reveals the still-running owner's
    # optimistic value instead of erasing the field.
    assert executor.value_with_pending(1, "pwr_on", False) is True
    assert owner.done() is False

    release_first.set()
    await owner


@pytest.mark.asyncio
async def test_queued_aba_value_is_not_confirmed_before_factory_runs():
    owner_started = asyncio.Event()
    release_owner = asyncio.Event()
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    async def owner_operation() -> None:
        owner_started.set()
        await release_owner.wait()

    queued_operation = AsyncMock()
    queued_factory = MagicMock(side_effect=lambda: queued_operation())
    owner = asyncio.create_task(
        executor.async_execute(
            1,
            owner_operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await owner_started.wait()
    queued = asyncio.create_task(
        executor.async_execute(
            1,
            queued_factory,
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)

    # False is the old confirmed value and the queued intent. Equality alone
    # must not consume a generation whose operation has not run.
    assert executor.value_with_pending(1, "pwr_on", False) is False
    assert executor.get_pending(1, "pwr_on") is not None
    queued_factory.assert_not_called()

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    release_owner.set()
    await owner


@pytest.mark.asyncio
async def test_cancellation_after_compound_write_starts_recovers_before_unlock():
    first_write_done = asyncio.Event()
    wait_forever = asyncio.Event()
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    async def partial_operation() -> None:
        first_write_done.set()
        await wait_forever.wait()

    task = asyncio.create_task(
        executor.async_execute(
            1,
            partial_operation,
            pending={"u_night": True, "fan_speed": 2},
            translation_key="command_failed",
        )
    )
    await first_write_done.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    refresh_device.assert_awaited_once_with(1)
    assert executor.get_pending(1, "u_night") is None
    assert executor.get_pending(1, "fan_speed") is None


@pytest.mark.asyncio
async def test_successful_write_with_failed_confirmation_remains_successful_and_pending():
    refresh_device = AsyncMock(
        side_effect=ApiError("test_refresh", "GET failed", status=503)
    )
    executor = AtmeexCommandExecutor(refresh_device)
    operation = AsyncMock()

    await executor.async_execute(
        1,
        operation,
        pending={"pwr_on": True},
        translation_key="command_failed",
    )

    operation.assert_awaited_once()
    refresh_device.assert_awaited_once_with(1)
    assert executor.value_with_pending(1, "pwr_on", False) is True
    assert executor.confirm(1, "pwr_on", True) is False
    executor.allow_recovery_confirmation(1)
    assert executor.confirm(1, "pwr_on", True) is True
    assert executor.value_with_pending(1, "pwr_on", False) is False


@pytest.mark.asyncio
async def test_different_devices_execute_concurrently():
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def refresh_device(device_id: int | str) -> None:
        return

    executor = AtmeexCommandExecutor(refresh_device)

    def factory(device_id: str):
        async def operation() -> None:
            started.add(device_id)
            if started == {"1", "2"}:
                both_started.set()
            await release.wait()

        return operation

    first = asyncio.create_task(
        executor.async_execute(
            1,
            factory("1"),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    second = asyncio.create_task(
        executor.async_execute(
            2,
            factory("2"),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )

    await asyncio.wait_for(both_started.wait(), timeout=1)
    release.set()
    await asyncio.gather(first, second)
~~~

- [ ] **Step 2: Run the executor tests and verify the missing module**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_command_executor.py
~~~

Expected: collection fails with ModuleNotFoundError: No module named 'custom_components.atmeex_cloud.command_executor'.

- [ ] **Step 3: Implement the complete minimal executor**

Create custom_components/atmeex_cloud/command_executor.py:

~~~python
"""Per-device atomic command execution for Atmeex entities."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .api import ApiError
from .const import DOMAIN

CommandCoroutineFactory = Callable[[], Awaitable[None]]
RecoveryRefresh = Callable[[int | str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """Newest optimistic value for one state field."""

    value: Any
    generation: int
    expires_at: float


class AtmeexCommandExecutor:
    """Serialize complete logical commands per device."""

    def __init__(
        self,
        refresh_device: RecoveryRefresh,
        *,
        pending_ttl: float = 10.0,
    ) -> None:
        self._refresh_device = refresh_device
        self._pending_ttl = pending_ttl
        self._generation = 0
        self._locks: dict[str, asyncio.Lock] = {}
        # Pending values are installed before lock acquisition. Retaining a
        # per-field generation stack lets cancellation of a newer waiter reveal
        # the still-live owner generation.
        self._pending: dict[str, dict[str, list[PendingCommand]]] = {}
        self._executed_generations: set[int] = set()
        self._confirmation_ready_generations: set[int] = set()

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _lock_for(self, device_id: int | str) -> asyncio.Lock:
        return self._locks.setdefault(str(device_id), asyncio.Lock())

    def _install_pending(
        self,
        device_id: int | str,
        generation: int,
        values: Mapping[str, Any],
    ) -> None:
        key = str(device_id)
        expires_at = time.monotonic() + self._pending_ttl
        fields = self._pending.setdefault(key, {})
        for attribute, value in values.items():
            fields.setdefault(attribute, []).append(
                PendingCommand(
                    value=value,
                    generation=generation,
                    expires_at=expires_at,
                )
            )

    def _clear_generation(self, device_id: int | str, generation: int) -> None:
        key = str(device_id)
        fields = self._pending.get(key)
        if fields is None:
            return
        for attribute, stack in tuple(fields.items()):
            stack[:] = [item for item in stack if item.generation != generation]
            if not stack:
                fields.pop(attribute)
        if not fields:
            self._pending.pop(key, None)
        self._executed_generations.discard(generation)
        self._confirmation_ready_generations.discard(generation)

    def _live_stack(
        self,
        device_id: int | str,
        attribute: str,
    ) -> list[PendingCommand]:
        key = str(device_id)
        fields = self._pending.get(key)
        if fields is None:
            return []
        stack = fields.get(attribute)
        if stack is None:
            return []
        now = time.monotonic()
        expired = {
            item.generation for item in stack if now >= item.expires_at
        }
        stack[:] = [item for item in stack if item.generation not in expired]
        if not stack:
            fields.pop(attribute, None)
            if not fields:
                self._pending.pop(key, None)
        for generation in expired:
            if not any(
                item.generation == generation
                for device_fields in self._pending.values()
                for pending_stack in device_fields.values()
                for item in pending_stack
            ):
                self._executed_generations.discard(generation)
                self._confirmation_ready_generations.discard(generation)
        return stack

    def get_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> PendingCommand | None:
        """Return a non-expired pending value for compatibility and tests."""
        stack = self._live_stack(device_id, attribute)
        return stack[-1] if stack else None

    def confirm(
        self,
        device_id: int | str,
        attribute: str,
        confirmed: Any,
    ) -> bool:
        """Clear a matching or expired optimistic value."""
        stack = self._live_stack(device_id, attribute)
        if not stack:
            return True
        key = str(device_id)
        fields = self._pending[key]
        matching_ready = [
            item.generation
            for item in stack
            if item.generation in self._confirmation_ready_generations
            and item.value == confirmed
        ]
        if not matching_ready:
            return False

        # A successful refresh for generation N reflects all writes serialized
        # before N, so it may retire N and predecessors, never a newer waiter.
        cutoff = max(matching_ready)
        retired = {item.generation for item in stack if item.generation <= cutoff}
        stack[:] = [item for item in stack if item.generation > cutoff]
        if not stack:
            fields.pop(attribute, None)
        if not fields:
            self._pending.pop(key, None)
        for generation in retired:
            if not any(
                item.generation == generation
                for device_fields in self._pending.values()
                for pending_stack in device_fields.values()
                for item in pending_stack
            ):
                self._executed_generations.discard(generation)
                self._confirmation_ready_generations.discard(generation)
        return not stack

    def allow_recovery_confirmation(self, device_id: int | str) -> None:
        """Allow a successful authoritative recovery to confirm prior writes."""
        fields = self._pending.get(str(device_id), {})
        self._confirmation_ready_generations.update(
            item.generation
            for stack in fields.values()
            for item in stack
            if item.generation in self._executed_generations
        )

    def value_with_pending(
        self,
        device_id: int | str,
        attribute: str,
        confirmed: Any,
    ) -> Any:
        """Return the newest pending value until confirmation or expiry."""
        if self.confirm(device_id, attribute, confirmed):
            return confirmed
        pending = self.get_pending(device_id, attribute)
        return confirmed if pending is None else pending.value

    def remove_device(self, device_id: int | str) -> None:
        """Release per-device executor state after confirmed device removal."""
        key = str(device_id)
        fields = self._pending.pop(key, {})
        generations = {
            item.generation
            for stack in fields.values()
            for item in stack
        }
        self._executed_generations.difference_update(generations)
        self._confirmation_ready_generations.difference_update(generations)
        self._locks.pop(key, None)

    def set_pending(
        self,
        device_id: int | str,
        attribute: str,
        value: Any,
    ) -> int:
        """Install an immediately confirmable temporary legacy value."""
        generation = self._next_generation()
        self._install_pending(
            device_id,
            generation,
            {attribute: value},
        )
        # Existing callers use this method both before and after eager legacy
        # commands and have no completion token. Keep their old equality
        # behavior only during migration; Task 6 removes this method after all
        # production commands use async_execute's confirmation tickets.
        self._executed_generations.add(generation)
        self._confirmation_ready_generations.add(generation)
        return generation

    def clear_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> None:
        """Clear one field without disturbing other pending fields."""
        key = str(device_id)
        fields = self._pending.get(key)
        if fields is None:
            return
        fields.pop(attribute, None)
        if not fields:
            self._pending.pop(key, None)

    def cancel_legacy_generation(
        self,
        device_id: int | str,
        generation: int,
    ) -> None:
        """Remove only one temporary eager-command generation."""
        self._clear_generation(device_id, generation)

    @property
    def pending_commands(
        self,
    ) -> dict[str, dict[str, PendingCommand]]:
        """Expose the newest-value migration view (do not mutate it)."""
        return {
            key: {
                attribute: stack[-1]
                for attribute, stack in fields.items()
                if stack
            }
            for key, fields in self._pending.items()
        }

    @property
    def device_locks(self) -> dict[str, asyncio.Lock]:
        """Expose the migration view used by device-removal code."""
        return self._locks

    async def _recover_after_write_failure(self, device_id: int | str) -> None:
        try:
            await self._refresh_device(device_id)
        except ApiError:
            return
        except asyncio.TimeoutError:
            return

    async def async_execute(
        self,
        device_id: int | str,
        operation: CommandCoroutineFactory,
        *,
        pending: Mapping[str, Any],
        translation_key: str,
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> None:
        """Execute one logical operation and its confirmation under one lock."""
        generation = self._next_generation()
        self._install_pending(device_id, generation, pending)

        try:
            async with self._lock_for(device_id):
                try:
                    await operation()
                except ApiError as err:
                    await self._recover_after_write_failure(device_id)
                    self._clear_generation(device_id, generation)
                    raise HomeAssistantError(
                        translation_domain=DOMAIN,
                        translation_key=translation_key,
                        translation_placeholders=dict(
                            translation_placeholders or {}
                        ),
                    ) from err
                except asyncio.CancelledError:
                    # A compound operation may already have completed one
                    # remote write. Reconcile under the same device lock before
                    # exposing cancellation or allowing the next command.
                    await self._recover_after_write_failure(device_id)
                    self._clear_generation(device_id, generation)
                    raise
                except Exception:
                    await self._recover_after_write_failure(device_id)
                    self._clear_generation(device_id, generation)
                    raise

                self._executed_generations.add(generation)
                try:
                    await self._refresh_device(device_id)
                except ApiError:
                    return
                except asyncio.TimeoutError:
                    return
                except asyncio.CancelledError:
                    await self._recover_after_write_failure(device_id)
                    self._clear_generation(device_id, generation)
                    raise
                else:
                    # Equality becomes confirmation-safe only after the
                    # authoritative refresh has completed. Marking before the
                    # GET would reintroduce the queued ABA race.
                    self._confirmation_ready_generations.add(generation)
        except asyncio.CancelledError:
            self._clear_generation(device_id, generation)
            raise
~~~

All typed API failures introduced by Plan 1 must inherit ApiError so this boundary translates them without exposing raw response bodies.

- [ ] **Step 4: Run the focused executor tests**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_command_executor.py
~~~

Expected: 7 passed.

- [ ] **Step 5: Commit the executor primitive**

~~~bash
git add custom_components/atmeex_cloud/command_executor.py tests/test_command_executor.py
git commit -m "feat: add atomic Atmeex command executor"
~~~

### Task 2: Make runtime and entities use the locked executor interface

**Files:**

- Modify: custom_components/atmeex_cloud/runtime.py
- Modify: custom_components/atmeex_cloud/__init__.py
- Modify: custom_components/atmeex_cloud/entity_base.py
- Modify: tests/test_runtime.py
- Modify: tests/test_entity_base.py
- Modify: tests/test_climate.py
- Modify: tests/test_unload.py

- [ ] **Step 1: Add RED runtime ownership and lazy-factory tests**

Append to tests/test_runtime.py:

~~~python
from unittest.mock import AsyncMock

from custom_components.atmeex_cloud.command_executor import AtmeexCommandExecutor


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
~~~

Replace the eager-coro error case in tests/test_entity_base.py with:

~~~python
@pytest.mark.asyncio
async def test_entity_passes_a_factory_to_the_entry_executor():
    ent, _cond, api, runtime = _make_entity_with_runtime()
    operation_started = False
    captured_factory = None

    async def capture_execute(
        device_id,
        operation,
        *,
        pending,
        translation_key,
        translation_placeholders=None,
    ):
        nonlocal captured_factory
        captured_factory = operation
        assert device_id == 1
        assert pending == {"fan_speed": 5}
        assert translation_key == "command_failed"

    runtime.command_executor.async_execute = AsyncMock(side_effect=capture_execute)

    async def set_fan_speed(device_id, speed):
        nonlocal operation_started
        operation_started = True

    api.set_fan_speed.side_effect = set_fan_speed
    await ent._execute_command(
        lambda: api.set_fan_speed(1, 5),
        pending={"fan_speed": 5},
        translation_key="command_failed",
    )

    assert operation_started is False
    assert callable(captured_factory)
    await captured_factory()
    assert operation_started is True
~~~

Also add these regressions for the temporary eager-awaitable migration branch.
They keep that branch safe until Task 6 deletes it:

~~~python
@pytest.mark.asyncio
async def test_legacy_waiter_cancellation_closes_unstarted_coroutine():
    ent, _cond, api, runtime = _make_entity_with_runtime()
    lock = runtime.get_device_lock(1)
    await lock.acquire()
    runtime.set_pending(1, "fan_speed", 3)

    waiter = asyncio.create_task(
        ent._execute_command(
            api.set_fan_speed(1, 5),
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    api.set_fan_speed.assert_not_awaited()
    assert runtime.get_pending(1, "fan_speed").value == 3
    lock.release()


@pytest.mark.asyncio
async def test_legacy_api_error_survives_failed_recovery_refresh():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    command_error = ApiError("legacy_command", "boom", status=500)
    runtime.refresh_device.side_effect = ApiError(
        "legacy_recovery",
        "unavailable",
        status=503,
    )

    async def fail_command() -> None:
        raise command_error

    with pytest.raises(HomeAssistantError) as raised:
        await ent._execute_command(
            fail_command(),
            pending_attr="fan_speed",
            pending_value=5,
        )

    assert raised.value.__cause__ is command_error
    runtime.refresh_device.assert_awaited_once()
    assert runtime.get_pending(1, "fan_speed") is None


@pytest.mark.asyncio
async def test_legacy_cancellation_survives_failed_recovery_refresh():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    operation_started = asyncio.Event()
    never_release = asyncio.Event()
    runtime.refresh_device.side_effect = ApiError(
        "legacy_recovery",
        "unavailable",
        status=503,
    )

    async def partial_command() -> None:
        operation_started.set()
        await never_release.wait()

    task = asyncio.create_task(
        ent._execute_command(
            partial_command(),
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await operation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    runtime.refresh_device.assert_awaited_once()
    assert runtime.get_pending(1, "fan_speed") is None
~~~

Add `import asyncio` and merge `AsyncMock` into the existing
`unittest.mock` import in `tests/test_entity_base.py`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run the focused tests with runtime warnings promoted to errors:

~~~bash
.venv/bin/python -W error::RuntimeWarning -m pytest -q tests/test_runtime.py::test_runtime_owns_one_command_executor tests/test_entity_base.py::test_entity_passes_a_factory_to_the_entry_executor tests/test_entity_base.py::test_legacy_waiter_cancellation_closes_unstarted_coroutine tests/test_entity_base.py::test_legacy_api_error_survives_failed_recovery_refresh tests/test_entity_base.py::test_legacy_cancellation_survives_failed_recovery_refresh
~~~

Expected: FAIL because AtmeexRuntimeData has no command_executor field,
_execute_command still accepts an already-created awaitable, and cancellation
while waiting for the legacy lock does not clean up its generation/coroutine.

- [ ] **Step 3: Add the executor to AtmeexRuntimeData**

In custom_components/atmeex_cloud/runtime.py, import the command types and update the dataclass fields as follows, preserving the Plan 2 state_store field and existing WebSocket fields:

~~~python
from .command_executor import AtmeexCommandExecutor, PendingCommand, RecoveryRefresh
from .state_store import AtmeexStateStore


@dataclass
class AtmeexRuntimeData:
    """Entry-owned Atmeex dependencies."""

    api: Any
    coordinator: Any
    refresh_device: RecoveryRefresh | None
    state_store: AtmeexStateStore | None = None
    command_executor: AtmeexCommandExecutor | None = None
    websocket_manager: Any = None
    websocket_start_task: asyncio.Task[None] | None = None
    websocket_message_task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        if self.command_executor is None and self.refresh_device is not None:
            self.command_executor = AtmeexCommandExecutor(self.refresh_device)

    def get_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> PendingCommand | None:
        if self.command_executor is None:
            return None
        return self.command_executor.get_pending(device_id, attribute)

    def set_pending(
        self,
        device_id: int | str,
        attribute: str,
        value: Any,
    ) -> int | None:
        if self.command_executor is None:
            return None
        return self.command_executor.set_pending(device_id, attribute, value)

    def clear_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> None:
        if self.command_executor is not None:
            self.command_executor.clear_pending(device_id, attribute)

    def cancel_legacy_generation(
        self,
        device_id: int | str,
        generation: int | None,
    ) -> None:
        if self.command_executor is not None and generation is not None:
            self.command_executor.cancel_legacy_generation(
                device_id,
                generation,
            )

    def get_device_lock(self, device_id: int | str) -> asyncio.Lock:
        if self.command_executor is None:
            raise RuntimeError("Command executor is unavailable")
        return self.command_executor.device_locks.setdefault(
            str(device_id),
            asyncio.Lock(),
        )

    @property
    def pending_commands(
        self,
    ) -> dict[str, dict[str, PendingCommand]]:
        if self.command_executor is None:
            return {}
        return self.command_executor.pending_commands

    @property
    def device_locks(self) -> dict[str, asyncio.Lock]:
        if self.command_executor is None:
            return {}
        return self.command_executor.device_locks

    def clear_pending_if_confirmed(
        self,
        device_id: int | str,
        attribute: str,
        confirmed_value: Any,
        tolerance: float = 10.0,
    ) -> bool:
        if self.command_executor is None:
            return True
        return self.command_executor.confirm(device_id, attribute, confirmed_value)
~~~

Remove runtime-owned device_locks, pending_commands, set_pending, clear_pending,
and their time/logging code. The temporary get_pending,
cancel_legacy_generation, and clear_pending_if_confirmed delegates keep older
in-repository tests readable until they are migrated in this plan; tolerance is
retained only as an ignored compatibility argument. Task 6 removes all of
these migration delegates after the last eager-awaitable command is gone.

- [ ] **Step 4: Construct and re-export the executor from the composition root**

In custom_components/atmeex_cloud/__init__.py, replace the PendingCommand import and runtime construction with:

~~~python
from .command_executor import AtmeexCommandExecutor, PendingCommand
from .runtime import AtmeexRuntimeData
~~~

After Plan 2's refresh_device closure is defined, construct the entry objects with:

~~~python
    command_executor = AtmeexCommandExecutor(refresh_device)
    runtime_data = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        state_store=coordinator.state_store,
        command_executor=command_executor,
        refresh_device=refresh_device,
        websocket_manager=websocket_manager,
        websocket_start_task=websocket_start_task,
    )
~~~

Keep PendingCommand in __all__ so imports from custom_components.atmeex_cloud do not break during the six-plan rollout.

In confirmed-device-removal cleanup, replace direct mutation of
`runtime.pending_commands` and `runtime.device_locks` with
`runtime.command_executor.remove_device(key)` when an executor exists. This is
required because `pending_commands` is only a newest-generation compatibility
view; mutating that view must never be treated as authoritative cleanup.

- [ ] **Step 5: Replace the mixin command boundary**

In custom_components/atmeex_cloud/entity_base.py, add these imports:

~~~python
import asyncio
from collections.abc import Awaitable, Mapping

from homeassistant.exceptions import ServiceValidationError

from .command_executor import (
    AtmeexCommandExecutor,
    CommandCoroutineFactory,
)
~~~

Replace _state_with_pending and _execute_command, and add the validation helpers:

~~~python
    def _state_with_pending(
        self,
        attribute: str,
        confirmed_value: Any,
        *,
        tolerance: float | None = None,
    ) -> Any:
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None)
        if executor is None:
            return confirmed_value
        return executor.value_with_pending(
            self._device_id,
            attribute,
            confirmed_value,
        )

    async def _execute_command(
        self,
        operation: CommandCoroutineFactory | Awaitable[None],
        *,
        pending: Mapping[str, Any] | None = None,
        translation_key: str = "command_failed",
        translation_placeholders: Mapping[str, str] | None = None,
        pending_attr: str | None = None,
        pending_value: Any = None,
        error_message: str = "Command failed",
    ) -> None:
        runtime = getattr(self, "_runtime", None)
        if callable(operation):
            if pending is None:
                raise TypeError("factory commands require a pending mapping")
            executor = getattr(runtime, "command_executor", None)
            if executor is None:
                executor = getattr(self, "_fallback_command_executor", None)
                if executor is None:
                    executor = AtmeexCommandExecutor(
                        lambda _device_id: self._refresh()
                    )
                    self._fallback_command_executor = executor
            await executor.async_execute(
                self._device_id,
                operation,
                pending=pending,
                translation_key=translation_key,
                translation_placeholders=translation_placeholders,
            )
            return

        # Temporary adapter for platform methods migrated in Tasks 3–6. It
        # preserves the old behavior so this intermediate commit stays green.
        legacy_generation = None
        if pending_attr is not None and runtime is not None:
            legacy_generation = runtime.set_pending(
                self._device_id,
                pending_attr,
                pending_value,
            )
        lock = runtime.get_device_lock(self._device_id) if runtime is not None else None

        def cancel_legacy_generation() -> None:
            if runtime is not None:
                runtime.cancel_legacy_generation(
                    self._device_id,
                    legacy_generation,
                )

        async def recover_legacy_best_effort() -> None:
            """Reconcile a possible partial write without masking its cause."""
            try:
                await self._refresh()
            except (asyncio.CancelledError, ApiError, asyncio.TimeoutError):
                return
            except Exception:
                # This adapter is temporary. Preserve the active command error;
                # the authoritative periodic refresh remains available.
                return

        operation_started = False

        async def run_legacy() -> None:
            nonlocal operation_started
            operation_started = True
            try:
                await operation
            except asyncio.CancelledError:
                # An eager legacy operation may have partially written before
                # cancellation. Reconcile while this device lock is still held,
                # then remove only this operation's optimistic generation.
                try:
                    await recover_legacy_best_effort()
                finally:
                    cancel_legacy_generation()
                raise
            except ApiError as err:
                try:
                    await recover_legacy_best_effort()
                finally:
                    cancel_legacy_generation()
                raise HomeAssistantError(error_message) from err
            except Exception:
                try:
                    await recover_legacy_best_effort()
                finally:
                    cancel_legacy_generation()
                raise
            await self._refresh()

        try:
            if lock is None:
                await run_legacy()
            else:
                async with lock:
                    await run_legacy()
        except asyncio.CancelledError:
            if not operation_started:
                if asyncio.iscoroutine(operation):
                    operation.close()
                elif isinstance(operation, asyncio.Future):
                    operation.cancel()
                cancel_legacy_generation()
            raise

    @staticmethod
    def _invalid_value(field: str, value: Any) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_command_value",
            translation_placeholders={
                "field": field,
                "value": str(value),
            },
        )

    @staticmethod
    def _unsupported_feature(feature: str) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_device_feature",
            translation_placeholders={"feature": feature},
        )
~~~

- [ ] **Step 6: Run runtime and mixin tests**

The eager-awaitable branch above is a temporary in-repository migration adapter,
not the final interface. It is removed in Task 6 after the last command platform
moves to factories.

Run:

~~~bash
.venv/bin/python -W error::RuntimeWarning -m pytest -q tests/test_runtime.py tests/test_entity_base.py
~~~

Expected: all tests pass after migrating **every** old pending double in the
same commit:

- In `tests/test_runtime.py`, replace direct expired-object injection with a
  `monkeypatch` of `command_executor.time.monotonic` around `set_pending`.
  Replace the legacy `isinstance(ts, float)` assertion with
  `isinstance(generation, int)` and assert later generations increase.
  Add a regression test that `cancel_legacy_generation` removes only the
  supplied generation while preserving a newer pending value for the same
  field.
- In `tests/test_climate.py`, replace the direct
  `runtime.pending_commands["1"] = ...` assignment with
  `runtime.set_pending(1, "pwr_on", value)`.
- In `tests/test_unload.py`, seed through `runtime.set_pending`, create locks
  through `runtime.get_device_lock`, and assert removal via the executor-backed
  accessors. Remove all constructors using the obsolete `timestamp` and
  `attribute` fields.
- Run `rg -n 'PendingCommand\(' tests`; only Task 1's new
  `value/generation/expires_at` assertions may remain.

- [ ] **Step 7: Commit runtime wiring**

~~~bash
git add custom_components/atmeex_cloud/runtime.py custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/entity_base.py tests/test_runtime.py tests/test_entity_base.py tests/test_climate.py tests/test_unload.py
git commit -m "refactor: route Atmeex entities through command executor"
~~~

### Task 3: Make fan commands one atomic logical action

**Files:**

- Modify: custom_components/atmeex_cloud/fan.py
- Modify: tests/test_fan.py
- Modify: tests/test_race_protection.py

- [ ] **Step 1: Replace fan refresh-count tests with RED atomic assertions**

Replace test_fan_async_set_percentage_turns_on_when_currently_off and test_fan_async_turn_on_with_percentage_sets_speed_then_power in tests/test_fan.py with:

~~~python
@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set_percentage", "turn_on"])
async def test_fan_speed_and_power_are_one_command_with_one_refresh(method):
    fan, cond, api, coordinator = _make_fan_entity()
    cond["pwr_on"] = False

    if method == "set_percentage":
        await fan.async_set_percentage(75)
    else:
        await fan.async_turn_on(percentage=75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_rejects_out_of_range_percentage():
    fan, _cond, api, coordinator = _make_fan_entity()

    with pytest.raises(ServiceValidationError) as raised:
        await fan.async_set_percentage(101)

    assert raised.value.translation_key == "invalid_command_value"
    api.set_fan_speed.assert_not_awaited()
    api.set_power.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()
~~~

Add this import:

~~~python
from homeassistant.exceptions import ServiceValidationError
~~~

Replace test_lock_serializes_operations in tests/test_race_protection.py with:

~~~python
@pytest.mark.asyncio
async def test_second_fan_command_starts_after_first_confirmation_refresh():
    fan, runtime, api, _coordinator = _make_fan_entity_with_runtime()
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    first_refresh_started = asyncio.Event()
    release_first_refresh = asyncio.Event()
    order: list[str] = []

    async def set_fan_speed(device_id, speed):
        order.append(f"write-{speed}")
        if speed == 4:
            first_write_started.set()
            await release_first_write.wait()

    refresh_count = 0

    async def refresh_device(device_id):
        nonlocal refresh_count
        refresh_count += 1
        order.append(f"refresh-{refresh_count}")
        if refresh_count == 1:
            first_refresh_started.set()
            await release_first_refresh.wait()

    api.set_fan_speed.side_effect = set_fan_speed
    runtime.command_executor = AtmeexCommandExecutor(refresh_device)

    first = asyncio.create_task(fan.async_set_percentage(50))
    await first_write_started.wait()
    second = asyncio.create_task(fan.async_set_percentage(75))
    release_first_write.set()
    await first_refresh_started.wait()

    assert order == ["write-4", "refresh-1"]

    release_first_refresh.set()
    await asyncio.gather(first, second)

    assert order == ["write-4", "refresh-1", "write-5", "refresh-2"]
~~~

Add this import:

~~~python
from custom_components.atmeex_cloud.command_executor import AtmeexCommandExecutor
~~~

- [ ] **Step 2: Run the fan regressions and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_fan.py::test_fan_speed_and_power_are_one_command_with_one_refresh tests/test_fan.py::test_fan_rejects_out_of_range_percentage tests/test_race_protection.py::test_second_fan_command_starts_after_first_confirmation_refresh
~~~

Expected: the first test fails with two refreshes, invalid input is accepted, and the old eager awaitable API is incompatible with the executor.

- [ ] **Step 3: Implement fan factories and compound pending values**

In custom_components/atmeex_cloud/fan.py, remove PENDING_COMMAND_TTL and remove tolerance arguments from property calls. Replace the command methods with:

~~~python
    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        if percentage is not None and not 0 <= percentage <= 100:
            raise self._invalid_value("percentage", percentage)
        speed = (
            self._percentage_to_speed(percentage)
            if percentage is not None
            else None
        )

        async def operation() -> None:
            if speed is not None:
                await self.api.set_fan_speed(self._device_id, speed)
            await self.api.set_power(self._device_id, True)

        pending: dict[str, Any] = {"pwr_on": True}
        if speed is not None:
            pending["fan_speed"] = speed
        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "turn on the fan"},
        )

    async def async_turn_off(self, **kwargs) -> None:
        async def operation() -> None:
            await self.api.set_power(self._device_id, False)

        await self._execute_command(
            operation,
            pending={"pwr_on": False},
            translation_placeholders={"action": "turn off the fan"},
        )

    async def async_set_percentage(self, percentage: int) -> None:
        if not 0 <= percentage <= 100:
            raise self._invalid_value("percentage", percentage)
        if percentage == 0:
            await self.async_turn_off()
            return
        speed = self._percentage_to_speed(percentage)
        turn_on = not self.is_on

        async def operation() -> None:
            await self.api.set_fan_speed(self._device_id, speed)
            if turn_on:
                await self.api.set_power(self._device_id, True)

        pending: dict[str, Any] = {"fan_speed": speed}
        if turn_on:
            pending["pwr_on"] = True
        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "set the fan speed"},
        )
~~~

- [ ] **Step 4: Run fan and race tests**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_fan.py tests/test_race_protection.py
~~~

Expected: all tests pass and the two compound fan paths each report exactly one refresh.

- [ ] **Step 5: Commit the fan migration**

~~~bash
git add custom_components/atmeex_cloud/fan.py tests/test_fan.py tests/test_race_protection.py
git commit -m "refactor: execute fan actions atomically"
~~~

### Task 4: Migrate climate writes and service validation

**Files:**

- Modify: custom_components/atmeex_cloud/climate.py
- Modify: tests/test_climate.py

- [ ] **Step 1: Add RED tests for compound temperature, complete pending maps, and validation**

Append to tests/test_climate.py:

~~~python
from homeassistant.exceptions import ServiceValidationError


@pytest.mark.asyncio
async def test_temperature_from_off_uses_atomic_api_write_and_one_refresh():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})

    api.set_power_and_heat.assert_awaited_once_with(1, True, 23.0)
    api.set_power.assert_not_awaited()
    api.set_target_temperature.assert_not_awaited()
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent.hvac_mode == HVACMode.HEAT


@pytest.mark.asyncio
async def test_fan_only_from_off_tracks_power_and_heater_fields():
    ent, _cond, _api, runtime = _make_entity_with_runtime({"pwr_on": False})

    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)

    assert runtime.command_executor.value_with_pending(1, "pwr_on", False) is True
    assert runtime.command_executor.value_with_pending(
        1, "u_temp_room", 225
    ) == -1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "field"),
    [
        (lambda ent: ent.async_set_temperature(temperature="bad"), "temperature"),
        (lambda ent: ent.async_set_fan_mode("8"), "fan_mode"),
        (lambda ent: ent.async_set_swing_mode("bad"), "swing_mode"),
        (lambda ent: ent.async_set_humidifier_stage(4), "humidifier_stage"),
    ],
)
async def test_climate_invalid_inputs_raise_translated_validation_error(call, field):
    ent, _cond, api, _runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await call(ent)

    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == field
    api.set_power.assert_not_awaited()
    api.set_fan_speed.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    api.set_humid_stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_humidifier_raises_unsupported_feature():
    ent, cond, api, _runtime = _make_entity_with_runtime()
    cond.pop("hum_stg")

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_humidity(50)

    assert raised.value.translation_key == "unsupported_device_feature"
    api.set_humid_stage.assert_not_awaited()
~~~

- [ ] **Step 2: Run the new climate tests and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py::test_temperature_from_off_uses_atomic_api_write_and_one_refresh tests/test_climate.py::test_fan_only_from_off_tracks_power_and_heater_fields tests/test_climate.py::test_climate_invalid_inputs_raise_translated_validation_error tests/test_climate.py::test_missing_humidifier_raises_unsupported_feature
~~~

Expected: FAIL because temperature uses two writes, pending fields are installed separately, and invalid or unsupported values silently return or clamp.

- [ ] **Step 3: Implement atomic HVAC and temperature methods**

Remove PENDING_COMMAND_TTL and tolerance arguments from climate state reads. Replace async_set_hvac_mode and async_set_temperature with:

~~~python
    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self.hvac_modes:
            raise self._invalid_value("hvac_mode", hvac_mode)

        if hvac_mode == HVACMode.OFF:
            async def operation() -> None:
                await self.api.set_power(self._device_id, False)

            await self._execute_command(
                operation,
                pending={"pwr_on": False},
                translation_placeholders={"action": "turn off climate control"},
            )
            return

        device_off = not bool(
            self._state_with_pending(
                "pwr_on",
                self._device_state.get("pwr_on"),
            )
        )
        if hvac_mode == HVACMode.FAN_ONLY:
            async def operation() -> None:
                if device_off:
                    await self.api.set_power_and_heat(
                        self._device_id, True, None
                    )
                else:
                    await self.api.set_heater_off(self._device_id)

            pending = {"u_temp_room": -1000}
            if device_off:
                pending["pwr_on"] = True
            await self._execute_command(
                operation,
                pending=pending,
                translation_placeholders={"action": "enter fan-only mode"},
            )
            return

        target_c = self._resolve_heat_target()

        async def operation() -> None:
            if device_off:
                await self.api.set_power_and_heat(
                    self._device_id, True, target_c
                )
            else:
                await self.api.set_target_temperature(
                    self._device_id, target_c
                )

        pending = {"u_temp_room": c_to_deci(target_c)}
        if device_off:
            pending["pwr_on"] = True
        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "enter heat mode"},
        )

    async def async_set_temperature(self, **kwargs) -> None:
        value = kwargs.get(ATTR_TEMPERATURE)
        if value is None:
            raise self._invalid_value("temperature", value)
        try:
            target = float(value)
        except (TypeError, ValueError) as err:
            raise self._invalid_value("temperature", value) from err
        if not self._attr_min_temp <= target <= self._attr_max_temp:
            raise self._invalid_value("temperature", value)

        device_off = not bool(
            self._state_with_pending(
                "pwr_on",
                self._device_state.get("pwr_on"),
            )
        )

        async def operation() -> None:
            if device_off:
                await self.api.set_power_and_heat(
                    self._device_id, True, target
                )
            else:
                await self.api.set_target_temperature(
                    self._device_id, target
                )

        pending = {"u_temp_room": c_to_deci(target)}
        if device_off:
            pending["pwr_on"] = True
        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "set target temperature"},
        )
~~~

- [ ] **Step 4: Implement humidity, fan, damper, and service factories**

Replace the remaining direct command methods with:

~~~python
    async def async_set_humidity(self, humidity: int) -> None:
        if not self._has_humidifier():
            raise self._unsupported_feature("humidifier")
        if not 0 <= humidity <= 100:
            raise self._invalid_value("humidity", humidity)
        stage = humidity_to_stage(humidity)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={"action": "set target humidity"},
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        if fan_mode not in FAN_MODES:
            raise self._invalid_value("fan_mode", fan_mode)
        speed = int(fan_mode)

        async def operation() -> None:
            await self.api.set_fan_speed(self._device_id, speed)

        await self._execute_command(
            operation,
            pending={"fan_speed": speed},
            translation_placeholders={"action": "set climate fan mode"},
        )

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        if swing_mode not in BREEZER_SWING_MODES:
            raise self._invalid_value("swing_mode", swing_mode)
        mode = BREEZER_SWING_MODES.index(swing_mode)
        power = mode != 3
        damper = 0 if mode == 3 else mode

        async def operation() -> None:
            await self.api.set_breezer_mode(self._device_id, mode)

        await self._execute_command(
            operation,
            pending={"pwr_on": power, "damp_pos": damper},
            translation_placeholders={"action": "set breezer mode"},
        )

    async def async_set_breezer_mode(self, mode: str) -> None:
        await self.async_set_swing_mode(mode)

    async def async_set_humidifier_stage(self, stage: int) -> None:
        if not self._has_humidifier():
            raise self._unsupported_feature("humidifier")
        if isinstance(stage, bool) or not isinstance(stage, int) or not 0 <= stage <= 3:
            raise self._invalid_value("humidifier_stage", stage)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={"action": "set humidifier stage"},
        )
~~~

- [ ] **Step 5: Run the climate command tests**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py -k "hvac or temperature or humidity or fan_mode or swing or humidifier"
~~~

Expected: all selected tests pass after replacing old no-op and clamping expectations with ServiceValidationError expectations.

- [ ] **Step 6: Commit climate command migration**

~~~bash
git add custom_components/atmeex_cloud/climate.py tests/test_climate.py
git commit -m "refactor: execute climate commands atomically"
~~~

### Task 5: Make each preset transition one executor operation

**Files:**

- Modify: custom_components/atmeex_cloud/climate.py
- Modify: tests/test_climate.py

- [ ] **Step 1: Add RED preset serialization tests**

Add `call` to the existing `unittest.mock` import, then append to
tests/test_climate.py:

~~~python
@pytest.mark.asyncio
async def test_sleep_preset_uses_one_refresh_for_mode_and_speed():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    ent.async_write_ha_state = MagicMock()

    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_sleep_mode.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_awaited_once_with(1, 2)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert runtime.command_executor.value_with_pending(
        1, "u_night", False
    ) is True
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_preset_mode_reads_pending_flags_during_delayed_confirmation():
    ent, _cond, _api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def blocked_refresh(_device_id):
        refresh_started.set()
        await release_refresh.wait()

    runtime.refresh_device.side_effect = blocked_refresh
    task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await refresh_started.wait()

    assert ent.preset_mode == PRESET_SLEEP

    release_refresh.set()
    await task


@pytest.mark.asyncio
async def test_rapid_sleep_to_auto_transition_uses_pending_previous_mode():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    first_refresh_started = asyncio.Event()
    release_first_refresh = asyncio.Event()
    refresh_calls = 0

    async def controlled_refresh(_device_id):
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            first_refresh_started.set()
            await release_first_refresh.wait()

    runtime.refresh_device.side_effect = controlled_refresh
    sleep_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await first_refresh_started.wait()
    auto_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_AUTO))
    await asyncio.sleep(0)

    assert ent.preset_mode == PRESET_AUTO

    release_first_refresh.set()
    await asyncio.gather(sleep_task, auto_task)
    api.set_sleep_mode.assert_has_awaits([call(1, True), call(1, False)])
    api.set_auto_mode.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_has_awaits([call(1, 2), call(1, 4)])
    assert refresh_calls == 2
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_preset_partial_failure_refreshes_once_before_error():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False}
    )
    ent.async_write_ha_state = MagicMock()
    api.set_fan_speed.side_effect = ApiError(
        "test_preset", "second write failed", status=503
    )

    with pytest.raises(HomeAssistantError):
        await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_sleep_mode.assert_awaited_once_with(1, True)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_invalid_preset_raises_service_validation_error():
    ent, _cond, api, runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_preset_mode("invalid")

    assert raised.value.translation_key == "invalid_command_value"
    api.set_auto_mode.assert_not_awaited()
    api.set_sleep_mode.assert_not_awaited()
    api.set_fan_speed.assert_not_awaited()
    runtime.refresh_device.assert_not_awaited()
~~~

- [ ] **Step 2: Run preset tests and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py -k "preset_mode_reads_pending or rapid_sleep_to_auto or sleep_preset_uses_one_refresh or preset_partial_failure or invalid_preset"
~~~

Expected: FAIL because the current preset path bypasses the lock, performs multiple refreshes, mutates local state before success, and accepts unknown preset values.

- [ ] **Step 3: Make the property consume pending flags and replace the preset method**

Replace `preset_mode` before changing the command method:

~~~python
    @property
    def preset_mode(self) -> str:
        """Return the newest local or cloud-backed preset."""
        local_preset = self._state_with_pending(
            "local_preset", self._local_preset
        )
        if local_preset == PRESET_BOOST or self._is_boost:
            return PRESET_BOOST
        night = bool(
            self._state_with_pending(
                "u_night", bool(self._device_state.get("u_night"))
            )
        )
        auto = bool(
            self._state_with_pending(
                "u_auto", bool(self._device_state.get("u_auto"))
            )
        )
        if night:
            return PRESET_SLEEP
        if auto:
            return PRESET_AUTO
        return PRESET_NONE
~~~

Replace async_set_preset_mode in custom_components/atmeex_cloud/climate.py:

~~~python
    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in self.preset_modes:
            raise self._invalid_value("preset_mode", preset_mode)

        # This is captured before the new generation is installed, so a queued
        # transition sees the preceding optimistic intent. Mutable restore
        # state is deliberately read later, inside the executor lock.
        old = self.preset_mode
        pending: dict[str, Any] = {
            "u_auto": preset_mode == PRESET_AUTO,
            "u_night": preset_mode == PRESET_SLEEP,
            "local_preset": (
                PRESET_BOOST if preset_mode == PRESET_BOOST else None
            ),
        }

        async def operation() -> None:
            # The factory runs only after the device lock is acquired. A prior
            # sleep/boost command has therefore already committed its saved fan
            # mode before this transition derives restore state.
            saved_before = self._saved_fan_mode
            current_fan = self.fan_mode
            base_fan = int(saved_before or current_fan or "1")
            saved_after = saved_before

            if old == PRESET_AUTO and preset_mode != PRESET_AUTO:
                await self.api.set_auto_mode(self._device_id, False)
            if old == PRESET_SLEEP and preset_mode != PRESET_SLEEP:
                await self.api.set_sleep_mode(self._device_id, False)

            leaving_override = old in {PRESET_SLEEP, PRESET_BOOST}
            entering_override = preset_mode in {PRESET_SLEEP, PRESET_BOOST}
            if leaving_override and not entering_override:
                if saved_before is not None:
                    await self.api.set_fan_speed(
                        self._device_id, int(saved_before)
                    )
                saved_after = None

            if preset_mode == PRESET_AUTO:
                if old != PRESET_AUTO:
                    await self.api.set_auto_mode(self._device_id, True)
            elif preset_mode == PRESET_SLEEP:
                if not leaving_override:
                    saved_after = str(base_fan)
                elif saved_before is None:
                    saved_after = str(base_fan)
                target = min(base_fan, int(self.sleep_max_fan_mode))
                if old != PRESET_SLEEP:
                    await self.api.set_sleep_mode(self._device_id, True)
                await self.api.set_fan_speed(self._device_id, target)
            elif preset_mode == PRESET_BOOST:
                if not leaving_override:
                    saved_after = str(base_fan)
                elif saved_before is None:
                    saved_after = str(base_fan)
                await self.api.set_fan_speed(
                    self._device_id, int(self.boost_fan_mode)
                )

            # Commit local restore state without another await, still inside
            # the executor lock. Partial failure/cancellation cannot publish a
            # half-committed local transition.
            self._saved_fan_mode = saved_after
            self._is_boost = preset_mode == PRESET_BOOST
            self._local_preset = (
                PRESET_BOOST if preset_mode == PRESET_BOOST else None
            )

        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "set climate preset"},
        )
        self.async_write_ha_state()
~~~

The operation intentionally retains multiple API calls where the current API exposes separate mode operations, but all of them and the one final refresh remain under the same device lock. Existing set_power_and_heat and set_breezer_mode operations continue to use the cloud's supported multi-field PUTs.

- [ ] **Step 4: Run all climate tests**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py
~~~

Expected: all tests pass; update legacy preset tests to expect one refresh and translated errors without changing preset names or entity IDs.

- [ ] **Step 5: Commit preset atomicity**

~~~bash
git add custom_components/atmeex_cloud/climate.py tests/test_climate.py
git commit -m "refactor: serialize climate preset transitions"
~~~

### Task 6: Migrate select and switch entities

**Files:**

- Modify: custom_components/atmeex_cloud/entity_base.py
- Modify: custom_components/atmeex_cloud/select.py
- Modify: custom_components/atmeex_cloud/switch.py
- Modify: tests/test_select.py
- Modify: tests/test_switch.py

- [ ] **Step 1: Add RED select validation and pending-map tests**

Append to tests/test_select.py:

~~~python
from homeassistant.exceptions import ServiceValidationError


@pytest.mark.asyncio
async def test_humidification_select_uses_executor_pending_value():
    hum, _breezer, _cond, api, coordinator = _make_selects({"hum_stg": 0})
    refresh = AsyncMock()
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=refresh,
    )
    hum._runtime = runtime
    hum._refresh_device_cb = refresh

    await hum.async_select_option("3")

    assert runtime.command_executor.value_with_pending(
        1, "hum_stg", 0
    ) == 3
    refresh.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_selects_reject_invalid_options():
    hum, breezer, _cond, api, coordinator = _make_selects()

    with pytest.raises(ServiceValidationError):
        await hum.async_select_option("invalid")
    with pytest.raises(ServiceValidationError):
        await breezer.async_select_option("invalid")

    api.set_humid_stage.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()
~~~

- [ ] **Step 2: Add a RED switch executor test**

Append to tests/test_switch.py:

~~~python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "method", "field", "expected"),
    [
        ("auto", "async_turn_on", "u_auto", True),
        ("sleep", "async_turn_off", "u_night", False),
        ("power", "async_turn_on", "pwr_on", True),
    ],
)
async def test_switches_publish_pending_through_executor(
    kind, method, field, expected
):
    if kind == "power":
        entity, _api, refresh = _make_power_switch(with_runtime=True)
    else:
        auto, sleep, _api, refresh = _make_switches(with_runtime=True)
        entity = auto if kind == "auto" else sleep

    await getattr(entity, method)()

    assert entity._runtime.command_executor.value_with_pending(
        42, field, not expected
    ) is expected
    refresh.assert_awaited_once_with(42)
~~~

- [ ] **Step 3: Run select and switch tests and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_select.py tests/test_switch.py
~~~

Expected: invalid selects silently return, humidification bypasses the executor, and switch methods still pass eager awaitables.

- [ ] **Step 4: Route both selects through factories**

Pass runtime=runtime when constructing AtmeexHumidificationSelect in async_setup_entry, add runtime to its constructor, and replace both select methods:

~~~python
    async def async_select_option(self, option: str) -> None:
        if option not in HUM_OPTIONS:
            raise self._invalid_value("humidification_option", option)
        stage = 0 if option == "off" else int(option)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={
                "action": "set humidification stage"
            },
        )
        self._attr_current_option = option
~~~

~~~python
    async def async_select_option(self, option: str) -> None:
        if option not in BREEZER_OPTIONS:
            raise self._invalid_value("breezer_option", option)
        mode = BREEZER_OPTIONS.index(option)
        power = mode != 3
        damper = 0 if mode == 3 else mode

        async def operation() -> None:
            await self.api.set_breezer_mode(self._device_id, mode)

        await self._execute_command(
            operation,
            pending={"pwr_on": power, "damp_pos": damper},
            translation_placeholders={"action": "set breezer mode"},
        )
        self._attr_current_option = option
~~~

- [ ] **Step 5: Route all switch methods through one shared helper**

Add this method to _BaseSwitch:

~~~python
    async def _set_boolean(
        self,
        api_method,
        *,
        attribute: str,
        value: bool,
        action: str,
    ) -> None:
        async def operation() -> None:
            await api_method(self._device_id, value)

        await self._execute_command(
            operation,
            pending={attribute: value},
            translation_placeholders={"action": action},
        )
~~~

Use it in the six entity methods:

~~~python
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_auto_mode,
            attribute="u_auto",
            value=True,
            action="enable AutoNanny",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_auto_mode,
            attribute="u_auto",
            value=False,
            action="disable AutoNanny",
        )
~~~

~~~python
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_sleep_mode,
            attribute="u_night",
            value=True,
            action="enable sleep mode",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_sleep_mode,
            attribute="u_night",
            value=False,
            action="disable sleep mode",
        )
~~~

~~~python
    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_power,
            attribute="pwr_on",
            value=True,
            action="turn on the device",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_power,
            attribute="pwr_on",
            value=False,
            action="turn off the device",
        )
~~~

- [ ] **Step 6: Run select, switch, and shared entity tests**

Now that climate, fan, select, and switch all pass factories, remove the
temporary eager-awaitable branch and its `pending_attr`, `pending_value`, and
`error_message` parameters from `AtmeexEntityMixin._execute_command`. Restore
the locked final signature shown in Task 2's callable branch and remove the
unused `Awaitable` import. Verify migration completeness first:

~~~bash
rg -n 'pending_attr=|pending_value=|error_message=' custom_components/atmeex_cloud
~~~

Expected: no matches and exit status 1.

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_select.py tests/test_switch.py tests/test_entity_base.py
~~~

Expected: all tests pass.

- [ ] **Step 7: Commit select and switch migration**

~~~bash
git add custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/select.py custom_components/atmeex_cloud/switch.py tests/test_select.py tests/test_switch.py
git commit -m "refactor: serialize select and switch commands"
~~~

### Task 7: Add translated command and validation exceptions

**Files:**

- Modify: custom_components/atmeex_cloud/strings.json
- Modify: custom_components/atmeex_cloud/translations/en.json
- Modify: custom_components/atmeex_cloud/translations/ru.json
- Modify: tests/test_entity_base.py

- [ ] **Step 1: Add a RED translation parity test**

Append to tests/test_entity_base.py:

~~~python
import json
from pathlib import Path


def test_command_exception_translations_match():
    root = Path(__file__).parents[1] / "custom_components" / "atmeex_cloud"
    documents = [
        json.loads((root / "strings.json").read_text()),
        json.loads((root / "translations" / "en.json").read_text()),
        json.loads((root / "translations" / "ru.json").read_text()),
    ]

    for document in documents:
        assert set(document["exceptions"]) >= {
            "command_failed",
            "invalid_command_value",
            "unsupported_device_feature",
        }
        assert "{action}" in document["exceptions"]["command_failed"]["message"]
        assert "{field}" in document["exceptions"]["invalid_command_value"]["message"]
        assert "{value}" in document["exceptions"]["invalid_command_value"]["message"]
        assert "{feature}" in document["exceptions"]["unsupported_device_feature"]["message"]
~~~

- [ ] **Step 2: Run the translation test and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_entity_base.py::test_command_exception_translations_match
~~~

Expected: FAIL with KeyError: 'exceptions'.

- [ ] **Step 3: Add the exact English exception object**

Add this top-level object to both custom_components/atmeex_cloud/strings.json and custom_components/atmeex_cloud/translations/en.json:

~~~json
"exceptions": {
  "command_failed": {
    "message": "Unable to {action} because the Atmeex Cloud command failed."
  },
  "invalid_command_value": {
    "message": "{value} is not a valid value for {field}."
  },
  "unsupported_device_feature": {
    "message": "This Atmeex device does not support {feature}."
  }
}
~~~

- [ ] **Step 4: Add the exact Russian exception object**

Add this top-level object to custom_components/atmeex_cloud/translations/ru.json:

~~~json
"exceptions": {
  "command_failed": {
    "message": "Не удалось выполнить действие «{action}»: команда Atmeex Cloud завершилась ошибкой."
  },
  "invalid_command_value": {
    "message": "Значение {value} недопустимо для поля {field}."
  },
  "unsupported_device_feature": {
    "message": "Устройство Atmeex не поддерживает функцию {feature}."
  }
}
~~~

- [ ] **Step 5: Run translation and error-boundary tests**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_entity_base.py
~~~

Expected: all tests pass, and ApiError assertions check translation_domain, translation_key, and translation_placeholders rather than raw cloud messages.

- [ ] **Step 6: Commit translations**

~~~bash
git add custom_components/atmeex_cloud/strings.json custom_components/atmeex_cloud/translations/en.json custom_components/atmeex_cloud/translations/ru.json tests/test_entity_base.py
git commit -m "feat: translate Atmeex command errors"
~~~

### Task 8: Verify the atomic-command subsystem and repository

**Files:**

- Modify: tests/test_refresh_device.py

- [ ] **Step 1: Add the final one-refresh integration assertion**

Extend the existing test_refresh_device_updates_coordinator_data body in tests/test_refresh_device.py by adding these lines after its state_after assertion:

~~~python
    runtime.api.get_device.reset_mock()

    async def operation() -> None:
        return

    await runtime.command_executor.async_execute(
        1,
        operation,
        pending={"pwr_on": False},
        translation_key="command_failed",
    )

    runtime.api.get_device.assert_awaited_once_with(1)
    assert runtime.coordinator.data["states"]["1"]["pwr_on"] is False
~~~

- [ ] **Step 2: Run the focused subsystem**

Run:

~~~bash
.venv/bin/python -m pytest -q -W error::RuntimeWarning tests/test_command_executor.py tests/test_runtime.py tests/test_entity_base.py tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py tests/test_race_protection.py tests/test_refresh_device.py
~~~

Expected: all selected tests pass with no RuntimeWarning about an un-awaited coroutine.

- [ ] **Step 3: Run the complete suite without hiding the known lifecycle warning**

Run:

~~~bash
.venv/bin/python -m pytest -q
~~~

Expected: the complete suite passes with 0 failures. The pre-existing
WebSocket-startup RuntimeWarning may remain until Plan 4; no command-executor
test or production path emits an un-awaited coroutine warning.

- [ ] **Step 4: Compile the integration**

Run:

~~~bash
.venv/bin/python -m compileall -q custom_components/atmeex_cloud
~~~

Expected: exit status 0 and no output.

- [ ] **Step 5: Review the public contract**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py -k "unique_id or service or options or setup_entry"
~~~

Expected: all selected compatibility tests pass; entity unique IDs, service names, accepted values, and automation-facing names remain unchanged.

- [ ] **Step 6: Commit final atomic-command regressions**

~~~bash
git add tests/test_refresh_device.py
git commit -m "test: verify atomic command refresh integration"
~~~

## Completion criteria

- Every production entity call passes a CommandCoroutineFactory, never an already-created coroutine.
- A generation is allocated and all expected pending fields are installed before lock acquisition.
- Older failure or cancellation cleanup cannot erase a newer generation.
- The per-device lock covers every write in the logical action and its one final RecoveryRefresh.
- Different device IDs execute concurrently.
- A successful write followed by a failed confirmation GET remains a successful Home Assistant action and keeps pending values until confirmation or the 10-second bound.
- A failed or partially applied multi-request operation clears only its generation, requests one authoritative targeted recovery refresh, and raises a translated HomeAssistantError.
- Invalid input and unsupported capabilities raise translated ServiceValidationError instances.
- Existing entity unique IDs, service names, service schemas, option keys, translations outside the new exception object, and automation inputs remain unchanged.
- The focused command tests and compileall command are green without un-awaited
  command coroutine warnings; the complete suite is green and leaves the known
  WebSocket-startup warning for Plan 4.
