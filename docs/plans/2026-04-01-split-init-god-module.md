# Split __init__.py God Module — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Break the ~813-line `__init__.py` into focused modules so that coordinator update logic, device refresh, WebSocket integration, and logbook throttling are individually testable and navigable.

**Architecture:** Extract in four layers, bottom-up — runtime data classes first (zero behaviour change), then coordinator update logic, then device refresh, then WebSocket integration. Each extraction is a pure move-and-re-export: existing tests must pass at every commit with zero modifications to test files (except adding new unit tests for the extracted modules). The `__init__.py` re-exports every public name so that `from custom_components.atmeex_cloud import AtmeexRuntimeData` etc. continue to work.

**Tech Stack:** Python 3.12, Home Assistant `DataUpdateCoordinator`, pytest + pytest-asyncio

**Baseline:** 170 tests, all passing. Run `python3 -m pytest -q` after every step.

**Import contract to preserve (non-negotiable):**
```python
# These all import from __init__.py and MUST NOT break:
from custom_components.atmeex_cloud import AtmeexRuntimeData       # 10+ files
from custom_components.atmeex_cloud import PendingCommand           # 2 test files
from custom_components.atmeex_cloud import EVENT_API_ERROR          # test_logbook
from custom_components.atmeex_cloud import EVENT_DEVICE_UPDATED     # test_logbook
from custom_components.atmeex_cloud import AtmeexCoordinator        # __all__
from custom_components.atmeex_cloud import AtmeexCoordinatorData    # test_diagnostics
from custom_components.atmeex_cloud import async_setup_entry        # test_refresh_device
# Also: test_init.py and test_refresh_device.py monkeypatch atmeex_init.AtmeexApi,
# atmeex_init.AtmeexCoordinator, atmeex_init.async_get_clientsession — these
# module-level names must remain on __init__.py for monkeypatching to work.
```

---

## Task 1: Extract `PendingCommand` + `AtmeexRuntimeData` → `runtime.py`

The data classes and their methods are pure (no HA imports, no coordinator interaction). Move them verbatim into a new module and re-export from `__init__.py`.

**Files:**
- Create: `custom_components/atmeex_cloud/runtime.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Test: `tests/test_runtime.py` (new, dedicated unit tests)

**Step 1: Write the failing test**

Create `tests/test_runtime.py` with a simple import-and-smoke test:

```python
"""Unit tests for runtime.py — extracted from __init__.py."""
import asyncio
import time

import pytest

from custom_components.atmeex_cloud.runtime import PendingCommand, AtmeexRuntimeData


def test_pending_command_fields():
    pc = PendingCommand(value=5, timestamp=1.0, attribute="fan_speed")
    assert pc.value == 5
    assert pc.attribute == "fan_speed"


def test_runtime_set_get_clear_pending():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    ts = rt.set_pending(1, "fan_speed", 7)
    assert isinstance(ts, float)

    p = rt.get_pending(1, "fan_speed")
    assert p is not None and p.value == 7

    rt.clear_pending(1, "fan_speed")
    assert rt.get_pending(1, "fan_speed") is None


def test_runtime_clear_pending_if_confirmed_matching():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "pwr_on", True)
    assert rt.clear_pending_if_confirmed(1, "pwr_on", True) is True
    assert rt.get_pending(1, "pwr_on") is None


def test_runtime_clear_pending_if_confirmed_stale():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "fan_speed", 7)
    assert rt.clear_pending_if_confirmed(1, "fan_speed", 3) is False
    assert rt.get_pending(1, "fan_speed") is not None


def test_runtime_clear_pending_if_confirmed_expired():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    rt.set_pending(1, "fan_speed", 7)
    # Backdate the timestamp
    rt.pending_commands["1"]["fan_speed"] = PendingCommand(
        value=7, timestamp=time.monotonic() - 20.0, attribute="fan_speed"
    )
    assert rt.clear_pending_if_confirmed(1, "fan_speed", 3, tolerance=5.0) is True


def test_device_lock_identity():
    rt = AtmeexRuntimeData(api=None, coordinator=None, refresh_device=None)
    lock_a = rt.get_device_lock(1)
    lock_b = rt.get_device_lock(1)
    lock_c = rt.get_device_lock(2)
    assert lock_a is lock_b
    assert lock_a is not lock_c
    assert isinstance(lock_a, asyncio.Lock)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.atmeex_cloud.runtime'`

**Step 3: Create `runtime.py` with the extracted classes**

Create `custom_components/atmeex_cloud/runtime.py`:

```python
"""Runtime data structures for the Atmeex Cloud integration.

These are pure data classes with no Home Assistant dependencies beyond
typing — safe to import from anywhere.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

_LOGGER = logging.getLogger(__name__)


@dataclass
class PendingCommand:
    """Tracks a pending command to prevent stale state overwrites."""
    value: Any
    timestamp: float
    attribute: str  # e.g., "fan_speed", "pwr_on"


@dataclass
class AtmeexRuntimeData:
    """Единый runtime-объект для записи конфигурации."""
    api: Any  # AtmeexApi
    coordinator: Any  # AtmeexCoordinator
    refresh_device: Callable[[int | str], Awaitable[None]] | None
    # Per-device locks to serialize set+refresh operations
    device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    # Per-device pending commands: device_id -> {attribute -> PendingCommand}
    pending_commands: dict[str, dict[str, PendingCommand]] = field(default_factory=dict)
    # WebSocket manager for real-time updates (optional, can be None for HTTP-only mode)
    websocket_manager: Any = None  # WebSocketManager | None
    # Task that performs initial WebSocket startup/retry bootstrap.
    websocket_start_task: asyncio.Task[None] | None = None
    # Serialized task for queued websocket state updates.
    websocket_message_task: asyncio.Task[None] | None = None

    def get_device_lock(self, device_id: int | str) -> asyncio.Lock:
        """Get or create a lock for the given device."""
        key = str(device_id)
        if key not in self.device_locks:
            self.device_locks[key] = asyncio.Lock()
        return self.device_locks[key]

    def set_pending(self, device_id: int | str, attribute: str, value: Any) -> float:
        """Record a pending command. Returns the timestamp."""
        key = str(device_id)
        ts = time.monotonic()
        if key not in self.pending_commands:
            self.pending_commands[key] = {}
        self.pending_commands[key][attribute] = PendingCommand(
            value=value, timestamp=ts, attribute=attribute
        )
        _LOGGER.debug(
            "Pending command set: device=%s attr=%s value=%s ts=%.3f",
            device_id, attribute, value, ts
        )
        return ts

    def get_pending(self, device_id: int | str, attribute: str) -> PendingCommand | None:
        """Get pending command if exists."""
        key = str(device_id)
        return self.pending_commands.get(key, {}).get(attribute)

    def clear_pending(self, device_id: int | str, attribute: str) -> None:
        """Clear a pending command after confirmation."""
        key = str(device_id)
        if key in self.pending_commands and attribute in self.pending_commands[key]:
            del self.pending_commands[key][attribute]
            _LOGGER.debug("Pending command cleared: device=%s attr=%s", device_id, attribute)

    def clear_pending_if_confirmed(
        self, device_id: int | str, attribute: str, confirmed_value: Any, tolerance: float = 5.0
    ) -> bool:
        """Clear pending if device confirmed the value or TTL expired.

        Returns True if the confirmed_value should be used (no stale pending).
        Returns False if there's a newer pending command that should take precedence.
        """
        pending = self.get_pending(device_id, attribute)
        if pending is None:
            return True  # No pending, use confirmed value

        age = time.monotonic() - pending.timestamp

        # If pending command is too old, clear it and use confirmed
        if age > tolerance:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command expired: device=%s attr=%s age=%.1fs",
                device_id, attribute, age
            )
            return True

        # If device confirmed our pending value, clear it
        if pending.value == confirmed_value:
            self.clear_pending(device_id, attribute)
            _LOGGER.debug(
                "Pending command confirmed: device=%s attr=%s value=%s",
                device_id, attribute, confirmed_value
            )
            return True

        # Pending command is newer than this response - ignore stale data
        _LOGGER.debug(
            "Ignoring stale value: device=%s attr=%s confirmed=%s pending=%s age=%.1fs",
            device_id, attribute, confirmed_value, pending.value, age
        )
        return False
```

**Step 4: Update `__init__.py` to import from `runtime.py` and re-export**

In `__init__.py`, replace the `PendingCommand` class (lines 41-46), the entire `AtmeexRuntimeData` class (lines 49-136), and the related imports (`time`, `dataclasses`, `Callable`, `Awaitable`) with:

```python
from .runtime import PendingCommand, AtmeexRuntimeData
```

Keep all other code in `__init__.py` unchanged. The `__all__` list already exports these names.

Specifically:
1. Remove `import time` only if it is no longer used elsewhere in `__init__.py` (it IS still used in `_async_update_data` and `_fire_api_error_event` etc., so **keep it**).
2. Remove `from dataclasses import dataclass, field` — no longer needed in `__init__.py` (verify no other usage).
3. Remove `from typing import Any, Callable, Awaitable` — check if `Any` is still used (yes, heavily). Keep `Any`. Remove `Callable, Awaitable` only if unused.
4. Remove the entire `PendingCommand` dataclass definition (lines 41-46).
5. Remove the entire `AtmeexRuntimeData` dataclass definition (lines 49-136).
6. Add `from .runtime import PendingCommand, AtmeexRuntimeData` after the existing internal imports.

**Step 5: Run full test suite to verify no breakage**

Run: `python3 -m pytest -q`
Expected: 170 + 6 new = 176 passed

**Step 6: Commit**

```bash
git add custom_components/atmeex_cloud/runtime.py tests/test_runtime.py custom_components/atmeex_cloud/__init__.py
git commit -m "refactor: extract PendingCommand + AtmeexRuntimeData → runtime.py"
```

---

## Task 2: Move `_fetch_devices_safely` + `_async_update_data` into `AtmeexCoordinator`

The coordinator's `update_method` closure (`_async_update_data`, lines 292-404) and its helper (`_fetch_devices_safely`, lines 228-290) capture `api`, `coordinator`, `_fire_api_error_event`, and `_ws_device_update_ts`. Moving them into the `AtmeexCoordinator` class makes the update logic a proper method that can be tested with a mock API — no need to run full `async_setup_entry`.

**Files:**
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Test: `tests/test_coordinator.py` (new)

**Step 1: Write the failing test**

Create `tests/test_coordinator.py`:

```python
"""Unit tests for AtmeexCoordinator._async_update_data."""
import logging
import time

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud.api import AtmeexDevice, ApiError
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator


def _make_coordinator(devices=None, get_device_side_effect=None):
    """Create a coordinator with a fake API for testing update logic."""
    dev_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
               "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    default_dev = AtmeexDevice.from_raw(dev_raw)
    devices = devices if devices is not None else [default_dev]

    api = MagicMock()
    api.get_devices = AsyncMock(return_value=devices)
    api.get_device = AsyncMock(
        side_effect=get_device_side_effect or (lambda did: default_dev)
    )
    api._retry_count = 0

    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
    )
    coord = AtmeexCoordinator(
        hass, logging.getLogger("test"), name="test",
        update_interval=None,
    )
    coord.setup_update(api=api, fire_logbook_event=MagicMock())
    return coord, api


@pytest.mark.asyncio
async def test_update_data_builds_states():
    coord, api = _make_coordinator()
    data = await coord._async_update_data()
    assert "1" in data["states"]
    assert data["states"]["1"]["pwr_on"] is True
    assert data["device_map"]["1"].id == 1
    assert coord.last_api_error is None


@pytest.mark.asyncio
async def test_update_data_sets_last_api_error_on_failure():
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(side_effect=ApiError("boom", status=500))

    from homeassistant.helpers.update_coordinator import UpdateFailed
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert isinstance(coord.last_api_error, ApiError)


@pytest.mark.asyncio
async def test_update_data_preserves_offline_devices():
    """Devices from previous poll that disappear from API should be preserved."""
    dev1_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
                "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    dev2_raw = {"id": 2, "name": "Dev2", "model": "m", "online": True,
                "condition": {"pwr_on": 0, "fan_speed": 1}, "settings": {}}
    dev1 = AtmeexDevice.from_raw(dev1_raw)
    dev2 = AtmeexDevice.from_raw(dev2_raw)

    coord, api = _make_coordinator(devices=[dev1, dev2])

    # First poll — both devices
    data1 = await coord._async_update_data()
    coord.data = data1
    coord.last_update_success = True
    assert "1" in data1["device_map"]
    assert "2" in data1["device_map"]

    # Second poll — dev2 disappeared
    api.get_devices = AsyncMock(return_value=[dev1])
    api.get_device = AsyncMock(return_value=dev1)
    data2 = await coord._async_update_data()
    # dev2 should still be in device_map from merge
    assert "2" in data2["device_map"]
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_coordinator.py -v`
Expected: FAIL — `AttributeError: 'AtmeexCoordinator' object has no attribute 'setup_update'`

**Step 3: Move update logic into `AtmeexCoordinator`**

Edit `custom_components/atmeex_cloud/coordinator.py`. Add three things:

1. Necessary imports at the top:
```python
import time
from typing import Any, Callable

import aiohttp
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed

from .api import AtmeexApi, AtmeexDevice, AtmeexState
```

2. A `setup_update` method that stores the dependencies the update logic needs:
```python
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
        # Per-device monotonic timestamp of last WS state update — used to
        # prevent polling from overwriting fresher WS data.
        self._ws_device_update_ts: dict[str, float] = {}
        # Throttle API-error logbook events to avoid flooding during outages.
        self._api_error_last_ts: float = float("-inf")
        self._api_error_suppressed: int = 0
```

3. Move `_fetch_devices_safely`, `_fire_api_error_event`, and `_async_update_data` as methods on the class, replacing closure variables with `self._api`, `self._fire_logbook_event`, etc.

The `_async_update_data` method should match the signature `async def _async_update_data(self) -> AtmeexCoordinatorData:` and be set as `update_method=self._async_update_data` when the coordinator is constructed.

Key renames inside the methods:
- `api` → `self._api`
- `coordinator.last_api_error` → `self.last_api_error`
- `coordinator.last_success_ts` → `self.last_success_ts`
- `coordinator.data` → `self.data`
- `coordinator.last_update_success` → `self.last_update_success`
- `_fire_api_error_event(...)` → `self._fire_api_error_event(...)`
- `_fire_logbook_event(...)` → `self._fire_logbook_event(...)`
- `_ws_device_update_ts` → `self._ws_device_update_ts`

The `_fire_api_error_event` method moves here too (it was a closure in `__init__.py` capturing `_api_error_last_ts` and `_api_error_suppressed`):

```python
    def _fire_api_error_event(self, data: dict[str, Any]) -> None:
        now = time.monotonic()
        from .const import WS_LOGBOOK_MIN_INTERVAL_SEC
        if now - self._api_error_last_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            self._api_error_suppressed += 1
            return
        if self._api_error_suppressed:
            data = {**data, "suppressed_errors": self._api_error_suppressed}
            self._api_error_suppressed = 0
        self._fire_logbook_event(EVENT_API_ERROR, data)
        self._api_error_last_ts = now
```

Wait — actually to avoid circular imports, import `EVENT_API_ERROR` from const:

```python
from .const import EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC
```

**Step 4: Update `__init__.py`**

In `async_setup_entry`:

1. Remove the `_fire_api_error_event` closure (lines 208-221).
2. Remove `_api_error_last_ts` and `_api_error_suppressed` variables (lines 208-209).
3. Remove the `_fetch_devices_safely` nested function (lines 228-290).
4. Remove the `_async_update_data` nested function (lines 292-404).
5. Remove `_ws_device_update_ts` variable (line 428).
6. Keep the `_fire_logbook_event` helper — it's still needed by the WebSocket block.

Change coordinator construction from:
```python
coordinator = AtmeexCoordinator(
    hass, _LOGGER, name="Atmeex Cloud",
    update_method=_async_update_data,
    update_interval=timedelta(seconds=update_interval_seconds),
)
```
to:
```python
coordinator = AtmeexCoordinator(
    hass, _LOGGER, name="Atmeex Cloud",
    update_interval=timedelta(seconds=update_interval_seconds),
)
coordinator.setup_update(api=api, fire_logbook_event=_fire_logbook_event)
```

The coordinator's `__init__` passes `update_method=self._async_update_data` to `super().__init__()` when `setup_update` has been called. But since `_async_update_data` is just a method, we can pass it at construction time:

Actually, simpler: override `__init__` to always pass `update_method=self._async_update_data`:

```python
def __init__(self, hass, logger, **kwargs):
    kwargs.setdefault("update_method", self._async_update_data)
    super().__init__(hass, logger, **kwargs)
    self.last_api_error = None
    self.last_success_ts = None
```

And `setup_update` just stores the injected dependencies.

Also update the `_apply_websocket_message` closure inside `__init__.py` — it references `_ws_device_update_ts` which now lives on the coordinator. Change:
```python
_ws_device_update_ts[did] = ws_now
```
to:
```python
coordinator._ws_device_update_ts[did] = ws_now
```

And for `_refresh_device_once` which calls `_fire_api_error_event` — that now lives on coordinator:
```python
coordinator._fire_api_error_event({...})
```

**Step 5: Run full test suite**

Run: `python3 -m pytest -q`
Expected: 176 + 3 new = 179 passed (all existing tests untouched)

**Step 6: Commit**

```bash
git add custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests/test_coordinator.py
git commit -m "refactor: move _async_update_data + _fetch_devices_safely into AtmeexCoordinator"
```

---

## Task 3: Extract `_refresh_device_once` + `refresh_device` → coordinator methods

The device refresh logic (`_refresh_device_once` lines 468-552, `refresh_device` lines 554-573) is tightly coupled to coordinator state. Move it into `AtmeexCoordinator` as methods. This also moves `state_update_lock` and `refresh_tasks` into the coordinator.

**Files:**
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Test: `tests/test_coordinator.py` (extend)

**Step 1: Write the failing test**

Append to `tests/test_coordinator.py`:

```python
@pytest.mark.asyncio
async def test_refresh_device_merges_into_coordinator_data():
    """refresh_device should fetch one device and merge its state."""
    dev_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
               "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    dev = AtmeexDevice.from_raw(dev_raw)
    coord, api = _make_coordinator(devices=[dev])

    # First full poll
    data = await coord._async_update_data()
    coord.data = data
    assert data["states"]["1"]["pwr_on"] is True

    # Now simulate the device turning off
    dev_off_raw = {**dev_raw, "condition": {"pwr_on": 0, "fan_speed": 2}}
    dev_off = AtmeexDevice.from_raw(dev_off_raw)
    api.get_device = AsyncMock(return_value=dev_off)

    await coord.refresh_device(1)

    assert coord.data["states"]["1"]["pwr_on"] is False


@pytest.mark.asyncio
async def test_refresh_device_coalesces():
    """Two concurrent refresh_device calls for the same device should make only one API call."""
    import asyncio
    gate = asyncio.Event()

    dev_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
               "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    dev = AtmeexDevice.from_raw(dev_raw)

    call_count = 0
    async def _slow_get_device(device_id):
        nonlocal call_count
        call_count += 1
        await gate.wait()
        return dev

    coord, api = _make_coordinator(devices=[dev])
    api.get_device = AsyncMock(side_effect=_slow_get_device)

    data = await coord._async_update_data()
    coord.data = data

    t1 = asyncio.create_task(coord.refresh_device(1))
    t2 = asyncio.create_task(coord.refresh_device(1))
    await asyncio.sleep(0)  # let both start
    gate.set()
    await asyncio.gather(t1, t2)

    assert call_count == 1
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_coordinator.py::test_refresh_device_merges_into_coordinator_data -v`
Expected: FAIL — `AttributeError: 'AtmeexCoordinator' object has no attribute 'refresh_device'`

**Step 3: Move refresh logic into coordinator**

Add to `AtmeexCoordinator` in `coordinator.py`:

1. Add `state_update_lock` and `refresh_tasks` as instance attributes in `__init__`:
```python
self._state_update_lock = asyncio.Lock()
self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
```

2. Add `_refresh_device_once` method — move the body from `__init__.py` lines 468-552, replacing:
   - `api.get_device(device_id)` → `self._api.get_device(device_id)`
   - `_fire_api_error_event(...)` → `self._fire_api_error_event(...)`
   - `async with state_update_lock:` → `async with self._state_update_lock:`
   - `coordinator.data` → `self.data`
   - `coordinator.async_set_updated_data(...)` → `self.async_set_updated_data(...)`
   - `_fire_logbook_event(...)` → `self._fire_logbook_event(...)`

3. Add `refresh_device` method — move from `__init__.py` lines 554-573, replacing:
   - `refresh_tasks` → `self._refresh_tasks`

**Step 4: Update `__init__.py`**

1. Remove the `_refresh_device_once` and `refresh_device` nested functions.
2. Remove `refresh_tasks` and `state_update_lock` local variables (lines 419-420).
3. Change the `AtmeexRuntimeData` construction to use `coordinator.refresh_device`:
```python
runtime_data = AtmeexRuntimeData(
    api=api,
    coordinator=coordinator,
    refresh_device=coordinator.refresh_device,
    ...
)
```
4. In `_apply_websocket_message`, replace `async with state_update_lock:` with `async with coordinator._state_update_lock:`.

**Step 5: Run full test suite**

Run: `python3 -m pytest -q`
Expected: 179 + 2 new = 181 passed

Important: `test_refresh_device.py` and `test_init.py::test_refresh_device_coalesces_parallel_requests` must still pass without modification — they go through `async_setup_entry` and exercise `runtime.refresh_device` which now delegates to `coordinator.refresh_device`.

**Step 6: Commit**

```bash
git add custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests/test_coordinator.py
git commit -m "refactor: move refresh_device + state_update_lock into AtmeexCoordinator"
```

---

## Task 4: Extract WebSocket integration → `websocket_setup.py`

The WebSocket block (lines 579-754 of the original `__init__.py`) contains `_apply_websocket_message`, `_drain_websocket_messages`, `on_websocket_message`, `_on_ws_auth_failure`, `_start_websocket`, and all setup logic. Extract it into a factory function in a new module.

**Files:**
- Create: `custom_components/atmeex_cloud/websocket_setup.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Test: `tests/test_websocket_setup.py` (new)

**Step 1: Write the failing test**

Create `tests/test_websocket_setup.py`:

```python
"""Unit tests for websocket_setup.py — WebSocket integration factory."""
import asyncio
import logging

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator
from custom_components.atmeex_cloud.websocket_setup import setup_websocket_integration


def _make_coordinator_with_data():
    dev_raw = {"id": 1, "name": "Dev1", "model": "m", "online": True,
               "condition": {"pwr_on": 1, "fan_speed": 2}, "settings": {}}
    dev = AtmeexDevice.from_raw(dev_raw)
    api = MagicMock()
    api.get_devices = AsyncMock(return_value=[dev])
    api.get_device = AsyncMock(return_value=dev)
    api._retry_count = 0

    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))
    coord = AtmeexCoordinator(
        hass, logging.getLogger("test"), name="test", update_interval=None,
    )
    coord.setup_update(api=api, fire_logbook_event=MagicMock())
    return coord, api, hass


@pytest.mark.asyncio
async def test_setup_returns_none_when_disabled():
    coord, api, hass = _make_coordinator_with_data()
    result = setup_websocket_integration(
        hass=hass,
        coordinator=coord,
        api=api,
        entry=SimpleNamespace(async_start_reauth=MagicMock()),
        session=MagicMock(spec=[]),  # no ws_connect
        enable=False,
    )
    assert result == (None, None)


@pytest.mark.asyncio
async def test_setup_returns_none_when_session_lacks_ws_connect():
    coord, api, hass = _make_coordinator_with_data()
    api._token = "tok"
    result = setup_websocket_integration(
        hass=hass,
        coordinator=coord,
        api=api,
        entry=SimpleNamespace(async_start_reauth=MagicMock()),
        session=MagicMock(spec=[]),  # no ws_connect
        enable=True,
    )
    assert result == (None, None)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_websocket_setup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'custom_components.atmeex_cloud.websocket_setup'`

**Step 3: Create `websocket_setup.py`**

Create `custom_components/atmeex_cloud/websocket_setup.py`:

Move the entire WebSocket block into a single factory function:

```python
"""WebSocket integration setup for Atmeex Cloud.

Extracted from __init__.py to keep async_setup_entry focused on orchestration.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any

from .const import EVENT_DEVICE_UPDATED, WS_LOGBOOK_MIN_INTERVAL_SEC
from .helpers import apply_condition_update, apply_settings_update

_LOGGER = logging.getLogger(__name__)


def setup_websocket_integration(
    *,
    hass: Any,
    coordinator: Any,
    api: Any,
    entry: Any,
    session: Any,
    enable: bool,
) -> tuple[Any, asyncio.Task[None] | None]:
    """Set up WebSocket real-time updates. Returns (manager, start_task).

    Returns (None, None) when WebSocket is disabled or unavailable.
    """
    if not enable:
        _LOGGER.info("WebSocket disabled in options, using HTTP polling only")
        return None, None

    try:
        from .websocket import WebSocketManager
    except ImportError:
        _LOGGER.warning("WebSocket module not available, using HTTP polling only")
        return None, None

    if not hasattr(session, "ws_connect"):
        _LOGGER.warning("WebSocket skipped: HTTP session has no ws_connect()")
        return None, None

    if not api.token:
        _LOGGER.warning("WebSocket skipped: API token is unavailable")
        return None, None

    # --- mutable state for the message queue ---
    message_queue: deque[dict[str, Any]] = deque(maxlen=500)
    _ws_task_ref: dict[str, asyncio.Task[None] | None] = {"task": None}
    # Mutable container so we can update runtime_data.websocket_message_task later
    _runtime_ref: dict[str, Any] = {"runtime": None}

    ws_logbook_last_event_ts: float = float("-inf")
    ws_logbook_suppressed_updates: int = 0

    def _fire_logbook_event(event_type: str, data: dict[str, Any]) -> None:
        bus = getattr(hass, "bus", None)
        if bus is None or not hasattr(bus, "async_fire"):
            return
        bus.async_fire(event_type, data)

    def _fire_websocket_device_updated(
        changed_device_ids: list[str], msg_type: Any,
    ) -> None:
        nonlocal ws_logbook_last_event_ts, ws_logbook_suppressed_updates
        if not changed_device_ids:
            return
        now = time.monotonic()
        if now - ws_logbook_last_event_ts < WS_LOGBOOK_MIN_INTERVAL_SEC:
            ws_logbook_suppressed_updates += len(changed_device_ids)
            return
        cur_map = (coordinator.data or {}).get("device_map", {})
        device_names = [
            cur_map[did].name
            for did in changed_device_ids
            if did in cur_map and hasattr(cur_map[did], "name")
        ]
        payload: dict[str, Any] = {
            "device_ids": changed_device_ids,
            "device_names": device_names or None,
            "source": "websocket",
            "message_type": msg_type,
        }
        if ws_logbook_suppressed_updates:
            payload["suppressed_updates"] = ws_logbook_suppressed_updates
            ws_logbook_suppressed_updates = 0
        _fire_logbook_event(EVENT_DEVICE_UPDATED, payload)
        ws_logbook_last_event_ts = now

    async def _apply_websocket_message(message: dict[str, Any]) -> None:
        msg_type = message.get("type")
        if msg_type not in ("condition", "settings"):
            _LOGGER.debug("WebSocket message type '%s' ignored", msg_type)
            return

        payload = message.get("data")
        if not isinstance(payload, list):
            _LOGGER.warning("WebSocket message has unexpected data format: %s", payload)
            return

        async with coordinator._state_update_lock:
            cur = coordinator.data or {
                "devices": [], "device_map": {}, "states": {},
                "last_success_ts": None, "avg_latency_ms": None, "request_retries": 0,
            }
            device_map = cur.get("device_map", {}) or {}
            if not device_map:
                return

            states: dict[str, dict[str, Any]] = dict(cur.get("states", {}))
            changed = False
            changed_device_ids: list[str] = []

            for device_data in payload:
                if not isinstance(device_data, dict):
                    continue
                device_id = device_data.get("id")
                if device_id is None:
                    continue
                key = str(device_id)
                if key not in device_map:
                    _LOGGER.debug(
                        "WebSocket: device %s not in current map, skipping message",
                        device_id,
                    )
                    continue

                if msg_type == "condition":
                    source = device_data.get("condition")
                    if not isinstance(source, dict) or not source:
                        continue
                    updated_state = apply_condition_update(states.get(key, {}), source)
                else:
                    source = device_data.get("settings")
                    if not isinstance(source, dict) or not source:
                        continue
                    updated_state = apply_settings_update(states.get(key, {}), source)

                if updated_state != states.get(key, {}):
                    states[key] = updated_state
                    changed = True
                    changed_device_ids.append(key)

            if not changed:
                return

            ws_now = time.monotonic()
            for did in changed_device_ids:
                coordinator._ws_device_update_ts[did] = ws_now

            coordinator.async_set_updated_data({
                "devices": cur.get("devices", []),
                "device_map": dict(device_map),
                "states": states,
                "last_success_ts": cur.get("last_success_ts"),
                "avg_latency_ms": cur.get("avg_latency_ms"),
                "request_retries": cur.get("request_retries", 0),
            })
            _fire_websocket_device_updated(changed_device_ids, msg_type)

    async def _drain_websocket_messages() -> None:
        try:
            while message_queue:
                message = message_queue.popleft()
                try:
                    await _apply_websocket_message(message)
                except Exception as err:
                    _LOGGER.error("Error processing WebSocket message: %s", err)
        finally:
            _ws_task_ref["task"] = None
            runtime = _runtime_ref.get("runtime")
            if runtime is not None:
                runtime.websocket_message_task = None

    def on_websocket_message(message: dict[str, Any]) -> None:
        message_queue.append(message)
        task = _ws_task_ref["task"]
        if task and not task.done():
            return
        new_task = hass.async_create_task(_drain_websocket_messages())
        _ws_task_ref["task"] = new_task
        runtime = _runtime_ref.get("runtime")
        if runtime is not None:
            runtime.websocket_message_task = new_task

    ws_reauth_started = False

    def _on_ws_auth_failure() -> None:
        nonlocal ws_reauth_started
        if ws_reauth_started:
            return
        ws_reauth_started = True
        _LOGGER.warning("WebSocket auth rejected; starting config-entry reauth flow")
        start_reauth = getattr(entry, "async_start_reauth", None)
        if not callable(start_reauth):
            _LOGGER.error(
                "Config entry has no async_start_reauth; WS auth failure cannot trigger reauth"
            )
            return
        try:
            start_reauth(hass)
        except Exception as err:
            _LOGGER.error("Failed to start reauth after WebSocket auth failure: %s", err)

    try:
        manager = WebSocketManager(
            session=session,
            token_getter=lambda: api.token,
            on_message=on_websocket_message,
            on_auth_failure=_on_ws_auth_failure,
            on_token_refresh=coordinator.async_request_refresh,
        )
    except Exception as err:
        _LOGGER.warning("Failed to initialize WebSocket: %s. Using HTTP polling only.", err)
        return None, None

    async def _start_websocket() -> None:
        try:
            success = await manager.connect()
            if success:
                _LOGGER.info("WebSocket connected for real-time updates")
            else:
                _LOGGER.warning(
                    "WebSocket bootstrap failed, reconnect loop will continue in background"
                )
        except Exception as err:
            _LOGGER.warning(
                "Failed to start WebSocket: %s. Using HTTP polling only.", err,
            )

    start_task = hass.async_create_task(_start_websocket())

    def set_runtime(runtime: Any) -> None:
        """Allow __init__.py to pass runtime_data after creation."""
        _runtime_ref["runtime"] = runtime

    # Attach the setter so __init__.py can call it
    manager._set_runtime = set_runtime

    return manager, start_task
```

**Step 4: Update `__init__.py`**

Replace the entire WebSocket block (everything from `websocket_manager = None` through the end of the websocket if/else/except) with:

```python
    from .websocket_setup import setup_websocket_integration

    session_for_ws = async_get_clientsession(hass)
    websocket_manager, websocket_start_task = setup_websocket_integration(
        hass=hass,
        coordinator=coordinator,
        api=api,
        entry=entry,
        session=session_for_ws,
        enable=enable_websocket,
    )
```

After `runtime_data` is created:
```python
    if websocket_manager is not None and hasattr(websocket_manager, '_set_runtime'):
        websocket_manager._set_runtime(runtime_data)
```

Also remove from `__init__.py`:
- `_fire_websocket_device_updated` closure
- `_apply_websocket_message` closure
- `_drain_websocket_messages` closure
- `on_websocket_message` closure
- `_on_ws_auth_failure` closure
- `_start_websocket` closure
- `websocket_message_queue` variable
- `_ws_task_ref` variable
- `ws_logbook_last_event_ts` and `ws_logbook_suppressed_updates` variables
- `_create_background_task` helper (if only used by WS block)

The `session` variable was already created at the top of `async_setup_entry` — reuse it (it's the same `async_get_clientsession(hass)`).

**Step 5: Run full test suite**

Run: `python3 -m pytest -q`
Expected: 181 + 2 new = 183 passed

Critical: `test_init.py::test_websocket_batch_message_updates_coordinator_once` MUST still pass — it exercises the full WebSocket flow through `async_setup_entry`.

**Step 6: Commit**

```bash
git add custom_components/atmeex_cloud/websocket_setup.py custom_components/atmeex_cloud/__init__.py tests/test_websocket_setup.py
git commit -m "refactor: extract WebSocket integration setup → websocket_setup.py"
```

---

## Task 5: Move `_fire_logbook_event` into coordinator + clean up `__init__.py`

After Tasks 1-4, `async_setup_entry` should be ~60-80 lines: login, create coordinator, first refresh, WS setup, build runtime, forward platforms. This task cleans up any remaining loose ends and verifies final line counts.

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- No new test file — just verify existing suite

**Step 1: Audit remaining closures in `__init__.py`**

After Tasks 1-4, the following should remain in `async_setup_entry`:
- `_fire_logbook_event` (4 lines) — used by `coordinator.setup_update(fire_logbook_event=...)`. Move it into the coordinator's `setup_update` method, since the coordinator is the only remaining consumer (WebSocket setup has its own copy).
- `_update_listener` (2 lines) — fine to keep inline, it's trivial.

Move `_fire_logbook_event` into `AtmeexCoordinator.setup_update`:

```python
def setup_update(self, *, api, hass) -> None:
    self._api = api
    self._hass = hass
    ...

def _fire_logbook_event(self, event_type: str, data: dict[str, Any]) -> None:
    bus = getattr(self._hass, "bus", None)
    if bus is None or not hasattr(bus, "async_fire"):
        return
    bus.async_fire(event_type, data)
```

Change the `setup_update` signature from `fire_logbook_event=...` to `hass=hass`.

**Step 2: Clean up imports in `__init__.py`**

Remove any imports that are no longer needed:
- `from collections import deque` — only used by WS block (now in websocket_setup.py)
- `from .helpers import apply_condition_update, apply_settings_update` — only used by WS block
- `aiohttp` — check if still needed (may still be in fetch logic... no, that's in coordinator now)
- `from homeassistant.helpers.update_coordinator import UpdateFailed` — now in coordinator
- Various const imports that moved to coordinator/websocket_setup

**Step 3: Run full test suite**

Run: `python3 -m pytest -q`
Expected: 183 passed (no new tests, no test changes)

**Step 4: Verify line count reduction**

Run: `wc -l custom_components/atmeex_cloud/__init__.py`
Expected: ~100-130 lines (down from 813)

Run: `wc -l custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/runtime.py custom_components/atmeex_cloud/websocket_setup.py`
Verify the total is roughly the same as before (code moved, not deleted).

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/coordinator.py
git commit -m "refactor: final cleanup — move _fire_logbook_event to coordinator, trim __init__.py imports"
```

---

## Summary of expected final module structure

| Module | Responsibility | Approx lines |
|--------|---------------|-------------|
| `__init__.py` | Login, wire up coordinator, WS, runtime, forward platforms | ~100-130 |
| `runtime.py` | `PendingCommand`, `AtmeexRuntimeData` | ~110 |
| `coordinator.py` | `AtmeexCoordinator`, `_async_update_data`, `_fetch_devices_safely`, `refresh_device`, logbook throttling | ~280 |
| `websocket_setup.py` | `setup_websocket_integration` factory with message queue, apply logic | ~200 |
| `websocket.py` | `WebSocketManager` (unchanged) | ~353 |

## Expected test result at every commit

170 baseline → always green, never modified. Only new test files added.
