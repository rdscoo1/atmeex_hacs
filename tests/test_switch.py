"""Tests for Atmeex switch entities."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.switch import (
    AtmeexAutoNannySwitch,
    AtmeexPowerSwitch,
    AtmeexSleepModeSwitch,
)
from custom_components.atmeex_cloud import AtmeexRuntimeData


RAW_DEVICE = {"id": 42, "name": "Test Breezer", "model": "X200", "online": True}


def _make_switches(state: dict | None = None, *, with_runtime: bool = False):
    dev = AtmeexDevice.from_raw(RAW_DEVICE)

    coordinator = SimpleNamespace(
        data={
            "device_map": {"42": dev},
            "states": {
                "42": state
                or {"online": True, "u_auto": False, "u_night": False}
            },
        },
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )

    api = MagicMock()
    api.set_auto_mode = AsyncMock()
    api.set_sleep_mode = AsyncMock()
    api.set_power = AsyncMock()

    refresh_cb = AsyncMock()
    runtime = None
    if with_runtime:
        runtime = AtmeexRuntimeData(
            api=api,
            coordinator=coordinator,
            refresh_device=refresh_cb,
        )

    auto = AtmeexAutoNannySwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
        runtime=runtime,
    )
    sleep = AtmeexSleepModeSwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
        runtime=runtime,
    )
    return auto, sleep, api, refresh_cb


def test_auto_nanny_properties():
    auto, _, _, _ = _make_switches({"online": True, "u_auto": True})
    assert auto.unique_id == "42_auto_nanny"
    assert getattr(auto, "_attr_name", None) is None
    assert auto.is_on is True
    assert auto.available is True


@pytest.mark.asyncio
async def test_auto_nanny_turn_on_handles_api_error():
    auto, _, api, _ = _make_switches()
    api.set_auto_mode.side_effect = ApiError("boom", status=500)

    with pytest.raises(HomeAssistantError, match="Failed to enable AutoNanny"):
        await auto.async_turn_on()


@pytest.mark.asyncio
async def test_auto_nanny_turn_off_calls_api_and_refresh():
    auto, _, api, refresh_cb = _make_switches()

    await auto.async_turn_off()

    api.set_auto_mode.assert_awaited_once_with(42, False)
    refresh_cb.assert_awaited_once_with(42)


def test_sleep_mode_properties():
    _, sleep, _, _ = _make_switches({"online": False, "u_night": True})
    assert sleep.unique_id == "42_sleep_mode"
    assert getattr(sleep, "_attr_name", None) is None
    assert sleep.is_on is True
    assert sleep.available is False


@pytest.mark.asyncio
async def test_sleep_mode_turn_on_handles_api_error():
    _, sleep, api, _ = _make_switches()
    api.set_sleep_mode.side_effect = ApiError("boom", status=500)

    with pytest.raises(HomeAssistantError, match="Failed to enable Sleep Mode"):
        await sleep.async_turn_on()


@pytest.mark.asyncio
async def test_sleep_mode_turn_off_calls_api_and_refresh():
    _, sleep, api, refresh_cb = _make_switches()

    await sleep.async_turn_off()

    api.set_sleep_mode.assert_awaited_once_with(42, False)
    refresh_cb.assert_awaited_once_with(42)


def test_auto_nanny_is_on_reflects_pending_before_confirmation():
    """is_on must return True immediately after pending is set, even before the API confirms."""
    auto, _, _, _ = _make_switches({"online": True, "u_auto": False}, with_runtime=True)
    # Simulate what _execute_command does before the API call
    auto._runtime.set_pending(42, "u_auto", True)
    assert auto.is_on is True


def test_sleep_mode_is_on_reflects_pending_before_confirmation():
    """is_on must return True immediately after pending is set, even before the API confirms."""
    _, sleep, _, _ = _make_switches({"online": True, "u_night": False}, with_runtime=True)
    sleep._runtime.set_pending(42, "u_night", True)
    assert sleep.is_on is True


# ---------------------------------------------------------------------------
# AtmeexPowerSwitch
# ---------------------------------------------------------------------------

def _make_power_switch(state: dict | None = None, *, with_runtime: bool = False):
    dev = AtmeexDevice.from_raw(RAW_DEVICE)
    coordinator = SimpleNamespace(
        data={
            "device_map": {"42": dev},
            "states": {"42": state or {"online": True, "pwr_on": False}},
        },
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )
    api = MagicMock()
    api.set_power = AsyncMock()
    refresh_cb = AsyncMock()
    runtime = None
    if with_runtime:
        runtime = AtmeexRuntimeData(
            api=api,
            coordinator=coordinator,
            refresh_device=refresh_cb,
        )
    switch = AtmeexPowerSwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
        runtime=runtime,
    )
    return switch, api, refresh_cb


def test_power_switch_unique_id():
    switch, _, _ = _make_power_switch()
    assert switch.unique_id == "42_power"


def test_power_switch_is_on_true():
    switch, _, _ = _make_power_switch({"online": True, "pwr_on": True})
    assert switch.is_on is True


def test_power_switch_is_on_false():
    switch, _, _ = _make_power_switch({"online": True, "pwr_on": False})
    assert switch.is_on is False


def test_power_switch_is_on_reflects_pending():
    switch, _, _ = _make_power_switch({"online": True, "pwr_on": False}, with_runtime=True)
    switch._runtime.set_pending(42, "pwr_on", True)
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_power_switch_turn_on_calls_api():
    switch, api, refresh_cb = _make_power_switch()
    await switch.async_turn_on()
    api.set_power.assert_awaited_once_with(42, True)
    refresh_cb.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_power_switch_turn_off_calls_api():
    switch, api, refresh_cb = _make_power_switch()
    await switch.async_turn_off()
    api.set_power.assert_awaited_once_with(42, False)
    refresh_cb.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_power_switch_turn_on_handles_api_error():
    switch, api, _ = _make_power_switch()
    api.set_power.side_effect = ApiError("boom", status=500)
    with pytest.raises(HomeAssistantError, match="Failed to turn on"):
        await switch.async_turn_on()
