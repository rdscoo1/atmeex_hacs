# Design: Power Switch, Supply-Valve Fix, HEAT Mode

**Date:** 2026-05-02
**Status:** Approved (brainstorm complete, awaiting implementation plan)
**Scope:** Closes feature gaps surfaced by comparison with the [anpavlov/atmeex_hacs](https://github.com/anpavlov/atmeex_hacs) fork and by physical-device testing of the official Atmeex mobile app.

---

## Background

Three gaps were identified:

1. **No "Power" switch.** The integration ships AutoNanny and Sleep Mode switches, but no entity that simply toggles `u_pwr_on`. Users wanting "turn the device on/off" must use Climate or Fan, which carries other semantics.
2. **The `supply_valve` work mode is broken.** `const.py` defines `BREEZER_MODES[3] = "supply_valve"` and the breezer select sends `u_damp_pos=3`. Real-device testing (logs at 2026-05-02 23:13–23:15) confirmed the device snaps back to `damp_pos=0` because **position 3 does not exist** at the device level. The official mobile app implements "Supply valve" as the *combined* state `u_pwr_on=False, u_damp_pos=0` — a UI-level work mode, not a fourth physical position.
3. **No HEAT mode on Climate.** Climate exposes only `[FAN_ONLY, OFF]` despite reading `u_temp_room` and supporting `set_target_temperature`. Setting a temperature works, but users cannot select HEAT explicitly, and the device's heater state is invisible in the climate UI.

Evidence — settings WS message at 23:15:30, immediately after pressing "Supply valve" in the official app:

```json
{"u_pwr_on": false, "u_fan_speed": 3, "u_damp_pos": 0, ...}
```

And at 23:15:38, after pressing "Forced ventilation" back:

```json
{"u_pwr_on": true, "u_fan_speed": 3, "u_damp_pos": 0, ...}
```

`u_damp_pos` was never `3` in either transition.

---

## Decisions

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Passive ventilation as separate macro switch, or fix supply_valve in select? | **Fix supply_valve in select.** No new switch entity. | The select already exposes "supply_valve". A macro switch would be a redundant alias. |
| 2 | Power switch semantics: power-only vs. power+damper coupling (fork style)? | **Power-only** (toggle `u_pwr_on` only, leave damper alone). | Matches HA "switch toggles one bit" convention. Composes cleanly with the breezer select. Minimum new code — reuses existing `api.set_power`. |
| 3 | New `set_work_mode` API method, or modify `set_breezer_mode`? | **Modify `set_breezer_mode`.** | Single source of truth, no new method, all existing call sites work unchanged. |
| 4 | `set_heater_off` as separate method or extend `set_target_temperature(None)`? | **Separate method.** | Cleaner intent at call sites; avoids `None` footgun. |
| 5 | Atomic multi-field PUT for HEAT entry/exit, or sequential calls? | **Atomic combined method `set_power_and_heat`.** | Avoids brief UI flicker between `pwr_on=True` and `u_temp_room=<target>` confirmations. Single PUT body. |

---

## Component Designs

### 1. `AtmeexPowerSwitch` (new)

**File:** `custom_components/atmeex_cloud/switch.py`
**Position:** Third `_BaseSwitch` subclass alongside `AtmeexAutoNannySwitch` and `AtmeexSleepModeSwitch`.

```python
class AtmeexPowerSwitch(_BaseSwitch):
    _attr_translation_key = "power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator, api, device, refresh_device_cb=None, runtime=None):
        super().__init__(coordinator, api, device, refresh_device_cb, runtime)
        self._attr_unique_id = f"{device.id}_power"

    @property
    def is_on(self) -> bool | None:
        confirmed = self._device_state.get("pwr_on", False)
        return bool(self._state_with_pending("pwr_on", confirmed, tolerance=_PENDING_TTL))

    async def async_turn_on(self, **kwargs) -> None:
        await self._execute_command(
            self.api.set_power(self._device_id, True),
            pending_attr="pwr_on", pending_value=True,
            error_message="Failed to turn on",
        )

    async def async_turn_off(self, **kwargs) -> None:
        await self._execute_command(
            self.api.set_power(self._device_id, False),
            pending_attr="pwr_on", pending_value=False,
            error_message="Failed to turn off",
        )
```

Appended in `_build_entities` (one line). `entity_category` left default — surfaces in primary UI alongside the other switches.

**Cross-entity behavior:** Power switch + Climate `pwr_on` + Fan `is_on` all observe the same `pwr_on` field through the coordinator. They stay in sync automatically. Per-device lock + pending state machinery serialize concurrent writes.

**Edge case — power-on while in supply_valve:** sends only `u_pwr_on=True`. Device transitions to `pwr=True, damp=0` which is "forced ventilation". Single-bit semantic; no surprise side effects.

### 2. Work-mode select fix

**File:** `custom_components/atmeex_cloud/api.py` (`set_breezer_mode`)

Replace single-field body with mode-aware body:

```python
async def set_breezer_mode(self, device_id: int | str, mode_index: int) -> None:
    """Set work mode 0..3.

    Modes 0/1/2 are physical damper positions and force u_pwr_on=True.
    Mode 3 (supply_valve) is a virtual mode: u_pwr_on=False + u_damp_pos=0.
    Matches the official mobile app's behavior.
    """
    mode = int(mode_index)
    if mode == 3:
        body = {"u_pwr_on": False, "u_damp_pos": 0}
    elif mode in (0, 1, 2):
        body = {"u_pwr_on": True, "u_damp_pos": mode}
    else:
        raise ApiError(f"set_breezer_mode: invalid mode {mode}")
    await self._put_params(device_id, body, "set_breezer_mode")
```

**File:** `custom_components/atmeex_cloud/select.py` (`AtmeexBreezerSelect`)

Switch from raw `try/except` to `_execute_command` + secondary pending:

```python
async def async_select_option(self, option: str) -> None:
    if option not in BREEZER_OPTIONS:
        return
    pos = BREEZER_OPTIONS.index(option)
    target_pwr = (pos != 3)
    target_damp = 0 if pos == 3 else pos

    if self._runtime is not None:
        self._runtime.set_pending(self._device_id, "damp_pos", target_damp)
    await self._execute_command(
        self.api.set_breezer_mode(self._device_id, pos),
        pending_attr="pwr_on", pending_value=target_pwr,
        error_message="Failed to set work mode",
    )
    self._attr_current_option = option
```

`current_option` derivation truth table:

| `pwr_on` | `damp_pos` | → `current_option` |
|---|---|---|
| True | 0 | forced_ventilation |
| True | 1 | recirculation |
| True | 2 | mixed_mode |
| False | 0 | supply_valve |
| False | 1 or 2 | last selected option (fallback) |

```python
@property
def current_option(self) -> str | None:
    pos = self._device_state.get("damp_pos")
    pwr = self._device_state.get("pwr_on")
    if pos == 0 and not pwr:
        return BREEZER_OPTIONS[3]  # supply_valve
    if isinstance(pos, int) and 0 <= pos < 3:
        return BREEZER_OPTIONS[pos]
    return getattr(self, "_attr_current_option", BREEZER_OPTIONS[0])
```

### 3. HVACMode.HEAT on Climate

**File:** `custom_components/atmeex_cloud/api.py` — two new methods:

```python
async def set_heater_off(self, device_id: int | str) -> None:
    """Disable the heater by sending the device's off-sentinel target temp."""
    await self._put_params(device_id, {"u_temp_room": -1000}, "set_heater_off")

async def set_power_and_heat(
    self, device_id: int | str, pwr_on: bool, temp_c: float | None
) -> None:
    """Atomic multi-field PUT for HEAT entry/exit transitions.

    temp_c=None means heater off (sends -1000).
    """
    body: dict[str, Any] = {"u_pwr_on": bool(pwr_on)}
    if temp_c is None:
        body["u_temp_room"] = -1000
    else:
        value = c_to_deci(temp_c)
        if value is None:
            raise ApiError(f"set_power_and_heat: invalid temperature {temp_c!r}")
        body["u_temp_room"] = value
    await self._put_params(device_id, body, "set_power_and_heat")
```

**File:** `custom_components/atmeex_cloud/climate.py`

```python
_attr_hvac_modes = [HVACMode.HEAT, HVACMode.FAN_ONLY, HVACMode.OFF]
```

`hvac_mode` truth table:

| `pwr_on` (effective) | `u_temp_room` valid (≥100)? | `damp_pos` | → mode |
|---|---|---|---|
| False | — | — | OFF |
| True | True | not 1 | HEAT |
| True | True | 1 (recirc) | FAN_ONLY (device auto-disabled heater) |
| True | False | — | FAN_ONLY |

Pending overlay applies to both `pwr_on` and `u_temp_room`.

`async_set_hvac_mode` transitions:

```python
async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
    if hvac_mode == HVACMode.OFF:
        await self._execute_command(
            self.api.set_power(self._device_id, False),
            pending_attr="pwr_on", pending_value=False,
            error_message="Failed to turn off",
        )
    elif hvac_mode == HVACMode.FAN_ONLY:
        device_off = not bool(self._device_state.get("pwr_on"))
        if device_off:
            await self._execute_command(
                self.api.set_power_and_heat(self._device_id, True, None),
                pending_attr="pwr_on", pending_value=True,
                error_message="Failed to enter fan-only mode",
            )
            self._runtime.set_pending(self._device_id, "u_temp_room", -1000)
        else:
            await self._execute_command(
                self.api.set_heater_off(self._device_id),
                pending_attr="u_temp_room", pending_value=-1000,
                error_message="Failed to disable heater",
            )
    elif hvac_mode == HVACMode.HEAT:
        target_c = self._resolve_heat_target()
        device_off = not bool(self._device_state.get("pwr_on"))
        await self._execute_command(
            self.api.set_power_and_heat(self._device_id, True, target_c),
            pending_attr="pwr_on", pending_value=True,
            error_message="Failed to enter heat mode",
        )
        self._runtime.set_pending(self._device_id, "u_temp_room", c_to_deci(target_c))
```

`_resolve_heat_target()`:
- Read current `u_temp_room`. If valid (`100 ≤ value ≤ 300`), return as °C.
- Else read `self._last_heat_temp` instance attr. Use if non-None.
- Else default to `20.0`.

`_last_heat_temp`: instance attr, updated on every confirmed valid `u_temp_room` (in `target_temperature` getter or via a coordinator listener — getter is simpler). Lost on HA restart; acceptable.

`async_set_temperature` (existing): keep current "auto-power-on if device off" behavior. Setting temperature on a FAN_ONLY device implies HEAT — already handled because the device transitions automatically when `u_temp_room` becomes valid.

**Recirculation interaction:** if user enters HEAT while `damp_pos == 1`, the command succeeds but the device blocks heater activation. Climate optimistically shows HEAT, then settles to FAN_ONLY when WS confirms. We do **not** auto-change `damp_pos` — that's user intent territory.

### 4. Translation strings

`strings.json`, `translations/en.json`, `translations/ru.json`:

```json
"entity": {
  "switch": {
    "power": { "name": "Power" }    // "Питание" in ru
  }
}
```

HVAC mode names are HA-builtin; no string changes needed.

---

## Test Plan

Tests warranted (TDD opt-in per project preference):

| Module | Cases |
|---|---|
| `tests/test_api.py` | `set_breezer_mode` parametrized 0/1/2 → `{u_pwr_on: True, u_damp_pos: <mode>}`; mode 3 → `{u_pwr_on: False, u_damp_pos: 0}`; invalid → `ApiError`. `set_heater_off` → `{u_temp_room: -1000}`. `set_power_and_heat` four cases: `(True, 22.5)`, `(True, None)`, `(False, 20.0)`, `(False, None)`. |
| `tests/test_climate.py` | `hvac_mode` truth table (4 rows). OFF → `set_power(False)`. FAN_ONLY from off → `set_power_and_heat(True, None)`. OFF → HEAT with no last-temp → `set_power_and_heat(True, 20.0)`. FAN_ONLY → HEAT with last-temp 24 → uses 24. HEAT → FAN_ONLY (device on) → `set_heater_off`. `async_set_temperature` from FAN_ONLY transitions to HEAT optimistically. `_resolve_heat_target` precedence. |
| `tests/test_select.py` (new or extended) | `async_select_option("supply_valve")` → `set_breezer_mode(3)` + pending `{damp_pos: 0, pwr_on: False}`. `async_select_option("recirculation")` → `set_breezer_mode(1)` + pending `{damp_pos: 1, pwr_on: True}`. `current_option` truth table (5 rows). |
| `tests/test_switch.py` | New `AtmeexPowerSwitch`: `is_on` reads `pwr_on` with pending overlay; turn_on/off route through `_execute_command` with correct pending. |

Tests skipped:
- Translation key existence (HA validation handles this).
- Trivial property pass-throughs already covered.

---

## File-Level Change Summary

| File | Change | Approx LOC |
|---|---|---|
| `api.py` | Modify `set_breezer_mode`; add `set_heater_off`; add `set_power_and_heat` | ~30 |
| `select.py` | Rewrite `AtmeexBreezerSelect.async_select_option` and `current_option` | ~30 |
| `switch.py` | Add `AtmeexPowerSwitch` class; append in `_build_entities` | ~35 |
| `climate.py` | HEAT mode plumbing: hvac_modes, hvac_mode property, async_set_hvac_mode, `_resolve_heat_target`, `_last_heat_temp`, pending for `u_temp_room` | ~70 |
| `strings.json` + 2 translations | New `power` switch key | ~6 |
| `tests/test_api.py` | New cases | ~50 |
| `tests/test_climate.py` | New cases | ~80 |
| `tests/test_switch.py` | New cases | ~40 |
| `tests/test_select.py` | New cases | ~60 |

**Total: ~400 LOC across ~9 files** (roughly half production, half tests).

---

## Migration & Rollout

- **No state migration.** No schema changes. New `{device_id}_power` entity appears via existing dynamic device discovery on next reload.
- **Behavioral change for existing users:** automations referencing `select.*_breezer_mode = "supply_valve"` will *now actually work* instead of silently snapping back. This is a fix, not a break.
- **Rollback:** revert the commit. No persisted state to clean up.

### Recommended commit order

1. API changes + API tests.
2. Power switch + tests.
3. Breezer select fix + tests.
4. HEAT mode + tests.
5. Translations.

Each commit independently shippable.

---

## Out of Scope (explicit non-goals)

- **Persisted snapshots / RestoreEntity.** YAGNI; in-memory `_last_heat_temp` is sufficient.
- **Renaming AutoNanny / Sleep Mode switches.** Existing names are fine.
- **Macro "passive ventilation" switch.** The supply_valve fix in the select gives users the same capability through the existing UI.
- **Touching WS / coordinator / pending-state machinery.** All new commands reuse the existing `_execute_command` + `runtime.set_pending` patterns established in `fan.py`.
- **Auto-changing damper when user enters HEAT during recirculation.** Treated as user-intent territory; the device's server-side enforcement is sufficient.
