# Codebase Concerns

**Analysis Date:** 2026-05-02
**Validated:** 2026-05-02 — full code review completed, all findings resolved
**Previous audit:** 2026-03-28
**Addendum:** 2026-07-17 — pre-release review of the state-store rework (see bottom). Entries above the addendum describe the pre-rework architecture; several mechanisms they reference (timestamp dicts, `state_update_lock`) were replaced by per-field revisions in `state_store.py`.

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

**Superseded 2026-07-17:** the state-store rework deliberately replaced the cooldown with strict one-shot reauth per loaded runtime (`test_websocket_reauth_is_one_shot` pins it across the former 300 s boundary). Rationale: HTTP polling independently raises `ConfigEntryAuthFailed` on genuine credential failure, which re-prompts via HA's coordinator machinery, so the WS-side prompt no longer needs to repeat. See the addendum for the accepted residual.

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

### ~~Unbounded dict growth (pending_commands/device_locks)~~ FIXED

**Severity: Low** | **Status: Resolved 2026-05-02**

`AtmeexRuntimeData.pending_commands` and `device_locks` accumulated one entry per device ID seen during the lifetime of the loaded config entry, with no cleanup on device removal.

**Fix:** `async_remove_config_entry_device` ([__init__.py:528-548](../../custom_components/atmeex_cloud/__init__.py#L528-L548)) now iterates the device entry's `(DOMAIN, ident)` identifiers and pops the corresponding keys from both `runtime.pending_commands` and `runtime.device_locks`. Defensive against missing `runtime_data` (failed setup) and unknown identifiers.

**Files changed:** `__init__.py`, `tests/test_init.py` (3 new tests)

---

## Active Concerns

No unresolved concerns identified as of 2026-05-02.

## Verified non-issues (2026-05-02)

The following items were verified against current code and remain correctly resolved by their existing mechanism:

- **Refresh tasks cache leak** — `finally` block at [__init__.py:295-297](../../custom_components/atmeex_cloud/__init__.py#L295-L297) pops with identity check (`is task`).
- **WebSocket token refresh timing** — [websocket.py:123](../../custom_components/atmeex_cloud/websocket.py#L123) calls `self._token_getter()` on every `_connect_once()`.
- **Bare `except` clauses** — `# noqa: BLE001` annotations cover logging-only / boundary catches; spot-checked at [__init__.py:104](../../custom_components/atmeex_cloud/__init__.py#L104), [config_flow.py:108](../../custom_components/atmeex_cloud/config_flow.py#L108), [websocket.py:145](../../custom_components/atmeex_cloud/websocket.py#L145).
- **Entity name collision** — `async_set_unique_id` + `_abort_if_unique_id_configured()` present at [config_flow.py:92-93](../../custom_components/atmeex_cloud/config_flow.py#L92-L93) (and reauth path).
- **Temperature parsing fragility** — `target_temperature` at [climate.py:210-215](../../custom_components/atmeex_cloud/climate.py#L210-L215) range-validates and returns `None` correctly.

---

## Addendum — 2026-07-17 pre-release review (state-store rework, v0.10.0)

Full-integration review of the 25 commits since v0.9.5.

### ~~HIGH: Options ("Configure") dialog crashed on the production path~~ FIXED

`AtmeexOptionsFlowHandler.config_entry` returned `self._config_entry`, which nothing on the production path set — HA's `OptionsFlowManager` builds the handler via `async_get_options_flow(entry)` and never sets that attribute, and the override shadowed core's own property. Opening Configure raised `AttributeError` on every supported HA version. The unit tests masked it by injecting `flow._config_entry` manually.

**Fix:** `async_get_options_flow` now attaches `handler._config_entry = config_entry`. New regression test `test_options_flow_via_flow_manager_resolves_config_entry` drives the flow through HA's real `OptionsFlowManager`.

**Files changed:** `config_flow.py`, `tests/test_config_flow.py`

### ~~MEDIUM: Release shipped without a version bump~~ FIXED

`manifest.json` and `const.INTEGRATION_VERSION` both still said `0.9.5` (the released tag) after ~24k changed lines. Bumped to **0.10.0**; `tests/test_manifest.py` now fails if the two version locations ever drift; the User-Agent assertions in `test_api.py` derive from `INTEGRATION_VERSION` instead of a literal.

**Files changed:** `manifest.json`, `const.py`, `tests/test_manifest.py` (new), `tests/test_api.py`

### ~~MEDIUM: Anonymized log labels were dead code~~ FIXED

Commit `cae5b9d` added `privacy.anonymous_device_label` but never wired it in; `__init__.py` logged raw device IDs. The targeted-refresh warning and the `set_fan_speed` debug line now use the label; `test_unexpected_refresh_failure_is_private_pending_and_recovered` asserts the label (and the absence of the raw ID) in the log text.

**Files changed:** `__init__.py`, `api.py`, `tests/test_refresh_device.py`

### ~~LOW: Unused `_inventory_refresh_lock`~~ FIXED

Created in `AtmeexCoordinator.__init__`, never acquired. Removed.

### ~~MEDIUM: CLAUDE.md / planning docs described the pre-rework architecture~~ FIXED

CLAUDE.md's concurrency section documented the removed timestamp-dict mechanism as "the single mechanism" and required stale DummyCoordinator attributes. Rewritten for the per-field revision model; stale-notice added for the other `.planning/codebase/` docs.

### Accepted / documented (no code change)

- **WS reauth is one-shot per runtime** — deliberate re-design of the 2026-05-02 cooldown (see superseded note above). Residual: a *transient* WS-only 401 storm leaves the WebSocket disabled (polling-only) until reload, with a single possibly-spurious reauth prompt. Accepted: genuine credential failure re-prompts through polling's `ConfigEntryAuthFailed`, and keeping the manager alive would retry sign-in with known-bad credentials.
- **Forward-compat coverage** — tests run against `homeassistant==2025.1.4`. Recommendation stands: a CI job that runs the suite against the latest `pytest-homeassistant-custom-component`/HA to catch API drift (the options-flow bug was exactly this class).
- **Minor:** `climate.hvac_mode` reads `damp_pos` without the pending overlay (brief HEAT/FAN_ONLY flicker after a swing-mode command); `command_executor` monkey-patches `asyncio.Lock.release` and reads the private `_waiters` attribute (well-tested, but revisit on CPython upgrades); `runtime.py`/`sensor.py` still carry migration shims.

---

*Concerns audit: 2026-05-02; addendum 2026-07-17*
*All 21 pre-rework concerns resolved (3 original + 17 code review + 1 follow-up cleanup); addendum: 5 fixed, 3 accepted/documented*
