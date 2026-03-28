# Architecture

**Analysis Date:** 2026-03-28

## Pattern Overview

**Overall:** Home Assistant Custom Integration with Data Update Coordinator Pattern + WebSocket Real-time Sync

**Key Characteristics:**
- Layered architecture: API Client → Coordinator → Platform Entities
- Dual update strategy: HTTP polling (primary) + WebSocket (optional real-time)
- Race-condition protection via pending command tracking and state locks
- Async/await throughout with proper cancellation and error handling
- TypedDict for coordinator data with typed attributes for diagnostics

## Layers

**API Layer:**
- Purpose: Abstract HTTP communication with Atmeex Cloud API
- Location: `custom_components/atmeex_cloud/api.py`
- Contains: `AtmeexApi` client, `AtmeexDevice` and `AtmeexState` data classes, `ApiError` exception
- Depends on: `aiohttp.ClientSession`, helpers for device normalization
- Used by: Coordinator and config flow

**Coordinator Layer:**
- Purpose: Centralized data management and refresh orchestration
- Location: `custom_components/atmeex_cloud/coordinator.py`, `custom_components/atmeex_cloud/__init__.py`
- Contains: `AtmeexCoordinator` (typed subclass), `AtmeexCoordinatorData` TypedDict, `AtmeexRuntimeData` for setup data
- Depends on: Home Assistant's `DataUpdateCoordinator`, API layer
- Used by: Platform entities, WebSocket message handler

**Entity Layer:**
- Purpose: Home Assistant entity representations (sensor, switch, fan, climate, binary_sensor, select)
- Location: `custom_components/atmeex_cloud/{sensor,switch,fan,climate,binary_sensor,select}.py`
- Contains: Entity implementations inheriting from `CoordinatorEntity` + `AtmeexEntityMixin`
- Depends on: `AtmeexEntityMixin` (entity_base.py), coordinator, Home Assistant entity classes
- Used by: Home Assistant core platform discovery

**WebSocket Real-time Layer:**
- Purpose: Optional real-time device state updates via WebSocket
- Location: `custom_components/atmeex_cloud/websocket.py`
- Contains: `WebSocketManager` with reconnect logic, message queue processing
- Depends on: `aiohttp.ClientSession`, coordinator state lock
- Used by: Runtime setup for continuous background updates

**Support Modules:**
- `entity_base.py`: `AtmeexEntityMixin` for shared entity logic
- `helpers.py`: Unit conversion functions (fan speed, temperature, humidity, state normalization)
- `config_flow.py`: Initial setup and options configuration
- `const.py`: Constants and configuration keys
- `diagnostics.py`: Diagnostic data snapshots
- `logbook.py`: Custom event logging

## Data Flow

**HTTP Polling (Primary Path):**

1. `AtmeexCoordinator.async_request_refresh()` triggered by interval or manual refresh
2. Coordinator calls `_async_update_data()` closure
3. Fetches device list via `api.get_devices()` with fallback support
4. Hydrates each device via `api.get_device(device_id)` individually
5. Normalizes raw device data into `AtmeexState` objects
6. Stores in `coordinator.data`: `{"device_map": {...}, "states": {...}, "devices": [...]}`
7. Notifies all listening `CoordinatorEntity` instances via `async_set_updated_data()`
8. Entities update their Home Assistant state

**WebSocket Real-time Path (Optional):**

1. WebSocket connects via `websocket_manager.connect()` after coordinator first refresh
2. Receives `type: "condition"` or `type: "settings"` messages continuously
3. Queues message to `websocket_message_queue`
4. Background task `_drain_websocket_messages()` processes queue serially
5. Applies partial state updates via `_apply_condition_update()` or `_apply_settings_update()`
6. Updates coordinator state within `state_update_lock` to avoid races
7. Fires logbook event `EVENT_DEVICE_UPDATED`

**Command Execution (Set Operations):**

1. User/automation calls entity's async_turn_on/async_set_percentage/async_set_temperature
2. Entity acquires device-specific lock via `device_locks[device_id]`
3. Executes set command: `api.set_pwr_on()`, `api.set_fan_speed()`, etc.
4. Records pending command with timestamp via `runtime_data.set_pending()`
5. Triggers device refresh via `runtime_data.refresh_device()`
6. Refresh fetches updated device state from API
7. Clears pending if confirmed or expired via `clear_pending_if_confirmed()`
8. Updates coordinator state

**State Management:**

- `coordinator.data` holds typed `AtmeexCoordinatorData` with device list, device map, and normalized states
- `states` dict maps device_id → normalized state dict containing `pwr_on`, `fan_speed`, `temp_room`, etc.
- `device_map` holds `AtmeexDevice` objects for metadata access
- `pending_commands` dict tracks in-flight commands with TTL for race protection
- `state_update_lock` serializes all coordinator state writes

## Key Abstractions

**AtmeexDevice:**
- Purpose: Typed representation of a physical device with static metadata
- Examples: `api.py` line 25-54
- Pattern: Dataclass with `from_raw()` classmethod for construction, properties for condition/settings extraction, `to_ha_dict()` for coordinator storage

**AtmeexState:**
- Purpose: Normalized device state combining condition + settings fields
- Examples: `api.py` line 78-111
- Pattern: Dataclass built from raw device dict via `from_device_dict()`, provides `to_ha_dict()` for coordinator

**AtmeexRuntimeData:**
- Purpose: Single mutable container for all integration setup data
- Examples: `__init__.py` line 49-136
- Pattern: Dataclass holding api, coordinator, locks, pending commands, websocket manager; methods for managing per-device state

**AtmeexEntityMixin:**
- Purpose: Shared logic for all entity types (property access, refresh, pending state resolution)
- Examples: `entity_base.py` line 12-97
- Pattern: Mixin providing `_device`, `_device_state`, `_state_with_pending()`, `available` property, `device_info` cached property

**WebSocketManager:**
- Purpose: Autonomous WebSocket connection lifecycle management
- Examples: `websocket.py` line 46-200+
- Pattern: Manages connection, authentication, reconnection with exponential backoff, graceful shutdown

## Entry Points

**`async_setup_entry()` in __init__.py:**
- Location: `custom_components/atmeex_cloud/__init__.py` line 160-794
- Triggers: Home Assistant calls when config entry is ready to setup
- Responsibilities:
  - Authenticates with Atmeex API
  - Creates `AtmeexCoordinator` with update interval
  - Performs initial device refresh
  - Sets up HTTP polling via coordinator
  - Optionally initializes WebSocket for real-time updates
  - Creates `AtmeexRuntimeData` with all integration state
  - Forwards setup to platform modules (sensor, switch, fan, etc.)

**`async_unload_entry()` in __init__.py:**
- Location: `custom_components/atmeex_cloud/__init__.py` line 797-826
- Triggers: Home Assistant calls when config entry is disabled/removed
- Responsibilities:
  - Cancels WebSocket message processing task
  - Cancels WebSocket startup task
  - Disconnects WebSocket manager
  - Unloads all platform entities

**Platform `async_setup_entry()` functions:**
- Location: `sensor.py` line 29-75, `switch.py` line 20-40, `fan.py` line 21-70, etc.
- Triggers: Called by Home Assistant after integration setup
- Responsibilities:
  - Access runtime_data from config entry
  - Get coordinator and device_map from coordinator.data
  - Create entity instances for each device
  - Register entities with `async_add_entities()`

**`async_step_user()` in config_flow.py:**
- Location: `custom_components/atmeex_cloud/config_flow.py` line 61-100+
- Triggers: User initiates setup via Home Assistant UI
- Responsibilities:
  - Prompts for email and password
  - Validates credentials by calling `api.login()`
  - Creates ConfigEntry with validated email
  - Triggers `async_setup_entry()`

## Error Handling

**Strategy:** Layered error handling with distinction between auth errors, transient errors, and unexpected errors

**Patterns:**

**API Layer (api.py):**
- Auth errors (401, 403) → raise `ApiError` with status
- Network errors (aiohttp.ClientError) → raise `ApiError` with message, caller decides retry
- Unexpected errors → raise `ApiError` with traceback

**Coordinator Layer (__init__.py line 282-375):**
- Auth errors (401, 403 from API) → raise `ConfigEntryAuthFailed` (triggers re-auth flow)
- Transient errors → raise `UpdateFailed` (coordinator retries on interval)
- Catches all exceptions and fires logbook event `EVENT_API_ERROR` with throttling

**Setup Flow (__init__.py line 160-185):**
- Auth errors during login → raise `ConfigEntryAuthFailed`
- Connection errors → raise `ConfigEntryNotReady` (triggers retry)

**Refresh Device (__init__.py line 436-461):**
- API errors → logs warning, fires logbook event, does not propagate
- Continues despite single device failure

**WebSocket (__init__.py line 760-780):**
- Connection errors → logs warning, falls back to polling
- Auth failures → triggers config entry reauth flow

## Cross-Cutting Concerns

**Logging:**
- Module-level logger `_LOGGER = logging.getLogger(__name__)` in every module
- Info: successful setup, WebSocket connect, migration
- Warning: failures, fallback operations, missing capabilities
- Debug: API requests, pending commands, state updates, WebSocket messages

**Validation:**
- Email normalization in config_flow via `_clean_email()` and `_email_unique_id()`
- Update interval clamping in `_resolve_update_interval_seconds()` (10-300 seconds)
- Device state normalization via `_normalize_device_state()` in helpers.py
- Fan speed/temperature/humidity validation in conversion helpers

**Authentication:**
- Token obtained during `api.login()`, stored in `api._token`
- Token passed to WebSocket via `token_getter` callable
- Automatic re-authentication on 401/403 via `ConfigEntryAuthFailed`
- WebSocket auth failure callbacks invoke entry reauth flow

**Concurrency:**
- Per-device locks via `device_locks[device_id]` for set+refresh serialization
- State update lock `state_update_lock` for coordinator writes from polling + WebSocket
- Refresh task deduplication via `refresh_tasks[device_id]` - reuses in-flight tasks
- WebSocket message queue processed by single background task for serial updates
- All async operations via `asyncio.create_task()` or coordinator methods

**Diagnostics:**
- `AtmeexCoordinator` has typed attributes: `last_api_error`, `last_success_ts`
- `AtmeexCoordinatorData` tracks: `last_success_ts`, `avg_latency_ms`, `request_retries`
- `diagnostics.py` provides snapshots for Home Assistant diagnostics integration

---

*Architecture analysis: 2026-03-28*
