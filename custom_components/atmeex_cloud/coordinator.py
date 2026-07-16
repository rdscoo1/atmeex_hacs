"""DataUpdateCoordinator subclass for Atmeex Cloud integration."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ApiError,
    AtmeexApi,
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
    AtmeexRateLimitError,
)
from .const import EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC
from .state_store import AtmeexStateStore


class AtmeexCoordinatorData(TypedDict):
    """Comparable state snapshot stored by the coordinator."""

    devices: list[dict[str, Any]]
    device_map: dict[str, AtmeexDevice]
    states: dict[str, dict[str, Any]]


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    """Coordinator with typed diagnostic attributes.

    Replaces ad-hoc ``setattr`` on a plain DataUpdateCoordinator with real
    instance attributes that are discoverable by type-checkers and IDE
    autocompletion.
    """

    last_api_error: ApiError | None
    last_success_ts: float | None
    avg_latency_ms: float | None
    request_retries: int

    def __init__(self, hass: HomeAssistant, logger: logging.Logger, **kwargs: Any) -> None:
        # Always use our own _async_update_data, ignoring any external update_method.
        # External callers may still pass update_method (e.g. for DummyCoordinator compat
        # in tests) — we accept but discard it so the real coordinator always runs its
        # own typed method.
        kwargs["update_method"] = self._async_update_data
        kwargs["always_update"] = False
        super().__init__(hass, logger, **kwargs)
        self.last_api_error = None
        self.last_success_ts = None
        self.avg_latency_ms = None
        self.request_retries = 0
        # Injected by setup_update:
        self._api: AtmeexApi | None = None
        self.state_store: AtmeexStateStore | None = None
        self._fire_logbook_event: Callable[[str, dict[str, Any]], None] | None = None
        self._api_error_last_ts: float = float("-inf")
        self._api_error_suppressed: int = 0

    def setup_update(
        self,
        *,
        api: AtmeexApi,
        fire_logbook_event: Callable[[str, dict[str, Any]], None],
        state_store: AtmeexStateStore | None = None,
    ) -> None:
        """Inject dependencies needed by _async_update_data.

        Called once from async_setup_entry after the coordinator is created.

        ``state_store`` is temporarily optional while callers migrate to the
        entry-owned store; omitting it creates a coordinator-owned store.
        """
        self._api = api
        # Public because the composition root and command executor share this
        # same entry-owned store.
        self.state_store = (
            state_store if state_store is not None else AtmeexStateStore()
        )
        self._fire_logbook_event = fire_logbook_event

    def _fire_api_error_event(self, data: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - self._api_error_last_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            self._api_error_suppressed += 1
            return
        if self._api_error_suppressed:
            data = {**data, "suppressed_errors": self._api_error_suppressed}
            self._api_error_suppressed = 0
        if self._fire_logbook_event is not None:
            self._fire_logbook_event(EVENT_API_ERROR, data)
        self._api_error_last_ts = now

    async def _fetch_devices_safely(self) -> list[AtmeexDevice]:
        """Fetch one authoritative inventory and hydrate each listed device."""
        api = self._api
        if api is None:
            raise AtmeexProtocolError(
                "get_devices", "coordinator API is not configured"
            )
        devices = await api.get_devices()
        hydrated: list[AtmeexDevice] = []
        for device in devices:
            try:
                hydrated.append(await api.get_device(device.id))
            except AtmeexAuthenticationError:
                raise
            except (
                AtmeexConnectionError,
                AtmeexRateLimitError,
                AtmeexProtocolError,
            ):
                hydrated.append(device)

        return hydrated

    async def _async_update_data(self) -> AtmeexCoordinatorData:
        """Плановый опрос: тянем устройства, при ошибке кидаем UpdateFailed / AuthFailed."""

        if not hasattr(self, "state_store"):
            # Temporary compatibility for coordinator-like test/migration
            # adapters that bind this method without calling setup_update().
            self.state_store = AtmeexStateStore()
        state_store = self.state_store
        if state_store is None:
            raise UpdateFailed("Atmeex state store is not configured")
        baselines = state_store.capture_all()
        try:
            start_ts = time.perf_counter()
            device_objs = await self._fetch_devices_safely()
            update = state_store.apply_inventory(device_objs, baselines)
            elapsed_ms = (time.perf_counter() - start_ts) * 1000.0
        except AtmeexAuthenticationError as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {
                    "message": str(err),
                    "status": err.status,
                    "source": "coordinator_update",
                }
            )
            raise ConfigEntryAuthFailed("Atmeex authentication failed") from err
        except (
            AtmeexConnectionError,
            AtmeexRateLimitError,
            AtmeexProtocolError,
        ) as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {
                    "message": str(err),
                    "status": err.status,
                    "source": "coordinator_update",
                }
            )
            raise UpdateFailed("Atmeex API update failed") from err
        self.last_success_ts = time.time()
        self.avg_latency_ms = round(elapsed_ms, 1)
        self.request_retries = getattr(self._api, "_retry_count", 0)
        self.last_api_error = None
        return update.data
