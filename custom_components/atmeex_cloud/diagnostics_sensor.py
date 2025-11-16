from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory

from . import AtmeexRuntimeData
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up diagnostics sensor from a config entry."""

    # runtime_data мы уже кладём в entry.runtime_data в __init__.py
    runtime: AtmeexRuntimeData = entry.runtime_data  # type: ignore[assignment]

    # Если по каким-то причинам runtime нет — ничего не создаём.
    if runtime is None:
        return

    # Оборачиваем в список — HA сам разберётся.
    async_add_entities([AtmeexDiagnosticsSensor(runtime, entry.entry_id)])


class AtmeexDiagnosticsSensor(SensorEntity):
    """Diagnostic sensor exposing basic Atmeex integration stats."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, runtime: AtmeexRuntimeData, entry_id: str) -> None:
        self._runtime = runtime
        self._entry_id = entry_id
        self._attr_name = "Atmeex diagnostics"
        self._attr_unique_id = f"{entry_id}_diagnostics"

    # ---- основное значение ----

    @property
    def native_value(self) -> int | None:
        """Return number of devices as main value."""
        coord = self._runtime.coordinator
        data: dict[str, Any] = getattr(coord, "data", {}) or {}
        devices = data.get("devices") or []
        if not isinstance(devices, list):
            return None
        return len(devices)

    # ---- атрибуты ----

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        coord = self._runtime.coordinator

        data: dict[str, Any] = getattr(coord, "data", {}) or {}
        devices = data.get("devices") or []
        states = data.get("states") or {}

        # Берём timestamp и последнее сообщение об ошибке
        last_success_ts = getattr(coord, "last_success_ts", None)
        last_api_error = getattr(coord, "last_api_error", None)

        # Удобное представление времени в UTC
        if isinstance(last_success_ts, (int, float)):
            last_success_utc = (
                datetime.fromtimestamp(last_success_ts, tz=timezone.utc).isoformat()
            )
        else:
            last_success_utc = None

        return {
            "device_count": len(devices) if isinstance(devices, list) else 0,
            "state_entries": len(states) if isinstance(states, dict) else 0,
            "last_success_ts": last_success_ts,
            "last_success_utc": last_success_utc,
            "last_api_error": last_api_error,
        }

    # ---- device_info, чтобы сенсор привязался к интеграции ----

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self._entry_id}_diagnostics")},
            "name": "Atmeex Cloud diagnostics",
            "manufacturer": "Atmeex",
        }
