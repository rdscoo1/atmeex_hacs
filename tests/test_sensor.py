import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from homeassistant.helpers.json import json_dumps

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.sensor import (
    AtmeexCO2Sensor,
    AtmeexHumiditySensor,
    AtmeexInletTempSensor,
    async_setup_entry,
)


class DummyCoordinator:
    def __init__(self, data, last_success_ts=None, last_api_error=None):
        self.data = data
        self.last_update_success = True
        self.last_update_success_time = None
        self.last_success_ts = last_success_ts
        self.last_api_error = last_api_error

    async def async_request_refresh(self):
        # для совместимости, но тестам обычно не нужен
        pass


@pytest.mark.asyncio
async def test_sensor_exposes_basic_attrs(hass):
    # coordinator с 2 устройствами и заполненной диагностикой
    coord = DummyCoordinator(
        data={
            "devices": [{"id": 1}, {"id": 2}],
            "states": {},
        },
        last_success_ts=1234567890.0,
        last_api_error="some error",
    )

    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),  # api тут не нужен
        coordinator=coord,
        refresh_device=AsyncMock(),
    )

    entry = SimpleNamespace(
        domain=DOMAIN,
        title="user@example.com",
        entry_id="entry1",
        data={},
        options={},
        runtime_data=runtime,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(entities) == 1
    sensor = entities[0]

    # native_value = количество устройств (если ты так реализовал)
    assert sensor.native_value == 2

    attrs = sensor.extra_state_attributes
    assert attrs["device_count"] == 2
    assert attrs["last_success_ts"] == 1234567890.0
    assert attrs["last_api_error"] == "some error"
    assert attrs["websocket_connected"] is None
    assert attrs["websocket_last_message_age_sec"] is None
    # если в сенсоре формируешь last_success_utc — просто проверяем наличие
    assert "last_success_utc" in attrs


@pytest.mark.asyncio
async def test_sensor_handles_no_data(hass):
    coord = DummyCoordinator(data={}, last_success_ts=None, last_api_error=None)

    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),
        coordinator=coord,
        refresh_device=AsyncMock(),
    )

    entry = SimpleNamespace(
        domain=DOMAIN,
        title="user@example.com",
        entry_id="entry1",
        data={},
        options={},
        runtime_data=runtime,
    )

    entities: list = []

    # тоже обычная функция
    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    assert len(entities) == 1
    sensor = entities[0]
    attrs = sensor.extra_state_attributes

    # при отсутствии devices — либо 0, либо None, в зависимости от реализации
    assert attrs["device_count"] in (0, None)
    assert "last_success_ts" in attrs
    assert "last_api_error" in attrs


def test_device_sensors_available_follows_online_state():
    dev = AtmeexDevice.from_raw({"id": 1, "name": "Dev1", "model": "m", "online": True})
    coordinator = SimpleNamespace(
        data={
            "device_map": {"1": dev},
            "states": {
                "1": {
                    "online": False,
                    "co2_ppm": 500,
                    "temp_in": 210,
                    "hum_room": 40,
                }
            },
        }
    )

    co2 = AtmeexCO2Sensor(coordinator, dev, "entry1")
    inlet = AtmeexInletTempSensor(coordinator, dev, "entry1")
    humidity = AtmeexHumiditySensor(coordinator, dev, "entry1")

    assert co2.available is False
    assert inlet.available is False
    assert humidity.available is False

    coordinator.data["states"]["1"]["online"] = True
    assert co2.available is True
    assert inlet.available is True
    assert humidity.available is True


@pytest.mark.asyncio
async def test_diagnostics_sensor_shows_websocket_state(hass):
    coord = DummyCoordinator(
        data={"devices": [{"id": 1}], "states": {}},
        last_success_ts=1234567890.0,
        last_api_error=None,
    )

    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),
        coordinator=coord,
        refresh_device=AsyncMock(),
        websocket_manager=SimpleNamespace(is_connected=True, last_message_age=12.34),
    )

    entry = SimpleNamespace(
        domain=DOMAIN,
        title="user@example.com",
        entry_id="entry1",
        data={},
        options={},
        runtime_data=runtime,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)
    sensor = entities[0]
    attrs = sensor.extra_state_attributes

    assert attrs["websocket_connected"] is True
    assert attrs["websocket_last_message_age_sec"] == 12.3


@pytest.mark.asyncio
async def test_diagnostics_sensor_serializes_api_error(hass):
    coord = DummyCoordinator(
        data={"devices": [{"id": 1}], "states": {}},
        last_success_ts=1234567890.0,
        last_api_error=ApiError("boom", status=500),
    )

    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),
        coordinator=coord,
        refresh_device=AsyncMock(),
    )

    entry = SimpleNamespace(
        domain=DOMAIN,
        title="user@example.com",
        entry_id="entry1",
        data={},
        options={},
        runtime_data=runtime,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(hass, entry, _add_entities)

    sensor = entities[0]
    attrs = sensor.extra_state_attributes

    assert attrs["last_api_error"] == "boom"
    assert attrs["last_api_error_status"] == 500
    json_dumps(attrs)
