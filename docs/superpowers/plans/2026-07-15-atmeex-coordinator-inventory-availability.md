# Atmeex Coordinator, Inventory, and Availability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make authoritative inventory refreshes truthful, bounded, age-enforced, and capability-driven while preserving every existing Atmeex entity unique ID and Home Assistant automation input.

**Architecture:** `AtmeexCoordinator` performs one authoritative list request, hydrates only incomplete devices with a concurrency limit of three, and publishes comparable snapshots exclusively through the existing `AtmeexStateStore` contract. The store owns the two-success absence counter; the coordinator owns refresh timing, Home Assistant error mapping, stale device-registry association removal, and the maximum-inventory-age deadline. Entity discovery records device/capability keys before invoking factories so unchanged updates neither reconstruct entities nor notify listeners.

**Tech Stack:** Python 3.12+, Home Assistant `DataUpdateCoordinator`, `asyncio.TaskGroup`/`Semaphore`, pytest, pytest-asyncio, pytest-homeassistant-custom-component, `unittest.mock`.

---

## File Responsibility Map

### Production files

- Modify: `custom_components/atmeex_cloud/coordinator.py` — authoritative inventory fetch, typed failure mapping, detail hydration, comparable publication, inventory deadline, and confirmed-stale registry cleanup.
- Modify: `custom_components/atmeex_cloud/entity_base.py` — coordinator-aware availability, capability predicates, and factory-based dynamic entity discovery.
- Modify: `custom_components/atmeex_cloud/__init__.py` — construct the coordinator with the state store and config-entry identity, start the entry-owned inventory watchdog after platform setup, and make manual device removal authoritative-inventory aware.
- Modify: `custom_components/atmeex_cloud/runtime.py` — retain the typed optional `inventory_watchdog_task` field introduced by the lifecycle plan; no second task registry is added.
- Modify: `custom_components/atmeex_cloud/climate.py` — discover the existing climate unique ID without reconstructing known entities.
- Modify: `custom_components/atmeex_cloud/fan.py` — discover the existing fan unique ID without reconstructing known entities.
- Modify: `custom_components/atmeex_cloud/select.py` — discover the existing breezer select always and the humidifier select only after capability evidence.
- Modify: `custom_components/atmeex_cloud/switch.py` — discover the three existing switch unique IDs without reconstructing known entities.
- Modify: `custom_components/atmeex_cloud/sensor.py` — create sensor entities only for reported or advertised fields while preserving suffixes and the account-wide CO2 option.
- Modify: `custom_components/atmeex_cloud/binary_sensor.py` — keep connectivity present for each device and add `no_water` only after humidifier capability evidence.

### Test files

- Create: `tests/test_inventory_semantics.py` — valid-empty, outage/auth failure, unchanged publication, and timing-property behavior.
- Create: `tests/test_detail_hydration.py` — detail skipping, concurrency bound, partial-detail preservation, and authentication abort.
- Create: `tests/test_inventory_removal.py` — two-success absence, failed-cycle behavior, registry disassociation, and manual removal policy.
- Create: `tests/test_inventory_age.py` — maximum age boundary, push-update independence, watchdog loop, and task ownership.
- Create: `tests/test_entity_availability.py` — coordinator/device/online availability truth table, including connectivity sensor semantics.
- Create: `tests/test_entity_discovery.py` — construction-once behavior and newly discovered device/capability keys.
- Modify: `tests/test_sensor.py` — capability-driven CO2/temperature/humidity construction using unchanged unique IDs.
- Modify: `tests/test_binary_sensor.py` — coordinator-aware online availability and one-time humidifier entity discovery.
- Modify: `tests/test_coordinator.py` — remove fallback/whole-device timestamp expectations superseded by the state store and retain throttling coverage.
- Modify: `tests/test_setup.py` — use the real coordinator constructor contract and assert post-platform watchdog startup.
- Modify: `tests/test_unload.py` — assert active devices cannot be manually removed and confirmed-absent devices can.

## Fixed Dependency Contracts

Plans 1–4 must be implemented first. This plan uses these names and signatures exactly:

- API errors use `AtmeexApiError(operation: str, message: str, *, status: int | None = None)` and the subclasses `AtmeexAuthenticationError`, `AtmeexConnectionError`, `AtmeexProtocolError`, and `AtmeexRateLimitError`; the rate-limit subtype adds `retry_after: float | None`.
- `FieldRevisionBaseline` is frozen and contains `device_id: str` plus `revisions: Mapping[str, int]`.
- `StateStoreUpdate` is frozen and contains `data: AtmeexCoordinatorData`, `changed: bool`, and `removed_device_ids: frozenset[str] = frozenset()`.
- `AtmeexStateStore(initial: AtmeexCoordinatorData | None = None)` exposes `data`, `capture_all()`, `apply_websocket_delta(device_id, *, state_delta, device_delta=None)`, `apply_refresh(device, baseline)`, and `apply_inventory(devices, baselines)`; every apply method returns `StateStoreUpdate`.
- `AtmeexRuntimeData` has typed `api`, `coordinator`, `state_store`, `command_executor`, `refresh_device`, `stopping`, `tasks`, and optional named task fields including `inventory_watchdog_task`; `track_task(task)` returns the same owned task and removes it from the set when complete.
- `AtmeexCommandExecutor.allow_recovery_confirmation(device_id)` is called only after an authoritative full recovery succeeds; queued/unexecuted generations remain non-confirmable.
- `compat.async_create_background_task(hass, coro, name)` returns the Home Assistant-owned `asyncio.Task` selected through feature detection.

## Execution Gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require all
tests to pass with no runtime or pending-task warning. The only remaining
baseline notice at this stage is the pytest-asyncio loop-scope configuration
assigned to Plan 6; no inventory task may add another warning.

### Task 1: Make Inventory Success and Failure Semantics Truthful

**Files:**
- Create: `tests/test_inventory_semantics.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `tests/test_setup.py`

- [ ] **Step 1: Add the complete failing inventory-semantics test module**

```python
from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
)
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _device(device_id: int = 1) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True, "fan_speed": 2},
            "settings": {},
        }
    )


def _coordinator(hass, api, store: AtmeexStateStore) -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=store,
        config_entry_id="entry-1",
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_valid_empty_inventory_is_a_success(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = []
    store = AtmeexStateStore()
    coordinator = _coordinator(hass, api, store)

    result = await coordinator._async_update_data()

    assert result == {"devices": [], "device_map": {}, "states": {}}
    assert coordinator.last_success_ts is not None
    assert coordinator.last_inventory_success_mono is not None
    assert coordinator.last_api_error is None
    api.get_devices.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_outage_keeps_confirmed_snapshot_and_success_timestamp(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device()]
    store = AtmeexStateStore()
    coordinator = _coordinator(hass, api, store)
    confirmed = await coordinator._async_update_data()
    success_ts = coordinator.last_success_ts
    inventory_mono = coordinator.last_inventory_success_mono
    api.get_devices.side_effect = AtmeexConnectionError(
        "get_devices", "cloud unavailable"
    )

    with pytest.raises(UpdateFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    assert store.data == confirmed
    assert coordinator.last_success_ts == success_ts
    assert coordinator.last_inventory_success_mono == inventory_mono
    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)


@pytest.mark.asyncio
async def test_authentication_failure_requests_reauthentication(hass) -> None:
    api = AsyncMock()
    api.get_devices.side_effect = AtmeexAuthenticationError(
        "get_devices", "access rejected", status=401
    )
    coordinator = _coordinator(hass, api, AtmeexStateStore())

    with pytest.raises(ConfigEntryAuthFailed, match="get_devices failed"):
        await coordinator._async_update_data()

    assert coordinator.last_success_ts is None
    assert isinstance(coordinator.last_api_error, AtmeexAuthenticationError)


@pytest.mark.asyncio
async def test_identical_inventory_is_comparable_and_does_not_notify(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_device()]
    coordinator = _coordinator(hass, api, AtmeexStateStore())
    first = await coordinator._async_update_data()
    coordinator.async_set_updated_data(first)
    listener = Mock()
    remove_listener = coordinator.async_add_listener(listener)

    second = await coordinator._async_update_data()
    coordinator.async_set_updated_data(second)

    assert second == first
    assert set(second) == {"devices", "device_map", "states"}
    assert coordinator.avg_latency_ms is not None
    assert coordinator.request_retries == 0
    listener.assert_not_called()
    remove_listener()
```

- [ ] **Step 2: Run the new module and verify the audited failures**

Run: `.venv/bin/python -m pytest tests/test_inventory_semantics.py -q`

Expected: FAIL during setup with `TypeError: AtmeexCoordinator.__init__() got an unexpected keyword argument 'api'`; on a branch with only the constructor migrated, the outage case instead fails because the current fallback converts the error to an empty success.

- [ ] **Step 3: Replace the coordinator data contract, constructor, and update method with the minimal truthful implementation**

Use these imports and definitions in `custom_components/atmeex_cloud/coordinator.py`:

```python
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any, TypedDict

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
from .state_store import AtmeexStateStore


class AtmeexCoordinatorData(TypedDict):
    devices: list[dict[str, Any]]
    device_map: dict[str, AtmeexDevice]
    states: dict[str, dict[str, Any]]


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        api: AtmeexApi,
        state_store: AtmeexStateStore,
        config_entry_id: str,
        name: str,
        update_interval: timedelta,
        fire_logbook_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        super().__init__(
            hass,
            logger,
            name=name,
            update_interval=update_interval,
            update_method=self._async_update_data,
            always_update=False,
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

    def _fire_api_error_event(self, data: dict[str, Any]) -> None:
        """Emit the existing event contract with only sanitized values."""
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

    async def _hydrate_devices(
        self, devices: list[AtmeexDevice]
    ) -> list[AtmeexDevice]:
        return devices

    async def _async_update_data(self) -> AtmeexCoordinatorData:
        baselines = self.state_store.capture_all()
        started = time.perf_counter()
        try:
            listed_devices = await self.api.get_devices()
            hydrated_devices = await self._hydrate_devices(listed_devices)
            update = self.state_store.apply_inventory(
                hydrated_devices,
                baselines,
            )
        except AtmeexAuthenticationError as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {
                    "message": str(err),
                    "operation": err.operation,
                    "status": err.status,
                    "source": "coordinator_update",
                }
            )
            raise ConfigEntryAuthFailed(f"{err.operation} failed") from err
        except (
            AtmeexConnectionError,
            AtmeexRateLimitError,
            AtmeexProtocolError,
        ) as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {
                    "message": str(err),
                    "operation": err.operation,
                    "status": err.status,
                    "source": "coordinator_update",
                }
            )
            raise UpdateFailed(f"{err.operation} failed") from err

        self.avg_latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        retry_count = getattr(self.api, "retry_count", 0)
        self.request_retries = retry_count if isinstance(retry_count, int) else 0
        self.last_success_ts = time.time()
        self.last_inventory_success_mono = time.monotonic()
        self.last_api_error = None
        return update.data
```

Delete `_fetch_devices_safely`, `_ws_device_update_ts`, `_refresh_device_update_ts`, and timing keys in `AtmeexCoordinatorData`. The state store now performs every merge, and only `devices`, `device_map`, and `states` participate in equality.

In `async_setup_entry`, migrate the composition root in the same task so the
constructor change never leaves the repository broken:

```python
    state_store = AtmeexStateStore()
    coordinator = AtmeexCoordinator(
        hass,
        _LOGGER,
        api=api,
        state_store=state_store,
        config_entry_id=entry.entry_id,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=update_interval_seconds),
        fire_logbook_event=_fire_logbook_event,
    )
    await coordinator.async_config_entry_first_refresh()
```

Pass that same `state_store` to `AtmeexRuntimeData`. Remove the old
`coordinator.setup_update(...)` call, but otherwise preserve the transactional
startup and task ownership established by Plan 4.

- [ ] **Step 4: Run the focused module green**

Run: `.venv/bin/python -m pytest tests/test_inventory_semantics.py tests/test_setup.py -q`

Expected: the inventory module and setup suite pass; the real setup path uses
the final coordinator constructor and the exact same state-store instance.

- [ ] **Step 5: Run the existing coordinator and state-store tests**

Run: `.venv/bin/python -m pytest tests/test_coordinator.py tests/test_state_store.py -q`

Expected: PASS with no fallback-empty or whole-device timestamp expectation remaining.

- [ ] **Step 6: Commit the truthful inventory boundary**

```bash
git add custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests/test_inventory_semantics.py tests/test_coordinator.py tests/test_setup.py
git commit -m "fix: make inventory failures authoritative"
```

### Task 2: Skip Complete Details and Bound Missing Detail Hydration to Three

**Files:**
- Create: `tests/test_detail_hydration.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`

- [ ] **Step 1: Add deterministic hydration tests with event barriers**

```python
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.atmeex_cloud.api import (
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
)
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _listed(device_id: int, *, complete: bool) -> AtmeexDevice:
    raw = {
        "id": device_id,
        "name": f"Listed {device_id}",
        "model": "AirNanny",
        "online": True,
    }
    if complete:
        raw["condition"] = {"pwr_on": True, "fan_speed": device_id}
        raw["settings"] = {}
    return AtmeexDevice.from_raw(raw)


def _detailed(device_id: int) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Detailed {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True, "fan_speed": device_id},
            "settings": {},
        }
    )


def _coordinator(hass, api) -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=AtmeexStateStore(),
        config_entry_id="entry-1",
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_complete_list_data_skips_every_detail_request(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_listed(1, complete=True), _listed(2, complete=True)]
    coordinator = _coordinator(hass, api)

    result = await coordinator._async_update_data()

    assert set(result["device_map"]) == {"1", "2"}
    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_details_never_exceed_three_concurrent_requests(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_listed(index, complete=False) for index in range(1, 6)]
    release = asyncio.Event()
    three_started = asyncio.Event()
    active = 0
    maximum_active = 0

    async def get_device(device_id: int | str) -> AtmeexDevice:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 3:
            three_started.set()
        await release.wait()
        active -= 1
        return _detailed(int(device_id))

    api.get_device.side_effect = get_device
    coordinator = _coordinator(hass, api)

    update_task = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(three_started.wait(), timeout=1.0)
    assert active == 3
    assert api.get_device.await_count == 3
    release.set()
    result = await update_task

    assert maximum_active == 3
    assert api.get_device.await_count == 5
    assert set(result["device_map"]) == {"1", "2", "3", "4", "5"}


@pytest.mark.asyncio
async def test_failed_detail_preserves_old_detail_but_accepts_list_metadata(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_detailed(1)]
    coordinator = _coordinator(hass, api)
    await coordinator._async_update_data()
    api.get_devices.return_value = [_listed(1, complete=False)]
    api.get_device.side_effect = AtmeexConnectionError(
        "get_device", "detail unavailable"
    )

    result = await coordinator._async_update_data()

    assert result["device_map"]["1"].name == "Listed 1"
    assert result["states"]["1"]["fan_speed"] == 1


@pytest.mark.asyncio
async def test_detail_authentication_failure_aborts_entire_refresh(hass) -> None:
    api = AsyncMock()
    api.get_devices.return_value = [_listed(1, complete=False)]
    api.get_device.side_effect = AtmeexAuthenticationError(
        "get_device", "token rejected", status=401
    )
    coordinator = _coordinator(hass, api)

    with pytest.raises(ConfigEntryAuthFailed, match="get_device failed"):
        await coordinator._async_update_data()
```

- [ ] **Step 2: Run the hydration module and observe the barrier timeout**

Run: `.venv/bin/python -m pytest tests/test_detail_hydration.py -q`

Expected: FAIL in `test_missing_details_never_exceed_three_concurrent_requests` with `TimeoutError`, because the Task 1 stub never requests missing details.

- [ ] **Step 3: Replace the hydration stub with complete bounded hydration helpers**

Add these methods to `AtmeexCoordinator` and the constant at module scope:

```python
_MAX_DETAIL_CONCURRENCY = 3


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    @staticmethod
    def _needs_detail(device: AtmeexDevice) -> bool:
        condition = device.raw.get("condition")
        settings = device.raw.get("settings")
        if not isinstance(condition, Mapping) or not isinstance(settings, Mapping):
            return True
        has_power = "pwr_on" in condition or "u_pwr_on" in settings
        has_fan = "fan_speed" in condition or "u_fan_speed" in settings
        return not (has_power and has_fan)

    def _preserve_previous_detail(self, listed: AtmeexDevice) -> AtmeexDevice:
        previous = self.state_store.data.get("device_map", {}).get(str(listed.id))
        if previous is None:
            return listed
        merged = previous.to_ha_dict()
        for key, value in listed.raw.items():
            if key in ("condition", "settings") and isinstance(value, Mapping):
                section = dict(merged.get(key, {}))
                section.update(value)
                merged[key] = section
            else:
                merged[key] = value
        return AtmeexDevice.from_raw(merged)

    async def _hydrate_one(
        self,
        device: AtmeexDevice,
        semaphore: asyncio.Semaphore,
    ) -> AtmeexDevice:
        if not self._needs_detail(device):
            return device
        try:
            async with semaphore:
                return await self.api.get_device(device.id)
        except AtmeexAuthenticationError:
            raise
        except (
            AtmeexConnectionError,
            AtmeexRateLimitError,
            AtmeexProtocolError,
        ):
            return self._preserve_previous_detail(device)

    async def _hydrate_devices(
        self,
        devices: list[AtmeexDevice],
    ) -> list[AtmeexDevice]:
        semaphore = asyncio.Semaphore(_MAX_DETAIL_CONCURRENCY)
        results: dict[int, AtmeexDevice] = {}

        async def hydrate_at(index: int, device: AtmeexDevice) -> None:
            results[index] = await self._hydrate_one(device, semaphore)

        try:
            async with asyncio.TaskGroup() as group:
                for index, device in enumerate(devices):
                    group.create_task(hydrate_at(index, device))
        except* AtmeexAuthenticationError as error_group:
            raise error_group.exceptions[0]

        return [results[index] for index in range(len(devices))]
```

`asyncio.TaskGroup` cancels sibling hydrations if one device reports an authentication failure. Transient detail failures retain the prior condition/settings while `merged.update(listed.raw)` accepts newer list-level name, model, online, and capability metadata.

- [ ] **Step 4: Run the focused hydration tests green**

Run: `.venv/bin/python -m pytest tests/test_detail_hydration.py -q`

Expected: `4 passed` and exit code 0.

- [ ] **Step 5: Run coordinator and API contract tests together**

Run: `.venv/bin/python -m pytest tests/test_inventory_semantics.py tests/test_detail_hydration.py tests/test_api.py -q`

Expected: PASS; `get_devices` is called once per inventory cycle, complete list payloads issue zero detail calls, and maximum observed detail concurrency is three.

- [ ] **Step 6: Commit bounded detail hydration**

```bash
git add custom_components/atmeex_cloud/coordinator.py tests/test_detail_hydration.py
git commit -m "perf: bound Atmeex detail hydration"
```

### Task 3: Reconcile Absence Across Two Successful Inventories and Remove Stale Registry Associations

**Files:**
- Create: `tests/test_inventory_removal.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `tests/test_unload.py`

- [ ] **Step 1: Add complete two-success and device-registry tests**

```python
from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.atmeex_cloud import async_remove_config_entry_device
from custom_components.atmeex_cloud.api import AtmeexConnectionError, AtmeexDevice
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _device(device_id: int) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True},
            "settings": {},
        }
    )


def _coordinator(hass, api, entry_id: str = "entry-1") -> AtmeexCoordinator:
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=AtmeexStateStore(),
        config_entry_id=entry_id,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_device_is_removed_after_two_successful_absences(hass) -> None:
    api = AsyncMock()
    api.get_devices.side_effect = [[_device(1), _device(2)], [_device(1)], [_device(1)]]
    coordinator = _coordinator(hass, api)

    first = await coordinator._async_update_data()
    one_absence = await coordinator._async_update_data()
    two_absences = await coordinator._async_update_data()

    assert set(first["device_map"]) == {"1", "2"}
    assert set(one_absence["device_map"]) == {"1", "2"}
    assert set(two_absences["device_map"]) == {"1"}


@pytest.mark.asyncio
async def test_failed_inventory_does_not_advance_absence_count(hass) -> None:
    api = AsyncMock()
    api.get_devices.side_effect = [
        [_device(1), _device(2)],
        [_device(1)],
        AtmeexConnectionError("get_devices", "outage"),
        [_device(1)],
    ]
    coordinator = _coordinator(hass, api)

    await coordinator._async_update_data()
    once_absent = await coordinator._async_update_data()
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    after_failure = coordinator.state_store.data
    twice_absent = await coordinator._async_update_data()

    assert "2" in once_absent["device_map"]
    assert "2" in after_failure["device_map"]
    assert "2" not in twice_absent["device_map"]


@pytest.mark.asyncio
async def test_confirmed_stale_device_loses_config_entry_association(hass) -> None:
    entry_id = "entry-1"
    registry = dr.async_get(hass)
    device_entry = registry.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, "2")},
        name="Device 2",
    )
    api = AsyncMock()
    api.get_devices.side_effect = [[_device(2)], [], []]
    coordinator = _coordinator(hass, api, entry_id)

    await coordinator._async_update_data()
    await coordinator._async_update_data()
    assert entry_id in registry.async_get(device_entry.id).config_entries
    await coordinator._async_update_data()

    assert entry_id not in registry.async_get(device_entry.id).config_entries


@pytest.mark.asyncio
async def test_manual_removal_refuses_active_and_allows_absent_device(hass) -> None:
    runtime = SimpleNamespace(
        state_store=SimpleNamespace(
            data={"devices": [], "device_map": {"7": _device(7)}, "states": {}}
        )
    )
    entry = SimpleNamespace(runtime_data=runtime)
    active = SimpleNamespace(identifiers={(DOMAIN, "7")})
    absent = SimpleNamespace(identifiers={(DOMAIN, "8")})

    assert await async_remove_config_entry_device(hass, entry, active) is False
    assert await async_remove_config_entry_device(hass, entry, absent) is True
```

- [ ] **Step 2: Run the removal tests and verify stale association/manual policy failures**

Run: `.venv/bin/python -m pytest tests/test_inventory_removal.py -q`

Expected: two failures: the config-entry association remains attached after the second absence, and `async_remove_config_entry_device` incorrectly returns `True` for the active identifier.

- [ ] **Step 3: Consume `removed_device_ids` and disassociate only confirmed stale devices**

Add the import and method to `custom_components/atmeex_cloud/coordinator.py`:

```python
from homeassistant.helpers import device_registry as dr


class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
    def _remove_confirmed_stale_devices(
        self,
        removed_device_ids: frozenset[str],
    ) -> None:
        if not removed_device_ids:
            return
        registry = dr.async_get(self.hass)
        for device_entry in dr.async_entries_for_config_entry(
            registry,
            self.config_entry_id,
        ):
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
```

Add `from .const import DOMAIN`, then call the method only after `apply_inventory` succeeds:

```python
        self._remove_confirmed_stale_devices(update.removed_device_ids)
        self.avg_latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
```

The store's `apply_inventory` owns the absence counters and returns an ID only on the second consecutive successful authoritative absence. Because the call is below the typed exception handlers, a failed inventory cannot disassociate anything.

- [ ] **Step 4: Replace permissive manual removal with an authoritative presence check**

Replace `async_remove_config_entry_device` in `custom_components/atmeex_cloud/__init__.py`:

```python
async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: AtmeexConfigEntry,
    device_entry: DeviceEntry,
) -> bool:
    runtime = getattr(config_entry, "runtime_data", None)
    if runtime is None:
        return False
    atmeex_ids = {
        str(identifier)
        for domain, identifier in device_entry.identifiers
        if domain == DOMAIN
    }
    if not atmeex_ids:
        return False
    current_ids = set(runtime.state_store.data.get("device_map", {}))
    return atmeex_ids.isdisjoint(current_ids)
```

`AtmeexConfigEntry` is the typed alias supplied by the lifecycle/typing plans. Manual removal is refused throughout the one-miss grace period because the store still publishes the device until the second successful absence.

- [ ] **Step 5: Run the removal module green**

Run: `.venv/bin/python -m pytest tests/test_inventory_removal.py -q`

Expected: `4 passed` and exit code 0.

- [ ] **Step 6: Replace obsolete unload removal assertions and run affected tests**

Run: `.venv/bin/python -m pytest tests/test_inventory_removal.py tests/test_unload.py tests/test_coordinator.py -q`

Expected: PASS; tests no longer expect per-device command-lock dictionaries owned by pre-Plan-3 runtime code.

- [ ] **Step 7: Commit two-success removal behavior**

```bash
git add custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests/test_inventory_removal.py tests/test_unload.py tests/test_coordinator.py
git commit -m "fix: retire devices after confirmed absence"
```

### Task 4: Enforce Maximum Inventory Age During Continuous Push Traffic

**Files:**
- Create: `tests/test_inventory_age.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/runtime.py`
- Modify: `tests/test_setup.py`

- [ ] **Step 1: Add complete deadline and task-ownership tests**

```python
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.atmeex_cloud as integration
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


def _coordinator(hass) -> AtmeexCoordinator:
    api = AsyncMock()
    return AtmeexCoordinator(
        hass,
        logging.getLogger(__name__),
        api=api,
        state_store=AtmeexStateStore(),
        config_entry_id="entry-1",
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_inventory_refreshes_at_maximum_age_boundary(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.last_inventory_success_mono = 100.0
    coordinator.async_request_refresh = AsyncMock()

    assert await coordinator.async_ensure_inventory_fresh(now_mono=129.999) is False
    assert await coordinator.async_ensure_inventory_fresh(now_mono=130.0) is True
    coordinator.async_request_refresh.assert_awaited_once_with()


def test_websocket_publication_does_not_move_inventory_deadline(hass) -> None:
    coordinator = _coordinator(hass)
    coordinator.last_inventory_success_mono = 100.0
    device = AtmeexDevice.from_raw(
        {
            "id": 1,
            "name": "Device 1",
            "model": "AirNanny",
            "online": True,
            "condition": {"pwr_on": True},
            "settings": {},
        }
    )
    baseline = coordinator.state_store.capture_all().get("1")
    coordinator.state_store.apply_inventory(
        [device], coordinator.state_store.capture_all()
    )
    coordinator.state_store.apply_websocket_delta(
        "1", state_delta={"fan_speed": 5}
    )
    coordinator.async_set_updated_data(coordinator.state_store.data)

    assert baseline is None
    assert coordinator.last_inventory_success_mono == 100.0


@pytest.mark.asyncio
async def test_watchdog_checks_freshness_without_untracked_work(monkeypatch) -> None:
    runtime = SimpleNamespace(stopping=False)

    async def stop_after_check() -> bool:
        runtime.stopping = True
        return True

    runtime.coordinator = SimpleNamespace(
        async_ensure_inventory_fresh=AsyncMock(side_effect=stop_after_check)
    )
    monkeypatch.setattr(integration.asyncio, "sleep", AsyncMock())

    await integration._async_inventory_watchdog(runtime, check_interval=5.0)

    runtime.coordinator.async_ensure_inventory_fresh.assert_awaited_once_with()


def test_watchdog_start_uses_compat_task_and_runtime_tracking(monkeypatch) -> None:
    created_task = MagicMock(spec=asyncio.Task)
    create_background = MagicMock(return_value=created_task)
    monkeypatch.setattr(integration, "async_create_background_task", create_background)
    runtime = SimpleNamespace(track_task=MagicMock(return_value=created_task))
    hass = SimpleNamespace()

    result = integration._start_inventory_watchdog(
        hass,
        runtime,
        check_interval=5.0,
    )

    assert result is created_task
    runtime.track_task.assert_called_once_with(created_task)
    assert create_background.call_args.args[0] is hass
    assert create_background.call_args.args[2] == "atmeex-inventory-watchdog"
    create_background.call_args.args[1].close()
```

- [ ] **Step 2: Run the deadline module and verify missing-method failures**

Run: `.venv/bin/python -m pytest tests/test_inventory_age.py -q`

Expected: FAIL with `AttributeError: 'AtmeexCoordinator' object has no attribute 'async_ensure_inventory_fresh'` and missing `_async_inventory_watchdog`/`_start_inventory_watchdog` symbols.

- [ ] **Step 3: Add the locked maximum-age check to the coordinator**

```python
    async def async_ensure_inventory_fresh(
        self,
        now_mono: float | None = None,
    ) -> bool:
        now = time.monotonic() if now_mono is None else now_mono
        last_success = self.last_inventory_success_mono
        if (
            last_success is not None
            and now - last_success < self._max_inventory_age_seconds
        ):
            return False

        async with self._inventory_refresh_lock:
            current_last_success = self.last_inventory_success_mono
            if (
                current_last_success is not None
                and now - current_last_success < self._max_inventory_age_seconds
            ):
                return False
            await self.async_request_refresh()
        return True
```

Only `_async_update_data` writes `last_inventory_success_mono`. Calls to `async_set_updated_data` from the WebSocket state-store publication path therefore cannot postpone inventory discovery.

- [ ] **Step 4: Add the watchdog coroutine and owned-task factory**

Add `from .compat import async_create_background_task` and these helpers to `custom_components/atmeex_cloud/__init__.py`:

```python
async def _async_inventory_watchdog(
    runtime: AtmeexRuntimeData,
    *,
    check_interval: float,
) -> None:
    while not runtime.stopping:
        await asyncio.sleep(check_interval)
        if runtime.stopping:
            return
        await runtime.coordinator.async_ensure_inventory_fresh()


def _start_inventory_watchdog(
    hass: HomeAssistant,
    runtime: AtmeexRuntimeData,
    *,
    check_interval: float,
) -> asyncio.Task[Any]:
    task = async_create_background_task(
        hass,
        _async_inventory_watchdog(runtime, check_interval=check_interval),
        "atmeex-inventory-watchdog",
    )
    return runtime.track_task(task)
```

After `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)` succeeds, start the task with an interval that catches the deadline without busy polling:

```python
    runtime.inventory_watchdog_task = _start_inventory_watchdog(
        hass,
        runtime,
        check_interval=max(1.0, min(30.0, update_interval_seconds / 2.0)),
    )
    return True
```

Do not start this task before platform forwarding. The Plan-4 cleanup routine already cancels and awaits everything in `runtime.tasks`, so platform-forward rollback and unload own this watchdog automatically.

- [ ] **Step 5: Run the deadline and setup tests green**

Run: `.venv/bin/python -m pytest tests/test_inventory_age.py tests/test_setup.py -q`

Expected: PASS; `tests/test_inventory_age.py` reports `4 passed`, and setup asserts the watchdog task is absent before platform forwarding and tracked after it succeeds.

- [ ] **Step 6: Run lifecycle tests to prove watchdog cleanup**

Run: `.venv/bin/python -m pytest tests/test_setup.py tests/test_unload.py tests/test_websocket_integration.py -q`

Expected: PASS with zero pending-task or un-awaited-coroutine warnings.

- [ ] **Step 7: Commit maximum inventory age enforcement**

```bash
git add custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/runtime.py tests/test_inventory_age.py tests/test_setup.py
git commit -m "fix: enforce maximum inventory age"
```

### Task 5: Combine Coordinator Health, Inventory Presence, and Device Connectivity

**Files:**
- Create: `tests/test_entity_availability.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Modify: `custom_components/atmeex_cloud/binary_sensor.py`
- Modify: `tests/test_sensor.py`
- Modify: `tests/test_binary_sensor.py`

- [ ] **Step 1: Add the complete availability truth-table tests**

```python
from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import AtmeexOnlineSensor
from custom_components.atmeex_cloud.sensor import AtmeexCO2Sensor


def _device() -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": 7,
            "name": "Bedroom",
            "model": "AirNanny",
            "online": True,
            "condition": {"online": True, "co2_ppm": 600},
            "settings": {},
        }
    )


def _coordinator(
    *,
    update_success: bool,
    present: bool,
    online: bool,
) -> SimpleNamespace:
    device = _device()
    return SimpleNamespace(
        last_update_success=update_success,
        data={
            "devices": [device.to_ha_dict()] if present else [],
            "device_map": {"7": device} if present else {},
            "states": {"7": {"online": online, "co2_ppm": 600}} if present else {},
        },
    )


@pytest.mark.parametrize(
    ("update_success", "present", "online", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_regular_entity_availability_truth_table(
    update_success: bool,
    present: bool,
    online: bool,
    expected: bool,
) -> None:
    entity = AtmeexCO2Sensor(
        _coordinator(
            update_success=update_success,
            present=present,
            online=online,
        ),
        _device(),
        "entry-1",
    )

    assert entity.available is expected


def test_online_sensor_unavailable_when_coordinator_failed() -> None:
    entity = AtmeexOnlineSensor(
        coordinator=_coordinator(update_success=False, present=True, online=True),
        device=_device(),
        entry_id="entry-1",
    )

    assert entity.available is False


def test_online_sensor_available_and_off_when_confirmed_offline() -> None:
    entity = AtmeexOnlineSensor(
        coordinator=_coordinator(update_success=True, present=True, online=False),
        device=_device(),
        entry_id="entry-1",
    )

    assert entity.available is True
    assert entity.is_on is False


def test_online_sensor_unavailable_after_confirmed_removal() -> None:
    entity = AtmeexOnlineSensor(
        coordinator=_coordinator(update_success=True, present=False, online=False),
        device=_device(),
        entry_id="entry-1",
    )

    assert entity.available is False
```

- [ ] **Step 2: Run the truth table and verify coordinator/removal failures**

Run: `.venv/bin/python -m pytest tests/test_entity_availability.py -q`

Expected: three failing cases: regular entities remain available during coordinator failure or removal, and the online sensor remains unconditionally available.

- [ ] **Step 3: Add reusable coordinator-and-presence availability and regular online gating**

Replace `AtmeexEntityMixin.available` and add the helper property in `custom_components/atmeex_cloud/entity_base.py`:

```python
    @property
    def _coordinator_and_device_available(self) -> bool:
        if not bool(getattr(self.coordinator, "last_update_success", False)):
            return False
        data = getattr(self.coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        return self._device_id_str in device_map

    @property
    def available(self) -> bool:
        if not self._coordinator_and_device_available:
            return False
        state = self._device_state
        if "online" in state:
            return bool(state["online"])
        return bool(getattr(self._device, "online", False))
```

This implements the regular-entity rule: coordinator success AND inventory presence AND device online.

- [ ] **Step 4: Make the connectivity entity reflect coordinator health without hiding offline state**

Replace `AtmeexOnlineSensor.available` in `custom_components/atmeex_cloud/binary_sensor.py`:

```python
    @property
    def available(self) -> bool:
        return self._coordinator_and_device_available
```

The connectivity entity intentionally omits the final device-online term: a confirmed offline device remains an available binary sensor with `is_on == False`; a coordinator outage or confirmed removal makes it unavailable.

- [ ] **Step 5: Run focused availability tests green**

Run: `.venv/bin/python -m pytest tests/test_entity_availability.py -q`

Expected: `7 passed` and exit code 0 (the parametrized truth table contributes four cases).

- [ ] **Step 6: Update superseded assertions and run all entity platforms**

Run: `.venv/bin/python -m pytest tests/test_sensor.py tests/test_binary_sensor.py tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py -q`

Expected: PASS; the former `test_online_sensor_always_available` assertion is replaced with coordinator-success/presence cases, and every public unique ID remains unchanged.

- [ ] **Step 7: Commit coordinator-aware availability**

```bash
git add custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/binary_sensor.py tests/test_entity_availability.py tests/test_sensor.py tests/test_binary_sensor.py
git commit -m "fix: honor coordinator health in availability"
```

### Task 6: Record Discovery Keys Before Constructing Entity Objects

**Files:**
- Create: `tests/test_entity_discovery.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`

- [ ] **Step 1: Add a complete construction-count regression test**

```python
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.entity_base import (
    EntityDiscovery,
    setup_dynamic_device_entities,
)


def _device(device_id: int) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": f"Device {device_id}",
            "model": "AirNanny",
            "online": True,
            "condition": {"online": True},
            "settings": {},
        }
    )


def test_known_keys_are_checked_before_entity_factories_run() -> None:
    listeners: list[object] = []
    device_one = _device(1)
    coordinator = SimpleNamespace(
        data={
            "devices": [device_one.to_ha_dict()],
            "device_map": {"1": device_one},
            "states": {"1": {"online": True}},
        },
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    entry = SimpleNamespace(async_on_unload=MagicMock())
    factory = MagicMock(side_effect=lambda device_id: SimpleNamespace(device_id=device_id))
    added: list[object] = []

    def discover(device: AtmeexDevice, state: dict[str, object]):
        assert state["online"] is True
        return (
            EntityDiscovery(
                key="main",
                factory=lambda: factory(str(device.id)),
            ),
        )

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=added.extend,
        discover_entities=discover,
    )
    assert factory.call_count == 1
    assert len(added) == 1

    listeners[0]()
    assert factory.call_count == 1
    assert len(added) == 1

    device_two = _device(2)
    coordinator.data["device_map"]["2"] = device_two
    coordinator.data["states"]["2"] = {"online": True}
    listeners[0]()

    assert factory.call_count == 2
    assert [entity.device_id for entity in added] == ["1", "2"]
    entry.async_on_unload.assert_called_once()
```

- [ ] **Step 2: Run the regression and verify the new discovery API is absent**

Run: `.venv/bin/python -m pytest tests/test_entity_discovery.py -q`

Expected: FAIL during collection with `ImportError: cannot import name 'EntityDiscovery' from 'custom_components.atmeex_cloud.entity_base'`.

- [ ] **Step 3: Implement factory-based discovery with the key check before construction**

Add these imports and replace `setup_dynamic_device_entities` in `custom_components/atmeex_cloud/entity_base.py`:

```python
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from homeassistant.helpers.entity import Entity


@dataclass(frozen=True, slots=True)
class EntityDiscovery:
    key: str
    factory: Callable[[], Entity]


def setup_dynamic_device_entities(
    *,
    entry: Any,
    coordinator: Any,
    async_add_entities: Callable[[list[Entity]], None],
    discover_entities: Callable[
        [AtmeexDevice, Mapping[str, Any]], Iterable[EntityDiscovery]
    ] | None = None,
    build_entities: Callable[[AtmeexDevice], Iterable[Entity]] | None = None,
) -> None:
    # Temporary bridge for the existing platforms. Task 7 removes this branch
    # after every call site supplies lazy EntityDiscovery factories.
    if discover_entities is None:
        if build_entities is None:
            raise TypeError("discover_entities is required")

        def discover_entities(
            device: AtmeexDevice,
            _state: Mapping[str, Any],
        ) -> tuple[EntityDiscovery, ...]:
            entities = tuple(build_entities(device))
            return tuple(
                EntityDiscovery(
                    key=str(
                        getattr(entity, "unique_id", None)
                        or getattr(entity, "_attr_unique_id", index)
                    ),
                    factory=lambda entity=entity: entity,
                )
                for index, entity in enumerate(entities)
            )

    known_keys: set[tuple[str, str]] = set()

    def sync_entities() -> None:
        data = getattr(coordinator, "data", None) or {}
        device_map = data.get("device_map", {}) or {}
        states = data.get("states", {}) or {}
        new_entities: list[Entity] = []

        for device_id, device in device_map.items():
            state = states.get(str(device_id), {}) or {}
            for discovery in discover_entities(device, state):
                discovery_key = (str(device_id), discovery.key)
                if discovery_key in known_keys:
                    continue
                known_keys.add(discovery_key)
                new_entities.append(discovery.factory())

        if new_entities:
            async_add_entities(new_entities)

    sync_entities()
    remove_listener = coordinator.async_add_listener(sync_entities)
    entry.async_on_unload(remove_listener)
```

The old helper constructed every candidate on every coordinator callback and discarded duplicates afterward. This API makes factories lazy and keys them with the canonical string device ID, while each factory continues to pass the original `AtmeexDevice.id` to preserve unique IDs.

The `build_entities` bridge deliberately retains the old eager behavior only
between Tasks 6 and 7 so this commit leaves every existing platform test green.

- [ ] **Step 4: Run the construction-count test green**

Run: `.venv/bin/python -m pytest tests/test_entity_discovery.py -q`

Expected: `1 passed` and exit code 0.

- [ ] **Step 5: Commit the discovery primitive before platform migrations**

```bash
git add custom_components/atmeex_cloud/entity_base.py tests/test_entity_discovery.py
git commit -m "refactor: construct entities only for new keys"
```

### Task 7: Migrate Every Platform to Capability-Driven Lazy Discovery

**Files:**
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/fan.py`
- Modify: `custom_components/atmeex_cloud/select.py`
- Modify: `custom_components/atmeex_cloud/switch.py`
- Modify: `custom_components/atmeex_cloud/sensor.py`
- Modify: `custom_components/atmeex_cloud/binary_sensor.py`
- Modify: `tests/test_sensor.py`
- Modify: `tests/test_binary_sensor.py`

- [ ] **Step 1: Add capability-driven sensor tests using the real platform setup**

Append this complete code to `tests/test_sensor.py`:

```python
@pytest.mark.asyncio
async def test_sensor_setup_creates_only_reported_capabilities(hass) -> None:
    from custom_components.atmeex_cloud.sensor import (
        AtmeexDeviceSensor,
        AtmeexDiagnosticsSensor,
    )

    device = AtmeexDevice.from_raw(
        {
            "id": 9,
            "name": "Bedroom",
            "model": "AirNanny",
            "online": True,
            "condition": {"online": True, "temp_in": 210},
            "settings": {},
        }
    )
    listeners: list[object] = []
    coordinator = SimpleNamespace(
        data={
            "devices": [device.to_ha_dict()],
            "device_map": {"9": device},
            "states": {"9": {"online": True, "temp_in": 210}},
        },
        last_update_success=True,
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),
        coordinator=coordinator,
        state_store=SimpleNamespace(data=coordinator.data),
        command_executor=SimpleNamespace(),
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        options={"enable_co2": True},
        runtime_data=runtime,
        async_on_unload=lambda callback: None,
    )
    entities: list[object] = []

    await async_setup_entry(hass, entry, entities.extend)

    assert sum(isinstance(entity, AtmeexDiagnosticsSensor) for entity in entities) == 1
    device_sensors = [
        entity for entity in entities if isinstance(entity, AtmeexDeviceSensor)
    ]
    assert [entity.unique_id for entity in device_sensors] == ["9_inlet_temp"]

    coordinator.data["states"]["9"]["co2_ppm"] = 700
    listeners[0]()
    listeners[0]()

    device_sensors = [
        entity for entity in entities if isinstance(entity, AtmeexDeviceSensor)
    ]
    assert [entity.unique_id for entity in device_sensors] == [
        "9_inlet_temp",
        "9_co2",
    ]


@pytest.mark.asyncio
async def test_sensor_setup_honors_advertised_capability_and_co2_option(hass) -> None:
    device = AtmeexDevice.from_raw(
        {
            "id": 10,
            "name": "Nursery",
            "model": "AirNanny",
            "online": True,
            "capabilities": {"co2_ppm": True},
            "condition": {"online": True},
            "settings": {},
        }
    )
    coordinator = SimpleNamespace(
        data={
            "devices": [device.to_ha_dict()],
            "device_map": {"10": device},
            "states": {"10": {"online": True}},
        },
        last_update_success=True,
        async_add_listener=lambda listener: (lambda: None),
    )
    runtime = AtmeexRuntimeData(
        api=SimpleNamespace(),
        coordinator=coordinator,
        state_store=SimpleNamespace(data=coordinator.data),
        command_executor=SimpleNamespace(),
        refresh_device=AsyncMock(),
    )

    async def unique_ids(enable_co2: bool) -> set[str]:
        entry = SimpleNamespace(
            entry_id="entry-1",
            options={"enable_co2": enable_co2},
            runtime_data=runtime,
            async_on_unload=lambda callback: None,
        )
        entities: list[object] = []
        await async_setup_entry(hass, entry, entities.extend)
        return {
            entity.unique_id
            for entity in entities
            if getattr(entity, "unique_id", None) is not None
        }

    assert "10_co2" in await unique_ids(True)
    assert "10_co2" not in await unique_ids(False)
```

- [ ] **Step 2: Run sensor and binary-sensor tests to verify eager unsupported entities still exist**

Run: `.venv/bin/python -m pytest tests/test_sensor.py tests/test_binary_sensor.py -q`

Expected: FAIL because sensor setup currently constructs CO2, inlet temperature, and humidity for every device whenever the account option is enabled; repeated callbacks also enter the old `build_entities` API.

- [ ] **Step 3: Add an exact advertised-or-reported capability predicate**

Add to `custom_components/atmeex_cloud/entity_base.py`:

```python
def supports_state_capability(
    device: AtmeexDevice,
    state: Mapping[str, Any],
    field: str,
) -> bool:
    if field in state:
        return True
    capabilities = device.raw.get("capabilities")
    if isinstance(capabilities, Mapping):
        return bool(capabilities.get(field, False))
    if isinstance(capabilities, (list, tuple, set, frozenset)):
        return field in capabilities
    return False


def supports_humidifier(
    state: Mapping[str, Any] | None,
    *,
    device: AtmeexDevice | None = None,
) -> bool:
    current = state or {}
    if "hum_stg" in current or "no_water" in current:
        return True
    if device is None:
        return False
    capabilities = device.raw.get("capabilities")
    if isinstance(capabilities, Mapping):
        return any(
            bool(capabilities.get(key, False))
            for key in ("humidifier", "hum_stg", "no_water")
        )
    if isinstance(capabilities, (list, tuple, set, frozenset)):
        return bool(
            {"humidifier", "hum_stg", "no_water"}.intersection(capabilities)
        )
    return False
```

Unknown capability shapes return `False`. Existing registry entries are not disabled or removed; this changes only which new entity objects are offered when a device has never exposed the field.

- [ ] **Step 4: Replace sensor and binary-sensor setup functions with lazy capability discovery**

Use these imports in both modules:

```python
from .entity_base import (
    AtmeexEntityMixin,
    EntityDiscovery,
    setup_dynamic_device_entities,
    supports_humidifier,
    supports_state_capability,
)
```

Replace `sensor.async_setup_entry`:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmeexConfigEntry,
    async_add_entities,
) -> None:
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    async_add_entities([AtmeexDiagnosticsSensor(runtime, entry.entry_id)])
    enable_co2 = entry.options.get(CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2)

    def discover(
        device: AtmeexDevice,
        state: Mapping[str, Any],
    ) -> list[EntityDiscovery]:
        discoveries: list[EntityDiscovery] = []
        for spec in _DEVICE_SENSOR_SPECS:
            if spec.key == "co2_ppm" and not enable_co2:
                continue
            if not supports_state_capability(device, state, spec.key):
                continue
            discoveries.append(
                EntityDiscovery(
                    key=spec.unique_suffix,
                    factory=lambda device=device, spec=spec: AtmeexDeviceSensor(
                        coordinator=coordinator,
                        device=device,
                        entry_id=entry.entry_id,
                        spec=spec,
                    ),
                )
            )
        return discoveries

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

Replace `binary_sensor.async_setup_entry`:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: AtmeexConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator

    def discover(
        device: AtmeexDevice,
        state: Mapping[str, Any],
    ) -> list[EntityDiscovery]:
        discoveries = [
            EntityDiscovery(
                key="online",
                factory=lambda device=device: AtmeexOnlineSensor(
                    coordinator=coordinator,
                    device=device,
                    entry_id=entry.entry_id,
                ),
            )
        ]
        if supports_humidifier(state, device=device):
            discoveries.append(
                EntityDiscovery(
                    key="no_water",
                    factory=lambda device=device: AtmeexNoWaterSensor(
                        coordinator=coordinator,
                        device=device,
                        entry_id=entry.entry_id,
                    ),
                )
            )
        return discoveries

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

- [ ] **Step 5: Migrate command platforms without changing their unique IDs**

Replace only each platform's `async_setup_entry` discovery closure. Use the exact keys below; they are internal and do not alter `_attr_unique_id`.

`climate.py`:

```python
    def discover(device: AtmeexDevice, state: Mapping[str, Any]):
        return (
            EntityDiscovery(
                key="climate",
                factory=lambda device=device: AtmeexClimateEntity(
                    coordinator=coordinator,
                    api=runtime.api,
                    entry_id=entry.entry_id,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            ),
        )

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

`fan.py`:

```python
    def discover(device: AtmeexDevice, state: Mapping[str, Any]):
        return (
            EntityDiscovery(
                key="fan",
                factory=lambda device=device: AtmeexFanEntity(
                    coordinator=coordinator,
                    api=runtime.api,
                    entry_id=entry.entry_id,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            ),
        )

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

`select.py`:

```python
    def discover(device: AtmeexDevice, state: Mapping[str, Any]):
        discoveries = [
            EntityDiscovery(
                key="breezer_mode",
                factory=lambda device=device: AtmeexBreezerSelect(
                    coordinator=coordinator,
                    api=runtime.api,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            )
        ]
        if supports_humidifier(state, device=device):
            discoveries.append(
                EntityDiscovery(
                    key="humidification",
                    factory=lambda device=device: AtmeexHumidificationSelect(
                        coordinator=coordinator,
                        api=runtime.api,
                        device=device,
                        refresh_device_cb=runtime.refresh_device,
                        runtime=runtime,
                    ),
                )
            )
        return tuple(discoveries)

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

`switch.py`:

```python
    def discover(device: AtmeexDevice, state: Mapping[str, Any]):
        return (
            EntityDiscovery(
                key="auto_nanny",
                factory=lambda device=device: AtmeexAutoNannySwitch(
                    coordinator=coordinator,
                    api=runtime.api,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            ),
            EntityDiscovery(
                key="sleep_mode",
                factory=lambda device=device: AtmeexSleepModeSwitch(
                    coordinator=coordinator,
                    api=runtime.api,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            ),
            EntityDiscovery(
                key="power",
                factory=lambda device=device: AtmeexPowerSwitch(
                    coordinator=coordinator,
                    api=runtime.api,
                    device=device,
                    refresh_device_cb=runtime.refresh_device,
                    runtime=runtime,
                ),
            ),
        )

    setup_dynamic_device_entities(
        entry=entry,
        coordinator=coordinator,
        async_add_entities=async_add_entities,
        discover_entities=discover,
    )
```

Add `Mapping` and `EntityDiscovery` imports to each migrated platform. Do not modify constructor arguments, translation keys, or `_attr_unique_id` expressions.

- [ ] **Step 6: Run capability and construction tests green**

After all platform call sites above use `discover_entities`, delete the temporary
`build_entities` parameter and bridge from Task 6. The final helper has only the
locked lazy-factory interface.

Run: `.venv/bin/python -m pytest tests/test_entity_discovery.py tests/test_sensor.py tests/test_binary_sensor.py tests/test_select.py -q`

Expected: PASS; the two new sensor tests pass, unsupported CO2/humidity entities are absent, and a newly reported capability creates exactly one entity.

- [ ] **Step 7: Run every platform test to lock public entity IDs**

Run: `.venv/bin/python -m pytest tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py tests/test_sensor.py tests/test_binary_sensor.py -q`

Expected: PASS with the existing unique IDs unchanged: `{id}_climate`, `{id}_fan`, `{id}_hum_mode`, `{id}_breezer_mode`, `{id}_auto_nanny`, `{id}_sleep_mode`, `{id}_power`, `{id}_co2`, `{id}_inlet_temp`, `{id}_humidity`, `{id}_online`, and `{id}_no_water`.

- [ ] **Step 8: Commit platform discovery migration**

```bash
git add custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/fan.py custom_components/atmeex_cloud/select.py custom_components/atmeex_cloud/switch.py custom_components/atmeex_cloud/sensor.py custom_components/atmeex_cloud/binary_sensor.py tests/test_sensor.py tests/test_binary_sensor.py
git commit -m "perf: discover entities from capabilities"
```

### Task 8: Wire the Real Coordinator and Remove Superseded Timestamp/Fallback Paths

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `tests/test_setup.py`
- Modify: `tests/test_coordinator.py`
- Modify: `tests/test_websocket_integration.py`

- [ ] **Step 1: Add a setup assertion for the shared state store and coordinator identity**

Append to the happy-path setup test in `tests/test_setup.py`:

```python
    assert runtime.coordinator.state_store is runtime.state_store
    assert runtime.coordinator.api is runtime.api
    assert runtime.coordinator.config_entry_id == entry.entry_id
    assert runtime.inventory_watchdog_task in runtime.tasks
    assert set(runtime.coordinator.data) == {"devices", "device_map", "states"}
```

- [ ] **Step 2: Verify obsolete composition code did not survive the staged migration**

Run: `rg -n 'setup_update|_ws_device_update_ts|_refresh_device_update_ts|get_devices\(fallback=' custom_components/atmeex_cloud tests`

Expected: no matches and exit status 1. If a temporary compatibility path remains,
remove it in Step 3 before running the setup and WebSocket integration tests.

- [ ] **Step 3: Construct the coordinator directly from the shared store**

Confirm the exact construction introduced in Task 1 remains in
`async_setup_entry` after API authentication and option normalization:

```python
    state_store = AtmeexStateStore()
    coordinator = AtmeexCoordinator(
        hass,
        _LOGGER,
        api=api,
        state_store=state_store,
        config_entry_id=entry.entry_id,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=update_interval_seconds),
        fire_logbook_event=_fire_logbook_event,
    )
    await coordinator.async_config_entry_first_refresh()
```

Construct `AtmeexRuntimeData` with that same `state_store` instance and the Plan-3 command executor. Do not retain `state_update_lock`, `_ws_device_update_ts`, `_refresh_device_update_ts`, the duplicate `get_devices(fallback=True)` path, or direct dictionary merges in setup. Targeted refresh and WebSocket code from Plans 2 and 4 already call `state_store.apply_refresh`/`apply_websocket_delta` and publish `update.data` only when `update.changed` is true.

- [ ] **Step 4: Delete tests for superseded faulty behavior**

Remove these old expectations from `tests/test_coordinator.py` and replace their coverage with Tasks 1–4:

```text
test_update_data_preserves_offline_devices
test_fetch_devices_primary_network_error_falls_back
test_poll_does_not_overwrite_fresher_targeted_refresh_state
```

The first encoded permanent retention, the second encoded outage-as-empty
fallback, and the third encoded whole-device timestamps replaced by field
revisions. Keep (or rename)
`test_fetch_devices_primary_unexpected_exception_propagates`: a programming
error must still propagate from a direct `_async_update_data()` call and must
never become an empty success. Keep the API-error event throttling test too;
assert that its existing `message`, `status`, and `source` keys remain readable,
that `message` is sanitized, and that `operation` plus the suppression count are
present where applicable.

- [ ] **Step 5: Run the complete coordinator/inventory/platform subsystem**

Run: `.venv/bin/python -m pytest tests/test_inventory_semantics.py tests/test_detail_hydration.py tests/test_inventory_removal.py tests/test_inventory_age.py tests/test_entity_availability.py tests/test_entity_discovery.py tests/test_coordinator.py tests/test_setup.py tests/test_unload.py tests/test_websocket_integration.py tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py tests/test_sensor.py tests/test_binary_sensor.py -q`

Expected: PASS with no warning; failures preserve the last confirmed snapshot, two successful absences remove a device, continuous WebSocket updates cannot postpone inventory polling, and unchanged inventory causes no coordinator listener notification.

- [ ] **Step 6: Run the entire suite**

Run: `.venv/bin/python -m pytest -q`

Expected: exit code 0, no `RuntimeWarning`, no pending-task report, and no deprecated fallback/timestamp test names in the collected suite.

- [ ] **Step 7: Commit final composition cleanup**

```bash
git add custom_components/atmeex_cloud/__init__.py tests/test_setup.py tests/test_coordinator.py tests/test_websocket_integration.py
git commit -m "refactor: publish authoritative inventory snapshots"
```

## Final Verification Checklist

- [ ] Run `.venv/bin/python -m pytest tests/test_inventory_semantics.py tests/test_detail_hydration.py tests/test_inventory_removal.py tests/test_inventory_age.py -q` and confirm exit code 0.
- [ ] Run `.venv/bin/python -m pytest tests/test_entity_availability.py tests/test_entity_discovery.py tests/test_sensor.py tests/test_binary_sensor.py -q` and confirm exit code 0.
- [ ] Run `.venv/bin/python -m pytest -q` and confirm the full suite exits 0 without runtime warnings.
- [ ] Run `.venv/bin/python -m compileall -q custom_components/atmeex_cloud` and confirm exit code 0 with no output.
- [ ] Run `git diff --check` and confirm exit code 0 with no output.
- [ ] Run `git status --short` and confirm only the files intentionally changed by this plan are listed.
