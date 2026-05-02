"""Binary sensor platform for Atmeex Cloud integration."""
from __future__ import annotations

import datetime
import logging
import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AtmeexRuntimeData
from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities, supports_humidifier

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Atmeex binary sensors from a config entry."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    def _build_entities(dev: AtmeexDevice) -> list[BinarySensorEntity]:
        entities: list[BinarySensorEntity] = []
        state = ((coordinator.data or {}).get("states", {}) or {}).get(str(dev.id), {}) or {}
        # Online status sensor
        entities.append(
            AtmeexOnlineSensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )
        # No water sensor (for humidifier)
        if supports_humidifier(state):
            entities.append(
                AtmeexNoWaterSensor(
                    coordinator=coordinator,
                    device=dev,
                    entry_id=entry.entry_id,
                )
            )
        return entities

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )


class AtmeexOnlineSensor(AtmeexEntityMixin, CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating device online status."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
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
    def available(self) -> bool:
        """Available unless the coordinator has been silent for more than 3× update_interval."""
        last_ts = getattr(self.coordinator, "last_success_ts", None)
        update_interval = getattr(self.coordinator, "update_interval", None)
        if (
            last_ts is not None
            and isinstance(update_interval, datetime.timedelta)
            and update_interval.total_seconds() > 0
        ):
            if time.time() - last_ts > update_interval.total_seconds() * 3:
                return False
        return True

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
