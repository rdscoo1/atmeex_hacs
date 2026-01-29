from __future__ import annotations

import logging
from typing import Any, Callable, Awaitable

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin

from .const import DOMAIN, BREEZER_MODES, HUMIDIFICATION_OPTIONS
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

HUM_OPTIONS = HUMIDIFICATION_OPTIONS
BREEZER_OPTIONS = BREEZER_MODES


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex select entities (humidifier + breezer mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    entities: list[SelectEntity] = []

    for key, dev in device_map.items():
        name = dev.name

        entities.append(
            AtmeexHumidificationSelect(
                coordinator=coordinator,
                api = runtime.api,
                device=dev,
                name=f"{name} humidification mode",
                refresh_device_cb=runtime.refresh_device,
            )
        )
        entities.append(
            AtmeexBreezerSelect(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                name=f"{name} breezer mode",
                refresh_device_cb=runtime.refresh_device,
            )
        )

    if entities:
        async_add_entities(entities)


class _BaseSelect(AtmeexEntityMixin, CoordinatorEntity, SelectEntity):
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


class AtmeexHumidificationSelect(_BaseSelect):
    _attr_options = HUM_OPTIONS
    _attr_translation_key = "humidification_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        name,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, name, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_hum_mode"

    @property
    def current_option(self) -> str | None:
        # сервер может отдавать либо hum_stg (0..3), либо только показания влажности hum_room — пробуем оба
        stg = self._device_state.get("hum_stg")
        if isinstance(stg, int) and 0 <= stg <= 3:
            return HUM_OPTIONS[stg] if stg > 0 else "off"
        # fallback к кэшированному значению (важно для тестов и первого запуска)
        return getattr(self, "_attr_current_option", "off")

    async def async_select_option(self, option: str) -> None:
        if option not in HUM_OPTIONS:
            return
        stage = 0 if option == "off" else int(option)
        await self.api.set_humid_stage(self._device_id, stage)
        self._attr_current_option = option
        await self._refresh()


class AtmeexBreezerSelect(_BaseSelect):
    _attr_options = BREEZER_OPTIONS
    _attr_translation_key = "breezer_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        name,
        refresh_device_cb=None,
    ) -> None:
        super().__init__(coordinator, api, device, name, refresh_device_cb)
        self._attr_unique_id = f"{device.id}_breezer_mode"

    @property
    def current_option(self) -> str | None:
        pos = self._device_state.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos < len(BREEZER_OPTIONS):
            return BREEZER_OPTIONS[pos]
        return getattr(self, "_attr_current_option", BREEZER_OPTIONS[0])

    async def async_select_option(self, option: str) -> None:
        if option not in BREEZER_OPTIONS:
            return
        pos = BREEZER_OPTIONS.index(option)
        await self.api.set_breezer_mode(self._device_id, pos)
        self._attr_current_option = option
        await self._refresh()