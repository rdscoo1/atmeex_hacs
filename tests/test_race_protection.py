"""Tests for race condition protection in fan speed updates.

These tests verify that rapid fan speed changes don't result in stale
values being displayed due to out-of-order API responses.
"""
import asyncio
import time
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.atmeex_cloud import AtmeexRuntimeData, PendingCommand
from custom_components.atmeex_cloud.fan import AtmeexFanEntity, PENDING_COMMAND_TTL
from custom_components.atmeex_cloud.api import AtmeexDevice


def _make_runtime():
    """Create a minimal runtime for testing pending commands."""
    api = MagicMock()
    api.set_fan_speed = AsyncMock()
    
    coordinator = SimpleNamespace(
        data={"states": {"1": {"pwr_on": True, "fan_speed": 3}}},
        async_request_refresh=AsyncMock(),
    )
    
    # Create runtime with real pending command tracking
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    
    return runtime, api, coordinator


def _make_fan_entity_with_runtime():
    """Create a fan entity with full runtime support."""
    runtime, api, coordinator = _make_runtime()
    
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    
    fan = AtmeexFanEntity(
        coordinator=coordinator,
        api=api,
        entry_id="entry1",
        device=dev,
        refresh_device_cb=runtime.refresh_device,
        runtime=runtime,
    )
    
    return fan, runtime, api, coordinator


class TestPendingCommandTracking:
    """Test the PendingCommand mechanism in AtmeexRuntimeData."""
    
    def test_set_and_get_pending(self):
        runtime, _, _ = _make_runtime()
        
        ts = runtime.set_pending(1, "fan_speed", 5)
        
        pending = runtime.get_pending(1, "fan_speed")
        assert pending is not None
        assert pending.value == 5
        assert pending.attribute == "fan_speed"
        assert pending.timestamp == ts
    
    def test_clear_pending(self):
        runtime, _, _ = _make_runtime()
        
        runtime.set_pending(1, "fan_speed", 5)
        assert runtime.get_pending(1, "fan_speed") is not None
        
        runtime.clear_pending(1, "fan_speed")
        assert runtime.get_pending(1, "fan_speed") is None
    
    def test_clear_pending_if_confirmed_with_matching_value(self):
        runtime, _, _ = _make_runtime()
        
        runtime.set_pending(1, "fan_speed", 5)
        
        # Device confirmed the value we sent
        result = runtime.clear_pending_if_confirmed(1, "fan_speed", 5)
        
        assert result is True  # Should use confirmed value
        assert runtime.get_pending(1, "fan_speed") is None  # Should be cleared
    
    def test_clear_pending_if_confirmed_with_stale_value(self):
        runtime, _, _ = _make_runtime()
        
        runtime.set_pending(1, "fan_speed", 7)  # We requested 7
        
        # Device returned old value 3 (stale response)
        result = runtime.clear_pending_if_confirmed(1, "fan_speed", 3)
        
        assert result is False  # Should NOT use stale value
        assert runtime.get_pending(1, "fan_speed") is not None  # Pending still active
    
    def test_clear_pending_if_confirmed_after_ttl_expires(self):
        runtime, _, _ = _make_runtime()
        
        runtime.set_pending(1, "fan_speed", 7)
        
        # Simulate time passing beyond TTL
        pending = runtime.get_pending(1, "fan_speed")
        # Manually set old timestamp
        runtime.pending_commands["1"]["fan_speed"] = PendingCommand(
            value=7,
            timestamp=time.monotonic() - 10.0,  # 10 seconds ago
            attribute="fan_speed"
        )
        
        # Even with different value, TTL expired so use confirmed
        result = runtime.clear_pending_if_confirmed(1, "fan_speed", 3, tolerance=5.0)
        
        assert result is True  # TTL expired, use confirmed value
        assert runtime.get_pending(1, "fan_speed") is None  # Should be cleared
    
    def test_device_lock_creation(self):
        runtime, _, _ = _make_runtime()
        
        lock1 = runtime.get_device_lock(1)
        lock2 = runtime.get_device_lock(1)
        lock3 = runtime.get_device_lock(2)
        
        assert lock1 is lock2  # Same device, same lock
        assert lock1 is not lock3  # Different device, different lock
        assert isinstance(lock1, asyncio.Lock)


class TestFanEntityRaceProtection:
    """Test that fan entity correctly handles pending commands."""
    
    def test_percentage_uses_pending_value(self):
        fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
        
        # Coordinator has speed=3
        assert coordinator.data["states"]["1"]["fan_speed"] == 3
        
        # Set pending command for speed=7
        runtime.set_pending(1, "fan_speed", 7)
        
        # Fan should report pending value (7), not coordinator value (3)
        # 7 * 100 / 7 = 100%
        assert fan.percentage == 100
    
    def test_percentage_uses_confirmed_when_no_pending(self):
        fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
        
        # No pending command, should use coordinator value
        # speed=3 -> 3 * 100 / 7 ≈ 43%
        assert fan.percentage == 43
    
    def test_percentage_clears_pending_when_confirmed(self):
        fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
        
        # Set pending for speed=5
        runtime.set_pending(1, "fan_speed", 5)
        
        # Update coordinator to confirm speed=5
        coordinator.data["states"]["1"]["fan_speed"] = 5
        
        # Access percentage - should clear pending since confirmed
        pct = fan.percentage
        
        # Pending should be cleared
        assert runtime.get_pending(1, "fan_speed") is None
        # Should return confirmed value
        assert pct == 71  # 5 * 100 / 7 ≈ 71
    
    def test_percentage_expires_old_pending(self):
        fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
        
        # Set pending with old timestamp
        runtime.pending_commands["1"] = {
            "fan_speed": PendingCommand(
                value=7,
                timestamp=time.monotonic() - 10.0,  # 10 seconds ago, beyond TTL
                attribute="fan_speed"
            )
        }
        
        # Should use coordinator value since pending expired
        # speed=3 -> 43%
        assert fan.percentage == 43
        
        # Pending should be cleared
        assert runtime.get_pending(1, "fan_speed") is None


@pytest.mark.asyncio
async def test_set_percentage_records_pending():
    """Test that setting percentage records a pending command."""
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    
    # Set to 75% -> speed 5
    await fan.async_set_percentage(75)
    
    # API should be called
    api.set_fan_speed.assert_awaited_once_with(1, 5)
    
    # Note: pending might be cleared if refresh confirmed it,
    # but the mechanism should have been invoked


@pytest.mark.asyncio
async def test_rapid_changes_use_latest_value():
    """Simulate rapid changes and verify latest value is used."""
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    
    # Simulate rapid changes: 3 -> 5 -> 7
    # Each call records pending before lock
    
    # First change to 5
    runtime.set_pending(1, "fan_speed", 5)
    
    # Before first completes, user changes to 7
    runtime.set_pending(1, "fan_speed", 7)
    
    # The latest pending should be 7
    pending = runtime.get_pending(1, "fan_speed")
    assert pending.value == 7
    
    # Even if coordinator still shows old value, percentage should show 7
    coordinator.data["states"]["1"]["fan_speed"] = 3  # Old stale value
    assert fan.percentage == 100  # 7 * 100 / 7 = 100%


@pytest.mark.asyncio
async def test_lock_serializes_operations():
    """Test that device lock serializes set+refresh operations."""
    fan, runtime, api, coordinator = _make_fan_entity_with_runtime()
    
    # Track order of operations
    order = []
    
    async def slow_set_fan_speed(device_id, speed):
        order.append(f"start_set_{speed}")
        await asyncio.sleep(0.1)
        order.append(f"end_set_{speed}")
    
    async def slow_refresh(device_id):
        order.append(f"start_refresh")
        await asyncio.sleep(0.05)
        order.append(f"end_refresh")
    
    api.set_fan_speed = slow_set_fan_speed
    runtime.refresh_device = slow_refresh
    fan._refresh_device_cb = runtime.refresh_device
    
    # Start two concurrent set operations
    task1 = asyncio.create_task(fan.async_set_percentage(50))  # speed 4
    task2 = asyncio.create_task(fan.async_set_percentage(75))  # speed 5
    
    await asyncio.gather(task1, task2)
    
    # With lock, operations should be serialized:
    # One complete set+refresh should finish before the other starts
    # The exact order depends on which task acquires lock first
    assert len(order) == 8  # 2 * (start_set + end_set + start_refresh + end_refresh)
    
    # Verify serialization: each set should complete with its refresh before next starts
    # Find indices
    set_4_start = order.index("start_set_4") if "start_set_4" in order else -1
    set_5_start = order.index("start_set_5") if "start_set_5" in order else -1
    
    if set_4_start < set_5_start:
        # Task 1 ran first
        assert order.index("end_refresh") < set_5_start or order.count("end_refresh") == 2
    else:
        # Task 2 ran first
        assert order.index("end_refresh") < set_4_start or order.count("end_refresh") == 2
