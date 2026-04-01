"""Unit tests for runtime.py — extracted from __init__.py."""
import asyncio
import time

import pytest

from custom_components.atmeex_cloud.runtime import PendingCommand, AtmeexRuntimeData


def test_pending_command_fields():
    pc = PendingCommand(value=5, timestamp=1.0, attribute="fan_speed")
    assert pc.value == 5
    assert pc.attribute == "fan_speed"


def test_runtime_set_get_clear_pending():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    ts = rt.set_pending(1, "fan_speed", 7)
    assert isinstance(ts, float)

    p = rt.get_pending(1, "fan_speed")
    assert p is not None and p.value == 7

    rt.clear_pending(1, "fan_speed")
    assert rt.get_pending(1, "fan_speed") is None


def test_runtime_clear_pending_if_confirmed_matching():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "pwr_on", True)
    assert rt.clear_pending_if_confirmed(1, "pwr_on", True) is True
    assert rt.get_pending(1, "pwr_on") is None


def test_runtime_clear_pending_if_confirmed_stale():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "fan_speed", 7)
    assert rt.clear_pending_if_confirmed(1, "fan_speed", 3) is False
    assert rt.get_pending(1, "fan_speed") is not None


def test_runtime_clear_pending_if_confirmed_expired():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "fan_speed", 7)
    # Backdate the timestamp
    rt.pending_commands["1"]["fan_speed"] = PendingCommand(
        value=7, timestamp=time.monotonic() - 20.0, attribute="fan_speed"
    )
    assert rt.clear_pending_if_confirmed(1, "fan_speed", 3, tolerance=5.0) is True


def test_device_lock_identity():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    lock_a = rt.get_device_lock(1)
    lock_b = rt.get_device_lock(1)
    lock_c = rt.get_device_lock(2)
    assert lock_a is lock_b
    assert lock_a is not lock_c
    assert isinstance(lock_a, asyncio.Lock)
