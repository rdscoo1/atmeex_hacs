# Codebase Concerns

**Analysis Date:** 2026-03-28
**Validated:** 2026-03-28 — audited against actual code, reduced from 14 to 3 real concerns
**Fixed:** 2026-03-28 — all 3 concerns resolved

## ~~WebSocket vs Polling State Race Condition~~ FIXED

**Severity: Medium** | **Status: Resolved**

`_async_update_data()` (polling) could overwrite fresher WebSocket state because it did not coordinate with `_apply_websocket_message()` which held `state_update_lock`.

**Fix applied:** Added `_ws_device_update_ts` dict that records per-device monotonic timestamps on every WS update. `_async_update_data()` captures `poll_start_mono` before the network call, then after building poll results, preserves the WS-pushed state for any device whose `_ws_device_update_ts >= poll_start_mono`.

**Files changed:** `custom_components/atmeex_cloud/__init__.py`

## ~~Device State Normalization in Three Places~~ FIXED

**Severity: Medium (maintainability)** | **Status: Resolved**

WebSocket partial-update functions (`_apply_condition_update`, `_apply_settings_update`) were defined as inline closures in `__init__.py`, separate from `_normalize_device_state()` in `helpers.py`.

**Fix applied:** Moved `apply_condition_update()` and `apply_settings_update()` to `helpers.py` as module-level functions, co-located with `_normalize_device_state()`. `__init__.py` now imports them from `helpers`. Two normalization entry points remain (`AtmeexState.from_device_dict` for full polling, `apply_*_update` for WS partials) but the WS helpers now live alongside the polling normalizer for easier maintenance.

**Files changed:** `custom_components/atmeex_cloud/__init__.py`, `custom_components/atmeex_cloud/helpers.py`

## ~~Unbounded WebSocket Message Queue~~ FIXED

**Severity: Low** | **Status: Resolved**

`websocket_message_queue` was `deque()` with no `maxlen`, allowing unbounded growth if processing stalled.

**Fix applied:** Changed to `deque(maxlen=500)`. Oldest messages are silently dropped if the queue fills.

**Files changed:** `custom_components/atmeex_cloud/__init__.py`

---

## Dismissed Concerns

The following concerns from the initial automated analysis were investigated and found to be non-issues:

- **Unbounded dict growth (pending_commands/device_locks)** — Typical user has 1-5 breezers; negligible memory even over years
- **Refresh tasks cache leak** — `finally` block always executes; cleanup works correctly
- **WebSocket token refresh timing** — `_token_getter()` is called on every reconnect attempt inside `_connect_once()`
- **Entry reload WS cleanup** — HA's entry management fully unloads before reloading
- **Bare except clauses** — Intentional for HA custom components; `# noqa: BLE001` annotations show deliberate choice
- **API error status propagation** — HTTP errors always include status; callers use `getattr(err, "status", None)` defensively
- **WS auth failure single-fire** — Flag is a local variable in `async_setup_entry()`, resets on every entry reload
- **Runtime import of WebSocketManager** — `websocket.py` is bundled with the integration; import always succeeds
- **Entity name collision** — Config flow's `async_set_unique_id` + `_abort_if_unique_id_configured()` prevents duplicate accounts
- **Temperature parsing fragility** — Fixed: `target_temperature` now validates range; remaining `None` returns are correct HA semantics
- **Pending command TTL race** — Inherent design tradeoff of optimistic updates; 8s TTL is reasonable for Atmeex API latency

---

*Concerns audit: 2026-03-28*
*Validated against codebase: 2026-03-28*
*All concerns resolved: 2026-03-28*
