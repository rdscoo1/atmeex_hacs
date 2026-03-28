"""DataUpdateCoordinator subclass for Atmeex Cloud integration."""
from __future__ import annotations

import logging
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import ApiError, AtmeexDevice

_LOGGER = logging.getLogger(__name__)


class AtmeexCoordinatorData(TypedDict, total=False):
    """Data structure stored by the coordinator."""

    devices: list[dict[str, Any]]
    device_map: dict[str, AtmeexDevice]
    states: dict[str, dict[str, Any]]
    last_success_ts: float | None
    avg_latency_ms: float | None
    request_retries: int


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    """Coordinator with typed diagnostic attributes.

    Replaces ad-hoc ``setattr`` on a plain DataUpdateCoordinator with real
    instance attributes that are discoverable by type-checkers and IDE
    autocompletion.
    """

    last_api_error: ApiError | None
    last_success_ts: float | None

    def __init__(self, hass: HomeAssistant, logger: logging.Logger, **kwargs: Any) -> None:
        super().__init__(hass, logger, **kwargs)
        self.last_api_error = None
        self.last_success_ts = None
