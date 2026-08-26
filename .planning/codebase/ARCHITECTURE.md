<!-- refreshed: 2026-05-02 -->
# Architecture

> ⚠️ **Stale (2026-07-17):** this document predates the state-store / command-executor rework (v0.10.0). Concurrency is now handled by per-field revisions in `state_store.py`; commands run through `command_executor.py`. See `CLAUDE.md` and `CONCERNS.md` (addendum) for current facts; where this file disagrees with the code, the code wins.

**Analysis Date:** 2026-05-02

## System Overview

```text
┌─────────────────────────────────────────────────────────────────┐
│                    Home Assistant Core                           │
│       Config Entries  ·  Entity Registry  ·  Event Bus          │
└────────────┬────────────────────────────────────────────────────┘
             │  async_setup_entry / async_unload_entry
             ▼
┌─────────────────────────────────────────────────────────────────┐
│            __init__.py  (Integration Setup)                      │
│  - AtmeexRuntimeData (runtime.py)                               │
│  - refresh_device() closure (coalesced, 65 s timeout)           │
│  - WebSocket plumbing (message queue + drain task)              │
│  - _apply_websocket_message() under state_update_lock           │
└──────┬──────────────────────────┬──────────────────────────────┘
       │                          │
       ▼                          ▼
┌─────────────────┐    ┌──────────────────────────────────────────┐
│  coordinator.py │    │          websocket.py                    │
│  AtmeexCoord-   │    │  WebSocketManager                        │
│  inator         │    │  - connect / disconnect                  │
│  _async_update_ │    │  - exponential backoff reconnect         │
│  data (polling) │    │  - _connect_once() resets both           │
│  _ws_device_    │    │    _reconnect_delay and                  │
│  update_ts      │    │    _consecutive_auth_failures on success │
│  _refresh_      │    └──────────────────────────────────────────┘
│  device_        │
│  update_ts      │
└──────┬──────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│                    api.py  (HTTP Client)                         │
│  AtmeexApi  ·  AtmeexDevice  ·  AtmeexState  ·  ApiError        │
│  _request() — 401/403 reactive re-sign-in serialized via _lock  │
│  _put_params() — no retry (set commands fire once)              │
└──────┬──────────────────────────────────────────────────────────┘
       │
       ▼
┌─────────────────────────────────────────────────────────────────┐
│       Platform Entities  (CoordinatorEntity + AtmeexEntityMixin) │
│  sensor  ·  switch  ·  fan  ·  climate  ·  binary_sensor  ·  select │
└─────────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| `AtmeexApi` | HTTP communication with Atmeex Cloud REST API | `custom_components/atmeex_cloud/api.py` |
| `AtmeexCoordinator` | Typed `DataUpdateCoordinator`, polling, freshness tracking | `custom_components/atmeex_cloud/coordinator.py` |
| `AtmeexRuntimeData` | Mutable container for all integration state (api, coordinator, locks, pending) | `custom_components/atmeex_cloud/runtime.py` |
| `PendingCommand` | Tracks in-flight commands to prevent UI regression | `custom_components/atmeex_cloud/runtime.py` |
| `WebSocketManager` | Real-time connection lifecycle, reconnect, auth failure handling | `custom_components/atmeex_cloud/websocket.py` |
| `AtmeexEntityMixin` | Shared entity logic: `_device`, `_device_state`, `_execute_command`, `_state_with_pending` | `custom_components/atmeex_cloud/entity_base.py` |
| Integration setup | `async_setup_entry`, `async_unload_entry`, `refresh_device` closure | `custom_components/atmeex_cloud/__init__.py` |
| Helpers | Unit conversion, state normalization, WebSocket state merging | `custom_components/atmeex_cloud/helpers.py` |

## Pattern Overview

**Overall:** Home Assistant Custom Integration with Data Update Coordinator Pattern + WebSocket Real-time Sync

**Key Characteristics:**
- Layered architecture: API Client → Coordinator → Platform Entities
- Dual update strategy: HTTP polling (primary) + WebSocket (optional real-time)
- Two-tier freshness protection: `_ws_device_update_ts` and `_refresh_device_update_ts` on coordinator guard against stale poll overwrites
- Race-condition protection via per-device locks, pending command tracking, and `state_update_lock`
- Set commands use `_put_params()` which calls `_request()` directly — no retry, intentional to avoid double-fire
- Async/await throughout; `asyncio.wait_for` used for bounded task awaiting

## Layers

**API Layer:**
- Purpose: Abstract HTTP communication with Atmeex Cloud
- Location: `custom_components/atmeex_cloud/api.py`
- Contains: `AtmeexApi` client, `AtmeexDevice` and `AtmeexState` dataclasses, `ApiError`
- Depends on: `aiohttp.ClientSession`, helpers for device normalization
- Used by: Coordinator (`coordinator.py`) and config flow

**Coordinator Layer:**
- Purpose: Centralized data management and update orchestration
- Location: `custom_components/atmeex_cloud/coordinator.py`
- Contains: `AtmeexCoordinator` (typed subclass), `AtmeexCoordinatorData` TypedDict
- Key attributes on coordinator:
  - `_ws_device_update_ts: dict[str, float]` — per-device monotonic timestamp of last WebSocket update
  - `_refresh_device_update_ts: dict[str, float]` — per-device monotonic timestamp of last targeted `refresh_device()` update
  - Both are checked in `_async_update_data()` after each poll completes; if either `>= poll_start_mono` the pre-poll state is preserved for that device
- Depends on: HA `DataUpdateCoordinator`, API layer
- Used by: Platform entities, WebSocket message handler

**Integration Setup / Runtime Layer:**
- Purpose: Setup lifecycle, per-device refresh coalescing, WebSocket orchestration
- Location: `custom_components/atmeex_cloud/__init__.py`, `custom_components/atmeex_cloud/runtime.py`
- Contains: `AtmeexRuntimeData`, `PendingCommand`, `refresh_device()` closure, `_apply_websocket_message()`
- Key behaviours:
  - `refresh_device()` coalesces concurrent calls: if a task is already in-flight for a device, the caller `await asyncio.wait_for(in_flight, timeout=65s)` on it instead of starting a new one
  - `async_unload_entry` uses `asyncio.wait({task}, timeout=5.0)` (never bare `await`) to avoid blocking on stuck tasks
  - `ws_reauth_last_ts: float` (initialized to `float("-inf")`) replaces the old boolean flag; WS reauth is throttled to at most once per 300 seconds
  - Token persistence wrapped in `try/except` to tolerate HA config-entry update failures

**Entity Layer:**
- Purpose: Home Assistant entity representations
- Location: `custom_components/atmeex_cloud/{sensor,switch,fan,climate,binary_sensor,select}.py`
- Contains: Entity implementations inheriting from `CoordinatorEntity` + `AtmeexEntityMixin`
- Depends on: `entity_base.py`, coordinator, HA entity base classes
- Used by: HA platform discovery

**WebSocket Layer:**
- Purpose: Optional real-time device state updates
- Location: `custom_components/atmeex_cloud/websocket.py`
- Contains: `WebSocketManager` with reconnect logic and auth failure handling
- Key behaviour: `_connect_once()` unconditionally resets both `_reconnect_delay` to `reconnect_delay_min` and `_consecutive_auth_failures` to 0 on every successful connection, ensuring clean backoff state after any recovery
- Depends on: `aiohttp.ClientSession`

**Support Modules:**
- `entity_base.py`: `AtmeexEntityMixin`, `setup_dynamic_device_entities`, `supports_humidifier`
- `helpers.py`: Unit conversion (`fan_speed_to_percent`, `deci_to_c`, `humidity_to_stage`), state normalization, WS incremental merge (`apply_condition_update`, `apply_settings_update`)
- `config_flow.py`: Initial setup and options config
- `const.py`: Constants, API URLs, configuration keys
- `diagnostics.py`: Diagnostic data snapshots
- `logbook.py`: Custom event logging

## Data Flow

### HTTP Polling (Primary Path)

1. `AtmeexCoordinator._async_update_data()` triggers on interval (`coordinator.py` line 149)
2. Records `poll_start_mono = time.monotonic()` before the network round-trip
3. `_fetch_devices_safely()` calls `api.get_devices()` (primary, then fallback) then `api.get_device(id)` per device
4. Normalizes raw data into `AtmeexState` objects; builds `device_map` and `states` dicts
5. **Freshness guard**: for each device, if `_ws_device_update_ts[did] >= poll_start_mono` OR `_refresh_device_update_ts[did] >= poll_start_mono`, retains the pre-poll `states[did]` instead of overwriting with the (potentially older) polled value
6. Stores result in `coordinator.data` (`AtmeexCoordinatorData`)
7. Notifies all `CoordinatorEntity` listeners

### WebSocket Real-time Path (Optional)

1. `WebSocketManager.connect()` called after first coordinator refresh
2. Receives `type: "condition"` or `type: "settings"` messages
3. `on_websocket_message()` callback enqueues message in `websocket_message_queue` (bounded `deque(maxlen=500)`)
4. Background `_drain_websocket_messages()` task processes queue serially
5. `_apply_websocket_message()` applies partial state via `apply_condition_update()` or `apply_settings_update()` under `state_update_lock`
6. Records `coordinator._ws_device_update_ts[did] = time.monotonic()` for changed devices
7. Fires throttled `EVENT_DEVICE_UPDATED` logbook event

### Command Execution (Set Operations)

1. User or automation calls entity method (`async_turn_on`, `async_set_percentage`, etc.)
2. Entity calls `_execute_command(api_coro, pending_attr=..., pending_value=...)` on `AtmeexEntityMixin`
3. `_execute_command` records pending via `runtime.set_pending()`, acquires per-device lock via `runtime.get_device_lock()`
4. Calls API (e.g. `api.set_fan_speed()`); on `ApiError` or exception, clears pending and re-raises
5. On success: calls `_refresh()` → `runtime.refresh_device(device_id)` (coalesced)
6. `refresh_device()` records `coordinator._refresh_device_update_ts[key] = time.monotonic()` after success
7. `_state_with_pending()` uses `runtime.clear_pending_if_confirmed()` for optimistic display until confirmation

### Targeted Device Refresh Path

1. `refresh_device(device_id)` closure in `__init__.py` (line 270)
2. Checks `refresh_tasks[key]`; if an in-flight task exists, awaits it with `asyncio.wait_for(..., timeout=65s)` instead of starting a duplicate
3. Otherwise creates new `asyncio.create_task(_refresh_device_once(device_id))`
4. `_refresh_device_once` calls `api.get_device(device_id)`, merges result under `state_update_lock`
5. Calls `coordinator.async_set_updated_data()` and records `coordinator._refresh_device_update_ts[key]`

**State Management:**
- `coordinator.data` holds typed `AtmeexCoordinatorData`: `devices` (list of raw dicts), `device_map` (id → `AtmeexDevice`), `states` (id → normalized dict), `last_success_ts`, `avg_latency_ms`, `request_retries`
- `states` dict values contain keys like `pwr_on`, `fan_speed`, `temp_room`, `u_temp_room`, `hum_stg`, `damp_pos`, `online`, `u_auto`, `u_night`
- `runtime.pending_commands` maps `device_id → {attribute → PendingCommand}` for in-flight command TTL tracking
- `state_update_lock` (`asyncio.Lock` in `__init__.py`) serializes all coordinator writes from polling, WebSocket, and targeted refresh

## Key Abstractions

**AtmeexDevice:**
- Purpose: Typed representation of a physical device with static metadata
- Location: `custom_components/atmeex_cloud/api.py` (line 24)
- Pattern: `@dataclass(slots=True)`, `from_raw()` classmethod, `condition`/`settings` properties, `to_ha_dict()` for coordinator storage

**AtmeexState:**
- Purpose: Normalized device state combining condition + settings fields
- Location: `custom_components/atmeex_cloud/api.py` (line 77)
- Pattern: `@dataclass(slots=True)`, built via `AtmeexState.from_device_dict()` → `_normalize_device_state()`, provides `to_ha_dict()`

**AtmeexRuntimeData:**
- Purpose: Single mutable container for all integration setup data
- Location: `custom_components/atmeex_cloud/runtime.py`
- Pattern: `@dataclass` holding `api`, `coordinator`, `refresh_device` callable, `device_locks`, `pending_commands`, `websocket_manager`, task refs; methods for managing per-device state

**PendingCommand:**
- Purpose: Tracks a single in-flight command by attribute with timestamp for TTL
- Location: `custom_components/atmeex_cloud/runtime.py`
- Pattern: `@dataclass` with `value`, `timestamp`, `attribute` fields; consumed by `clear_pending_if_confirmed()`

**AtmeexEntityMixin:**
- Purpose: Shared logic for all entity types
- Location: `custom_components/atmeex_cloud/entity_base.py`
- Key members: `_device` (live from `device_map`), `_device_state` (normalized state dict), `_state_with_pending()` (optimistic display), `_execute_command()` (lock + pending + refresh), `available` (checks `online` in state), `device_info` (plain `@property`, not `@cached_property`)

**WebSocketManager:**
- Purpose: Autonomous WebSocket lifecycle management
- Location: `custom_components/atmeex_cloud/websocket.py`
- Pattern: `_connect_once()` resets `_reconnect_delay` and `_consecutive_auth_failures` to defaults on every successful connect; `_ensure_reconnect_task()` starts background reconnect loop; `_on_auth_failure` callback stops `_running` flag

## Entry Points

**`async_setup_entry()` in `__init__.py`:**
- Location: `custom_components/atmeex_cloud/__init__.py` line 68
- Triggers: HA calls when config entry is ready
- Responsibilities:
  - Authenticates with Atmeex API; persists refresh token with `try/except` guard
  - Creates `AtmeexCoordinator`, calls `setup_update()` to inject api + logbook callable
  - Performs initial refresh via `async_config_entry_first_refresh()`
  - Creates `refresh_device` closure with coalescing and 65 s timeout
  - Optionally initializes `WebSocketManager` with time-throttled reauth callback
  - `ws_reauth_last_ts: float = float("-inf")` replaces old boolean flag; 300 s cooldown
  - Builds `AtmeexRuntimeData` and assigns to `entry.runtime_data`
  - Forwards setup to all platform modules

**`async_unload_entry()` in `__init__.py`:**
- Location: `custom_components/atmeex_cloud/__init__.py` line 498
- Triggers: HA calls when config entry is disabled/removed
- Responsibilities:
  - Cancels WebSocket message task and start task each with `asyncio.wait({task}, timeout=5.0)`
  - Disconnects `WebSocketManager`
  - Unloads platform entities via `async_unload_platforms()`

**Platform `async_setup_entry()` functions:**
- Location: `sensor.py`, `switch.py`, `fan.py`, `climate.py`, `binary_sensor.py`, `select.py`
- Triggers: HA after integration setup
- Responsibilities:
  - Access `entry.runtime_data` as `AtmeexRuntimeData`
  - Use `setup_dynamic_device_entities()` from `entity_base.py` for initial + ongoing entity discovery

**`async_step_user()` in `config_flow.py`:**
- Location: `custom_components/atmeex_cloud/config_flow.py`
- Triggers: User initiates setup via HA UI
- Responsibilities: Prompts email/password, validates via `api.login()`, creates ConfigEntry

## Architectural Constraints

- **Threading:** Single-threaded asyncio event loop throughout; no threads, no thread locks
- **Global state:** None at module level; all mutable state lives in `AtmeexRuntimeData` (scoped to a config entry)
- **State update serialization:** All writes to `coordinator.data` from polling, WebSocket handler, and targeted refresh go through `state_update_lock` (defined in `__init__.py`, referenced via closures)
- **Set command retry policy:** `api._put_params()` calls `_request()` directly without `_with_retries`; set commands deliberately fire exactly once to avoid double-application on network errors
- **Freshness invariant:** `_async_update_data()` never overwrites a device state that was updated more recently than the poll started; this is enforced by comparing `_ws_device_update_ts` and `_refresh_device_update_ts` against `poll_start_mono`
- **Circular imports:** `__init__.py` imports from `runtime.py`; `entity_base.py` and all platform modules import `AtmeexRuntimeData` from `__init__.py`

## Anti-Patterns

### Bypassing `_execute_command` in entity commands

**What happens:** Calling `api.set_*()` directly without going through `_execute_command()`.
**Why it's wrong:** Skips per-device lock, pending tracking, and the refresh callback — breaks optimistic display and allows interleaved commands.
**Do this instead:** Always call `await self._execute_command(self.api.set_*(…), pending_attr=…, pending_value=…)` as shown in `switch.py` lines 79–94.

### Adding retry logic to set commands in `_put_params`

**What happens:** Wrapping `_put_params` with `_with_retries`.
**Why it's wrong:** Set commands may have already applied on the device before the network error; retrying double-fires the command.
**Do this instead:** Keep `_put_params` calling `_request()` directly, as established in `api.py` line 427.

### Using bare `await task` during unload

**What happens:** Awaiting a background task without a timeout in `async_unload_entry`.
**Why it's wrong:** A stuck task in a `finally` block can block HA's unload indefinitely.
**Do this instead:** Use `asyncio.wait({task}, timeout=_UNLOAD_TASK_TIMEOUT_SEC)` and log a warning if tasks remain, as in `__init__.py` lines 505–508.

## Error Handling

**Strategy:** Layered with clear distinction between auth errors, transient errors, and unexpected errors

**API Layer (`api.py`):**
- Auth errors (401, 403) → raise `ApiError` with `status=`
- Network errors (`aiohttp.ClientError`, `asyncio.TimeoutError`) → retried up to `RETRY_MAX_ATTEMPTS=3` by `_with_retries()`; GET/list operations only — set commands are not retried
- 401/403 on `_request()` → reactive re-sign-in serialized under `self._lock` (double-checked locking to avoid duplicate logins)

**Coordinator (`coordinator.py`):**
- 401/403 from API → raise `ConfigEntryAuthFailed` (triggers HA re-auth flow)
- Other `ApiError` → raise `UpdateFailed` (coordinator retries on next interval)
- Fires throttled `EVENT_API_ERROR` logbook event on all failures

**Setup flow (`__init__.py`):**
- 401/403 during login → `ConfigEntryAuthFailed`
- Other `ApiError` → `ConfigEntryNotReady`
- Token persistence failure → `_LOGGER.warning`, continues

**WebSocket auth failure:**
- HTTP 401/403 handshake → `_running = False`, `_on_auth_failure()` callback invoked once per 300 s (`ws_reauth_last_ts` guard)
- Application-level `{"type": "unauthorized"}` after `WS_MAX_UNAUTHORIZED_BEFORE_REAUTH=5` consecutive failures → same callback

## Cross-Cutting Concerns

**Logging:**
- Module-level `_LOGGER = logging.getLogger(__name__)` in every module
- Info: setup completion, WebSocket connect/disconnect
- Warning: API failures, fallback operations, WS auth rejections
- Debug: API requests, pending command lifecycle, state update decisions (freshness guard logs)

**Validation:**
- Update interval clamped to 10–300 s via `_resolve_update_interval_seconds()`
- Device state normalized via `_normalize_device_state()` in `helpers.py`
- Fan speed/temperature/humidity validated in conversion helpers

**Authentication:**
- Token obtained during `api.login()`, stored in `api._token`
- Refresh token persisted to config entry `data["refresh_token"]` with try/except
- `_ensure_token()` tries refresh token first, falls back to full login under `_lock`
- WebSocket uses `lambda: api.token` getter so reconnects pick up refreshed tokens

**Concurrency:**
- Per-device `asyncio.Lock` via `runtime.get_device_lock(device_id)` for set+refresh serialization
- `state_update_lock` for all coordinator writes
- `refresh_tasks[device_id]` for task coalescing (reuses in-flight task, 65 s timeout)
- WebSocket message `deque(maxlen=500)` + single `_drain_websocket_messages()` drain task

**Diagnostics:**
- `AtmeexCoordinator.last_api_error`, `.last_success_ts` typed instance attributes
- `AtmeexCoordinatorData` tracks `last_success_ts`, `avg_latency_ms`, `request_retries`
- `diagnostics.py` provides redacted snapshots for HA diagnostics integration

---

*Architecture analysis: 2026-05-02*
