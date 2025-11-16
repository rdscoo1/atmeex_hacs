import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from custom_components.atmeex_cloud import async_setup_entry
from custom_components.atmeex_cloud.const import DOMAIN
import custom_components.atmeex_cloud as atmeex_init


class FakeApi:
    """Фейковый API для проверки refresh_device без реального HA."""

    def __init__(self, session):
        self._session = session
        self.login_called = False
        self.get_device_calls = []
        self._token = "t"

    async def async_init(self):
        return None

    async def login(self, email, password):
        self.login_called = True

    async def get_devices(self, fallback: bool = False):
        # одно устройство, онлайн, включено
        return [
            {"id": 1, "name": "Dev", "condition": {"pwr_on": True}},
        ]

    async def get_device(self, device_id):
        self.get_device_calls.append(device_id)
        # вернём обновлённое состояние pwr_on = False
        return {"id": device_id, "condition": {"pwr_on": False}}


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
        entry_id="entry1",
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
