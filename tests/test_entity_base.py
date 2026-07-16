"""Shared entity mixin contract tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.climate import BREEZER_SWING_MODES
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import AtmeexOnlineSensor
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.sensor import AtmeexCO2Sensor
from custom_components.atmeex_cloud.select import AtmeexBreezerSelect, BREEZER_OPTIONS
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData
from tests.test_climate import _make_entity, _make_entity_with_runtime
from tests.test_fan import _make_fan_entity
from tests.test_select import _make_selects
from tests.test_switch import _make_power_switch, _make_switches


def _make_auto_switch():
    auto, _sleep, api, refresh_cb = _make_switches()
    return auto, None, api, refresh_cb


def _make_sleep_switch():
    _auto, sleep, api, refresh_cb = _make_switches()
    return sleep, None, api, refresh_cb


def _make_power_switch_standard():
    power, api, refresh_cb = _make_power_switch()
    return power, None, api, refresh_cb


def _make_humidification_select():
    hum, _breezer, _cond, api, coord = _make_selects()
    return hum, None, api, coord


def _make_breezer_select():
    _hum, breezer, _cond, api, coord = _make_selects()
    return breezer, None, api, coord


@pytest.mark.parametrize(
    ("make_entity", "set_pending", "read_value", "expected"),
    [
        (
            lambda: _make_entity_with_runtime({"pwr_on": False}),
            lambda ent, runtime: runtime.set_pending(1, "pwr_on", True),
            lambda ent: ent.hvac_mode,
            HVACMode.HEAT,
        ),
        (
            lambda: (_make_switches({"online": True, "u_auto": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_auto", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_switches({"online": True, "u_night": False}, with_runtime=True)[1],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_night", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_power_switch({"online": True, "pwr_on": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "pwr_on", True),
            lambda ent: ent.is_on,
            True,
        ),
    ],
)
def test_pending_state_via_mixin(make_entity, set_pending, read_value, expected):
    made = make_entity()
    ent = made[0]
    runtime = getattr(ent, "_runtime", made[3] if len(made) > 3 else None)

    set_pending(ent, runtime)

    assert read_value(ent) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_entity", "configure_failure", "action", "match"),
    [
        (
            _make_entity_with_runtime,
            lambda ent, api: setattr(
                api.set_power,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_set_hvac_mode(HVACMode.OFF),
            "Failed to turn off",
        ),
        (
            _make_entity_with_runtime,
            lambda ent, api: setattr(
                api.set_fan_speed,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_set_fan_mode("5"),
            "Failed to set fan mode",
        ),
        (
            _make_entity,
            lambda ent, api: setattr(
                api.set_breezer_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_set_swing_mode(BREEZER_SWING_MODES[1]),
            "Failed to set swing mode",
        ),
        (
            _make_fan_entity,
            lambda ent, api: setattr(
                api.set_fan_speed,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_set_percentage(75),
            "Failed to set fan speed",
        ),
        (
            _make_auto_switch,
            lambda ent, api: setattr(
                api.set_auto_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "Failed to enable AutoNanny",
        ),
        (
            _make_sleep_switch,
            lambda ent, api: setattr(
                api.set_sleep_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "Failed to enable Sleep Mode",
        ),
        (
            _make_power_switch_standard,
            lambda ent, api: setattr(
                api.set_power,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "Failed to turn on",
        ),
        (
            _make_humidification_select,
            lambda ent, api: setattr(
                api.set_humid_stage,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_select_option("2"),
            "Failed to set humidification stage",
        ),
        (
            _make_breezer_select,
            lambda ent, api: setattr(
                api.set_breezer_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_select_option(BREEZER_OPTIONS[1]),
            "Failed to set work mode",
        ),
    ],
)
async def test_api_error_translates_to_home_assistant_error(
    make_entity, configure_failure, action, match
):
    made = make_entity()
    ent = made[0]
    api = made[2]
    configure_failure(ent, api)

    with pytest.raises(HomeAssistantError, match=match):
        await action(ent)


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


@pytest.mark.asyncio
async def test_entity_reuses_one_fallback_executor():
    ent, _cond, _api = _make_entity()

    async def operation() -> None:
        return

    await ent._execute_command(
        operation,
        pending={"fan_speed": 3},
        translation_key="command_failed",
    )
    first = ent._fallback_command_executor
    await ent._execute_command(
        operation,
        pending={"fan_speed": 5},
        translation_key="command_failed",
    )

    assert ent._fallback_command_executor is first


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
async def test_legacy_waiter_cancellation_preserves_newer_generation():
    ent, _cond, api, runtime = _make_entity_with_runtime()
    lock = runtime.get_device_lock(1)
    await lock.acquire()
    waiter = asyncio.create_task(
        ent._execute_command(
            api.set_fan_speed(1, 5),
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await asyncio.sleep(0)
    newer_generation = runtime.set_pending(1, "fan_speed", 7)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None
    assert pending.value == 7
    assert pending.generation == newer_generation
    api.set_fan_speed.assert_not_awaited()
    lock.release()


@pytest.mark.asyncio
async def test_legacy_future_waiter_cancellation_cancels_unstarted_future():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    lock = runtime.get_device_lock(1)
    await lock.acquire()
    future = asyncio.get_running_loop().create_future()
    waiter = asyncio.create_task(
        ent._execute_command(
            future,
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await asyncio.sleep(0)
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert future.cancelled()
    assert runtime.get_pending(1, "fan_speed") is None
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
async def test_legacy_api_error_survives_unexpected_recovery_failure():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    command_error = ApiError("legacy_command", "boom", status=500)
    runtime.refresh_device.side_effect = RuntimeError("recovery bug")

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


@pytest.mark.asyncio
async def test_legacy_command_runs_lockless_when_runtime_has_no_executor():
    ent, _cond, api = _make_entity()
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=ent.coordinator,
        refresh_device=None,
        command_executor=None,
    )
    ent._runtime = runtime
    ent._refresh = AsyncMock()

    await ent._execute_command(
        api.set_fan_speed(1, 5),
        pending_attr="fan_speed",
        pending_value=5,
    )

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    ent._refresh.assert_awaited_once()
    assert runtime.command_executor is None


@pytest.mark.asyncio
async def test_legacy_cancellation_during_confirmation_recovers_and_keeps_newer():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    confirmation_started = asyncio.Event()
    recovery_started = asyncio.Event()
    never_release = asyncio.Event()
    refresh_calls = 0

    async def refresh_device(device_id: int | str) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        assert runtime.get_device_lock(device_id).locked()
        if refresh_calls == 1:
            confirmation_started.set()
            await never_release.wait()
        else:
            recovery_started.set()

    runtime.refresh_device.side_effect = refresh_device

    async def completed_write() -> None:
        return

    task = asyncio.create_task(
        ent._execute_command(
            completed_write(),
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await confirmation_started.wait()
    newer_generation = runtime.set_pending(1, "fan_speed", 7)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert recovery_started.is_set()
    assert refresh_calls == 2
    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None
    assert pending.value == 7
    assert pending.generation == newer_generation
    assert runtime.get_device_lock(1).locked() is False


@pytest.mark.asyncio
async def test_legacy_cancellation_during_api_error_recovery_wins():
    ent, _cond, _api, runtime = _make_entity_with_runtime()
    command_error = ApiError("legacy_command", "boom", status=500)
    recovery_started = asyncio.Event()
    never_release = asyncio.Event()

    async def refresh_device(device_id: int | str) -> None:
        assert runtime.get_device_lock(device_id).locked()
        recovery_started.set()
        await never_release.wait()

    runtime.refresh_device.side_effect = refresh_device

    async def failed_write() -> None:
        raise command_error

    task = asyncio.create_task(
        ent._execute_command(
            failed_write(),
            pending_attr="fan_speed",
            pending_value=5,
        )
    )
    await recovery_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert runtime.get_pending(1, "fan_speed") is None
    assert runtime.get_device_lock(1).locked() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refresh_error",
    [
        ApiError("legacy_confirmation", "unavailable", status=503),
        asyncio.TimeoutError(),
    ],
)
async def test_legacy_success_ignores_typed_confirmation_failure(refresh_error):
    ent, _cond, api, runtime = _make_entity_with_runtime()
    runtime.refresh_device.side_effect = refresh_error

    await ent._execute_command(
        api.set_fan_speed(1, 5),
        pending_attr="fan_speed",
        pending_value=5,
    )

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    runtime.refresh_device.assert_awaited_once_with(1)
    pending = runtime.get_pending(1, "fan_speed")
    assert pending is not None
    assert pending.value == 5
    assert runtime.get_device_lock(1).locked() is False


def _make_lifecycle_coordinator():
    remove_listener = MagicMock()
    coordinator = MagicMock()
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    coordinator.data = {
        "device_map": {"1": dev},
        "states": {
            "1": {
                "online": True,
                "pwr_on": True,
                "fan_speed": 3,
                "damp_pos": 2,
                "hum_stg": 1,
                "co2_ppm": 420,
            }
        },
    }
    coordinator.async_add_listener = MagicMock(return_value=remove_listener)
    return coordinator, dev, remove_listener


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_factory",
    [
        lambda coord, dev: _make_entity()[0],
        lambda coord, dev: AtmeexFanEntity(coord, MagicMock(), "entry1", dev),
        lambda coord, dev: _make_power_switch()[0],
        lambda coord, dev: AtmeexBreezerSelect(coord, MagicMock(), dev),
        lambda coord, dev: AtmeexCO2Sensor(coord, dev, "entry1"),
        lambda coord, dev: AtmeexOnlineSensor(coord, dev, "entry1"),
    ],
)
async def test_entity_lifecycle_registers_and_removes_coordinator_listener(entity_factory):
    coordinator, dev, remove_listener = _make_lifecycle_coordinator()
    ent = entity_factory(coordinator, dev)
    if getattr(ent, "coordinator", None) is not coordinator:
        ent.coordinator.async_add_listener = coordinator.async_add_listener
        remove_listener = coordinator.async_add_listener.return_value

    await ent.async_added_to_hass()
    ent._call_on_remove_callbacks()
    await ent.async_will_remove_from_hass()

    ent.coordinator.async_add_listener.assert_called_once()
    remove_listener.assert_called_once()
