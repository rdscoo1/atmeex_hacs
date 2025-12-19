from __future__ import annotations

from homeassistant.components import logbook
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from . import EVENT_API_ERROR, EVENT_DEVICE_UPDATED  # из __init__.py


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event):
    """Описание событий для Logbook."""
    async_describe_event(
        DOMAIN,
        EVENT_API_ERROR,
        logbook.LogbookEntry(
            name="Atmeex Cloud",
            message="{{ message }}",
            domain=DOMAIN,
        ),
    )

    async_describe_event(
        DOMAIN,
        EVENT_DEVICE_UPDATED,
        logbook.LogbookEntry(
            name="Atmeex device",
            message="Device {{ device_id }} state updated",
            domain=DOMAIN,
        ),
    )
