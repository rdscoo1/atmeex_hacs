from __future__ import annotations

from homeassistant.core import HomeAssistant, Event, callback

from .const import DOMAIN
from . import EVENT_API_ERROR, EVENT_DEVICE_UPDATED


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
        return {
            "name": "Atmeex Cloud",
            "message": event.data.get("message", "API error occurred"),
        }

    @callback
    def async_describe_device_updated(event: Event) -> dict[str, str]:
        """Describe device updated event."""
        device_id = event.data.get("device_id", "unknown")
        return {
            "name": "Atmeex device",
            "message": f"Device {device_id} state updated",
        }

    async_describe_event(DOMAIN, EVENT_API_ERROR, async_describe_api_error)
    async_describe_event(DOMAIN, EVENT_DEVICE_UPDATED, async_describe_device_updated)
