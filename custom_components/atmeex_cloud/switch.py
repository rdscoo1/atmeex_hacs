from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin

from .const import DOMAIN
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex switch entities (AutoNanny + Sleep Mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    entities: list[SwitchEntity] = []

    for key, dev in device_map.items():
        name = dev.name

        entities.append(
            AtmeexAutoNannySwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                name=f"{name} AutoNanny",
                refresh_device_cb=runtime.refresh_device,
            )
        )
        entities.append(
            AtmeexSleepModeSwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                name=f"{name} Sleep Mode",
                refresh_device_cb=runtime.refresh_device,
            )
        )

    if entities:
        async_add_entities(entities)


class _BaseSwitch(AtmeexEntityMixin, CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, api, device: AtmeexDevice, name: str, refresh_device_cb=None):
        super().__init__(coordinator)
        self.api = api
        self._device_meta = device
        self._device_id = device.id
        self._attr_name = name
        self._attr_has_entity_name = True
        self._refresh_device_cb = refresh_device_cb

    async def _refresh(self) -> None:
        if callable(self._refresh_device_cb):
            await self._refresh_device_cb(self._device_id)
        else:
            await self.coordinator.async_request_refresh()

    @property
    def available(self) -> bool:
        st = self._device_state
        return bool(st.get("online", getattr(self._device_meta, "online", False)))


class AtmeexAutoNannySwitch(_BaseSwitch):
    _attr_translation_key = "auto_nanny"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        name,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, name, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_auto_nanny"

    @property
    def is_on(self) -> bool | None:
        return self._device_state.get("u_auto", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.api.set_auto_mode(self._device_id, True)
        await self._refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.api.set_auto_mode(self._device_id, False)
        await self._refresh()


class AtmeexSleepModeSwitch(_BaseSwitch):
    _attr_translation_key = "sleep_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        name,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, name, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_sleep_mode"

    @property
    def is_on(self) -> bool | None:
        return self._device_state.get("u_night", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.api.set_sleep_mode(self._device_id, True)
        await self._refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.api.set_sleep_mode(self._device_id, False)
        await self._refresh()
