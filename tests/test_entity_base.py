"""Shared entity mixin contract tests."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.climate import BREEZER_SWING_MODES
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import AtmeexOnlineSensor
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.sensor import AtmeexCO2Sensor
from custom_components.atmeex_cloud.select import AtmeexBreezerSelect, BREEZER_OPTIONS
from tests.test_climate import _make_entity, _make_entity_with_runtime
from tests.test_fan import _make_fan_entity
from tests.test_select import _make_selects
from tests.test_switch import _make_power_switch, _make_switches


def _make_auto_switch():
    auto, _sleep, api, refresh_cb = _make_switches()
    return auto, None, api, refresh_cb


def _make_sleep_switch():
    _auto, sleep, api, refresh_cb = _make_switches()
    return sleep, None, api, refresh_cb


def _make_power_switch_standard():
    power, api, refresh_cb = _make_power_switch()
    return power, None, api, refresh_cb


def _make_humidification_select():
    hum, _breezer, _cond, api, coord = _make_selects()
    return hum, None, api, coord


def _make_breezer_select():
    _hum, breezer, _cond, api, coord = _make_selects()
    return breezer, None, api, coord


@pytest.mark.parametrize(
    ("make_entity", "set_pending", "read_value", "expected"),
    [
        (
            lambda: _make_entity_with_runtime({"pwr_on": False}),
            lambda ent, runtime: runtime.set_pending(1, "pwr_on", True),
            lambda ent: ent.hvac_mode,
            HVACMode.HEAT,
        ),
        (
            lambda: (_make_switches({"online": True, "u_auto": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_auto", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_switches({"online": True, "u_night": False}, with_runtime=True)[1],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_night", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_power_switch({"online": True, "pwr_on": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "pwr_on", True),
            lambda ent: ent.is_on,
            True,
        ),
    ],
)
def test_pending_state_via_mixin(make_entity, set_pending, read_value, expected):
    made = make_entity()
    ent = made[0]
    runtime = getattr(ent, "_runtime", made[3] if len(made) > 3 else None)

    set_pending(ent, runtime)

    assert read_value(ent) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_entity", "configure_failure", "action", "match"),
    [
        (
            _make_entity_with_runtime,
            lambda ent, api: setattr(api.set_power, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_set_hvac_mode(HVACMode.OFF),
            "Failed to turn off",
        ),
        (
            _make_entity_with_runtime,
            lambda ent, api: setattr(api.set_fan_speed, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_set_fan_mode("5"),
            "Failed to set fan mode",
        ),
        (
            _make_entity,
            lambda ent, api: setattr(api.set_breezer_mode, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_set_swing_mode(BREEZER_SWING_MODES[1]),
            "Failed to set swing mode",
        ),
        (
            _make_fan_entity,
            lambda ent, api: setattr(api.set_fan_speed, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_set_percentage(75),
            "Failed to set fan speed",
        ),
        (
            _make_auto_switch,
            lambda ent, api: setattr(api.set_auto_mode, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_turn_on(),
            "Failed to enable AutoNanny",
        ),
        (
            _make_sleep_switch,
            lambda ent, api: setattr(api.set_sleep_mode, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_turn_on(),
            "Failed to enable Sleep Mode",
        ),
        (
            _make_power_switch_standard,
            lambda ent, api: setattr(api.set_power, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_turn_on(),
            "Failed to turn on",
        ),
        (
            _make_humidification_select,
            lambda ent, api: setattr(api.set_humid_stage, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_select_option("2"),
            "Failed to set humidification stage",
        ),
        (
            _make_breezer_select,
            lambda ent, api: setattr(api.set_breezer_mode, "side_effect", ApiError("boom", status=500)),
            lambda ent: ent.async_select_option(BREEZER_OPTIONS[1]),
            "Failed to set work mode",
        ),
    ],
)
async def test_api_error_translates_to_home_assistant_error(
    make_entity, configure_failure, action, match
):
    made = make_entity()
    ent = made[0]
    api = made[2]
    configure_failure(ent, api)

    with pytest.raises(HomeAssistantError, match=match):
        await action(ent)


def _make_lifecycle_coordinator():
    remove_listener = MagicMock()
    coordinator = MagicMock()
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    coordinator.data = {
        "device_map": {"1": dev},
        "states": {
            "1": {
                "online": True,
                "pwr_on": True,
                "fan_speed": 3,
                "damp_pos": 2,
                "hum_stg": 1,
                "co2_ppm": 420,
            }
        },
    }
    coordinator.async_add_listener = MagicMock(return_value=remove_listener)
    return coordinator, dev, remove_listener


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_factory",
    [
        lambda coord, dev: _make_entity()[0],
        lambda coord, dev: AtmeexFanEntity(coord, MagicMock(), "entry1", dev),
        lambda coord, dev: _make_power_switch()[0],
        lambda coord, dev: AtmeexBreezerSelect(coord, MagicMock(), dev),
        lambda coord, dev: AtmeexCO2Sensor(coord, dev, "entry1"),
        lambda coord, dev: AtmeexOnlineSensor(coord, dev, "entry1"),
    ],
)
async def test_entity_lifecycle_registers_and_removes_coordinator_listener(entity_factory):
    coordinator, dev, remove_listener = _make_lifecycle_coordinator()
    ent = entity_factory(coordinator, dev)
    if getattr(ent, "coordinator", None) is not coordinator:
        ent.coordinator.async_add_listener = coordinator.async_add_listener
        remove_listener = coordinator.async_add_listener.return_value

    await ent.async_added_to_hass()
    ent._call_on_remove_callbacks()
    await ent.async_will_remove_from_hass()

    ent.coordinator.async_add_listener.assert_called_once()
    remove_listener.assert_called_once()
