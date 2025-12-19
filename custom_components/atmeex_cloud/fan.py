from __future__ import annotations

import logging
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
            )
        )

    if entities:
        async_add_entities(entities)


class AtmeexFanEntity(AtmeexEntityMixin, CoordinatorEntity, FanEntity):
    """Fan entity exposing Atmeex fan speed as percentage."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_has_entity_name = True
    _attr_percentage_step = 1

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        api,
        entry_id: str | None,
        device: AtmeexDevice,
        refresh_device_cb: Callable[[int | str], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.api = api
        self._entry_id = entry_id
        self._device_meta = device
        self._device_id = device.id
        self._refresh_device_cb = refresh_device_cb
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
        return bool(self._device_state.get("pwr_on", False))

    @property
    def percentage(self) -> int | None:
        return self._speed_to_percentage(self._device_state.get("fan_speed"))

    # ----- commands -----

    async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
        if percentage is None:
            percentage = self.percentage or 100
        speed = self._percentage_to_speed(percentage)
        try:
            await self.api.set_fan_speed(self._device_id, speed)
        except ApiError as err:
            _LOGGER.error("Failed to set fan speed for %s: %s", self._device_id, err)
            return
        await self._refresh()

    async def async_turn_off(self, **kwargs) -> None:
        try:
            await self.api.set_fan_speed(self._device_id, 0)
        except ApiError as err:
            _LOGGER.error("Failed to turn off fan %s: %s", self._device_id, err)
            return
        await self._refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        speed = self._percentage_to_speed(percentage)
        try:
            await self.api.set_fan_speed(self._device_id, speed)
        except ApiError as err:
            _LOGGER.error("Failed to set fan speed for %s: %s", self._device_id, err)
            return
        await self._refresh()
