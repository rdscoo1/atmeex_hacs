"""Shared test fixtures for the Atmeex Cloud integration."""
from __future__ import annotations

from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData


class DummyCoordinator:
    """Lightweight coordinator stand-in used by integration-level tests.

    Accepts the same constructor signature as ``AtmeexCoordinator`` (which
    passes everything through to ``DataUpdateCoordinator.__init__``), but
    avoids the full HA machinery.
    """

    def __init__(self, hass=None, logger=None, name=None, update_method=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval
        self.data = kwargs.get("data")
        self.last_update_success = False
        self.last_update_success_time = None
        # Attributes that AtmeexCoordinator adds
        self.last_api_error = kwargs.get("last_api_error")
        self.last_success_ts = kwargs.get("last_success_ts")
        # Extra counter used by some WS tests
        self.update_calls = 0

    def setup_update(self, *, api, state_store, fire_logbook_event):
        self._api = api
        self.state_store = state_store
        self._fire_logbook_event = fire_logbook_event
        self._api_error_last_ts = float("-inf")
        self._api_error_suppressed = 0
        for method_name in (
            "_fetch_devices_safely",
            "_fire_api_error_event",
            "_async_update_data",
        ):
            setattr(
                self,
                method_name,
                MethodType(getattr(AtmeexCoordinator, method_name), self),
            )

    async def async_config_entry_first_refresh(self):
        if hasattr(self, "_async_update_data"):
            self.data = await self._async_update_data()
        else:
            self.data = await self.update_method()
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.update_calls += 1
        self.data = data

    async def async_request_refresh(self):
        if hasattr(self, "_async_update_data"):
            self.data = await self._async_update_data()


def make_fake_api_class(
    *,
    devices: list[dict] | None = None,
    token: str | None = "token",
):
    """Factory for a FakeApi class usable in ``monkeypatch.setattr``."""
    if devices is None:
        devices = [
            {
                "id": 1,
                "name": "Dev1",
                "model": "test-model",
                "online": True,
                "condition": {"pwr_on": 1, "fan_speed": 3},
                "settings": {},
            }
        ]

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = token

            dev_objs = [AtmeexDevice.from_raw(d) for d in devices]
            self.get_devices = AsyncMock(return_value=dev_objs)
            self.get_device = AsyncMock(return_value=dev_objs[0] if dev_objs else None)

        @property
        def token(self):
            return self._token or ""

    return FakeApi


def make_hass_stub(*, with_bus: bool = False, with_create_task: bool = False):
    """Create a minimal ``hass`` SimpleNamespace for unit tests."""
    kw: dict = {
        "data": {},
        "config_entries": SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ),
    }
    if with_bus:
        from unittest.mock import MagicMock
        kw["bus"] = SimpleNamespace(async_fire=MagicMock())
    if with_create_task:
        import asyncio
        kw["async_create_task"] = asyncio.create_task
    return SimpleNamespace(**kw)


def make_entry_stub(*, options: dict | None = None):
    """Create a minimal config-entry SimpleNamespace for unit tests."""
    return SimpleNamespace(
        data={"email": "user@example.com", "password": "pwd"},
        options=options or {},
        entry_id="entry1",
        add_update_listener=lambda _listener: (lambda: None),
        async_on_unload=lambda _cb: None,
    )


def make_runtime(hass, entry, api, coordinator, *, websocket_manager=None):
    """Build runtime data and attach it to a config entry."""
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=AsyncMock(),
        state_store=getattr(coordinator, "state_store", None),
        websocket_manager=websocket_manager,
    )
    entry.runtime_data = runtime
    return runtime
