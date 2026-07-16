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

    def __init__(
        self,
        hass=None,
        logger=None,
        *,
        api=None,
        state_store=None,
        config_entry_id=None,
        config_entry=None,
        fire_logbook_event=None,
        name=None,
        update_interval=None,
        **kwargs,
    ):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.api = api
        self.state_store = state_store
        self.config_entry_id = config_entry_id
        self.config_entry = config_entry
        self.data = kwargs.get("data")
        self.last_update_success = False
        self.last_update_success_time = None
        # Attributes that AtmeexCoordinator adds
        self.last_api_error = kwargs.get("last_api_error")
        self.last_success_ts = kwargs.get("last_success_ts")
        self.last_inventory_success_mono = None
        self.avg_latency_ms = None
        self.request_retries = 0
        # Extra counter used by some WS tests
        self.update_calls = 0
        self._fire_logbook_event = fire_logbook_event
        self._api_error_last_ts = float("-inf")
        self._api_error_suppressed = 0
        self._last_detail_error = None
        self._last_detail_failure_count = 0
        for static_name in (
            "_safe_detail_error",
            "_needs_detail",
            "_merge_detail_source",
            "_exception_leaves",
        ):
            setattr(
                self,
                static_name,
                getattr(AtmeexCoordinator, static_name),
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
                MethodType(getattr(AtmeexCoordinator, method_name), self),
            )
        self._safe_error_event = AtmeexCoordinator._safe_error_event

    async def async_config_entry_first_refresh(self):
        if hasattr(self, "_async_update_data"):
            self.data = await self._async_update_data()
        self.last_update_success = True

    def async_set_updated_data(self, data):
        self.update_calls += 1
        self.data = data

    async def async_request_refresh(self):
        inventory_success_before = self.last_inventory_success_mono
        self.last_update_success = False
        if hasattr(self, "_async_update_data"):
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
