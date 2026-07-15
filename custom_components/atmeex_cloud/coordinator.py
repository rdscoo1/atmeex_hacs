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
    AtmeexState,
)
from .const import EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC

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
        # Always use our own _async_update_data, ignoring any external update_method.
        # External callers may still pass update_method (e.g. for DummyCoordinator compat
        # in tests) — we accept but discard it so the real coordinator always runs its
        # own typed method.
        kwargs["update_method"] = self._async_update_data
        super().__init__(hass, logger, **kwargs)
        self.last_api_error = None
        self.last_success_ts = None
        # Injected by setup_update:
        self._api: AtmeexApi | None = None
        self._fire_logbook_event: Callable[[str, dict[str, Any]], None] | None = None
        self._api_error_last_ts: float = float("-inf")
        self._api_error_suppressed: int = 0
        # Per-device monotonic timestamp of last WS state update
        self._ws_device_update_ts: dict[str, float] = {}
        # Per-device monotonic timestamp of last targeted refresh (refresh_device)
        self._refresh_device_update_ts: dict[str, float] = {}

    def setup_update(
        self,
        *,
        api: AtmeexApi,
        fire_logbook_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        """Inject dependencies needed by _async_update_data.

        Called once from async_setup_entry after the coordinator is created.
        """
        self._api = api
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

        # Record monotonic time *before* the network round-trip so we can
        # detect WS updates that arrived while the poll was in-flight.
        poll_start_mono = time.monotonic()
        start_ts = time.perf_counter()
        try:
            device_objs = await self._fetch_devices_safely()
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
        except Exception as err:
            self.last_api_error = None
            self._fire_api_error_event(
                {"message": str(err), "source": "coordinator_update"}
            )
            raise UpdateFailed(
                f"Unexpected error while updating Atmeex data: {err}"
            ) from err

        elapsed_ms = (time.perf_counter() - start_ts) * 1000.0

        if not isinstance(device_objs, list):
            raise UpdateFailed("Atmeex API returned non-list devices payload")

        # Строим карту id -> AtmeexDevice
        device_map: dict[str, AtmeexDevice] = {
            str(d.id): d for d in device_objs}

        # Для обратной совместимости (диагностика, тесты) храним ещё и "плоские" dict'ы
        devices_raw: list[dict[str, Any]] = [d.to_ha_dict() for d in device_objs]

        # Мержим с предыдущими устройствами, чтобы не терять оффлайн-девайсы
        if getattr(self, "last_update_success", False) and isinstance(
            getattr(self, "data", None), dict
        ):
            prev: AtmeexCoordinatorData = self.data
            for d_raw in prev.get("devices", []):
                did = d_raw.get("id")
                if did is None:
                    continue
                key = str(did)
                if key not in device_map:
                    try:
                        device_map[key] = AtmeexDevice.from_raw(d_raw)
                        devices_raw.append(d_raw)
                    except Exception:
                        devices_raw.append(d_raw)

        # --- строим нормализованные состояния через AtmeexState ---
        states: dict[str, dict[str, Any]] = {}
        for did, dev in device_map.items():
            try:
                ha_dict = dev.to_ha_dict()
                st = AtmeexState.from_device_dict(ha_dict)
            except Exception as e:
                _LOGGER.warning("Failed to normalize state for device %s: %s", did, e)
                continue
            states[did] = st.to_ha_dict()

        retry_count = getattr(self._api, "_retry_count", 0)

        # Preserve WS state for devices that received a fresher WebSocket
        # update while this poll was in-flight.
        cur_data = self.data
        if cur_data and isinstance(cur_data, dict):
            cur_states = cur_data.get("states") or {}
            for did, ws_ts in self._ws_device_update_ts.items():
                if ws_ts >= poll_start_mono and did in cur_states and did in states:
                    _LOGGER.debug(
                        "Preserving fresher WS state for device %s "
                        "(ws_ts=%.3f >= poll_start=%.3f)",
                        did, ws_ts, poll_start_mono,
                    )
                    states[did] = cur_states[did]

            # Preserve targeted-refresh state for devices whose per-device
            # refresh completed after this poll started.
            for did, ref_ts in self._refresh_device_update_ts.items():
                if ref_ts >= poll_start_mono and did in cur_states and did in states:
                    _LOGGER.debug(
                        "Preserving fresher targeted-refresh state for device %s "
                        "(ref_ts=%.3f >= poll_start=%.3f)",
                        did, ref_ts, poll_start_mono,
                    )
                    states[did] = cur_states[did]

        data: AtmeexCoordinatorData = {
            "devices": devices_raw,
            "device_map": device_map,
            "states": states,
            "last_success_ts": time.time(),
            "avg_latency_ms": round(elapsed_ms, 1),
            "request_retries": retry_count,
        }

        self.last_success_ts = data["last_success_ts"]
        self.last_api_error = None

        return data
