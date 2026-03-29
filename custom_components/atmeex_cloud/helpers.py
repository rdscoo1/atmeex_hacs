from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

FAN_MIN = 1
FAN_MAX = 7
# API uses 0-6 range, so we need to convert 1-7 to 0-6
API_FAN_MIN = 0
API_FAN_MAX = 6


def clamp(value: float | int, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, float(value)))


def fan_speed_to_percent(speed: int | float | None) -> int:
    """Convert fan speed (1-7) to percentage (14-100).
    
    Speed 0 or None → 0%
    Speed 1-7 → 14-100%
    """
    if not isinstance(speed, (int, float)):
        return 0
    s = int(clamp(speed, 0, FAN_MAX))
    if s <= 0:
        return 0
    return int(round(s * 100 / FAN_MAX))


def percent_to_fan_speed(percent: int | float) -> int:
    """Convert percentage (0-100) to fan speed (0-7).
    
    0% → 0
    1-100% → 1-7
    """
    try:
        p = int(clamp(percent, 0, 100))
    except (TypeError, ValueError):
        return 0
    if p <= 0:
        return 0
    s = int(round(p * FAN_MAX / 100))
    return max(FAN_MIN, min(FAN_MAX, s))


def fan_speed_to_api(speed: int) -> int:
    """Convert HA fan speed (1-7) to API fan speed (0-6).
    
    HA uses 1-7, API uses 0-6.
    Speed 0 stays 0 (off).
    Speed 1-7 → 0-6
    """
    if speed <= 0:
        return 0
    # Convert 1-7 to 0-6
    return max(API_FAN_MIN, min(API_FAN_MAX, speed - 1))


def api_to_fan_speed(api_speed: int | float | str | None) -> int:
    """Convert API fan speed (0-6) to HA fan speed (1-7).
    
    API uses 0-6 for working speeds, HA uses 1-7.
    Power on/off is determined by pwr_on field, not fan_speed.
    
    API 0 → HA 1 (minimum speed)
    API 1 → HA 2
    ...
    API 6 → HA 7 (maximum speed)
    
    API may return string values, so we handle that.
    """
    if api_speed is None:
        return 1  # Default to minimum speed if not specified
    
    try:
        s = int(api_speed)
    except (TypeError, ValueError):
        return 1  # Default to minimum speed on parse error
    
    # Clamp to valid range and convert: 0-6 → 1-7
    s = max(0, min(6, s))
    return s + 1


def deci_to_c(value: int | float | None) -> float | None:
    """Десятые доли градуса → °C (215 → 21.5)."""
    if not isinstance(value, (int, float)):
        return None
    return float(value) / 10.0


def c_to_deci(value_c: float | int | None) -> int | None:
    """°C → деци-градусы (21.5 → 215)."""
    if value_c is None:
        return None
    try:
        return int(round(float(value_c) * 10))
    except (TypeError, ValueError):
        return None

# Допустимые уровни целевой влажности (для «прилипания» слайдера)
HUM_ALLOWED = [0, 33, 66, 100]


def quantize_humidity(val: int | float | None) -> int:
    """Привести влажность к ближайшему значению 0/33/66/100."""
    if val is None:
        return 0
    from math import isfinite

    try:
        v = float(val)
    except (TypeError, ValueError):
        return 0
    if not isfinite(v):
        return 0
    
    v_clamped = max(0, min(100, v))
    v_int = int(round(v_clamped))
    return min(HUM_ALLOWED, key=lambda x: abs(x - v_int))


def to_bool(v: Any) -> bool:
    """Аккуратное приведение к bool (можно заменить твой _to_bool)."""
    if isinstance(v, bool):
        return v
    try:
        return bool(int(v))
    except (TypeError, ValueError):
        return bool(v)


def _to_int(value: Any) -> int | None:
    """Safe int conversion, returns None on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_condition_update(
    state: dict[str, Any], condition_data: dict[str, Any]
) -> dict[str, Any]:
    """Apply a WebSocket condition update to an existing device state.

    Handles the same fields as ``_normalize_device_state`` but operates
    incrementally — only the fields present in *condition_data* are touched.
    """
    updated = dict(state)

    if "pwr_on" in condition_data:
        updated["pwr_on"] = to_bool(condition_data["pwr_on"])

    if "fan_speed" in condition_data:
        updated["fan_speed"] = api_to_fan_speed(condition_data["fan_speed"])

    for field in ("temp_room", "temp_in", "temp_out", "hum_room", "co2_ppm", "damp_pos", "hum_stg"):
        if field in condition_data:
            parsed = _to_int(condition_data[field])
            if parsed is not None:
                updated[field] = parsed

    if "no_water" in condition_data:
        updated["no_water"] = to_bool(condition_data["no_water"])

    if "time" in condition_data:
        updated["time"] = condition_data["time"]

    for bool_field in ("u_auto", "u_night"):
        if bool_field in condition_data:
            updated[bool_field] = to_bool(condition_data[bool_field])

    # Any received WS condition means device connection is alive right now.
    updated["online"] = True
    return updated


def apply_settings_update(
    state: dict[str, Any], settings_data: dict[str, Any]
) -> dict[str, Any]:
    """Apply a WebSocket settings update to an existing device state.

    Same rationale as ``apply_condition_update`` — incremental overlay.
    """
    updated = dict(state)

    if "u_pwr_on" in settings_data:
        updated["pwr_on"] = to_bool(settings_data["u_pwr_on"])

    if "u_fan_speed" in settings_data:
        normalized_speed = api_to_fan_speed(settings_data["u_fan_speed"])
        updated["u_fan_speed"] = normalized_speed
        # Sync fan_speed (condition) only when device is on to avoid reporting
        # a non-zero speed while the device is actually off.
        if updated.get("pwr_on"):
            updated["fan_speed"] = normalized_speed

    if "u_temp_room" in settings_data:
        parsed = _to_int(settings_data["u_temp_room"])
        if parsed is not None:
            updated["u_temp_room"] = parsed

    if "u_hum_stg" in settings_data:
        parsed = _to_int(settings_data["u_hum_stg"])
        if parsed is not None:
            updated["hum_stg"] = parsed

    if "u_damp_pos" in settings_data:
        parsed = _to_int(settings_data["u_damp_pos"])
        if parsed is not None:
            updated["damp_pos"] = parsed

    for bool_field in ("u_auto", "u_night"):
        if bool_field in settings_data:
            updated[bool_field] = to_bool(settings_data[bool_field])

    updated["online"] = True
    return updated


def _normalize_device_state(item: dict[str, Any]) -> dict[str, Any]:
    """Merge condition + settings into a normalized HA state."""
    cond = dict(item.get("condition") or {})
    st = dict(item.get("settings") or {})

    pwr_cond = cond.get("pwr_on")
    pwr_settings = st.get("u_pwr_on")

    if pwr_cond is not None:
        pwr = to_bool(pwr_cond)
    elif pwr_settings is not None:
        pwr = to_bool(pwr_settings)
    else:
        pwr = False

    _LOGGER.debug(
        "Normalize pwr_on: condition=%s, settings=%s, result=%s",
        pwr_cond,
        pwr_settings,
        pwr,
    )

    fan_raw = cond.get("fan_speed")
    u_fan_raw = st.get("u_fan_speed")

    _LOGGER.debug(
        "Normalize fan_speed: condition.fan_speed=%s, settings.u_fan_speed=%s, pwr=%s",
        fan_raw,
        u_fan_raw,
        pwr,
    )

    if (fan_raw is None or (fan_raw == 0 and pwr)) and u_fan_raw is not None and pwr:
        fan = api_to_fan_speed(u_fan_raw)
    else:
        fan = api_to_fan_speed(fan_raw) if fan_raw is not None else None

    damp = cond.get("damp_pos")
    if damp is None and "u_damp_pos" in st:
        damp = st.get("u_damp_pos")

    u_temp = cond.get("u_temp_room")
    if u_temp is None and "u_temp_room" in st:
        u_temp = st.get("u_temp_room")

    hum_stg = cond.get("hum_stg")
    if hum_stg is None and "u_hum_stg" in st:
        hum_stg = st.get("u_hum_stg")

    hum_room = cond.get("hum_room")
    temp_room = cond.get("temp_room")

    out = dict(cond) if cond else {}
    if pwr is not None:
        out["pwr_on"] = bool(pwr)
    if fan is not None:
        try:
            out["fan_speed"] = int(fan)
        except (TypeError, ValueError):
            pass

    if damp is not None:
        try:
            out["damp_pos"] = int(damp)
        except (TypeError, ValueError):
            pass

    if hum_stg is not None:
        try:
            out["hum_stg"] = int(hum_stg)
        except (TypeError, ValueError):
            pass

    if u_temp is not None:
        try:
            out["u_temp_room"] = int(u_temp)
        except (TypeError, ValueError):
            pass

    if isinstance(hum_room, (int, float)):
        out["hum_room"] = int(hum_room)
    if isinstance(temp_room, (int, float)):
        out["temp_room"] = int(temp_room)

    temp_out = cond.get("temp_out")
    if isinstance(temp_out, (int, float)):
        out["temp_out"] = int(temp_out)

    # Normalize boolean mode flags (auto / night)
    for bool_field in ("u_auto", "u_night"):
        val = cond.get(bool_field)
        if val is None:
            val = st.get(bool_field)
        if val is not None:
            out[bool_field] = to_bool(val)

    online = item.get("online")
    if online is not None:
        out["online"] = bool(online)
    else:
        out["online"] = bool(cond and cond.get("time"))

    return out
