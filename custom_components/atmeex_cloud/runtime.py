"""Runtime data structures for the Atmeex Cloud integration.

These are pure data classes with no Home Assistant dependencies beyond
typing — safe to import from anywhere.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """Tracks a pending command to prevent stale state overwrites."""
    value: Any
    timestamp: float
    attribute: str  # e.g., "fan_speed", "pwr_on"


@dataclass
class AtmeexRuntimeData:
    """Единый runtime-объект для записи конфигурации."""
    api: Any  # AtmeexApi
    coordinator: Any  # AtmeexCoordinator
    refresh_device: Callable[[int | str], Awaitable[None]] | None
    # Per-device locks to serialize set+refresh operations
    device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Per-device pending commands: device_id -> {attribute -> PendingCommand}
    pending_commands: dict[str, dict[str, PendingCommand]] = field(default_factory=dict)
    # WebSocket manager for real-time updates (optional, can be None for HTTP-only mode)
    websocket_manager: Any = None  # WebSocketManager | None
    # Task that performs initial WebSocket startup/retry bootstrap.
    websocket_start_task: asyncio.Task[None] | None = None
    # Serialized task for queued websocket state updates.
    websocket_message_task: asyncio.Task[None] | None = None

    def get_device_lock(self, device_id: int | str) -> asyncio.Lock:
        """Get or create a lock for the given device."""
        key = str(device_id)
        if key not in self.device_locks:
            self.device_locks[key] = asyncio.Lock()
        return self.device_locks[key]

    def set_pending(self, device_id: int | str, attribute: str, value: Any) -> float:
        """Record a pending command. Returns the timestamp."""
        key = str(device_id)
        ts = time.monotonic()
        if key not in self.pending_commands:
            self.pending_commands[key] = {}
        self.pending_commands[key][attribute] = PendingCommand(
            value=value, timestamp=ts, attribute=attribute
        )
        _LOGGER.debug(
            "Pending command set: device=%s attr=%s value=%s ts=%.3f",
            device_id, attribute, value, ts
        )
        return ts

    def get_pending(self, device_id: int | str, attribute: str) -> PendingCommand | None:
        """Get pending command if exists."""
        key = str(device_id)
        return self.pending_commands.get(key, {}).get(attribute)

    def clear_pending(self, device_id: int | str, attribute: str) -> None:
        """Clear a pending command after confirmation."""
        key = str(device_id)
        if key in self.pending_commands and attribute in self.pending_commands[key]:
            del self.pending_commands[key][attribute]
            _LOGGER.debug("Pending command cleared: device=%s attr=%s", device_id, attribute)

    def clear_pending_if_confirmed(
        self, device_id: int | str, attribute: str, confirmed_value: Any, tolerance: float = 5.0
    ) -> bool:
        """Clear pending if device confirmed the value or TTL expired.

        Returns True if the confirmed_value should be used (no stale pending).
        Returns False if there's a newer pending command that should take precedence.
        """
        pending = self.get_pending(device_id, attribute)
        if pending is None:
            return True  # No pending, use confirmed value

        age = time.monotonic() - pending.timestamp

        # If pending command is too old, clear it and use confirmed
        if age > tolerance:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command expired: device=%s attr=%s age=%.1fs",
                device_id, attribute, age
            )
            return True

        # If device confirmed our pending value, clear it
        if pending.value == confirmed_value:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command confirmed: device=%s attr=%s value=%s",
                device_id, attribute, confirmed_value
            )
            return True

        # Pending command is newer than this response - ignore stale data
        _LOGGER.debug(
            "Ignoring stale value: device=%s attr=%s confirmed=%s pending=%s age=%.1fs",
            device_id, attribute, confirmed_value, pending.value, age
        )
        return False
