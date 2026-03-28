"""Shared test fixtures for the Atmeex Cloud integration."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.atmeex_cloud.api import AtmeexDevice


class DummyCoordinator:
    """Lightweight coordinator stand-in used by integration-level tests.

    Accepts the same constructor signature as ``AtmeexCoordinator`` (which
    passes everything through to ``DataUpdateCoordinator.__init__``), but
    avoids the full HA machinery.
    """

    def __init__(self, hass, logger, name=None, update_method=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = False
        # Attributes that AtmeexCoordinator adds
        self.last_api_error = None
        self.last_success_ts = None
        # Extra counter used by some WS tests
        self.update_calls = 0

    async def async_config_entry_first_refresh(self):
        self.data = await self.update_method()

    def async_set_updated_data(self, data):
        self.update_calls += 1
        self.data = data


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
        def __init__(self, session):
            self.session = session
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
