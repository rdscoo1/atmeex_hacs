# Codebase Concerns

**Analysis Date:** 2026-03-28

## Unbounded Dictionary Growth

**Pending Commands and Device Locks:**
- Issue: `AtmeexRuntimeData.pending_commands` and `device_locks` dictionaries grow unbounded as new device IDs are encountered. There is no cleanup mechanism when devices are removed from the account or the integration is reloaded. Over long-running instances with many device add/remove cycles, these dicts will accumulate stale entries.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 55-70, 72-136)
- Impact: Memory leak over months/years, especially in multi-user or multi-device scenarios. Each dead device ID holds an asyncio.Lock and nested dict of PendingCommand objects indefinitely.
- Fix approach: Implement periodic cleanup keyed to device discovery updates. When `_fetch_devices_safely()` completes, iterate `pending_commands` and `device_locks`, removing entries for IDs no longer in the device list. Alternatively, add TTL-based cleanup in a background task that runs every 24 hours.

**Refresh Tasks Cache:**
- Issue: `refresh_tasks` dict at line 390 has task cleanup (`refresh_tasks.pop(key, None)` at line 541) but only inside the finally block of `refresh_device()`. If a device refresh task is somehow orphaned or the finally block doesn't execute, stale task references accumulate.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 390, 526-541)
- Impact: Memory growth, potential race conditions if old tasks are referenced after completion.
- Fix approach: Add periodic sweep of completed tasks, or use `weakref` to automatically drop references. Ensure finally block always executes with proper exception handling.

## Pending Command TTL and Race Conditions

**Stale Pending Command Handling:**
- Issue: Pending commands use 8-second TTL (PENDING_COMMAND_TTL in climate.py line 39), but API condition updates can be slow or delayed. If a user sends rapid commands (e.g., fan speed 1→7 in quick succession), the second command may arrive before the first is confirmed, and the TTL-based cleanup could incorrectly accept stale device values.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 99-136), `custom_components/atmeex_cloud/climate.py` (line 39)
- Impact: UI shows wrong state briefly, or user sees their change revert unexpectedly if server response arrives after TTL.
- Fix approach: Increase TTL to 15+ seconds during high-latency conditions. Add metrics to detect slow API responses and auto-adjust TTL. Document the tolerance parameter more clearly for entity implementations.

## WebSocket Token Refresh Timing

**Static Token Getter in WebSocket:**
- Issue: WebSocketManager accepts a `token_getter` callable (websocket.py line 65, 74) but the token is only retrieved once during `_connect_once()` at line 116. If the API token is refreshed (e.g., nightly), the WebSocket will use a stale token and silently fail on reconnection until the next manual reconnect.
- Files: `custom_components/atmeex_cloud/websocket.py` (lines 116-123), `custom_components/atmeex_cloud/api.py` (lines 273-277)
- Impact: WebSocket connection lost after token expiry, falling back to polling silently. Users won't know their real-time updates are disabled.
- Fix approach: Call `token_getter()` inside the connect loop, not before. Pass token in WebSocket headers on every reconnect attempt, not just initial connect.

## Race Condition in WebSocket State Updates

**State Update Lock vs Coordinator Updates:**
- Issue: `_apply_websocket_message()` acquires `state_update_lock` (line 625 in __init__.py) to update coordinator state, but `_async_update_data()` (the main coordinator update method) does not acquire the same lock. If WebSocket message arrives while polling update runs, both may try to update `coordinator.data` simultaneously, causing lost updates or inconsistent state.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 282-375, 614-687)
- Impact: Device state inconsistency, dropped WebSocket updates during polling cycles, stale offline flags.
- Fix approach: Either (1) always update coordinator under the state_update_lock in both paths, or (2) use coordinator's built-in mechanisms (if available) to serialize updates. Consider using `async_set_updated_data()` which should be thread-safe.

## Configuration Entry Reload and WebSocket Cleanup

**Task Cancellation During Reload:**
- Issue: `async_unload_entry()` (lines 797-826) cancels WebSocket tasks, but if the user changes options during an active WebSocket session, the entry reload may not cleanly disconnect before the new instance tries to connect. Racing conditions can occur where old WebSocket stays connected while new one tries to establish.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 801-815)
- Impact: Duplicate WebSocket connections (port exhaustion), message delivery to wrong handler, memory leaks.
- Fix approach: Add explicit state flag (e.g., `_shutting_down`) that all tasks check. Ensure all tasks complete cancellation before returning from `async_unload_entry()`. Add timeout to task cancellation awaits.

## Unbounded WebSocket Message Queue

**Queue Without Size Limit:**
- Issue: `websocket_message_queue` (line 392) is a plain `deque()` with no max length. If WebSocket messages arrive faster than `_drain_websocket_messages()` can process them, queue grows indefinitely, especially during high-frequency updates or network lag.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 392, 696-717)
- Impact: Memory exhaustion, Home Assistant slow/unresponsive if 100K+ messages queue up during a network blip.
- Fix approach: Use `deque(maxlen=500)` or similar bounded size. When full, drop oldest messages with warning log. Or implement backpressure: pause WebSocket message callback if queue is full.

## Bare Except Clauses

**Overly Broad Exception Handling:**
- Issue: Code uses `except Exception as err` and `# noqa: BLE001` throughout without re-raising critical exceptions. Examples:
  - `api.py` line 356: catches all exceptions during device parsing, silently continues
  - `__init__.py` line 275, 451: catches all exceptions during device fetch/refresh
  - `websocket.py` line 138: catches all exceptions during WebSocket connection
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 240-241, 275, 357, 451, 700-701, 741-742, 769-770, 779-780), `custom_components/atmeex_cloud/api.py` (line 356), `custom_components/atmeex_cloud/websocket.py` (lines 138-140, 216-217)
- Impact: Silent failures, hard-to-debug issues, unhandled errors masked. Async task errors can be silently lost.
- Fix approach: Replace broad `except Exception` with specific exception types. Use `except (asyncio.TimeoutError, aiohttp.ClientError, ValueError)` to catch expected errors. Let unexpected exceptions propagate and log properly.

## API Error Status Not Always Captured

**Inconsistent Error Status Propagation:**
- Issue: `ApiError` can be raised with or without `status` field (api.py lines 260, 369). Some callers check `getattr(err, "status", None)` defensively, others assume it's set. If a network error is wrapped without status, downstream auth-failure detection fails.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 175, 235, 250, 290-294, 298), `custom_components/atmeex_cloud/api.py` (lines 220, 260, 369)
- Impact: 401/403 auth failures treated as temporary network errors, triggering retries instead of reauth flow. Users locked out of integration instead of prompted to re-authenticate.
- Fix approach: Ensure all `ApiError` constructor calls include `status=None` explicitly if unknown. Add type hints to make status field mandatory. Always pass original HTTP status from response to error constructor.

## Device State Normalization Complexity

**Multiple Normalization Paths:**
- Issue: Device state is normalized in three places with slightly different logic:
  - `_normalize_device_state()` in helpers.py (lines 136-226): merges condition + settings
  - `AtmeexState.from_device_dict()` in api.py (lines 92-107): builds typed state
  - `_apply_condition_update()` and `_apply_settings_update()` in __init__.py (lines 553-612): WebSocket updates
- Each path has different fallbacks and priority logic for `pwr_on`, `fan_speed`, etc.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 553-612), `custom_components/atmeex_cloud/api.py` (lines 91-107), `custom_components/atmeex_cloud/helpers.py` (lines 136-226)
- Impact: Subtle state inconsistencies, e.g., fan_speed shown as 0 in coordinator but 1 in climate entity. Difficult to debug and maintain.
- Fix approach: Consolidate normalization into single function in helpers.py. Both polling and WebSocket paths call the same function. Add comprehensive tests for all combinations.

## WebSocket Auth Failure Only Triggers Once

**Single-Fire Reauth Mechanism:**
- Issue: `ws_reauth_started` flag (line 718) prevents `_on_ws_auth_failure()` from firing reauth more than once. If a token refresh fails and WebSocket attempts reconnect, the reauth is only triggered on the first failure. Subsequent reconnect attempts after token refresh will silently fail.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 718-744)
- Impact: After failed reauth, WebSocket never recovers even if user updates credentials. Requires manual integration reload.
- Fix approach: Instead of one-time flag, track reauth state per token value. If token changes, allow reauth to trigger again. Or implement exponential backoff for reauth attempts.

## Import of WebSocketManager at Runtime

**Deferred Import Dependency:**
- Issue: `WebSocketManager` is imported inside `async_setup_entry()` with try/except (lines 691-780), allowing the feature to be optional. But if import fails silently, users won't know WebSocket is disabled. No warning in UI or diagnostics.
- Files: `custom_components/atmeex_cloud/__init__.py` (lines 691, 777-780)
- Impact: User configures WebSocket, thinks it's enabled, but due to missing optional dependency it silently falls back to polling. No visibility.
- Fix approach: Import at module level, fail fast if WebSocket is required. Or improve logging to surface import failures in diagnostics panel and warn user.

## Entity Name Collision Risk

**Entity Unique ID Generation:**
- Issue: Entity unique IDs are simple concatenation of device ID and platform name (e.g., `f"{device.id}_climate"` in climate.py line 133). If the same Atmeex account is added to Home Assistant twice (different config entries), both will generate identical unique IDs, causing entity duplication/conflicts.
- Files: `custom_components/atmeex_cloud/climate.py` (line 133), `custom_components/atmeex_cloud/switch.py` (line 69), and similar in other platforms
- Impact: Duplicate entities in UI, automation confusion, state conflicts between two instances of same device.
- Fix approach: Include config entry ID in unique ID generation: `f"{entry.entry_id}_{device.id}_climate"`. This ensures uniqueness across multiple integrations.

## Temperature/Humidity Value Parsing Fragility

**No Validation of Numeric Conversions:**
- Issue: Helpers like `c_to_deci()` (line 100) silently return `None` on conversion failure. Climate entity then uses this `None` as a field value, which may cause rendering issues if Home Assistant assumes numeric type.
- Files: `custom_components/atmeex_cloud/helpers.py` (lines 95-102, 108-123), `custom_components/atmeex_cloud/climate.py` (lines 252-276, 300-317)
- Impact: Missing temperature/humidity values in UI without clear error message. User assumes device is offline when actually parsing failed.
- Fix approach: Log warnings when conversion fails, not silent None. Add validation in climate entity properties to handle None gracefully. Consider using fallback defaults (e.g., 20°C for temperature).

---

*Concerns audit: 2026-03-28*
