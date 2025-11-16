from __future__ import annotations

from typing import Any

from homeassistant.components.fan import (
    FanEntity,
    FanEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex fan entities from a config entry."""
    runtime = entry.runtime_data  # AtmeexRuntimeData
    coordinator = runtime.coordinator
    api = runtime.api

    entities: list[AtmeexFan] = []
    for dev in coordinator.data.get("devices", []):
        did = dev.get("id")
        if did is None:
            continue
        name = dev.get("name") or f"Device {did}"
        entities.append(AtmeexFan(coordinator, api, entry.entry_id, did, name))

    if entities:
        async_add_entities(entities)


class AtmeexFan(CoordinatorEntity, FanEntity):
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
        coordinator,
        api,
        entry_id: str,
        device_id: int | str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self.api = api
        self._entry_id = entry_id  # пока не используется, но оставляем для консистентности
        self._device_id = device_id
        self._attr_name = name
        self._attr_unique_id = f"{device_id}_fan"

    # ----- helpers -----

    @property
    def _cond(self) -> dict[str, Any]:
        return self.coordinator.data.get("states", {}).get(str(self._device_id), {}) or {}

    def _speed_to_percentage(self, speed: int | float | None) -> int:
        """Map device speed 0..7 to percentage 0..100."""
        if not isinstance(speed, (int, float)):
            return 0
        s = max(0, min(7, int(speed)))
        if s <= 0:
            return 0
        # 1..7 -> ~14..100
        return int(round(s * 100 / 7))

    def _percentage_to_speed(self, percentage: int | float) -> int:
        """Map percentage 0..100 back to discrete speed 0..7."""
        try:
            p = max(0, min(100, int(percentage)))
        except Exception:
            p = 0
        if p <= 0:
            return 0
        s = int(round(p * 7 / 100))  # 1..7
        return max(1, min(7, s))

    # ----- properties -----

    @property
    def is_on(self) -> bool:
        return bool(self._cond.get("pwr_on", False))

    @property
    def percentage(self) -> int | None:
        return self._speed_to_percentage(self._cond.get("fan_speed"))

    # ----- commands -----

    async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
        if percentage is None:
            percentage = self.percentage or 100
        speed = self._percentage_to_speed(percentage)
        await self.api.set_fan_speed(self._device_id, speed)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        await self.api.set_fan_speed(self._device_id, 0)
        await self.coordinator.async_request_refresh()

    async def async_set_percentage(self, percentage: int) -> None:
        speed = self._percentage_to_speed(percentage)
        await self.api.set_fan_speed(self._device_id, speed)
        await self.coordinator.async_request_refresh()

    # ----- device registry -----

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": self._attr_name,
            "manufacturer": "Atmeex",
        }
