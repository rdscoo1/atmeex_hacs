from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Iterable

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo

from .api import AtmeexDevice
from .command_executor import (
    AtmeexCommandExecutor,
    CommandCoroutineFactory,
    RecoveryRefresh,
)
from .const import DOMAIN


class AtmeexEntityMixin:
    """Общее для всех сущностей Atmeex."""

    coordinator: Any  # CoordinatorEntity уже дает .coordinator
    _device_id: int | str
    _device_meta: AtmeexDevice
    _refresh_device_cb: RecoveryRefresh | None = None

    @property
    def _device_id_str(self) -> str:
        return str(self._device_id)

    @property
    def _device(self) -> AtmeexDevice:
        """Текущий девайс из device_map, fallback на _device_meta."""
        data = getattr(self.coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        return device_map.get(self._device_id_str) or self._device_meta
    
    @property
    def _device_state(self) -> dict[str, Any]:
        data = getattr(self.coordinator, "data", None) or {}
        return (data.get("states", {}) or {}).get(self._device_id_str, {}) or {}

    async def _refresh(self) -> None:
        """Refresh this device, preferring targeted refresh callback when available."""
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)
            return
        await self.coordinator.async_request_refresh()

    def _state_with_pending(
        self,
        attribute: str,
        confirmed_value: Any,
    ) -> Any:
        """Return the executor's optimistic value while it awaits confirmation."""
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None)
        if executor is None:
            return confirmed_value
        return executor.value_with_pending(
            self._device_id,
            attribute,
            confirmed_value,
        )

    async def _execute_command(
        self,
        operation: CommandCoroutineFactory,
        *,
        pending: Mapping[str, Any],
        translation_key: str = "command_failed",
        translation_placeholders: Mapping[str, str] | None = None,
    ) -> bool:
        """Execute one lazy command through the entry-owned executor."""
        runtime = getattr(self, "_runtime", None)
        executor = getattr(runtime, "command_executor", None)
        if executor is None:
            executor = getattr(self, "_fallback_command_executor", None)
            if executor is None:
                executor = AtmeexCommandExecutor(
                    lambda _device_id: self._refresh()
                )
                self._fallback_command_executor = executor
        return await executor.async_execute(
            self._device_id,
            operation,
            pending=pending,
            translation_key=translation_key,
            translation_placeholders=translation_placeholders,
        )

    @staticmethod
    def _invalid_value(field: str, value: Any) -> ServiceValidationError:
        try:
            rendered_value = str(value)
        except Exception:
            rendered_value = f"<{type(value).__name__} outside supported range>"
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_command_value",
            translation_placeholders={"field": field, "value": rendered_value},
        )

    @staticmethod
    def _unsupported_feature(feature: str) -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unsupported_device_feature",
            translation_placeholders={"feature": feature},
        )

    @property
    def available(self) -> bool:
        """Entity is available only when device is reported online."""
        st = self._device_state
        if "online" in st:
            return bool(st["online"])
        return bool(getattr(self._device_meta, "online", False))

    @property
    def device_info(self) -> DeviceInfo:
        dev = self._device_meta  # мета фиксирована, не зависит от апдейтов
        # Try to get firmware version from raw device data
        raw = getattr(dev, "raw", {}) or {}
        sw_version = raw.get("firmware_version") or raw.get("fw_version") or raw.get("version")
        
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id_str)},
            name=getattr(dev, "name", None),
            manufacturer="Atmeex",
            model=getattr(dev, "model", None),
            sw_version=sw_version,
        )


def supports_humidifier(state: dict[str, Any] | None) -> bool:
    """Return True when the current device state exposes humidifier features."""
    device_state = state or {}
    return "hum_stg" in device_state or "no_water" in device_state


def setup_dynamic_device_entities(
    *,
    entry: Any,
    coordinator: Any,
    async_add_entities: Callable[[list[Any]], None],
    build_entities: Callable[[AtmeexDevice], Iterable[Any]],
) -> None:
    """Add entities for current devices and discover newly available ones later."""
    known_unique_ids: set[str] = set()

    def _sync_entities() -> None:
        data = getattr(coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        new_entities: list[Any] = []

        for dev in device_map.values():
            for entity in build_entities(dev):
                unique_id = getattr(entity, "unique_id", None) or getattr(
                    entity, "_attr_unique_id", None
                )
                if unique_id is None:
                    continue
                unique_key = str(unique_id)
                if unique_key in known_unique_ids:
                    continue
                known_unique_ids.add(unique_key)
                new_entities.append(entity)

        if new_entities:
            async_add_entities(new_entities)

    _sync_entities()

    add_listener = getattr(coordinator, "async_add_listener", None)
    if not callable(add_listener):
        return

    remove_listener = add_listener(_sync_entities)
    async_on_unload = getattr(entry, "async_on_unload", None)
    if callable(async_on_unload):
        async_on_unload(remove_listener)
