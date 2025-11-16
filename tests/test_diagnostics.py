import pytest
import logging
from types import SimpleNamespace

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.helpers import device_registry as dr

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

    api = SimpleNamespace(_token="t")
    async def _refresh(_device_id):  # pragma: no cover - просто заглушка
        return None

    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=_refresh,
    )
    entry.runtime_data = runtime

    diag = await async_get_config_entry_diagnostics(hass, entry)

    assert diag["entry"]["title"] == entry.title
    # убеждаемся, что структура данных координатора протащилась
    assert diag["coordinator"]["data"]["devices"][0]["id"] == 1
    # и что секреты (email/password) отредактированы
    assert diag["entry"]["data"][CONF_EMAIL] != "user@example.com"
    assert diag["entry"]["data"][CONF_PASSWORD] != "secret"


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

    assert diag["device"]["internal_id"] == "1"
    assert diag["device"]["state"]["pwr_on"] is True
    # и secret-ы также редактируются
    assert diag["entry"]["data"][CONF_EMAIL] != "user@example.com"
