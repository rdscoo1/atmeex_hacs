from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
from .const import DOMAIN
from .entity_base import AtmeexEntityMixin
from .helpers import deci_to_c


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создать сенсоры для интеграции Atmeex Cloud."""

    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    if runtime is None:
        return

    entities: list[SensorEntity] = []

    # Диагностический сенсор интеграции
    entities.append(AtmeexDiagnosticsSensor(runtime, entry.entry_id))

    # Сенсоры для каждого устройства
    data = coordinator.data or {}
    device_map: dict[str, AtmeexDevice] = data.get("device_map", {}) or {}

    for dev in device_map.values():
        # CO2 sensor
        entities.append(
            AtmeexCO2Sensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )
        # Inlet temperature sensor
        entities.append(
            AtmeexInletTempSensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )
        # Humidity sensor
        entities.append(
            AtmeexHumiditySensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
            )
        )

    if entities:
        async_add_entities(entities)


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
        """Вернуть дополнительные диагностические атрибуты."""
        data: dict[str, Any] = getattr(self.coordinator, "data", {}) or {}
        devices = data.get("devices") or []
        states = data.get("states") or {}

        last_success_ts = data.get("last_success_ts")
        last_api_error = data.get("last_api_error")

        if isinstance(last_success_ts, (int, float)):
            last_success_utc = datetime.fromtimestamp(
                last_success_ts,
                tz=timezone.utc,
            ).isoformat()
        else:
            last_success_utc = None

        return {
            "device_count": len(devices) if isinstance(devices, list) else 0,
            "state_entries": len(states) if isinstance(states, dict) else 0,
            "last_success_ts": last_success_ts,
            "last_success_utc": last_success_utc,
            "last_api_error": last_api_error,
            "domain": DOMAIN,
        }


class AtmeexCO2Sensor(AtmeexEntityMixin, CoordinatorEntity, SensorEntity):
    """Сенсор уровня CO2 в помещении."""

    _attr_device_class = SensorDeviceClass.CO2
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_has_entity_name = True
    _attr_translation_key = "co2"

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
    ) -> None:
        """Инициализация сенсора CO2."""
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._attr_unique_id = f"{device.id}_co2"

    @property
    def native_value(self) -> int | None:
        """Вернуть уровень CO2 в ppm."""
        val = self._device_state.get("co2_ppm")
        return int(val) if isinstance(val, (int, float)) else None


class AtmeexInletTempSensor(AtmeexEntityMixin, CoordinatorEntity, SensorEntity):
    """Сенсор температуры входящего воздуха."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_has_entity_name = True
    _attr_translation_key = "inlet_temperature"

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
    ) -> None:
        """Инициализация сенсора температуры входящего воздуха."""
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._attr_unique_id = f"{device.id}_inlet_temp"

    @property
    def native_value(self) -> float | None:
        """Вернуть температуру входящего воздуха в °C."""
        return deci_to_c(self._device_state.get("temp_in"))


class AtmeexHumiditySensor(AtmeexEntityMixin, CoordinatorEntity, SensorEntity):
    """Сенсор влажности в помещении."""

    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_has_entity_name = True
    _attr_translation_key = "humidity"

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
    ) -> None:
        """Инициализация сенсора влажности."""
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._attr_unique_id = f"{device.id}_humidity"

    @property
    def native_value(self) -> int | None:
        """Вернуть влажность в %."""
        val = self._device_state.get("hum_room")
        return int(val) if isinstance(val, (int, float)) else None
