"""Binary sensor platform for Atmeex Cloud integration."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AtmeexRuntimeData
from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Atmeex binary sensors from a config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    entities: list[BinarySensorEntity] = []

    for dev in device_map.values():
        # Online status sensor
        entities.append(
            AtmeexOnlineSensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )
        # No water sensor (for humidifier)
        entities.append(
            AtmeexNoWaterSensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )

    if entities:
        async_add_entities(entities)


class AtmeexOnlineSensor(AtmeexEntityMixin, CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating device online status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_has_entity_name = True
    _attr_translation_key = "online"

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
    ) -> None:
        """Initialize the online sensor."""
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._attr_unique_id = f"{device.id}_online"

    @property
    def is_on(self) -> bool:
        """Return True if the device is online."""
        return bool(self._device_state.get("online", False))


class AtmeexNoWaterSensor(AtmeexEntityMixin, CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating no water in humidifier."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_has_entity_name = True
    _attr_translation_key = "no_water"

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
    ) -> None:
        """Initialize the no water sensor."""
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._attr_unique_id = f"{device.id}_no_water"

    @property
    def is_on(self) -> bool:
        """Return True if there is no water (problem state)."""
        return bool(self._device_state.get("no_water", False))
