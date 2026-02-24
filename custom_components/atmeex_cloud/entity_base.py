from __future__ import annotations

from functools import cached_property
from typing import Any, Awaitable, Callable

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
