"""Whitelist-only diagnostics for the Atmeex Cloud integration.

Diagnostics are assembled from fresh dictionaries containing only known-safe
fields. We never copy ``entry.data``/``entry.title``, coordinator raw device
payloads, registry IDs, device names, areas, coordinates, or raw error
messages — only categories, counts, booleans, and rounded metrics.
"""
from __future__ import annotations

from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_AUTH_METHOD,
    CONF_ENABLE_CO2,
    CONF_ENABLE_WEBSOCKET,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ENABLE_CO2,
    DEFAULT_ENABLE_WEBSOCKET,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)


def _manifest_version(hass: HomeAssistant) -> str | None:
    """Best-effort integration version without importing manifest at module top."""
    try:
        integration = hass.data["integrations"][DOMAIN]  # type: ignore[index]
        return getattr(integration, "version", None)
    except Exception:  # noqa: BLE001 - version is optional context only
        return None


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _rounded(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(float(value)):
        return round(float(value), 1)
    return None


def _coordinator_snapshot(coordinator: Any) -> dict[str, Any]:
    data = getattr(coordinator, "data", None) or {}
    device_map = data.get("device_map", {}) or {}
    devices = data.get("devices", []) or []
    states = data.get("states", {}) or {}
    if isinstance(device_map, dict) and device_map:
        device_count = len(device_map)
    else:
        device_count = len(devices) if isinstance(devices, list) else 0
    last_error = getattr(coordinator, "last_api_error", None)
    return {
        "last_update_success": bool(getattr(coordinator, "last_update_success", False)),
        "device_count": device_count,
        "state_count": len(states) if isinstance(states, dict) else 0,
        "avg_latency_ms": _rounded(getattr(coordinator, "avg_latency_ms", None)),
        "request_retries": _int_or_none(getattr(coordinator, "request_retries", None)),
        "last_api_error_operation": getattr(last_error, "operation", None),
        "last_api_error_status": _int_or_none(getattr(last_error, "status", None)),
    }


def _options_snapshot(options: dict[str, Any]) -> dict[str, Any]:
    return {
        "update_interval": _int_or_none(
            options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        ),
        "enable_websocket": bool(
            options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)
        ),
        "enable_co2": bool(options.get(CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2)),
    }


def _websocket_snapshot(runtime: Any, options: dict[str, Any]) -> dict[str, Any]:
    ws_manager = getattr(runtime, "websocket_manager", None)
    ws_age = getattr(ws_manager, "last_message_age", None) if ws_manager is not None else None
    return {
        "configured": bool(options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)),
        "manager_initialized": ws_manager is not None,
        "is_connected": bool(getattr(ws_manager, "is_connected", False))
        if ws_manager is not None
        else False,
        "last_message_age_sec": _rounded(ws_age),
        "overflow_count": _int_or_none(
            getattr(runtime, "websocket_overflow_count", None)
        ),
    }


def get_diagnostics_snapshot(coordinator: Any) -> dict[str, Any]:
    """Legacy coordinator snapshot for the diagnostics sensor's attributes.

    Kept for backward compatibility with the diagnostic entity. Error strings
    are already sanitized at their source (typed operation-based messages), so
    exposing them here carries no raw payload or credential.
    """
    from datetime import datetime, timezone

    data = getattr(coordinator, "data", None) or {}
    devices = data.get("devices") or []
    last_ts = getattr(coordinator, "last_success_ts", None)
    last_error = getattr(coordinator, "last_api_error", None)

    last_success_utc: str | None = None
    if isinstance(last_ts, (int, float)):
        try:
            last_success_utc = datetime.fromtimestamp(
                last_ts, tz=timezone.utc
            ).isoformat()
        except Exception:  # pragma: no cover - defensive only
            last_success_utc = None

    return {
        "device_count": len(devices) if isinstance(devices, list) else 0,
        "last_success_ts": last_ts,
        "last_success_utc": last_success_utc,
        "last_api_error": str(last_error) if last_error is not None else None,
        "last_api_error_status": _int_or_none(getattr(last_error, "status", None)),
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Whitelist-only config-entry diagnostics."""
    runtime = entry.runtime_data
    options = dict(getattr(entry, "options", {}) or {})
    coordinator = getattr(runtime, "coordinator", None)
    api = getattr(runtime, "api", None)

    return {
        "integration": {
            "domain": DOMAIN,
            "version": _manifest_version(hass),
            "auth_method": entry.data.get(CONF_AUTH_METHOD, "email"),
        },
        "options": _options_snapshot(options),
        "coordinator": _coordinator_snapshot(coordinator),
        "api": {"has_token": bool(getattr(api, "token", "")) if api is not None else None},
        "websocket": _websocket_snapshot(runtime, options),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> dict[str, Any]:
    """Whitelist-only per-device diagnostics.

    Reports only capability/shape booleans and counters for the device — never
    its name, area, raw payload, or state values.
    """
    runtime = entry.runtime_data
    options = dict(getattr(entry, "options", {}) or {})
    coordinator = getattr(runtime, "coordinator", None)
    data = getattr(coordinator, "data", None) or {}
    states = data.get("states", {}) or {}

    atmeex_id: str | None = None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN:
            atmeex_id = str(identifier)
            break

    state = states.get(atmeex_id, {}) if atmeex_id is not None else {}
    device_map = data.get("device_map", {}) or {}
    devices = data.get("devices", []) or []
    known = atmeex_id is not None and (
        atmeex_id in device_map
        or atmeex_id in states
        or any(str(d.get("id")) == atmeex_id for d in devices if isinstance(d, dict))
    )
    return {
        "integration": {"domain": DOMAIN},
        "device": {
            "known_to_coordinator": known,
            "has_state": bool(state),
            "online": bool(state.get("online")) if isinstance(state, dict) else False,
            "has_humidifier": isinstance(state, dict)
            and ("hum_stg" in state or "no_water" in state),
        },
        "coordinator": _coordinator_snapshot(coordinator),
        "websocket": _websocket_snapshot(runtime, options),
    }
