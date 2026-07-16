"""Focused tests for fan set/update interleaving."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.command_executor import AtmeexCommandExecutor
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.helpers import apply_settings_update


def _make_runtime():
    api = MagicMock()
    api.set_fan_speed = AsyncMock()
    api.set_power = AsyncMock()
    coordinator = SimpleNamespace(
        data={"states": {"1": {"pwr_on": True, "fan_speed": 3}}},
        async_request_refresh=AsyncMock(),
    )
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    return runtime, api, coordinator


def _make_fan_entity_with_runtime():
    runtime, api, coordinator = _make_runtime()
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    fan = AtmeexFanEntity(
        coordinator=coordinator,
        api=api,
        entry_id="entry1",
        device=dev,
        refresh_device_cb=runtime.refresh_device,
        runtime=runtime,
    )
    return fan, runtime, api, coordinator


@pytest.mark.asyncio
async def test_set_percentage_records_pending():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()

    await fan.async_set_percentage(75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)


@pytest.mark.asyncio
async def test_ws_settings_update_does_not_override_in_flight_set():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    api_started = asyncio.Event()
    api_release = asyncio.Event()

    async def slow_set_fan_speed(device_id, speed):
        api_started.set()
        await api_release.wait()

    api.set_fan_speed.side_effect = slow_set_fan_speed

    task = asyncio.create_task(fan.async_set_percentage(75))
    await api_started.wait()

    coordinator.data["states"]["1"] = apply_settings_update(
        coordinator.data["states"]["1"],
        {"u_fan_speed": 2},
    )

    assert coordinator.data["states"]["1"]["fan_speed"] == 3
    assert fan.percentage == 71

    api_release.set()
    await task


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


@pytest.mark.asyncio
async def test_fan_commands_for_different_devices_do_not_share_a_lock():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    coordinator.data["states"]["2"] = {"pwr_on": True, "fan_speed": 3}
    second_device = AtmeexDevice.from_raw(
        {"id": 2, "name": "Dev2", "model": "m", "online": True}
    )
    second_fan = AtmeexFanEntity(
        coordinator=coordinator,
        api=api,
        entry_id="entry1",
        device=second_device,
        refresh_device_cb=runtime.refresh_device,
        runtime=runtime,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def set_fan_speed(device_id, speed):
        if device_id == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()

    api.set_fan_speed.side_effect = set_fan_speed
    runtime.command_executor = AtmeexCommandExecutor(AsyncMock())

    first = asyncio.create_task(fan.async_set_percentage(50))
    await first_started.wait()
    second = asyncio.create_task(second_fan.async_set_percentage(75))
    await second_started.wait()

    assert first.done() is False
    release_first.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_queued_fan_command_rechecks_confirmed_power_after_owner_failure():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    coordinator.data["states"]["1"]["pwr_on"] = False
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()

    async def set_fan_speed(device_id, speed):
        if speed == 4:
            first_write_started.set()
            await release_first_write.wait()
            raise ApiError("set_fan_speed", "first write failed", status=503)

    api.set_fan_speed.side_effect = set_fan_speed
    api.set_power = AsyncMock()

    first = asyncio.create_task(fan.async_set_percentage(50))
    await first_write_started.wait()
    second = asyncio.create_task(fan.async_set_percentage(75))
    await asyncio.sleep(0)

    assert runtime.get_pending(1, "fan_speed").value == 5
    assert runtime.get_pending(1, "pwr_on").value is True

    release_first_write.set()
    with pytest.raises(HomeAssistantError):
        await first
    await second

    api.set_power.assert_awaited_once_with(1, True)
    assert coordinator.data["states"]["1"]["pwr_on"] is False


@pytest.mark.asyncio
async def test_queued_speed_powers_on_after_turn_off_confirmation_failure():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    first_refresh_started = asyncio.Event()
    release_first_refresh = asyncio.Event()
    refresh_count = 0
    remote_power = True

    async def set_power(device_id, power):
        nonlocal remote_power
        remote_power = power

    async def refresh_device(device_id):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 1:
            first_refresh_started.set()
            await release_first_refresh.wait()
            raise ApiError("refresh_device", "confirmation failed", status=503)

    api.set_power.side_effect = set_power
    runtime.command_executor = AtmeexCommandExecutor(refresh_device)

    turn_off = asyncio.create_task(fan.async_turn_off())
    await first_refresh_started.wait()
    set_speed = asyncio.create_task(fan.async_set_percentage(75))
    await asyncio.sleep(0)

    assert runtime.get_pending(1, "pwr_on").value is True

    release_first_refresh.set()
    await asyncio.gather(turn_off, set_speed)

    assert api.set_power.await_args_list == [
        call(1, False),
        call(1, True),
    ]
    assert coordinator.data["states"]["1"]["pwr_on"] is True
    assert remote_power is True
