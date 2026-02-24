"""Tests for Atmeex switch entities."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.switch import (
    AtmeexAutoNannySwitch,
    AtmeexSleepModeSwitch,
)


RAW_DEVICE = {"id": 42, "name": "Test Breezer", "model": "X200", "online": True}


def _make_switches(state: dict | None = None):
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

    refresh_cb = AsyncMock()

    auto = AtmeexAutoNannySwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
    )
    sleep = AtmeexSleepModeSwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
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
