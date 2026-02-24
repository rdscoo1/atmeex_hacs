"""Tests for Atmeex binary sensor entities."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import (
    AtmeexNoWaterSensor,
    AtmeexOnlineSensor,
)


RAW_DEVICE = {"id": 7, "name": "My Breezer", "model": "X100", "online": True}


def _make_sensors(state: dict | None = None):
    dev = AtmeexDevice.from_raw(RAW_DEVICE)

    coordinator = SimpleNamespace(
        data={
            "device_map": {"7": dev},
            "states": {"7": state or {"online": True, "no_water": False}},
        },
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )

    online = AtmeexOnlineSensor(coordinator=coordinator, device=dev, entry_id="entry1")
    no_water = AtmeexNoWaterSensor(
        coordinator=coordinator,
        device=dev,
        entry_id="entry1",
    )
    return online, no_water


def test_online_sensor_properties():
    online, _ = _make_sensors({"online": True})
    assert online.unique_id == "7_online"
    assert online.device_class == BinarySensorDeviceClass.CONNECTIVITY
    assert online.is_on is True


def test_online_sensor_reports_offline():
    online, _ = _make_sensors({"online": False})
    assert online.is_on is False


def test_no_water_sensor_properties():
    _, no_water = _make_sensors({"online": True, "no_water": True})
    assert no_water.unique_id == "7_no_water"
    assert no_water.device_class == BinarySensorDeviceClass.PROBLEM
    assert no_water.is_on is True


def test_no_water_sensor_reports_water_present():
    _, no_water = _make_sensors({"online": True, "no_water": False})
    assert no_water.is_on is False
