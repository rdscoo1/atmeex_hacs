from __future__ import annotations

from typing import Any


FAN_MIN = 1
FAN_MAX = 7


def clamp(value: float | int, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, float(value)))


def fan_speed_to_percent(speed: int | float | None) -> int:
    """1..7 → 14..100, 0/None → 0."""
    if not isinstance(speed, (int, float)):
        return 0
    s = int(clamp(speed, 0, FAN_MAX))
    if s <= 0:
        return 0
    return int(round(s * 100 / FAN_MAX))


def percent_to_fan_speed(percent: int | float) -> int:
    """0..100 → 0..7, с округлением."""
    try:
        p = int(clamp(percent, 0, 100))
    except (TypeError, ValueError):
        return 0
    if p <= 0:
        return 0
    s = int(round(p * FAN_MAX / 100))
    return max(FAN_MIN, min(FAN_MAX, s))


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
