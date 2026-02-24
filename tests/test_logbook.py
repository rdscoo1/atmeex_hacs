from types import SimpleNamespace

from custom_components.atmeex_cloud import EVENT_API_ERROR, EVENT_DEVICE_UPDATED
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.logbook import async_describe_events


def test_logbook_registers_event_describers() -> None:
    registered: list[tuple[str, str, object]] = []

    def _register(domain: str, event_type: str, describer) -> None:
        registered.append((domain, event_type, describer))

    async_describe_events(SimpleNamespace(), _register)

    assert len(registered) == 2
    assert registered[0][0] == DOMAIN
    assert registered[0][1] == EVENT_API_ERROR
    assert registered[1][0] == DOMAIN
    assert registered[1][1] == EVENT_DEVICE_UPDATED


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
            }
        )
    )
    assert api_result["name"] == "Atmeex Cloud"
    assert api_result["message"] == "[coordinator_update] Timeout while calling API (status=504)"

    device_result = describers[EVENT_DEVICE_UPDATED](
        SimpleNamespace(
            data={
                "device_ids": ["1", "2"],
                "source": "websocket",
                "suppressed_updates": 3,
            }
        )
    )
    assert device_result["name"] == "Atmeex device"
    assert device_result["message"] == "2 devices state updated (+3 suppressed) via websocket"
