"""Authoritative inventory coordinator for Atmeex Cloud."""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import timedelta
from typing import Any, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    AtmeexApi,
    AtmeexApiError,
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
    AtmeexRateLimitError,
)
from .const import DOMAIN, EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC
from .helpers import normalize_device_id, parse_atmeex_bool
from .state_store import AtmeexStateStore


_MAX_DETAIL_CONCURRENCY = 3


def _valid_list_section_field(
    section_name: str,
    field_name: str,
    value: Any,
) -> bool:
    """Return whether one list core field is safe to overlay on detail."""
    if (section_name, field_name) in {
        ("condition", "pwr_on"),
        ("settings", "u_pwr_on"),
    }:
        try:
            parse_atmeex_bool(value)
        except ValueError:
            return False
    if (section_name, field_name) in {
        ("condition", "fan_speed"),
        ("settings", "u_fan_speed"),
    }:
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= 6
        )
    return True


class AtmeexCoordinatorData(TypedDict):
    """Comparable device snapshot published to Home Assistant."""

    devices: list[dict[str, Any]]
    device_map: dict[str, AtmeexDevice]
    states: dict[str, dict[str, Any]]


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    """Fetch authoritative inventory and publish state-store snapshots."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        api: AtmeexApi,
        state_store: AtmeexStateStore,
        config_entry_id: str,
        config_entry: ConfigEntry | None = None,
        name: str,
        update_interval: timedelta,
        fire_logbook_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        coordinator_kwargs: dict[str, Any] = {}
        if (
            "config_entry"
            in inspect.signature(DataUpdateCoordinator.__init__).parameters
        ):
            # Passing None explicitly prevents current Home Assistant releases
            # from implicitly capturing an unrelated current-entry context.
            coordinator_kwargs["config_entry"] = config_entry
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            update_method=self._async_update_data,
            always_update=False,
            **coordinator_kwargs,
        )
        self.api = api
        self.state_store = state_store
        self.config_entry_id = config_entry_id
        self.last_api_error: AtmeexApiError | None = None
        self.last_success_ts: float | None = None
        self.last_inventory_success_mono: float | None = None
        self.avg_latency_ms: float | None = None
        self.request_retries = 0
        self._max_inventory_age_seconds = update_interval.total_seconds()
        self._inventory_refresh_lock = asyncio.Lock()
        self._fire_logbook_event = fire_logbook_event
        self._api_error_last_ts = float("-inf")
        self._api_error_suppressed = 0
        self._last_detail_error: AtmeexApiError | None = None
        self._last_detail_failure_count = 0

    def _fire_api_error_event(self, data: dict[str, Any]) -> None:
        """Emit one throttled API-error event without masking the API error."""
        now = time.monotonic()
        if now - self._api_error_last_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            self._api_error_suppressed += 1
            return
        if self._api_error_suppressed:
            data = {**data, "suppressed_errors": self._api_error_suppressed}
            self._api_error_suppressed = 0
        if self._fire_logbook_event is not None:
            try:
                self._fire_logbook_event(EVENT_API_ERROR, data)
            except Exception as err:
                self.logger.warning(
                    "Atmeex API-error event publication failed: %s",
                    type(err).__name__,
                )
        self._api_error_last_ts = now

    @staticmethod
    def _safe_error_event(err: AtmeexApiError) -> dict[str, Any]:
        """Build the fixed privacy-safe event payload for one API failure."""
        return {
            "message": f"{err.operation} failed",
            "operation": err.operation,
            "status": err.status,
            "error_type": type(err).__name__,
            "source": "coordinator_update",
        }

    @staticmethod
    def _safe_detail_error(err: AtmeexApiError) -> AtmeexApiError:
        """Copy a detail failure without retaining a server/client message."""
        if isinstance(err, AtmeexRateLimitError):
            return AtmeexRateLimitError(
                err.operation,
                "detail hydration unavailable",
                status=err.status,
                retry_after=err.retry_after,
            )
        return type(err)(
            err.operation,
            "detail hydration unavailable",
            status=err.status,
        )

    @staticmethod
    def _needs_detail(device: AtmeexDevice) -> bool:
        """Return whether a list item lacks core device state.

        Presence-based, matching the plan contract: a list item is complete
        when it carries condition/settings sections and both a power and a fan
        field. Field *validity* is not checked here — an invalid literal must
        surface through state normalization (as a truthful UpdateFailed), not be
        masked by a detail fetch.
        """
        if not device.condition_present or not device.settings_present:
            return True

        condition = device.condition
        settings = device.settings
        has_power = "pwr_on" in condition or "u_pwr_on" in settings
        has_fan = "fan_speed" in condition or "u_fan_speed" in settings
        return not (has_power and has_fan)

    @staticmethod
    def _merge_detail_source(
        listed: AtmeexDevice,
        detail_source: AtmeexDevice,
    ) -> AtmeexDevice:
        """Overlay authoritative list fields on confirmed detail sections."""
        merged = deepcopy(detail_source.to_ha_dict())
        listed_data = listed.to_ha_dict()
        for key, value in listed_data.items():
            if key in ("condition", "settings"):
                continue
            merged[key] = deepcopy(value)
        for section_name in ("condition", "settings"):
            section = dict(merged.get(section_name, {}))
            for field_name, value in listed.raw.get(
                section_name,
                {},
            ).items():
                if _valid_list_section_field(
                    section_name,
                    field_name,
                    value,
                ):
                    section[field_name] = deepcopy(value)
            merged[section_name] = section
        return AtmeexDevice.from_raw(merged)

    def _previous_device(
        self,
        listed: AtmeexDevice,
    ) -> AtmeexDevice | None:
        key = normalize_device_id(listed.id)
        return self.state_store.data.get("device_map", {}).get(key)

    async def _hydrate_one(
        self,
        listed: AtmeexDevice,
    ) -> tuple[AtmeexDevice, AtmeexApiError | None]:
        """Hydrate one incomplete list item or reuse confirmed prior detail."""
        try:
            detailed = await self.api.get_device(listed.id)
            if normalize_device_id(detailed.id) != normalize_device_id(listed.id):
                raise AtmeexProtocolError(
                    "get_device",
                    "device id does not match request",
                )
            return self._merge_detail_source(listed, detailed), None
        except AtmeexAuthenticationError:
            raise
        except (
            AtmeexConnectionError,
            AtmeexRateLimitError,
            AtmeexProtocolError,
        ) as err:
            previous = self._previous_device(listed)
            if previous is None:
                raise
            return self._merge_detail_source(listed, previous), err

    @staticmethod
    def _exception_leaves(error: BaseException) -> list[Exception]:
        """Flatten TaskGroup failures so coordinator mappings see bare errors."""
        if isinstance(error, BaseExceptionGroup):
            leaves: list[Exception] = []
            for nested in error.exceptions:
                leaves.extend(AtmeexCoordinator._exception_leaves(nested))
            return leaves
        return [error] if isinstance(error, Exception) else []

    async def _hydrate_devices(
        self,
        devices: list[AtmeexDevice],
    ) -> list[AtmeexDevice]:
        """Hydrate incomplete devices through at most three worker tasks."""
        self._last_detail_error = None
        self._last_detail_failure_count = 0
        incomplete = deque(
            index
            for index, device in enumerate(devices)
            if self._needs_detail(device)
        )
        if not incomplete:
            return devices

        results = list(devices)
        failures: list[AtmeexApiError | None] = [None] * len(devices)

        async def worker() -> None:
            while incomplete:
                index = incomplete.popleft()
                results[index], failures[index] = await self._hydrate_one(
                    devices[index]
                )

        caught: BaseExceptionGroup[Exception] | None = None
        try:
            async with asyncio.TaskGroup() as group:
                for _index in range(
                    min(_MAX_DETAIL_CONCURRENCY, len(incomplete))
                ):
                    group.create_task(worker())
        except* Exception as error_group:
            caught = error_group

        if caught is not None:
            leaves = self._exception_leaves(caught)
            authentication_error = next(
                (
                    error
                    for error in leaves
                    if isinstance(error, AtmeexAuthenticationError)
                ),
                None,
            )
            raise authentication_error or leaves[0]

        detail_failures = [error for error in failures if error is not None]
        if detail_failures:
            self._last_detail_error = self._safe_detail_error(
                detail_failures[0]
            )
            self._last_detail_failure_count = len(detail_failures)
        return results

    def _remove_confirmed_stale_devices(
        self,
        removed_device_ids: frozenset[str],
    ) -> None:
        """Drop this entry's association from devices confirmed absent.

        Home Assistant purges a device once no config entry references it. The
        store only reports an ID here after two consecutive successful
        authoritative polls saw it absent, so a transient outage cannot retire
        a device.
        """
        if not removed_device_ids:
            return
        hass = getattr(self, "hass", None)
        if hass is None:
            return
        try:
            registry = dr.async_get(hass)
            stale_entries = dr.async_entries_for_config_entry(
                registry,
                self.config_entry_id,
            )
        except Exception:  # noqa: BLE001 - registry cleanup is best-effort
            # Never let device-registry housekeeping break a successful poll
            # (e.g. under a minimal test hass with no registry).
            self.logger.debug(
                "Skipping stale-device cleanup: registry unavailable"
            )
            return
        for device_entry in stale_entries:
            atmeex_ids = {
                str(identifier)
                for domain, identifier in device_entry.identifiers
                if domain == DOMAIN
            }
            if atmeex_ids.isdisjoint(removed_device_ids):
                continue
            registry.async_update_device(
                device_entry.id,
                remove_config_entry_id=self.config_entry_id,
            )

    async def async_ensure_inventory_fresh(
        self,
        *,
        now_mono: float | None = None,
    ) -> bool:
        """Force an authoritative refresh if the inventory is older than the age cap.

        Continuous WebSocket push traffic keeps device *state* fresh without any
        authoritative ``/devices`` poll, so a removed or renamed device could
        otherwise linger indefinitely. Returns True when a refresh was requested.
        """
        now = time.monotonic() if now_mono is None else now_mono
        last_success = self.last_inventory_success_mono
        if (
            last_success is not None
            and now - last_success < self._max_inventory_age_seconds
        ):
            return False
        await self.async_request_refresh()
        return True

    async def async_inventory_watchdog(
        self,
        check_interval: float | None = None,
    ) -> None:
        """Entry-owned loop that enforces the maximum inventory age.

        Cancelled by runtime cleanup on unload; no internal stop flag needed.
        """
        interval = (
            check_interval
            if check_interval is not None
            else max(self._max_inventory_age_seconds / 2, 1.0)
        )
        while True:
            await asyncio.sleep(interval)
            await self.async_ensure_inventory_fresh()

    async def _async_update_data(self) -> AtmeexCoordinatorData:
        """Fetch one authoritative inventory or report a truthful failure."""
        baselines = self.state_store.capture_all()
        started = time.perf_counter()
        self._last_detail_error = None
        self._last_detail_failure_count = 0
        try:
            listed_devices = await self.api.get_devices()
            hydrated_devices = await self._hydrate_devices(listed_devices)
            update = self.state_store.apply_inventory(
                hydrated_devices,
                baselines,
            )
        except AtmeexAuthenticationError as err:
            self.last_api_error = err
            self._fire_api_error_event(self._safe_error_event(err))
            raise ConfigEntryAuthFailed(
                f"{err.operation} failed"
            ) from err
        except (
            AtmeexConnectionError,
            AtmeexRateLimitError,
            AtmeexProtocolError,
        ) as err:
            self.last_api_error = err
            self._fire_api_error_event(self._safe_error_event(err))
            raise UpdateFailed(f"{err.operation} failed") from err

        # Disassociate only devices the store confirmed absent across two
        # consecutive successful authoritative polls. This is below the typed
        # exception handlers, so a failed inventory can never retire a device.
        self._remove_confirmed_stale_devices(update.removed_device_ids)

        self.avg_latency_ms = round(
            (time.perf_counter() - started) * 1000.0,
            1,
        )
        retry_count = getattr(self.api, "retry_count", 0)
        self.request_retries = retry_count if isinstance(retry_count, int) else 0
        self.last_success_ts = time.time()
        self.last_inventory_success_mono = time.monotonic()
        self.last_api_error = self._last_detail_error
        if self._last_detail_error is not None:
            event = self._safe_error_event(self._last_detail_error)
            event["source"] = "coordinator_detail_hydration"
            event["detail_failure_count"] = self._last_detail_failure_count
            self._fire_api_error_event(event)
        return update.data
