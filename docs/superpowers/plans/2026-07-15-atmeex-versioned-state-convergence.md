# Atmeex Versioned State Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that an Atmeex poll or targeted refresh can apply unrelated fresh fields but can never overwrite a field changed after that request began.

**Architecture:** Add one copy-on-write `AtmeexStateStore` as the only writer of the coordinator snapshot. Every device/state field carries a monotonic revision; HTTP work captures immutable baselines before I/O, WebSocket deltas always advance the fields they contain, and response merges accept only fields whose revisions still match their baseline. The store also owns the two-success authoritative-inventory absence counter and publishes the existing `devices`, `device_map`, and `states` shape with canonical string map keys.

**Tech Stack:** Python 3.12+, Home Assistant 2024.8+ `DataUpdateCoordinator`, `dataclasses`, `MappingProxyType`, `pytest`, `pytest-asyncio`, deterministic `asyncio.Event` barriers.

---

## File Map

- Create `custom_components/atmeex_cloud/state_store.py` — own immutable baselines, per-field revisions, copy-on-write snapshots, all three merge paths, and absence tracking.
- Modify `custom_components/atmeex_cloud/helpers.py:168-245` — normalize partial condition/settings messages into isolated state and raw-device deltas without accepting unknown boolean literals.
- Modify `custom_components/atmeex_cloud/coordinator.py:20-263` — capture poll baselines before I/O, publish store snapshots, move timing to properties, and remove whole-device timestamp guards.
- Modify `custom_components/atmeex_cloud/runtime.py:25-40` — give runtime an explicit `state_store` ownership field.
- Modify `custom_components/atmeex_cloud/__init__.py:133-398` — construct/inject the store and route targeted refresh and WebSocket writes through it.
- Create `tests/test_state_store.py` — unit-test contracts, copy-on-write behavior, field races, metadata races, and two-success removal.
- Modify `tests/test_helpers.py:1-92` and `tests/test_coordinator.py:16-149` — test isolated WebSocket parsing and the `asyncio.Event` poll/WebSocket race.
- Modify `tests/test_setup.py:19-117` and `tests/test_refresh_device.py:1-100` — assert store ownership and the `asyncio.Event` targeted-refresh/WebSocket race.

## Fixed Public Interfaces

Use frozen `FieldRevisionBaseline(device_id: str, revisions: Mapping[str, int])`, frozen `StateStoreUpdate(data: AtmeexCoordinatorData, changed: bool, removed_device_ids: frozenset[str] = frozenset())`, and `AtmeexStateStore(initial: AtmeexCoordinatorData | None = None)` with `data`, `capture_device`, `capture_all`, `apply_websocket_delta`, `apply_refresh`, and `apply_inventory`. Do not rename these symbols in later plans.

Field revision keys use `state.<field>`, `device.<field>`, `device.condition.<field>`, and `device.settings.<field>`. Canonical store/map keys are strings. Existing entity unique IDs remain unchanged because their formatted ID text remains identical.

## Execution Gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require all
tests to pass. Until Plans 4 and 6 land, only the documented WebSocket-startup
RuntimeWarning and pytest-asyncio loop-scope notice may remain; no state-store
task may introduce another warning.

### Task 1: Add Immutable Baselines and an Empty Comparable Snapshot

**Files:**
- Create: `custom_components/atmeex_cloud/state_store.py`
- Create: `tests/test_state_store.py`

- [ ] **Step 1: Write the baseline contract test**

Create `tests/test_state_store.py` with:

```python
from types import MappingProxyType
import pytest
from custom_components.atmeex_cloud.api import AtmeexDevice, AtmeexState
from custom_components.atmeex_cloud.state_store import (
    AtmeexStateStore,
    FieldRevisionBaseline,
    StateStoreUpdate,
)
def device(
    device_id: int | str = 1,
    *,
    name: str = "Breezer",
    pwr_on: int = 1,
    fan_speed: int = 2,
    temp_in: int = 180,
) -> AtmeexDevice:
    return AtmeexDevice.from_raw(
        {
            "id": device_id,
            "name": name,
            "model": "Atmeex",
            "online": True,
            "condition": {
                "pwr_on": pwr_on,
                "fan_speed": fan_speed,
                "temp_in": temp_in,
            },
            "settings": {},
        }
    )
def seed_store(*devices: AtmeexDevice) -> AtmeexStateStore:
    device_map = {str(item.id): item for item in devices}
    return AtmeexStateStore(
        {
            "devices": [item.to_ha_dict() for item in devices],
            "device_map": device_map,
            "states": {
                key: AtmeexState.from_device_dict(item.to_ha_dict()).to_ha_dict()
                for key, item in device_map.items()
            },
        }
    )
def test_empty_store_contract_and_immutable_baseline():
    store = AtmeexStateStore()

    assert store.data == {"devices": [], "device_map": {}, "states": {}}
    baseline = store.capture_device(1)
    assert baseline == FieldRevisionBaseline("1", MappingProxyType({}))
    with pytest.raises(TypeError):
        baseline.revisions["state.pwr_on"] = 7
    assert store.capture_all() == {}
```

- [ ] **Step 2: Run the baseline test and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py`

Expected: FAIL during collection with `ModuleNotFoundError: No module named 'custom_components.atmeex_cloud.state_store'`.

- [ ] **Step 3: Implement the immutable contracts and capture methods**

Create `custom_components/atmeex_cloud/state_store.py` with:

```python
from __future__ import annotations
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from .api import AtmeexDevice, AtmeexState
from .helpers import normalize_device_id
if TYPE_CHECKING:
    from .coordinator import AtmeexCoordinatorData
@dataclass(frozen=True, slots=True)
class FieldRevisionBaseline:
    device_id: str
    revisions: Mapping[str, int]
@dataclass(frozen=True, slots=True)
class StateStoreUpdate:
    data: AtmeexCoordinatorData
    changed: bool
    removed_device_ids: frozenset[str] = frozenset()
def _empty_data() -> AtmeexCoordinatorData:
    return {"devices": [], "device_map": {}, "states": {}}
class AtmeexStateStore:
    """Canonical copy-on-write device snapshot with per-field revisions."""

    def __init__(self, initial: AtmeexCoordinatorData | None = None) -> None:
        source = initial or _empty_data()
        self._data: AtmeexCoordinatorData = {
            "devices": [dict(item) for item in source.get("devices", [])],
            "device_map": {
                normalize_device_id(device_id): device
                for device_id, device in source.get("device_map", {}).items()
            },
            "states": {
                str(device_id): dict(state)
                for device_id, state in source.get("states", {}).items()
            },
        }
        self._revision = 0
        self._field_revisions: dict[str, dict[str, int]] = {}
        self._absence_counts: dict[str, int] = {}

    @property
    def data(self) -> AtmeexCoordinatorData:
        return self._data

    def capture_device(self, device_id: int | str) -> FieldRevisionBaseline:
        key = normalize_device_id(device_id)
        return FieldRevisionBaseline(
            key,
            MappingProxyType(dict(self._field_revisions.get(key, {}))),
        )

    def capture_all(self) -> dict[str, FieldRevisionBaseline]:
        keys = set(self._data.get("device_map", {})) | set(self._field_revisions)
        return {key: self.capture_device(key) for key in keys}
```

- [ ] **Step 4: Run the baseline tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py`

Expected: `1 passed`.

- [ ] **Step 5: Commit the state-store contracts**

```bash
git add custom_components/atmeex_cloud/state_store.py tests/test_state_store.py
git commit -m "refactor: add Atmeex state-store contracts"
```

### Task 2: Apply WebSocket Fields Copy-on-Write and Advance Revisions

**Files:**
- Modify: `custom_components/atmeex_cloud/state_store.py`
- Test: `tests/test_state_store.py`

- [ ] **Step 1: Add RED copy-on-write WebSocket tests**

Append to `tests/test_state_store.py`:

```python
def test_websocket_delta_is_copy_on_write_and_advances_only_present_fields():
    store = seed_store(device())
    before = store.data
    before_state = dict(before["states"]["1"])

    update = store.apply_websocket_delta(
        1,
        state_delta={"pwr_on": False},
        device_delta={"condition": {"pwr_on": 0}},
    )
    assert update.changed is True
    assert update.data is store.data
    assert update.data is not before
    assert before["states"]["1"] == before_state
    assert update.data["states"]["1"]["pwr_on"] is False
    revisions = store.capture_device("1").revisions
    assert revisions["state.pwr_on"] > 0
    assert revisions["device.condition.pwr_on"] == revisions["state.pwr_on"]
    assert "state.fan_speed" not in revisions


def test_unchanged_websocket_observation_advances_revision_without_publishing():
    store = seed_store(device(pwr_on=1))
    before = store.data
    baseline = store.capture_device("1")

    update = store.apply_websocket_delta("1", state_delta={"pwr_on": True})

    assert update == StateStoreUpdate(before, False)
    assert store.data is before
    assert (
        store.capture_device("1").revisions["state.pwr_on"]
        > baseline.revisions.get("state.pwr_on", 0)
    )
```

- [ ] **Step 2: Run the WebSocket tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py -k websocket`

Expected: FAIL with `AttributeError: 'AtmeexStateStore' object has no attribute 'apply_websocket_delta'`.

- [ ] **Step 3: Add flatten/rebuild/commit primitives and WebSocket application**

Add these module helpers and class methods to `state_store.py`:

```python
def _flatten_device(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("condition", "settings") and isinstance(value, Mapping):
            for nested_key, nested_value in value.items():
                fields[f"device.{key}.{nested_key}"] = nested_value
        else:
            fields[f"device.{key}"] = value
    return fields
def _rebuild_device(base: AtmeexDevice, fields: Mapping[str, Any]) -> AtmeexDevice:
    raw = base.to_ha_dict()
    raw["condition"] = dict(raw.get("condition", {}))
    raw["settings"] = dict(raw.get("settings", {}))
    for path, value in fields.items():
        parts = path.split(".")
        if len(parts) == 2:
            raw[parts[1]] = value
        else:
            raw[parts[1]][parts[2]] = value
    raw["id"] = base.id
    return AtmeexDevice.from_raw(raw)
```

```python
    def _commit(
        self,
        device_map: dict[str, AtmeexDevice],
        states: dict[str, dict[str, Any]],
        changed_paths: set[tuple[str, str]],
        touched_paths: set[tuple[str, str]] | None = None,
        removed: frozenset[str] = frozenset(),
    ) -> StateStoreUpdate:
        revision_paths = changed_paths if touched_paths is None else touched_paths
        if revision_paths:
            self._revision += 1
            for device_id, path in revision_paths:
                self._field_revisions.setdefault(device_id, {})[path] = self._revision
        if not changed_paths and not removed:
            return StateStoreUpdate(self._data, False)
        for device_id in removed:
            self._field_revisions.pop(device_id, None)
            self._absence_counts.pop(device_id, None)
        self._data = {
            "devices": [item.to_ha_dict() for item in device_map.values()],
            "device_map": dict(device_map),
            "states": {key: dict(value) for key, value in states.items()},
        }
        return StateStoreUpdate(self._data, True, removed)

    def apply_websocket_delta(
        self,
        device_id: int | str,
        *,
        state_delta: Mapping[str, Any],
        device_delta: Mapping[str, Any] | None = None,
    ) -> StateStoreUpdate:
        key = normalize_device_id(device_id)
        current_map = self._data.get("device_map", {})
        current_device = current_map.get(key)
        if current_device is None:
            return StateStoreUpdate(self._data, False)
        device_map = dict(current_map)
        states = {item_key: dict(value) for item_key, value in self._data.get("states", {}).items()}
        changed: set[tuple[str, str]] = set()
        touched: set[tuple[str, str]] = set()

        state = states.setdefault(key, {})
        for field, value in state_delta.items():
            path = f"state.{field}"
            touched.add((key, path))
            if state.get(field) != value:
                state[field] = value
                changed.add((key, path))

        if device_delta:
            current_fields = _flatten_device(current_device.to_ha_dict())
            accepted = _flatten_device(device_delta)
            replacements: dict[str, Any] = {}
            for path, value in accepted.items():
                touched.add((key, path))
                if current_fields.get(path) != value:
                    replacements[path] = value
                    changed.add((key, path))
            if replacements:
                device_map[key] = _rebuild_device(current_device, replacements)

        return self._commit(
            device_map,
            states,
            changed,
            touched_paths=touched,
        )
```

- [ ] **Step 4: Run the WebSocket tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py -k websocket`

Expected: `2 passed`.

- [ ] **Step 5: Commit WebSocket revision application**

```bash
git add custom_components/atmeex_cloud/state_store.py tests/test_state_store.py
git commit -m "feat: version Atmeex websocket fields"
```

### Task 3: Merge Targeted Refreshes Against Their Starting Baseline

**Files:**
- Modify: `custom_components/atmeex_cloud/state_store.py`
- Test: `tests/test_state_store.py`

- [ ] **Step 1: Add a deterministic stale-refresh field/metadata test**

Append:

```python
def test_refresh_preserves_newer_websocket_fields_but_accepts_unrelated_fields():
    store = seed_store(device(name="Original", fan_speed=1, temp_in=170))
    baseline = store.capture_device("1")
    store.apply_websocket_delta(
        "1",
        state_delta={"fan_speed": 7},
        device_delta={"name": "Push Name", "condition": {"fan_speed": 6}},
    )
    result = store.apply_refresh(
        device(name="Stale Name", fan_speed=1, temp_in=225),
        baseline,
    )
    assert result.changed is True
    assert result.data["states"]["1"]["fan_speed"] == 7
    assert result.data["states"]["1"]["temp_in"] == 225
    assert result.data["device_map"]["1"].name == "Push Name"
    assert result.data["device_map"]["1"].condition["temp_in"] == 225


def test_same_value_newer_push_still_blocks_an_older_different_refresh():
    store = seed_store(device(pwr_on=1))
    baseline = store.capture_device("1")
    store.apply_websocket_delta("1", state_delta={"pwr_on": True})

    result = store.apply_refresh(device(pwr_on=0), baseline)

    assert result.data["states"]["1"]["pwr_on"] is True
```

- [ ] **Step 2: Run the refresh race and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py::test_refresh_preserves_newer_websocket_fields_but_accepts_unrelated_fields`

Expected: FAIL with `AttributeError: 'AtmeexStateStore' object has no attribute 'apply_refresh'`.

- [ ] **Step 3: Implement baseline-aware device/state merging**

Add these methods:

```python
    def _merge_device(
        self,
        incoming: AtmeexDevice,
        baseline: FieldRevisionBaseline,
        device_map: dict[str, AtmeexDevice],
        states: dict[str, dict[str, Any]],
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        key = normalize_device_id(incoming.id)
        if baseline.device_id != key:
            raise ValueError("baseline device id does not match response device id")
        changed: set[tuple[str, str]] = set()
        touched: set[tuple[str, str]] = set()
        current_device = device_map.get(key)
        current_fields = (
            {}
            if current_device is None
            else _flatten_device(current_device.to_ha_dict())
        )

        incoming_fields = _flatten_device(incoming.to_ha_dict())
        replacements: dict[str, Any] = {}
        revisions = self._field_revisions.get(key, {})
        for path, value in incoming_fields.items():
            if revisions.get(path, 0) != baseline.revisions.get(path, 0):
                continue
            touched.add((key, path))
            if current_fields.get(path) != value:
                replacements[path] = value
                changed.add((key, path))
        if current_device is not None and replacements:
            device_map[key] = _rebuild_device(current_device, replacements)
        elif current_device is None:
            accepted_device_paths = {path for item_key, path in touched if item_key == key}
            if accepted_device_paths != set(incoming_fields):
                return set(), set()
            device_map[key] = incoming

        incoming_state = AtmeexState.from_device_dict(incoming.to_ha_dict()).to_ha_dict()
        current_state = states.setdefault(key, {})
        for field, value in incoming_state.items():
            path = f"state.{field}"
            if revisions.get(path, 0) != baseline.revisions.get(path, 0):
                continue
            touched.add((key, path))
            if current_state.get(field) != value:
                current_state[field] = value
                changed.add((key, path))
        return changed, touched

    def apply_refresh(
        self,
        device: AtmeexDevice,
        baseline: FieldRevisionBaseline,
    ) -> StateStoreUpdate:
        device_map = dict(self._data.get("device_map", {}))
        states = {key: dict(value) for key, value in self._data.get("states", {}).items()}
        changed, touched = self._merge_device(
            device,
            baseline,
            device_map,
            states,
        )
        # Only successful authoritative inventories affect consecutive-absence
        # tracking. A targeted refresh or push is not inventory evidence.
        return self._commit(
            device_map,
            states,
            changed,
            touched_paths=touched,
        )
```

- [ ] **Step 4: Run all state-store tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py`

Expected: `5 passed`.

- [ ] **Step 5: Commit targeted-refresh convergence**

```bash
git add custom_components/atmeex_cloud/state_store.py tests/test_state_store.py
git commit -m "fix: merge Atmeex refreshes by field revision"
```

### Task 4: Apply One Authoritative Inventory and Remove After Two Successes

**Files:**
- Modify: `custom_components/atmeex_cloud/state_store.py`
- Test: `tests/test_state_store.py`

- [ ] **Step 1: Add RED poll-race and absence tests**

Append:

```python
def test_device_is_removed_only_after_two_successful_absent_inventories():
    store = seed_store(device())

    first = store.apply_inventory([], store.capture_all())
    second = store.apply_inventory([], store.capture_all())
    assert first.changed is False
    assert "1" in first.data["device_map"]
    assert second.changed is True
    assert second.removed_device_ids == frozenset({"1"})
    assert second.data == {"devices": [], "device_map": {}, "states": {}}


def test_targeted_refresh_does_not_reset_authoritative_absence_count():
    current = device()
    store = seed_store(current)

    first = store.apply_inventory([], store.capture_all())
    store.apply_refresh(current, store.capture_device("1"))
    second = store.apply_inventory([], store.capture_all())

    assert first.changed is False
    assert second.removed_device_ids == frozenset({"1"})
```

- [ ] **Step 2: Run inventory tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py -k inventory`

Expected: FAIL with `AttributeError: 'AtmeexStateStore' object has no attribute 'apply_inventory'`.

- [ ] **Step 3: Implement the single-publication authoritative inventory merge**

Add:

```python
    def apply_inventory(
        self,
        devices: Sequence[AtmeexDevice],
        baselines: Mapping[str, FieldRevisionBaseline],
    ) -> StateStoreUpdate:
        device_map = dict(self._data.get("device_map", {}))
        states = {key: dict(value) for key, value in self._data.get("states", {}).items()}
        changed: set[tuple[str, str]] = set()
        touched: set[tuple[str, str]] = set()
        seen: set[str] = set()

        for item in devices:
            key = normalize_device_id(item.id)
            seen.add(key)
            baseline = baselines.get(key) or FieldRevisionBaseline(
                key,
                MappingProxyType({}),
            )
            item_changed, item_touched = self._merge_device(
                item,
                baseline,
                device_map,
                states,
            )
            changed.update(item_changed)
            touched.update(item_touched)
            self._absence_counts[key] = 0

        removed: set[str] = set()
        for key in tuple(device_map):
            if key in seen:
                continue
            count = self._absence_counts.get(key, 0) + 1
            self._absence_counts[key] = count
            if count < 2:
                continue
            device_map.pop(key, None)
            states.pop(key, None)
            removed.add(key)

        return self._commit(
            device_map,
            states,
            changed,
            touched_paths=touched,
            removed=frozenset(removed),
        )
```

- [ ] **Step 4: Run state-store tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py`

Expected: `7 passed`.

- [ ] **Step 5: Commit inventory convergence**

```bash
git add custom_components/atmeex_cloud/state_store.py tests/test_state_store.py
git commit -m "feat: converge authoritative Atmeex inventory"
```

### Task 5: Normalize Isolated WebSocket Deltas

**Files:**
- Modify: `custom_components/atmeex_cloud/helpers.py:168-245`
- Test: `tests/test_helpers.py`

- [ ] **Step 1: Add RED partial-delta tests**

Add imports and tests:

```python
from custom_components.atmeex_cloud.helpers import (
    normalize_condition_delta,
    normalize_settings_delta,
)
def test_condition_delta_ignores_bad_boolean_but_keeps_valid_sibling():
    state_delta, device_delta = normalize_condition_delta(
        {"pwr_on": "unknown", "temp_in": "215"}
    )
    assert "pwr_on" not in state_delta
    assert state_delta == {"temp_in": 215, "online": True}
    assert device_delta == {"condition": {"temp_in": 215}, "online": True}
```

- [ ] **Step 2: Run helper tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_helpers.py -k delta`

Expected: FAIL during collection because `normalize_condition_delta` and `normalize_settings_delta` do not exist.

- [ ] **Step 3: Implement delta normalizers and compatibility wrappers**

Replace `apply_condition_update` and `apply_settings_update` with:

```python
def _parsed_bool(value: Any) -> bool | None:
    try:
        return parse_atmeex_bool(value)
    except ValueError:
        return None
def normalize_condition_delta(
    condition_data: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state: dict[str, Any] = {}
    accepted: dict[str, Any] = {}
    for field in ("pwr_on", "no_water", "u_auto", "u_night"):
        if field in condition_data:
            parsed = _parsed_bool(condition_data[field])
            if parsed is not None:
                state[field] = parsed
                accepted[field] = parsed
    if "fan_speed" in condition_data:
        parsed_speed = _to_int(condition_data["fan_speed"])
        if parsed_speed is not None:
            state["fan_speed"] = api_to_fan_speed(parsed_speed)
            accepted["fan_speed"] = parsed_speed
    for field in ("temp_room", "temp_in", "hum_room", "co2_ppm", "damp_pos", "hum_stg"):
        if field in condition_data:
            parsed = _to_int(condition_data[field])
            if parsed is not None:
                state[field] = parsed
                accepted[field] = parsed
    if "time" in condition_data:
        state["time"] = condition_data["time"]
        accepted["time"] = condition_data["time"]
    state["online"] = True
    device = {"online": True}
    if accepted:
        device["condition"] = accepted
    return state, device
def normalize_settings_delta(
    settings_data: Mapping[str, Any],
    current_state: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    state: dict[str, Any] = {}
    accepted: dict[str, Any] = {}
    if "u_pwr_on" in settings_data:
        parsed_power = _parsed_bool(settings_data["u_pwr_on"])
        if parsed_power is not None:
            state["pwr_on"] = parsed_power
            accepted["u_pwr_on"] = parsed_power
    effective_power = state.get("pwr_on", current_state.get("pwr_on", False))
    if "u_fan_speed" in settings_data:
        parsed_speed = _to_int(settings_data["u_fan_speed"])
        if parsed_speed is not None:
            normalized_speed = api_to_fan_speed(parsed_speed)
            state["u_fan_speed"] = normalized_speed
            accepted["u_fan_speed"] = parsed_speed
            if effective_power:
                state["fan_speed"] = normalized_speed
    for source, target in (
        ("u_temp_room", "u_temp_room"),
        ("u_hum_stg", "hum_stg"),
        ("u_damp_pos", "damp_pos"),
    ):
        if source in settings_data:
            parsed = _to_int(settings_data[source])
            if parsed is not None:
                state[target] = parsed
                accepted[source] = parsed
    for field in ("u_auto", "u_night"):
        if field in settings_data:
            parsed = _parsed_bool(settings_data[field])
            if parsed is not None:
                state[field] = parsed
                accepted[field] = parsed
    state["online"] = True
    device = {"online": True}
    if accepted:
        device["settings"] = accepted
    return state, device
def apply_condition_update(
    state: dict[str, Any], condition_data: dict[str, Any]
) -> dict[str, Any]:
    delta, _device_delta = normalize_condition_delta(condition_data)
    return {**state, **delta}
def apply_settings_update(
    state: dict[str, Any], settings_data: dict[str, Any]
) -> dict[str, Any]:
    delta, _device_delta = normalize_settings_delta(settings_data, state)
    return {**state, **delta}
```

Add `Mapping` to the `collections.abc` imports and use Plan 1's `parse_atmeex_bool`.

- [ ] **Step 4: Run helper tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_helpers.py tests/test_race_protection.py -k 'delta or apply_settings'`

Expected: PASS with no failed selected tests.

- [ ] **Step 5: Commit isolated WebSocket normalization**

```bash
git add custom_components/atmeex_cloud/helpers.py tests/test_helpers.py
git commit -m "fix: isolate invalid Atmeex websocket fields"
```

### Task 6: Route Coordinator Polls Through the Store

**Files:**
- Modify: `custom_components/atmeex_cloud/coordinator.py:20-263`
- Modify: `tests/test_coordinator.py:16-149`

- [ ] **Step 1: Add a deterministic poll/WebSocket race test**

Add imports and this test:

```python
from custom_components.atmeex_cloud.state_store import AtmeexStateStore


@pytest.mark.asyncio
async def test_poll_baseline_preserves_later_websocket_field_with_event_barrier():
    stale = AtmeexDevice.from_raw(
        {
            "id": 1,
            "online": True,
            "condition": {"pwr_on": 1, "fan_speed": 1, "temp_in": 230},
            "settings": {},
        }
    )
    store = AtmeexStateStore()
    store.apply_inventory([stale], store.capture_all())
    started = asyncio.Event()
    release = asyncio.Event()
    coord, api = _make_coordinator(devices=[stale])
    coord.setup_update(
        api=api,
        state_store=store,
        fire_logbook_event=MagicMock(),
    )
    async def blocked_inventory():
        started.set()
        await release.wait()
        return [stale]

    api.get_devices = AsyncMock(side_effect=blocked_inventory)
    api.get_device = AsyncMock(return_value=stale)
    update_task = asyncio.create_task(coord._async_update_data())
    await started.wait()
    store.apply_websocket_delta("1", state_delta={"pwr_on": False})
    release.set()
    data = await update_task
    assert data["states"]["1"]["pwr_on"] is False
    assert data["states"]["1"]["temp_in"] == 230
    assert coord.state_store is store
```

- [ ] **Step 2: Run the coordinator race and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_coordinator.py::test_poll_baseline_preserves_later_websocket_field_with_event_barrier`

Expected: FAIL with `TypeError` because `setup_update` does not accept `state_store`.

- [ ] **Step 3: Inject the store and replace timestamp merging**

Make `AtmeexCoordinatorData` contain only the three comparable snapshot keys, set `kwargs["always_update"] = False`, initialize `avg_latency_ms` and `request_retries` as coordinator properties, and use:

```python
    def setup_update(
        self,
        *,
        api: AtmeexApi,
        state_store: AtmeexStateStore,
        fire_logbook_event: Callable[[str, dict[str, Any]], None],
    ) -> None:
        self._api = api
        # Public because the composition root and Plan 3's command executor
        # share this same entry-owned store.
        self.state_store = state_store
        self._fire_logbook_event = fire_logbook_event
```

Replace `_async_update_data`'s successful merge section with this exact
sequence. The inventory fetch **and** `apply_inventory` stay inside Plan 1's
typed exception boundary because nested condition/settings normalization can
raise `AtmeexProtocolError` too:

```python
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
                {"message": str(err), "status": err.status, "source": "coordinator_update"}
            )
            raise ConfigEntryAuthFailed("Atmeex authentication failed") from err
        except (AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError) as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {"message": str(err), "status": err.status, "source": "coordinator_update"}
            )
            raise UpdateFailed("Atmeex API update failed") from err
        self.last_success_ts = time.time()
        self.avg_latency_ms = round(elapsed_ms, 1)
        self.request_retries = getattr(self._api, "_retry_count", 0)
        self.last_api_error = None
        return update.data
```

Capture `baselines` before `_fetch_devices_safely()` begins. Delete
`_ws_device_update_ts`, `_refresh_device_update_ts`, `poll_start_mono`, and both
whole-device preservation loops. Add
`test_malformed_nested_inventory_maps_to_update_failed_and_preserves_snapshot`:
seed a valid snapshot, return a device with an invalid nested boolean, assert
`UpdateFailed` with `AtmeexProtocolError` as `__cause__`, and assert
`coord.state_store.data` is still the exact prior snapshot object.

- [ ] **Step 4: Update `_make_coordinator` and run coordinator tests GREEN**

In `_make_coordinator`, construct `state_store = AtmeexStateStore()` and pass it in the existing `setup_update` call:

```python
    coord.setup_update(
        api=api,
        state_store=state_store,
        fire_logbook_event=MagicMock(),
    )
```

Run: `.venv/bin/python -m pytest -q tests/test_coordinator.py tests/test_state_store.py`

Expected: PASS; the event-barrier race preserves `pwr_on=False` and no timestamp attribute assertion remains.

- [ ] **Step 5: Commit coordinator/store polling**

```bash
git add custom_components/atmeex_cloud/coordinator.py tests/test_coordinator.py
git commit -m "fix: merge Atmeex polls through versioned store"
```

### Task 7: Route Setup, Targeted Refresh, and WebSocket Publication Through the Store

**Files:**
- Modify: `custom_components/atmeex_cloud/runtime.py:25-40`
- Modify: `custom_components/atmeex_cloud/__init__.py:133-398`
- Modify: `tests/conftest.py:15-60`
- Modify: `tests/test_setup.py:19-117`
- Modify: `tests/test_refresh_device.py:1-100`
- Modify: `tests/test_websocket_integration.py:1-1110`

- [ ] **Step 1: Add RED runtime ownership and targeted race assertions**

In `test_async_setup_entry_happy_path`, add:

```python
    assert runtime.state_store.data is runtime.coordinator.data
    assert runtime.state_store.data["states"]["1"]["pwr_on"] is True
```

Add this deterministic assertion to the existing blocked targeted-refresh fixture in `tests/test_refresh_device.py` after its GET has started and before releasing it:

```python
    runtime.state_store.apply_websocket_delta(
        "1",
        state_delta={"fan_speed": 7},
        device_delta={"condition": {"fan_speed": 6}},
    )
    release_get.set()
    await refresh_task
    assert runtime.coordinator.data["states"]["1"]["fan_speed"] == 7
    assert runtime.coordinator.data["states"]["1"]["temp_in"] == 225
```

- [ ] **Step 2: Run ownership and targeted-race tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_setup.py::test_async_setup_entry_happy_path tests/test_refresh_device.py`

Expected: FAIL with `AttributeError: 'AtmeexRuntimeData' object has no attribute 'state_store'`.

- [ ] **Step 3: Add store ownership and setup injection**

Add `state_store: AtmeexStateStore | None = None` after the three existing
required fields in `AtmeexRuntimeData`. Keeping the default is an intentional
in-repository compatibility bridge for lightweight platform tests; production
setup always supplies a real store and Plan 5 tightens the final runtime type.
In setup, construct and inject it before the first refresh:

```python
    state_store = AtmeexStateStore()
    coordinator = AtmeexCoordinator(
        hass,
        _LOGGER,
        name="Atmeex Cloud",
        update_interval=timedelta(seconds=update_interval_seconds),
    )
    coordinator.setup_update(
        api=api,
        state_store=state_store,
        fire_logbook_event=_fire_logbook_event,
    )
    await coordinator.async_config_entry_first_refresh()
```

Pass `state_store=state_store` when constructing `AtmeexRuntimeData`. Update
shared runtime factories to supply it where coordinator/store behavior is under
test, but do not force unrelated entity-unit fakes to construct a store while
the compatibility default remains.

- [ ] **Step 4: Replace targeted refresh mutation with baseline application**

Import `AtmeexApiError` from `.api` before replacing the refresh body.

Capture before the GET and replace the manual device/state copies with:

```python
    async def _refresh_device_once(device_id: int | str) -> None:
        baseline = state_store.capture_device(device_id)
        try:
            full = await api.get_device(device_id)
        except AtmeexApiError as err:
            coordinator._fire_api_error_event(
                {
                    "message": str(err),
                    "status": err.status,
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            recovery_coro = coordinator.async_request_refresh()
            try:
                hass.async_create_task(
                    recovery_coro,
                    name="atmeex targeted-refresh recovery",
                )
            except BaseException:
                recovery_coro.close()
                raise
            raise
        update = state_store.apply_refresh(full, baseline)
        if not update.changed:
            return
        coordinator.async_set_updated_data(update.data)
        _fire_logbook_event(
            EVENT_DEVICE_UPDATED,
            {"device_id": full.id, "device_name": full.name, "source": "refresh_device"},
        )
```

Keep existing unexpected-exception logging outside the typed branch. Remove the targeted-refresh timestamp write.
The typed failure must propagate to `AtmeexCommandExecutor`, which keeps a
successfully written command successful and retains its pending values. The
separately scheduled coordinator refresh is the immediate authoritative
recovery. Plan 4 replaces this temporary Home Assistant-owned task creation
with the entry task registry and preserves the same behavior.

- [ ] **Step 5: Replace WebSocket mutation with one store publication per message**

Import `normalize_condition_delta`, `normalize_settings_delta`, and `normalize_device_id`, then replace the body after payload validation with:

```python
        changed_device_ids: list[str] = []
        for device_data in payload:
            if not isinstance(device_data, dict) or "id" not in device_data:
                continue
            try:
                key = normalize_device_id(device_data["id"])
            except ValueError:
                continue
            current_state = state_store.data.get("states", {}).get(key, {})
            if msg_type == "condition":
                source = device_data.get("condition")
                if not isinstance(source, dict) or not source:
                    continue
                state_delta, device_delta = normalize_condition_delta(source)
            else:
                source = device_data.get("settings")
                if not isinstance(source, dict) or not source:
                    continue
                state_delta, device_delta = normalize_settings_delta(source, current_state)
            update = state_store.apply_websocket_delta(
                key,
                state_delta=state_delta,
                device_delta=device_delta,
            )
            if update.changed:
                changed_device_ids.append(key)

        if changed_device_ids:
            coordinator.async_set_updated_data(state_store.data)
            _fire_websocket_device_updated(changed_device_ids, msg_type)
```

Delete `state_update_lock` and all direct `devices`, `device_map`, and `states` mutation. Plan 4 will coalesce multiple queued messages per event-loop turn; this plan guarantees one publication for each currently drained message.

- [ ] **Step 6: Run integration paths GREEN**

Before running, migrate every coordinator double to the new keyword-only
signature. Update `tests/conftest.py`, all local `DummyCoordinator` classes in
`tests/test_websocket_integration.py`, and every local coordinator double in
`tests/test_setup.py` so
`setup_update(self, *, api, state_store, fire_logbook_event)` retains
`self.state_store = state_store`. Do not leave a `**kwargs` escape hatch that
could hide a misspelled dependency. Prefer the shared fixture for new tests.

Run: `.venv/bin/python -m pytest -q tests/test_setup.py tests/test_refresh_device.py tests/test_websocket_integration.py tests/test_race_protection.py`

Expected: PASS; both Event-barrier races converge, and no direct timestamp guard is required.

- [ ] **Step 7: Run the full convergence and repository verification**

Run: `.venv/bin/python -m pytest -q tests/test_state_store.py tests/test_helpers.py tests/test_coordinator.py tests/test_setup.py tests/test_refresh_device.py tests/test_websocket_integration.py`

Expected: PASS with zero failures. The known WebSocket-startup warning and
pytest-asyncio loop-scope notice remain assigned to Plans 4 and 6.

Run: `.venv/bin/python -m pytest -q`

Expected: PASS with zero failures and no new warning beyond those two recorded
baseline warnings.

Run: `rg -n '_ws_device_update_ts|_refresh_device_update_ts|poll_start_mono' custom_components/atmeex_cloud tests`

Expected: no matches and exit status 1.

- [ ] **Step 8: Commit complete versioned convergence wiring**

```bash
git add custom_components/atmeex_cloud/runtime.py custom_components/atmeex_cloud/__init__.py tests/conftest.py tests/test_setup.py tests/test_refresh_device.py tests/test_websocket_integration.py tests/test_race_protection.py
git commit -m "refactor: route Atmeex state through versioned store"
```
