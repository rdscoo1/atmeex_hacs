from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ApiError, AtmeexDevice
from .entity_base import AtmeexEntityMixin

from . import AtmeexRuntimeData

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex switch entities (AutoNanny + Sleep Mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    entities: list[SwitchEntity] = []

    for dev in device_map.values():
        entities.append(
            AtmeexAutoNannySwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
            )
        )
        entities.append(
            AtmeexSleepModeSwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
            )
        )

    if entities:
        async_add_entities(entities)


class _BaseSwitch(AtmeexEntityMixin, CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, api, device: AtmeexDevice, refresh_device_cb=None):
        super().__init__(coordinator)
        self.api = api
        self._device_meta = device
        self._device_id = device.id
        self._attr_has_entity_name = True
        self._refresh_device_cb = refresh_device_cb


class AtmeexAutoNannySwitch(_BaseSwitch):
    _attr_translation_key = "auto_nanny"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_auto_nanny"

    @property
    def is_on(self) -> bool | None:
        return self._device_state.get("u_auto", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.api.set_auto_mode(self._device_id, True)
        except ApiError as err:
            raise HomeAssistantError(f"Failed to enable AutoNanny: {err}") from err
        await self._refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.api.set_auto_mode(self._device_id, False)
        except ApiError as err:
            raise HomeAssistantError(f"Failed to disable AutoNanny: {err}") from err
        await self._refresh()


class AtmeexSleepModeSwitch(_BaseSwitch):
    _attr_translation_key = "sleep_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_sleep_mode"

    @property
    def is_on(self) -> bool | None:
        return self._device_state.get("u_night", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self.api.set_sleep_mode(self._device_id, True)
        except ApiError as err:
            raise HomeAssistantError(f"Failed to enable Sleep Mode: {err}") from err
        await self._refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.api.set_sleep_mode(self._device_id, False)
        except ApiError as err:
            raise HomeAssistantError(f"Failed to disable Sleep Mode: {err}") from err
        await self._refresh()
