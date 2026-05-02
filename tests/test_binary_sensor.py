"""Tests for Atmeex binary sensor entities."""
from __future__ import annotations

import datetime
import time

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud import AtmeexRuntimeData
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers.entity import EntityCategory

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import (
    AtmeexNoWaterSensor,
    AtmeexOnlineSensor,
    async_setup_entry,
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


@pytest.mark.asyncio
async def test_async_setup_entry_skips_no_water_sensor_without_humidifier():
    dev = AtmeexDevice.from_raw(RAW_DEVICE)
    listeners: list = []
    coordinator = SimpleNamespace(
        data={
            "device_map": {"7": dev},
            "states": {"7": {"online": True}},
        },
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=runtime,
        async_on_unload=lambda cb: None,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(None, entry, _add_entities)

    assert [type(entity) for entity in entities] == [AtmeexOnlineSensor]
    assert len(listeners) == 1

    coordinator.data["states"]["7"]["no_water"] = True
    listeners[0]()

    assert {type(entity) for entity in entities} == {
        AtmeexOnlineSensor,
        AtmeexNoWaterSensor,
    }


def test_online_sensor_has_diagnostic_entity_category():
    """Online sensor should carry EntityCategory.DIAGNOSTIC so it's hidden from the main card."""
    online, _ = _make_sensors()
    assert online._attr_entity_category == EntityCategory.DIAGNOSTIC


def test_online_sensor_not_available_when_coordinator_stale():
    """available must return False when the coordinator hasn't updated for > 3× interval."""
    dev = AtmeexDevice.from_raw(RAW_DEVICE)
    coordinator = SimpleNamespace(
        data={"device_map": {"7": dev}, "states": {"7": {"online": True}}},
        last_update_success=True,
        last_success_ts=time.time() - 500,           # 500 s ago
        update_interval=datetime.timedelta(seconds=30),  # 3× = 90 s
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )
    online = AtmeexOnlineSensor(coordinator=coordinator, device=dev, entry_id="e")
    assert online.available is False


def test_online_sensor_available_when_coordinator_fresh():
    """available must return True when last_success_ts is recent."""
    dev = AtmeexDevice.from_raw(RAW_DEVICE)
    coordinator = SimpleNamespace(
        data={"device_map": {"7": dev}, "states": {"7": {"online": True}}},
        last_update_success=True,
        last_success_ts=time.time() - 10,
        update_interval=datetime.timedelta(seconds=30),
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )
    online = AtmeexOnlineSensor(coordinator=coordinator, device=dev, entry_id="e")
    assert online.available is True
