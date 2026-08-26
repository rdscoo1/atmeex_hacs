import asyncio
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import (
    ApiError,
    AtmeexConnectionError,
    AtmeexDevice,
)
from custom_components.atmeex_cloud.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from custom_components.atmeex_cloud.coordinator import (
    AtmeexCoordinator as RealAtmeexCoordinator,
)
from custom_components.atmeex_cloud.helpers import to_bool, _normalize_device_state
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


class _IntegrationCoordinatorFake:
    """Small coordinator double that preserves the production composition contract."""

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
        self.update_calls = 0
        self.async_update_listeners = MagicMock()
        self._fire_logbook_event = fire_logbook_event
        self._api_error_last_ts = float("-inf")
        self._api_error_suppressed = 0
        self._last_detail_error = None
        self._last_detail_failure_count = 0
        self._fire_api_error_event = MethodType(
            RealAtmeexCoordinator._fire_api_error_event,
            self,
        )
        self._safe_error_event = RealAtmeexCoordinator._safe_error_event
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
        self._previous_device = MethodType(
            RealAtmeexCoordinator._previous_device,
            self,
        )
        self._hydrate_one = MethodType(
            RealAtmeexCoordinator._hydrate_one,
            self,
        )
        self._hydrate_devices = MethodType(
            RealAtmeexCoordinator._hydrate_devices,
            self,
        )
        self._remove_confirmed_stale_devices = MethodType(
            RealAtmeexCoordinator._remove_confirmed_stale_devices,
            self,
        )
        self._async_update_data = MethodType(
            RealAtmeexCoordinator._async_update_data,
            self,
        )

    async def async_config_entry_first_refresh(self):
        self.data = await self._async_update_data()
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.update_calls += 1
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


async def test_websocket_batch_message_updates_coordinator_once(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    created_callbacks = []
    created_ws_managers = []

    class FakeWebSocketManager:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
            self.session = session
            self.token_getter = token_getter
            self.on_message = on_message
            self.on_auth_failure = on_auth_failure
            self.on_token_refresh = on_token_refresh
            self.task_factory = task_factory
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
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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
        data={"email": "test@example.com", "password": "testpassword"},
        options={"update_interval": 30, "enable_websocket": True},
        entry_id="test_entry",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data
    if runtime.websocket_start_task:
        await runtime.websocket_start_task

    assert len(created_ws_managers) == 1
    assert len(created_callbacks) == 1
    assert created_ws_managers[0].task_factory is not None
    assert (
        created_ws_managers[0].on_token_refresh
        is runtime.api.async_refresh_access_token
    )

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
    assert coordinator.data["device_map"]["1"].condition["fan_speed"] == 3
    assert coordinator.data["device_map"]["2"].condition["fan_speed"] == 4
    device_events = [
        call
        for call in hass.bus.async_fire.call_args_list
        if call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
    ]
    assert len(device_events) == 1
    assert any(
        call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
        for call in hass.bus.async_fire.call_args_list
    )

async def test_setup_entry_websocket_auth_failure_starts_reauth(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    class FakeWebSocketManager:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
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
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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

async def test_ws_reauth_is_one_shot_for_entry_lifetime(monkeypatch):
    """A successful socket connection does not allow duplicate reauth flows."""
    import custom_components.atmeex_cloud.websocket as websocket_mod

    on_auth_failure_cb = None
    class FakeWebSocketManager:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
            nonlocal on_auth_failure_cb
            self.on_auth_failure = on_auth_failure
            on_auth_failure_cb = on_auth_failure

        async def connect(self):
            return True

        async def disconnect(self):
            return None

    monkeypatch.setattr(websocket_mod, "WebSocketManager", FakeWebSocketManager)

    class FakeSession:
        async def ws_connect(self, *args, **kwargs):
            raise AssertionError("ws_connect should not be called")

    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

    hass = SimpleNamespace(
        data={},
        async_create_task=lambda coro, **kwargs: asyncio.create_task(coro),
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

    assert on_auth_failure_cb is not None
    on_auth_failure_cb()
    on_auth_failure_cb()

    entry.async_start_reauth.assert_called_once_with(hass)

async def test_websocket_settings_message_updates_state(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    callbacks = []

    class FakeWebSocketManager:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
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
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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
    settings = runtime.coordinator.data["device_map"]["1"].settings
    assert settings["u_fan_speed"] == 3
    assert settings["u_pwr_on"] is True

async def test_websocket_logbook_device_events_are_throttled(monkeypatch):
    import custom_components.atmeex_cloud.websocket as websocket_mod

    callbacks = []

    class FakeWebSocketManager:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
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
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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

async def _build_ws_runtime(monkeypatch, *, initial_condition=None):
    """Build a WS-enabled runtime with one device and return (runtime, ws_callback, hass).

    Patches AtmeexApi, DataUpdateCoordinator, and WebSocketManager so that
    actual network calls are never made.
    """
    import custom_components.atmeex_cloud.websocket as websocket_mod

    _callbacks: list = []

    class _FakeWS:
        def __init__(
            self,
            session,
            token_getter,
            on_message,
            on_auth_failure=None,
            on_token_refresh=None,
            task_factory=None,
        ):
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
        def __init__(self, _s, *, on_refresh_token_changed=None):
            self.session = _s
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            self.async_refresh_access_token = AsyncMock()
            self._token = "tok"
            self._devices = [dev]
            self.get_devices = AsyncMock(return_value=self._devices)
            self.get_device = AsyncMock(return_value=dev)

        @property
        def token(self):
            return self._token or ""

    monkeypatch.setattr(atmeex_init, "AtmeexApi", _FakeApi)

    class _FakeSession:
        async def ws_connect(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must not be called")

    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _h: _FakeSession())

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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


@pytest.mark.parametrize("eager_tasks", [False, True])
async def test_burst_coalesces_later_fields_into_one_publication(
    monkeypatch,
    eager_tasks,
):
    runtime, callback, hass = await _build_ws_runtime(monkeypatch)
    if eager_tasks:
        loop = asyncio.get_running_loop()

        def eager_create_task(coro, *, name=None):
            return asyncio.eager_task_factory(loop, coro, name=name)

        hass.async_create_task = eager_create_task
    coordinator = runtime.coordinator
    coordinator.update_calls = 0
    apply_delta = MagicMock(
        wraps=runtime.state_store.apply_websocket_delta
    )
    runtime.state_store.apply_websocket_delta = apply_delta

    accepted = [
        callback(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"fan_speed": speed}}],
            }
        )
        for speed in range(1, 7)
    ]
    drain_task = runtime.websocket_message_task
    assert drain_task is not None
    await drain_task

    assert accepted == [True] * 6
    assert coordinator.update_calls == 1
    assert apply_delta.call_count == 1
    assert coordinator.data["states"]["1"]["fan_speed"] == 7


async def test_mixed_batch_event_reports_all_changed_device_sources(monkeypatch):
    runtime, callback, hass = await _build_ws_runtime(monkeypatch)
    coordinator = runtime.coordinator
    coordinator.update_calls = 0
    hass.bus.async_fire.reset_mock()

    accepted = [
        callback(
            {
                "type": "condition",
                "data": [{"id": "001", "condition": {"fan_speed": 3}}],
            }
        ),
        callback(
            {
                "type": "settings",
                "data": [{"id": 1, "settings": {"u_temp_room": "215"}}],
            }
        ),
    ]
    drain_task = runtime.websocket_message_task
    assert drain_task is not None
    await drain_task

    device_events = [
        call.args[1]
        for call in hass.bus.async_fire.call_args_list
        if call.args and call.args[0] == atmeex_init.EVENT_DEVICE_UPDATED
    ]
    assert accepted == [True, True]
    assert coordinator.update_calls == 1
    assert len(device_events) == 1
    assert device_events[0]["device_ids"] == ["1"]
    assert device_events[0]["message_type"] == "mixed"


async def test_queue_overflow_rejects_after_limit_and_schedules_one_resync(
    monkeypatch,
):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.coordinator.async_request_refresh = AsyncMock()
    apply_delta = MagicMock(
        wraps=runtime.state_store.apply_websocket_delta
    )
    runtime.state_store.apply_websocket_delta = apply_delta

    accepted = [
        callback(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"temp_in": value}}],
            }
        )
        for value in range(502)
    ]
    resync_task = runtime.websocket_resync_task
    assert resync_task is not None
    assert resync_task in runtime.tasks
    tasks = tuple(runtime.tasks)
    await asyncio.gather(*tasks)

    assert accepted[:500] == [True] * 500
    assert accepted[500:] == [False, False]
    assert runtime.websocket_overflow_count == 2
    runtime.coordinator.async_request_refresh.assert_awaited_once_with()
    assert apply_delta.call_count == 1
    assert runtime.websocket_resync_task is None
    assert runtime.coordinator.data["states"]["1"]["temp_in"] == 499


async def test_overflow_during_inflight_resync_runs_one_followup(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    release_second = asyncio.Event()
    refresh_calls = 0
    concurrent_refreshes = 0
    max_concurrent_refreshes = 0

    async def controlled_refresh() -> None:
        nonlocal refresh_calls
        nonlocal concurrent_refreshes, max_concurrent_refreshes
        refresh_calls += 1
        concurrent_refreshes += 1
        max_concurrent_refreshes = max(
            max_concurrent_refreshes,
            concurrent_refreshes,
        )
        try:
            if refresh_calls == 1:
                first_started.set()
                await release_first.wait()
            elif refresh_calls == 2:
                second_started.set()
                await release_second.wait()
            else:  # pragma: no cover - a third refresh is a regression
                pytest.fail("overflow resync ran more than one follow-up")
        finally:
            concurrent_refreshes -= 1

    runtime.coordinator.async_request_refresh = AsyncMock(
        side_effect=controlled_refresh
    )

    first_results = [
        callback(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"temp_in": value}}],
            }
        )
        for value in range(501)
    ]
    first_drain = runtime.websocket_message_task
    owner = runtime.websocket_resync_task
    assert first_drain is not None
    assert owner is not None

    try:
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await first_drain
        assert first_results[:500] == [True] * 500
        assert first_results[500] is False

        second_results = [
            callback(
                {
                    "type": "condition",
                    "data": [
                        {"id": 1, "condition": {"temp_in": 1000 + value}}
                    ],
                }
            )
            for value in range(501)
        ]
        second_drain = runtime.websocket_message_task
        assert second_drain is not None
        assert runtime.websocket_resync_task is owner
        assert runtime.websocket_resync_task in runtime.tasks
        assert sum(
            task.get_name() == "atmeex websocket overflow resync"
            for task in runtime.tasks
        ) == 1
        await second_drain
        assert second_results[:500] == [True] * 500
        assert second_results[500] is False

        release_first.set()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        assert refresh_calls == 2
        assert runtime.websocket_resync_task is owner
        assert max_concurrent_refreshes == 1

        release_second.set()
        await owner
        assert runtime.websocket_resync_task is None
        assert refresh_calls == 2
        assert max_concurrent_refreshes == 1
    finally:
        release_first.set()
        release_second.set()
        await asyncio.gather(*tuple(runtime.tasks), return_exceptions=True)


async def test_callback_refuses_work_after_stopping(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.stopping = True

    accepted = callback(
        {
            "type": "condition",
            "data": [{"id": 1, "condition": {"pwr_on": 0}}],
        }
    )
    task = runtime.websocket_message_task
    if task is not None:
        await task

    assert accepted is False
    assert runtime.websocket_message_task is None
    assert runtime.websocket_resync_task is None
    assert runtime.coordinator.data["states"]["1"]["pwr_on"] is True


async def test_overflow_resync_refuses_refresh_after_stopping(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.coordinator.async_request_refresh = AsyncMock()

    for value in range(501):
        callback(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"temp_in": value}}],
            }
        )
    tasks = tuple(runtime.tasks)
    assert runtime.websocket_resync_task is not None
    runtime.stopping = True
    await asyncio.gather(*tasks)

    runtime.coordinator.async_request_refresh.assert_not_awaited()
    assert runtime.websocket_resync_task is None
    assert runtime.websocket_message_task is None


async def test_overflow_resync_creation_failure_rolls_back_owner(
    monkeypatch,
):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    for speed in range(500):
        callback(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"fan_speed": speed}}],
            }
        )
    drain_task = runtime.websocket_message_task
    assert drain_task is not None

    reject_task = MagicMock(side_effect=RuntimeError("scheduler stopped"))
    monkeypatch.setattr(
        atmeex_init,
        "async_create_background_task",
        reject_task,
    )
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="scheduler stopped"):
                callback(
                    {
                        "type": "condition",
                        "data": [{"id": 1, "condition": {"fan_speed": 500}}],
                    }
                )
            assert runtime.websocket_resync_task is None
    finally:
        await drain_task

    assert reject_task.call_count == 2
    assert runtime.websocket_overflow_count == 2
    assert all(
        call.args[1].cr_frame is None
        for call in reject_task.call_args_list
    )


async def test_cancelled_refresh_waiter_does_not_cancel_owner(monkeypatch):
    runtime, _callback, _hass = await _build_ws_runtime(monkeypatch)
    api = runtime.api
    api.started = asyncio.Event()
    api.release = asyncio.Event()
    api.get_device_calls = 0

    async def blocked_get_device(device_id):
        api.get_device_calls += 1
        api.started.set()
        await api.release.wait()
        return api._devices[0]

    api.get_device.reset_mock()
    api.get_device.side_effect = blocked_get_device
    owner_waiter = asyncio.create_task(runtime.refresh_device(1))
    await api.started.wait()

    try:
        cancelled_waiter = asyncio.create_task(runtime.refresh_device("001"))
        await asyncio.sleep(0)
        cancelled_waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert api.get_device_calls == 1
        owner = runtime.refresh_tasks["1"]
        assert owner.cancelled() is False

        monkeypatch.setattr(atmeex_init, "_REFRESH_TASK_TIMEOUT_SEC", 0.01)
        with pytest.raises(asyncio.TimeoutError):
            await runtime.refresh_device(1)
        assert api.get_device_calls == 1
        assert runtime.refresh_tasks["1"] is owner
        assert owner.cancelled() is False

        api.release.set()
        await owner_waiter
        await asyncio.sleep(0)
        assert runtime.refresh_tasks == {}
    finally:
        api.release.set()
        await asyncio.gather(owner_waiter, return_exceptions=True)


async def test_failed_confirmation_serializes_recovery_and_preserves_executed_aba(
    monkeypatch,
):
    runtime, _callback, hass = await _build_ws_runtime(monkeypatch)
    api = runtime.api
    coordinator = runtime.coordinator
    executor = runtime.command_executor
    assert executor is not None

    scheduled: list[asyncio.Task] = []

    def create_task(coro, **kwargs):
        task = asyncio.create_task(coro, name=kwargs.get("name"))
        scheduled.append(task)
        return task

    hass.async_create_task = create_task
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    original_full_refresh = coordinator.async_request_refresh

    async def blocked_full_refresh():
        recovery_started.set()
        await release_recovery.wait()
        await original_full_refresh()

    coordinator.async_request_refresh = AsyncMock(
        side_effect=blocked_full_refresh
    )
    allow_recovery = MagicMock(
        wraps=executor.allow_recovery_confirmation
    )
    executor.allow_recovery_confirmation = allow_recovery
    notified_pending_generations: list[int | None] = []

    def notify_listeners() -> None:
        confirmed = coordinator.data["states"]["1"]["pwr_on"]
        executor.value_with_pending(1, "pwr_on", confirmed)
        pending = executor.get_pending(1, "pwr_on")
        notified_pending_generations.append(
            None if pending is None else pending.generation
        )

    coordinator.async_update_listeners.side_effect = notify_listeners
    api.get_device.side_effect = [
        AtmeexConnectionError(
            "get_device",
            "confirmation unavailable",
        ),
        api._devices[0],
        api._devices[0],
    ]

    second = None
    release_second_operation = asyncio.Event()
    try:
        first_confirmation = await executor.async_execute(
            1,
            AsyncMock(),
            pending={"pwr_on": False},
            translation_key="command_failed",
        )
        assert first_confirmation is False
        await asyncio.wait_for(recovery_started.wait(), timeout=1.0)
        first_pending = executor.get_pending(1, "pwr_on")
        assert first_pending is not None
        assert first_pending.value is False

        await asyncio.sleep(0)
        recovery_tasks = tuple(
            task for task in runtime.tasks if not task.done()
        )
        assert len(recovery_tasks) == 1
        allow_recovery.assert_not_called()
        coordinator.async_update_listeners.assert_not_called()

        second_operation_started = asyncio.Event()

        async def second_operation() -> None:
            second_operation_started.set()
            await release_second_operation.wait()

        second = asyncio.create_task(
            executor.async_execute(
                "001",
                second_operation,
                pending={"pwr_on": True},
                translation_key="command_failed",
            )
        )

        for _ in range(20):
            await asyncio.sleep(0)
            second_pending = executor.get_pending(1, "pwr_on")
            if (
                second_pending is not None
                and second_pending.generation != first_pending.generation
            ):
                break
        else:
            pytest.fail("queued ABA generation was not installed")

        assert second_operation_started.is_set() is False
        assert second_pending.value is True
        second_generation = second_pending.generation

        release_recovery.set()
        await asyncio.gather(*recovery_tasks)
        await asyncio.sleep(0)

        allow_recovery.assert_called_once_with(1)
        coordinator.async_update_listeners.assert_called_once_with()
        assert notified_pending_generations == [second_generation]
        pending_after_recovery = executor.get_pending(1, "pwr_on")
        assert pending_after_recovery is not None
        assert pending_after_recovery.generation == second_generation

        await asyncio.wait_for(second_operation_started.wait(), timeout=1.0)
        pending_during_second_operation = executor.get_pending(1, "pwr_on")
        assert pending_during_second_operation is not None
        assert pending_during_second_operation.generation == second_generation

        release_second_operation.set()
        assert await second is True
        assert second_operation_started.is_set() is True
        confirmed = coordinator.data["states"]["1"]["pwr_on"]
        assert executor.value_with_pending(1, "pwr_on", confirmed) is True
        assert executor.get_pending(1, "pwr_on") is None
    finally:
        release_recovery.set()
        release_second_operation.set()
        if second is not None:
            await asyncio.gather(second, return_exceptions=True)
        await asyncio.gather(*scheduled, return_exceptions=True)

async def _fire_and_drain(runtime, callback, message):
    """Fire a WS message and wait for the drain task to finish."""
    callback(message)
    if runtime.websocket_message_task:
        await runtime.websocket_message_task


async def test_websocket_invalid_boolean_keeps_valid_sibling_and_device_model(
    monkeypatch,
):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)

    await _fire_and_drain(
        runtime,
        callback,
        {
            "type": "condition",
            "data": [
                {
                    "id": "01",
                    "condition": {
                        "pwr_on": "not-a-boolean",
                        "fan_speed": 4,
                    },
                }
            ],
        },
    )

    state = runtime.coordinator.data["states"]["1"]
    device = runtime.coordinator.data["device_map"]["1"]
    assert runtime.coordinator.update_calls == 1
    assert state["pwr_on"] is True
    assert state["fan_speed"] == 5
    assert device.condition["pwr_on"] == 1
    assert device.condition["fan_speed"] == 4


async def test_websocket_equivalent_delta_does_not_publish_but_touches_revision(
    monkeypatch,
):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    baseline = runtime.state_store.capture_device("1")
    publish_count = runtime.coordinator.update_calls

    await _fire_and_drain(
        runtime,
        callback,
        {
            "type": "condition",
            "data": [{"id": 1, "condition": {"pwr_on": 1, "fan_speed": 2}}],
        },
    )

    assert runtime.coordinator.update_calls == publish_count
    stale = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Dev1",
            "model": "m",
            "online": True,
            "condition": {"pwr_on": 0, "fan_speed": 2},
            "settings": {},
        }
    )
    update = runtime.state_store.apply_refresh(stale, baseline)
    assert update.changed is False
    assert runtime.state_store.data["states"]["1"]["pwr_on"] is True

@pytest.mark.parametrize(
    ("initial_condition", "condition", "expected"),
    [
        pytest.param(
            {"online": False},
            {"pwr_on": 0},
            {"online": True},
            id="always-marks-online-even-without-time",
        ),
        pytest.param(None, {"no_water": 1}, {"no_water": True}, id="no-water-extracted"),
        pytest.param(
            {"fan_speed": 5, "damp_pos": 3},
            {"pwr_on": 0},
            # fan_speed API 5 → HA 6; absent fields must stay untouched
            {"pwr_on": False, "fan_speed": 6, "damp_pos": 3},
            id="absent-fields-not-overwritten",
        ),
        pytest.param(
            None,
            {"time": "2026-01-27 21:24:15"},
            {"time": "2026-01-27 21:24:15"},
            id="time-propagated-as-is",
        ),
    ],
)
async def test_ws_condition_update_normalizes_into_state(
    monkeypatch, initial_condition, condition, expected
):
    kwargs = {} if initial_condition is None else {"initial_condition": initial_condition}
    runtime, cb, _hass = await _build_ws_runtime(monkeypatch, **kwargs)
    await _fire_and_drain(
        runtime, cb,
        {"type": "condition", "data": [{"id": 1, "condition": condition}]},
    )
    state = runtime.coordinator.data["states"]["1"]
    for key, value in expected.items():
        assert state[key] == value
        assert isinstance(state[key], bool) is isinstance(value, bool)


@pytest.mark.parametrize(
    ("initial_condition", "settings", "expected"),
    [
        pytest.param(
            {"pwr_on": 1, "fan_speed": 2},
            {"u_fan_speed": 4},
            # API 4 → HA 5; fan_speed synced because device is on
            {"u_fan_speed": 5, "fan_speed": 5},
            id="fan-speed-synced-when-on",
        ),
        pytest.param(
            {"pwr_on": 0, "fan_speed": 3},
            {"u_fan_speed": 4},
            # u_fan_speed updated, fan_speed (API 3 → HA 4) NOT overwritten while off
            {"u_fan_speed": 5, "fan_speed": 4},
            id="fan-speed-not-synced-when-off",
        ),
        pytest.param(
            {"pwr_on": 0, "fan_speed": 2},
            {"u_pwr_on": 1, "u_fan_speed": 4},
            # fan_speed follows the new power state within one payload
            {"pwr_on": True, "u_fan_speed": 5, "fan_speed": 5},
            id="power-and-speed-in-single-payload",
        ),
        pytest.param(
            None,
            {"u_pwr_on": 0, "u_temp_room": "215", "u_hum_stg": 3, "u_damp_pos": 2},
            {
                "pwr_on": False,
                "u_temp_room": 215,
                "hum_stg": 3,
                "damp_pos": 2,
                "online": True,
            },
            id="all-settings-fields-applied",
        ),
        pytest.param(
            {"online": False},
            {"u_pwr_on": 1},
            {"online": True},
            id="always-marks-online",
        ),
    ],
)
async def test_ws_settings_update_normalizes_into_state(
    monkeypatch, initial_condition, settings, expected
):
    kwargs = {} if initial_condition is None else {"initial_condition": initial_condition}
    runtime, cb, _hass = await _build_ws_runtime(monkeypatch, **kwargs)
    await _fire_and_drain(
        runtime, cb,
        {"type": "settings", "data": [{"id": 1, "settings": settings}]},
    )
    state = runtime.coordinator.data["states"]["1"]
    for key, value in expected.items():
        assert state[key] == value
        assert isinstance(state[key], bool) is isinstance(value, bool)

async def test_refresh_device_coalesces_parallel_requests(monkeypatch):
    created_apis = []

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
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

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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

async def test_refresh_device_hung_task_times_out_for_second_caller(monkeypatch):
    """A hung in-flight refresh must not block the second caller indefinitely.

    The timeout applies to each shielded waiter without cancelling or evicting
    the shared owner. This test patches the timeout constant to a tiny value.
    """
    gate = asyncio.Event()          # open in cleanup so inner tasks can finish
    task_started = asyncio.Event()  # signals that the explicit refresh task reached gate.wait()

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.refresh_token = None
            self.login = AsyncMock()
            dev_raw = {
                "id": 1, "name": "Dev1", "model": "m",
                "online": True, "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {},
            }
            self._dev = AtmeexDevice.from_raw(dev_raw)
            self.get_devices = AsyncMock(return_value=[self._dev])
            # Authoritative inventory setup does not issue per-device detail
            # requests; the first targeted refresh should block for this test.
            self._get_device_calls = 0

            async def _gate_get_device(_device_id):
                self._get_device_calls += 1
                if self._get_device_calls >= 1:
                    task_started.set()  # notify that we reached the blocking point
                    await gate.wait()
                return self._dev

            self.get_device = _gate_get_device

        @property
        def token(self):
            return "tok"

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())
    # Patch the in-flight timeout to a tiny value so the test completes quickly
    monkeypatch.setattr(atmeex_init, "_REFRESH_TASK_TIMEOUT_SEC", 0.05)

    monkeypatch.setattr(
        atmeex_init,
        "AtmeexCoordinator",
        _IntegrationCoordinatorFake,
    )

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
        options={"update_interval": 30, "enable_websocket": False},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    runtime = entry.runtime_data

    # First call starts — blocks inside gate.wait()
    t1 = asyncio.create_task(runtime.refresh_device(1))
    # Wait until the inner _refresh_device_once task is truly blocking at gate.wait().
    # This ensures refresh_tasks["1"] is populated before the second call is made.
    for _ in range(20):
        if task_started.is_set():
            break
        if t1.done():
            await t1
        await asyncio.sleep(0)
    assert task_started.is_set()

    # Second caller should time out (internal wait_for) rather than hang forever
    with pytest.raises(asyncio.TimeoutError):
        await runtime.refresh_device(1)

    # Unblock the inner task so t1 can finish (avoids lingering tasks)
    gate.set()
    await asyncio.gather(t1, return_exceptions=True)
