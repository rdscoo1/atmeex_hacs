# Codebase Concerns

**Analysis Date:** 2026-05-02
**Validated:** 2026-05-02 — full code review completed, all findings resolved
**Previous audit:** 2026-03-28

---

## Resolved — Original 3 Concerns (2026-03-28)

### ~~WebSocket vs Polling State Race Condition~~ FIXED

**Severity: Medium** | **Status: Resolved 2026-03-28**

`_async_update_data()` (polling) could overwrite fresher WebSocket state.

**Fix applied:** `_ws_device_update_ts` dict records per-device monotonic timestamps on every WS update. `_async_update_data()` captures `poll_start_mono` before the network call, then preserves WS-pushed state for any device whose `_ws_device_update_ts >= poll_start_mono`.

**Files changed:** `coordinator.py`, `__init__.py`

---

### ~~Device State Normalization in Three Places~~ FIXED

**Severity: Medium (maintainability)** | **Status: Resolved 2026-03-28**

WebSocket partial-update functions were defined as inline closures in `__init__.py`, separate from `_normalize_device_state()` in `helpers.py`.

**Fix applied:** `apply_condition_update()` and `apply_settings_update()` moved to `helpers.py`.

**Files changed:** `__init__.py`, `helpers.py`

---

### ~~Unbounded WebSocket Message Queue~~ FIXED

**Severity: Low** | **Status: Resolved 2026-03-28**

`websocket_message_queue` was `deque()` with no `maxlen`.

**Fix applied:** Changed to `deque(maxlen=500)`.

**Files changed:** `__init__.py`

---

## Resolved — Code Review Findings (2026-05-02)

All 17 findings from the full code review were implemented with TDD (RED → GREEN).

### ~~HIGH: Implicit power-on in `async_set_temperature` lacked pending tracking~~ FIXED

**Status: Resolved 2026-05-02**

Setting temperature on an `OFF` device calls `set_power(True)` inside a closure but did not track `pwr_on=True` as pending, causing the UI to briefly flash `OFF` between the poll and the refresh.

**Fix:** `device_is_off` captured before the closure; `_execute_command` called with `pending_attr="pwr_on", pending_value=True` when the device is off.

**Files changed:** `climate.py`

---

### ~~HIGH: Token refresh race in `_request()` 401/403 path~~ FIXED

**Status: Resolved 2026-05-02**

The reactive sign-in on 401/403 was unprotected — N concurrent requests each called `_sign_in()` independently, overwriting each other's token.

**Fix:** Acquire `self._lock` around reactive `_sign_in()`; double-check `_token == stale_token` before signing in (double-checked locking identical to `_ensure_token()`).

**Files changed:** `api.py`

---

### ~~HIGH: `ws_reauth_started` flag never reset~~ FIXED

**Status: Resolved 2026-05-02**

Boolean flag was set to `True` on first WS auth failure and never reset, silently suppressing all subsequent reauth prompts for the lifetime of the loaded entry.

**Fix:** Replaced with `ws_reauth_last_ts: float = float("-inf")` and a `_WS_REAUTH_COOLDOWN_SEC = 300.0` throttle. Subsequent failures after the cooldown re-trigger the reauth flow.

**Files changed:** `__init__.py`

---

### ~~MEDIUM: Polling bypassed `state_update_lock` for targeted refreshes~~ FIXED

**Status: Resolved 2026-05-02**

`_async_update_data` already preserved WS state via `_ws_device_update_ts`, but a targeted `refresh_device` write that completed after `poll_start_mono` could still be overwritten.

**Fix:** Added `_refresh_device_update_ts: dict[str, float] = {}` to `AtmeexCoordinator`. `_refresh_device_once` records the timestamp after writing; `_async_update_data` has a second preservation loop mirroring the WS one.

**Files changed:** `coordinator.py`, `__init__.py`

---

### ~~MEDIUM: `refresh_tasks` dedup awaited indefinitely on a hung task~~ FIXED

**Status: Resolved 2026-05-02**

Second callers for the same device would `await in_flight` with no timeout.

**Fix:** Wrapped with `asyncio.wait_for(in_flight, timeout=_REFRESH_TASK_TIMEOUT_SEC)` (65 s). On `TimeoutError`, the key is evicted from `refresh_tasks` and the error propagates to the caller.

**Files changed:** `__init__.py`

---

### ~~MEDIUM: Token persistence failure was silent~~ FIXED

**Status: Resolved 2026-05-02**

`hass.config_entries.async_update_entry()` called bare — any storage failure lost the new refresh token silently.

**Fix:** Wrapped in `try/except Exception` with `_LOGGER.warning(...)`. No exception escapes.

**Files changed:** `__init__.py`

---

### ~~MEDIUM: Set commands retried without idempotency~~ FIXED

**Status: Resolved 2026-05-02**

`_put_params` used `_with_retries`, so a timeout after the server applied the change caused a duplicate PUT that could race with user input.

**Fix:** `_put_params` now calls `_request()` directly (no retries). Set commands fail-fast.

**Files changed:** `api.py`

---

### ~~MEDIUM: Primary `get_devices` exception too broad~~ FIXED

**Status: Resolved 2026-05-02**

`except Exception` in the primary `get_devices` call could swallow programming errors (e.g., `NameError`), masking bugs.

**Fix:** Narrowed to `except (asyncio.TimeoutError, aiohttp.ClientError)`. All other exceptions propagate.

**Files changed:** `coordinator.py`

---

### ~~MEDIUM: `async_unload_entry` had no upper bound on cancellation wait~~ FIXED

**Status: Resolved 2026-05-02**

`message_task.cancel(); await message_task` could block HA's unload if the task had a slow `finally`.

**Fix:** Both tasks now use `asyncio.wait({task}, timeout=_UNLOAD_TASK_TIMEOUT_SEC)` (5 s). Returns immediately after timeout regardless of task state.

**Files changed:** `__init__.py`

---

### ~~MEDIUM: `device_info` cached for entity lifetime~~ FIXED

**Status: Resolved 2026-05-02**

`@cached_property` froze `name`, `model`, `sw_version` at entity creation. A rename in the Atmeex app would not be reflected until entry reload.

**Fix:** Converted to plain `@property`.

**Files changed:** `entity_base.py`

---

### ~~MEDIUM: WS backoff reset gated on auth failure counter~~ FIXED

**Status: Resolved 2026-05-02**

`_reconnect_delay` was only reset `if self._consecutive_auth_failures == 0`, leaving elevated backoff after any prior auth failure even when subsequent reconnects succeeded cleanly.

**Fix:** Both `_reconnect_delay` and `_consecutive_auth_failures` unconditionally reset on every successful authenticated handshake.

**Files changed:** `websocket.py`

---

### ~~LOW: Online sensor never went stale~~ FIXED

**Status: Resolved 2026-05-02**

`AtmeexOnlineSensor.available` returned `True` unconditionally, hiding a dead coordinator.

**Fix:** `available` now returns `False` when `time.time() - last_success_ts > update_interval.total_seconds() * 3`. Gracefully handles `None` values.

**Files changed:** `binary_sensor.py`

---

### ~~LOW: Missing `EntityCategory.DIAGNOSTIC`~~ FIXED

**Status: Resolved 2026-05-02**

`AtmeexOnlineSensor` appeared on the main device card rather than the diagnostic section. (`AtmeexDiagnosticsSensor` in `sensor.py` already had it.)

**Fix:** Added `_attr_entity_category = EntityCategory.DIAGNOSTIC` to `AtmeexOnlineSensor`.

**Files changed:** `binary_sensor.py`

---

### ~~LOW: `HUM_ALLOWED.index(quantize_humidity(...))` brittle~~ FIXED

**Status: Resolved 2026-05-02**

Inline `HUM_ALLOWED.index(q)` would raise `ValueError` if `quantize_humidity` ever returned an off-list value.

**Fix:** Added `humidity_to_stage(val)` to `helpers.py` that co-locates the quantize + index in one place. `climate.py` now calls `stage = humidity_to_stage(humidity)`.

**Files changed:** `helpers.py`, `climate.py`

---

### ~~LOW: Switches didn't receive `runtime`, no pending state~~ FIXED

**Status: Resolved 2026-05-02**

`AtmeexAutoNannySwitch` and `AtmeexSleepModeSwitch` bypassed `_execute_command` entirely (own try/except) and had no `_runtime`, so rapid toggles caused UI flap.

**Fix:** `_BaseSwitch` accepts `runtime` param; both switches route through `_execute_command` with `pending_attr`/`pending_value`; `is_on` uses `_state_with_pending` for optimistic display.

**Files changed:** `switch.py`

---

### ~~NIT: Over-broad `except Exception` in coordinator~~ FIXED

**Status: Resolved 2026-05-02**

Same as MEDIUM #8 above — `coordinator.py` primary get_devices path narrowed.

---

### ~~NIT: `PendingCommand` re-export undocumented~~ RESOLVED

**Status: Resolved 2026-05-02**

`PendingCommand` is re-exported in `__init__.__all__` and imported by `test_race_protection.py` and `test_climate.py`. A comment was added documenting the intent.

**Files changed:** `__init__.py`

---

## Active Concerns

No unresolved concerns identified as of 2026-05-02.

Items previously in "Dismissed Concerns" (2026-03-28) remain non-issues:
- Unbounded dict growth (pending_commands/device_locks) — typical 1-5 devices, negligible memory
- Refresh tasks cache leak — `finally` block always executes correctly
- WebSocket token refresh timing — `_token_getter()` called on every `_connect_once()`
- Bare `except` clauses marked intentional — `# noqa: BLE001` annotations remain appropriate
- Entity name collision — `async_set_unique_id` + `_abort_if_unique_id_configured()` prevents duplicates
- Temperature parsing fragility — `target_temperature` validates range; `None` returns are correct HA semantics

---

*Concerns audit: 2026-05-02*
*All 20 concerns resolved (3 original + 17 code review)*
