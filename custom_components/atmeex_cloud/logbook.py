from __future__ import annotations

from homeassistant.core import HomeAssistant, Event, callback

from .const import DOMAIN, EVENT_API_ERROR, EVENT_DEVICE_UPDATED


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: callback,
) -> None:
    """Описание событий для Logbook.
    
    Uses the new callback-based API for Home Assistant 2023.8+
    """

    @callback
    def async_describe_api_error(event: Event) -> dict[str, str]:
        """Describe API error event."""
        source = event.data.get("source")
        status = event.data.get("status")
        suppressed_errors = event.data.get("suppressed_errors")
        message = event.data.get("message", "API error occurred")
        if source:
            message = f"[{source}] {message}"
        if status is not None:
            message = f"{message} (status={status})"
        if isinstance(suppressed_errors, int) and suppressed_errors > 0:
            message = f"{message} (+{suppressed_errors} suppressed)"
        return {
            "name": "Atmeex Cloud",
            "message": message,
        }

    @callback
    def async_describe_device_updated(event: Event) -> dict[str, str]:
        """Describe device updated event."""
        source = event.data.get("source")
        device_id = event.data.get("device_id")
        device_ids = event.data.get("device_ids")
        suppressed_updates = event.data.get("suppressed_updates")
        if device_id is not None:
            device_name = event.data.get("device_name") or f"Device {device_id}"
            message = f"{device_name} state updated"
        elif isinstance(device_ids, list) and device_ids:
            names = event.data.get("device_names")
            if isinstance(names, list) and names:
                message = f"{', '.join(str(n) for n in names)} state updated"
            else:
                message = f"{len(device_ids)} devices state updated"
        else:
            message = "Device state updated"
        if isinstance(suppressed_updates, int) and suppressed_updates > 0:
            message = f"{message} (+{suppressed_updates} suppressed)"
        if source:
            message = f"{message} via {source}"
        return {
            "name": "Atmeex device",
            "message": message,
        }

    async_describe_event(DOMAIN, EVENT_API_ERROR, async_describe_api_error)
    # EVENT_DEVICE_UPDATED still fires on the bus (automations depend on it and
    # its payload keys), but it is intentionally NOT registered with the logbook
    # platform: routine per-poll/per-WS device updates would otherwise flood the
    # logbook/recorder with technical noise.
