"""Plan 5: maximum inventory-age enforcement under continuous push traffic."""
from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.atmeex_cloud.api import AtmeexConnectionError
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _coordinator(hass) -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=AsyncMock(),
        state_store=AtmeexStateStore(),
        config_entry_id="entry-1",
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_fresh_inventory_does_not_force_refresh(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_request_refresh = AsyncMock()
    coordinator.last_inventory_attempt_mono = 1000.0

    # Only 10s old (< 30s cap) -> no forced refresh.
    forced = await coordinator.async_ensure_inventory_fresh(now_mono=1010.0)

    assert forced is False
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_inventory_forces_one_refresh(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_request_refresh = AsyncMock()
    coordinator.last_inventory_attempt_mono = 1000.0

    # 31s old (> 30s cap) -> exactly one authoritative refresh.
    forced = await coordinator.async_ensure_inventory_fresh(now_mono=1031.0)

    assert forced is True
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_never_polled_inventory_forces_refresh(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_request_refresh = AsyncMock()
    coordinator.last_inventory_attempt_mono = None

    forced = await coordinator.async_ensure_inventory_fresh(now_mono=5.0)

    assert forced is True
    coordinator.async_request_refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_failing_polls_are_not_doubled_by_the_watchdog(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.async_request_refresh = AsyncMock()
    # Cloud outage: last success long ago, but the scheduled poll just ran.
    coordinator.last_inventory_success_mono = 100.0
    coordinator.last_inventory_attempt_mono = 1000.0

    forced = await coordinator.async_ensure_inventory_fresh(now_mono=1010.0)

    assert forced is False
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_poll_still_stamps_inventory_attempt(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.api.get_devices = AsyncMock(
        side_effect=AtmeexConnectionError("get_devices", "offline")
    )

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    assert coordinator.last_inventory_attempt_mono is not None
    assert coordinator.last_inventory_success_mono is None
