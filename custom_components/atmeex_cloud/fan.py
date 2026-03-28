from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator
)

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
    _attr_speed_count = 7
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
        self._attr_unique_id = f"{device.id}_fan"

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
        confirmed_pwr = bool(self._device_state.get("pwr_on", False))
        effective_pwr = self._state_with_pending(
            "pwr_on",
            confirmed_pwr,
            tolerance=PENDING_COMMAND_TTL,
        )
        if effective_pwr != confirmed_pwr:
            _LOGGER.debug(
                "Fan: using pending pwr_on=%s instead of confirmed=%s",
                effective_pwr,
                confirmed_pwr,
            )
        return bool(effective_pwr)

    @property
    def percentage(self) -> int | None:
        """Return current fan speed percentage.
        
        Uses pending command value if a recent command was sent and not yet
        confirmed by the device, to prevent UI regression during rapid changes.
        """
        confirmed_speed_raw = self._device_state.get("fan_speed")
        confirmed_speed = int(confirmed_speed_raw) if isinstance(
            confirmed_speed_raw, (int, float)
        ) else None
        effective_speed = self._state_with_pending(
            "fan_speed",
            confirmed_speed,
            tolerance=PENDING_COMMAND_TTL,
        )
        if effective_speed != confirmed_speed:
            _LOGGER.debug(
                "Fan: using pending speed=%s instead of confirmed=%s",
                effective_speed,
                confirmed_speed,
            )
        return self._speed_to_percentage(effective_speed)

    # ----- commands -----

    async def _set_fan_speed_with_lock(self, speed: int) -> None:
        """Set fan speed with race protection.
        
        Uses device lock to serialize operations and tracks pending command
        to prevent stale responses from overwriting newer state.
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
        
        async def _do_set_and_refresh() -> None:
            try:
                await self.api.set_fan_speed(self._device_id, speed)
            except ApiError as err:
                _LOGGER.error("Failed to set fan speed for %s: %s", self._device_id, err)
                # Clear pending on error
                if self._runtime is not None:
                    self._runtime.clear_pending(self._device_id, "fan_speed")
                raise HomeAssistantError("Failed to set fan speed") from err
            
            # Immediately refresh to get confirmed state
            await self._refresh()
            
            _LOGGER.debug(
                "Fan speed set complete: device=%s speed=%s",
                self._device_id, speed
            )
        
        if lock is not None:
            async with lock:
                await _do_set_and_refresh()
        else:
            await _do_set_and_refresh()

    async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
        if percentage is None:
            percentage = self.percentage or 100
        speed = self._percentage_to_speed(percentage)
        # Set pending pwr_on=True for immediate UI feedback
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "pwr_on", True)
        try:
            await self._set_fan_speed_with_lock(speed)
        except HomeAssistantError:
            if self._runtime is not None:
                self._runtime.clear_pending(self._device_id, "pwr_on")
            raise

    async def async_turn_off(self, **kwargs) -> None:
        # Set pending pwr_on=False for immediate UI feedback
        if self._runtime is not None:
            self._runtime.set_pending(self._device_id, "pwr_on", False)

        lock = self._runtime.get_device_lock(self._device_id) if self._runtime else None

        async def _do_turn_off() -> None:
            try:
                await self.api.set_power(self._device_id, False)
            except ApiError as err:
                _LOGGER.error("Failed to turn off fan for %s: %s", self._device_id, err)
                if self._runtime is not None:
                    self._runtime.clear_pending(self._device_id, "pwr_on")
                raise HomeAssistantError("Failed to turn off fan") from err

            await self._refresh()

        if lock is not None:
            async with lock:
                await _do_turn_off()
        else:
            await _do_turn_off()

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return
        speed = self._percentage_to_speed(percentage)
        await self._set_fan_speed_with_lock(speed)
