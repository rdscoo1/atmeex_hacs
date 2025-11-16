from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN
from . import AtmeexRuntimeData, get_diagnostics_snapshot  # runtime_data + helper

# Поля, которые всегда редактируем (удаляем/маскируем) из diagnostics
TO_REDACT: set[str] = {
    CONF_EMAIL,
    CONF_PASSWORD,
    "access_token",
    "token",
    "authorization",
    "Authorization",
    "refresh_token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Вызывается Home Assistant при нажатии "Download diagnostics"
    для всей интеграции (config entry).
    """

    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    api = runtime.api

    coordinator_data: dict[str, Any] = getattr(coordinator, "data", {}) or {}

    # NEW: компактный snapshot по координатору (device_count, last_success_ts, last_api_error)
    coordinator_diag = get_diagnostics_snapshot(coordinator)

    diag: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "coordinator": {
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "last_update_success_time": getattr(
                coordinator, "last_update_success_time", None
            ),
            "data": coordinator_data,
        },
        "coordinator_diagnostics": coordinator_diag,
        "api": {
            # Только факт наличия токена, без самого токена
            "has_token": bool(getattr(api, "_token", None)) if api is not None else None,
        },
    }

    return async_redact_data(diag, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Return diagnostics for a single device."""

    runtime: AtmeexRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    coordinator_data: dict[str, Any] = getattr(coordinator, "data", {}) or {}
    devices = coordinator_data.get("devices", []) or []
    states = coordinator_data.get("states", {}) or {}

    atmeex_device_id: str | None = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            atmeex_device_id = str(identifier)
            break

    device_info = None
    device_state = None

    if atmeex_device_id is not None:
        device_info = next(
            (d for d in devices if str(d.get("id")) == atmeex_device_id),
            None,
        )
        device_state = states.get(atmeex_device_id)

    coordinator_diag = get_diagnostics_snapshot(coordinator)

    diag: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
        },
        "device_entry": {
            "id": device.id,
            "name": device.name,
            "identifiers": list(device.identifiers),
            "manufacturer": device.manufacturer,
            "model": device.model,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "area_id": device.area_id,
        },
        "device": {
            "internal_id": atmeex_device_id,
            "info": device_info,
            "state": device_state,
        },
        "coordinator_diagnostics": coordinator_diag,
    }

    return async_redact_data(diag, TO_REDACT)
