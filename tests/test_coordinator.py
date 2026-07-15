"""Unit tests for AtmeexCoordinator._async_update_data."""
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
    coord.setup_update(api=api, fire_logbook_event=MagicMock())
    return coord, api


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
async def test_poll_does_not_overwrite_fresher_targeted_refresh_state():
    """A targeted device refresh recorded after poll_start_mono must not be overwritten.

    Mirrors the _ws_device_update_ts guard but for _refresh_device_update_ts.
    """
    coord, api = _make_coordinator()

    # First poll to establish coordinator.data
    data1 = await coord._async_update_data()
    coord.data = data1
    coord.last_update_success = True

    # Simulate a targeted refresh that wrote a fresher state after the poll began
    fresh_state = dict(data1["states"]["1"])
    fresh_state["fan_speed"] = 99  # sentinel value the poll can never produce
    coord.data["states"]["1"] = fresh_state
    coord._refresh_device_update_ts["1"] = float("inf")  # always newer than any poll

    # Second poll — API still returns original stale data
    data2 = await coord._async_update_data()

    # The targeted-refresh state must be preserved, not overwritten by stale poll
    assert data2["states"]["1"]["fan_speed"] == 99


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
