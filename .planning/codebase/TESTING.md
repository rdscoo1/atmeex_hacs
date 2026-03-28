# Testing Patterns

**Analysis Date:** 2026-03-28

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
pytest                          # Run all tests
pytest -v                       # Verbose output
pytest tests/test_api.py        # Run specific test file
pytest -k test_login_success    # Run tests matching pattern
pytest --asyncio-mode=auto      # Explicit async mode
```

## Test File Organization

**Location:**
- Co-located in `tests/` directory at project root
- Test files parallel source structure but separate
- Example: `custom_components/atmeex_cloud/api.py` tested by `tests/test_api.py`

**Naming:**
- Test files: `test_*.py` prefix (e.g., `test_api.py`, `test_sensor.py`)
- Test functions: `test_*` prefix (e.g., `test_login_success`, `test_get_devices_error_no_fallback`)
- Parametrized tests: `@pytest.mark.parametrize` used for multiple test cases

**Structure:**
```
tests/
├── conftest.py           # Shared fixtures and factory functions
├── test_api.py           # API client tests
├── test_init.py          # Integration setup tests
├── test_sensor.py        # Sensor entity tests
├── test_climate.py       # Climate entity tests
├── test_fan.py           # Fan entity tests
├── test_binary_sensor.py # Binary sensor tests
├── test_select.py        # Select entity tests
├── test_switch.py        # Switch entity tests
├── test_config_flow.py   # Config flow tests
├── test_diagnostics.py   # Diagnostics tests
└── ... (more entity tests)
```

## Test Structure

**Test Organization Pattern:**
- Fixture setup in `conftest.py` provides reusable test doubles
- Unit tests for individual functions/methods
- Integration tests for setup flow and coordinator interaction
- Mock-heavy approach: FakeApi, FakeSession, DummyCoordinator, mock.MagicMock

**Example Test Suite Structure (from test_api.py):**
```python
import pytest
from custom_components.atmeex_cloud.api import AtmeexApi, ApiError

class FakeResponse:
    """Mock HTTP response."""
    def __init__(self, status: int, json_data=None, text_data=""):
        ...

class FakeSession:
    """Mock aiohttp.ClientSession."""
    def __init__(self):
        ...
    def queue_response(self, resp: FakeResponse):
        ...

@pytest.mark.asyncio
async def test_login_success():
    """Test successful login flow."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={...}))
    api = AtmeexApi(session)
    await api.login("user@example.com", "pwd")
    assert api._token == "token123"
```

**Setup and Teardown:**
- No explicit setup/teardown fixtures; tests create isolated mock objects
- Parametrization used instead of loops: `@pytest.mark.parametrize`
- Example from `test_init.py`:
  ```python
  @pytest.mark.parametrize(
      "value, expected",
      [
          (True, True),
          (False, False),
          (1, True),
          (0, False),
      ],
  )
  def test_to_bool(value, expected):
      assert to_bool(value) is expected
  ```

## Mocking

**Framework:** `unittest.mock` (Python standard library)

**Patterns:**

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
magic_mock.async_fire = MagicMock()  # Mock event firing
```

**SimpleNamespace for Test Doubles:**
```python
from types import SimpleNamespace

hass = SimpleNamespace(
    data={},
    config_entries=SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
    ),
)
```

**Custom Test Double Classes:**
- `FakeSession`: Mocks `aiohttp.ClientSession` with response queuing
- `FakeResponse`: Mocks HTTP responses with status, JSON, and text
- `DummyCoordinator`: Lightweight coordinator stand-in without HA machinery
- `FakeApi`: Complete mock of `AtmeexApi` with configurable devices and token

**Factory Functions (from conftest.py):**
```python
def make_fake_api_class(*, devices: list[dict] | None = None, token: str | None = "token"):
    """Factory for a FakeApi class usable in monkeypatch.setattr."""
    class FakeApi:
        def __init__(self, session):
            self.session = session
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self._token = token
            dev_objs = [AtmeexDevice.from_raw(d) for d in devices or [...]]
            self.get_devices = AsyncMock(return_value=dev_objs)
            self.get_device = AsyncMock(return_value=dev_objs[0] if dev_objs else None)
    return FakeApi

def make_hass_stub(*, with_bus: bool = False, with_create_task: bool = False):
    """Create a minimal hass SimpleNamespace for unit tests."""
    ...

def make_entry_stub(*, options: dict | None = None):
    """Create a minimal config-entry SimpleNamespace for unit tests."""
    ...
```

**What to Mock:**
- HTTP client and sessions: Always mock aiohttp to avoid network calls
- External services: API responses, auth tokens
- Home Assistant machinery: hass object, config entries, coordinator
- Async tasks: Can mock or use real asyncio in tests marked `@pytest.mark.asyncio`

**What NOT to Mock:**
- Pure helper functions: `to_bool()`, `_normalize_device_state()` tested directly
- Data structures: `AtmeexDevice`, `AtmeexState` instantiated directly
- Dataclass instances: Test data created as actual instances, not mocks

## Fixtures and Factories

**Test Data:**
- Device objects created from raw dict via `AtmeexDevice.from_raw()`:
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
- File-specific fixtures in individual test files (e.g., `FakeSession` in `test_api.py`)
- Parametrized test data inline with `@pytest.mark.parametrize`

**Example Fixture from conftest.py:**
```python
class DummyCoordinator:
    """Lightweight coordinator stand-in used by integration-level tests."""
    def __init__(self, hass, logger, name=None, update_method=None, update_interval=None, **kwargs):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_method = update_method
        self.update_interval = update_interval
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

## Coverage

**Requirements:** No coverage targets enforced; `.coverage` file exists from prior runs

**View Coverage:**
```bash
pytest --cov=custom_components/atmeex_cloud --cov-report=html
```

**Coverage File:** `.coverage` present in repo root (generated by pytest runs)

## Test Types

**Unit Tests:**
- Scope: Individual functions and methods
- Approach: Test in isolation with mocked dependencies
- Examples:
  - `test_login_success()`: Tests `AtmeexApi.login()` with fake response
  - `test_to_bool()`: Tests helper function with parametrized inputs
  - `test_normalize_device_state_basic()`: Tests state normalization logic
- No external network calls; all HTTP mocked

**Integration Tests:**
- Scope: Setup flow, coordinator interaction, entity integration
- Approach: Use lightweight test doubles (DummyCoordinator) to simulate system behavior
- Examples:
  - `test_async_setup_entry_happy_path()`: Tests full integration setup
  - `test_sensor_exposes_basic_attrs()`: Tests sensor entity with dummy coordinator
  - `test_websocket_manager.py`: Tests WebSocket integration with mocked asyncio
- Exercise multiple components together but avoid HA framework code

**E2E Tests:**
- Not used; Home Assistant integration testing relies on pytest-homeassistant-custom-component
- Focus is on unit and integration testing with mocks

## Common Patterns

**Async Testing:**
- All async tests marked with `@pytest.mark.asyncio`
- Coroutines awaited directly: `await api.login(...)`
- `AsyncMock` used for async methods with `return_value` or `side_effect`
- Example:
  ```python
  @pytest.mark.asyncio
  async def test_get_devices_success():
      session = FakeSession()
      session.queue_response(FakeResponse(200, json_data=[{"id": 1}]))
      api = AtmeexApi(session)
      api._token = "t"
      result = await api.get_devices()
      assert len(result) == 1
  ```

**Error Testing:**
- Exceptions tested via `pytest.raises(ExceptionType) as exc`
- Error message content verified: `assert "error text" in str(exc.value)`
- Status codes tested via custom exception attributes: `getattr(err, "status", None)`
- Example:
  ```python
  @pytest.mark.asyncio
  async def test_login_error_raises_apierror():
      session = FakeSession()
      session.queue_response(FakeResponse(401, text_data="unauthorized"))
      api = AtmeexApi(session)
      with pytest.raises(ApiError) as exc:
          await api.login("user@example.com", "wrong")
      assert "Auth failed 401" in str(exc.value)
  ```

**Parametrized Testing:**
- Used for multiple similar test cases
- Defined inline with `@pytest.mark.parametrize`
- Parameters listed as tuple pairs of input/expected
- Example:
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

**Response Queuing:**
- FakeSession uses queue for sequential response ordering
- Multiple calls expected: queue multiple responses
- Assertion on requests made: `session.requests` list tracks all calls
- Example:
  ```python
  session.queue_response(FakeResponse(200, json_data={...}))
  session.queue_response(FakeResponse(200, json_data={...}))
  # ... make two API calls ...
  method, url, payload, headers = session.requests[0]
  assert method == "POST"
  ```

## Test File Locations

**Entry Points & Core:**
- `tests/test_init.py`: Tests `custom_components/atmeex_cloud/__init__.py` setup flow
- `tests/test_api.py`: Tests `custom_components/atmeex_cloud/api.py` API client
- `tests/test_websocket_manager.py`: Tests WebSocket manager

**Entity Tests:**
- `tests/test_sensor.py`: Tests `sensor.py`
- `tests/test_climate.py`: Tests `climate.py`
- `tests/test_fan.py`: Tests `fan.py`
- `tests/test_binary_sensor.py`: Tests `binary_sensor.py`
- `tests/test_select.py`: Tests `select.py`
- `tests/test_switch.py`: Tests `switch.py`

**Feature Tests:**
- `tests/test_config_flow.py`: Tests config flow UI
- `tests/test_diagnostics.py`: Tests diagnostics endpoint
- `tests/test_logbook.py`: Tests logbook integration
- `tests/test_race_protection.py`: Tests race condition handling
- `tests/test_api_fallback_extra.py`: Tests API fallback behavior
- `tests/test_refresh_device.py`: Tests device refresh logic

---

*Testing analysis: 2026-03-28*
