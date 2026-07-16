"""Entry-owned runtime dependencies for the Atmeex Cloud integration."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

from .helpers import normalize_device_id

if TYPE_CHECKING:
    from .api import AtmeexApi
    from .command_executor import (
        AtmeexCommandExecutor,
        PendingCommand,
        RecoveryRefresh,
    )
    from .coordinator import AtmeexCoordinator
    from .state_store import AtmeexStateStore
    from .websocket import WebSocketManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class AtmeexRuntimeData:
    """Dependencies and background tasks owned by one config entry."""

    # Keep these optional throughout the compatibility migration. Production
    # setup supplies them, while platform tests still use lightweight fakes.
    api: AtmeexApi | None
    coordinator: AtmeexCoordinator | None
    refresh_device: RecoveryRefresh | None
    state_store: AtmeexStateStore | None = None
    command_executor: AtmeexCommandExecutor | None = None
    websocket_manager: WebSocketManager | None = None
    websocket_start_task: asyncio.Task[Any] | None = None
    websocket_message_task: asyncio.Task[None] | None = None
    stopping: bool = False
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    refresh_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    inventory_watchdog_task: asyncio.Task[None] | None = None
    websocket_resync_task: asyncio.Task[None] | None = None
    websocket_overflow_count: int = 0

    def __post_init__(self) -> None:
        if self.command_executor is None and self.refresh_device is not None:
            from .command_executor import AtmeexCommandExecutor

            self.command_executor = AtmeexCommandExecutor(self.refresh_device)

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        """Track one entry-owned task until it reaches a terminal state."""
        self.tasks.add(task)

        def task_done(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _LOGGER.warning(
                    "Atmeex background task failed: %s",
                    type(error).__name__,
                )

        task.add_done_callback(task_done)
        return task

    def get_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> PendingCommand | None:
        """Return one executor-owned pending value during migration."""
        if self.command_executor is None:
            return None
        return self.command_executor.get_pending(device_id, attribute)

    def set_pending(
        self,
        device_id: int | str,
        attribute: str,
        value: Any,
    ) -> int | None:
        """Install one temporary pending generation during migration."""
        if self.command_executor is None:
            return None
        return self.command_executor.set_pending(device_id, attribute, value)

    def clear_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> None:
        """Clear one temporary pending field during migration."""
        if self.command_executor is not None:
            self.command_executor.clear_pending(device_id, attribute)

    def cancel_legacy_generation(
        self,
        device_id: int | str,
        generation: int | None,
    ) -> None:
        """Cancel only the supplied temporary eager-command generation."""
        if self.command_executor is not None and generation is not None:
            self.command_executor.cancel_legacy_generation(device_id, generation)

    def get_device_lock(self, device_id: int | str) -> asyncio.Lock:
        """Return the executor lock for the canonical device ID."""
        if self.command_executor is None:
            raise RuntimeError("Command executor is unavailable")
        key = normalize_device_id(device_id)
        return self.command_executor.device_locks.setdefault(key, asyncio.Lock())

    @property
    def pending_commands(self) -> dict[str, dict[str, PendingCommand]]:
        """Return a detached compatibility snapshot of pending values."""
        if self.command_executor is None:
            return {}
        return self.command_executor.pending_commands

    @property
    def device_locks(self) -> dict[str, asyncio.Lock]:
        """Return a detached compatibility mapping of current locks."""
        if self.command_executor is None:
            return {}
        return dict(self.command_executor.device_locks)

    def clear_pending_if_confirmed(
        self,
        device_id: int | str,
        attribute: str,
        confirmed_value: Any,
        tolerance: float = 10.0,
    ) -> bool:
        """Delegate confirmation; tolerance remains for migration callers."""
        del tolerance
        if self.command_executor is None:
            return True
        return self.command_executor.confirm(
            device_id,
            attribute,
            confirmed_value,
        )
