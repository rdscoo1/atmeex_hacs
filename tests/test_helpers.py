import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as atmeex_init
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice
from custom_components.atmeex_cloud.const import (
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from custom_components.atmeex_cloud.helpers import (
    _normalize_device_state,
    normalize_condition_delta,
    normalize_device_id,
    normalize_settings_delta,
    parse_atmeex_bool,
)
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("yes", True),
        (False, False),
        (0, False),
        ("0", False),
        ("false", False),
        ("OFF", False),
        ("no", False),
        ("", False),
    ],
)
def test_parse_atmeex_bool_accepts_only_documented_literals(value, expected):
    assert parse_atmeex_bool(value) is expected


@pytest.mark.parametrize("value", [None, "enabled", "2", 2, object()])
def test_parse_atmeex_bool_rejects_unknown_literals(value):
    with pytest.raises(ValueError, match="unsupported Atmeex boolean literal"):
        parse_atmeex_bool(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "1"), ("1", "1"), (" 0007 ", "7"), (123456789, "123456789")],
)
def test_normalize_device_id_returns_stable_string_key(value, expected):
    assert normalize_device_id(value) == expected


@pytest.mark.parametrize("value", [None, True, False, 1.0, "", "   "])
def test_normalize_device_id_rejects_missing_or_boolean_ids(value):
    with pytest.raises(ValueError, match="invalid Atmeex device id"):
        normalize_device_id(value)


def test_condition_delta_ignores_bad_boolean_but_keeps_valid_sibling():
    state_delta, device_delta = normalize_condition_delta(
        {"pwr_on": "unknown", "temp_in": "215"}
    )
    assert "pwr_on" not in state_delta
    assert state_delta == {"temp_in": 215, "online": True}
    assert device_delta == {"condition": {"temp_in": 215}, "online": True}


def test_condition_delta_isolates_invalid_fields_and_ignores_unknown_fields():
    condition = {
        "pwr_on": "unknown",
        "no_water": 2,
        "u_auto": False,
        "u_night": "false",
        "fan_speed": "fast",
        "temp_room": "warm",
        "temp_in": "215",
        "unknown": {"preserve": "input"},
    }
    original = {
        **condition,
        "unknown": dict(condition["unknown"]),
    }

    state_delta, device_delta = normalize_condition_delta(condition)

    assert condition == original
    assert state_delta == {
        "u_auto": False,
        "u_night": False,
        "temp_in": 215,
        "online": True,
    }
    assert device_delta == {
        "condition": {"u_auto": False, "u_night": False, "temp_in": 215},
        "online": True,
    }


def test_condition_delta_accepts_false_protocol_booleans():
    state_delta, device_delta = normalize_condition_delta(
        {"pwr_on": 0, "no_water": "off"}
    )

    assert state_delta == {"pwr_on": False, "no_water": False, "online": True}
    assert device_delta == {
        "condition": {"pwr_on": False, "no_water": False},
        "online": True,
    }


@pytest.mark.parametrize(
    ("power", "expected_fan_speed"),
    [(False, None), (True, 5)],
)
def test_settings_delta_uses_accepted_power_before_fan_speed(
    power, expected_fan_speed
):
    settings = {"u_pwr_on": power, "u_fan_speed": "4"}
    current_state = {"pwr_on": not power, "fan_speed": 2}
    original_settings = dict(settings)
    original_state = dict(current_state)

    state_delta, device_delta = normalize_settings_delta(settings, current_state)

    assert settings == original_settings
    assert current_state == original_state
    assert state_delta == {
        "pwr_on": power,
        "u_fan_speed": 5,
        **({"fan_speed": expected_fan_speed} if expected_fan_speed is not None else {}),
        "online": True,
    }
    assert device_delta == {
        "settings": {"u_pwr_on": power, "u_fan_speed": 4},
        "online": True,
    }


def test_settings_delta_isolates_invalid_fields_and_keeps_valid_siblings():
    settings = {
        "u_pwr_on": "unknown",
        "u_fan_speed": "fast",
        "u_temp_room": "215",
        "u_hum_stg": float("inf"),
        "u_damp_pos": None,
        "u_auto": "invalid",
        "u_night": False,
        "unknown": 42,
    }
    current_state = {"pwr_on": True, "fan_speed": 4}

    state_delta, device_delta = normalize_settings_delta(settings, current_state)

    assert state_delta == {"u_temp_room": 215, "u_night": False, "online": True}
    assert device_delta == {
        "settings": {"u_temp_room": 215, "u_night": False},
        "online": True,
    }


def test_settings_delta_ignores_bad_fan_speed_instead_of_defaulting_to_minimum():
    state_delta, device_delta = normalize_settings_delta(
        {"u_fan_speed": "not-a-speed"}, {"pwr_on": True}
    )

    assert state_delta == {"online": True}
    assert device_delta == {"online": True}


def test_normalize_device_state_basic():
    """Test normalization converts API fan_speed (0-6) to HA fan_speed (1-7).
    
    API returns fan_speed=3, which should be converted to HA fan_speed=4.
    """
    item = {
        "condition": {
            "pwr_on": 1,
            "fan_speed": "3",  # API speed 3 → HA speed 4
            "damp_pos": "2",
            "hum_stg": "1",
            "u_temp_room": "215",
            "hum_room": 47.9,
            "temp_room": 198.3,
        },
        "online": False,
    }
    out = _normalize_device_state(item)
    assert out["pwr_on"] is True
    assert out["fan_speed"] == 4  # API 3 → HA 4
    assert out["damp_pos"] == 2
    assert out["hum_stg"] == 1
    assert out["u_temp_room"] == 215
    assert out["hum_room"] == 47
    assert out["temp_room"] == 198
    assert out["online"] is False

def test_normalize_device_state_uses_settings_and_fan_fallback():
    """Test fallback to settings.u_fan_speed when condition.fan_speed is 0.
    
    API settings.u_fan_speed=4 → HA fan_speed=5.
    Device is online if condition has time field.
    """
    item = {
        "condition": {
            "pwr_on": None,
            "fan_speed": 0,
            "time": "2026-01-27 21:24:15",  # Fresh condition data = online
        },
        "settings": {
            "u_pwr_on": "1",
            "u_fan_speed": 4.2,  # API speed 4 → HA speed 5
            "u_damp_pos": "1",
            "u_temp_room": 205.6,
            "u_hum_stg": "2",
        },
    }
    out = _normalize_device_state(item)
    assert out["pwr_on"] is True
    assert out["fan_speed"] == 5  # API 4 → HA 5
    assert out["damp_pos"] == 1
    assert out["u_temp_room"] == 205
    assert out["hum_stg"] == 2
    assert out["online"] is True  # Has condition.time = online

def test_resolve_update_interval_invalid_input_falls_back_to_default():
    assert atmeex_init._resolve_update_interval_seconds({CONF_UPDATE_INTERVAL: "bad"}) == DEFAULT_UPDATE_INTERVAL
