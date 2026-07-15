import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
)
from custom_components.atmeex_cloud.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from custom_components.atmeex_cloud.helpers import to_bool, _normalize_device_state
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


async def test_async_setup_entry_happy_path(monkeypatch):
    # подменяем AtmeexApi
    created_apis = []

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
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
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)
            created_apis.append(self)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    captured = {}

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            captured["update_interval"] = update_interval
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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

async def test_setup_entry_raises_auth_failed_on_invalid_credentials(monkeypatch):
    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.login = AsyncMock(
                side_effect=AtmeexAuthenticationError(
                    "test_setup", "bad creds", status=401
                )
            )

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

async def test_setup_entry_raises_not_ready_on_non_auth_error(monkeypatch):
    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.login = AsyncMock(
                side_effect=AtmeexConnectionError(
                    "test_setup", "server down", status=500
                )
            )

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

async def test_setup_entry_uses_authoritative_inventory_and_hydration_fallback(
    monkeypatch,
):
    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self._token = "token"

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

            self.get_devices = AsyncMock(return_value=[self.dev])
            self.get_device = AsyncMock(
                side_effect=AtmeexProtocolError(
                    "get_device", "partial device payload"
                )
            )

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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

    runtime.api.get_devices.assert_awaited_once_with()
    runtime.api.get_device.assert_awaited_once_with(1)
    assert runtime.coordinator.data["device_map"]["1"].id == 1
    assert runtime.coordinator.data["states"]["1"]["fan_speed"] == 3

async def test_setup_entry_reauth_on_authoritative_inventory_auth_error(monkeypatch):
    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self._token = "token"

            self.get_devices = AsyncMock(
                side_effect=AtmeexAuthenticationError(
                    "get_devices", "token expired", status=401
                )
            )
            self.get_device = AsyncMock()

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

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

async def test_setup_entry_reload_listener_reloads_for_options_change(monkeypatch):
    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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
    entry.options = {"enable_websocket": False, "update_interval": 90}
    await captured_listener["cb"](hass, entry)
    hass.config_entries.async_reload.assert_awaited_once_with("entry1")


def _setup_test_coordinator_class():
    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}
            self._refresh_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types

            from custom_components.atmeex_cloud.coordinator import (
                AtmeexCoordinator as RealCoordinator,
            )

            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for method_name in (
                "_fetch_devices_safely",
                "_fire_api_error_event",
                "_async_update_data",
            ):
                method = getattr(RealCoordinator, method_name)
                setattr(self, method_name, types.MethodType(method, self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

    return DummyCoordinator


async def test_rotated_refresh_token_persists_without_entry_reload(monkeypatch):
    callbacks: list[object] = []

    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            callbacks.append(on_refresh_token_changed)
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = "stored"
            self._refresh_token = "stored"
            self.token = "access"
            device = AtmeexDevice.from_raw({"id": 1, "condition": {}, "settings": {}})
            self.get_devices = AsyncMock(return_value=[device])
            self.get_device = AsyncMock(return_value=device)

        def restore_refresh_token(self, refresh_token):
            self._refresh_token = refresh_token

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", _setup_test_coordinator_class())
    update_entry = MagicMock()
    reload_entry = AsyncMock()
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_update_entry=update_entry,
            async_reload=reload_entry,
        ),
    )
    captured_listener = {}

    def add_update_listener(callback):
        captured_listener["callback"] = callback
        return lambda: None

    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pw", "refresh_token": "stored"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=add_update_listener,
        async_on_unload=lambda _callback: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    callbacks[0]("rotated")
    entry.data = {**entry.data, "refresh_token": "rotated"}
    await captured_listener["callback"](hass, entry)

    update_entry.assert_called_once_with(
        entry,
        data={"email": "user@example.com", "password": "pw", "refresh_token": "rotated"},
    )
    reload_entry.assert_not_awaited()


async def test_refresh_token_persistence_failure_is_logged_not_raised(monkeypatch):
    """If async_update_entry raises when persisting a new refresh token, the error
    must be caught and logged — not propagated as an unhandled exception that
    would abort async_setup_entry.
    """
    import logging

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.login = AsyncMock(side_effect=self._login)
            # Return a NEW refresh token different from what's stored
            self.refresh_token = "new-refresh-token"
            self._token = "access-token"
            dev = AtmeexDevice.from_raw(
                {"id": 1, "name": "D", "model": "m", "online": True, "condition": {}, "settings": {}}
            )
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

        async def _login(self, _email, _password):
            self.on_refresh_token_changed("new-refresh-token")

        def restore_refresh_token(self, refresh_token):
            self._refresh_token = refresh_token

        @property
        def token(self):
            return self._token

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)

    persist_calls = []

    def _raising_update_entry(entry, data):
        persist_calls.append(data)
        raise RuntimeError("Disk full — cannot persist")

    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_update_entry=_raising_update_entry,
        ),
    )
    entry = SimpleNamespace(
        # stored refresh_token differs from what the API will return
        data={"email": "u@example.com", "password": "pw", "refresh_token": "old-token"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    # async_setup_entry must succeed even though persisting the token raised
    result = await atmeex_init.async_setup_entry(hass, entry)
    assert result is True, "setup must not fail when token persistence raises"
    assert len(persist_calls) == 1, "async_update_entry should have been attempted"

async def test_setup_entry_websocket_skipped_without_ws_connect(monkeypatch):
    class SessionNoWS:
        pass

    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self._token = "token"
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: SessionNoWS())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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

async def test_setup_entry_websocket_skipped_without_token(monkeypatch):
    class SessionWS:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called without token")

    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self._token = None
            dev = AtmeexDevice.from_raw({"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: SessionWS())

    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

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
