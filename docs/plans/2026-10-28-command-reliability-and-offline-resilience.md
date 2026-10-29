# Command Reliability & Offline Resilience Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make temperature, fan-speed and valve (breezer-mode) commands robust and cheap, keep breezers controllable through short cloud hiccups, and stop deleting a breezer's Home Assistant setup while it is offline or being re-paired.

**Architecture:** Every change stays inside an existing layer — `AtmeexCommandExecutor` (UI notification, per-command "settled" view), `AtmeexEntityMixin` (write filtering against the cloud's stored targets), `AtmeexApi` (short confirmation GET, resend of provably undelivered writes), `AtmeexCoordinator` / `AtmeexStateStore` (availability, hydration, watchdog, retirement grace) and `WebSocketManager` (backoff). No new modules, no new dependencies.

**Tech Stack:** Python 3.12, Home Assistant 2025.1 test harness (`pytest-homeassistant-custom-component`), aiohttp, pytest + pytest-asyncio + pytest-timeout.

---

## Before you start

- **Every diff below is verified.** Each task was prototyped on `1700e91` in a scratch copy; the diffs apply in order and leave the suite green after every task (654 → 696 tests). Context lines are exact, so you can apply a block with `git apply` (save it to a file first) or make the edits by hand.
- **Test policy for this repo:** implement first, then add/adjust tests only where behavior is externally observable (project preference, overrides the generic TDD default). The behavior tests in Tasks 3 and 4 were mutation-checked: they fail when the fix is reverted.
- **Always pass `--timeout=15`.** A wrong concurrency edit makes a test hang instead of fail.
- Read `CLAUDE.md` § "Key concurrency facts" before Task 1.
- Work on a branch (Task 0). Never commit to `master`.

### Domain facts you need

| Fact | Where it matters |
|------|------------------|
| `PUT /devices/{id}/params` takes `u_*` fields. The cloud stores them as **targets** in `settings.u_*` as soon as it accepts the PUT. `condition.*` is what the device **reports** and lags until the device acknowledges. | Task 3 compares against `settings`, never `condition`. |
| Commit `fa6b806`: the cloud **drops `u_temp_room`** from a multi-field body whose `u_pwr_on` is unchanged. Never put an unchanged `u_pwr_on` into a combined body. | Tasks 3 (damper sent alone) |
| HA silently skips entity service calls to **unavailable** entities (`homeassistant/helpers/service.py`: `if not entity.available: continue`). | Task 5 |
| After a refresh, HA's `DataUpdateCoordinator._async_refresh` only notifies listeners when success flips or data changes — **not** on a second consecutive failure. | Task 5 |
| The executor installs optimistic per-field "pending" values when a command is queued (TTL 10 s) and confirms them via a targeted refresh, all serialized per device by one lock. | Tasks 1–4 |

### Commands

```bash
.venv/bin/python -m pytest -q --timeout=15            # full suite
.venv/bin/python -m pytest -q --timeout=15 -k NAME    # subset
```

## Tasks

### Task 0: Branch

**Step 1: Create the branch and confirm the baseline**

```bash
git checkout -b fix/command-reliability
.venv/bin/python -m pytest -q --timeout=15
```
Expected: `654 passed`.

### Task 1: Show optimistic values the moment a command starts

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/command_executor.py`
- Test: `tests/conftest.py`
- Test: `tests/test_command_executor.py`
- Test: `tests/test_setup.py`
- Test: `tests/test_websocket_integration.py`

**Why:** `AtmeexCommandExecutor.async_execute` installs the optimistic value but never tells HA, so the UI shows the new temperature/speed only after the PUT(s) **and** the confirmation GET finish — and only if that GET changed something. A failed command never reverts the UI either.

**What:**

Add an `on_change` callback to the executor, wired to `coordinator.async_update_listeners`. Call it right after installing pending values and once more in the final `finally` (success, failure or cancellation). Failures inside the callback are swallowed so a UI refresh can never fail a command.

Deliberately **not** done: a timer that re-renders when the 10 s optimistic value expires. HA's test harness flags it as a lingering timer, it would outlive an unload, and the next poll or WebSocket push re-renders anyway.

Test fallout: fake coordinators need `async_update_listeners`; the ABA recovery test counts recovery publications, so it switches the executor's own notifications off (`executor._on_change = None`).

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/__init__.py b/custom_components/atmeex_cloud/__init__.py
index fadcee5..6c8b6ac 100644
--- a/custom_components/atmeex_cloud/__init__.py
+++ b/custom_components/atmeex_cloud/__init__.py
@@ -420,7 +420,10 @@ async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
             timeout=_REFRESH_TASK_TIMEOUT_SEC,
         )
 
-    command_executor = AtmeexCommandExecutor(refresh_device)
+    command_executor = AtmeexCommandExecutor(
+        refresh_device,
+        on_change=coordinator.async_update_listeners,
+    )
     runtime_data = AtmeexRuntimeData(
         api=api,
         coordinator=coordinator,
diff --git a/custom_components/atmeex_cloud/command_executor.py b/custom_components/atmeex_cloud/command_executor.py
index 1b5b117..2561508 100644
--- a/custom_components/atmeex_cloud/command_executor.py
+++ b/custom_components/atmeex_cloud/command_executor.py
@@ -5,6 +5,7 @@ import asyncio
 from collections.abc import Awaitable, Callable, Mapping
 from copy import deepcopy
 from dataclasses import dataclass
+import logging
 import time
 from typing import Any
 
@@ -14,6 +15,8 @@ from .api import ApiError
 from .const import DOMAIN
 from .helpers import normalize_device_id
 
+_LOGGER = logging.getLogger(__name__)
+
 CommandCoroutineFactory = Callable[[], Awaitable[None]]
 RecoveryRefresh = Callable[[int | str], Awaitable[None]]
 
@@ -35,9 +38,11 @@ class AtmeexCommandExecutor:
         refresh_device: RecoveryRefresh,
         *,
         pending_ttl: float = 10.0,
+        on_change: Callable[[], None] | None = None,
     ) -> None:
         self._refresh_device = refresh_device
         self._pending_ttl = pending_ttl
+        self._on_change = on_change
         self._generation = 0
         self._locks: dict[str, asyncio.Lock] = {}
         self._lock_users: dict[str, int] = {}
@@ -51,6 +56,15 @@ class AtmeexCommandExecutor:
         self._executed_generations: set[int] = set()
         self._confirmation_ready_generations: set[int] = set()
 
+    def _notify(self) -> None:
+        """Ask entities to re-render so optimistic values show (or revert) now."""
+        if self._on_change is None:
+            return
+        try:
+            self._on_change()
+        except Exception:  # noqa: BLE001 - a UI refresh must never fail a command
+            _LOGGER.debug("Atmeex pending-state notification failed")
+
     @staticmethod
     def _device_key(device_id: int | str) -> str:
         return normalize_device_id(device_id)
@@ -409,6 +423,8 @@ class AtmeexCommandExecutor:
         key = self._device_key(device_id)
         generation = self._next_generation()
         self._install_pending(key, generation, pending)
+        # Show the optimistic value now, not after the PUT + confirmation GET.
+        self._notify()
         lock = self._retain_lock(key)
 
         try:
@@ -467,3 +483,5 @@ class AtmeexCommandExecutor:
                 raise
         finally:
             self._release_lock(key, lock)
+            # Success, failure or cancellation may have retired pending values.
+            self._notify()
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 1d30531..b6770e2 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -53,6 +53,7 @@ class DummyCoordinator:
         self.request_retries = 0
         # Extra counter used by some WS tests
         self.update_calls = 0
+        self.listener_updates = 0
         self._fire_logbook_event = fire_logbook_event
         self._api_error_last_ts = float("-inf")
         self._api_error_suppressed = 0
@@ -93,6 +94,9 @@ class DummyCoordinator:
         self.update_calls += 1
         self.data = data
 
+    def async_update_listeners(self):
+        self.listener_updates += 1
+
     async def async_request_refresh(self):
         inventory_success_before = self.last_inventory_success_mono
         self.last_update_success = False
diff --git a/tests/test_command_executor.py b/tests/test_command_executor.py
index 5019838..c403c7b 100644
--- a/tests/test_command_executor.py
+++ b/tests/test_command_executor.py
@@ -913,3 +913,59 @@ async def test_api_error_translation_preserves_key_placeholders_and_cause():
     assert raised.value.translation_key == "command_failed"
     assert raised.value.translation_placeholders == {"action": "set fan speed"}
     assert raised.value.__cause__ is api_error
+
+
+@pytest.mark.asyncio
+async def test_on_change_publishes_pending_before_the_write_and_after_it():
+    seen: list[object] = []
+    executor: AtmeexCommandExecutor
+
+    def on_change() -> None:
+        pending = executor.get_pending(1, "fan_speed")
+        seen.append(None if pending is None else pending.value)
+
+    executor = AtmeexCommandExecutor(AsyncMock(), on_change=on_change)
+
+    async def operation() -> None:
+        seen.append("write")
+
+    await executor.async_execute(
+        1, operation, pending={"fan_speed": 5}, translation_key="command_failed"
+    )
+
+    # Optimistic value is published before the remote write starts.
+    assert seen[:2] == [5, "write"]
+    assert len(seen) == 3
+
+
+@pytest.mark.asyncio
+async def test_on_change_publishes_revert_after_failed_write():
+    seen: list[object] = []
+    executor: AtmeexCommandExecutor
+
+    def on_change() -> None:
+        pending = executor.get_pending(1, "pwr_on")
+        seen.append(None if pending is None else pending.value)
+
+    executor = AtmeexCommandExecutor(AsyncMock(), on_change=on_change)
+
+    async def failing() -> None:
+        raise ApiError("set_power", "rejected", status=400)
+
+    with pytest.raises(HomeAssistantError):
+        await executor.async_execute(
+            1, failing, pending={"pwr_on": True}, translation_key="command_failed"
+        )
+
+    assert seen == [True, None]
+
+
+@pytest.mark.asyncio
+async def test_on_change_failure_never_fails_the_command():
+    executor = AtmeexCommandExecutor(
+        AsyncMock(), on_change=MagicMock(side_effect=RuntimeError("boom"))
+    )
+
+    assert await executor.async_execute(
+        1, AsyncMock(), pending={"pwr_on": True}, translation_key="command_failed"
+    )
diff --git a/tests/test_setup.py b/tests/test_setup.py
index 41d0741..fc7dbf3 100644
--- a/tests/test_setup.py
+++ b/tests/test_setup.py
@@ -765,6 +765,7 @@ def _setup_lifecycle_fakes(monkeypatch, order):
             self.config_entry_id = config_entry_id
             self.data = state_store.data
             self.async_request_refresh = AsyncMock()
+            self.async_update_listeners = MagicMock()
 
         async def async_config_entry_first_refresh(self):
             return
diff --git a/tests/test_websocket_integration.py b/tests/test_websocket_integration.py
index 6d6bf7a..8855900 100644
--- a/tests/test_websocket_integration.py
+++ b/tests/test_websocket_integration.py
@@ -1149,6 +1149,9 @@ async def test_failed_confirmation_serializes_recovery_and_preserves_executed_ab
     executor = runtime.command_executor
     assert executor is not None
 
+    # This test counts recovery publications; keep the executor's own
+    # optimistic-state notifications out of that count.
+    executor._on_change = None
     scheduled: list[asyncio.Task] = []
 
     def create_task(coro, **kwargs):
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_command_executor.py -k on_change -v`
Expected: `3 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `657 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/command_executor.py tests/conftest.py tests/test_command_executor.py tests/test_setup.py tests/test_websocket_integration.py
git commit -m "fix: publish optimistic command values to the UI immediately"
```

### Task 2: Pending-aware climate targets + one breezer-mode mapping

**Files:**
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/helpers.py`
- Modify: `custom_components/atmeex_cloud/select.py`
- Test: `tests/test_climate.py`
- Test: `tests/test_helpers.py`

**Why:**

- `target_temperature`, `swing_mode`, `target_humidity` and the damper check in `hvac_mode` read only confirmed state. Frequent WebSocket condition pushes (CO2 etc.) re-render the card between the PUT and the confirmation, so the thermostat snaps back to the old target.
- The climate card maps damper 0 → `supply_ventilation` even when power is off, while the breezer-mode select correctly shows `supply_valve` (power off + damper 0). Same device, two different valve modes.

**What:** Route those properties through `_state_with_pending`. Move the select's mapping into `helpers.breezer_mode_from_state(damp_pos, pwr_on)` and use it from both `AtmeexClimateEntity.swing_mode` and `AtmeexBreezerSelect.current_option`.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/climate.py b/custom_components/atmeex_cloud/climate.py
index 0439428..b80f850 100644
--- a/custom_components/atmeex_cloud/climate.py
+++ b/custom_components/atmeex_cloud/climate.py
@@ -4,7 +4,14 @@ from datetime import datetime, timezone
 import logging
 import math
 from typing import Any, Callable, Awaitable
-from .helpers import c_to_deci, deci_to_c, quantize_humidity, humidity_to_stage, HUM_ALLOWED
+from .helpers import (
+    HUM_ALLOWED,
+    breezer_mode_from_state,
+    c_to_deci,
+    deci_to_c,
+    humidity_to_stage,
+    quantize_humidity,
+)
 from .entity_base import (
     AtmeexEntityMixin,
     setup_dynamic_device_entities,
@@ -211,7 +218,9 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             confirmed_temp,
         )
         temp_valid = isinstance(effective_temp, (int, float)) and effective_temp >= 100
-        damp_pos = self._device_state.get("damp_pos")
+        damp_pos = self._state_with_pending(
+            "damp_pos", self._device_state.get("damp_pos")
+        )
         recirculation = damp_pos == 1
         if temp_valid and not recirculation:
             return HVACMode.HEAT
@@ -264,7 +273,11 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
 
     @property
     def target_temperature(self) -> float | None:
-        target = deci_to_c(self._device_state.get("u_temp_room"))
+        target = deci_to_c(
+            self._state_with_pending(
+                "u_temp_room", self._device_state.get("u_temp_room")
+            )
+        )
         if (
             target is not None
             and self._attr_min_temp <= target <= self._attr_max_temp
@@ -339,7 +352,9 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
         """
         if not self._has_humidifier():
             return None
-        stg = self._device_state.get("hum_stg")
+        stg = self._state_with_pending(
+            "hum_stg", self._device_state.get("hum_stg")
+        )
         if not isinstance(stg, (int, float)):
             stg = 0
         stg = max(0, min(3, int(stg)))
@@ -430,11 +445,15 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
 
     @property
     def swing_mode(self) -> str | None:
-        """Текущий режим заслонки / бризера."""
-        pos = self._device_state.get("damp_pos")
-        if isinstance(pos, int) and 0 <= pos <= 3:
-            return BREEZER_SWING_MODES[pos]
-        return None
+        """Current breezer mode; same mapping as the breezer-mode select."""
+        return breezer_mode_from_state(
+            self._state_with_pending(
+                "damp_pos", self._device_state.get("damp_pos")
+            ),
+            self._state_with_pending(
+                "pwr_on", self._device_state.get("pwr_on")
+            ),
+        )
 
     async def async_set_swing_mode(self, swing_mode: str) -> None:
         if (
diff --git a/custom_components/atmeex_cloud/helpers.py b/custom_components/atmeex_cloud/helpers.py
index b4e9944..1170401 100644
--- a/custom_components/atmeex_cloud/helpers.py
+++ b/custom_components/atmeex_cloud/helpers.py
@@ -4,6 +4,8 @@ import logging
 from collections.abc import Mapping
 from typing import Any
 
+from .const import BREEZER_MODES
+
 _LOGGER = logging.getLogger(__name__)
 
 FAN_MIN = 1
@@ -135,6 +137,25 @@ def humidity_to_stage(val: int | float | None) -> int:
     return HUM_ALLOWED.index(q)
 
 
+def breezer_mode_from_state(damp_pos: Any, pwr_on: Any) -> str | None:
+    """Map damper position + power to the app's breezer mode name.
+
+    supply_valve is virtual: power off with the damper at 0 (see
+    AtmeexApi.set_breezer_mode). Unknown combinations map to None.
+    """
+    if not isinstance(damp_pos, int) or isinstance(damp_pos, bool):
+        return None
+    if damp_pos == 0:
+        if pwr_on is False:
+            return BREEZER_MODES[3]
+        if pwr_on is True:
+            return BREEZER_MODES[0]
+        return None
+    if pwr_on is True and 0 < damp_pos < 3:
+        return BREEZER_MODES[damp_pos]
+    return None
+
+
 _TRUE_LITERALS = frozenset({"1", "true", "on", "yes"})
 _FALSE_LITERALS = frozenset({"", "0", "false", "off", "no"})
 
diff --git a/custom_components/atmeex_cloud/select.py b/custom_components/atmeex_cloud/select.py
index 6912bd4..c29f375 100644
--- a/custom_components/atmeex_cloud/select.py
+++ b/custom_components/atmeex_cloud/select.py
@@ -7,6 +7,7 @@ from homeassistant.helpers.update_coordinator import CoordinatorEntity
 
 from .api import AtmeexDevice
 from .entity_base import AtmeexEntityMixin, setup_dynamic_device_entities, supports_humidifier
+from .helpers import breezer_mode_from_state
 
 from .const import BREEZER_MODES, HUMIDIFICATION_OPTIONS
 from . import AtmeexRuntimeData
@@ -135,23 +136,14 @@ class AtmeexBreezerSelect(_BaseSelect):
 
     @property
     def current_option(self) -> str | None:
-        position = self._state_with_pending(
-            "damp_pos", self._device_state.get("damp_pos")
+        return breezer_mode_from_state(
+            self._state_with_pending(
+                "damp_pos", self._device_state.get("damp_pos")
+            ),
+            self._state_with_pending(
+                "pwr_on", self._device_state.get("pwr_on")
+            ),
         )
-        power = self._state_with_pending(
-            "pwr_on", self._device_state.get("pwr_on")
-        )
-        if not isinstance(position, int) or isinstance(position, bool):
-            return None
-        if position == 0:
-            if power is False:
-                return BREEZER_OPTIONS[3]
-            if power is True:
-                return BREEZER_OPTIONS[0]
-            return None
-        if power is True and 0 < position < 3:
-            return BREEZER_OPTIONS[position]
-        return None
 
     async def async_select_option(self, option: str) -> None:
         if not isinstance(option, str) or option not in BREEZER_OPTIONS:
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/test_climate.py b/tests/test_climate.py
index b044774..41e4458 100644
--- a/tests/test_climate.py
+++ b/tests/test_climate.py
@@ -1651,3 +1651,24 @@ async def test_sleep_fan_target_is_pending_during_blocked_confirmation():
     release_confirmation.set()
     await task
     assert fan_mode_during_confirmation == "2"
+
+
+def test_climate_swing_mode_matches_select_for_supply_valve():
+    # supply_valve is "power off + damper 0"; climate used to report
+    # supply_ventilation here while the select reported supply_valve.
+    ent, _cond, _api = _make_entity({"pwr_on": False, "damp_pos": 0})
+
+    assert ent.swing_mode == "supply_valve"
+
+
+def test_climate_targets_show_pending_command_values():
+    ent, _cond, _api, runtime = _make_entity_with_runtime()
+    runtime.set_pending(1, "u_temp_room", 240)
+    runtime.set_pending(1, "damp_pos", 1)
+    runtime.set_pending(1, "hum_stg", 3)
+
+    assert ent.target_temperature == 24.0
+    assert ent.swing_mode == "recirculation"
+    assert ent.target_humidity == HUM_ALLOWED[3]
+    # Pending recirculation also drives HEAT -> FAN_ONLY without waiting.
+    assert ent.hvac_mode == HVACMode.FAN_ONLY
diff --git a/tests/test_helpers.py b/tests/test_helpers.py
index 2616b49..5b7859f 100644
--- a/tests/test_helpers.py
+++ b/tests/test_helpers.py
@@ -233,3 +233,23 @@ def test_normalize_device_state_uses_settings_and_fan_fallback():
 
 def test_resolve_update_interval_invalid_input_falls_back_to_default():
     assert atmeex_init._resolve_update_interval_seconds({CONF_UPDATE_INTERVAL: "bad"}) == DEFAULT_UPDATE_INTERVAL
+
+
+@pytest.mark.parametrize(
+    ("damp_pos", "pwr_on", "expected"),
+    [
+        (0, True, "supply_ventilation"),
+        (1, True, "recirculation"),
+        (2, True, "mixed_mode"),
+        (0, False, "supply_valve"),
+        (1, False, None),
+        (0, None, None),
+        (3, True, None),
+        (True, True, None),
+        (None, True, None),
+    ],
+)
+def test_breezer_mode_from_state(damp_pos, pwr_on, expected):
+    from custom_components.atmeex_cloud.helpers import breezer_mode_from_state
+
+    assert breezer_mode_from_state(damp_pos, pwr_on) == expected
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_climate.py tests/test_helpers.py -k "supply_valve or pending_command_values or breezer_mode_from_state" -v`
Expected: `11 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `668 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/helpers.py custom_components/atmeex_cloud/select.py tests/test_climate.py tests/test_helpers.py
git commit -m "fix: show pending climate targets and unify breezer-mode mapping"
```

### Task 3: Send only the fields whose cloud target differs

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py`
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/command_executor.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Modify: `custom_components/atmeex_cloud/fan.py`
- Modify: `custom_components/atmeex_cloud/select.py`
- Modify: `custom_components/atmeex_cloud/switch.py`
- Test: `tests/test_api.py`
- Test: `tests/test_climate.py`
- Test: `tests/test_command_executor.py`
- Test: `tests/test_fan.py`

**Why:** Every action costs 2–3 sequential PUTs: setting a temperature sends `set_power(True)` then the temperature, a fan-speed change sends speed then power, a preset sends up to three. Automations that re-send the same value (e.g. fan speed from CO2 on every update) send 2 PUTs each time. Changing the valve mode while the breezer is on sends `{u_pwr_on: true, u_damp_pos: N}` — the exact `fa6b806` pattern the cloud is known to mishandle.

**What:**

Three pieces, each forced by a failure found while prototyping:

1. **Decide inside the lock, not at call time.** `AtmeexCommandExecutor` records which generation is running (`_running`) and exposes `settled_value(device, field, confirmed)`: inside an operation it ignores that command's own pending value and everything queued after it, but still counts older writes that succeeded without confirmation. *Why:* deciding at call time made a queued "set temperature" skip its power-on because an earlier queued "turn on" was pending — and when that earlier command failed the breezer stayed off (`test_queued_fan_command_rechecks_confirmed_power_after_owner_failure` guards this).
2. **Compare against the cloud's stored targets (`settings.u_*`), not reported `condition`.** `AtmeexEntityMixin._target_setting` maps state fields to settings keys. *Why:* `condition` lags until the device acknowledges; once the 10 s optimistic value expires, a stale condition made the next command skip a write the device still needed (the TTL-expiry preset tests caught this). Targets are updated the moment the cloud accepts the PUT and stay valid while the device is off. If a target is missing the field is simply sent (safe fallback).
3. **After any failed operation, send every field until a command succeeds** (`executor.last_write_failed`). *Why:* AUTO wrote `u_auto=True`, then its fan write failed; the generation's pending values were discarded, and a queued NONE would have skipped `set_auto_mode(False)`.

Entity API:
- `_already_applied(desired)` — call-time short-circuit: nothing in flight **and** targets already match → zero requests (no PUT, no confirmation GET).
- `_fields_to_write(desired)` — call **inside** the operation; returns the fields to send.
- `_apply_breezer_mode(mode)` (shared by climate swing and the select) sends `set_breezer_mode` only when power changes, otherwise the new `AtmeexApi.set_damper_position` → `{"u_damp_pos": N}`.
- Climate `_power_on_with_target(temp_c | None, action)` replaces three copies of power+temperature logic (HEAT, FAN_ONLY, set_temperature). Fan `_turn_on_at(speed, action)` replaces two.
- Presets still always run (local BOOST/restore bookkeeping) but gate each flag/speed write.

Pending maps keep the full desired values, so the optimistic UI is unchanged. Existing test fixtures have empty `settings`, so they keep "send everything" semantics; exactly one assertion changes (the queued temperature no longer repeats a power-on that already succeeded).

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/api.py b/custom_components/atmeex_cloud/api.py
index 2477b68..e7266f8 100644
--- a/custom_components/atmeex_cloud/api.py
+++ b/custom_components/atmeex_cloud/api.py
@@ -883,6 +883,14 @@ class AtmeexApi:
             raise AtmeexProtocolError("set_breezer_mode", "invalid mode")
         await self._put_params(device_id, body, "set_breezer_mode")
 
+    async def set_damper_position(self, device_id: int | str, position: int) -> None:
+        """Move the damper only (0 supply, 1 recirculation, 2 mixed)."""
+        if position not in (0, 1, 2):
+            raise AtmeexProtocolError("set_damper_position", "invalid position")
+        await self._put_params(
+            device_id, {"u_damp_pos": position}, "set_damper_position"
+        )
+
     async def set_heater_off(self, device_id: int | str) -> None:
         """Disable the heater by sending the device's off-sentinel target temp."""
         await self._put_params(device_id, {"u_temp_room": -1000}, "set_heater_off")
diff --git a/custom_components/atmeex_cloud/climate.py b/custom_components/atmeex_cloud/climate.py
index b80f850..c16edcb 100644
--- a/custom_components/atmeex_cloud/climate.py
+++ b/custom_components/atmeex_cloud/climate.py
@@ -231,6 +231,9 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             raise self._invalid_command_value("hvac_mode", hvac_mode)
 
         if hvac_mode == HVACMode.OFF:
+            if self._already_applied({"pwr_on": False}):
+                return
+
             async def operation() -> None:
                 await self.api.set_power(self._device_id, False)
 
@@ -242,27 +245,43 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             return
 
         if hvac_mode == HVACMode.FAN_ONLY:
-            async def operation() -> None:
-                await self.api.set_power(self._device_id, True)
-                await self.api.set_heater_off(self._device_id)
-
-            await self._execute_command(
-                operation,
-                pending={"pwr_on": True, "u_temp_room": -1000},
-                translation_placeholders={"action": "enter fan-only mode"},
-            )
+            await self._power_on_with_target(None, "enter fan-only mode")
             return
 
-        target_c = self._resolve_heat_target()
+        await self._power_on_with_target(
+            self._resolve_heat_target(), "enter heat mode"
+        )
+
+    async def _power_on_with_target(
+        self, temp_c: float | None, action: str
+    ) -> None:
+        """Power on with a heater target (None = heater off), skipping no-ops.
+
+        Separate PUTs on purpose: the cloud drops u_temp_room from a combined
+        body whose u_pwr_on is unchanged (fa6b806).
+        """
+        desired = {
+            "pwr_on": True,
+            "u_temp_room": -1000 if temp_c is None else c_to_deci(temp_c),
+        }
+        if self._already_applied(desired):
+            return
 
         async def operation() -> None:
-            await self.api.set_power(self._device_id, True)
-            await self.api.set_target_temperature(self._device_id, target_c)
+            writes = self._fields_to_write(desired)
+            if "pwr_on" in writes:
+                await self.api.set_power(self._device_id, True)
+            if "u_temp_room" not in writes:
+                return
+            if temp_c is None:
+                await self.api.set_heater_off(self._device_id)
+            else:
+                await self.api.set_target_temperature(self._device_id, temp_c)
 
         await self._execute_command(
             operation,
-            pending={"pwr_on": True, "u_temp_room": c_to_deci(target_c)},
-            translation_placeholders={"action": "enter heat mode"},
+            pending=desired,
+            translation_placeholders={"action": action},
         )
 
     # ---------- температура ----------
@@ -325,15 +344,7 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
         ):
             raise self._invalid_command_value("temperature", value)
 
-        async def operation() -> None:
-            await self.api.set_power(self._device_id, True)
-            await self.api.set_target_temperature(self._device_id, target)
-
-        await self._execute_command(
-            operation,
-            pending={"pwr_on": True, "u_temp_room": c_to_deci(target)},
-            translation_placeholders={"action": "set target temperature"},
-        )
+        await self._power_on_with_target(target, "set target temperature")
 
     # ---------- влажность ----------
 
@@ -371,6 +382,8 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
         ):
             raise self._invalid_command_value("humidity", humidity)
         stage = humidity_to_stage(humidity)
+        if self._already_applied({"hum_stg": stage}):
+            return
 
         async def operation() -> None:
             await self.api.set_humid_stage(self._device_id, stage)
@@ -414,6 +427,8 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
         if not isinstance(fan_mode, str) or fan_mode not in FAN_MODES:
             raise self._invalid_command_value("fan_mode", fan_mode)
         speed = int(fan_mode)
+        if self._already_applied({"fan_speed": speed}):
+            return
         restore_intent_token = object()
         tracks_manual_baseline: bool | None = None
 
@@ -461,18 +476,7 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             or swing_mode not in BREEZER_SWING_MODES
         ):
             raise self._invalid_command_value("swing_mode", swing_mode)
-        mode = BREEZER_SWING_MODES.index(swing_mode)
-        power = mode != 3
-        damper = 0 if mode == 3 else mode
-
-        async def operation() -> None:
-            await self.api.set_breezer_mode(self._device_id, mode)
-
-        await self._execute_command(
-            operation,
-            pending={"pwr_on": power, "damp_pos": damper},
-            translation_placeholders={"action": "set breezer mode"},
-        )
+        await self._apply_breezer_mode(BREEZER_SWING_MODES.index(swing_mode))
 
     async def async_set_breezer_mode(self, mode: str) -> None:
         """Service handler: set damper/breezer mode by name."""
@@ -487,6 +491,8 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             or not 0 <= stage <= 3
         ):
             raise self._invalid_command_value("humidifier_stage", stage)
+        if self._already_applied({"hum_stg": stage}):
+            return
 
         async def operation() -> None:
             await self.api.set_humid_stage(self._device_id, stage)
@@ -608,19 +614,30 @@ class AtmeexClimateEntity(AtmeexEntityMixin, CoordinatorEntity, ClimateEntity):
             pending[_PRESET_RESTORE_FAN] = restore_fan
         if fan_target is not None:
             pending["fan_speed"] = fan_target
+        remote = {
+            key: pending[key]
+            for key in ("u_auto", "u_night", "fan_speed")
+            if key in pending
+        }
 
         async def operation() -> None:
+            # Local preset bookkeeping below still runs when nothing remote
+            # changes (e.g. BOOST while already at speed 7).
+            writes = self._fields_to_write(remote)
             if preset_mode == PRESET_AUTO:
-                await self.api.set_sleep_mode(self._device_id, False)
-                await self.api.set_auto_mode(self._device_id, True)
-            elif preset_mode == PRESET_SLEEP:
-                await self.api.set_auto_mode(self._device_id, False)
-                await self.api.set_sleep_mode(self._device_id, True)
+                if "u_night" in writes:
+                    await self.api.set_sleep_mode(self._device_id, False)
+                if "u_auto" in writes:
+                    await self.api.set_auto_mode(self._device_id, True)
             else:
-                await self.api.set_auto_mode(self._device_id, False)
-                await self.api.set_sleep_mode(self._device_id, False)
-
-            if fan_target is not None:
+                if "u_auto" in writes:
+                    await self.api.set_auto_mode(self._device_id, False)
+                if "u_night" in writes:
+                    await self.api.set_sleep_mode(
+                        self._device_id, preset_mode == PRESET_SLEEP
+                    )
+
+            if "fan_speed" in writes:
                 await self.api.set_fan_speed(self._device_id, fan_target)
 
             # Commit all local state together, after the final remote write and
diff --git a/custom_components/atmeex_cloud/command_executor.py b/custom_components/atmeex_cloud/command_executor.py
index 2561508..a722623 100644
--- a/custom_components/atmeex_cloud/command_executor.py
+++ b/custom_components/atmeex_cloud/command_executor.py
@@ -55,6 +55,10 @@ class AtmeexCommandExecutor:
         self._pending: dict[str, dict[str, list[PendingCommand]]] = {}
         self._executed_generations: set[int] = set()
         self._confirmation_ready_generations: set[int] = set()
+        # Generation whose operation currently holds each device lock.
+        self._running: dict[str, int] = {}
+        # Devices whose most recent operation failed part-way.
+        self._failed_last: set[str] = set()
 
     def _notify(self) -> None:
         """Ask entities to re-render so optimistic values show (or revert) now."""
@@ -256,6 +260,38 @@ class AtmeexCommandExecutor:
             expires_at=pending.expires_at,
         )
 
+    def last_write_failed(self, device_id: int | str) -> bool:
+        """Return whether the device's most recent operation failed."""
+        return self._device_key(device_id) in self._failed_last
+
+    def has_pending(self, device_id: int | str) -> bool:
+        """Return whether any unexpired command value exists for the device."""
+        key = self._device_key(device_id)
+        self._purge_expired(key)
+        return bool(self._pending.get(key))
+
+    def settled_value(
+        self,
+        device_id: int | str,
+        attribute: str,
+        confirmed: Any,
+    ) -> Any:
+        """Return the value a write should be compared against right now.
+
+        While an operation holds the device lock this ignores that command's
+        own pending value and anything queued after it, but still counts older
+        writes that succeeded without confirmation yet. Outside an operation
+        it is the newest pending value, else the confirmed one.
+        """
+        key = self._device_key(device_id)
+        running = self._running.get(key)
+        older = [
+            item
+            for item in self._live_stack(key, attribute)
+            if running is None or item.generation < running
+        ]
+        return deepcopy(older[-1].value) if older else confirmed
+
     def confirm(
         self,
         device_id: int | str,
@@ -317,6 +353,7 @@ class AtmeexCommandExecutor:
     def remove_device(self, device_id: int | str) -> None:
         """Clear one device without letting a command bypass its active lock."""
         key = self._device_key(device_id)
+        self._failed_last.discard(key)
         fields = self._pending.pop(key, {})
         generations = {
             item.generation for stack in fields.values() for item in stack
@@ -430,10 +467,13 @@ class AtmeexCommandExecutor:
         try:
             try:
                 async with lock:
+                    self._running[key] = generation
+                    succeeded = False
                     try:
                         # The factory is intentionally invoked only after the
                         # per-device lock has been acquired.
                         await operation()
+                        succeeded = True
                     except ApiError as err:
                         try:
                             await self._recover_after_write_failure(device_id)
@@ -458,6 +498,14 @@ class AtmeexCommandExecutor:
                         finally:
                             self._clear_generation(key, generation)
                         raise
+                    finally:
+                        self._running.pop(key, None)
+                        # A part-way failure may have applied some writes the
+                        # stored targets do not show yet.
+                        if succeeded:
+                            self._failed_last.discard(key)
+                        else:
+                            self._failed_last.add(key)
 
                     if self._generation_is_referenced(generation):
                         self._executed_generations.add(generation)
diff --git a/custom_components/atmeex_cloud/entity_base.py b/custom_components/atmeex_cloud/entity_base.py
index 73f0fa9..13c9711 100644
--- a/custom_components/atmeex_cloud/entity_base.py
+++ b/custom_components/atmeex_cloud/entity_base.py
@@ -13,6 +13,18 @@ from .command_executor import (
     RecoveryRefresh,
 )
 from .const import DOMAIN
+from .helpers import api_to_fan_speed, parse_atmeex_bool
+
+# State field -> (cloud settings key, converter into the state field's scale).
+_SETTING_FOR_FIELD: dict[str, tuple[str, Callable[[Any], Any]]] = {
+    "pwr_on": ("u_pwr_on", parse_atmeex_bool),
+    "fan_speed": ("u_fan_speed", api_to_fan_speed),
+    "u_temp_room": ("u_temp_room", int),
+    "damp_pos": ("u_damp_pos", int),
+    "hum_stg": ("u_hum_stg", int),
+    "u_auto": ("u_auto", parse_atmeex_bool),
+    "u_night": ("u_night", parse_atmeex_bool),
+}
 
 
 class AtmeexEntityMixin:
@@ -62,15 +74,9 @@ class AtmeexEntityMixin:
             confirmed_value,
         )
 
-    async def _execute_command(
-        self,
-        operation: CommandCoroutineFactory,
-        *,
-        pending: Mapping[str, Any],
-        translation_key: str = "command_failed",
-        translation_placeholders: Mapping[str, str] | None = None,
-    ) -> bool:
-        """Execute one lazy command through the entry-owned executor."""
+    @property
+    def _command_executor(self) -> AtmeexCommandExecutor:
+        """Entry-owned executor, or a lazily created per-entity fallback."""
         runtime = getattr(self, "_runtime", None)
         executor = getattr(runtime, "command_executor", None)
         if executor is None:
@@ -80,7 +86,91 @@ class AtmeexEntityMixin:
                     lambda _device_id: self._refresh()
                 )
                 self._fallback_command_executor = executor
-        return await executor.async_execute(
+        return executor
+
+    def _target_setting(self, field: str) -> Any:
+        """Return the cloud's stored target for a state field, or None.
+
+        Targets (settings.u_*) are updated by the cloud as soon as it accepts
+        a PUT and stay valid while the device is off, unlike the reported
+        condition, which lags until the device acknowledges.
+        """
+        settings_key, convert = _SETTING_FOR_FIELD[field]
+        raw = self._device.settings.get(settings_key)
+        if raw is None:
+            return None
+        try:
+            return convert(raw)
+        except (TypeError, ValueError):
+            return None
+
+    def _fields_to_write(self, desired: Mapping[str, Any]) -> dict[str, Any]:
+        """Return the desired fields whose cloud target differs.
+
+        Inside a command operation this compares against the target *before*
+        that command (stored settings plus older unconfirmed writes), so a
+        queued predecessor can never make this command skip a needed write.
+        After a failed write the stored targets may be stale, so everything
+        is sent until a command succeeds again.
+        """
+        executor = self._command_executor
+        if executor.last_write_failed(self._device_id):
+            return dict(desired)
+        return {
+            field: value
+            for field, value in desired.items()
+            if executor.settled_value(
+                self._device_id, field, self._target_setting(field)
+            )
+            != value
+        }
+
+    def _already_applied(self, desired: Mapping[str, Any]) -> bool:
+        """True when nothing is in flight and the device already matches.
+
+        Lets repeated identical calls (e.g. an automation re-sending the same
+        fan speed on every CO2 update) cost zero requests.
+        """
+        return not self._command_executor.has_pending(
+            self._device_id
+        ) and not self._fields_to_write(desired)
+
+    async def _apply_breezer_mode(self, mode: int) -> None:
+        """Set breezer mode 0..3, sending only what changes.
+
+        u_pwr_on rides in the combined body only when it actually changes; with
+        power unchanged the damper is sent alone, because the cloud drops
+        fields from a combined body whose u_pwr_on is unchanged (fa6b806).
+        """
+        power = mode != 3
+        damper = 0 if mode == 3 else mode
+        desired = {"pwr_on": power, "damp_pos": damper}
+        if self._already_applied(desired):
+            return
+
+        async def operation() -> None:
+            writes = self._fields_to_write(desired)
+            if "pwr_on" in writes:
+                await self.api.set_breezer_mode(self._device_id, mode)
+            elif "damp_pos" in writes:
+                await self.api.set_damper_position(self._device_id, damper)
+
+        await self._execute_command(
+            operation,
+            pending=desired,
+            translation_placeholders={"action": "set breezer mode"},
+        )
+
+    async def _execute_command(
+        self,
+        operation: CommandCoroutineFactory,
+        *,
+        pending: Mapping[str, Any],
+        translation_key: str = "command_failed",
+        translation_placeholders: Mapping[str, str] | None = None,
+    ) -> bool:
+        """Execute one lazy command through the entry-owned executor."""
+        return await self._command_executor.async_execute(
             self._device_id,
             operation,
             pending=pending,
diff --git a/custom_components/atmeex_cloud/fan.py b/custom_components/atmeex_cloud/fan.py
index 65ba66c..827d8cf 100644
--- a/custom_components/atmeex_cloud/fan.py
+++ b/custom_components/atmeex_cloud/fan.py
@@ -181,22 +181,33 @@ class AtmeexFanEntity(AtmeexEntityMixin, CoordinatorEntity, FanEntity):
             if percentage is not None
             else None
         )
+        await self._turn_on_at(speed, "turn on the fan")
+
+    async def _turn_on_at(self, speed: int | None, action: str) -> None:
+        """Power on (optionally at a speed), sending only what changes."""
+        desired: dict[str, Any] = {"pwr_on": True}
+        if speed is not None:
+            desired["fan_speed"] = speed
+        if self._already_applied(desired):
+            return
 
         async def operation() -> None:
-            if speed is not None:
+            writes = self._fields_to_write(desired)
+            if "fan_speed" in writes:
                 await self.api.set_fan_speed(self._device_id, speed)
-            await self.api.set_power(self._device_id, True)
+            if "pwr_on" in writes:
+                await self.api.set_power(self._device_id, True)
 
-        pending: dict[str, Any] = {"pwr_on": True}
-        if speed is not None:
-            pending["fan_speed"] = speed
         await self._execute_command(
             operation,
-            pending=pending,
-            translation_placeholders={"action": "turn on the fan"},
+            pending=desired,
+            translation_placeholders={"action": action},
         )
 
     async def async_turn_off(self, **kwargs) -> None:
+        if self._already_applied({"pwr_on": False}):
+            return
+
         async def operation() -> None:
             await self.api.set_power(self._device_id, False)
 
@@ -211,18 +222,6 @@ class AtmeexFanEntity(AtmeexEntityMixin, CoordinatorEntity, FanEntity):
         if percentage == 0:
             await self.async_turn_off()
             return
-        speed = self._percentage_to_speed(percentage)
-
-        async def operation() -> None:
-            await self.api.set_fan_speed(self._device_id, speed)
-            # Always assert power after changing speed. The coordinator can be
-            # stale after a predecessor's successful write but failed
-            # confirmation refresh, so its confirmed power value cannot safely
-            # decide whether this idempotent write is needed.
-            await self.api.set_power(self._device_id, True)
-
-        await self._execute_command(
-            operation,
-            pending={"fan_speed": speed, "pwr_on": True},
-            translation_placeholders={"action": "set the fan speed"},
+        await self._turn_on_at(
+            self._percentage_to_speed(percentage), "set the fan speed"
         )
diff --git a/custom_components/atmeex_cloud/select.py b/custom_components/atmeex_cloud/select.py
index c29f375..ae9bac8 100644
--- a/custom_components/atmeex_cloud/select.py
+++ b/custom_components/atmeex_cloud/select.py
@@ -106,6 +106,8 @@ class AtmeexHumidificationSelect(_BaseSelect):
         if not isinstance(option, str) or option not in HUM_OPTIONS:
             raise self._invalid_value("humidification_option", option)
         stage = 0 if option == "off" else int(option)
+        if self._already_applied({"hum_stg": stage}):
+            return
 
         async def operation() -> None:
             await self.api.set_humid_stage(self._device_id, stage)
@@ -148,15 +150,4 @@ class AtmeexBreezerSelect(_BaseSelect):
     async def async_select_option(self, option: str) -> None:
         if not isinstance(option, str) or option not in BREEZER_OPTIONS:
             raise self._invalid_value("breezer_option", option)
-        mode = BREEZER_OPTIONS.index(option)
-        power = mode != 3
-        damper = 0 if mode == 3 else mode
-
-        async def operation() -> None:
-            await self.api.set_breezer_mode(self._device_id, mode)
-
-        await self._execute_command(
-            operation,
-            pending={"pwr_on": power, "damp_pos": damper},
-            translation_placeholders={"action": "set breezer mode"},
-        )
+        await self._apply_breezer_mode(BREEZER_OPTIONS.index(option))
diff --git a/custom_components/atmeex_cloud/switch.py b/custom_components/atmeex_cloud/switch.py
index 8925007..d4002ec 100644
--- a/custom_components/atmeex_cloud/switch.py
+++ b/custom_components/atmeex_cloud/switch.py
@@ -73,6 +73,9 @@ class _BaseSwitch(AtmeexEntityMixin, CoordinatorEntity, SwitchEntity):
         value: bool,
         action: str,
     ) -> None:
+        if self._already_applied({attribute: value}):
+            return
+
         async def operation() -> None:
             await api_method(self._device_id, value)
 
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/test_api.py b/tests/test_api.py
index b592484..069758c 100644
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -1299,3 +1299,25 @@ async def test_set_power_and_heat_body(pwr_on, temp_c, expected_body):
     req = session.requests[0]
     assert req[0] == "PUT"
     assert req[2] == expected_body
+
+
+@pytest.mark.asyncio
+async def test_set_damper_position_sends_only_the_damper():
+    session = FakeSession()
+    session.queue_response(FakeResponse(200))
+    api = AtmeexApi(session)
+    api._token = "t"
+
+    await api.set_damper_position(1, 2)
+
+    assert session.requests[0][0] == "PUT"
+    assert session.requests[0][2] == {"u_damp_pos": 2}
+
+
+@pytest.mark.asyncio
+async def test_set_damper_position_rejects_virtual_supply_valve():
+    api = AtmeexApi(FakeSession())
+    api._token = "t"
+
+    with pytest.raises(AtmeexProtocolError, match="set_damper_position"):
+        await api.set_damper_position(1, 3)
diff --git a/tests/test_climate.py b/tests/test_climate.py
index 41e4458..5da168e 100644
--- a/tests/test_climate.py
+++ b/tests/test_climate.py
@@ -939,7 +939,9 @@ async def test_temperature_avoids_compound_write_after_stale_power_on_confirmati
     release_first_confirmation.set()
     await asyncio.gather(first, queued)
 
-    assert api.set_power.await_args_list == [call(1, True), call(1, True)]
+    # The first power-on succeeded (its pending value is still live), so the
+    # queued command does not repeat it.
+    assert api.set_power.await_args_list == [call(1, True)]
     assert api.set_target_temperature.await_args_list == [
         call(1, 20.0),
         call(1, 23.0),
@@ -1672,3 +1674,76 @@ def test_climate_targets_show_pending_command_values():
     assert ent.target_humidity == HUM_ALLOWED[3]
     # Pending recirculation also drives HEAT -> FAN_ONLY without waiting.
     assert ent.hvac_mode == HVACMode.FAN_ONLY
+
+
+def _with_targets(ent, **settings):
+    """Give the entity's device stored cloud targets (settings.u_*)."""
+    ent.coordinator.data["device_map"]["1"] = AtmeexDevice.from_raw(
+        {"id": 1, "name": "Test Device", "online": True,
+         "condition": {}, "settings": settings}
+    )
+
+
+@pytest.mark.asyncio
+async def test_temperature_skips_power_write_when_target_already_on():
+    ent, _cond, api, runtime = _make_entity_with_runtime()
+    _with_targets(ent, u_pwr_on=True, u_temp_room=225)
+
+    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
+
+    api.set_power.assert_not_awaited()
+    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
+
+
+@pytest.mark.asyncio
+async def test_identical_temperature_sends_nothing():
+    ent, _cond, api, runtime = _make_entity_with_runtime()
+    _with_targets(ent, u_pwr_on=True, u_temp_room=225)
+
+    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 22.5})
+
+    api.set_power.assert_not_awaited()
+    api.set_target_temperature.assert_not_awaited()
+    runtime.refresh_device.assert_not_awaited()
+
+
+@pytest.mark.asyncio
+async def test_swing_change_while_on_sends_damper_alone():
+    ent, _cond, api, _runtime = _make_entity_with_runtime()
+    api.set_damper_position = AsyncMock()
+    _with_targets(ent, u_pwr_on=True, u_damp_pos=0)
+
+    await ent.async_set_swing_mode("recirculation")
+
+    # A combined body with an unchanged u_pwr_on can lose fields (fa6b806).
+    api.set_damper_position.assert_awaited_once_with(1, 1)
+    api.set_breezer_mode.assert_not_awaited()
+
+
+@pytest.mark.asyncio
+async def test_swing_change_that_powers_on_uses_combined_mode_write():
+    ent, _cond, api, _runtime = _make_entity_with_runtime()
+    api.set_damper_position = AsyncMock()
+    _with_targets(ent, u_pwr_on=False, u_damp_pos=0)
+
+    await ent.async_set_swing_mode("recirculation")
+
+    api.set_breezer_mode.assert_awaited_once_with(1, 1)
+    api.set_damper_position.assert_not_awaited()
+
+
+@pytest.mark.asyncio
+async def test_command_after_failed_write_sends_every_field():
+    ent, _cond, api, _runtime = _make_entity_with_runtime()
+    _with_targets(ent, u_pwr_on=True, u_temp_room=225)
+    api.set_target_temperature.side_effect = [
+        ApiError("set_target_temperature", "failed", status=503),
+        None,
+    ]
+
+    with pytest.raises(HomeAssistantError):
+        await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
+    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
+
+    # Stored targets may be stale after a failure: power is asserted again.
+    api.set_power.assert_awaited_once_with(1, True)
diff --git a/tests/test_command_executor.py b/tests/test_command_executor.py
index c403c7b..e748971 100644
--- a/tests/test_command_executor.py
+++ b/tests/test_command_executor.py
@@ -969,3 +969,56 @@ async def test_on_change_failure_never_fails_the_command():
     assert await executor.async_execute(
         1, AsyncMock(), pending={"pwr_on": True}, translation_key="command_failed"
     )
+
+
+@pytest.mark.asyncio
+async def test_settled_value_ignores_own_and_newer_generations():
+    executor = AtmeexCommandExecutor(AsyncMock())
+    executor.set_pending(1, "pwr_on", True)  # older, still unconfirmed
+    seen: list[object] = []
+    release = asyncio.Event()
+
+    async def first() -> None:
+        seen.append(executor.settled_value(1, "pwr_on", "confirmed"))
+        await release.wait()
+
+    owner = asyncio.create_task(
+        executor.async_execute(
+            1, first, pending={"pwr_on": False}, translation_key="command_failed"
+        )
+    )
+    await asyncio.sleep(0)
+    # A newer queued value must not leak into the running command's view.
+    queued = asyncio.create_task(
+        executor.async_execute(
+            1, AsyncMock(), pending={"pwr_on": None}, translation_key="command_failed"
+        )
+    )
+    await asyncio.sleep(0)
+    release.set()
+    await asyncio.gather(owner, queued)
+
+    assert seen == [True]
+    # Outside any operation it is simply the newest pending value.
+    executor.set_pending(1, "fan_speed", 4)
+    assert executor.settled_value(1, "fan_speed", 2) == 4
+    assert executor.settled_value(1, "hum_stg", 2) == 2
+
+
+@pytest.mark.asyncio
+async def test_last_write_failed_until_next_success():
+    executor = AtmeexCommandExecutor(AsyncMock())
+
+    async def failing() -> None:
+        raise ApiError("set_power", "rejected", status=503)
+
+    with pytest.raises(HomeAssistantError):
+        await executor.async_execute(
+            1, failing, pending={"pwr_on": True}, translation_key="command_failed"
+        )
+    assert executor.last_write_failed(1) is True
+
+    await executor.async_execute(
+        1, AsyncMock(), pending={"pwr_on": True}, translation_key="command_failed"
+    )
+    assert executor.last_write_failed(1) is False
diff --git a/tests/test_fan.py b/tests/test_fan.py
index c073c1a..3f0d966 100644
--- a/tests/test_fan.py
+++ b/tests/test_fan.py
@@ -270,3 +270,35 @@ async def test_async_setup_entry_adds_entities_for_new_devices():
 
     listeners[0]()
     assert len(entities) == 2
+
+
+def _fan_with_targets(**settings):
+    fan, cond, api, coordinator = _make_fan_entity()
+    coordinator.data["device_map"] = {
+        "1": AtmeexDevice.from_raw(
+            {"id": 1, "online": True, "condition": {}, "settings": settings}
+        )
+    }
+    return fan, api, coordinator
+
+
+@pytest.mark.asyncio
+async def test_fan_speed_change_while_on_skips_power_write():
+    fan, api, _coordinator = _fan_with_targets(u_pwr_on=True, u_fan_speed=2)
+
+    await fan.async_set_percentage(75)  # speed 5
+
+    api.set_fan_speed.assert_awaited_once_with(1, 5)
+    api.set_power.assert_not_awaited()
+
+
+@pytest.mark.asyncio
+async def test_fan_repeated_identical_speed_sends_nothing():
+    # API speed 2 == HA speed 3 == 43%; e.g. a CO2 automation re-sending it.
+    fan, api, coordinator = _fan_with_targets(u_pwr_on=True, u_fan_speed=2)
+
+    await fan.async_set_percentage(43)
+
+    api.set_fan_speed.assert_not_awaited()
+    api.set_power.assert_not_awaited()
+    coordinator.async_request_refresh.assert_not_awaited()
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_api.py tests/test_command_executor.py tests/test_climate.py tests/test_fan.py -k "damper_position or settled_value or last_write_failed or skips_power or identical or damper_alone or combined_mode or after_failed_write or stale_power_on" -v`
Expected: `14 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `679 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/command_executor.py custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/fan.py custom_components/atmeex_cloud/select.py custom_components/atmeex_cloud/switch.py tests/test_api.py tests/test_climate.py tests/test_command_executor.py tests/test_fan.py
git commit -m "perf: send only the command fields whose cloud target differs"
```

### Task 4: Short confirmation GET; resend writes that provably never arrived

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/api.py`
- Modify: `custom_components/atmeex_cloud/const.py`
- Test: `tests/test_api.py`
- Test: `tests/test_refresh_device.py`
- Test: `tests/test_websocket_integration.py`

**Why:**

- The post-command confirmation GET retries 3 × 20 s while holding the device lock, so one slow cloud response blocks every later command for that breezer (and the calling automation) for up to ~100 s. Confirmation is already best-effort: a failure returns `False` and keeps the optimistic value.
- PUTs are never retried, so one network blip loses the user's command with an error.

**What:**

- `AtmeexApi.get_device(device_id, *, quick=False)`; `quick=True` = one attempt with `API_CONFIRM_TIMEOUT_SEC = 10`. `_refresh_device_once` uses it; `_REFRESH_TASK_TIMEOUT_SEC` drops 65 → 35 s (10 s GET + room for a 20 s token sign-in).
- `_request` gains `max_attempts` / `timeout` parameters. Writes are resent **at most once** and only when `_provably_undelivered(err)`: HTTP 429, 502, 503, or an `aiohttp.ClientConnectorError` (connection never established). Timeouts, 500 and 504 are never resent — the cloud may have applied the write. Values are absolute and the per-device lock serializes commands, so a resend cannot interleave with another HA command.

Test fallout: fake `get_device` functions accept `**_kwargs`; one assertion expects `quick=True`.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/__init__.py b/custom_components/atmeex_cloud/__init__.py
index 6c8b6ac..c585d47 100644
--- a/custom_components/atmeex_cloud/__init__.py
+++ b/custom_components/atmeex_cloud/__init__.py
@@ -57,10 +57,10 @@ _LOGGER = logging.getLogger(__name__)
 # asyncio.wait returns after this limit even if the task is stuck in finally.
 _UNLOAD_TASK_TIMEOUT_SEC: float = 5.0
 
-# Upper-bound for awaiting a coalesced in-flight refresh_device task.
-# Sized to cover all retries (RETRY_MAX_ATTEMPTS=3 × 20 s timeout + headroom).
+# Upper-bound for awaiting a coalesced in-flight refresh_device task. The
+# confirmation GET is one 10 s attempt; headroom covers a token sign-in (20 s).
 # Module-level so tests can monkeypatch it without touching the closure.
-_REFRESH_TASK_TIMEOUT_SEC: float = 65.0
+_REFRESH_TASK_TIMEOUT_SEC: float = 35.0
 
 from .runtime import AtmeexRuntimeData
 
@@ -366,7 +366,7 @@ async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
         baseline = runtime_data.state_store.capture_device(device_id)
         unexpected_failure: AtmeexProtocolError | None = None
         try:
-            full: AtmeexDevice = await api.get_device(device_id)
+            full: AtmeexDevice = await api.get_device(device_id, quick=True)
         except AtmeexApiError as err:
             _report_targeted_failure(device_id, err)
             raise
diff --git a/custom_components/atmeex_cloud/api.py b/custom_components/atmeex_cloud/api.py
index e7266f8..c93d18a 100644
--- a/custom_components/atmeex_cloud/api.py
+++ b/custom_components/atmeex_cloud/api.py
@@ -12,7 +12,7 @@ import time
 from typing import Any
 from urllib.parse import quote
 
-from aiohttp import ClientSession, ClientError, ClientResponse
+from aiohttp import ClientConnectorError, ClientSession, ClientError, ClientResponse
 from yarl import URL
 
 from .helpers import (
@@ -26,6 +26,7 @@ from .privacy import anonymous_device_label
 from .const import (
     API_AUTH_TIMEOUT_SEC,
     API_BASE_URL,
+    API_CONFIRM_TIMEOUT_SEC,
     API_REQUEST_TIMEOUT_SEC,
     RETRY_BASE_DELAY_SEC,
     RETRY_MAX_ATTEMPTS,
@@ -139,6 +140,15 @@ def _error_for_status(
     return AtmeexProtocolError(operation, "request rejected", status=status)
 
 
+def _provably_undelivered(err: AtmeexApiError) -> bool:
+    """True when the cloud cannot have applied the request, so resending is safe."""
+    return (
+        isinstance(err, AtmeexRateLimitError)
+        or err.status in (502, 503)
+        or isinstance(err.__cause__, ClientConnectorError)
+    )
+
+
 def _device_url(device_id: int | str, suffix: str = "") -> URL:
     """Build a URL whose device identifier remains one opaque path segment."""
     canonical_id = normalize_device_id(device_id)
@@ -616,6 +626,7 @@ class AtmeexApi:
         snapshot: _TokenSnapshot,
         json_body: Any | None,
         expect_json: bool,
+        timeout: float,
     ) -> Any:
         request_url = path if isinstance(path, URL) else f"{API_BASE_URL}{path}"
         try:
@@ -624,7 +635,7 @@ class AtmeexApi:
                 request_url,
                 json=json_body,
                 headers=self._headers_for(snapshot),
-                timeout=API_REQUEST_TIMEOUT_SEC,
+                timeout=timeout,
             ) as response:
                 return await self._consume_response(
                     response,
@@ -644,9 +655,12 @@ class AtmeexApi:
         operation: str,
         json_body: Any | None = None,
         expect_json: bool = True,
+        max_attempts: int | None = None,
+        timeout: float = API_REQUEST_TIMEOUT_SEC,
     ) -> Any:
         await self._ensure_token()
-        max_attempts = RETRY_MAX_ATTEMPTS if method == "GET" else 2
+        if max_attempts is None:
+            max_attempts = RETRY_MAX_ATTEMPTS if method == "GET" else 2
         retry_delay = RETRY_BASE_DELAY_SEC
         recovered_auth = False
 
@@ -660,6 +674,7 @@ class AtmeexApi:
                     snapshot=snapshot,
                     json_body=json_body,
                     expect_json=expect_json,
+                    timeout=timeout,
                 )
             except AtmeexAuthenticationError as err:
                 if err.status != 401:
@@ -674,7 +689,12 @@ class AtmeexApi:
                     await self._recover_locked(snapshot, err.status or 401)
                 recovered_auth = True
             except (AtmeexConnectionError, AtmeexRateLimitError) as err:
-                if method != "GET" or attempt == max_attempts:
+                # Writes are resent only when the cloud provably never applied
+                # them; after a timeout or a 500 a late duplicate could race
+                # the user's next command.
+                if attempt == max_attempts or (
+                    method != "GET" and not _provably_undelivered(err)
+                ):
                     raise
                 self._retry_count += 1
                 wait = (
@@ -799,13 +819,21 @@ class AtmeexApi:
             )
         return devices
 
-    async def get_device(self, device_id: int | str) -> AtmeexDevice:
+    async def get_device(
+        self,
+        device_id: int | str,
+        *,
+        quick: bool = False,
+    ) -> AtmeexDevice:
+        """Fetch one device; quick=True makes one short attempt (confirmation)."""
         canonical_id = normalize_device_id(device_id)
         payload = await self._request(
             "GET",
             _device_url(device_id),
             operation="get_device",
             expect_json=True,
+            max_attempts=1 if quick else None,
+            timeout=API_CONFIRM_TIMEOUT_SEC if quick else API_REQUEST_TIMEOUT_SEC,
         )
         if not isinstance(payload, dict):
             raise AtmeexProtocolError(
diff --git a/custom_components/atmeex_cloud/const.py b/custom_components/atmeex_cloud/const.py
index c8fd402..4c97467 100644
--- a/custom_components/atmeex_cloud/const.py
+++ b/custom_components/atmeex_cloud/const.py
@@ -13,6 +13,8 @@ API_BASE_URL = "https://api.iot.atmeex.com"
 INTEGRATION_VERSION = "0.10.0"
 USER_AGENT = f"AtmeexCloudHomeAssistant/{INTEGRATION_VERSION}"
 API_REQUEST_TIMEOUT_SEC = 20
+# Post-command confirmation GET: best effort, so one short attempt.
+API_CONFIRM_TIMEOUT_SEC = 10
 API_AUTH_TIMEOUT_SEC = 20
 RETRY_MAX_ATTEMPTS = 3
 RETRY_BASE_DELAY_SEC = 1.0
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/test_api.py b/tests/test_api.py
index 069758c..a0ccbdf 100644
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -21,6 +21,7 @@ from custom_components.atmeex_cloud.api import (
 )
 from custom_components.atmeex_cloud.const import (
     API_AUTH_TIMEOUT_SEC,
+    API_CONFIRM_TIMEOUT_SEC,
     API_REQUEST_TIMEOUT_SEC,
     INTEGRATION_VERSION,
 )
@@ -1321,3 +1322,80 @@ async def test_set_damper_position_rejects_virtual_supply_valve():
 
     with pytest.raises(AtmeexProtocolError, match="set_damper_position"):
         await api.set_damper_position(1, 3)
+
+
+@pytest.mark.asyncio
+async def test_quick_get_device_is_one_short_attempt(monkeypatch):
+    session = FakeSession()
+    session.queue_response(FakeResponse(503))
+    api = AtmeexApi(session)
+    api._token = "t"
+    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
+
+    with pytest.raises(AtmeexConnectionError):
+        await api.get_device(1, quick=True)
+
+    assert len(session.requests) == 1
+    assert session.requests[0][4] == API_CONFIRM_TIMEOUT_SEC
+
+
+def _connector_error():
+    from aiohttp import ClientConnectorError
+
+    return ClientConnectorError(MagicMock(), OSError("connection refused"))
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    "first_failure",
+    [FakeResponse(503), FakeResponse(502), FakeResponse(429), "connector"],
+    ids=["503", "502", "429", "connect-refused"],
+)
+async def test_put_is_resent_once_when_provably_undelivered(monkeypatch, first_failure):
+    session = FakeSession()
+    session.queue_response(
+        _connector_error() if first_failure == "connector" else first_failure
+    )
+    session.queue_response(FakeResponse(200))
+    api = AtmeexApi(session)
+    api._token = "t"
+    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
+
+    await api.set_fan_speed(1, 3)
+
+    assert [request[0] for request in session.requests] == ["PUT", "PUT"]
+    assert session.requests[1][2] == {"u_fan_speed": 2}
+
+
+@pytest.mark.asyncio
+@pytest.mark.parametrize(
+    "failure",
+    [FakeResponse(500), FakeResponse(504), asyncio.TimeoutError(), ClientError()],
+    ids=["500", "504", "timeout", "client-error"],
+)
+async def test_put_is_not_resent_when_it_may_have_applied(monkeypatch, failure):
+    session = FakeSession()
+    session.queue_response(failure)
+    api = AtmeexApi(session)
+    api._token = "t"
+    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
+
+    with pytest.raises(AtmeexConnectionError):
+        await api.set_fan_speed(1, 3)
+
+    assert len(session.requests) == 1
+
+
+@pytest.mark.asyncio
+async def test_put_is_resent_at_most_once(monkeypatch):
+    session = FakeSession()
+    session.queue_response(FakeResponse(503))
+    session.queue_response(FakeResponse(503))
+    api = AtmeexApi(session)
+    api._token = "t"
+    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
+
+    with pytest.raises(AtmeexConnectionError):
+        await api.set_fan_speed(1, 3)
+
+    assert len(session.requests) == 2
diff --git a/tests/test_refresh_device.py b/tests/test_refresh_device.py
index f2ce25a..0ce55f8 100644
--- a/tests/test_refresh_device.py
+++ b/tests/test_refresh_device.py
@@ -58,7 +58,7 @@ class FakeApi:
         # первый полный опрос — список устройств (включённое)
         self.get_devices = AsyncMock(return_value=[self._dev_initial])
 
-        def _get_device_side_effect(device_id):
+        def _get_device_side_effect(device_id, **_kwargs):
             """Targeted refresh returns the newer device snapshot."""
             return self._dev_refreshed
 
@@ -125,7 +125,7 @@ async def test_refresh_device_updates_coordinator_data(monkeypatch):
         translation_key="command_failed",
     )
 
-    runtime.api.get_device.assert_awaited_once_with(1)
+    runtime.api.get_device.assert_awaited_once_with(1, quick=True)
     assert runtime.coordinator.data["states"]["1"]["pwr_on"] is False
 
 
@@ -164,7 +164,7 @@ async def test_refresh_device_preserves_newer_websocket_field(monkeypatch):
             self.login = AsyncMock()
             self.get_devices = AsyncMock(return_value=[initial])
 
-        async def get_device(self, _device_id):
+        async def get_device(self, _device_id, **_kwargs):
             get_started.set()
             await release_get.wait()
             return stale_refresh
diff --git a/tests/test_websocket_integration.py b/tests/test_websocket_integration.py
index 8855900..0f5fd7a 100644
--- a/tests/test_websocket_integration.py
+++ b/tests/test_websocket_integration.py
@@ -196,7 +196,7 @@ async def test_websocket_batch_message_updates_coordinator_once(monkeypatch):
             self._devices = [dev1, dev2]
             self.get_devices = AsyncMock(return_value=self._devices)
 
-            async def _get_device(device_id):
+            async def _get_device(device_id, **_kwargs):
                 for dev in self._devices:
                     if dev.id == int(device_id):
                         return dev
@@ -1102,7 +1102,7 @@ async def test_cancelled_refresh_waiter_does_not_cancel_owner(monkeypatch):
     api.release = asyncio.Event()
     api.get_device_calls = 0
 
-    async def blocked_get_device(device_id):
+    async def blocked_get_device(device_id, **_kwargs):
         api.get_device_calls += 1
         api.started.set()
         await api.release.wait()
@@ -1470,7 +1470,7 @@ async def test_refresh_device_coalesces_parallel_requests(monkeypatch):
             self.get_devices = AsyncMock(return_value=[self._dev])
             self.get_device_calls = 0
 
-            async def _get_device(_device_id):
+            async def _get_device(_device_id, **_kwargs):
                 self.get_device_calls += 1
                 if self._block_get_device:
                     await self._gate.wait()
@@ -1552,7 +1552,7 @@ async def test_refresh_device_hung_task_times_out_for_second_caller(monkeypatch)
             # requests; the first targeted refresh should block for this test.
             self._get_device_calls = 0
 
-            async def _gate_get_device(_device_id):
+            async def _gate_get_device(_device_id, **_kwargs):
                 self._get_device_calls += 1
                 if self._get_device_calls >= 1:
                     task_started.set()  # notify that we reached the blocking point
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_api.py -k "quick_get_device or resent" -v`
Expected: `10 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `689 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/const.py tests/test_api.py tests/test_refresh_device.py tests/test_websocket_integration.py
git commit -m "fix: quick confirmation GET and resend provably undelivered writes"
```

### Task 5: Keep entities available through short cloud outages

**Files:**
- Modify: `custom_components/atmeex_cloud/const.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Test: `tests/conftest.py`
- Test: `tests/test_coordinator.py`
- Test: `tests/test_entity_base.py`

**Why:** `AtmeexEntityMixin.available` returns False whenever `coordinator.last_update_success` is False, so one failed poll makes every entity of every breezer unavailable — and HA then silently drops their commands. WebSocket pushes flip `last_update_success` back to True, so availability flaps.

**What:** New `AtmeexCoordinator.inventory_stale`: True only when the last successful inventory is older than `STALE_INVENTORY_INTERVALS` (3) × update interval (90 s by default), or there never was one. `available` uses it instead of `last_update_success`. Because HA does not notify listeners on a second consecutive failure, `_async_update_data` calls `self.async_update_listeners()` in both failure branches so entities re-check staleness. `DummyCoordinator` mirrors the property and `_max_inventory_age_seconds`.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/const.py b/custom_components/atmeex_cloud/const.py
index 4c97467..862c982 100644
--- a/custom_components/atmeex_cloud/const.py
+++ b/custom_components/atmeex_cloud/const.py
@@ -41,4 +41,7 @@ DEFAULT_UPDATE_INTERVAL = 30
 DEFAULT_ENABLE_WEBSOCKET = True
 DEFAULT_ENABLE_CO2 = True
 MIN_UPDATE_INTERVAL = 10
+# Entities go unavailable only after this many update intervals without a
+# successful inventory poll (one failed poll must not block every command).
+STALE_INVENTORY_INTERVALS = 3
 MAX_UPDATE_INTERVAL = 300
diff --git a/custom_components/atmeex_cloud/coordinator.py b/custom_components/atmeex_cloud/coordinator.py
index 39d1929..64810f4 100644
--- a/custom_components/atmeex_cloud/coordinator.py
+++ b/custom_components/atmeex_cloud/coordinator.py
@@ -29,7 +29,12 @@ from .api import (
     AtmeexProtocolError,
     AtmeexRateLimitError,
 )
-from .const import DOMAIN, EVENT_API_ERROR, WS_LOGBOOK_MIN_INTERVAL_SEC
+from .const import (
+    DOMAIN,
+    EVENT_API_ERROR,
+    STALE_INVENTORY_INTERVALS,
+    WS_LOGBOOK_MIN_INTERVAL_SEC,
+)
 from .helpers import normalize_device_id, parse_atmeex_bool
 from .state_store import AtmeexStateStore
 
@@ -119,6 +124,19 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         self._last_detail_error: AtmeexApiError | None = None
         self._last_detail_failure_count = 0
 
+    @property
+    def inventory_stale(self) -> bool:
+        """True once no authoritative inventory has succeeded for too long.
+
+        A single failed poll must not mark every breezer unavailable: Home
+        Assistant silently drops service calls to unavailable entities.
+        """
+        last = self.last_inventory_success_mono
+        return last is None or (
+            time.monotonic() - last
+            > STALE_INVENTORY_INTERVALS * self._max_inventory_age_seconds
+        )
+
     def _fire_api_error_event(self, data: dict[str, Any]) -> None:
         """Emit one throttled API-error event without masking the API error."""
         now = time.monotonic()
@@ -404,6 +422,9 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         except AtmeexAuthenticationError as err:
             self.last_api_error = err
             self._fire_api_error_event(self._safe_error_event(err))
+            # HA notifies listeners only when success flips, so re-check
+            # entity availability (inventory_stale) on every failure.
+            self.async_update_listeners()
             raise ConfigEntryAuthFailed(
                 f"{err.operation} failed"
             ) from err
@@ -414,6 +435,7 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         ) as err:
             self.last_api_error = err
             self._fire_api_error_event(self._safe_error_event(err))
+            self.async_update_listeners()
             raise UpdateFailed(f"{err.operation} failed") from err
 
         # Disassociate only devices the store confirmed absent across two
diff --git a/custom_components/atmeex_cloud/entity_base.py b/custom_components/atmeex_cloud/entity_base.py
index 13c9711..c36b6c3 100644
--- a/custom_components/atmeex_cloud/entity_base.py
+++ b/custom_components/atmeex_cloud/entity_base.py
@@ -200,15 +200,14 @@ class AtmeexEntityMixin:
 
     @property
     def available(self) -> bool:
-        """Entity availability = coordinator health AND device online.
+        """Entity availability = inventory not stale AND device online.
 
-        Honors coordinator health so a stale integration (e.g. the cloud has
-        been unreachable for several polls) does not keep presenting last-known
-        device state as if it were live.
+        Tolerates short cloud hiccups (see AtmeexCoordinator.inventory_stale)
+        but stops presenting last-known state once the outage is sustained.
         """
         coordinator = getattr(self, "coordinator", None)
-        if coordinator is not None and not getattr(
-            coordinator, "last_update_success", True
+        if coordinator is not None and getattr(
+            coordinator, "inventory_stale", False
         ):
             return False
         st = self._device_state
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index b6770e2..474d31c 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -20,6 +20,8 @@ class DummyCoordinator:
     avoids the full HA machinery.
     """
 
+    inventory_stale = AtmeexCoordinator.inventory_stale
+
     def __init__(
         self,
         hass=None,
@@ -38,6 +40,9 @@ class DummyCoordinator:
         self.logger = logger
         self.name = name
         self.update_interval = update_interval
+        self._max_inventory_age_seconds = (
+            update_interval.total_seconds() if update_interval else 30.0
+        )
         self.api = api
         self.state_store = state_store
         self.config_entry_id = config_entry_id
diff --git a/tests/test_coordinator.py b/tests/test_coordinator.py
index c891166..7474dbf 100644
--- a/tests/test_coordinator.py
+++ b/tests/test_coordinator.py
@@ -348,3 +348,31 @@ def test_fire_api_error_event_throttles_repeated_events(monkeypatch):
         EVENT_API_ERROR,
         {"message": "third", "suppressed_errors": 1},
     )
+
+
+def test_inventory_stale_tolerates_a_short_outage():
+    coord, _api = _make_coordinator()  # 30 s interval -> stale after 90 s
+
+    coord.last_inventory_success_mono = time.monotonic() - 89
+    assert coord.inventory_stale is False
+    coord.last_inventory_success_mono = time.monotonic() - 91
+    assert coord.inventory_stale is True
+    coord.last_inventory_success_mono = None
+    assert coord.inventory_stale is True
+
+
+@pytest.mark.asyncio
+async def test_failed_poll_renotifies_entities_to_recheck_availability():
+    coord, api = _make_coordinator()
+    await coord._async_update_data()
+    coord.async_update_listeners = MagicMock()
+    api.get_devices = AsyncMock(
+        side_effect=AtmeexConnectionError("get_devices", "offline")
+    )
+
+    with pytest.raises(UpdateFailed):
+        await coord._async_update_data()
+
+    # HA skips listeners on a second consecutive failure; we must not.
+    coord.async_update_listeners.assert_called_once_with()
+    assert coord.inventory_stale is False  # one failure is tolerated
diff --git a/tests/test_entity_base.py b/tests/test_entity_base.py
index 07191f8..d354342 100644
--- a/tests/test_entity_base.py
+++ b/tests/test_entity_base.py
@@ -322,26 +322,28 @@ class _BareEntity(AtmeexEntityMixin):
         self._device_meta = meta
 
 
-def _availability_coordinator(*, success: bool, online: bool):
+def _availability_coordinator(*, stale: bool, online: bool):
     dev = AtmeexDevice.from_raw(
         {"id": 1, "online": online, "condition": {}, "settings": {}}
     )
     return SimpleNamespace(
         data={"states": {"1": {"online": online}}, "device_map": {"1": dev}},
-        last_update_success=success,
+        # A single failed poll flips this to False; it must not matter.
+        last_update_success=False,
+        inventory_stale=stale,
     ), dev
 
 
 @pytest.mark.parametrize(
-    ("success", "online", "expected"),
+    ("stale", "online", "expected"),
     [
-        (True, True, True),    # healthy + online -> available
-        (True, False, False),  # healthy + offline -> unavailable
-        (False, True, False),  # coordinator unhealthy -> unavailable regardless
-        (False, False, False),
+        (False, True, True),   # fresh inventory + online -> available
+        (False, False, False), # fresh inventory + offline -> unavailable
+        (True, True, False),   # sustained outage -> unavailable regardless
+        (True, False, False),
     ],
 )
-def test_available_honors_coordinator_health_and_online(success, online, expected):
-    coordinator, dev = _availability_coordinator(success=success, online=online)
+def test_available_honors_inventory_staleness_and_online(stale, online, expected):
+    coordinator, dev = _availability_coordinator(stale=stale, online=online)
     entity = _BareEntity(coordinator, 1, dev)
     assert entity.available is expected
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_coordinator.py tests/test_entity_base.py -k "inventory_stale or renotifies or staleness" -v`
Expected: `6 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `691 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/const.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/entity_base.py tests/conftest.py tests/test_coordinator.py tests/test_entity_base.py
git commit -m "fix: keep entities available through short cloud outages"
```

### Task 6: One breezer's failed detail fetch must not fail the whole poll

**Files:**
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Test: `tests/test_detail_hydration.py`

**Why:** When a list item lacks core state, the coordinator fetches its details. If that fetch fails and there is no earlier detail to reuse (a new device, or the very first refresh), the whole poll fails — at startup the entire integration fails to load because of one (e.g. offline) breezer.

**What:** `_hydrate_one` returns the list item as-is plus the error. The error is still reported through `last_api_error` and the throttled API-error event.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/coordinator.py b/custom_components/atmeex_cloud/coordinator.py
index 64810f4..86f6488 100644
--- a/custom_components/atmeex_cloud/coordinator.py
+++ b/custom_components/atmeex_cloud/coordinator.py
@@ -258,7 +258,9 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         ) as err:
             previous = self._previous_device(listed)
             if previous is None:
-                raise
+                # No earlier detail to reuse: publish the list item as-is
+                # rather than failing every device (and, at startup, setup).
+                return listed, err
             return self._merge_detail_source(listed, previous), err
 
     @staticmethod
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/test_detail_hydration.py b/tests/test_detail_hydration.py
index 17a5d78..d4c8f61 100644
--- a/tests/test_detail_hydration.py
+++ b/tests/test_detail_hydration.py
@@ -307,25 +307,24 @@ async def test_transient_detail_failure_uses_canonical_previous_and_list_delta(
 
 
 @pytest.mark.asyncio
-async def test_transient_detail_failure_without_previous_aborts_refresh(
+async def test_detail_failure_without_previous_publishes_list_item(
     hass,
 ) -> None:
+    # One breezer's failed detail fetch must not fail every device (or, on
+    # the first refresh, the whole config entry).
     api = AsyncMock()
-    api.get_devices.return_value = [_device(1)]
+    api.get_devices.return_value = [_device(1), _complete(2)]
     api.get_device.side_effect = AtmeexConnectionError(
         "get_device",
         "detail unavailable",
     )
-    store = AtmeexStateStore()
-    prior = store.data
-    coordinator = _coordinator(hass, api, store=store)
+    coordinator = _coordinator(hass, api, store=AtmeexStateStore())
 
-    with pytest.raises(UpdateFailed, match="get_device failed"):
-        await coordinator._async_update_data()
+    result = await coordinator._async_update_data()
 
-    assert store.data is prior
-    assert coordinator.last_success_ts is None
-    assert coordinator.last_inventory_success_mono is None
+    assert set(result["device_map"]) == {"1", "2"}
+    assert coordinator.last_inventory_success_mono is not None
+    assert isinstance(coordinator.last_api_error, AtmeexConnectionError)
 
 
 @pytest.mark.asyncio
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_detail_hydration.py -k publishes_list_item -v`
Expected: `1 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `691 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/coordinator.py tests/test_detail_hydration.py
git commit -m "fix: publish the list item when one device's detail fetch fails"
```

### Task 7: Reset WebSocket backoff only after a stable connection

**Files:**
- Modify: `custom_components/atmeex_cloud/websocket.py`
- Test: `tests/test_websocket_manager.py`

**Why:** `_connect_once` resets the reconnect backoff on every successful handshake. A server that accepts and immediately closes is retried every second forever (~86 000 handshakes/day), which risks rate limiting that would also hit commands.

**What:** The backoff resets in `_listen`'s `finally` only if the connection lived at least `WebSocketConfig.stable_connection` (`WS_STABLE_CONNECTION_SEC = 60`); a shorter-lived connection counts as a transport failure (1, 2, 4 … 60 s).

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/websocket.py b/custom_components/atmeex_cloud/websocket.py
index 625cb23..c9790ae 100644
--- a/custom_components/atmeex_cloud/websocket.py
+++ b/custom_components/atmeex_cloud/websocket.py
@@ -34,6 +34,10 @@ WS_RECONNECT_DELAY_MAX = 60.0  # seconds
 WS_PING_INTERVAL = 30.0  # seconds
 WS_PING_TIMEOUT = 10.0  # seconds
 WS_MAX_UNAUTHORIZED_BEFORE_REAUTH = 5
+# A connection must stay up this long before the reconnect backoff resets;
+# a server that accepts and immediately closes would otherwise be retried
+# every WS_RECONNECT_DELAY_MIN seconds forever.
+WS_STABLE_CONNECTION_SEC = 60.0
 
 MessageCallback = Callable[[dict[str, Any]], bool]
 TokenRecoveryCallback = Callable[[], Awaitable[None]]
@@ -65,6 +69,7 @@ class WebSocketConfig:
     reconnect_delay_max: float = WS_RECONNECT_DELAY_MAX
     ping_interval: float = WS_PING_INTERVAL
     ping_timeout: float = WS_PING_TIMEOUT
+    stable_connection: float = WS_STABLE_CONNECTION_SEC
 
 
 class WebSocketManager:
@@ -157,8 +162,6 @@ class WebSocketManager:
             self._record_transport_failure()
             return False
 
-        self._transport_failures = 0
-        self._reconnect_delay = self._config.reconnect_delay_min
         self._ws = ws
         listen_coro = self._listen(ws)
         self._listen_task = None
@@ -257,6 +260,7 @@ class WebSocketManager:
 
     async def _listen(self, ws: ClientWebSocketResponse) -> None:
         """Listen on one captured WebSocket until it closes."""
+        connected_at = time.monotonic()
         try:
             async for msg in ws:
                 if not self._running:
@@ -285,8 +289,13 @@ class WebSocketManager:
         except asyncio.CancelledError:
             raise
         except (aiohttp.ClientError, asyncio.TimeoutError):
-            self._record_transport_failure()
+            pass  # counted below like any other short-lived connection
         finally:
+            if time.monotonic() - connected_at >= self._config.stable_connection:
+                self._transport_failures = 0
+                self._reconnect_delay = self._config.reconnect_delay_min
+            else:
+                self._record_transport_failure()
             try:
                 if not ws.closed:
                     await ws.close()
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/test_websocket_manager.py b/tests/test_websocket_manager.py
index fe3999f..0f42644 100644
--- a/tests/test_websocket_manager.py
+++ b/tests/test_websocket_manager.py
@@ -1037,38 +1037,49 @@ async def test_unauthorized_triggers_reauth_after_max_failures():
 
 
 @pytest.mark.asyncio
-async def test_reconnect_backoff_resets_on_success_even_after_prior_auth_failure():
-    """Backoff delay must reset to minimum on every successful connection.
+async def test_immediately_closed_connection_grows_backoff():
+    """A server that accepts and then closes must not be retried every second."""
+
+    class _AcceptThenCloseSession:
+        async def ws_connect(self, *args, **kwargs):
+            return _FakeWebSocket()  # iteration ends at once
+
+    cfg = WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0)
+    manager = _manager(_AcceptThenCloseSession(), config=cfg)
+    manager._transport_failures = 3
+    manager._reconnect_delay = 8.0
+
+    assert await manager._connect_once() is True
+    await manager._listen_task
+
+    assert manager._reconnect_delay == 16.0
+    assert manager._transport_failures == 4
+    await manager.disconnect()
 
-    Transport backoff resets independently from application authentication.
-    """
-    connected = asyncio.Event()
 
-    class _SuccessfulSession:
+@pytest.mark.asyncio
+async def test_backoff_resets_after_stable_connection_even_after_prior_auth_failure():
+    """Transport backoff resets independently from application authentication."""
+
+    class _AcceptThenCloseSession:
         async def ws_connect(self, *args, **kwargs):
-            connected.set()
             return _FakeWebSocket()
 
-    cfg = WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0)
-    manager = _manager(
-        _SuccessfulSession(),
-        on_message=AsyncMock(),
-        config=cfg,
+    cfg = WebSocketConfig(
+        reconnect_delay_min=1.0,
+        reconnect_delay_max=60.0,
+        stable_connection=0.0,  # any lifetime counts as stable here
     )
-
-    # Simulate that a prior auth failure bumped the counter and the backoff
+    manager = _manager(_AcceptThenCloseSession(), config=cfg)
     manager._application_unauthorized_count = 2
     manager._transport_failures = 3
-    manager._reconnect_delay = 30.0  # elevated from prior failures
+    manager._reconnect_delay = 30.0
 
-    result = await manager._connect_once()
+    assert await manager._connect_once() is True
+    await manager._listen_task
 
-    assert result is True
-    # A successful transport does not prove application authentication.
-    assert manager._reconnect_delay == cfg.reconnect_delay_min, (
-        f"Backoff not reset: {manager._reconnect_delay} != {cfg.reconnect_delay_min}"
-    )
+    assert manager._reconnect_delay == cfg.reconnect_delay_min
     assert manager._transport_failures == 0
+    # A stable transport does not prove application authentication.
     assert manager._application_unauthorized_count == 2
-
     await manager.disconnect()
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_websocket_manager.py -k "immediately_closed or stable_connection" -v`
Expected: `2 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `692 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/websocket.py tests/test_websocket_manager.py
git commit -m "fix: reset WebSocket backoff only after a stable connection"
```

### Task 8: Key the inventory watchdog on the last poll attempt

**Files:**
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Test: `tests/conftest.py`
- Test: `tests/test_inventory_age.py`

**Why:** The watchdog forces a refresh when the last *successful* inventory is older than the update interval and runs every half interval. During a cloud outage the success timestamp never moves, so the watchdog adds a forced poll on top of every scheduled one — double the requests exactly when the cloud struggles.

**What:** `_async_update_data` stamps `last_inventory_attempt_mono` first; `async_ensure_inventory_fresh` checks that instead. It still forces a poll when WebSocket publications keep postponing HA's timer (no attempt at all). `DummyCoordinator` mirrors the attribute.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/coordinator.py b/custom_components/atmeex_cloud/coordinator.py
index 86f6488..9228860 100644
--- a/custom_components/atmeex_cloud/coordinator.py
+++ b/custom_components/atmeex_cloud/coordinator.py
@@ -115,6 +115,7 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         self.last_api_error: AtmeexApiError | None = None
         self.last_success_ts: float | None = None
         self.last_inventory_success_mono: float | None = None
+        self.last_inventory_attempt_mono: float | None = None
         self.avg_latency_ms: float | None = None
         self.request_retries = 0
         self._max_inventory_age_seconds = update_interval.total_seconds()
@@ -375,17 +376,19 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
         *,
         now_mono: float | None = None,
     ) -> bool:
-        """Force an authoritative refresh if the inventory is older than the age cap.
+        """Force an authoritative refresh if no poll was attempted recently.
 
         Continuous WebSocket push traffic keeps device *state* fresh without any
         authoritative ``/devices`` poll, so a removed or renamed device could
-        otherwise linger indefinitely. Returns True when a refresh was requested.
+        otherwise linger indefinitely. Keyed on the last *attempt*, so a cloud
+        outage (scheduled polls failing) is not polled twice as often.
+        Returns True when a refresh was requested.
         """
         now = time.monotonic() if now_mono is None else now_mono
-        last_success = self.last_inventory_success_mono
+        last_attempt = self.last_inventory_attempt_mono
         if (
-            last_success is not None
-            and now - last_success < self._max_inventory_age_seconds
+            last_attempt is not None
+            and now - last_attempt < self._max_inventory_age_seconds
         ):
             return False
         await self.async_request_refresh()
@@ -410,6 +413,7 @@ class AtmeexCoordinator(DataUpdateCoordinator[AtmeexCoordinatorData]):
 
     async def _async_update_data(self) -> AtmeexCoordinatorData:
         """Fetch one authoritative inventory or report a truthful failure."""
+        self.last_inventory_attempt_mono = time.monotonic()
         baselines = self.state_store.capture_all()
         started = time.perf_counter()
         self._last_detail_error = None
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 474d31c..239ad09 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -54,6 +54,7 @@ class DummyCoordinator:
         self.last_api_error = kwargs.get("last_api_error")
         self.last_success_ts = kwargs.get("last_success_ts")
         self.last_inventory_success_mono = None
+        self.last_inventory_attempt_mono = None
         self.avg_latency_ms = None
         self.request_retries = 0
         # Extra counter used by some WS tests
diff --git a/tests/test_inventory_age.py b/tests/test_inventory_age.py
index 4438eed..bc95ffc 100644
--- a/tests/test_inventory_age.py
+++ b/tests/test_inventory_age.py
@@ -27,7 +27,7 @@ def _coordinator(hass) -> AtmeexCoordinator:
 async def test_fresh_inventory_does_not_force_refresh(hass) -> None:
     coordinator = _coordinator(hass)
     coordinator.async_request_refresh = AsyncMock()
-    coordinator.last_inventory_success_mono = 1000.0
+    coordinator.last_inventory_attempt_mono = 1000.0
 
     # Only 10s old (< 30s cap) -> no forced refresh.
     forced = await coordinator.async_ensure_inventory_fresh(now_mono=1010.0)
@@ -40,7 +40,7 @@ async def test_fresh_inventory_does_not_force_refresh(hass) -> None:
 async def test_stale_inventory_forces_one_refresh(hass) -> None:
     coordinator = _coordinator(hass)
     coordinator.async_request_refresh = AsyncMock()
-    coordinator.last_inventory_success_mono = 1000.0
+    coordinator.last_inventory_attempt_mono = 1000.0
 
     # 31s old (> 30s cap) -> exactly one authoritative refresh.
     forced = await coordinator.async_ensure_inventory_fresh(now_mono=1031.0)
@@ -53,9 +53,23 @@ async def test_stale_inventory_forces_one_refresh(hass) -> None:
 async def test_never_polled_inventory_forces_refresh(hass) -> None:
     coordinator = _coordinator(hass)
     coordinator.async_request_refresh = AsyncMock()
-    coordinator.last_inventory_success_mono = None
+    coordinator.last_inventory_attempt_mono = None
 
     forced = await coordinator.async_ensure_inventory_fresh(now_mono=5.0)
 
     assert forced is True
     coordinator.async_request_refresh.assert_awaited_once_with()
+
+
+@pytest.mark.asyncio
+async def test_failing_polls_are_not_doubled_by_the_watchdog(hass) -> None:
+    coordinator = _coordinator(hass)
+    coordinator.async_request_refresh = AsyncMock()
+    # Cloud outage: last success long ago, but the scheduled poll just ran.
+    coordinator.last_inventory_success_mono = 100.0
+    coordinator.last_inventory_attempt_mono = 1000.0
+
+    forced = await coordinator.async_ensure_inventory_fresh(now_mono=1010.0)
+
+    assert forced is False
+    coordinator.async_request_refresh.assert_not_awaited()
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_inventory_age.py -v`
Expected: `4 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `693 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/coordinator.py tests/conftest.py tests/test_inventory_age.py
git commit -m "fix: key the inventory watchdog on the last poll attempt"
```

### Task 9: Keep absent breezers offline for a grace period instead of deleting them

**Files:**
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/const.py`
- Modify: `custom_components/atmeex_cloud/state_store.py`
- Test: `tests/conftest.py`
- Test: `tests/test_coordinator.py`
- Test: `tests/test_inventory_removal.py`
- Test: `tests/test_state_store.py`

**Why:** A breezer missing from two consecutive polls (~60 s) is deleted from HA together with all its entities. While a breezer is being reset and re-added to the account it is missing for minutes, so its entities, area, names and device-based automations are wiped.

**What:**

`AtmeexStateStore.apply_inventory(..., now_mono=None)` tracks `_absent_since`. After the second consecutive miss the device stays in the store with `state["online"] = False` (entities show unavailable); it is retired only after `DEVICE_RETIRE_GRACE_SEC` (7 days). A returning device merges normally and comes back online. `is_confirmed_absent()` lets `async_remove_config_entry_device` allow manual deletion as soon as the device is confirmed absent, without waiting for the grace period.

Test fallout: the 7 tests that pin "removed on the second miss" request a new `no_retire_grace` fixture (grace = 0), so they keep testing the counting rules; new tests cover the grace path.

**Step 1: Apply the production change**

```diff
diff --git a/custom_components/atmeex_cloud/__init__.py b/custom_components/atmeex_cloud/__init__.py
index c585d47..2b4f888 100644
--- a/custom_components/atmeex_cloud/__init__.py
+++ b/custom_components/atmeex_cloud/__init__.py
@@ -764,9 +764,9 @@ async def async_remove_config_entry_device(
 ) -> bool:
     """Authoritatively allow device removal only when the cloud confirms absence.
 
-    A device is refused while it is still in the authoritative inventory (and
-    throughout the one-miss grace period, since the store keeps publishing it
-    until the second successful absence). An absent device is allowed, and its
+    A device is refused while the authoritative inventory still lists it,
+    including its first missed poll. Once two consecutive polls missed it the
+    user may remove it without waiting for automatic retirement. Its
     per-device executor state is dropped so it doesn't accumulate across
     add/remove cycles.
     """
@@ -784,7 +784,11 @@ async def async_remove_config_entry_device(
 
     state_store = getattr(runtime, "state_store", None)
     current_ids = (
-        set(state_store.data.get("device_map", {}))
+        {
+            key
+            for key in state_store.data.get("device_map", {})
+            if not state_store.is_confirmed_absent(key)
+        }
         if state_store is not None
         else set()
     )
diff --git a/custom_components/atmeex_cloud/const.py b/custom_components/atmeex_cloud/const.py
index 862c982..d06f820 100644
--- a/custom_components/atmeex_cloud/const.py
+++ b/custom_components/atmeex_cloud/const.py
@@ -44,4 +44,8 @@ MIN_UPDATE_INTERVAL = 10
 # Entities go unavailable only after this many update intervals without a
 # successful inventory poll (one failed poll must not block every command).
 STALE_INVENTORY_INTERVALS = 3
+# A device missing from the account is shown offline after two missed polls
+# but only retired (HA device + entities deleted) after this grace period, so
+# resetting and re-adding a breezer does not wipe its Home Assistant setup.
+DEVICE_RETIRE_GRACE_SEC = 7 * 24 * 3600
 MAX_UPDATE_INTERVAL = 300
diff --git a/custom_components/atmeex_cloud/state_store.py b/custom_components/atmeex_cloud/state_store.py
index 27eff71..90c48c6 100644
--- a/custom_components/atmeex_cloud/state_store.py
+++ b/custom_components/atmeex_cloud/state_store.py
@@ -3,10 +3,12 @@ from __future__ import annotations
 from collections.abc import Mapping, Sequence
 from copy import deepcopy
 from dataclasses import dataclass
+import time
 from types import MappingProxyType
 from typing import TYPE_CHECKING, Any
 
 from .api import AtmeexDevice, AtmeexState
+from .const import DEVICE_RETIRE_GRACE_SEC
 from .helpers import normalize_device_id
 
 if TYPE_CHECKING:
@@ -79,11 +81,16 @@ class AtmeexStateStore:
         self._revision = 0
         self._field_revisions: dict[str, dict[str, int]] = {}
         self._absence_counts: dict[str, int] = {}
+        self._absent_since: dict[str, float] = {}
 
     @property
     def data(self) -> AtmeexCoordinatorData:
         return self._data
 
+    def is_confirmed_absent(self, device_id: int | str) -> bool:
+        """True once two consecutive successful polls missed the device."""
+        return self._absence_counts.get(normalize_device_id(device_id), 0) >= 2
+
     def capture_device(self, device_id: int | str) -> FieldRevisionBaseline:
         key = normalize_device_id(device_id)
         return FieldRevisionBaseline(
@@ -260,10 +267,14 @@ class AtmeexStateStore:
         self,
         devices: Sequence[AtmeexDevice],
         baselines: Mapping[str, FieldRevisionBaseline],
+        *,
+        now_mono: float | None = None,
     ) -> StateStoreUpdate:
+        now = time.monotonic() if now_mono is None else now_mono
         device_map = dict(self._data.get("device_map", {}))
         states = dict(self._data.get("states", {}))
         absence_counts = dict(self._absence_counts)
+        absent_since = dict(self._absent_since)
         canonical_baselines = {
             normalize_device_id(device_id): baseline
             for device_id, baseline in baselines.items()
@@ -288,6 +299,7 @@ class AtmeexStateStore:
             changed.update(item_changed)
             touched.update(item_touched)
             absence_counts[key] = 0
+            absent_since.pop(key, None)
 
         removed: set[str] = set()
         for key in tuple(device_map):
@@ -295,12 +307,23 @@ class AtmeexStateStore:
                 continue
             count = absence_counts.get(key, 0) + 1
             absence_counts[key] = count
+            first_missed = absent_since.setdefault(key, now)
             if count < 2:
                 continue
-            device_map.pop(key, None)
-            states.pop(key, None)
-            absence_counts.pop(key, None)
-            removed.add(key)
+            if now - first_missed >= DEVICE_RETIRE_GRACE_SEC:
+                device_map.pop(key, None)
+                states.pop(key, None)
+                absence_counts.pop(key, None)
+                absent_since.pop(key, None)
+                removed.add(key)
+                continue
+            # Missing from the account (e.g. while being re-paired): keep the
+            # HA device and its entities, but show it offline.
+            state = states.get(key, {})
+            if state.get("online") is not False:
+                states[key] = {**state, "online": False}
+                changed.add((key, "state.online"))
+                touched.add((key, "state.online"))
 
         update = self._commit(
             device_map,
@@ -310,4 +333,5 @@ class AtmeexStateStore:
             removed=frozenset(removed),
         )
         self._absence_counts = absence_counts
+        self._absent_since = absent_since
         return update
```

**Step 2: Apply the test changes**

```diff
diff --git a/tests/conftest.py b/tests/conftest.py
index 239ad09..4dc373c 100644
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -124,6 +124,14 @@ class DummyCoordinator:
             )
 
 
+@pytest.fixture
+def no_retire_grace(monkeypatch):
+    """Retire absent devices on the second missed poll (no grace period)."""
+    import custom_components.atmeex_cloud.state_store as state_store_module
+
+    monkeypatch.setattr(state_store_module, "DEVICE_RETIRE_GRACE_SEC", 0)
+
+
 def make_fake_api_class(
     *,
     devices: list[dict] | None = None,
diff --git a/tests/test_coordinator.py b/tests/test_coordinator.py
index 7474dbf..eb44aa7 100644
--- a/tests/test_coordinator.py
+++ b/tests/test_coordinator.py
@@ -175,7 +175,9 @@ async def test_malformed_nested_inventory_maps_to_update_failed_and_preserves_sn
 
 
 @pytest.mark.asyncio
-async def test_authoritative_absence_removes_device_after_two_successful_polls():
+async def test_authoritative_absence_removes_device_after_two_successful_polls(
+    no_retire_grace,
+):
     device = AtmeexDevice.from_raw(
         {
             "id": 1,
@@ -199,7 +201,7 @@ async def test_authoritative_absence_removes_device_after_two_successful_polls()
 
 
 @pytest.mark.asyncio
-async def test_failed_poll_does_not_advance_authoritative_absence():
+async def test_failed_poll_does_not_advance_authoritative_absence(no_retire_grace):
     device = AtmeexDevice.from_raw(
         {
             "id": 1,
diff --git a/tests/test_inventory_removal.py b/tests/test_inventory_removal.py
index 9b53a0e..7a8998e 100644
--- a/tests/test_inventory_removal.py
+++ b/tests/test_inventory_removal.py
@@ -43,7 +43,9 @@ def _coordinator(hass, api, entry_id="entry-1") -> AtmeexCoordinator:
 
 
 @pytest.mark.asyncio
-async def test_confirmed_stale_device_loses_config_entry_association(hass) -> None:
+async def test_confirmed_stale_device_loses_config_entry_association(
+    hass, no_retire_grace
+) -> None:
     config_entry = MockConfigEntry(domain=DOMAIN, entry_id="entry-1")
     config_entry.add_to_hass(hass)
     entry_id = config_entry.entry_id
@@ -73,7 +75,8 @@ async def test_confirmed_stale_device_loses_config_entry_association(hass) -> No
 async def test_manual_removal_refuses_active_and_allows_absent_device(hass) -> None:
     runtime = SimpleNamespace(
         state_store=SimpleNamespace(
-            data={"devices": [], "device_map": {"7": _device(7)}, "states": {}}
+            data={"devices": [], "device_map": {"7": _device(7)}, "states": {}},
+            is_confirmed_absent=lambda _device_id: False,
         ),
         command_executor=None,
     )
@@ -83,3 +86,24 @@ async def test_manual_removal_refuses_active_and_allows_absent_device(hass) -> N
 
     assert await async_remove_config_entry_device(hass, entry, active) is False
     assert await async_remove_config_entry_device(hass, entry, absent) is True
+
+
+@pytest.mark.asyncio
+async def test_manual_removal_allowed_once_confirmed_absent_before_retirement(
+    hass,
+) -> None:
+    store = AtmeexStateStore()
+    store.apply_inventory([_device(7)], store.capture_all(), now_mono=0.0)
+    store.apply_inventory([], store.capture_all(), now_mono=30.0)
+    entry = SimpleNamespace(
+        runtime_data=SimpleNamespace(state_store=store, command_executor=None)
+    )
+    device_entry = SimpleNamespace(identifiers={(DOMAIN, "7")})
+
+    # One missed poll: still refused.
+    assert await async_remove_config_entry_device(hass, entry, device_entry) is False
+
+    store.apply_inventory([], store.capture_all(), now_mono=60.0)
+    # Confirmed absent but kept for the grace period: the user may delete it.
+    assert "7" in store.data["device_map"]
+    assert await async_remove_config_entry_device(hass, entry, device_entry) is True
diff --git a/tests/test_state_store.py b/tests/test_state_store.py
index 5e7fc9c..d09fa2e 100644
--- a/tests/test_state_store.py
+++ b/tests/test_state_store.py
@@ -401,7 +401,9 @@ def test_refresh_copies_only_the_changed_target_state():
     assert update.data["states"]["2"] is before_unaffected
 
 
-def test_device_is_removed_only_after_two_successful_absent_inventories():
+def test_device_is_removed_only_after_two_successful_absent_inventories(
+    no_retire_grace,
+):
     store = seed_store(device())
 
     first = store.apply_inventory([], store.capture_all())
@@ -413,7 +415,9 @@ def test_device_is_removed_only_after_two_successful_absent_inventories():
     assert second.data == {"devices": [], "device_map": {}, "states": {}}
 
 
-def test_targeted_refresh_does_not_reset_authoritative_absence_count():
+def test_targeted_refresh_does_not_reset_authoritative_absence_count(
+    no_retire_grace,
+):
     current = device()
     store = seed_store(current)
 
@@ -471,7 +475,9 @@ def test_inventory_reappearance_resets_consecutive_absence_count():
     assert store._absence_counts == {"1": 1}
 
 
-def test_inventory_absence_count_is_not_reset_by_websocket_updates():
+def test_inventory_absence_count_is_not_reset_by_websocket_updates(
+    no_retire_grace,
+):
     store = seed_store(device())
 
     first = store.apply_inventory([], store.capture_all())
@@ -522,3 +528,35 @@ def test_inventory_owns_new_devices_under_canonical_keys():
     assert set(update.data["device_map"]) == {"7"}
     assert set(update.data["states"]) == {"7"}
     assert stored.condition["history"] == {"samples": [18]}
+
+
+def test_absent_device_stays_offline_until_retire_grace_expires():
+    from custom_components.atmeex_cloud.const import DEVICE_RETIRE_GRACE_SEC
+
+    store = seed_store(device())
+    first = store.apply_inventory([], store.capture_all(), now_mono=0.0)
+    assert first.data["states"]["1"]["online"] is True  # one miss: no change
+
+    second = store.apply_inventory([], store.capture_all(), now_mono=60.0)
+    # Missing from the account (e.g. mid re-pairing): kept, but offline.
+    assert "1" in second.data["device_map"]
+    assert second.data["states"]["1"]["online"] is False
+    assert second.removed_device_ids == frozenset()
+    assert store.is_confirmed_absent(1)
+
+    retired = store.apply_inventory(
+        [], store.capture_all(), now_mono=float(DEVICE_RETIRE_GRACE_SEC)
+    )
+    assert retired.removed_device_ids == frozenset({"1"})
+    assert "1" not in retired.data["device_map"]
+
+
+def test_returning_device_comes_back_online_and_clears_absence():
+    store = seed_store(device())
+    store.apply_inventory([], store.capture_all(), now_mono=0.0)
+    store.apply_inventory([], store.capture_all(), now_mono=60.0)
+
+    back = store.apply_inventory([device()], store.capture_all(), now_mono=120.0)
+
+    assert back.data["states"]["1"]["online"] is True
+    assert not store.is_confirmed_absent(1)
```

**Step 3: Run the new tests**

Run: `.venv/bin/python -m pytest -q --timeout=15 tests/test_state_store.py tests/test_inventory_removal.py tests/test_coordinator.py -k "grace or returning_device or confirmed_absent or absence or stale_device or manual_removal" -v`
Expected: `10 passed`.

**Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `696 passed`.

**Step 5: Commit**

```bash
git add custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/const.py custom_components/atmeex_cloud/state_store.py tests/conftest.py tests/test_coordinator.py tests/test_inventory_removal.py tests/test_state_store.py
git commit -m "fix: keep absent breezers offline for a grace period instead of deleting them"
```

### Task 10: Update CLAUDE.md invariants and README troubleshooting

**Files:**
- Modify: `CLAUDE.md`
- Modify: `README.md`

**Why:** `CLAUDE.md` now states three things that are no longer true (two-poll retirement, success-keyed watchdog, "set commands never retry") and lacks the new command-write rules. Users need guidance for breezers that drop offline.

**What:** Update the invariants, the DummyCoordinator mirror list and the retry gotcha; add the write-filtering gotcha. Add a Russian README section "Бризер уходит в офлайн" with an offline-alert / smart-plug power-cycle automation.

**Step 1: Apply the documentation change**

```diff
diff --git a/CLAUDE.md b/CLAUDE.md
index b9cc39b..38c22d4 100644
--- a/CLAUDE.md
+++ b/CLAUDE.md
@@ -111,15 +111,16 @@ The per-field revision map inside `AtmeexStateStore` is the single mechanism pre
 
 Other invariants:
 
-- A device is retired only after **two consecutive successful** polls report it absent (`_absence_counts`); a failed poll can never retire a device.
-- `async_set_updated_data` (WS/refresh publications) reschedules HA's poll timer, so busy WS traffic would postpone polls forever — the **inventory watchdog** (`async_inventory_watchdog`) forces an authoritative refresh when the inventory is older than the update interval.
+- A device missing from **two consecutive successful** polls is marked `online=False` but kept (`_absence_counts`, `_absent_since`); it is retired (HA device + entities deleted) only after `DEVICE_RETIRE_GRACE_SEC` (7 days), so re-pairing a breezer keeps its HA setup. A failed poll never advances absence. Users may delete a confirmed-absent device manually at any time.
+- `async_set_updated_data` (WS/refresh publications) reschedules HA's poll timer, so busy WS traffic would postpone polls forever — the **inventory watchdog** (`async_inventory_watchdog`) forces an authoritative refresh when no poll was *attempted* within the update interval (attempt, not success, so an outage is not polled twice as often).
+- Entity availability uses `coordinator.inventory_stale` (no successful inventory for `STALE_INVENTORY_INTERVALS` × interval), **not** `last_update_success`: HA silently drops service calls to unavailable entities, so one failed poll must not block commands. `_async_update_data` calls `async_update_listeners()` on every failure because HA only notifies when success flips.
 - Poll failures are typed: auth → `ConfigEntryAuthFailed`, transport/rate-limit/protocol → `UpdateFailed`. Never mask one as the other.
 
 ---
 
 ## DummyCoordinator — keep it in sync
 
-`tests/conftest.py` has the single `DummyCoordinator` used by integration-level tests (other files import it from there). It must have every attribute that `__init__.py` and diagnostics access on the real coordinator (`last_api_error`, `last_success_ts`, `last_inventory_success_mono`, `avg_latency_ms`, `request_retries`, the `_api_error_*` throttle fields, `_last_detail_*`), and it borrows several real `AtmeexCoordinator` methods via `getattr`. **Whenever you add an attribute to `AtmeexCoordinator.__init__`, mirror it in `DummyCoordinator`.**
+`tests/conftest.py` has the single `DummyCoordinator` used by integration-level tests (other files import it from there). It must have every attribute that `__init__.py` and diagnostics access on the real coordinator (`last_api_error`, `last_success_ts`, `last_inventory_success_mono`, `last_inventory_attempt_mono`, `_max_inventory_age_seconds`, `avg_latency_ms`, `request_retries`, the `_api_error_*` throttle fields, `_last_detail_*`, plus `async_update_listeners` and the `inventory_stale` property), and it borrows several real `AtmeexCoordinator` methods via `getattr`. **Whenever you add an attribute to `AtmeexCoordinator.__init__`, mirror it in `DummyCoordinator`.**
 
 ---
 
@@ -201,7 +202,9 @@ Use Serena for navigation (finding where things are defined or referenced) rathe
 
 **`_request()` 401 recovery path** — token recovery goes through `_recover_locked(snapshot, status)` under `self._lock`, keyed by the token *generation* captured before the failure. After acquiring the lock, a newer generation means someone else already recovered — return without a new sign-in. Never call `_sign_in()`/`_signin_refresh()` outside this mechanism.
 
-**Set commands don't retry on transport errors** — non-GET requests in `_request` use `max_attempts = 2`, and the transport/rate-limit retry branch is GET-only. The second attempt exists solely for the single 401→recover→replay cycle. This is intentional: retrying a non-idempotent PUT after a timeout can race with user input.
+**Set commands are resent at most once, and only when provably undelivered** — non-GET requests in `_request` use `max_attempts = 2`, shared by the 401→recover→replay cycle and `_provably_undelivered` failures (429, 502, 503, `ClientConnectorError`). Timeouts, 500 and 504 are never resent: the cloud may have applied the write, and a late duplicate could race the user's next command. The post-command confirmation GET (`get_device(quick=True)`) is one 10 s attempt.
+
+**Commands send only what changes — decided inside the lock** — entity commands call `_already_applied(desired)` at call time (skip entirely when nothing is in flight and the cloud's stored targets, `settings.u_*`, already match) and `_fields_to_write(desired)` *inside* the operation, where `executor.settled_value` ignores the running command's own pending value and anything queued after it. Never decide writes before `_execute_command`: a queued predecessor may still fail. After any failed operation (`executor.last_write_failed`) everything is sent until a command succeeds. `u_pwr_on` may share a PUT body only when it actually changes — the cloud drops fields from a combined body with an unchanged `u_pwr_on` (commit `fa6b806`), which is why the damper goes alone via `set_damper_position`.
 
 **Version lives in two places** — `manifest.json` and `const.INTEGRATION_VERSION` must be bumped together; `tests/test_manifest.py` fails if they drift.
 
diff --git a/README.md b/README.md
index 81de7e8..8779ffb 100644
--- a/README.md
+++ b/README.md
@@ -290,6 +290,37 @@ automation:
 4. Для аккаунтов с email: введите email и пароль заново
 5. Для аккаунтов с телефоном: подтвердите отправку SMS-кода — новое сообщение придёт только после вашего подтверждения
 
+### Бризер уходит в офлайн
+
+Интеграция не может отвязать бризер от аккаунта — она только читает состояние и отправляет команды. Если бризер теряет связь с облаком, причина обычно в Wi‑Fi‑модуле устройства или в сети (2,4 ГГц, смена канала роутером, mesh‑роуминг).
+
+- Бризер, пропавший из аккаунта (например, на время сброса и повторного добавления), показывается в Home Assistant как «Недоступно», но **не удаляется** 7 дней — после повторного добавления с тем же ID его сущности, зоны и автоматизации продолжат работать.
+- Сравните историю `binary_sensor.<имя>_online` с активностью автоматизаций: если обрывы совпадают с частыми командами — уменьшите их частоту; если обрывы случаются в одно и то же время — проверьте роутер.
+
+Уведомление (и, если бризер подключён через умную розетку, перезапуск питания), когда бризер офлайн дольше 10 минут:
+
+```yaml
+automation:
+  - alias: "Бризер офлайн"
+    trigger:
+      - platform: state
+        entity_id: binary_sensor.bedroom_breezer_online
+        to: "off"
+        for: "00:10:00"
+    action:
+      - service: notify.notify
+        data:
+          message: "Бризер в спальне офлайн 10 минут"
+      # Необязательно: перезапуск питания через умную розетку
+      - service: switch.turn_off
+        target:
+          entity_id: switch.bedroom_breezer_plug
+      - delay: "00:00:30"
+      - service: switch.turn_on
+        target:
+          entity_id: switch.bedroom_breezer_plug
+```
+
 ### Пропадает сенсор CO2
 
 Отключите сенсор CO2 в опциях интеграции, если ваше устройство не оснащено датчиком CO2.
```

**Step 2: Run the full suite**

Run: `.venv/bin/python -m pytest -q --timeout=15`
Expected: `696 passed`.

**Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: document command, availability and retirement invariants"
```

### Task 11: Verify against the real cloud

The suite uses fakes; these checks need a real Home Assistant with a real breezer.

**Step 1:** Copy `custom_components/atmeex_cloud/` to the HA config, restart, and enable debug logging for `custom_components.atmeex_cloud`.

**Step 2 — damper-only write (the one new wire format):** breezer ON in supply mode → select `recirculation`. Expected: the damper moves. This sends `{"u_damp_pos": 1}` via `set_damper_position`. If the damper does **not** move, make `_apply_breezer_mode` always call `set_breezer_mode`, and record the finding in `.planning/codebase/CONCERNS.md`.

**Step 3 — commands:** breezer ON → change the temperature and fan speed several times in quick succession. Expected: the card shows each new value at once without snapping back, and the breezer follows. Repeating the current speed sends nothing (no `API set_fan_speed` debug line).

**Step 4 — out-of-band change:** turn the breezer off in the mobile app, wait ~5 s, then set a temperature in HA. Expected: the breezer turns on. If it stays off, the cloud's payload lacks `settings.u_pwr_on`: suppression then sends everything (safe, but no savings) — note it in CONCERNS.md.

**Step 5 — offline:** cut the breezer's power for ~2 minutes. Expected: `binary_sensor.*_online` turns off, the HA device and its entities remain, and everything returns when power is restored.

---

## Out of scope (deliberately)

- **Re-render timer when an optimistic value expires** — dropped in Task 1 (lingering timer; the next poll or push re-renders).
- **`online` fallback from `condition.time`** — only matters if the cloud omits `online`; there is no evidence it does.
- **Decoupling inventory age from the update interval when the WebSocket is up** — low value.
- **Simplifying the executor's lock-cleanup machinery** — simplification, not a fix.
- **Version bump** (`manifest.json` + `const.INTEGRATION_VERSION`, kept in sync by `tests/test_manifest.py`) — do it at release time.
