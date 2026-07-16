from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
)
from custom_components.atmeex_cloud.const import EVENT_API_ERROR
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _device(device_id: int = 1) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True, "fan_speed": 2},
            "settings": {},
        }
    )


def _coordinator(
    hass,
    api,
    store: AtmeexStateStore,
    *,
    fire_logbook_event=None,
) -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=store,
        config_entry_id="entry-1",
        config_entry=None,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
        fire_logbook_event=fire_logbook_event,
    )


@pytest.mark.asyncio
async def test_valid_empty_inventory_is_a_success(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = []
    api.retry_count = 0
    store = AtmeexStateStore()
    coordinator = _coordinator(hass, api, store)

    result = await coordinator._async_update_data()

    assert result == {"devices": [], "device_map": {}, "states": {}}
    assert set(result) == {"devices", "device_map", "states"}
    assert coordinator.last_success_ts is not None
    assert coordinator.last_inventory_success_mono is not None
    assert coordinator.last_api_error is None
    assert coordinator.avg_latency_ms is not None
    assert coordinator.request_retries == 0
    api.get_devices.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_outage_keeps_confirmed_snapshot_and_success_timestamps(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device()]
    api.retry_count = 0
    store = AtmeexStateStore()
    coordinator = _coordinator(hass, api, store)
    confirmed = await coordinator._async_update_data()
    success_ts = coordinator.last_success_ts
    inventory_mono = coordinator.last_inventory_success_mono
    latency = coordinator.avg_latency_ms
    retries = coordinator.request_retries
    api.get_devices.side_effect = AtmeexConnectionError(
        "get_devices",
        "cloud unavailable",
    )

    with pytest.raises(UpdateFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    assert store.data is confirmed
    assert coordinator.last_success_ts == success_ts
    assert coordinator.last_inventory_success_mono == inventory_mono
    assert coordinator.avg_latency_ms == latency
    assert coordinator.request_retries == retries
    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)


@pytest.mark.asyncio
async def test_authentication_failure_requests_reauthentication(hass) -> None:
    api = AsyncMock()
    api.get_devices.side_effect = AtmeexAuthenticationError(
        "get_devices",
        "access rejected",
        status=401,
    )
    coordinator = _coordinator(hass, api, AtmeexStateStore())

    with pytest.raises(ConfigEntryAuthFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    assert coordinator.last_success_ts is None
    assert coordinator.last_inventory_success_mono is None
    assert isinstance(coordinator.last_api_error, AtmeexAuthenticationError)


@pytest.mark.asyncio
async def test_api_error_event_uses_only_fixed_safe_context(hass) -> None:
    privacy_sentinel = "household-secret-response"
    api = AsyncMock()
    api.get_devices.side_effect = AtmeexConnectionError(
        "get_devices",
        privacy_sentinel,
        status=503,
    )
    fire_logbook_event = Mock()
    coordinator = _coordinator(
        hass,
        api,
        AtmeexStateStore(),
        fire_logbook_event=fire_logbook_event,
    )

    with pytest.raises(UpdateFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    fire_logbook_event.assert_called_once_with(
        EVENT_API_ERROR,
        {
            "message": "get_devices failed",
            "operation": "get_devices",
            "status": 503,
            "error_type": "AtmeexConnectionError",
            "source": "coordinator_update",
        },
    )
    assert privacy_sentinel not in repr(fire_logbook_event.call_args)


@pytest.mark.asyncio
async def test_unexpected_programming_error_propagates_without_event(hass) -> None:
    class UnexpectedBug(RuntimeError):
        pass

    api = AsyncMock()
    api.get_devices.side_effect = UnexpectedBug("programming bug")
    fire_logbook_event = Mock()
    coordinator = _coordinator(
        hass,
        api,
        AtmeexStateStore(),
        fire_logbook_event=fire_logbook_event,
    )

    with pytest.raises(UnexpectedBug, match="programming bug"):
        await coordinator._async_update_data()

    assert coordinator.last_success_ts is None
    assert coordinator.last_inventory_success_mono is None
    assert coordinator.last_api_error is None
    fire_logbook_event.assert_not_called()


@pytest.mark.asyncio
async def test_identical_refresh_does_not_notify_listener(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device()]
    api.retry_count = 0
    coordinator = _coordinator(hass, api, AtmeexStateStore())

    await coordinator.async_refresh()
    first = coordinator.data
    listener = Mock()
    remove_listener = coordinator.async_add_listener(listener)
    try:
        await coordinator.async_refresh()

        assert coordinator.data is first
        assert set(coordinator.data) == {"devices", "device_map", "states"}
        assert coordinator.always_update is False
        listener.assert_not_called()
    finally:
        remove_listener()

