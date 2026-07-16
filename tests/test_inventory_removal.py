"""Plan 5: authoritative device retirement + manual-removal policy."""
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmeex_cloud import async_remove_config_entry_device
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _device(device_id: int | str) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True, "fan_speed": 1},
            "settings": {},
        }
    )


def _coordinator(hass, api, entry_id="entry-1") -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=AtmeexStateStore(),
        config_entry_id=entry_id,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_confirmed_stale_device_loses_config_entry_association(hass) -> None:
    config_entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-1")
    config_entry.add_to_hass(hass)
    entry_id = config_entry.entry_id
    registry = dr.async_get(hass)
    device_entry = registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, "2")},
        name="Device 2",
    )
    api = AsyncMock()
    api.get_devices.side_effect = [[_device(2)], [], []]
    coordinator = _coordinator(hass, api, entry_id)

    await coordinator._async_update_data()  # present
    await coordinator._async_update_data()  # first absence (grace)
    assert entry_id in registry.async_get(device_entry.id).config_entries

    await coordinator._async_update_data()  # second absence -> retire

    # Removing this entry's only association retires the device: HA either
    # deletes the now-orphaned device entry or drops the association.
    retired = registry.async_get(device_entry.id)
    assert retired is None or entry_id not in retired.config_entries


@pytest.mark.asyncio
async def test_manual_removal_refuses_active_and_allows_absent_device(hass) -> None:
    runtime = SimpleNamespace(
        state_store=SimpleNamespace(
            data={"devices": [], "device_map": {"7": _device(7)}, "states": {}}
        ),
        command_executor=None,
    )
    entry = SimpleNamespace(runtime_data=runtime)
    active = SimpleNamespace(identifiers={(DOMAIN, "7")})
    absent = SimpleNamespace(identifiers={(DOMAIN, "8")})

    assert await async_remove_config_entry_device(hass, entry, active) is False
    assert await async_remove_config_entry_device(hass, entry, absent) is True
