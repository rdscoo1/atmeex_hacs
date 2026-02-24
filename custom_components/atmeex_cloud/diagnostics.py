from __future__ import annotations

from math import isfinite
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN, CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET
from . import AtmeexRuntimeData, AtmeexCoordinatorData

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


def get_diagnostics_snapshot(
    coordinator: DataUpdateCoordinator[AtmeexCoordinatorData],
) -> dict[str, Any]:
    """Компактный snapshot для диагностики (entities / diagnostics UI).

    Возвращает:
    - количество устройств;
    - timestamp последнего успешного обновления (raw + ISO-строка);
    - последнее сообщение об ошибке API (если есть).
    """
    data: AtmeexCoordinatorData = getattr(coordinator, "data", None) or {
        "devices": [],
        "states": {},
    }
    devices = data.get("devices") or []

    # Метаданные лежат как атрибуты координатора (см. DummyCoordinator в тестах)
    last_ts = getattr(coordinator, "last_success_ts", None)
    last_error = getattr(coordinator, "last_api_error", None)

    # Читаемый ISO-формат времени последнего успеха
    last_success_utc: str | None = None
    if isinstance(last_ts, (int, float)):
        from datetime import datetime, timezone

        try:
            last_success_utc = datetime.fromtimestamp(
                last_ts, tz=timezone.utc
            ).isoformat()
        except Exception:  # pragma: no cover — сильно защитный код
            last_success_utc = None

    return {
        "device_count": len(devices),
        "last_success_ts": last_ts,
        "last_success_utc": last_success_utc,
        "last_api_error": last_error,
    }


def _get_websocket_snapshot(runtime: AtmeexRuntimeData, options: dict[str, Any]) -> dict[str, Any]:
    """Return websocket diagnostics snapshot."""
    ws_manager = getattr(runtime, "websocket_manager", None)
    configured = bool(options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET))
    ws_age = getattr(ws_manager, "last_message_age", None) if ws_manager is not None else None

    age_seconds: float | None = None
    if isinstance(ws_age, (int, float)) and isfinite(float(ws_age)):
        age_seconds = round(float(ws_age), 1)

    return {
        "configured": configured,
        "manager_initialized": ws_manager is not None,
        "is_connected": bool(getattr(ws_manager, "is_connected", False))
        if ws_manager is not None
        else False,
        "last_message_age_sec": age_seconds,
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

    # компактный snapshot по координатору (device_count, last_success_ts, last_api_error)
    coordinator_diag = get_diagnostics_snapshot(coordinator)

    options = dict(entry.options)
    diag: dict[str, Any] = {
        "entry": {
            "title": entry.title,
            "data": dict(entry.data),
            "options": options,
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
        "websocket": _get_websocket_snapshot(runtime, options),
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
    options = dict(entry.options)
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
        "websocket": _get_websocket_snapshot(runtime, options),
    }

    return async_redact_data(diag, TO_REDACT)
