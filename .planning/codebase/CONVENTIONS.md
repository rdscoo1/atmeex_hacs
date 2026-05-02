# Coding Conventions

**Analysis Date:** 2026-05-02

## Naming Patterns

**Files:**
- Module files: lowercase with underscores (e.g., `api.py`, `coordinator.py`, `entity_base.py`, `runtime.py`)
- Test files: `test_*.py` prefix (e.g., `test_api.py`, `test_sensor.py`)
- Configuration files: `const.py` for constants, config flow in `config_flow.py`

**Classes:**
- PascalCase: `AtmeexApi`, `AtmeexDevice`, `AtmeexState`, `AtmeexCoordinator`, `AtmeexRuntimeData`
- Mixins: PascalCase with "Mixin" suffix (e.g., `AtmeexEntityMixin`)
- Entity classes: PascalCase with entity type suffix (e.g., `AtmeexCO2Sensor`, `AtmeexOnlineSensor`)
- Exception classes: PascalCase inheriting from `Exception` (e.g., `ApiError`)
- TypedDict classes: PascalCase with "Data" suffix (e.g., `AtmeexCoordinatorData`)

**Functions:**
- Snake_case for module-level and class methods: `async_setup_entry()`, `async_login()`, `get_devices()`, `_normalize_device_state()`
- Private functions: leading underscore prefix (e.g., `_with_retries()`, `_ensure_token()`, `_put_params()`)
- Async functions: `async_` prefix for coroutines (e.g., `async_setup_entry()`, `async_init()`)

**Variables:**
- Snake_case: `device_map`, `coordinator_data`, `pending_commands`, `device_locks`
- Constants: UPPER_SNAKE_CASE in `const.py` (e.g., `API_BASE_URL`, `RETRY_MAX_ATTEMPTS`, `DEFAULT_UPDATE_INTERVAL`)
- Private module variables: leading underscore (e.g., `_LOGGER`, `_token`, `_retry_count`)
- Timeout constants at module level for monkeypatching: `_UNLOAD_TASK_TIMEOUT_SEC`, `_REFRESH_TASK_TIMEOUT_SEC`

**TypedDict/Dataclass Field Names:**
- Snake_case for all fields: `last_success_ts`, `avg_latency_ms`, `request_retries`
- Optional fields marked with `| None` (e.g., `token_expires_at: float | None`)

## Code Style

**Formatting:**
- PEP 8 standard Python formatting
- 4-space indentation
- Line length: practical limit (not strictly enforced but readable)
- Blank lines: 2 between top-level definitions, 1 between methods

**Future Annotations:**
- All files begin with `from __future__ import annotations` for forward references without quotes

**Type Hints:**
- Comprehensive type hints on all functions and class methods
- Use modern syntax: `dict[str, Any]` not `Dict[str, Any]`
- Optional types: `X | None` preferred over `Optional[X]`
- Union types: `int | str` format
- Callable signatures: `Callable[[int | str], Awaitable[None]]`

**Linting:**
- No explicit linting config; relies on IDE/manual review
- `# noqa: BLE001` comments suppress broad exception warnings where deliberately used

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first)
2. Standard library imports (`asyncio`, `logging`, `time`, `dataclasses`, etc.)
3. Third-party imports (`aiohttp`, `homeassistant.*`)
4. Local imports (`. import`, `from .const import`, etc.)

**Path Aliases:**
- No explicit path aliases; uses relative imports within the package
- Example: `from .api import AtmeexApi`, `from .runtime import AtmeexRuntimeData`

**Module Exports:**
- `__all__` defined in `__init__.py` to declare the public API: classes, functions, exceptions
- Re-exports: `PendingCommand` re-exported from `__init__.py` for test convenience

## Error Handling

**Patterns:**
- Custom exception `ApiError(message, *, status=None)` with optional HTTP status attribute
- Broad exception handling with `except Exception as e` narrowed by `getattr(err, "status", None)` checks
- HomeAssistant-specific exceptions raised based on error conditions:
  - `ConfigEntryAuthFailed` for 401/403 HTTP errors
  - `ConfigEntryNotReady` for network/connectivity issues at setup time
  - `UpdateFailed` for coordinator update failures
  - `HomeAssistantError` for entity command failures
- Network errors retried via `_with_retries()`; HTTP/logic errors never retried
- Example from `__init__.py`:
  ```python
  try:
      await api.login(email, password)
  except ApiError as err:
      status = getattr(err, "status", None)
      if status in (401, 403):
          raise ConfigEntryAuthFailed(f"Invalid Atmeex credentials: {err}") from err
      raise ConfigEntryNotReady(f"Cannot connect to Atmeex Cloud: {err}") from err
  ```

## Set Command Design (no retries)

**Rule:** `_put_params()` in `api.py` calls `_request()` directly without `_with_retries()`.

**Why:** The server may have already applied the change before a network error occurred, so retrying a set command could double-fire. Only read operations (`get_devices`, `get_device`, `login`, `refresh_token`) use `_with_retries()`.

**Pattern:**
```python
async def _put_params(self, device_id, body, action_name, timeout=20):
    """Set-commands are intentionally NOT retried: double-fire risk."""
    status, data = await self._request("PUT", f"/devices/{device_id}/params", json=body, timeout=timeout)
    if status >= 400:
        raise ApiError(f"{action_name} {status}: {str(data)[:200]}", status=status)
```

## Pending State Pattern

**Rule:** All entity set commands MUST use `_execute_command()` with `pending_attr` + `pending_value` to track optimistic state before the API confirms. This prevents UI flicker while the round-trip is in-flight.

**Pattern** (from `switch.py`):
```python
async def async_turn_on(self, **kwargs):
    await self._execute_command(
        self.api.set_auto_mode(self._device_id, True),
        pending_attr="u_auto",
        pending_value=True,
        error_message="Failed to enable AutoNanny",
    )
```

**`_execute_command()` behavior** (in `entity_base.py`):
1. Records pending state via `runtime.set_pending(device_id, attr, value)` before the API call
2. Acquires per-device lock from `runtime.get_device_lock(device_id)` to serialize operations
3. Clears pending on any exception (API error or other)
4. Calls `_refresh()` (targeted device refresh) after success

## Double-Checked Locking for Auth (api.py)

The `_request()` method uses double-checked locking for reactive 401/403 re-sign-in:

```python
if resp.status in (401, 403) and retry_auth and self._email and self._password:
    stale_token = self._token
    async with self._lock:
        # Another coroutine may have already refreshed the token.
        if self._token == stale_token:
            self._token_expires_at = None
            await self._sign_in()
    return await _do(retry_auth=False)
```

The inner check `if self._token == stale_token` prevents a second concurrent coroutine from triggering another login if the first one already refreshed the token.

## Async Timeout Patterns

**For task cancellation during unload** — use `asyncio.wait({task}, timeout=N)` NOT `asyncio.wait_for`:
```python
# Correct — does not raise on timeout, returns (done, pending) sets
_, pending = await asyncio.wait({message_task}, timeout=_UNLOAD_TASK_TIMEOUT_SEC)
if pending:
    _LOGGER.warning("Task did not finish within %.1fs; abandoning", ...)
```

**For in-flight task coalescing** — use `asyncio.wait_for(in_flight, timeout=N)` which raises `asyncio.TimeoutError` to evict stuck tasks:
```python
await asyncio.wait_for(in_flight, timeout=_REFRESH_TASK_TIMEOUT_SEC)
```

**Why the distinction:** `asyncio.wait` is preferred for unload paths because the goal is to abandon the task after a deadline without raising an exception that would abort unload. `asyncio.wait_for` is appropriate when the caller needs to know a deadline was exceeded.

## Entity Base Properties

**`device_info` is a plain `@property`** (not `@cached_property`) in `entity_base.py`:
```python
@property
def device_info(self) -> DeviceInfo:
    dev = self._device_meta
    raw = getattr(dev, "raw", {}) or {}
    sw_version = raw.get("firmware_version") or raw.get("fw_version") or raw.get("version")
    return DeviceInfo(
        identifiers={(DOMAIN, self._device_id_str)},
        name=getattr(dev, "name", None),
        manufacturer="Atmeex",
        model=getattr(dev, "model", None),
        sw_version=sw_version,
    )
```

The `@cached_property` decorator was removed to avoid stale reads when device metadata changes. Do not reintroduce it.

## Entity Category

Diagnostic and connectivity binary sensors carry `EntityCategory.DIAGNOSTIC`:
```python
# binary_sensor.py and sensor.py
from homeassistant.helpers.entity import EntityCategory
_attr_entity_category = EntityCategory.DIAGNOSTIC
```

This hides these entities from the main card in the HA UI. Use `EntityCategory.DIAGNOSTIC` for sensors that report connectivity status or internal diagnostic values (online sensor, connectivity indicators).

## Logging

**Framework:** `logging` module with logger per-module

**Logger Creation:**
- `_LOGGER = logging.getLogger(__name__)` at module top level
- Logger name: module's fully qualified name (e.g., `custom_components.atmeex_cloud.api`)

**Log Levels:**
- `debug()`: detailed diagnostic info, pending command tracking, parameter values
- `info()`: significant lifecycle events (WebSocket connected, device refreshed)
- `warning()`: recoverable issues (network retry, fallback used, stale data)
- `error()`: serious failures (WebSocket error, unexpected exception)
- `exception()`: caught exceptions with full traceback (used in config flow)

**Patterns:**
- Include context variables in log messages: `"device=%s attr=%s value=%s ts=%.3f"`
- Time-related logs include elapsed time: `elapsed_ms`, `age=%.1fs`
- Example from `runtime.py`:
  ```python
  _LOGGER.debug(
      "Pending command set: device=%s attr=%s value=%s ts=%.3f",
      device_id, attribute, value, ts
  )
  ```

## Comments

**When to Comment:**
- Docstrings required for all public functions, classes, and modules
- Inline comments explain "why" not "what" — code should be self-documenting
- Comments in Russian exist in legacy code; new code may use English
- API quirks documented: e.g., "API uses 0-6 range, so we need to convert 1-7 to 0-6"
- Module-level timeout constants accompanied by comments explaining their sizing

**Docstring Style:**
- Google-style docstrings for functions with Args/Returns sections on multi-param functions
- Class docstrings describe purpose and usage
- Single-line docstrings for simple methods

## Function Design

**Size:** Functions are reasonably sized; complex logic broken into helper functions

**Parameters:**
- Type hints always included
- Keyword-only arguments (after `*`) for boolean flags: `async def _with_retries(..., use_fallback: bool = False)`
- Default values for optional parameters
- Device IDs accepted as `int | str` for flexibility

**Return Values:**
- Always typed
- Functions return meaningful values: `tuple[int, Any]` from `_request()`, `AtmeexDevice` from factory methods
- Async functions return coroutines; type hints use `Awaitable[None]` in Callable signatures
- Nullable returns typed as `X | None`

## Module Design

**Exports:**
- `__all__` used in `__init__.py` to declare public API
- Re-exports of runtime types from `__init__.py` for test convenience

**Dataclasses:**
- Heavy use of `@dataclass` for structured data: `AtmeexDevice`, `AtmeexState`, `PendingCommand`, `AtmeexRuntimeData`
- `slots=True` on `@dataclass(slots=True)` for memory efficiency: `AtmeexDevice`, `AtmeexState`
- `field(default_factory=...)` for mutable defaults: `device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)`
- TypedDict used for coordinator data structure: `AtmeexCoordinatorData(TypedDict, total=False)`

**Encapsulation:**
- Private methods with leading underscore: `_ensure_token()`, `_request()`, `_json()`, `_put_params()`
- Protected attributes: `_token`, `_email`, `_password` (marked as internal)
- Public properties for computed values: `@property def token(self)` returns read-only access to `_token`

**State Management:**
- State stored in dataclass instances with type safety
- Coordinator state centralized through `DataUpdateCoordinator.data` and `async_set_updated_data()`
- Runtime state in `AtmeexRuntimeData` tracks API, coordinator, locks, pending commands, WebSocket tasks
- WS timestamp tracking in coordinator: `_ws_device_update_ts` and `_refresh_device_update_ts` (per-device monotonic timestamps)
- Closure-scoped mutable state uses single-element dicts to avoid `nonlocal`: `_ws_task_ref: dict[str, asyncio.Task | None] = {"task": None}`

---

*Convention analysis: 2026-05-02*
