from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

from .helpers import fan_speed_to_percent, percent_to_fan_speed
from . import AtmeexRuntimeData
from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities

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

_LOGGER = logging.getLogger(__name__)

# Coordinator-driven updates; no per-entity update serialization needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex fan entities from a config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data 
    coordinator = runtime.coordinator
    api = runtime.api

    def _build_entities(dev: AtmeexDevice) -> list[AtmeexFanEntity]:
        return [
            AtmeexFanEntity(
                coordinator=coordinator,
                api=api,
                entry_id=entry.entry_id,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            )
        ]

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )


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
        speed = percent_to_fan_speed(percentage)
        return max(1, speed) if percentage > 0 else speed

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
        )
        if effective_speed != confirmed_speed:
            _LOGGER.debug(
                "Fan: using pending speed=%s instead of confirmed=%s",
                effective_speed,
                confirmed_speed,
            )
        return self._speed_to_percentage(effective_speed)

    # ----- commands -----

    def _validated_percentage(
        self,
        percentage: Any,
        *,
        allow_none: bool = False,
        allow_zero: bool = True,
    ) -> int | float | None:
        if allow_none and percentage is None:
            return None
        if (
            isinstance(percentage, bool)
            or not isinstance(percentage, (int, float))
            or not 0 <= percentage <= 100
            or not math.isfinite(float(percentage))
            or not allow_zero and percentage == 0
        ):
            try:
                error = self._invalid_value("percentage", percentage)
            except (TypeError, ValueError, OverflowError):
                error = self._invalid_value(
                    "percentage",
                    f"<{type(percentage).__name__} outside supported range>",
                )
            raise error
        return percentage

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs,
    ) -> None:
        percentage = self._validated_percentage(
            percentage,
            allow_none=True,
            allow_zero=False,
        )
        speed = (
            self._percentage_to_speed(percentage)
            if percentage is not None
            else None
        )

        async def operation() -> None:
            if speed is not None:
                await self.api.set_fan_speed(self._device_id, speed)
            await self.api.set_power(self._device_id, True)

        pending: dict[str, Any] = {"pwr_on": True}
        if speed is not None:
            pending["fan_speed"] = speed
        await self._execute_command(
            operation,
            pending=pending,
            translation_placeholders={"action": "turn on the fan"},
        )

    async def async_turn_off(self, **kwargs) -> None:
        async def operation() -> None:
            await self.api.set_power(self._device_id, False)

        await self._execute_command(
            operation,
            pending={"pwr_on": False},
            translation_placeholders={"action": "turn off the fan"},
        )

    async def async_set_percentage(self, percentage: int) -> None:
        percentage = self._validated_percentage(percentage)
        if percentage == 0:
            await self.async_turn_off()
            return
        speed = self._percentage_to_speed(percentage)

        async def operation() -> None:
            await self.api.set_fan_speed(self._device_id, speed)
            # Always assert power after changing speed. The coordinator can be
            # stale after a predecessor's successful write but failed
            # confirmation refresh, so its confirmed power value cannot safely
            # decide whether this idempotent write is needed.
            await self.api.set_power(self._device_id, True)

        await self._execute_command(
            operation,
            pending={"fan_speed": speed, "pwr_on": True},
            translation_placeholders={"action": "set the fan speed"},
        )
