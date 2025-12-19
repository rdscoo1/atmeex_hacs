import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.atmeex_cloud import async_setup_entry
from custom_components.atmeex_cloud.const import DOMAIN
import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import AtmeexDevice

class FakeApi:
    """Фейковый API для проверки refresh_device без реального HA."""

    def __init__(self, session):
        # начальное состояние: устройство включено
        dev_initial_raw = {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 3},
            "settings": {},
        }
        self._dev_initial = AtmeexDevice.from_raw(dev_initial_raw)

        # состояние после refresh_device: устройство выключено
        dev_refreshed_raw = {
            "id": 1,
            "name": "Dev1",
            "model": "test-model",
            "online": True,
            "condition": {"pwr_on": 0, "fan_speed": 3},
            "settings": {},
        }
        self._dev_refreshed = AtmeexDevice.from_raw(dev_refreshed_raw)

        self.async_init = AsyncMock()
        self.login = AsyncMock()

        # первый полный опрос — список устройств (включённое)
        self.get_devices = AsyncMock(return_value=[self._dev_initial])

        # считаем вызовы get_device
        self._get_device_call_count = 0

        def _get_device_side_effect(device_id):
            """Первая дочитка — включённый девайс, далее — выключенный."""
            self._get_device_call_count += 1
            if self._get_device_call_count == 1:
                return self._dev_initial
            return self._dev_refreshed

        # точечное дочтение: сначала включённый, затем выключенный
        self.get_device = AsyncMock(side_effect=_get_device_side_effect)


@pytest.mark.asyncio
async def test_refresh_device_updates_coordinator_data(monkeypatch):
    # подменяем AtmeexApi на наш фейк
    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)

    # подменяем DataUpdateCoordinator на простую реализацию, как в test_init
    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "DataUpdateCoordinator", DummyCoordinator)

    # подменяем async_get_clientsession, чтобы не создавать реальную сессию
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    # hass-заглушка без реального Home Assistant
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    # entry-заглушка с нужными полями
    entry = SimpleNamespace(
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        options={},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )


    # запуск setup_entry создаст FakeApi, DummyCoordinator и refresh_device
    result = await async_setup_entry(hass, entry)
    assert result is True

    runtime = entry.runtime_data
    coordinator = runtime.coordinator

    # sanity-check: после первого refresh устройство есть и pwr_on=True
    state_before = coordinator.data["states"]["1"]
    assert state_before["pwr_on"] is True

    # вызываем refresh_device
    await runtime.refresh_device(1)

    state_after = coordinator.data["states"]["1"]
    assert state_after["pwr_on"] is False
