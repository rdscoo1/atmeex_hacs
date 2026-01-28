from __future__ import annotations

import logging
import time
from typing import Any, Callable, Awaitable

from .helpers import fan_speed_to_percent, percent_to_fan_speed
from . import AtmeexRuntimeData
from .api import ApiError, AtmeexDevice
from .entity_base import AtmeexEntityMixin

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Tolerance for pending command expiration (seconds)
# Increased to 8s because API condition updates are slow
PENDING_COMMAND_TTL = 8.0

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex fan entities from a config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data 
    coordinator = runtime.coordinator
    api = runtime.api

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    entities: list[AtmeexFanEntity] = []

    for key, dev in device_map.items():
        entities.append(
            AtmeexFanEntity(
                coordinator=coordinator,
                api=api,
                entry_id=entry.entry_id,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            )
        )

    if entities:
        async_add_entities(entities)


class AtmeexFanEntity(AtmeexEntityMixin, CoordinatorEntity, FanEntity):
    """Fan entity exposing Atmeex fan speed as percentage."""

    # Build supported features - TURN_ON/TURN_OFF added in HA 2024.8
    _supported_features = FanEntityFeature.SET_SPEED
    if hasattr(FanEntityFeature, "TURN_ON"):
        _supported_features |= FanEntityFeature.TURN_ON
    if hasattr(FanEntityFeature, "TURN_OFF"):
        _supported_features |= FanEntityFeature.TURN_OFF
    
    _attr_supported_features = _supported_features
    _attr_has_entity_name = True
    _attr_percentage_step = 1
    _attr_translation_key = "breezer_fan"

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api,
        entry_id: str | None,
        device: AtmeexDevice,
        refresh_device_cb: Callable[[int | str], Awaitable[None]] | None = None,
        runtime: AtmeexRuntimeData | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.api = api
        self._entry_id = entry_id
        self._device_meta = device
        self._device_id = device.id
        self._refresh_device_cb = refresh_device_cb
        self._runtime = runtime
        self._attr_name = device.name
        self._attr_unique_id = f"{device.id}_fan"

    async def _refresh(self) -> None:
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)
        else:
            await self.coordinator.async_request_refresh()

    @property
    def _online(self) -> bool:
        """Онлайн: сначала из state.online, затем из meta.online."""
        st = self._device_state
        if "online" in st:
            return bool(st["online"])
        return bool(self._device_meta.online)

    @property
    def available(self) -> bool:
        return self._online

    def _speed_to_percentage(self, speed: int | float | None) -> int:
        return fan_speed_to_percent(speed)

    def _percentage_to_speed(self, percentage: int | float) -> int:
        return percent_to_fan_speed(percentage)

    # ----- properties -----

    @property
    def is_on(self) -> bool:
        """Return True if the fan is on.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression.
        """
        confirmed_pwr = self._device_state.get("pwr_on", False)
        
        # Check if there's a pending pwr_on command that should take precedence
        if self._runtime is not None:
            pending = self._runtime.get_pending(self._device_id, "pwr_on")
            if pending is not None:
                age = time.monotonic() - pending.timestamp
                if age <= PENDING_COMMAND_TTL:
                    # Use pending value if not expired and not yet confirmed
                    if pending.value != confirmed_pwr:
                        _LOGGER.debug(
                            "Fan: Using pending pwr_on=%s instead of confirmed=%s (age=%.1fs)",
                            pending.value, confirmed_pwr, age
                        )
                        return bool(pending.value)
                    else:
                        # Device confirmed our value, clear pending
                        self._runtime.clear_pending(self._device_id, "pwr_on")
                else:
                    # Pending expired, clear it
                    self._runtime.clear_pending(self._device_id, "pwr_on")
        
        return bool(confirmed_pwr)

    @property
    def percentage(self) -> int | None:
        """Return current fan speed percentage.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression during rapid changes.
        """
        confirmed_speed = self._device_state.get("fan_speed")
        
        # Check if there's a pending fan_speed command that should take precedence
        if self._runtime is not None:
            pending = self._runtime.get_pending(self._device_id, "fan_speed")
            if pending is not None:
                age = time.monotonic() - pending.timestamp
                if age <= PENDING_COMMAND_TTL:
                    # Use pending value if not expired and not yet confirmed
                    if pending.value != confirmed_speed:
                        _LOGGER.debug(
                            "Using pending fan_speed=%s instead of confirmed=%s (age=%.1fs)",
                            pending.value, confirmed_speed, age
                        )
                        return self._speed_to_percentage(pending.value)
                    else:
                        # Device confirmed our value, clear pending
                        self._runtime.clear_pending(self._device_id, "fan_speed")
                else:
                    # Pending expired, clear it
                    self._runtime.clear_pending(self._device_id, "fan_speed")
        
        return self._speed_to_percentage(confirmed_speed)

    # ----- commands -----

    async def _set_fan_speed_with_lock(self, speed: int) -> bool:
        """Set fan speed with race protection.
        
        Uses device lock to serialize operations and tracks pending command
        to prevent stale responses from overwriting newer state.
        
        Returns True on success, False on failure.
        """
        # Record pending command BEFORE acquiring lock (captures user intent timestamp)
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "fan_speed", speed)
        
        _LOGGER.debug(
            "Setting fan speed: device=%s speed=%s",
            self._device_id, speed
        )
        
        # Use device lock to serialize set+refresh operations
        lock = self._runtime.get_device_lock(self._device_id) if self._runtime else None
        
        async def _do_set_and_refresh():
            try:
                await self.api.set_fan_speed(self._device_id, speed)
            except ApiError as err:
                _LOGGER.error("Failed to set fan speed for %s: %s", self._device_id, err)
                # Clear pending on error
                if self._runtime is not None:
                    self._runtime.clear_pending(self._device_id, "fan_speed")
                return False
            
            # Immediately refresh to get confirmed state
            await self._refresh()
            
            _LOGGER.debug(
                "Fan speed set complete: device=%s speed=%s",
                self._device_id, speed
            )
            return True
        
        if lock is not None:
            async with lock:
                return await _do_set_and_refresh()
        else:
            return await _do_set_and_refresh()

    async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
        if percentage is None:
            percentage = self.percentage or 100
        speed = self._percentage_to_speed(percentage)
        # Set pending pwr_on=True for immediate UI feedback
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "pwr_on", True)
        await self._set_fan_speed_with_lock(speed)

    async def async_turn_off(self, **kwargs) -> None:
        # Set pending pwr_on=False for immediate UI feedback
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "pwr_on", False)
        await self._set_fan_speed_with_lock(0)

    async def async_set_percentage(self, percentage: int) -> None:
        speed = self._percentage_to_speed(percentage)
        await self._set_fan_speed_with_lock(speed)
