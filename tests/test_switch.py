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
async def test_sleep_mode_turn_off_calls_api_and_refresh():
    _, sleep, api, refresh_cb = _make_switches()

    await sleep.async_turn_off()

    api.set_sleep_mode.assert_awaited_once_with(42, False)
    refresh_cb.assert_awaited_once_with(42)




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
    api.set_auto_mode = AsyncMock()
    api.set_sleep_mode = AsyncMock()
    api.set_breezer_mode = AsyncMock()
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



@pytest.mark.asyncio
async def test_power_switch_turn_on_calls_api():
    switch, api, refresh_cb = _make_power_switch()
    await switch.async_turn_on()
    api.set_power.assert_awaited_once_with(42, True)
    api.set_auto_mode.assert_not_awaited()
    api.set_sleep_mode.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    refresh_cb.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_power_switch_turn_off_calls_api():
    switch, api, refresh_cb = _make_power_switch()
    await switch.async_turn_off()
    api.set_power.assert_awaited_once_with(42, False)
    api.set_auto_mode.assert_not_awaited()
    api.set_sleep_mode.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    refresh_cb.assert_awaited_once_with(42)

