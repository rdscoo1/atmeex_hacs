"""Privacy: anonymized labels + whitelist diagnostics never leak sentinels."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.json import json_dumps
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.atmeex_cloud.privacy import anonymous_device_label


def test_anonymous_device_label_is_stable_and_non_reversible():
    label = anonymous_device_label("PRIVATE_DEVICE_ID")
    assert label == anonymous_device_label("PRIVATE_DEVICE_ID")  # stable within run
    assert label != anonymous_device_label("other")
    assert "PRIVATE_DEVICE_ID" not in label
    assert label.startswith("device-")


@pytest.mark.asyncio
async def test_config_entry_diagnostics_omit_every_private_sentinel(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRIVATE_EMAIL@example.com",
        data={
            CONF_EMAIL: "PRIVATE_EMAIL@example.com",
            CONF_PASSWORD: "PRIVATE_PASSWORD",
            "refresh_token": "PRIVATE_REFRESH_TOKEN",
        },
    )
    entry.add_to_hass(hass)

    async def _update():
        return {"devices": [], "device_map": {}, "states": {}}

    coordinator = DataUpdateCoordinator(
        hass, logging.getLogger(__name__), name="t", update_method=_update
    )
    coordinator.data = {
        "devices": [{"id": 1, "name": "PRIVATE_DEVICE_NAME"}],
        "device_map": {"1": SimpleNamespace(name="PRIVATE_DEVICE_NAME")},
        "states": {"1": {"pwr_on": True, "temp_room": 215}},
    }

    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(token="PRIVATE_ACCESS_TOKEN"),
        coordinator=coordinator,
        refresh_device=None,
    )
    entry.runtime_data = runtime

    diag = await async_get_config_entry_diagnostics(hass, entry)
    dumped = json_dumps(diag)

    for sentinel in (
        "PRIVATE_EMAIL@example.com",
        "PRIVATE_PASSWORD",
        "PRIVATE_REFRESH_TOKEN",
        "PRIVATE_ACCESS_TOKEN",
        "PRIVATE_DEVICE_NAME",
    ):
        assert sentinel not in dumped
    # Only the whitelisted count survives.
    assert diag["coordinator"]["device_count"] == 1
