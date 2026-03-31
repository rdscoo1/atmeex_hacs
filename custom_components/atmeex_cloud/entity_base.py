from __future__ import annotations

from functools import cached_property
from typing import Any, Awaitable, Callable, Iterable

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .api import AtmeexDevice


class AtmeexEntityMixin:
    """Общее для всех сущностей Atmeex."""

    coordinator: Any  # CoordinatorEntity уже дает .coordinator
    _device_id: int | str
    _device_meta: AtmeexDevice
    _refresh_device_cb: Callable[[int | str], Awaitable[None]] | None = None

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
        *,
        tolerance: float,
    ) -> Any:
        """Return effective value considering pending commands.

        Uses runtime.clear_pending_if_confirmed() as the single decision point.
        """
        runtime = getattr(self, "_runtime", None)
        if (
            runtime is None
            or not hasattr(runtime, "clear_pending_if_confirmed")
            or not hasattr(runtime, "get_pending")
        ):
            return confirmed_value

        use_confirmed = runtime.clear_pending_if_confirmed(
            self._device_id,
            attribute,
            confirmed_value,
            tolerance=tolerance,
        )
        if use_confirmed:
            return confirmed_value

        pending = runtime.get_pending(self._device_id, attribute)
        if pending is None:
            return confirmed_value
        return pending.value

    async def _execute_command(
        self,
        api_coro,
        *,
        pending_attr: str | None = None,
        pending_value: Any = None,
        error_message: str = "Command failed",
    ) -> None:
        """Execute an API command with device lock, pending tracking, and refresh.

        Parameters:
            api_coro: awaitable that performs the API call.
            pending_attr: state attribute name to track as pending (e.g. "fan_speed").
            pending_value: value to record as pending before the call.
            error_message: human-readable message for HomeAssistantError on failure.
        """
        from homeassistant.exceptions import HomeAssistantError
        from .api import ApiError

        runtime = getattr(self, "_runtime", None)

        if pending_attr is not None and runtime is not None:
            runtime.set_pending(self._device_id, pending_attr, pending_value)

        lock = runtime.get_device_lock(self._device_id) if runtime is not None else None

        async def _do() -> None:
            try:
                await api_coro
            except ApiError as err:
                if pending_attr is not None and runtime is not None:
                    runtime.clear_pending(self._device_id, pending_attr)
                raise HomeAssistantError(error_message) from err
            await self._refresh()

        if lock is not None:
            async with lock:
                await _do()
        else:
            await _do()

    @property
    def available(self) -> bool:
        """Entity is available only when device is reported online."""
        st = self._device_state
        if "online" in st:
            return bool(st["online"])
        return bool(getattr(self._device_meta, "online", False))

    @cached_property
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
