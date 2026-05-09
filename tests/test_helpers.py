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
from custom_components.atmeex_cloud.helpers import to_bool, _normalize_device_state
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady


@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("1", True),
        ("0", False),
        ("", False),
        ("foo", True),
        (None, False),
    ],
)
def test_to_bool(value, expected):
    assert to_bool(value) is expected

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
