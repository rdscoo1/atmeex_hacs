from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import AtmeexRuntimeData
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Создать диагностический сенсор для интеграции Atmeex Cloud."""

    # runtime_data мы уже кладём в entry.runtime_data в __init__.py
    runtime: AtmeexRuntimeData = entry.runtime_data  # type: ignore[assignment]

    # Если по каким-то причинам runtime нет — просто выходим.
    if runtime is None:
        return

    # Создаём один сенсор диагностики на уровень интеграции.
    async_add_entities([AtmeexDiagnosticsSensor(runtime, entry.entry_id)])


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
