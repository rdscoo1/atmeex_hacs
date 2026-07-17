import pytest
import logging
from types import SimpleNamespace

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers import device_registry as dr

from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.atmeex_cloud import AtmeexRuntimeData, AtmeexCoordinatorData

from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.mark.asyncio
async def test_config_entry_diagnostics(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)

    async def _dummy_update():
        return {"devices": [], "states": {}}

    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData] = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name="Test",
        update_method=_dummy_update,
    )
    coordinator.data = {
        "devices": [{"id": 1, "name": "Dev"}],
        "states": {"1": {"pwr_on": True}},
    }

    api = SimpleNamespace(
        _token="SECRET_ACCESS_TOKEN_SENTINEL",
        refresh_token="SECRET_REFRESH_TOKEN_SENTINEL",
    )
    async def _refresh(_device_id):  # pragma: no cover - просто заглушка
        return None

    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=_refresh,
        websocket_manager=SimpleNamespace(is_connected=True, last_message_age=3.21),
    )
    entry.runtime_data = runtime

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Whitelist-only: no entry.data / entry.title / raw coordinator payload.
    assert "entry" not in diag
    assert "data" not in diag.get("coordinator", {})
    assert diag["integration"]["domain"] == DOMAIN
    assert diag["coordinator"]["device_count"] == 1
    assert diag["websocket"]["configured"] is True
    assert diag["websocket"]["manager_initialized"] is True
    assert diag["websocket"]["is_connected"] is True
    assert diag["websocket"]["last_message_age_sec"] == 3.2

    # Nothing private survives anywhere in the payload.
    dumped = json_dumps(diag)
    for sentinel in (
        "user@example.com",
        "secret",
        "SECRET_ACCESS_TOKEN_SENTINEL",
        "SECRET_REFRESH_TOKEN_SENTINEL",
        "Dev",  # device name must not appear
    ):
        assert sentinel not in dumped


@pytest.mark.asyncio
async def test_config_entry_diagnostics_serializes_api_error(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)

    async def _dummy_update():
        return {"devices": [], "states": {}}

    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData] = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name="Test",
        update_method=_dummy_update,
    )
    coordinator.data = {
        "devices": [{"id": 1, "name": "Dev"}],
        "states": {"1": {"pwr_on": True}},
    }
    coordinator.last_api_error = ApiError("test_diagnostics", "boom", status=500)
    coordinator.last_success_ts = 1234567890.0

    api = SimpleNamespace(_token="t")

    async def _refresh(_device_id):
        return None

    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=_refresh,
    )
    entry.runtime_data = runtime

    diag = await async_get_config_entry_diagnostics(hass, entry)

    # Only the sanitized operation label + status, never the raw error message.
    assert diag["coordinator"]["last_api_error_operation"] == "test_diagnostics"
    assert diag["coordinator"]["last_api_error_status"] == 500
    assert "boom" not in json_dumps(diag)


@pytest.mark.asyncio
async def test_device_diagnostics(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
    )
    entry.add_to_hass(hass)

    async def _dummy_update():
        return {"devices": [], "states": {}}

    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData] = DataUpdateCoordinator(
        hass,
        logging.getLogger(__name__),
        name="Test",
        update_method=_dummy_update,
    )

    coordinator.data = {
        "devices": [{"id": 1, "name": "Dev"}],
        "states": {"1": {"pwr_on": True}},
    }

    api = SimpleNamespace(_token="t")
    async def _refresh(_device_id):
        return None

    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=_refresh,
    )
    entry.runtime_data = runtime

    # создаём запись устройства в реестре
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "1")},
        manufacturer="Atmeex",
        name="Dev",
    )

    diag = await async_get_device_diagnostics(hass, entry, device)

    # Whitelist-only per-device: capability/shape booleans, no name/state/raw.
    assert diag["device"]["known_to_coordinator"] is True
    assert diag["device"]["has_state"] is True
    assert diag["device"]["online"] is False
    assert diag["device"]["has_humidifier"] is False
    assert diag["websocket"]["configured"] is True
    assert diag["websocket"]["manager_initialized"] is False
    assert "entry" not in diag
    for sentinel in ("user@example.com", "secret", "Dev"):
        assert sentinel not in json_dumps(diag)
