import asyncio
from types import MethodType, SimpleNamespace
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
from custom_components.atmeex_cloud.coordinator import (
    AtmeexCoordinator as RealAtmeexCoordinator,
)
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


class _SetupCoordinatorFake:
    """Lightweight fake honoring the constructor-injected coordinator contract."""

    def __init__(
        self,
        hass,
        logger,
        *,
        api,
        state_store,
        config_entry_id,
        config_entry=None,
        name,
        update_interval,
        fire_logbook_event=None,
        **_kwargs,
    ):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.api = api
        self.state_store = state_store
        self.config_entry_id = config_entry_id
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = False
        self.last_api_error = None
        self.last_success_ts = None
        self.last_inventory_success_mono = None
        self.avg_latency_ms = None
        self.request_retries = 0
        self._fire_logbook_event = fire_logbook_event
        self._api_error_last_ts = float("-inf")
        self._api_error_suppressed = 0
        self._last_detail_error = None
        self._last_detail_failure_count = 0
        self.async_update_listeners = MagicMock()
        for static_name in (
            "_safe_detail_error",
            "_needs_detail",
            "_merge_detail_source",
            "_exception_leaves",
        ):
            setattr(
                self,
                static_name,
                getattr(RealAtmeexCoordinator, static_name),
            )
        for method_name in (
            "_fire_api_error_event",
            "_previous_device",
            "_hydrate_one",
            "_hydrate_devices",
            "_remove_confirmed_stale_devices",
            "_async_update_data",
        ):
            setattr(
                self,
                method_name,
                MethodType(
                    getattr(RealAtmeexCoordinator, method_name),
                    self,
                ),
            )
        self._safe_error_event = RealAtmeexCoordinator._safe_error_event

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.data = data

    async def async_request_refresh(self):
        inventory_success_before = self.last_inventory_success_mono
        self.last_update_success = False
        self.data = await self._async_update_data()
        self.last_update_success = True
        if (
            self.last_inventory_success_mono is None
            or (
                inventory_success_before is not None
                and self.last_inventory_success_mono
                <= inventory_success_before
            )
        ):
            self.last_inventory_success_mono = (
                0.0
                if inventory_success_before is None
                else inventory_success_before + 1.0
            )


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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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
    assert runtime.state_store is runtime.coordinator.state_store
    assert runtime.state_store.data is runtime.coordinator.data
    assert runtime.coordinator.data["devices"][0]["id"] == 1
    assert runtime.state_store.data["states"]["1"]["pwr_on"] is True
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

    class DummyCoordinator(_SetupCoordinatorFake):
        def __init__(self, hass, logger, *, update_interval, **kwargs):
            captured["update_interval"] = update_interval
            super().__init__(
                hass,
                logger,
                update_interval=update_interval,
                **kwargs,
            )

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

async def test_setup_entry_uses_complete_authoritative_inventory_without_detail(
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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
    runtime.api.get_device.assert_not_awaited()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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
    return _SetupCoordinatorFake


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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: SessionWS())

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _SetupCoordinatorFake,
    )

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


def _setup_lifecycle_fakes(monkeypatch, order):
    import custom_components.atmeex_cloud.websocket as websocket_mod
    from custom_components.atmeex_cloud.state_store import AtmeexStateStore

    device = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Device",
            "model": "m",
            "online": True,
            "condition": {"pwr_on": 1},
            "settings": {},
        }
    )

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = None
            self.token = "token"
            self.get_devices = AsyncMock(return_value=[device])
            self.get_device = AsyncMock(return_value=device)
            self.async_refresh_access_token = AsyncMock()

    class FakeCoordinator:
        def __init__(
            self,
            hass,
            logger,
            *,
            api,
            state_store,
            config_entry_id,
            config_entry=None,
            name,
            update_interval,
            fire_logbook_event=None,
            **kwargs,
        ):
            state_store.apply_inventory(
                [device],
                state_store.capture_all(),
            )
            self.api = api
            self.state_store = state_store
            self.config_entry_id = config_entry_id
            self.data = state_store.data
            self.async_request_refresh = AsyncMock()

        async def async_config_entry_first_refresh(self):
            return

        def async_set_updated_data(self, data):
            self.data = data

    manager = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        on_auth_failure=None,
        on_token_refresh=None,
    )
    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", FakeCoordinator)
    def create_manager(**kwargs):
        manager.on_auth_failure = kwargs.get("on_auth_failure")
        manager.on_token_refresh = kwargs.get("on_token_refresh")
        return manager

    monkeypatch.setattr(websocket_mod, "WebSocketManager", create_manager)
    monkeypatch.setattr(
        atmeex_init,
        "async_get_clientsession",
        lambda hass: SimpleNamespace(ws_connect=AsyncMock()),
    )

    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
        config_entries=config_entries,
        async_create_task=lambda coro, **kwargs: asyncio.create_task(coro),
    )
    unload_callbacks = []
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "secret"},
        options={"enable_websocket": True},
        entry_id="entry1",
        runtime_data=None,
        add_update_listener=lambda callback: lambda: None,
        async_on_unload=unload_callbacks.append,
        async_start_reauth=MagicMock(),
        unload_callbacks=unload_callbacks,
    )
    return entry, hass, manager


@pytest.mark.asyncio
async def test_websocket_starts_only_after_platforms(monkeypatch):
    runtime_order: list[str] = []
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, runtime_order)

    async def forward_platforms(entry, platforms):
        runtime_order.append("platforms")
        await asyncio.sleep(0)
        assert manager.connect.await_count == 0

    hass.config_entries.async_forward_entry_setups.side_effect = (
        forward_platforms
    )
    manager.connect.side_effect = (
        lambda: runtime_order.append("websocket") or True
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    await entry.runtime_data.websocket_start_task

    assert runtime_order == ["platforms", "websocket"]
    assert len(entry.unload_callbacks) == 1


@pytest.mark.asyncio
async def test_platform_failure_rolls_back_runtime(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
        "platform failed"
    )

    with pytest.raises(RuntimeError, match="platform failed"):
        await atmeex_init.async_setup_entry(hass, entry)

    manager.disconnect.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry,
        PLATFORMS,
    )
    assert entry.runtime_data is None
    assert entry.unload_callbacks == []


@pytest.mark.asyncio
async def test_start_task_creation_failure_closes_coroutine(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])
    monkeypatch.setattr(
        atmeex_init,
        "async_create_background_task",
        MagicMock(side_effect=RuntimeError("scheduler stopped")),
    )

    with pytest.raises(RuntimeError, match="scheduler stopped"):
        await atmeex_init.async_setup_entry(hass, entry)

    manager.disconnect.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry,
        PLATFORMS,
    )
    assert entry.runtime_data is None
    assert entry.unload_callbacks == []


@pytest.mark.asyncio
async def test_websocket_reauth_is_one_shot(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])
    monotonic = MagicMock(side_effect=[0.0, 301.0])
    monkeypatch.setattr(
        atmeex_init,
        "time",
        SimpleNamespace(monotonic=monotonic),
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    await entry.runtime_data.websocket_start_task
    assert manager.on_auth_failure is not None

    manager.on_auth_failure()
    manager.on_auth_failure()

    entry.async_start_reauth.assert_called_once_with(hass)
    await atmeex_init._async_cleanup_runtime(entry.runtime_data)


@pytest.mark.asyncio
async def test_websocket_reauth_is_suppressed_while_stopping(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    await entry.runtime_data.websocket_start_task
    entry.runtime_data.stopping = True
    assert manager.on_auth_failure is not None

    manager.on_auth_failure()

    entry.async_start_reauth.assert_not_called()
    await atmeex_init._async_cleanup_runtime(entry.runtime_data)


@pytest.mark.asyncio
async def test_cleanup_bounds_websocket_disconnect(monkeypatch):
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        refresh_device=None,
    )
    never = asyncio.Event()
    runtime.websocket_manager = SimpleNamespace(
        disconnect=AsyncMock(side_effect=never.wait),
    )
    monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.01)

    await asyncio.wait_for(
        atmeex_init._async_cleanup_runtime(runtime),
        timeout=0.1,
    )

    assert runtime.stopping is True


@pytest.mark.asyncio
@pytest.mark.skipif(
    not hasattr(asyncio, "eager_task_factory"),
    reason="eager task startup only happens on Python 3.12+",
)
async def test_listener_failure_cleans_eager_completed_startup(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])
    loop = asyncio.get_running_loop()
    captured_runtime = None

    def eager_create_task(coro, **kwargs):
        return asyncio.eager_task_factory(
            loop,
            coro,
            name=kwargs.get("name"),
        )

    def fail_listener_registration(_listener):
        nonlocal captured_runtime
        captured_runtime = entry.runtime_data
        startup = captured_runtime.websocket_start_task
        assert startup is not None
        assert startup.done()
        captured_runtime.refresh_tasks["1"] = startup
        raise RuntimeError("listener registration failed")

    hass.async_create_task = eager_create_task
    manager.connect.return_value = True
    entry.add_update_listener = fail_listener_registration

    with pytest.raises(RuntimeError, match="listener registration failed"):
        await atmeex_init.async_setup_entry(hass, entry)

    assert captured_runtime is not None
    assert captured_runtime.tasks == set()
    assert captured_runtime.refresh_tasks == {}
    assert captured_runtime.websocket_start_task is None
    assert captured_runtime.websocket_message_task is None
    assert captured_runtime.websocket_resync_task is None
    assert captured_runtime.inventory_watchdog_task is None
    manager.disconnect.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry,
        PLATFORMS,
    )
    assert entry.runtime_data is None
    assert entry.unload_callbacks == []
