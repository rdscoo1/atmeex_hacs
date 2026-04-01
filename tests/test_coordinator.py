"""Unit tests for AtmeexCoordinator._async_update_data."""
import logging
import time

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud.api import AtmeexDevice, ApiError
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
async def test_update_data_raises_auth_failed_on_401():
    """ApiError(401) from get_devices should propagate as ConfigEntryAuthFailed."""
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(side_effect=ApiError("unauthorized", status=401))

    from homeassistant.exceptions import ConfigEntryAuthFailed
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()
    assert isinstance(coord.last_api_error, ApiError)


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
