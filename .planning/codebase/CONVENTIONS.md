# Coding Conventions

**Analysis Date:** 2026-03-28

## Naming Patterns

**Files:**
- Module files: lowercase with underscores (e.g., `api.py`, `coordinator.py`, `entity_base.py`)
- Test files: `test_*.py` prefix convention (e.g., `test_api.py`, `test_sensor.py`)
- Configuration files: `const.py` for constants, config flow in `config_flow.py`

**Classes:**
- PascalCase: `AtmeexApi`, `AtmeexDevice`, `AtmeexState`, `AtmeexCoordinator`, `AtmeexRuntimeData`
- Mixins: PascalCase with "Mixin" suffix (e.g., `AtmeexEntityMixin`)
- Entity classes: PascalCase with entity type suffix (e.g., `AtmeexCO2Sensor`, `AtmeexHumiditySensor`)
- Exception classes: PascalCase inheriting from `Exception` (e.g., `ApiError`)

**Functions:**
- Snake_case for module-level and class methods: `async_setup_entry()`, `async_login()`, `get_devices()`, `_normalize_device_state()`
- Private functions: leading underscore prefix (e.g., `_with_retries()`, `_ensure_token()`, `_apply_condition_update()`)
- Async functions: `async_` prefix for coroutines (e.g., `async_setup_entry()`, `async_init()`)

**Variables:**
- Snake_case: `device_map`, `coordinator_data`, `pending_commands`, `device_locks`
- Constants: UPPER_SNAKE_CASE in `const.py` (e.g., `API_BASE_URL`, `RETRY_MAX_ATTEMPTS`, `DEFAULT_UPDATE_INTERVAL`)
- Private module variables: leading underscore (e.g., `_LOGGER`, `_token`, `_retry_count`)
- Temporary/loop variables: concise (e.g., `dev`, `did`, `key`, `ts`)

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
- Use modern syntax: `dict[str, Any]` not `Dict[str, Any]` (though `Dict` is imported in `api.py`)
- Optional types: `X | None` preferred over `Optional[X]`
- Union types: `int | str` format
- Callable signatures: `Callable[[int | str], Awaitable[None]]`

**Linting:**
- No explicit `.eslintrc` or linting config found; appears to rely on IDE/manual review
- Code uses `# noqa: BLE001` comments to suppress broad exception warnings where appropriate
- Exception handling comments: `# noqa: BLE001` for bare `except Exception` blocks

## Import Organization

**Order:**
1. `from __future__ import annotations` (always first)
2. Standard library imports (`asyncio`, `logging`, `time`, `dataclasses`, etc.)
3. Third-party imports (`aiohttp`, `homeassistant.*`)
4. Local imports (`. import`, `from .const import`, etc.)

**Path Aliases:**
- No explicit path aliases; uses relative imports within package
- Example: `from .api import AtmeexApi`, `from . import AtmeexRuntimeData`

**Module Exports:**
- `__all__` lists defined in modules where useful (e.g., in `__init__.py`)
- Barrel files used to re-export key classes: `from .api import ...` then `from . import AtmeexRuntimeData`

## Error Handling

**Patterns:**
- Custom exception class `ApiError` with optional `status` attribute: `ApiError(message, status=401)`
- Broad exception handling with `except Exception as e` wrapped in conditional checks for error type
- HomeAssistant-specific exceptions raised based on error conditions:
  - `ConfigEntryAuthFailed` for 401/403 HTTP errors
  - `ConfigEntryNotReady` for network/connectivity issues
  - `UpdateFailed` for coordinator update failures
- Network errors differentiated from HTTP errors: retries only on `asyncio.TimeoutError` and `aiohttp.ClientError`
- Two-level error handling in coordinator: network errors logged and handled at integration level, auth errors escalated
- Example pattern in `__init__.py`:
  ```python
  try:
      await api.login(email, password)
  except ApiError as err:
      status = getattr(err, "status", None)
      if status in (401, 403):
          raise ConfigEntryAuthFailed(f"Invalid credentials: {err}") from err
      raise ConfigEntryNotReady(f"Cannot connect: {err}") from err
  ```

## Logging

**Framework:** `logging` module with logger per-module

**Logger Creation:**
- `_LOGGER = logging.getLogger(__name__)` at module top level
- Logger name: module's fully qualified name (e.g., `custom_components.atmeex_cloud.api`)

**Log Levels:**
- `debug()`: detailed diagnostic info, function entry/exit, parameter values
- `info()`: significant lifecycle events (WebSocket connected, device refreshed)
- `warning()`: recoverable issues (network retry, fallback used, stale data)
- `error()`: serious failures (WebSocket error, unexpected exception)
- `exception()`: caught exceptions with full traceback (used in config flow)

**Patterns:**
- Include context variables in log messages: `"device=%s attr=%s value=%s"`, device_id, attribute, value
- Time-related logs include elapsed time: `elapsed_ms`, `age=%.1fs`
- Suppression tracking: `suppressed_errors`, `suppressed_updates` included in events
- Example from `__init__.py`:
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
- Comments in Russian in many places (legacy convention) — new code may use English
- Complex algorithm steps documented with step-by-step comments
- API quirks documented: e.g., "API uses 0-6 range, so we need to convert 1-7 to 0-6"

**Docstring Style:**
- Google-style docstrings for functions:
  ```python
  """Short description of what function does.

  Longer explanation if needed.

  Args:
      param1: Description
      param2: Description

  Returns:
      Description of return value
  """
  ```
- Class docstrings describe purpose and usage
- Example from `api.py`:
  ```python
  """Собрать устройство из сырого ответа API, с дефолтами."""
  ```

## Function Design

**Size:** Functions are reasonably sized; complex logic broken into helper functions

**Parameters:**
- Type hints always included
- Keyword-only arguments (after `*`) for optional behavior: `async def _with_retries(..., use_fallback: bool = False)`
- Default values for optional parameters
- Device IDs accepted as `int | str` for flexibility

**Return Values:**
- Always typed
- Functions return meaningful values: `tuple[int, Any]` from `_request()`, `AtmeexDevice` from factory methods
- Async functions return coroutines: `Awaitable[None]` in type hints
- Nullable returns typed as `X | None`

**Error Handling in Functions:**
- Network operations wrapped in retry logic via `_with_retries()`
- Validation of parsed values before use: `isinstance(data, dict)` checks
- Fallback values provided for graceful degradation: `fallback_value=[]`

## Module Design

**Exports:**
- `__all__` used in `__init__.py` to declare public API: classes, functions, exceptions
- Other modules export via direct imports

**Dataclasses:**
- Heavy use of `@dataclass` for structured data: `AtmeexDevice`, `AtmeexState`, `PendingCommand`, `AtmeexCoordinatorData`
- `slots=True` used in dataclasses for memory efficiency: `@dataclass(slots=True)`
- `field(default_factory=...)` for mutable defaults: `device_locks: dict[str, asyncio.Lock] = field(default_factory=dict)`
- TypedDict used for coordinator data structure: `AtmeexCoordinatorData(TypedDict)`

**Encapsulation:**
- Private methods with leading underscore: `_ensure_token()`, `_request()`, `_json()`
- Protected attributes: `_token`, `_email`, `_password` (could be changed from outside but marked as internal)
- Public properties for computed values: `@property def token(self)` returns read-only access to internal `_token`

**State Management:**
- State stored in dataclass instances with type safety
- Coordinator state centralized through `DataUpdateCoordinator.data` and `async_set_updated_data()`
- Runtime state in `AtmeexRuntimeData` tracks API, coordinator, locks, pending commands, WebSocket
- Nonlocal closure variables used for function-scoped state tracking (e.g., `_api_error_last_ts`, `ws_logbook_suppressed_updates`)

---

*Convention analysis: 2026-03-28*
