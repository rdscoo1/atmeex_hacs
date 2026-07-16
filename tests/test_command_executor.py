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


@pytest.mark.asyncio
async def test_numeric_device_id_aliases_share_one_serial_queue():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    started: list[str] = []
    active = 0
    maximum_active = 0

    async def operation(label: str, release: asyncio.Event | None = None) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        started.append(label)
        if label == "one":
            first_started.set()
        if release is not None:
            await release.wait()
        active -= 1

    executor = AtmeexCommandExecutor(AsyncMock())
    one = asyncio.create_task(
        executor.async_execute(
            1,
            lambda: operation("one", release_first),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await first_started.wait()
    zero_one = asyncio.create_task(
        executor.async_execute(
            "01",
            lambda: operation("zero-one"),
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    many_zeroes = asyncio.create_task(
        executor.async_execute(
            "0001",
            lambda: operation("many-zeroes"),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)

    assert started == ["one"]
    release_first.set()
    await asyncio.gather(one, zero_one, many_zeroes)

    assert started == ["one", "zero-one", "many-zeroes"]
    assert maximum_active == 1
    assert set(executor.device_locks) == {"1"}


@pytest.mark.asyncio
async def test_multi_field_generation_survives_field_by_field_confirmation():
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    await executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": True, "fan_speed": 4},
        translation_key="command_failed",
    )

    assert executor.confirm("01", "pwr_on", True) is True
    assert executor.get_pending("0001", "pwr_on") is None
    assert executor.get_pending(1, "fan_speed") is not None
    assert executor.confirm(1, "fan_speed", 4) is True
    assert executor.pending_commands == {}


@pytest.mark.asyncio
async def test_confirming_older_field_never_erases_a_newer_generation():
    newer_started = asyncio.Event()
    release_newer = asyncio.Event()
    executor = AtmeexCommandExecutor(AsyncMock())

    await executor.async_execute(
        1,
        AsyncMock(),
        pending={"pwr_on": True, "fan_speed": 4},
        translation_key="command_failed",
    )

    async def newer_operation() -> None:
        newer_started.set()
        await release_newer.wait()

    newer = asyncio.create_task(
        executor.async_execute(
            "01",
            newer_operation,
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    await newer_started.wait()

    assert executor.confirm(1, "pwr_on", True) is False
    assert executor.get_pending(1, "pwr_on").value is False
    assert executor.confirm(1, "fan_speed", 4) is True
    assert executor.get_pending(1, "pwr_on").value is False

    newer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await newer


def test_expired_generations_are_removed_from_all_public_pending_views():
    executor = AtmeexCommandExecutor(AsyncMock(), pending_ttl=0)
    executor.set_pending("01", "pwr_on", True)
    executor.set_pending(1, "fan_speed", 4)

    assert executor.value_with_pending("0001", "pwr_on", False) is False
    assert executor.get_pending(1, "fan_speed") is None
    assert executor.pending_commands == {}


def test_public_pending_reads_do_not_expose_mutable_internal_values():
    executor = AtmeexCommandExecutor(AsyncMock())
    source = {"stages": [1]}
    executor.set_pending(1, "from_get", source)
    executor.set_pending("01", "from_value", {"stages": [2]})
    executor.set_pending("0001", "from_view", {"stages": [3]})
    executor.set_pending(1, "scalar", True)
    source["stages"].append(99)

    from_get = executor.get_pending(1, "from_get")
    assert from_get is not None
    assert from_get.value == {"stages": [1]}
    from_get.value["stages"].append(99)

    from_value = executor.value_with_pending(1, "from_value", None)
    assert from_value == {"stages": [2]}
    from_value["stages"].append(99)

    from_view = executor.pending_commands["1"]["from_view"]
    from_view.value["stages"].append(99)

    assert executor.get_pending(1, "from_get").value == {"stages": [1]}
    assert executor.get_pending(1, "from_value").value == {"stages": [2]}
    assert executor.get_pending(1, "from_view").value == {"stages": [3]}
    assert executor.get_pending(1, "scalar").value is True


@pytest.mark.asyncio
async def test_synchronous_factory_failure_cleans_generation_and_recovers():
    refresh_device = AsyncMock()
    executor = AtmeexCommandExecutor(refresh_device)

    def failing_factory():
        raise RuntimeError("factory failed")

    with pytest.raises(RuntimeError, match="factory failed"):
        await executor.async_execute(
            1,
            failing_factory,
            pending={"pwr_on": True, "fan_speed": 4},
            translation_key="command_failed",
        )

    refresh_device.assert_awaited_once_with(1)
    assert executor.get_pending(1, "pwr_on") is None
    assert executor.get_pending(1, "fan_speed") is None


@pytest.mark.asyncio
async def test_recovery_bug_does_not_mask_a_translated_api_error():
    executor = AtmeexCommandExecutor(
        AsyncMock(side_effect=RuntimeError("recovery bug"))
    )
    api_error = ApiError("set_speed", "write failed", status=503)

    async def operation() -> None:
        raise api_error

    with pytest.raises(HomeAssistantError) as raised:
        await executor.async_execute(
            1,
            operation,
            pending={"fan_speed": 7},
            translation_key="command_failed",
        )

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "command_failed"
    assert raised.value.__cause__ is api_error
    assert executor.get_pending(1, "fan_speed") is None


@pytest.mark.asyncio
async def test_recovery_bug_does_not_mask_operation_cancellation():
    operation_started = asyncio.Event()
    never_release = asyncio.Event()
    executor = AtmeexCommandExecutor(
        AsyncMock(side_effect=RuntimeError("recovery bug"))
    )

    async def operation() -> None:
        operation_started.set()
        await never_release.wait()

    task = asyncio.create_task(
        executor.async_execute(
            1,
            operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await operation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert executor.get_pending(1, "pwr_on") is None


@pytest.mark.asyncio
async def test_unexpected_confirmation_exception_propagates_and_keeps_pending():
    refresh_device = AsyncMock(side_effect=RuntimeError("bad confirmation"))
    executor = AtmeexCommandExecutor(refresh_device)

    with pytest.raises(RuntimeError, match="bad confirmation"):
        await executor.async_execute(
            1,
            AsyncMock(),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )

    assert executor.value_with_pending(1, "pwr_on", False) is True
    assert executor.confirm(1, "pwr_on", True) is False


@pytest.mark.asyncio
async def test_cancellation_during_confirmation_recovers_and_cleans_generation():
    confirmation_started = asyncio.Event()
    release_confirmation = asyncio.Event()
    calls = 0

    async def refresh_device(device_id: int | str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            confirmation_started.set()
            await release_confirmation.wait()

    executor = AtmeexCommandExecutor(refresh_device)
    task = asyncio.create_task(
        executor.async_execute(
            1,
            AsyncMock(),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await confirmation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == 2
    assert executor.get_pending(1, "pwr_on") is None


@pytest.mark.asyncio
async def test_remove_device_cleans_idle_state_through_any_alias():
    executor = AtmeexCommandExecutor(AsyncMock())
    await executor.async_execute(
        "01",
        AsyncMock(),
        pending={"pwr_on": True, "fan_speed": 4},
        translation_key="command_failed",
    )

    executor.remove_device("0001")

    assert executor.get_pending(1, "pwr_on") is None
    assert executor.get_pending("01", "fan_speed") is None
    assert "1" not in executor.pending_commands
    assert "1" not in executor.device_locks


@pytest.mark.asyncio
async def test_remove_device_during_command_does_not_create_a_bypass_lock():
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    async def first_operation() -> None:
        order.append("first-start")
        first_started.set()
        await release_first.wait()
        order.append("first-end")

    async def second_operation() -> None:
        order.append("second")
        second_started.set()

    executor = AtmeexCommandExecutor(AsyncMock())
    first = asyncio.create_task(
        executor.async_execute(
            1,
            first_operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await first_started.wait()

    executor.remove_device("01")
    second = asyncio.create_task(
        executor.async_execute(
            "0001",
            second_operation,
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)

    assert second_started.is_set() is False
    release_first.set()
    await asyncio.gather(first, second)
    assert order == ["first-start", "first-end", "second"]


@pytest.mark.asyncio
async def test_remove_device_retains_a_compatibility_lock_while_it_is_held():
    second_started = asyncio.Event()
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()

    executor.remove_device("01")
    command = asyncio.create_task(
        executor.async_execute(
            "0001",
            lambda: second_started.set() or asyncio.sleep(0),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)

    assert second_started.is_set() is False
    compatibility_lock.release()
    await command
    assert second_started.is_set() is True


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_drop_a_deferred_compatibility_lock():
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()
    executor.remove_device("01")

    waiter_factory = MagicMock()
    waiter = asyncio.create_task(
        executor.async_execute(
            "0001",
            waiter_factory,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    waiter_factory.assert_not_called()

    third_started = asyncio.Event()

    async def third_operation() -> None:
        third_started.set()

    third = asyncio.create_task(
        executor.async_execute(
            1,
            third_operation,
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)

    assert third_started.is_set() is False
    compatibility_lock.release()
    await third
    assert third_started.is_set() is True


@pytest.mark.asyncio
async def test_deferred_compatibility_lock_is_cleaned_after_external_release():
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()
    executor.remove_device("01")

    waiter = asyncio.create_task(
        executor.async_execute(
            "0001",
            MagicMock(),
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert "1" in executor.device_locks
    assert executor._remove_when_idle == {"1"}
    assert executor._lock_users == {}

    compatibility_lock.release()
    await asyncio.sleep(0)

    assert "1" not in executor.device_locks
    assert executor._remove_when_idle == set()
    assert executor._lock_users == {}
    assert executor._release_observers == {}


@pytest.mark.asyncio
async def test_release_observer_preserves_a_queued_compatibility_waiter():
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()
    executor.remove_device(1)

    compatibility_waiter_started = asyncio.Event()
    release_compatibility_waiter = asyncio.Event()

    async def compatibility_waiter() -> None:
        async with compatibility_lock:
            compatibility_waiter_started.set()
            await release_compatibility_waiter.wait()

    waiter = asyncio.create_task(compatibility_waiter())
    await asyncio.sleep(0)
    compatibility_lock.release()
    await compatibility_waiter_started.wait()

    command_started = asyncio.Event()

    async def operation() -> None:
        command_started.set()

    command = asyncio.create_task(
        executor.async_execute(
            "01",
            operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await asyncio.sleep(0)
    assert command_started.is_set() is False

    release_compatibility_waiter.set()
    await asyncio.gather(waiter, command)
    assert command_started.is_set() is True
    assert "1" not in executor.device_locks


@pytest.mark.asyncio
async def test_remove_during_release_handoff_cannot_bypass_awakened_waiter():
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()

    waiter_started = asyncio.Event()
    release_waiter = asyncio.Event()

    async def compatibility_waiter() -> None:
        async with compatibility_lock:
            waiter_started.set()
            await release_waiter.wait()

    waiter = asyncio.create_task(compatibility_waiter())
    await asyncio.sleep(0)
    compatibility_lock.release()
    # The awakened waiter has not resumed yet: Lock.locked() is false, but the
    # handoff still owns the serialization slot.
    executor.remove_device("01")

    command_started = asyncio.Event()

    async def operation() -> None:
        command_started.set()

    command = asyncio.create_task(
        executor.async_execute(
            "0001",
            operation,
            pending={"pwr_on": True},
            translation_key="command_failed",
        )
    )
    await waiter_started.wait()
    await asyncio.sleep(0)

    command_was_blocked = command_started.is_set() is False
    release_waiter.set()
    await asyncio.gather(waiter, command)
    assert command_was_blocked is True
    assert command_started.is_set() is True


@pytest.mark.asyncio
async def test_cancelled_release_handoff_is_cleaned_on_next_loop_turn():
    executor = AtmeexCommandExecutor(AsyncMock())
    compatibility_lock = asyncio.Lock()
    executor.device_locks["1"] = compatibility_lock
    await compatibility_lock.acquire()

    waiter = asyncio.create_task(compatibility_lock.acquire())
    await asyncio.sleep(0)
    compatibility_lock.release()
    waiter.cancel()
    executor.remove_device("01")

    assert "1" in executor.device_locks
    assert executor._remove_when_idle == {"1"}
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)

    assert "1" not in executor.device_locks
    assert executor._remove_when_idle == set()
    assert executor._release_observers == {}


@pytest.mark.asyncio
async def test_api_error_translation_preserves_key_placeholders_and_cause():
    executor = AtmeexCommandExecutor(AsyncMock())
    api_error = ApiError("set_speed", "write failed", status=503)

    async def operation() -> None:
        raise api_error

    with pytest.raises(HomeAssistantError) as raised:
        await executor.async_execute(
            1,
            operation,
            pending={"fan_speed": 7},
            translation_key="command_failed",
            translation_placeholders={"action": "set fan speed"},
        )

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "command_failed"
    assert raised.value.translation_placeholders == {"action": "set fan speed"}
    assert raised.value.__cause__ is api_error
