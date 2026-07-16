from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import AtmeexDevice
from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities

from . import AtmeexRuntimeData


# Coordinator-driven updates; no per-entity update serialization needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up Atmeex switch entities (AutoNanny + Sleep Mode)."""
    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    def _build_entities(dev: AtmeexDevice) -> list[SwitchEntity]:
        return [
            AtmeexAutoNannySwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            ),
            AtmeexSleepModeSwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            ),
            AtmeexPowerSwitch(
                coordinator=coordinator,
                api=runtime.api,
                device=dev,
                refresh_device_cb=runtime.refresh_device,
                runtime=runtime,
            ),
        ]

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )


class _BaseSwitch(AtmeexEntityMixin, CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, api, device: AtmeexDevice, refresh_device_cb=None, runtime=None):
        super().__init__(coordinator)
        self.api = api
        self._device_meta = device
        self._device_id = device.id
        self._attr_has_entity_name = True
        self._refresh_device_cb = refresh_device_cb
        self._runtime = runtime

    async def _set_boolean(
        self,
        api_method,
        *,
        attribute: str,
        value: bool,
        action: str,
    ) -> None:
        async def operation() -> None:
            await api_method(self._device_id, value)

        await self._execute_command(
            operation,
            pending={attribute: value},
            translation_placeholders={"action": action},
        )


class AtmeexAutoNannySwitch(_BaseSwitch):
    _attr_translation_key = "auto_nanny"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
        runtime=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb, runtime)
        self._attr_unique_id = f"{device.id}_auto_nanny"

    @property
    def is_on(self) -> bool | None:
        confirmed = self._device_state.get("u_auto", False)
        return bool(self._state_with_pending("u_auto", confirmed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_auto_mode,
            attribute="u_auto",
            value=True,
            action="enable AutoNanny",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_auto_mode,
            attribute="u_auto",
            value=False,
            action="disable AutoNanny",
        )


class AtmeexSleepModeSwitch(_BaseSwitch):
    _attr_translation_key = "sleep_mode"

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
        runtime=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb, runtime)
        self._attr_unique_id = f"{device.id}_sleep_mode"

    @property
    def is_on(self) -> bool | None:
        confirmed = self._device_state.get("u_night", False)
        return bool(self._state_with_pending("u_night", confirmed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_sleep_mode,
            attribute="u_night",
            value=True,
            action="enable sleep mode",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_sleep_mode,
            attribute="u_night",
            value=False,
            action="disable sleep mode",
        )


class AtmeexPowerSwitch(_BaseSwitch):
    _attr_translation_key = "power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self,
        coordinator,
        api,
        device: AtmeexDevice,
        refresh_device_cb=None,
        runtime=None,
    ) -> None:
        super().__init__(coordinator, api, device, refresh_device_cb, runtime)
        self._attr_unique_id = f"{device.id}_power"

    @property
    def is_on(self) -> bool | None:
        confirmed = self._device_state.get("pwr_on", False)
        return bool(self._state_with_pending("pwr_on", confirmed))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_power,
            attribute="pwr_on",
            value=True,
            action="turn on the device",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_boolean(
            self.api.set_power,
            attribute="pwr_on",
            value=False,
            action="turn off the device",
        )
