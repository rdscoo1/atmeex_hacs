# Quick Refactors (Items 2, 3, 5, 6, 7, 8) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate boilerplate, fix type inconsistencies, remove dead code, and DRY up repeated patterns across the Atmeex Cloud integration.

**Architecture:** Six independent refactors applied in sequence. Each is backward-compatible — no public API changes, no new features. Existing tests must keep passing; new tests added only where behavior changes.

**Tech Stack:** Python 3.12, Home Assistant 2025.1.4, pytest + pytest-asyncio

**Baseline:** 170 tests passing. All work on branch `master`.

---

### Task 1: Extract lock-and-pending helper into `AtmeexEntityMixin` (Item #2)

The lock-acquire + pending-set + API-call + pending-clear-on-error + refresh pattern is copy-pasted ~5 times across `fan.py` and `climate.py`. Extract it into one method on the mixin.

**Files:**
- Modify: `custom_components/atmeex_cloud/entity_base.py` (add method to `AtmeexEntityMixin`)
- Modify: `custom_components/atmeex_cloud/fan.py` (use new helper)
- Modify: `custom_components/atmeex_cloud/climate.py` (use new helper)
- Test: `tests/test_fan.py`, `tests/test_climate.py` (existing tests must pass)

**Step 1: Add `_execute_command` to `AtmeexEntityMixin` in `entity_base.py`**

Add this method to the `AtmeexEntityMixin` class, after the `_state_with_pending` method (after line 74):

```python
async def _execute_command(
    self,
    api_coro,
    *,
    pending_attr: str | None = None,
    pending_value: Any = None,
    error_message: str = "Command failed",
) -> None:
    """Execute an API command with device lock, pending tracking, and refresh.

    Parameters:
        api_coro: awaitable that performs the API call.
        pending_attr: state attribute name to track as pending (e.g. "fan_speed").
        pending_value: value to record as pending before the call.
        error_message: human-readable message for HomeAssistantError on failure.
    """
    from homeassistant.exceptions import HomeAssistantError
    from .api import ApiError

    runtime = getattr(self, "_runtime", None)

    if pending_attr is not None and runtime is not None:
        runtime.set_pending(self._device_id, pending_attr, pending_value)

    lock = runtime.get_device_lock(self._device_id) if runtime is not None else None

    async def _do() -> None:
        try:
            await api_coro
        except ApiError as err:
            if pending_attr is not None and runtime is not None:
                runtime.clear_pending(self._device_id, pending_attr)
            raise HomeAssistantError(error_message) from err
        await self._refresh()

    if lock is not None:
        async with lock:
            await _do()
    else:
        await _do()
```

**Step 2: Refactor `fan.py` to use `_execute_command`**

Replace the 5 command methods in `AtmeexFanEntity`. The key changes:

Remove `_set_fan_speed_with_lock` entirely. Replace its body and callers:

`async_turn_on`:
```python
async def async_turn_on(self, percentage: int | None = None, **kwargs) -> None:
    if percentage is None:
        percentage = self.percentage or 100
    speed = self._percentage_to_speed(percentage)
    if self._runtime is not None:
        self._runtime.set_pending(self._device_id, "pwr_on", True)
    try:
        await self._execute_command(
            self.api.set_fan_speed(self._device_id, speed),
            pending_attr="fan_speed",
            pending_value=speed,
            error_message="Failed to set fan speed",
        )
    except Exception:
        if self._runtime is not None:
            self._runtime.clear_pending(self._device_id, "pwr_on")
        raise
```

`async_turn_off`:
```python
async def async_turn_off(self, **kwargs) -> None:
    await self._execute_command(
        self.api.set_power(self._device_id, False),
        pending_attr="pwr_on",
        pending_value=False,
        error_message="Failed to turn off fan",
    )
```

`async_set_percentage`:
```python
async def async_set_percentage(self, percentage: int) -> None:
    if percentage == 0:
        await self.async_turn_off()
        return
    speed = self._percentage_to_speed(percentage)
    await self._execute_command(
        self.api.set_fan_speed(self._device_id, speed),
        pending_attr="fan_speed",
        pending_value=speed,
        error_message="Failed to set fan speed",
    )
```

Remove `_set_fan_speed_with_lock` method entirely. Remove `PENDING_COMMAND_TTL` constant (it's only used in property definitions, not in command methods — keep it if properties still reference it, which they do).

**Step 3: Refactor `climate.py` to use `_execute_command`**

Replace the command methods in `AtmeexClimateEntity`:

`async_set_hvac_mode`:
```python
async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
    power_on = hvac_mode != HVACMode.OFF
    await self._execute_command(
        self.api.set_power(self._device_id, power_on),
        pending_attr="pwr_on",
        pending_value=power_on,
        error_message="Failed to set HVAC mode",
    )
```

`async_set_fan_mode`:
```python
async def async_set_fan_mode(self, fan_mode: str) -> None:
    try:
        speed = int(fan_mode)
    except (ValueError, TypeError):
        _LOGGER.warning("Unsupported fan_mode: %s", fan_mode)
        return
    await self._execute_command(
        self.api.set_fan_speed(self._device_id, speed),
        pending_attr="fan_speed",
        pending_value=speed,
        error_message="Failed to set fan mode",
    )
```

`async_set_swing_mode`:
```python
async def async_set_swing_mode(self, swing_mode: str) -> None:
    if swing_mode not in BREEZER_SWING_MODES:
        _LOGGER.warning("Unsupported swing_mode: %s", swing_mode)
        return
    await self._execute_command(
        self.api.set_breezer_mode(self._device_id, BREEZER_SWING_MODES.index(swing_mode)),
        error_message="Failed to set swing mode",
    )
```

`async_set_temperature` — this one has extra logic (auto power-on), so keep it mostly as-is but use the helper for the tail:
```python
async def async_set_temperature(self, **kwargs) -> None:
    t = kwargs.get(ATTR_TEMPERATURE)
    if t is None:
        return
    try:
        t_float = float(t)
    except (ValueError, TypeError):
        _LOGGER.warning("Invalid temperature value: %s", t)
        return
    t_clamped = max(self._attr_min_temp, min(self._attr_max_temp, t_float))

    async def _set_temp():
        if not bool(self._device_state.get("pwr_on")):
            await self.api.set_power(self._device_id, True)
        await self.api.set_target_temperature(self._device_id, t_clamped)

    await self._execute_command(
        _set_temp(),
        error_message="Failed to set temperature",
    )
```

`async_set_humidity`:
```python
async def async_set_humidity(self, humidity: int) -> None:
    if not self._has_humidifier():
        return
    q = quantize_humidity(humidity)
    stage = HUM_ALLOWED.index(q)
    await self._execute_command(
        self.api.set_humid_stage(self._device_id, stage),
        error_message="Failed to set humidity",
    )
```

`async_set_humidifier_stage`:
```python
async def async_set_humidifier_stage(self, stage: int) -> None:
    if not self._has_humidifier():
        return
    stage = max(0, min(3, int(stage)))
    await self._execute_command(
        self.api.set_humid_stage(self._device_id, stage),
        error_message="Failed to set humidifier stage",
    )
```

Remove the old `_do_set_and_refresh` inner functions and the manual lock management from each method. Keep the debug logging in properties (those are fine).

**Step 4: Run all tests**

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 5: Commit**

```
refactor: extract _execute_command helper to eliminate lock+pending boilerplate
```

---

### Task 2: Collapse sensor entity boilerplate (Item #3)

Replace 4 near-identical sensor classes with a single data-driven class.

**Files:**
- Modify: `custom_components/atmeex_cloud/sensor.py`
- Test: `tests/test_sensor.py` (existing tests must pass)

**Step 1: Define sensor specs and a generic class**

At module level in `sensor.py`, after imports, add:

```python
from dataclasses import dataclass
from typing import Callable

@dataclass(frozen=True, slots=True)
class _SensorSpec:
    key: str
    unique_suffix: str
    device_class: SensorDeviceClass
    unit: str
    translation_key: str
    state_class: str = SensorStateClass.MEASUREMENT
    convert: Callable[[Any], Any] = lambda v: int(v) if isinstance(v, (int, float)) else None

_DEVICE_SENSOR_SPECS: tuple[_SensorSpec, ...] = (
    _SensorSpec(
        key="co2_ppm",
        unique_suffix="co2",
        device_class=SensorDeviceClass.CO2,
        unit=CONCENTRATION_PARTS_PER_MILLION,
        translation_key="co2",
    ),
    _SensorSpec(
        key="temp_in",
        unique_suffix="inlet_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="inlet_temperature",
        convert=deci_to_c,
    ),
    _SensorSpec(
        key="temp_out",
        unique_suffix="outdoor_temp",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit=UnitOfTemperature.CELSIUS,
        translation_key="outdoor_temperature",
        convert=deci_to_c,
    ),
    _SensorSpec(
        key="hum_room",
        unique_suffix="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit=PERCENTAGE,
        translation_key="humidity",
    ),
)
```

**Step 2: Create the generic sensor class**

Replace the four individual classes (`AtmeexCO2Sensor`, `AtmeexInletTempSensor`, `AtmeexOutdoorTempSensor`, `AtmeexHumiditySensor`) with:

```python
class AtmeexDeviceSensor(AtmeexEntityMixin, CoordinatorEntity, SensorEntity):
    """Generic per-device sensor driven by a _SensorSpec."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator,
        device: AtmeexDevice,
        entry_id: str,
        spec: _SensorSpec,
    ) -> None:
        super().__init__(coordinator)
        self._device_meta = device
        self._device_id = device.id
        self._entry_id = entry_id
        self._spec = spec
        self._attr_unique_id = f"{device.id}_{spec.unique_suffix}"
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_translation_key = spec.translation_key

    @property
    def native_value(self) -> float | int | None:
        return self._spec.convert(self._device_state.get(self._spec.key))
```

**Step 3: Update `_build_entities` in `async_setup_entry`**

Replace the manual entity construction:

```python
def _build_entities(dev: AtmeexDevice) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    for spec in _DEVICE_SENSOR_SPECS:
        if spec.key == "co2_ppm" and not enable_co2:
            continue
        entities.append(
            AtmeexDeviceSensor(
                coordinator=coordinator,
                device=dev,
                entry_id=entry.entry_id,
                spec=spec,
            )
        )
    return entities
```

**Step 4: Keep backward-compatible names for test imports**

The tests import `AtmeexCO2Sensor`, `AtmeexInletTempSensor`, etc. Add aliases at module bottom:

```python
# Backward-compatible aliases for tests and external consumers
AtmeexCO2Sensor = AtmeexDeviceSensor
AtmeexInletTempSensor = AtmeexDeviceSensor
AtmeexOutdoorTempSensor = AtmeexDeviceSensor
AtmeexHumiditySensor = AtmeexDeviceSensor
```

**Step 5: Run all tests**

Run: `python3 -m pytest tests/test_sensor.py -q`
Expected: all sensor tests pass

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 6: Commit**

```
refactor: replace 4 identical sensor classes with data-driven AtmeexDeviceSensor
```

---

### Task 3: Modernize type annotations in `api.py` (Item #5)

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py`

**Step 1: Replace old-style type annotations**

In `api.py`:
- Remove `from typing import Any, Dict, Optional` — replace with `from typing import Any`
- Replace all `Dict[str, Any]` with `dict[str, Any]`
- Replace all `Optional[str]` with `str | None`, `Optional[float]` with `float | None`, etc.

Specific replacements (all occurrences):
- `Dict[str, Any]` → `dict[str, Any]` (in `AtmeexDevice.raw`, `from_raw`, `condition`, `settings`, `to_ha_dict`, `_headers`, `_put_params`, `_request`, `AtmeexState.raw`, `from_device_dict`)
- `Optional[str]` → `str | None` (in `__init__`: `_token`, `_refresh_token`, `_email`, `_password`)
- `Optional[WebSocketConfig]` already uses modern style elsewhere — check `websocket.py` import too (it already uses `Optional` from typing — leave `websocket.py` alone for now, it's not in scope)

**Step 2: Run tests**

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 3: Commit**

```
refactor: modernize api.py type annotations from Dict/Optional to dict/pipe syntax
```

---

### Task 4: Simplify `AtmeexState` — remove unused typed fields (Item #6)

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py`

**Step 1: Simplify `AtmeexState` dataclass**

The typed fields (`pwr_on`, `fan_speed`, etc.) are never read after construction — `to_ha_dict()` returns `dict(self.raw)`. Simplify to:

```python
@dataclass(slots=True)
class AtmeexState:
    """Normalized device state (condition + settings merged)."""
    id: int
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_device_dict(cls, device: dict[str, Any]) -> AtmeexState:
        """Build normalized state from raw device payload."""
        normalized = _normalize_device_state(device)
        return cls(id=int(device["id"]), raw=normalized)

    def to_ha_dict(self) -> dict[str, Any]:
        """State dict stored in coordinator.data['states'][id]."""
        return dict(self.raw)
```

**Step 2: Run tests**

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 3: Commit**

```
refactor: simplify AtmeexState to id+raw — typed fields were unused at runtime
```

---

### Task 5: DRY diagnostics computation (Item #7)

**Files:**
- Modify: `custom_components/atmeex_cloud/sensor.py` (`AtmeexDiagnosticsSensor.extra_state_attributes`)
- Test: `tests/test_sensor.py` (existing tests must pass)

**Step 1: Reuse `get_diagnostics_snapshot` in the diagnostics sensor**

In `sensor.py`, add import:
```python
from .diagnostics import get_diagnostics_snapshot
```

Replace the `extra_state_attributes` property body of `AtmeexDiagnosticsSensor` with:

```python
@property
def extra_state_attributes(self) -> dict[str, Any]:
    """Return diagnostic attributes, reusing the shared snapshot."""
    attrs = get_diagnostics_snapshot(self.coordinator)

    # WebSocket-specific fields
    ws_manager = getattr(self._runtime, "websocket_manager", None)
    ws_connected = None
    ws_last_message_age_sec = None
    if ws_manager is not None:
        ws_connected = bool(getattr(ws_manager, "is_connected", False))
        ws_age = getattr(ws_manager, "last_message_age", None)
        if isinstance(ws_age, (int, float)) and isfinite(float(ws_age)):
            ws_last_message_age_sec = round(float(ws_age), 1)

    attrs["websocket_connected"] = ws_connected
    attrs["websocket_last_message_age_sec"] = ws_last_message_age_sec
    attrs["domain"] = DOMAIN

    return attrs
```

This removes the duplicated last_success_ts/utc/last_api_error computation.

Note: `get_diagnostics_snapshot` returns keys `device_count`, `last_success_ts`, `last_success_utc`, `last_api_error`, `last_api_error_status`. The old sensor also returned `state_entries`. We need to add that back:

Actually, check the existing test expectations. The test at `tests/test_sensor.py` checks for `"state_entries"` in attrs. `get_diagnostics_snapshot` doesn't include `state_entries`. Two options: (a) add `state_entries` to `get_diagnostics_snapshot`, or (b) add it after the call. Option (b) is simpler:

```python
@property
def extra_state_attributes(self) -> dict[str, Any]:
    attrs = get_diagnostics_snapshot(self.coordinator)

    # Additional fields not in the shared snapshot
    data: dict[str, Any] = getattr(self.coordinator, "data", {}) or {}
    states = data.get("states") or {}
    attrs["state_entries"] = len(states) if isinstance(states, dict) else 0

    ws_manager = getattr(self._runtime, "websocket_manager", None)
    ws_connected = None
    ws_last_message_age_sec = None
    if ws_manager is not None:
        ws_connected = bool(getattr(ws_manager, "is_connected", False))
        ws_age = getattr(ws_manager, "last_message_age", None)
        if isinstance(ws_age, (int, float)) and isfinite(float(ws_age)):
            ws_last_message_age_sec = round(float(ws_age), 1)

    attrs["websocket_connected"] = ws_connected
    attrs["websocket_last_message_age_sec"] = ws_last_message_age_sec
    attrs["domain"] = DOMAIN

    return attrs
```

Remove the now-unused imports from `sensor.py`: `datetime`, `timezone` are no longer needed in the sensor module (they're used in `diagnostics.py`). Check that nothing else in sensor.py uses them — `AtmeexDiagnosticsSensor` was the only user.

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_sensor.py tests/test_diagnostics.py -q`
Expected: all pass

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 3: Commit**

```
refactor: reuse get_diagnostics_snapshot in sensor to eliminate duplicated logic
```

---

### Task 6: Remove dead `async_migrate_entry` (Item #8)

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`

**Step 1: Remove `async_migrate_entry` function**

Delete the entire function (lines 816-838). It's a no-op — version is 1, the function does nothing, and the commented-out code is template noise. HA doesn't require this function to exist when VERSION=1.

Also remove it from `__all__` if present (check — it's not in `__all__`, so nothing to change there).

**Step 2: Run tests**

Run: `python3 -m pytest -q`
Expected: 170 passed

**Step 3: Commit**

```
refactor: remove no-op async_migrate_entry — VERSION=1 needs no migration
```

---

## Execution Order

Tasks 1-6 are independent of each other and can be applied in any order. The plan numbers above give a reasonable sequence (biggest impact first).

## Verification

After all tasks, run full suite:
```bash
python3 -m pytest -q
python3 -m py_compile custom_components/atmeex_cloud/*.py
```
Expected: 170 passed, no syntax errors.
