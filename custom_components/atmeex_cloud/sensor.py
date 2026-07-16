from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AtmeexRuntimeData
from .api import AtmeexDevice
from .const import DOMAIN, CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2
from .diagnostics import get_diagnostics_snapshot
from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities
from .helpers import deci_to_c


@dataclass(frozen=True, slots=True)
class _SensorSpec:
    key: str
    unique_suffix: str
    device_class: SensorDeviceClass
    unit: str
    translation_key: str
    convert: Callable[[Any], Any] = lambda v: int(v) if isinstance(v, (int, float)) else None


_DEVICE_SENSOR_SPECS: tuple[_SensorSpec, ...] = (
    _SensorSpec(
        key="co2_ppm",
        unique_suffix="co2",
        device_class=SensorDeviceClass.CO2,
        unit=CONCENTRATION_PARTS_PER_MILLION,
        translation_key="co2",
    ),
    _SensorSpec(
        key="temp_in",
        unique_suffix="inlet_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="inlet_temperature",
        convert=deci_to_c,
    ),
    _SensorSpec(
        key="hum_room",
        unique_suffix="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
        translation_key="humidity",
    ),
)

# Convenient lookup by unique_suffix for backward-compatible aliases
_SPEC_BY_SUFFIX: dict[str, _SensorSpec] = {s.unique_suffix: s for s in _DEVICE_SENSOR_SPECS}


# Coordinator-driven updates; no per-entity update serialization needed.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создать сенсоры для интеграции Atmeex Cloud."""

    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    # Диагностический сенсор интеграции
    async_add_entities([AtmeexDiagnosticsSensor(runtime, entry.entry_id)])

    options = getattr(entry, "options", {}) or {}
    enable_co2 = options.get(CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2)

    def _build_entities(dev: AtmeexDevice) -> list[SensorEntity]:
        entities: list[SensorEntity] = []
        for spec in _DEVICE_SENSOR_SPECS:
            if spec.key == "co2_ppm" and not enable_co2:
                continue
            entities.append(
                AtmeexDeviceSensor(
                    coordinator=coordinator,
                    device=dev,
                    entry_id=entry.entry_id,
                    spec=spec,
                )
            )
        return entities

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        build_entities=_build_entities,
    )


class AtmeexDiagnosticsSensor(CoordinatorEntity, SensorEntity):
    """Диагностический сенсор с базовой статистикой по интеграции."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:cloud-check"
    _attr_name = "Atmeex diagnostics"
    _attr_native_unit_of_measurement = "devices"

    def __init__(self, runtime: AtmeexRuntimeData, entry_id: str) -> None:
        """Инициализация сенсора диагностики.

        Привязываемся к DataUpdateCoordinator, чтобы иметь доступ
        к его данным и диагностическим полям (last_success_ts и т.п.).
        """
        super().__init__(runtime.coordinator)
        self._entry_id = entry_id
        self._runtime = runtime

    @property
    def unique_id(self) -> str:
        """Уникальный ID сенсора внутри Home Assistant."""
        return f"{self._entry_id}_diagnostics"

    # ---------- основное значение ----------

    @property
    def native_value(self) -> int | None:
        """Вернуть количество устройств как основное значение сенсора."""
        data: dict[str, Any] = getattr(self.coordinator, "data", {}) or {}
        devices = data.get("devices") or []
        if not isinstance(devices, list):
            return None
        return len(devices)

    # ---------- дополнительные атрибуты ----------

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes."""
        attrs = get_diagnostics_snapshot(self.coordinator)

        # state_entries is sensor-specific (diagnostics snapshot doesn't include it)
        data: dict[str, Any] = getattr(self.coordinator, "data", {}) or {}
        states = data.get("states") or {}
        attrs["state_entries"] = len(states) if isinstance(states, dict) else 0

        # WebSocket fields
        ws_manager = getattr(self._runtime, "websocket_manager", None)
        ws_connected = None
        ws_last_message_age_sec = None
        if ws_manager is not None:
            ws_connected = bool(getattr(ws_manager, "is_connected", False))
            ws_age = getattr(ws_manager, "last_message_age", None)
            if isinstance(ws_age, (int, float)) and isfinite(float(ws_age)):
                ws_last_message_age_sec = round(float(ws_age), 1)

        attrs["websocket_connected"] = ws_connected
        attrs["websocket_last_message_age_sec"] = ws_last_message_age_sec
        attrs["domain"] = DOMAIN

        return attrs


class AtmeexDeviceSensor(AtmeexEntityMixin, CoordinatorEntity, SensorEntity):
    """Generic per-device sensor driven by a _SensorSpec."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
        spec: _SensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._spec = spec
        self._attr_unique_id = f"{device.id}_{spec.unique_suffix}"
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_translation_key = spec.translation_key

    @property
    def native_value(self) -> float | int | None:
        return self._spec.convert(self._device_state.get(self._spec.key))


# Backward-compatible aliases for tests and external consumers
def AtmeexCO2Sensor(coordinator, device: AtmeexDevice, entry_id: str) -> AtmeexDeviceSensor:
    """Factory alias for CO2 sensor."""
    return AtmeexDeviceSensor(coordinator, device, entry_id, _SPEC_BY_SUFFIX["co2"])


def AtmeexInletTempSensor(coordinator, device: AtmeexDevice, entry_id: str) -> AtmeexDeviceSensor:
    """Factory alias for inlet temperature sensor."""
    return AtmeexDeviceSensor(coordinator, device, entry_id, _SPEC_BY_SUFFIX["inlet_temp"])



def AtmeexHumiditySensor(coordinator, device: AtmeexDevice, entry_id: str) -> AtmeexDeviceSensor:
    """Factory alias for humidity sensor."""
    return AtmeexDeviceSensor(coordinator, device, entry_id, _SPEC_BY_SUFFIX["humidity"])
