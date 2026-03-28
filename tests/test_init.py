import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.helpers import to_bool, _normalize_device_state
from custom_components.atmeex_cloud.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.api import ApiError
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


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
    out = _normalize_device_state(item)
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
    out = _normalize_device_state(item)
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

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

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
        options={"update_interval": 60, "enable_websocket": False},
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
@pytest.mark.parametrize(
    ("configured_interval", "expected_interval"),
    [
        (60, 60),
        (1, 10),
        (999, 300),
    ],
)
async def test_async_setup_entry_uses_options_update_interval(
    monkeypatch, configured_interval, expected_interval
):
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

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

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
        options={
            "update_interval": configured_interval,
            "enable_websocket": False,
        },
        entry_id="entry1",
        add_update_listener=_add_update_listener,
        async_on_unload=_async_on_unload,
    )


    ok = await atmeex_init.async_setup_entry(hass, entry)
    assert ok is True
    assert captured["update_interval"].total_seconds() == expected_interval


@pytest.mark.asyncio
async def test_refresh_device_coalesces_parallel_requests(monkeypatch):
    created_apis = []

    class FakeApi:
        def __init__(self, session):
            self.session = session
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._gate = asyncio.Event()
            self._block_get_device = False

            dev_raw = {
                "id": 1,
                "name": "Dev1",
                "model": "test-model",
                "online": True,
                "condition": {"pwr_on": 1, "fan_speed": 2},
                "settings": {},
            }
            self._dev = AtmeexDevice.from_raw(dev_raw)
            self.get_devices = AsyncMock(return_value=[self._dev])
            self.get_device_calls = 0

            async def _get_device(_device_id):
                self.get_device_calls += 1
                if self._block_get_device:
                    await self._gate.wait()
                return self._dev

            self.get_device = AsyncMock(side_effect=_get_device)
            created_apis.append(self)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

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

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )

    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 30, "enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    api = created_apis[0]
    api.get_device_calls = 0
    api._block_get_device = True

    t1 = asyncio.create_task(runtime.refresh_device(1))
    t2 = asyncio.create_task(runtime.refresh_device(1))

    try:
        for _ in range(20):
            if api.get_device_calls >= 1:
                break
            await asyncio.sleep(0)
        assert api.get_device_calls == 1
    finally:
        api._gate.set()
        await asyncio.gather(t1, t2, return_exceptions=True)

    assert api.get_device_calls == 1


@pytest.mark.asyncio
async def test_websocket_batch_message_updates_coordinator_once(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    created_callbacks = []
    created_ws_managers = []

    class FakeWebSocketManager:
        def __init__(self, session, token_getter, on_message, on_auth_failure=None):
            self.session = session
            self.token_getter = token_getter
            self.on_message = on_message
            self.on_auth_failure = on_auth_failure
            created_callbacks.append(on_message)
            created_ws_managers.append(self)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", FakeWebSocketManager)

    class FakeSession:
        async def ws_connect(self, *args, **kwargs):  # pragma: no cover - should not be called
            raise AssertionError("ws_connect must not be called in this test")

    class FakeApi:
        def __init__(self, session):
            self.session = session
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token-123"

            dev1 = AtmeexDevice.from_raw(
                {
                    "id": 1,
                    "name": "Dev1",
                    "model": "test-model",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 2},
                    "settings": {},
                }
            )
            dev2 = AtmeexDevice.from_raw(
                {
                    "id": 2,
                    "name": "Dev2",
                    "model": "test-model",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 1},
                    "settings": {},
                }
            )
            self._devices = [dev1, dev2]
            self.get_devices = AsyncMock(return_value=self._devices)

            async def _get_device(device_id):
                for dev in self._devices:
                    if dev.id == int(device_id):
                        return dev
                raise ValueError("unknown device")

            self.get_device = AsyncMock(side_effect=_get_device)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: FakeSession())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None
            self.update_calls = 0

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.update_calls += 1
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 30, "enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    assert len(created_ws_managers) == 1
    assert len(created_callbacks) == 1

    callback = created_callbacks[0]
    callback(
        {
            "type": "condition",
            "data": [
                {"id": 1, "condition": {"pwr_on": 0, "fan_speed": 3, "time": "ok"}},
                {"id": 2, "condition": {"pwr_on": 1, "fan_speed": 4, "time": "ok"}},
            ],
        }
    )
    if runtime.websocket_message_task:
        await runtime.websocket_message_task

    coordinator = runtime.coordinator
    assert coordinator.update_calls == 1
    assert coordinator.data["states"]["1"]["fan_speed"] == 4
    assert coordinator.data["states"]["2"]["fan_speed"] == 5
    assert any(
        call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
        for call in hass.bus.async_fire.call_args_list
    )


def test_resolve_update_interval_invalid_input_falls_back_to_default():
    assert atmeex_init._resolve_update_interval_seconds({CONF_UPDATE_INTERVAL: "bad"}) == DEFAULT_UPDATE_INTERVAL


@pytest.mark.asyncio
async def test_setup_entry_raises_auth_failed_on_invalid_credentials(monkeypatch):
    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock(side_effect=ApiError("bad creds", status=401))

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await atmeex_init.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_entry_raises_not_ready_on_non_auth_error(monkeypatch):
    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock(side_effect=ApiError("server down", status=500))

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    with pytest.raises(ConfigEntryNotReady):
        await atmeex_init.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_entry_uses_fallback_devices_and_hydration_fallback(monkeypatch):
    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            self.get_devices_calls = []

            self.dev = AtmeexDevice.from_raw(
                {
                    "id": 1,
                    "name": "FallbackDev",
                    "model": "m",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 2},
                    "settings": {},
                }
            )

            async def _get_devices(*, fallback=False):
                self.get_devices_calls.append(fallback)
                if not fallback:
                    raise ApiError("primary failed", status=500)
                return [self.dev]

            self.get_devices = AsyncMock(side_effect=_get_devices)
            self.get_device = AsyncMock(side_effect=ApiError("partial failure", status=500))

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data

    assert runtime.api.get_devices_calls == [False, True]
    runtime.api.get_device.assert_awaited_once_with(1)
    assert runtime.coordinator.data["device_map"]["1"].id == 1
    assert runtime.coordinator.data["states"]["1"]["fan_speed"] == 3


@pytest.mark.asyncio
async def test_setup_entry_reauth_on_fallback_auth_error(monkeypatch):
    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"

            async def _get_devices(*, fallback=False):
                if not fallback:
                    return []
                raise ApiError("token expired", status=401)

            self.get_devices = AsyncMock(side_effect=_get_devices)
            self.get_device = AsyncMock()

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await atmeex_init.async_setup_entry(hass, entry)


@pytest.mark.asyncio
async def test_setup_entry_registers_reload_listener(monkeypatch):
    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_method = update_method
            self.update_interval = update_interval
            self.data = None
            self.last_update_success = True

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    captured_listener = {}

    def add_update_listener(listener):
        captured_listener["cb"] = listener
        return lambda: None

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_reload=AsyncMock(),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=add_update_listener,
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    await captured_listener["cb"](hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("entry1")


@pytest.mark.asyncio
async def test_setup_entry_websocket_skipped_without_ws_connect(monkeypatch):
    class SessionNoWS:
        pass

    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: SessionNoWS())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.last_update_success = True
            self.update_method = update_method

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    assert entry.runtime_data.websocket_manager is None


@pytest.mark.asyncio
async def test_setup_entry_websocket_skipped_without_token(monkeypatch):
    class SessionWS:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called without token")

    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = None
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: SessionWS())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.last_update_success = True
            self.update_method = update_method

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    assert entry.runtime_data.websocket_manager is None


@pytest.mark.asyncio
async def test_setup_entry_websocket_auth_failure_starts_reauth(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    class FakeWebSocketManager:
        def __init__(self, session, token_getter, on_message, on_auth_failure=None):
            self.on_auth_failure = on_auth_failure

        async def connect(self):
            assert self.on_auth_failure is not None
            self.on_auth_failure()
            return False

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", FakeWebSocketManager)

    class FakeSession:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called")

    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw(
                {
                    "id": 1,
                    "name": "Dev1",
                    "model": "m",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 2},
                    "settings": {},
                }
            )
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: FakeSession())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.last_update_success = True
            self.update_method = update_method

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
        async_start_reauth=MagicMock(),
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_websocket_settings_message_updates_state(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    callbacks = []

    class FakeWebSocketManager:
        def __init__(self, session, token_getter, on_message, on_auth_failure=None):
            self.on_message = on_message
            self.on_auth_failure = on_auth_failure
            callbacks.append(on_message)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", FakeWebSocketManager)

    class FakeSession:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called")

    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw(
                {
                    "id": 1,
                    "name": "Dev1",
                    "model": "m",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 2, "damp_pos": 0, "hum_stg": 0},
                    "settings": {},
                }
            )
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: FakeSession())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.last_update_success = True
            self.update_method = update_method
            self.update_calls = 0

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.update_calls += 1
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 30, "enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    callback = callbacks[0]
    callback(
        {
            "type": "settings",
            "data": [
                {
                    "id": 1,
                    "settings": {
                        "u_fan_speed": 3,
                        "u_pwr_on": 1,
                        "u_temp_room": "215",
                        "u_hum_stg": 2,
                        "u_damp_pos": 1,
                    },
                }
            ],
        }
    )
    callback({"type": "unknown", "data": []})
    callback({"type": "settings", "data": "bad"})
    if runtime.websocket_message_task:
        await runtime.websocket_message_task

    state = runtime.coordinator.data["states"]["1"]
    assert runtime.coordinator.update_calls == 1
    assert state["u_fan_speed"] == 4
    assert state["fan_speed"] == 4
    assert state["pwr_on"] is True
    assert state["u_temp_room"] == 215
    assert state["hum_stg"] == 2
    assert state["damp_pos"] == 1


@pytest.mark.asyncio
async def test_websocket_logbook_device_events_are_throttled(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    callbacks = []

    class FakeWebSocketManager:
        def __init__(self, session, token_getter, on_message, on_auth_failure=None):
            self.on_message = on_message
            self.on_auth_failure = on_auth_failure
            callbacks.append(on_message)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", FakeWebSocketManager)

    monotonic_state = {"value": 100.0}
    monkeypatch.setattr(atmeex_init.time, "monotonic", lambda: monotonic_state["value"])

    class FakeSession:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called")

    class FakeApi:
        def __init__(self, _session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw(
                {
                    "id": 1,
                    "name": "Dev1",
                    "model": "m",
                    "online": True,
                    "condition": {"pwr_on": 1, "fan_speed": 2},
                    "settings": {},
                }
            )
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: FakeSession())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.last_update_success = True
            self.update_method = update_method

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options={"update_interval": 30, "enable_websocket": True},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    callback = callbacks[0]
    callback(
        {"type": "condition", "data": [{"id": 1, "condition": {"fan_speed": 3, "time": "ok"}}]}
    )
    if runtime.websocket_message_task:
        await runtime.websocket_message_task

    monotonic_state["value"] = 101.0
    callback(
        {"type": "condition", "data": [{"id": 1, "condition": {"fan_speed": 4, "time": "ok"}}]}
    )
    if runtime.websocket_message_task:
        await runtime.websocket_message_task

    monotonic_state["value"] = 106.0
    callback(
        {"type": "condition", "data": [{"id": 1, "condition": {"fan_speed": 5, "time": "ok"}}]}
    )
    if runtime.websocket_message_task:
        await runtime.websocket_message_task

    device_events = [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
    ]
    assert len(device_events) == 2
    assert device_events[0]["source"] == "websocket"
    assert device_events[1]["suppressed_updates"] == 1


# ---------------------------------------------------------------------------
# Helper for _apply_condition_update / _apply_settings_update tests
# ---------------------------------------------------------------------------

async def _build_ws_runtime(monkeypatch, *, initial_condition=None):
    """Build a WS-enabled runtime with one device and return (runtime, ws_callback, hass).

    Patches AtmeexApi, DataUpdateCoordinator, and WebSocketManager so that
    actual network calls are never made.
    """
    import custom_components.atmeex_cloud.websocket as websocket_mod

    _callbacks: list = []

    class _FakeWS:
        def __init__(self, session, token_getter, on_message, on_auth_failure=None):
            _callbacks.append(on_message)

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", _FakeWS)

    cond = {"pwr_on": 1, "fan_speed": 2, "damp_pos": 0, "hum_stg": 0}
    if initial_condition:
        cond.update(initial_condition)

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True, "condition": cond, "settings": {}}
    )

    class _FakeApi:
        def __init__(self, _s):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = "tok"
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", _FakeApi)

    class _FakeSession:
        async def ws_connect(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _h: _FakeSession())

    class _Coord:
        def __init__(self, hass, logger, name, update_method, update_interval):
            self.data = None
            self.update_method = update_method

        async def async_config_entry_first_refresh(self):
            self.data = await self.update_method()

        def async_set_updated_data(self, data):
            self.data = data

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", _Coord)

    hass = SimpleNamespace(
        data={},
        bus=SimpleNamespace(async_fire=MagicMock()),
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    )
    entry = SimpleNamespace(
        data={"email": "u@e.com", "password": "p"},
        options={"update_interval": 30, "enable_websocket": True},
        entry_id="e1",
        add_update_listener=lambda _l: (lambda: None),
        async_on_unload=lambda _c: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    return runtime, _callbacks[0], hass


async def _fire_and_drain(runtime, callback, message):
    """Fire a WS message and wait for the drain task to finish."""
    callback(message)
    if runtime.websocket_message_task:
        await runtime.websocket_message_task


# ---------------------------------------------------------------------------
# _apply_condition_update tests (via WS "condition" messages)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_condition_update_sets_online_always_true(monkeypatch):
    """A WS condition message always marks the device as online, even without a time field."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"online": False}
    )
    await _fire_and_drain(
        runtime, cb,
        {"type": "condition", "data": [{"id": 1, "condition": {"pwr_on": 0}}]},
    )
    assert runtime.coordinator.data["states"]["1"]["online"] is True


@pytest.mark.asyncio
async def test_apply_condition_update_no_water_field(monkeypatch):
    """no_water is correctly extracted from a condition message."""
    runtime, cb, _hass = await _build_ws_runtime(monkeypatch)
    await _fire_and_drain(
        runtime, cb,
        {"type": "condition", "data": [{"id": 1, "condition": {"no_water": 1}}]},
    )
    assert runtime.coordinator.data["states"]["1"]["no_water"] is True


@pytest.mark.asyncio
async def test_apply_condition_update_only_present_fields_changed(monkeypatch):
    """Fields absent from the condition payload are NOT overwritten."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"fan_speed": 5, "damp_pos": 3}
    )
    # Send a message that only changes pwr_on — fan_speed and damp_pos must stay
    await _fire_and_drain(
        runtime, cb,
        {"type": "condition", "data": [{"id": 1, "condition": {"pwr_on": 0}}]},
    )
    state = runtime.coordinator.data["states"]["1"]
    assert state["pwr_on"] is False
    assert state["fan_speed"] == 6   # API 5 → HA 6 (unchanged)
    assert state["damp_pos"] == 3    # unchanged


@pytest.mark.asyncio
async def test_apply_condition_update_time_field_propagated(monkeypatch):
    """The 'time' field from a condition message is stored as-is."""
    runtime, cb, _hass = await _build_ws_runtime(monkeypatch)
    ts = "2026-01-27 21:24:15"
    await _fire_and_drain(
        runtime, cb,
        {"type": "condition", "data": [{"id": 1, "condition": {"time": ts}}]},
    )
    assert runtime.coordinator.data["states"]["1"]["time"] == ts


# ---------------------------------------------------------------------------
# _apply_settings_update tests (via WS "settings" messages)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_apply_settings_update_fan_speed_synced_when_pwr_on(monkeypatch):
    """fan_speed in state is synced from u_fan_speed when the device is on."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"pwr_on": 1, "fan_speed": 2}
    )
    await _fire_and_drain(
        runtime, cb,
        {"type": "settings", "data": [{"id": 1, "settings": {"u_fan_speed": 4}}]},
    )
    state = runtime.coordinator.data["states"]["1"]
    assert state["u_fan_speed"] == 5   # API 4 → HA 5
    assert state["fan_speed"] == 5     # synced because pwr_on=True


@pytest.mark.asyncio
async def test_apply_settings_update_fan_speed_not_synced_when_pwr_off(monkeypatch):
    """fan_speed in state is NOT touched when the device is off."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"pwr_on": 0, "fan_speed": 3}
    )
    await _fire_and_drain(
        runtime, cb,
        {"type": "settings", "data": [{"id": 1, "settings": {"u_fan_speed": 4}}]},
    )
    state = runtime.coordinator.data["states"]["1"]
    assert state["u_fan_speed"] == 5   # u_fan_speed updated
    assert state["fan_speed"] == 4     # API 3 → HA 4 (NOT overwritten by u_fan_speed)


@pytest.mark.asyncio
async def test_apply_settings_update_power_and_speed_in_single_payload(monkeypatch):
    """When payload contains u_pwr_on and u_fan_speed, fan_speed follows new power state."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"pwr_on": 0, "fan_speed": 2}
    )
    await _fire_and_drain(
        runtime,
        cb,
        {
            "type": "settings",
            "data": [{"id": 1, "settings": {"u_pwr_on": 1, "u_fan_speed": 4}}],
        },
    )
    state = runtime.coordinator.data["states"]["1"]
    assert state["pwr_on"] is True
    assert state["u_fan_speed"] == 5
    assert state["fan_speed"] == 5


@pytest.mark.asyncio
async def test_apply_settings_update_all_fields(monkeypatch):
    """All settings fields (u_temp_room, u_hum_stg, u_damp_pos, u_pwr_on) are applied."""
    runtime, cb, _hass = await _build_ws_runtime(monkeypatch)
    await _fire_and_drain(
        runtime, cb,
        {
            "type": "settings",
            "data": [
                {
                    "id": 1,
                    "settings": {
                        "u_pwr_on": 0,
                        "u_temp_room": "215",
                        "u_hum_stg": 3,
                        "u_damp_pos": 2,
                    },
                }
            ],
        },
    )
    state = runtime.coordinator.data["states"]["1"]
    assert state["pwr_on"] is False
    assert state["u_temp_room"] == 215
    assert state["hum_stg"] == 3
    assert state["damp_pos"] == 2
    assert state["online"] is True


@pytest.mark.asyncio
async def test_apply_settings_update_sets_online_true(monkeypatch):
    """A settings update always marks the device as online."""
    runtime, cb, _hass = await _build_ws_runtime(
        monkeypatch, initial_condition={"online": False}
    )
    await _fire_and_drain(
        runtime, cb,
        {"type": "settings", "data": [{"id": 1, "settings": {"u_pwr_on": 1}}]},
    )
    assert runtime.coordinator.data["states"]["1"]["online"] is True
