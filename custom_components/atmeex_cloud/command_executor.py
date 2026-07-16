"""Per-device atomic command execution for Atmeex entities."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .api import ApiError
from .const import DOMAIN
from .helpers import normalize_device_id

CommandCoroutineFactory = Callable[[], Awaitable[None]]
RecoveryRefresh = Callable[[int | str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PendingCommand:
    """Newest optimistic value for one state field."""

    value: Any
    generation: int
    expires_at: float


class AtmeexCommandExecutor:
    """Serialize complete logical commands per canonical device ID."""

    def __init__(
        self,
        refresh_device: RecoveryRefresh,
        *,
        pending_ttl: float = 10.0,
    ) -> None:
        self._refresh_device = refresh_device
        self._pending_ttl = pending_ttl
        self._generation = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._lock_users: dict[str, int] = {}
        self._remove_when_idle: set[str] = set()
        self._release_observers: dict[
            str, tuple[asyncio.Lock, bool, object, Callable[[], None]]
        ] = {}
        # Values are installed before lock acquisition. Per-field stacks let a
        # cancelled newer waiter reveal a still-live owner generation.
        self._pending: dict[str, dict[str, list[PendingCommand]]] = {}
        self._executed_generations: set[int] = set()
        self._confirmation_ready_generations: set[int] = set()

    @staticmethod
    def _device_key(device_id: int | str) -> str:
        return normalize_device_id(device_id)

    def _next_generation(self) -> int:
        self._generation += 1
        return self._generation

    def _lock_for(self, device_id: int | str) -> asyncio.Lock:
        key = self._device_key(device_id)
        return self._locks.setdefault(key, asyncio.Lock())

    def _retain_lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.setdefault(key, asyncio.Lock())
        self._lock_users[key] = self._lock_users.get(key, 0) + 1
        return lock

    def _release_lock(self, key: str, lock: asyncio.Lock) -> None:
        remaining = self._lock_users[key] - 1
        if remaining:
            self._lock_users[key] = remaining
            return
        self._lock_users.pop(key, None)
        if key in self._remove_when_idle:
            self._cleanup_removed_lock(key, lock)

    @staticmethod
    def _lock_has_waiters(lock: asyncio.Lock) -> bool:
        waiters = getattr(lock, "_waiters", None)
        return bool(
            waiters
            and any(not waiter.cancelled() for waiter in waiters)
        )

    def _observe_lock_release(self, key: str, lock: asyncio.Lock) -> None:
        existing = self._release_observers.get(key)
        if existing is not None and existing[0] is lock:
            return
        if existing is not None:
            self._restore_lock_release(key, existing[0])

        instance_attributes = vars(lock)
        had_instance_release = "release" in instance_attributes
        original_instance_release = instance_attributes.get("release")
        original_release = lock.release

        def observed_release() -> None:
            original_release()
            self._schedule_removed_lock_cleanup(key, lock)

        setattr(lock, "release", observed_release)
        self._release_observers[key] = (
            lock,
            had_instance_release,
            original_instance_release,
            observed_release,
        )

    def _schedule_removed_lock_cleanup(
        self,
        key: str,
        lock: asyncio.Lock,
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._cleanup_removed_lock(key, lock)
        else:
            # Let a waiter awakened by Lock.release acquire first. The cleanup
            # callback then sees either its locked state or tracked executor
            # reference and preserves serialization.
            loop.call_soon(self._cleanup_removed_lock, key, lock)

    def _restore_lock_release(self, key: str, lock: asyncio.Lock) -> None:
        observer = self._release_observers.get(key)
        if observer is None or observer[0] is not lock:
            return
        _, had_instance_release, original_instance_release, callback = observer
        instance_attributes = vars(lock)
        if instance_attributes.get("release") is callback:
            if had_instance_release:
                instance_attributes["release"] = original_instance_release
            else:
                instance_attributes.pop("release", None)
        self._release_observers.pop(key, None)

    def _cleanup_removed_lock(self, key: str, lock: asyncio.Lock) -> None:
        if key not in self._remove_when_idle or self._locks.get(key) is not lock:
            self._restore_lock_release(key, lock)
            return
        if (
            self._lock_users.get(key, 0)
            or lock.locked()
            or self._lock_has_waiters(lock)
        ):
            self._observe_lock_release(key, lock)
            return
        self._restore_lock_release(key, lock)
        self._locks.pop(key, None)
        self._remove_when_idle.discard(key)

    def _install_pending(
        self,
        device_id: int | str,
        generation: int,
        values: Mapping[str, Any],
    ) -> None:
        key = self._device_key(device_id)
        expires_at = time.monotonic() + self._pending_ttl
        fields = self._pending.setdefault(key, {})
        for attribute, value in values.items():
            fields.setdefault(attribute, []).append(
                PendingCommand(
                    value=deepcopy(value),
                    generation=generation,
                    expires_at=expires_at,
                )
            )
        if not fields:
            self._pending.pop(key, None)

    def _generation_is_referenced(self, generation: int) -> bool:
        return any(
            item.generation == generation
            for device_fields in self._pending.values()
            for stack in device_fields.values()
            for item in stack
        )

    def _discard_generation_if_unreferenced(self, generation: int) -> None:
        if self._generation_is_referenced(generation):
            return
        self._executed_generations.discard(generation)
        self._confirmation_ready_generations.discard(generation)

    def _clear_generation(self, device_id: int | str, generation: int) -> None:
        key = self._device_key(device_id)
        fields = self._pending.get(key)
        if fields is not None:
            for attribute, stack in tuple(fields.items()):
                stack[:] = [item for item in stack if item.generation != generation]
                if not stack:
                    fields.pop(attribute)
            if not fields:
                self._pending.pop(key, None)
        self._executed_generations.discard(generation)
        self._confirmation_ready_generations.discard(generation)

    def _purge_expired(self, device_id: int | str | None = None) -> None:
        if device_id is None:
            keys = tuple(self._pending)
        else:
            keys = (self._device_key(device_id),)
        now = time.monotonic()
        expired: list[tuple[str, int]] = []
        for key in keys:
            fields = self._pending.get(key, {})
            expired.extend(
                (key, item.generation)
                for stack in fields.values()
                for item in stack
                if now >= item.expires_at
            )
        for key, generation in set(expired):
            self._clear_generation(key, generation)

    def _live_stack(
        self,
        device_id: int | str,
        attribute: str,
    ) -> list[PendingCommand]:
        key = self._device_key(device_id)
        self._purge_expired(key)
        return self._pending.get(key, {}).get(attribute, [])

    def get_pending(
        self,
        device_id: int | str,
        attribute: str,
    ) -> PendingCommand | None:
        """Return a non-expired pending value for compatibility and tests."""
        stack = self._live_stack(device_id, attribute)
        if not stack:
            return None
        pending = stack[-1]
        return PendingCommand(
            value=deepcopy(pending.value),
            generation=pending.generation,
            expires_at=pending.expires_at,
        )

    def confirm(
        self,
        device_id: int | str,
        attribute: str,
        confirmed: Any,
    ) -> bool:
        """Retire only confirmation-safe matching generations."""
        key = self._device_key(device_id)
        stack = self._live_stack(key, attribute)
        if not stack:
            return True
        fields = self._pending[key]
        matching_ready = [
            item.generation
            for item in stack
            if item.generation in self._confirmation_ready_generations
            and item.value == confirmed
        ]
        if not matching_ready:
            return False

        # A successful refresh for N reflects writes serialized before N, but
        # must never retire a newer generation that was merely queued.
        cutoff = max(matching_ready)
        retired = {item.generation for item in stack if item.generation <= cutoff}
        stack[:] = [item for item in stack if item.generation > cutoff]
        if not stack:
            fields.pop(attribute, None)
        if not fields:
            self._pending.pop(key, None)
        for generation in retired:
            self._discard_generation_if_unreferenced(generation)
        return not stack

    def allow_recovery_confirmation(self, device_id: int | str) -> None:
        """Allow an authoritative recovery to confirm completed writes."""
        key = self._device_key(device_id)
        self._purge_expired(key)
        fields = self._pending.get(key, {})
        self._confirmation_ready_generations.update(
            item.generation
            for stack in fields.values()
            for item in stack
            if item.generation in self._executed_generations
        )

    def value_with_pending(
        self,
        device_id: int | str,
        attribute: str,
        confirmed: Any,
    ) -> Any:
        """Return the newest pending value until confirmation or expiry."""
        if self.confirm(device_id, attribute, confirmed):
            return confirmed
        pending = self.get_pending(device_id, attribute)
        return confirmed if pending is None else pending.value

    def remove_device(self, device_id: int | str) -> None:
        """Clear one device without letting a command bypass its active lock."""
        key = self._device_key(device_id)
        fields = self._pending.pop(key, {})
        generations = {
            item.generation for stack in fields.values() for item in stack
        }
        self._executed_generations.difference_update(generations)
        self._confirmation_ready_generations.difference_update(generations)

        lock = self._locks.get(key)
        if self._lock_users.get(key, 0) or (
            lock is not None
            and (lock.locked() or self._lock_has_waiters(lock))
        ):
            # Commands arriving after removal must still queue behind the lock
            # currently protecting an in-flight remote write.
            self._remove_when_idle.add(key)
            if lock is not None:
                self._observe_lock_release(key, lock)
                if not lock.locked():
                    # This is the release-to-waiter handoff window. Audit it on
                    # the next turn so a resumed waiter acquires first, while a
                    # cancelled waiter cannot leave deferred state behind.
                    self._schedule_removed_lock_cleanup(key, lock)
        else:
            if lock is not None:
                self._restore_lock_release(key, lock)
            self._locks.pop(key, None)
            self._remove_when_idle.discard(key)

    def set_pending(
        self,
        device_id: int | str,
        attribute: str,
        value: Any,
    ) -> int:
        """Install an immediately confirmable temporary legacy value."""
        generation = self._next_generation()
        self._install_pending(device_id, generation, {attribute: value})
        self._executed_generations.add(generation)
        self._confirmation_ready_generations.add(generation)
        return generation

    def clear_pending(self, device_id: int | str, attribute: str) -> None:
        """Clear one field without disturbing other pending fields."""
        key = self._device_key(device_id)
        fields = self._pending.get(key)
        if fields is None:
            return
        generations = {item.generation for item in fields.pop(attribute, [])}
        if not fields:
            self._pending.pop(key, None)
        for generation in generations:
            self._discard_generation_if_unreferenced(generation)

    def cancel_legacy_generation(
        self,
        device_id: int | str,
        generation: int,
    ) -> None:
        """Remove only one temporary eager-command generation."""
        self._clear_generation(device_id, generation)

    @property
    def pending_commands(self) -> dict[str, dict[str, PendingCommand]]:
        """Expose the newest-value migration view (do not mutate it)."""
        self._purge_expired()
        return {
            key: {
                attribute: PendingCommand(
                    value=deepcopy(stack[-1].value),
                    generation=stack[-1].generation,
                    expires_at=stack[-1].expires_at,
                )
                for attribute, stack in fields.items()
                if stack
            }
            for key, fields in self._pending.items()
        }

    @property
    def device_locks(self) -> dict[str, asyncio.Lock]:
        """Expose the migration view used by device-removal code."""
        return self._locks

    async def _recover_after_write_failure(self, device_id: int | str) -> None:
        try:
            await self._refresh_device(device_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Reconciliation is best effort and must not replace the original
            # command failure or cancellation with an unrelated refresh bug.
            return

    async def async_execute(
        self,
        device_id: int | str,
        operation: CommandCoroutineFactory,
        *,
        pending: Mapping[str, Any],
        translation_key: str,
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> bool:
        """Execute one logical operation and confirmation under one lock."""
        key = self._device_key(device_id)
        generation = self._next_generation()
        self._install_pending(key, generation, pending)
        lock = self._retain_lock(key)

        try:
            try:
                async with lock:
                    try:
                        # The factory is intentionally invoked only after the
                        # per-device lock has been acquired.
                        await operation()
                    except ApiError as err:
                        try:
                            await self._recover_after_write_failure(device_id)
                        finally:
                            self._clear_generation(key, generation)
                        raise HomeAssistantError(
                            translation_domain=DOMAIN,
                            translation_key=translation_key,
                            translation_placeholders=dict(
                                translation_placeholders or {}
                            ),
                        ) from err
                    except asyncio.CancelledError:
                        try:
                            await self._recover_after_write_failure(device_id)
                        finally:
                            self._clear_generation(key, generation)
                        raise
                    except Exception:
                        try:
                            await self._recover_after_write_failure(device_id)
                        finally:
                            self._clear_generation(key, generation)
                        raise

                    if self._generation_is_referenced(generation):
                        self._executed_generations.add(generation)
                    try:
                        await self._refresh_device(device_id)
                    except (ApiError, asyncio.TimeoutError):
                        # The remote write succeeded. A failed confirmation is
                        # not a failed command, and its optimistic value stays.
                        return False
                    except asyncio.CancelledError:
                        try:
                            await self._recover_after_write_failure(device_id)
                        finally:
                            self._clear_generation(key, generation)
                        raise
                    else:
                        if self._generation_is_referenced(generation):
                            self._confirmation_ready_generations.add(generation)
                        return True
            except asyncio.CancelledError:
                # Also handles cancellation while waiting for the lock.
                self._clear_generation(key, generation)
                raise
        finally:
            self._release_lock(key, lock)
