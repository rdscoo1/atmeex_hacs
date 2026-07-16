from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

import custom_components.atmeex_cloud.coordinator as coordinator_module
from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
)
from custom_components.atmeex_cloud.const import EVENT_API_ERROR
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


_OMITTED = object()


def _device(
    device_id: int | str,
    *,
    name: str | None = None,
    model: str = "AirNanny",
    online: bool = True,
    condition: object = _OMITTED,
    settings: object = _OMITTED,
    **metadata,
) -> AtmeexDevice:
    raw = {
        "id": device_id,
        "name": name or f"Device {device_id}",
        "model": model,
        "online": online,
        **metadata,
    }
    if condition is not _OMITTED:
        raw["condition"] = condition
    if settings is not _OMITTED:
        raw["settings"] = settings
    return AtmeexDevice.from_raw(raw)


def _complete(
    device_id: int | str,
    *,
    name: str | None = None,
    fan_speed: int = 1,
    **metadata,
) -> AtmeexDevice:
    return _device(
        device_id,
        name=name,
        condition={"pwr_on": True, "fan_speed": fan_speed},
        settings={},
        **metadata,
    )


def _coordinator(
    hass,
    api,
    *,
    store: AtmeexStateStore | None = None,
    fire_logbook_event=None,
) -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=store or AtmeexStateStore(),
        config_entry_id="entry-1",
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
        fire_logbook_event=fire_logbook_event,
    )


@pytest.mark.parametrize(
    "device",
    [
        _device(
            1,
            condition={"pwr_on": True, "fan_speed": 1},
            settings={},
        ),
        _device(
            2,
            condition={},
            settings={"u_pwr_on": False, "u_fan_speed": 6},
        ),
    ],
)
def test_explicit_complete_core_sections_do_not_need_detail(device) -> None:
    assert AtmeexCoordinator._needs_detail(device) is False


@pytest.mark.parametrize(
    "device",
    [
        _device(1, condition={"pwr_on": True, "fan_speed": 1}),
        _device(1, settings={"u_pwr_on": True, "u_fan_speed": 1}),
        _device(1, condition={"pwr_on": True}, settings={}),
        _device(1, condition={"fan_speed": 1}, settings={}),
    ],
)
def test_omitted_partial_core_needs_detail(device) -> None:
    # Presence-based per the plan contract: a list item needs a detail fetch
    # only when a core section or the power/fan field is *absent*. Present-but-
    # invalid literals are NOT hydrated away — they surface through state
    # normalization as a truthful UpdateFailed (see
    # test_malformed_nested_inventory_maps_to_update_failed_and_preserves_snapshot).
    assert AtmeexCoordinator._needs_detail(device) is True


@pytest.mark.asyncio
async def test_complete_list_data_creates_no_hydration_tasks(
    hass,
    monkeypatch,
) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_complete(1), _complete(2)]
    created_tasks = 0
    original_create_task = asyncio.TaskGroup.create_task

    def create_task_spy(self, coro, *args, **kwargs):
        nonlocal created_tasks
        created_tasks += 1
        return original_create_task(self, coro, *args, **kwargs)

    monkeypatch.setattr(
        coordinator_module.asyncio.TaskGroup,
        "create_task",
        create_task_spy,
    )
    coordinator = _coordinator(hass, api)

    result = await coordinator._async_update_data()

    assert set(result["device_map"]) == {"1", "2"}
    assert created_tasks == 0
    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_details_use_only_three_worker_tasks_and_requests(
    hass,
    monkeypatch,
) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device(index) for index in range(1, 6)]
    release = asyncio.Event()
    three_started = asyncio.Event()
    active = 0
    maximum_active = 0
    created_tasks = 0
    original_create_task = asyncio.TaskGroup.create_task

    def create_task_spy(self, coro, *args, **kwargs):
        nonlocal created_tasks
        created_tasks += 1
        return original_create_task(self, coro, *args, **kwargs)

    async def get_device(device_id: int | str) -> AtmeexDevice:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            three_started.set()
        try:
            await release.wait()
            return _complete(device_id)
        finally:
            active -= 1

    monkeypatch.setattr(
        coordinator_module.asyncio.TaskGroup,
        "create_task",
        create_task_spy,
    )
    api.get_device.side_effect = get_device
    coordinator = _coordinator(hass, api)
    update_task = asyncio.create_task(coordinator._async_update_data())

    try:
        await asyncio.wait_for(three_started.wait(), timeout=1.0)
        assert active == 3
        assert api.get_device.await_count == 3
        assert created_tasks == 3
        release.set()
        result = await asyncio.wait_for(update_task, timeout=1.0)
    finally:
        release.set()
        if not update_task.done():
            update_task.cancel()
            with suppress(asyncio.CancelledError):
                await update_task

    assert maximum_active == 3
    assert api.get_device.await_count == 5
    assert set(result["device_map"]) == {"1", "2", "3", "4", "5"}


@pytest.mark.asyncio
async def test_successful_detail_keeps_list_metadata_and_partial_state(
    hass,
) -> None:
    listed = _device(
        1,
        name="Authoritative list name",
        model="List model",
        online=False,
        condition={"pwr_on": False},
        settings={},
        **{"capability.version": 2},
    )
    detailed = _device(
        1,
        name="Stale detail name",
        model="Detail model",
        online=True,
        condition={"pwr_on": True, "fan_speed": 2, "temp_in": 210},
        settings={"u_night": False},
    )
    api = AsyncMock()
    api.get_devices.return_value = [listed]
    api.get_device.return_value = detailed
    coordinator = _coordinator(hass, api)

    result = await coordinator._async_update_data()
    merged = result["device_map"]["1"]

    assert merged.name == "Authoritative list name"
    assert merged.model == "List model"
    assert merged.online is False
    assert merged.raw["capability.version"] == 2
    assert merged.condition == {
        "pwr_on": False,
        "fan_speed": 2,
        "temp_in": 210,
    }
    assert merged.settings == {"u_night": False}


@pytest.mark.asyncio
async def test_invalid_list_core_surfaces_as_truthful_update_failed(hass) -> None:
    # Plan contract: a present-but-invalid core literal is NOT hydrated away.
    # It carries both core sections, so no detail is fetched; the invalid value
    # surfaces through state normalization as an authoritative UpdateFailed
    # rather than being silently masked by a detail response.
    api = AsyncMock()
    api.get_devices.return_value = [
        _device(
            1,
            condition={"pwr_on": "invalid", "fan_speed": True},
            settings={},
        )
    ]
    api.get_device.return_value = _complete(1, fan_speed=3)
    coordinator = _coordinator(hass, api)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_detail_failure_uses_canonical_previous_and_list_delta(
    hass,
) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_complete("0007", name="Previous")]
    coordinator = _coordinator(hass, api)
    await coordinator._async_update_data()
    api.get_devices.return_value = [
        _device(
            "0007",
            name="Listed",
            online=False,
            condition={"pwr_on": False},
            **{"capability.version": 3},
        )
    ]
    api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "detail unavailable",
    )

    result = await coordinator._async_update_data()
    merged = result["device_map"]["7"]

    assert merged.name == "Listed"
    assert merged.online is False
    assert merged.raw["capability.version"] == 3
    assert merged.condition == {"pwr_on": False, "fan_speed": 1}
    assert result["states"]["7"]["fan_speed"] == 2
    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)


@pytest.mark.asyncio
async def test_transient_detail_failure_without_previous_aborts_refresh(
    hass,
) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device(1)]
    api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        "detail unavailable",
    )
    store = AtmeexStateStore()
    prior = store.data
    coordinator = _coordinator(hass, api, store=store)

    with pytest.raises(UpdateFailed, match="get_device failed"):
        await coordinator._async_update_data()

    assert store.data is prior
    assert coordinator.last_success_ts is None
    assert coordinator.last_inventory_success_mono is None


@pytest.mark.asyncio
async def test_incomplete_detail_is_accepted_as_is_without_error(
    hass,
) -> None:
    # Plan contract: an incomplete list item triggers a detail fetch, and the
    # detail response is accepted as returned (no post-fetch completeness
    # re-check, no synthetic ProtocolError). A missing optional field such as
    # fan_speed is simply absent — it is not a failure.
    api = AsyncMock()
    api.get_devices.return_value = [_complete(1)]
    coordinator = _coordinator(hass, api)
    await coordinator._async_update_data()
    api.get_devices.return_value = [_device(1)]
    api.get_device.return_value = _device(
        1,
        condition={"pwr_on": True},
        settings={},
    )

    result = await coordinator._async_update_data()

    assert "1" in result["device_map"]
    assert coordinator.last_api_error is None


@pytest.mark.asyncio
async def test_degraded_details_emit_one_fixed_private_aggregate_event(
    hass,
) -> None:
    secret = "household-secret-detail-response"
    api = AsyncMock()
    api.get_devices.return_value = [_complete(1), _complete(2)]
    fire_logbook_event = Mock()
    coordinator = _coordinator(
        hass,
        api,
        fire_logbook_event=fire_logbook_event,
    )
    await coordinator._async_update_data()
    fire_logbook_event.reset_mock()
    api.get_devices.return_value = [_device(1), _device(2)]
    api.get_device.side_effect = AtmeexConnectionError(
        "get_device",
        secret,
        status=503,
    )

    await coordinator._async_update_data()

    fire_logbook_event.assert_called_once_with(
        EVENT_API_ERROR,
        {
            "message": "get_device failed",
            "operation": "get_device",
            "status": 503,
            "error_type": "AtmeexConnectionError",
            "source": "coordinator_detail_hydration",
            "detail_failure_count": 2,
        },
    )
    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)
    assert secret not in str(coordinator.last_api_error)
    assert secret not in repr(fire_logbook_event.call_args)


@pytest.mark.asyncio
async def test_detail_authentication_cancels_siblings_and_never_starts_fourth(
    hass,
) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device(index) for index in range(1, 6)]
    three_started = asyncio.Event()
    blocked_forever = asyncio.Event()
    started: set[int] = set()
    cancelled: set[int] = set()

    async def get_device(device_id: int | str) -> AtmeexDevice:
        canonical_id = int(device_id)
        started.add(canonical_id)
        if len(started) == 3:
            three_started.set()
        await three_started.wait()
        if canonical_id == 1:
            raise AtmeexAuthenticationError(
                "get_device",
                "token rejected",
                status=401,
            )
        try:
            await blocked_forever.wait()
        except asyncio.CancelledError:
            cancelled.add(canonical_id)
            raise
        return _complete(canonical_id)

    api.get_device.side_effect = get_device
    store = AtmeexStateStore()
    prior = store.data
    coordinator = _coordinator(hass, api, store=store)

    with pytest.raises(ConfigEntryAuthFailed, match="get_device failed"):
        await asyncio.wait_for(coordinator._async_update_data(), timeout=1.0)

    assert started == {1, 2, 3}
    assert cancelled == {2, 3}
    assert api.get_device.await_count == 3
    assert store.data is prior
    assert coordinator.last_success_ts is None


@pytest.mark.asyncio
async def test_inventory_duplicate_failure_never_hydrates_or_mutates(hass) -> None:
    api = AsyncMock()
    api.get_devices.side_effect = AtmeexProtocolError(
        "get_devices",
        "duplicate canonical device id",
    )
    store = AtmeexStateStore()
    prior = store.data
    coordinator = _coordinator(hass, api, store=store)

    with pytest.raises(UpdateFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    api.get_device.assert_not_awaited()
    assert store.data is prior


@pytest.mark.asyncio
async def test_unexpected_detail_error_propagates_without_mutation(hass) -> None:
    class UnexpectedBug(RuntimeError):
        pass

    api = AsyncMock()
    api.get_devices.return_value = [_device(1)]
    failure = UnexpectedBug("programming bug")
    api.get_device.side_effect = failure
    store = AtmeexStateStore()
    prior = store.data
    coordinator = _coordinator(hass, api, store=store)

    with pytest.raises(UnexpectedBug) as caught:
        await coordinator._async_update_data()

    assert caught.value is failure
    assert store.data is prior
    assert coordinator.last_api_error is None
