from types import SimpleNamespace

from custom_components.atmeex_cloud import EVENT_API_ERROR, EVENT_DEVICE_UPDATED
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.logbook import async_describe_events


def test_logbook_registers_event_describers() -> None:
    registered: list[tuple[str, str, object]] = []

    def _register(domain: str, event_type: str, describer) -> None:
        registered.append((domain, event_type, describer))

    async_describe_events(SimpleNamespace(), _register)

    # Only the API-error event is described in the logbook; routine device
    # updates still fire on the bus but are no longer logbook/recorder noise.
    assert len(registered) == 1
    assert registered[0][0] == DOMAIN
    assert registered[0][1] == EVENT_API_ERROR
    assert all(event_type != EVENT_DEVICE_UPDATED for _, event_type, _ in registered)


def test_logbook_describers_format_messages() -> None:
    registered: list[tuple[str, str, object]] = []

    def _register(domain: str, event_type: str, describer) -> None:
        registered.append((domain, event_type, describer))

    async_describe_events(SimpleNamespace(), _register)
    describers = {event_type: describer for _, event_type, describer in registered}

    api_result = describers[EVENT_API_ERROR](
        SimpleNamespace(
            data={
                "message": "Timeout while calling API",
                "source": "coordinator_update",
                "status": 504,
                "suppressed_errors": 2,
            }
        )
    )
    assert api_result["name"] == "Atmeex Cloud"
    assert (
        api_result["message"]
        == "[coordinator_update] Timeout while calling API (status=504) (+2 suppressed)"
    )

    # EVENT_DEVICE_UPDATED is no longer registered with the logbook platform.
    assert EVENT_DEVICE_UPDATED not in describers
