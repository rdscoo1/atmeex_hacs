from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities, supports_humidifier

from .const import BREEZER_MODES, HUMIDIFICATION_OPTIONS
from . import AtmeexRuntimeData

HUM_OPTIONS = HUMIDIFICATION_OPTIONS
BREEZER_OPTIONS = BREEZER_MODES


# Coordinator-driven updates; no per-entity update serialization needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex select entities (humidifier + breezer mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    def _build_entities(dev: AtmeexDevice) -> list[SelectEntity]:
        entities: list[SelectEntity] = []
        state = ((coordinator.data or {}).get("states", {}) or {}).get(str(dev.id), {}) or {}
        if supports_humidifier(state):
            entities.append(
                AtmeexHumidificationSelect(
                    coordinator=coordinator,
                    api=runtime.api,
                    device=dev,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                )
            )
        entities.append(
            AtmeexBreezerSelect(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            )
        )
        return entities

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )


class _BaseSelect(AtmeexEntityMixin, CoordinatorEntity, SelectEntity):
    def __init__(self, coordinator, api, device: AtmeexDevice, refresh_device_cb=None, runtime=None):
        super().__init__(coordinator)
        self.api = api
        self._device_meta = device
        self._device_id = device.id
        self._attr_has_entity_name = True
        self._refresh_device_cb = refresh_device_cb
        self._runtime = runtime


class AtmeexHumidificationSelect(_BaseSelect):
    _attr_options = HUM_OPTIONS
    _attr_translation_key = "humidification_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
        runtime=None,
    ) -> None:
        super().__init__(
            coordinator,
            api,
            device,
            refresh_device_cb,
            runtime,
        )
        self._attr_unique_id = f"{device.id}_hum_mode"

    @property
    def current_option(self) -> str | None:
        stage = self._state_with_pending(
            "hum_stg", self._device_state.get("hum_stg")
        )
        if (
            isinstance(stage, int)
            and not isinstance(stage, bool)
            and 0 <= stage < len(HUM_OPTIONS)
        ):
            return HUM_OPTIONS[stage]
        return None

    async def async_select_option(self, option: str) -> None:
        if not isinstance(option, str) or option not in HUM_OPTIONS:
            raise self._invalid_value("humidification_option", option)
        stage = 0 if option == "off" else int(option)

        async def operation() -> None:
            await self.api.set_humid_stage(self._device_id, stage)

        await self._execute_command(
            operation,
            pending={"hum_stg": stage},
            translation_placeholders={
                "action": "set humidification stage"
            },
        )


class AtmeexBreezerSelect(_BaseSelect):
    _attr_options = BREEZER_OPTIONS
    _attr_translation_key = "breezer_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
        runtime=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb, runtime)
        self._attr_unique_id = f"{device.id}_breezer_mode"

    @property
    def current_option(self) -> str | None:
        position = self._state_with_pending(
            "damp_pos", self._device_state.get("damp_pos")
        )
        power = self._state_with_pending(
            "pwr_on", self._device_state.get("pwr_on")
        )
        if not isinstance(position, int) or isinstance(position, bool):
            return None
        if position == 0:
            if power is False:
                return BREEZER_OPTIONS[3]
            if power is True:
                return BREEZER_OPTIONS[0]
            return None
        if power is True and 0 < position < 3:
            return BREEZER_OPTIONS[position]
        return None

    async def async_select_option(self, option: str) -> None:
        if not isinstance(option, str) or option not in BREEZER_OPTIONS:
            raise self._invalid_value("breezer_option", option)
        mode = BREEZER_OPTIONS.index(option)
        power = mode != 3
        damper = 0 if mode == 3 else mode

        async def operation() -> None:
            await self.api.set_breezer_mode(self._device_id, mode)

        await self._execute_command(
            operation,
            pending={"pwr_on": power, "damp_pos": damper},
            translation_placeholders={"action": "set breezer mode"},
        )
