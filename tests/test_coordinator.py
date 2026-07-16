"""Unit tests for AtmeexCoordinator._async_update_data."""
import asyncio
import logging
import time

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
    AtmeexRateLimitError,
)
from custom_components.atmeex_cloud.const import EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _make_coordinator(devices=None, get_device_side_effect=None):
    """Create a coordinator with a fake API for testing update logic."""
    dev_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
               "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    default_dev = AtmeexDevice.from_raw(dev_raw)
    devices = devices if devices is not None else [default_dev]

    devices_by_id = {d.id: d for d in devices}

    api = MagicMock()
    api.get_devices = AsyncMock(return_value=devices)
    api.get_device = AsyncMock(
        side_effect=get_device_side_effect or (lambda did: devices_by_id.get(did, default_dev))
    )
    api._retry_count = 0

    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
    )
    coord = AtmeexCoordinator(
        hass, logging.getLogger("test"), name="test",
        update_interval=None,
    )
    coord.setup_update(
        api=api,
        state_store=AtmeexStateStore(),
        fire_logbook_event=MagicMock(),
    )
    return coord, api


@pytest.mark.asyncio
async def test_poll_baseline_preserves_later_websocket_field_with_event_barrier():
    stale = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 1, "temp_in": 230},
            "settings": {},
        }
    )
    store = AtmeexStateStore()
    store.apply_inventory([stale], store.capture_all())
    started = asyncio.Event()
    release = asyncio.Event()
    coord, api = _make_coordinator(devices=[stale])
    coord.setup_update(
        api=api,
        state_store=store,
        fire_logbook_event=MagicMock(),
    )

    async def blocked_inventory():
        started.set()
        await release.wait()
        return [stale]

    api.get_devices = AsyncMock(side_effect=blocked_inventory)
    api.get_device = AsyncMock(return_value=stale)
    update_task = asyncio.create_task(coord._async_update_data())
    await started.wait()
    store.apply_websocket_delta("1", state_delta={"pwr_on": False})
    release.set()
    data = await update_task
    assert data["states"]["1"]["pwr_on"] is False
    assert data["states"]["1"]["temp_in"] == 230
    assert coord.state_store is store


@pytest.mark.asyncio
async def test_poll_race_accepts_unrelated_field_while_preserving_websocket_field():
    current = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 1, "temp_in": 170},
            "settings": {},
        }
    )
    polled = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 1, "temp_in": 230},
            "settings": {},
        }
    )
    store = AtmeexStateStore()
    store.apply_inventory([current], store.capture_all())
    started = asyncio.Event()
    release = asyncio.Event()
    coord, api = _make_coordinator(devices=[polled])
    coord.setup_update(
        api=api,
        state_store=store,
        fire_logbook_event=MagicMock(),
    )

    async def blocked_inventory():
        started.set()
        await release.wait()
        return [polled]

    api.get_devices = AsyncMock(side_effect=blocked_inventory)
    api.get_device = AsyncMock(return_value=polled)
    update_task = asyncio.create_task(coord._async_update_data())
    await started.wait()
    store.apply_websocket_delta("1", state_delta={"pwr_on": False})
    release.set()

    data = await update_task

    assert data["states"]["1"]["pwr_on"] is False
    assert data["states"]["1"]["temp_in"] == 230


@pytest.mark.asyncio
async def test_malformed_nested_inventory_maps_to_update_failed_and_preserves_snapshot():
    valid = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 1},
            "settings": {},
        }
    )
    malformed = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": "sometimes", "fan_speed": 2},
            "settings": {},
        }
    )
    store = AtmeexStateStore()
    store.apply_inventory([valid], store.capture_all())
    store.apply_websocket_delta("1", state_delta={"fan_speed": 7})
    before = store.data
    before_revisions = dict(store.capture_device("1").revisions)
    coord, api = _make_coordinator(devices=[malformed])
    coord.setup_update(
        api=api,
        state_store=store,
        fire_logbook_event=MagicMock(),
    )

    with pytest.raises(UpdateFailed, match="Atmeex API update failed") as exc_info:
        await coord._async_update_data()

    assert isinstance(exc_info.value.__cause__, AtmeexProtocolError)
    assert store.data is before
    assert store.capture_device("1").revisions == before_revisions


@pytest.mark.asyncio
async def test_authoritative_absence_removes_device_after_two_successful_polls():
    device = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1},
            "settings": {},
        }
    )
    coord, api = _make_coordinator(devices=[device])
    initial = await coord._async_update_data()
    assert "1" in initial["device_map"]
    api.get_devices = AsyncMock(return_value=[])
    api.get_device = AsyncMock()

    first_absence = await coord._async_update_data()
    second_absence = await coord._async_update_data()

    assert "1" in first_absence["device_map"]
    assert second_absence == {"devices": [], "device_map": {}, "states": {}}
    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_poll_does_not_advance_authoritative_absence():
    device = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1},
            "settings": {},
        }
    )
    coord, api = _make_coordinator(devices=[device])
    initial = await coord._async_update_data()
    api.get_devices = AsyncMock(return_value=[])
    first_absence = await coord._async_update_data()
    api.get_devices = AsyncMock(
        side_effect=AtmeexConnectionError("get_devices", "offline")
    )

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

    assert coord.state_store.data is first_absence
    assert coord.state_store.data is initial
    assert "1" in coord.state_store.data["device_map"]
    api.get_devices = AsyncMock(return_value=[])
    second_absence = await coord._async_update_data()
    assert "1" not in second_absence["device_map"]


@pytest.mark.asyncio
async def test_metrics_are_not_part_of_comparable_coordinator_data():
    coord, api = _make_coordinator()
    api._retry_count = 3

    first = await coord._async_update_data()
    second = await coord._async_update_data()

    assert set(first) == {"devices", "device_map", "states"}
    assert second is first
    assert coord.always_update is False
    assert coord.last_success_ts is not None
    assert coord.avg_latency_ms is not None
    assert coord.request_retries == 3


@pytest.mark.asyncio
async def test_unexpected_coordinator_programming_error_propagates():
    coord, api = _make_coordinator()

    class _UnexpectedBug(RuntimeError):
        pass

    api.get_devices = AsyncMock(side_effect=_UnexpectedBug("programming bug"))

    with pytest.raises(_UnexpectedBug, match="programming bug"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_data_builds_states():
    coord, api = _make_coordinator()
    data = await coord._async_update_data()
    assert "1" in data["states"]
    assert data["states"]["1"]["pwr_on"] is True
    assert data["device_map"]["1"].id == 1
    assert coord.last_api_error is None


@pytest.mark.asyncio
async def test_update_data_preserves_offline_devices():
    """Devices from previous poll that disappear from API should be preserved."""
    dev1_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
                "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    dev2_raw = {"id": 2, "name": "Dev2", "model": "m", "online": True,
                "condition": {"pwr_on": 0, "fan_speed": 1}, "settings": {}}
    dev1 = AtmeexDevice.from_raw(dev1_raw)
    dev2 = AtmeexDevice.from_raw(dev2_raw)

    coord, api = _make_coordinator(devices=[dev1, dev2])

    # First poll — both devices
    data1 = await coord._async_update_data()
    coord.data = data1
    coord.last_update_success = True
    assert "1" in data1["device_map"]
    assert "2" in data1["device_map"]

    # Second poll — dev2 disappeared
    api.get_devices = AsyncMock(return_value=[dev1])
    api.get_device = AsyncMock(return_value=dev1)
    data2 = await coord._async_update_data()
    # dev2 should still be in device_map from merge
    assert "2" in data2["device_map"]


@pytest.mark.asyncio
async def test_fetch_devices_uses_one_authoritative_inventory_call():
    coord, api = _make_coordinator(devices=[])

    assert await coord._fetch_devices_safely() == []
    api.get_devices.assert_awaited_once_with()
    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError],
)
async def test_update_data_maps_transient_typed_errors_to_update_failed(error_type):
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(side_effect=error_type("get_devices", "failed"))

    with pytest.raises(UpdateFailed, match="Atmeex API update failed"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_data_maps_typed_auth_error_to_config_entry_auth_failed():
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(
        side_effect=AtmeexAuthenticationError(
            "get_devices",
            "authentication rejected",
            status=401,
        )
    )

    with pytest.raises(ConfigEntryAuthFailed, match="Atmeex authentication failed"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_fetch_devices_primary_unexpected_exception_propagates():
    """A non-network exception from the primary get_devices call must propagate.

    The over-broad `except Exception` swallows programming errors (e.g., a
    NameError inside the API code) and makes them hard to diagnose.
    After the fix, only network exceptions (TimeoutError, aiohttp.ClientError)
    are caught; everything else propagates to the coordinator's error handler.
    """
    coord, api = _make_coordinator()

    class _UnexpectedBug(RuntimeError):
        pass

    api.get_devices = AsyncMock(side_effect=_UnexpectedBug("programming bug"))

    with pytest.raises(_UnexpectedBug):
        await coord._fetch_devices_safely()


def test_fire_api_error_event_throttles_repeated_events(monkeypatch):
    fired = MagicMock()
    coord, _api = _make_coordinator()
    coord._fire_logbook_event = fired

    now = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)

    coord._fire_api_error_event({"message": "first"})
    coord._fire_api_error_event({"message": "second"})

    fired.assert_called_once_with(EVENT_API_ERROR, {"message": "first"})

    now += WS_LOGBOOK_MIN_INTERVAL_SEC + 0.1
    coord._fire_api_error_event({"message": "third"})

    assert fired.call_count == 2
    fired.assert_called_with(
        EVENT_API_ERROR,
        {"message": "third", "suppressed_errors": 1},
    )
