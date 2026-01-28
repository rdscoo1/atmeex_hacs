import pytest
from unittest.mock import AsyncMock
from types import SimpleNamespace

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.helpers import to_bool
from custom_components.atmeex_cloud.const import DOMAIN, PLATFORMS
from custom_components.atmeex_cloud.api import AtmeexDevice


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
        ("", False),
        ("foo", True),
        (None, False),
    ],
)
def test_to_bool(value, expected):
    assert to_bool(value) is expected


def test_normalize_device_state_basic():
    """Test normalization converts API fan_speed (0-6) to HA fan_speed (1-7).
    
    API returns fan_speed=3, which should be converted to HA fan_speed=4.
    """
    item = {
        "condition": {
            "pwr_on": 1,
            "fan_speed": "3",  # API speed 3 → HA speed 4
            "damp_pos": "2",
            "hum_stg": "1",
            "u_temp_room": "215",
            "hum_room": 47.9,
            "temp_room": 198.3,
        },
        "online": False,
    }
    out = atmeex_init._normalize_device_state(item)
    assert out["pwr_on"] is True
    assert out["fan_speed"] == 4  # API 3 → HA 4
    assert out["damp_pos"] == 2
    assert out["hum_stg"] == 1
    assert out["u_temp_room"] == 215
    assert out["hum_room"] == 47
    assert out["temp_room"] == 198
    assert out["online"] is False


def test_normalize_device_state_uses_settings_and_fan_fallback():
    """Test fallback to settings.u_fan_speed when condition.fan_speed is 0.
    
    API settings.u_fan_speed=4 → HA fan_speed=5.
    Device is online if condition has time field.
    """
    item = {
        "condition": {
            "pwr_on": None,
            "fan_speed": 0,
            "time": "2026-01-27 21:24:15",  # Fresh condition data = online
        },
        "settings": {
            "u_pwr_on": "1",
            "u_fan_speed": 4.2,  # API speed 4 → HA speed 5
            "u_damp_pos": "1",
            "u_temp_room": 205.6,
            "u_hum_stg": "2",
        },
    }
    out = atmeex_init._normalize_device_state(item)
    assert out["pwr_on"] is True
    assert out["fan_speed"] == 5  # API 4 → HA 5
    assert out["damp_pos"] == 1
    assert out["u_temp_room"] == 205
    assert out["hum_stg"] == 2
    assert out["online"] is True  # Has condition.time = online


@pytest.mark.asyncio
async def test_async_setup_entry_happy_path(monkeypatch):
    # подменяем AtmeexApi
    created_apis = []

    class FakeApi:
        def __init__(self, session):
            self.session = session
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            dev_raw = {
                "id": 1,
                "name": "Dev1",
                "model": "test-model",
                "online": True,
                "condition": {"pwr_on": 1, "fan_speed": 3},
                "settings": {},
            }
            dev = AtmeexDevice.from_raw(dev_raw)

            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

            created_apis.append(self)



    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)

    # подменяем DataUpdateCoordinator на простую реализацию
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

    # подменяем async_get_clientsession
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    # hass-заглушка
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    def _add_update_listener(_listener):
    # в HA возвращает callback, который снимет listener
        return lambda: None

    def _async_on_unload(_cb):
        # в HA регистрирует callback на выгрузку entry
        return None


    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 60},  # где нужно
        entry_id="entry1",
        add_update_listener=_add_update_listener,
        async_on_unload=_async_on_unload,
    )

    result = await atmeex_init.async_setup_entry(hass, entry)
    assert result is True

    # Новое поведение: данные лежат в entry.runtime_data
    runtime = entry.runtime_data
    assert runtime.api is created_apis[0]
    assert runtime.coordinator.data["devices"][0]["id"] == 1
    assert runtime.coordinator.data["states"]["1"]["pwr_on"] is True
    assert runtime.coordinator.data["states"]["1"]["fan_speed"] == 4  # API 3 → HA 4


@pytest.mark.asyncio
async def test_async_unload_entry_clears_data(monkeypatch):
    hass = SimpleNamespace(
        data={DOMAIN: {"entry1": {"some": "data"}}},
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=SimpleNamespace(websocket_manager=None)
    )

    result = await atmeex_init.async_unload_entry(hass, entry)
    assert result is True
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.asyncio
async def test_async_setup_entry_uses_options_update_interval(monkeypatch):
    created_apis = []

    class FakeApi:
        def __init__(self, session):
            self.session = session
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)
            created_apis.append(self)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    captured = {}

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            captured["update_interval"] = update_interval
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

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    def _add_update_listener(_listener):
        # в HA возвращает callback, который снимет listener
        return lambda: None

    def _async_on_unload(_cb):
        # в HA регистрирует callback на выгрузку entry
        return None


    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 60},  # где нужно
        entry_id="entry1",
        add_update_listener=_add_update_listener,
        async_on_unload=_async_on_unload,
    )


    ok = await atmeex_init.async_setup_entry(hass, entry)
    assert ok is True
    assert captured["update_interval"].total_seconds() == 60
