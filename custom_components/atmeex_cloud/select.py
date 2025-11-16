from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import AtmeexRuntimeData

_LOGGER = logging.getLogger(__name__)

HUM_OPTIONS = ["off", "1", "2", "3"]
BRIZER_OPTIONS = [
    "приточная вентиляция",  # 0
    "рециркуляция",          # 1
    "смешанный режим",       # 2
    "приточный клапан",      # 3
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex select entities (humidifier + brizer mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api

    entities: list[SelectEntity] = []
    for dev in coordinator.data.get("devices", []):
        did = dev.get("id")
        if did is None:
            continue
        name = dev.get("name") or f"Device {did}"
        entities.append(
            HumidificationSelect(
                coordinator,
                api,
                did,
                f"{name} humidification mode",
            )
        )
        entities.append(
            BrizerModeSelect(
                coordinator,
                api,
                did,
                f"{name} brizer mode",
            )
        )

    if entities:
        async_add_entities(entities)


class _BaseSelect(CoordinatorEntity, SelectEntity):
    def __init__(self, coordinator, api, device_id: int | str, name: str) -> None:
        super().__init__(coordinator)
        self.api = api
        self._device_id = device_id
        self._attr_name = name
        self._attr_has_entity_name = True

    @property
    def _cond(self) -> dict[str, Any]:
        return self.coordinator.data.get("states", {}).get(str(self._device_id), {}) or {}

    @property
    def available(self) -> bool:
        # Если координатор временно без данных — не хотим мигать unavailable
        return True

    @property
    def device_info(self):
        base_name = (
            self._attr_name.replace(" humidification mode", "")
            .replace(" brizer mode", "")
        )
        return {
            "identifiers": {(DOMAIN, str(self._device_id))},
            "name": base_name,
            "manufacturer": "Atmeex",
        }


class HumidificationSelect(_BaseSelect):
    _attr_options = HUM_OPTIONS

    def __init__(self, coordinator, api, device_id, name) -> None:
        super().__init__(coordinator, api, device_id, name)
        self._attr_unique_id = f"{device_id}_hum_mode"

    @property
    def current_option(self) -> str | None:
        stg = self._cond.get("hum_stg")
        if isinstance(stg, int) and 0 <= stg <= 3:
            return HUM_OPTIONS[stg] if stg > 0 else "off"
        return getattr(self, "_attr_current_option", "off")

    async def async_select_option(self, option: str) -> None:
        if option not in HUM_OPTIONS:
            return
        stage = 0 if option == "off" else int(option)
        await self.api.set_humid_stage(self._device_id, stage)
        self._attr_current_option = option
        await self.coordinator.async_request_refresh()


class BrizerModeSelect(_BaseSelect):
    _attr_options = BRIZER_OPTIONS

    def __init__(self, coordinator, api, device_id, name) -> None:
        super().__init__(coordinator, api, device_id, name)
        self._attr_unique_id = f"{device_id}_brizer_mode"

    @property
    def current_option(self) -> str | None:
        pos = self._cond.get("damp_pos")
        if isinstance(pos, int) and 0 <= pos < len(BRIZER_OPTIONS):
            return BRIZER_OPTIONS[pos]
        return getattr(self, "_attr_current_option", BRIZER_OPTIONS[0])

    async def async_select_option(self, option: str) -> None:
        if option not in BRIZER_OPTIONS:
            return
        pos = BRIZER_OPTIONS.index(option)
        await self.api.set_brizer_mode(self._device_id, pos)
        self._attr_current_option = option
        await self.coordinator.async_request_refresh()
