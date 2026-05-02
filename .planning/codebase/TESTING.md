# Testing Patterns

**Analysis Date:** 2026-05-02

## Test Framework

**Runner:**
- `pytest` with `pytest-asyncio` for async test support
- Config: `pytest.ini` at project root

**Test Configuration:**
```ini
[pytest]
testpaths = tests
asyncio_mode = auto
addopts = -ra
pythonpath = .
```

**Assertion Library:**
- Standard `pytest` assertions with `assert` statements
- No additional assertion library; uses built-in pytest mechanisms

**Run Commands:**
```bash
pytest                                              # Run all tests (205 total)
pytest -v                                           # Verbose output
pytest tests/test_coordinator.py                   # Run specific test file
pytest -k test_login_success                       # Run tests matching pattern
pytest --cov=custom_components/atmeex_cloud --cov-report=html  # With coverage
```

## Test File Organization

**Location:**
- Co-located in `tests/` directory at project root
- Test files parallel source structure but separate
- Example: `custom_components/atmeex_cloud/api.py` tested by `tests/test_api.py`

**Naming:**
- Test files: `test_*.py` prefix
- Test functions: `test_*` prefix (e.g., `test_login_success`, `test_poll_does_not_overwrite_fresher_targeted_refresh_state`)
- Parametrized tests: `@pytest.mark.parametrize` used for multiple cases

**Structure:**
```
tests/
├── conftest.py                  # Shared fixtures and factory functions
├── test_api.py                  # API client tests (16 tests)
├── test_api_fallback_extra.py   # API fallback behavior (2 tests)
├── test_binary_sensor.py        # Binary sensor entity tests (8 tests)
├── test_climate.py              # Climate entity tests (45 tests)
├── test_config_flow.py          # Config flow UI tests (13 tests)
├── test_coordinator.py          # AtmeexCoordinator unit tests (6 tests)
├── test_diagnostics.py          # Diagnostics endpoint tests (3 tests)
├── test_fan.py                  # Fan entity tests (5 tests)
├── test_init.py                 # Integration setup/unload tests (42 tests)
├── test_logbook.py              # Logbook integration tests (2 tests)
├── test_race_protection.py      # Race condition handling (14 tests)
├── test_refresh_device.py       # Device refresh logic (1 test)
├── test_runtime.py              # AtmeexRuntimeData tests (6 tests)
├── test_select.py               # Select entity tests (10 tests)
├── test_sensor.py               # Sensor entity tests (5 tests)
├── test_switch.py               # Switch entity tests (8 tests)
└── test_websocket_manager.py    # WebSocketManager unit tests (19 tests)
```

## DummyCoordinator — Critical Rule

Two separate `DummyCoordinator` definitions exist in the test suite:

1. **`conftest.py`** — shared lightweight stand-in used by entity-level tests
2. **`test_refresh_device.py`** — inline definition with richer coordinator methods

The DummyCoordinator in **`test_init.py`** (inline, one per test function) is the dominant pattern for integration-level tests. It bridges the real `AtmeexCoordinator` methods via `types.MethodType`.

**CRITICAL:** When a new attribute is added to `AtmeexCoordinator.__init__`, it MUST be added to ALL DummyCoordinator definitions. Currently both timestamp dicts are required:

```python
# Required in every DummyCoordinator definition that uses _async_update_data
self._ws_device_update_ts = {}
self._refresh_device_update_ts = {}   # MUST add when _refresh_device_update_ts is used
```

- `test_init.py` DummyCoordinator instances currently only declare `_ws_device_update_ts` (not `_refresh_device_update_ts`)
- `test_refresh_device.py` DummyCoordinator declares both

If you add a new instance attribute to `AtmeexCoordinator.__init__`, update both conftest.py and every inline DummyCoordinator in test_refresh_device.py and test_init.py.

## Test Structure

**Suite Organization Pattern:**
```python
# Standard integration-level test pattern (test_init.py)
@pytest.mark.asyncio
async def test_async_setup_entry_happy_path(monkeypatch):
    # 1. Define FakeApi inline
    class FakeApi:
        def __init__(self, session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = None
            self._token = "token"
            dev = AtmeexDevice.from_raw({...})
            self.get_devices = AsyncMock(return_value=[dev])
            self.get_device = AsyncMock(return_value=dev)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)

    # 2. Define DummyCoordinator with MethodType bridge
    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types
            from custom_components.atmeex_cloud.coordinator import AtmeexCoordinator as _Real
            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for m in ("_fetch_devices_safely", "_fire_api_error_event", "_async_update_data"):
                setattr(self, m, types.MethodType(getattr(_Real, m), self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", DummyCoordinator)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda hass: object())

    # 3. Build hass + entry stubs
    hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(...))
    entry = SimpleNamespace(data={...}, options={...}, entry_id="entry1", ...)

    # 4. Call and assert
    result = await atmeex_init.async_setup_entry(hass, entry)
    assert result is True
```

**Entity-level tests** (test_switch.py, test_climate.py, etc.) use a simpler pattern:
```python
def _make_switches(state=None, *, with_runtime=False):
    dev = AtmeexDevice.from_raw(RAW_DEVICE)
    coordinator = SimpleNamespace(
        data={"device_map": {"42": dev}, "states": {"42": state or {...}}},
        last_update_success=True,
        async_request_refresh=AsyncMock(),
        async_add_listener=lambda cb: (lambda: None),
    )
    api = MagicMock()
    runtime = AtmeexRuntimeData(...) if with_runtime else None
    entity = AtmeexAutoNannySwitch(
        coordinator=coordinator,
        api=api,
        device=dev,
        refresh_device_cb=refresh_cb,
        runtime=runtime,   # NOTE: runtime= kwarg required by switch constructors
    )
```

## Switch Entity Constructor — Critical Rule

Switch entities (`AtmeexAutoNannySwitch`, `AtmeexSleepModeSwitch`) accept a `runtime=` keyword argument in their constructor. Tests MUST pass `runtime=` explicitly, even when testing without runtime features:

```python
# Correct
auto = AtmeexAutoNannySwitch(
    coordinator=coordinator,
    api=api,
    device=dev,
    refresh_device_cb=refresh_cb,
    runtime=None,   # explicit None is valid
)

# Also correct (with runtime for pending state tests)
runtime = AtmeexRuntimeData(api=api, coordinator=coordinator, refresh_device=refresh_cb)
auto = AtmeexAutoNannySwitch(..., runtime=runtime)
```

## Binary Sensor EntityCategory Import

The `EntityCategory` import in binary sensor tests MUST come from `homeassistant.helpers.entity` (not `homeassistant.const` or other locations):

```python
# Correct (test_binary_sensor.py)
from homeassistant.helpers.entity import EntityCategory

def test_online_sensor_has_diagnostic_entity_category():
    online, _ = _make_sensors()
    assert online._attr_entity_category == EntityCategory.DIAGNOSTIC
```

The source (`binary_sensor.py`) uses the same import path.

## Unload Tests — asyncio.wait Pattern

Tests for `async_unload_entry` that verify timeout behaviour use `asyncio.wait_for` to bound the test itself, and verify the production code's use of `asyncio.wait({task}, timeout=N)`:

```python
@pytest.mark.asyncio
async def test_unload_entry_hung_task_does_not_block_unload(monkeypatch):
    gate = asyncio.Event()

    async def _hung_task():
        try:
            await asyncio.sleep(9999)
        finally:
            await gate.wait()   # simulate slow cleanup

    task = asyncio.create_task(_hung_task())
    await asyncio.sleep(0)      # let task start

    monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.05)
    # ...
    # Wrap entire unload in wait_for so the test itself has a deadline
    result = await asyncio.wait_for(atmeex_init.async_unload_entry(hass, entry), timeout=5.0)
    assert result is True

    gate.set()
    await asyncio.gather(task, return_exceptions=True)
```

Do NOT use `asyncio.wait_for` in the production `async_unload_entry` path for task cancellation — use `asyncio.wait({task}, timeout=N)` there. Tests use `asyncio.wait_for` only to bound the test's own execution time.

## Mocking

**Framework:** `unittest.mock` (Python standard library)

**AsyncMock for Async Functions:**
```python
from unittest.mock import AsyncMock

async_mock = AsyncMock(return_value=[device])
async_mock = AsyncMock(side_effect=ApiError("error"))
```

**MagicMock for Synchronous Methods:**
```python
from unittest.mock import MagicMock

magic_mock = MagicMock()
magic_mock.async_fire = MagicMock()   # Mock event firing
```

**SimpleNamespace for Lightweight Test Doubles:**
```python
from types import SimpleNamespace

coordinator = SimpleNamespace(
    data={"device_map": {...}, "states": {...}},
    last_update_success=True,
    async_request_refresh=AsyncMock(),
    async_add_listener=lambda cb: (lambda: None),
)
```

**Custom Test Double Classes:**
- `FakeSession` (in `test_api.py`): Mocks `aiohttp.ClientSession` with response queuing
- `FakeResponse` (in `test_api.py`): Mocks HTTP responses with status, JSON, and text
- `DummyCoordinator` (in `conftest.py` and inline in test files): Lightweight coordinator stand-in
- `FakeApi` (inline per test): Complete mock of `AtmeexApi` with configurable devices and token
- `_FlakySession` / `_ScriptedWebSocket` (in `test_websocket_manager.py`): WS-specific mocks

**Monkeypatching Module-Level Timeout Constants:**
Tests that verify timeout behaviour patch module-level constants to speed up execution:
```python
monkeypatch.setattr(atmeex_init, "_UNLOAD_TASK_TIMEOUT_SEC", 0.05)
monkeypatch.setattr(atmeex_init, "_REFRESH_TASK_TIMEOUT_SEC", 0.05)
monkeypatch.setattr(atmeex_init.time, "monotonic", lambda: ...)
```

**What to Mock:**
- HTTP client and sessions: Always mock aiohttp to avoid network calls
- External services: API responses, auth tokens
- Home Assistant machinery: hass object, config entries, coordinator
- Time functions: `time.monotonic` for testing cooldown/throttle logic

**What NOT to Mock:**
- Pure helper functions: `to_bool()`, `_normalize_device_state()` tested directly
- Data structures: `AtmeexDevice`, `AtmeexState` instantiated directly via `from_raw()`
- Dataclass instances: Test data created as actual instances, not mocks

## Fixtures and Factories

**Shared factories (from `tests/conftest.py`):**
```python
def make_fake_api_class(*, devices=None, token="token"):
    """Factory for a FakeApi class usable in monkeypatch.setattr."""
    class FakeApi:
        def __init__(self, session):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = token
            dev_objs = [AtmeexDevice.from_raw(d) for d in devices or [...]]
            self.get_devices = AsyncMock(return_value=dev_objs)
            self.get_device = AsyncMock(return_value=dev_objs[0] if dev_objs else None)

        @property
        def token(self):
            return self._token or ""
    return FakeApi


def make_hass_stub(*, with_bus=False, with_create_task=False):
    """Create a minimal hass SimpleNamespace for unit tests."""
    ...

def make_entry_stub(*, options=None):
    """Create a minimal config-entry SimpleNamespace for unit tests."""
    ...
```

**DummyCoordinator in conftest.py** (for entity-level tests — does NOT include `_refresh_device_update_ts`):
```python
class DummyCoordinator:
    def __init__(self, hass, logger, name=None, update_method=None, update_interval=None, **kwargs):
        self.hass = hass
        self.data = None
        self.last_update_success = False
        self.last_api_error = None
        self.last_success_ts = None
        self.update_calls = 0

    async def async_config_entry_first_refresh(self):
        self.data = await self.update_method()

    def async_set_updated_data(self, data):
        self.update_calls += 1
        self.data = data
```

**Device raw data pattern:**
```python
dev_raw = {
    "id": 1,
    "name": "Dev1",
    "model": "test-model",
    "online": True,
    "condition": {"pwr_on": 1, "fan_speed": 3},
    "settings": {},
}
dev = AtmeexDevice.from_raw(dev_raw)
```

**Location:**
- Shared test utilities in `tests/conftest.py`
- File-specific fixtures inline in individual test files
- Parametrized test data inline with `@pytest.mark.parametrize`

## Coverage

**Requirements:** No coverage targets enforced

**View Coverage:**
```bash
pytest --cov=custom_components/atmeex_cloud --cov-report=html
```

**Coverage File:** `.coverage` present in repo root

## Test Types

**Unit Tests:**
- Scope: Individual functions and methods
- Approach: Test in isolation with mocked dependencies
- Examples:
  - `test_login_success()` in `test_api.py`: Tests `AtmeexApi.login()` with fake response
  - `test_to_bool()` in `test_init.py`: Tests helper function with parametrized inputs
  - `test_update_data_builds_states()` in `test_coordinator.py`: Tests `AtmeexCoordinator._async_update_data()` directly
  - `test_connect_bootstraps_reconnect_until_success()` in `test_websocket_manager.py`: Tests WS reconnect logic

**Integration Tests:**
- Scope: Setup flow, coordinator interaction, entity lifecycle
- Approach: Use DummyCoordinator + MethodType bridge to run real coordinator methods without HA framework
- Examples:
  - `test_async_setup_entry_happy_path()`: Full integration setup
  - `test_refresh_device_coalesces_parallel_requests()`: Task coalescing via concurrent `asyncio.create_task`
  - `test_websocket_batch_message_updates_coordinator_once()`: WS message pipeline

**E2E Tests:**
- Not used; focus is on unit and integration testing with mocks

## Common Patterns

**Async Testing:**
- All async tests use `@pytest.mark.asyncio` (auto-applied via `asyncio_mode = auto`)
- Coroutines awaited directly: `await api.login(...)`
- `AsyncMock` used for async methods with `return_value` or `side_effect`

**Error Testing:**
```python
with pytest.raises(HomeAssistantError, match="Failed to enable AutoNanny"):
    await auto.async_turn_on()

with pytest.raises(ConfigEntryAuthFailed):
    await atmeex_init.async_setup_entry(hass, entry)

with pytest.raises(asyncio.TimeoutError):
    await runtime.refresh_device(1)
```

**Parametrized Testing:**
```python
@pytest.mark.parametrize(
    "value, expected",
    [
        (True, True),
        (1, True),
        ("foo", True),
        (None, False),
    ],
)
def test_to_bool(value, expected):
    assert to_bool(value) is expected
```

**Response Queuing (test_api.py):**
```python
session.queue_response(FakeResponse(200, json_data={...}))
session.queue_response(FakeResponse(200, json_data={...}))
# make two API calls
method, url, payload, headers = session.requests[0]
assert method == "POST"
```

**Coordinator Direct Test (test_coordinator.py):**
```python
def _make_coordinator(devices=None, get_device_side_effect=None):
    coord = AtmeexCoordinator(
        hass, logging.getLogger("test"), name="test", update_interval=None,
    )
    coord.setup_update(api=api, fire_logbook_event=MagicMock())
    return coord, api

async def test_update_data_builds_states():
    coord, api = _make_coordinator()
    data = await coord._async_update_data()
    assert "1" in data["states"]
```

**WS Message Testing Helper (test_init.py):**
```python
async def _build_ws_runtime(monkeypatch, *, initial_condition=None):
    """Shared helper for WS-enabled runtime tests."""
    ...
    return runtime, _callbacks[0], hass

async def _fire_and_drain(runtime, callback, message):
    callback(message)
    if runtime.websocket_message_task:
        await runtime.websocket_message_task
```

## Test File Locations

**Entry Points & Core:**
- `tests/test_init.py` (42 tests): Tests `__init__.py` — setup, unload, WebSocket integration, WS message processing
- `tests/test_api.py` (16 tests): Tests `api.py` — login, get_devices, get_device, set commands, error handling
- `tests/test_coordinator.py` (6 tests): Tests `coordinator.py` — `_async_update_data`, `_fetch_devices_safely`, timestamp guards
- `tests/test_websocket_manager.py` (19 tests): Tests `websocket.py` — reconnect logic, auth failures, message handling
- `tests/test_refresh_device.py` (1 test): Tests end-to-end `refresh_device` flow via full setup

**Entity Tests:**
- `tests/test_climate.py` (45 tests): Tests `climate.py`
- `tests/test_select.py` (10 tests): Tests `select.py`
- `tests/test_switch.py` (8 tests): Tests `switch.py`
- `tests/test_binary_sensor.py` (8 tests): Tests `binary_sensor.py`
- `tests/test_sensor.py` (5 tests): Tests `sensor.py`
- `tests/test_fan.py` (5 tests): Tests `fan.py`

**Feature Tests:**
- `tests/test_config_flow.py` (13 tests): Tests config flow UI
- `tests/test_race_protection.py` (14 tests): Tests concurrent access and pending state
- `tests/test_runtime.py` (6 tests): Tests `AtmeexRuntimeData` methods
- `tests/test_diagnostics.py` (3 tests): Tests diagnostics endpoint
- `tests/test_logbook.py` (2 tests): Tests logbook integration
- `tests/test_api_fallback_extra.py` (2 tests): Tests API fallback edge cases

---

*Testing analysis: 2026-05-02*
