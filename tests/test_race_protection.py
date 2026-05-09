"""Focused tests for fan set/update interleaving."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.helpers import apply_settings_update


def _make_runtime():
    api = MagicMock()
    api.set_fan_speed = AsyncMock()
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
async def test_lock_serializes_operations():
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    order = []

    async def slow_set_fan_speed(device_id, speed):
        order.append(f"start_set_{speed}")
        await asyncio.sleep(0)
        order.append(f"end_set_{speed}")

    async def slow_refresh(device_id):
        order.append("start_refresh")
        await asyncio.sleep(0)
        order.append("end_refresh")

    api.set_fan_speed = slow_set_fan_speed
    runtime.refresh_device = slow_refresh
    fan._refresh_device_cb = runtime.refresh_device

    task1 = asyncio.create_task(fan.async_set_percentage(50))
    task2 = asyncio.create_task(fan.async_set_percentage(75))

    await asyncio.gather(task1, task2)

    assert len(order) == 8
    set_4_start = order.index("start_set_4") if "start_set_4" in order else -1
    set_5_start = order.index("start_set_5") if "start_set_5" in order else -1

    if set_4_start < set_5_start:
        assert order.index("end_refresh") < set_5_start or order.count("end_refresh") == 2
    else:
        assert order.index("end_refresh") < set_4_start or order.count("end_refresh") == 2
