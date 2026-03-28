# Codebase Structure

**Analysis Date:** 2026-03-28

## Directory Layout

```
atmeex_hacs/
├── custom_components/atmeex_cloud/    # Main integration package
│   ├── __init__.py                    # Setup/unload entry points, runtime data
│   ├── api.py                         # API client and data classes
│   ├── coordinator.py                 # Typed coordinator subclass
│   ├── entity_base.py                 # Shared entity mixin
│   ├── config_flow.py                 # User setup and options flow
│   ├── helpers.py                     # Conversion and normalization utilities
│   ├── const.py                       # Constants and configuration keys
│   ├── websocket.py                   # WebSocket manager for real-time updates
│   ├── diagnostics.py                 # Diagnostic data provider
│   ├── logbook.py                     # Custom event logging
│   ├── manifest.json                  # Integration metadata
│   ├── services.yaml                  # Custom service definitions
│   ├── strings.json                   # UI strings and schema
│   ├── sensor.py                      # Sensor entities
│   ├── switch.py                      # Switch entities
│   ├── fan.py                         # Fan entities
│   ├── climate.py                     # Climate/thermostat entities
│   ├── binary_sensor.py               # Binary sensor entities
│   ├── select.py                      # Select entities
│   ├── brands/                        # Brand/icon assets
│   ├── translations/                  # Localization files
│   └── __pycache__/                   # Python cache
├── tests/                             # Test suite (73+ tests)
│   ├── conftest.py                    # Pytest fixtures and setup
│   ├── test_api.py                    # API client tests
│   ├── test_init.py                   # Setup/unload tests
│   ├── test_config_flow.py            # Config flow tests
│   ├── test_sensor.py                 # Sensor entity tests
│   ├── test_switch.py                 # Switch entity tests
│   ├── test_fan.py                    # Fan entity tests
│   ├── test_climate.py                # Climate entity tests
│   ├── test_binary_sensor.py          # Binary sensor tests
│   ├── test_select.py                 # Select entity tests
│   ├── test_websocket_manager.py      # WebSocket connection tests
│   ├── test_refresh_device.py         # Device refresh logic tests
│   ├── test_race_protection.py        # Pending command race condition tests
│   ├── test_api_fallback_extra.py     # API fallback scenarios
│   ├── test_diagnostics.py            # Diagnostics provider tests
│   ├── test_logbook.py                # Event logging tests
│   └── __pycache__/                   # Python cache
├── scripts/                           # Development/manual testing scripts
│   ├── test_api_interactive.py        # Interactive API testing
│   ├── test_api_manual.py             # Manual API validation
│   ├── test_websocket_connection.py   # WebSocket debugging
│   ├── test_websocket_debug.py        # Extended WebSocket tests
│   └── test_websocket_live.py         # Live WebSocket testing
├── pytest.ini                         # Pytest configuration
├── requirements-dev.txt               # Development dependencies
├── README.en.md                       # English documentation
├── README.md                          # Russian documentation
├── HACS_SETUP.md                      # HACS installation guide
├── LICENSE                            # MIT license
├── manifest.json (root)               # NOT USED - ignore
└── hacs.json                          # HACS metadata
```

## Directory Purposes

**`custom_components/atmeex_cloud/`:**
- Purpose: Main Home Assistant integration package containing all source code
- Contains: API client, coordinator, entities, configuration flow, WebSocket handler
- Key files: `__init__.py` (entry point), `api.py` (HTTP client), `websocket.py` (real-time updates)

**`tests/`:**
- Purpose: Comprehensive pytest test suite with 73+ automated tests
- Contains: Unit tests, integration tests for all entity types and API functionality
- Key files: `conftest.py` (fixtures), `test_race_protection.py` (concurrency tests)

**`scripts/`:**
- Purpose: Manual testing and debugging utilities for development
- Contains: Interactive API testing and WebSocket debugging scripts
- NOT imported by main integration - development only

**`brands/`:**
- Purpose: Integration branding assets (icons, logos)
- Contains: Atmeex brand configuration and SVG assets
- Key path: `brands/atmeex_cloud/`

**`translations/`:**
- Purpose: Localization files for different languages
- Contains: Language-specific UI strings
- Key path: `translations/{language}.json`

## Key File Locations

**Entry Points:**
- `custom_components/atmeex_cloud/__init__.py`: `async_setup_entry()`, `async_unload_entry()` - integration lifecycle
- `custom_components/atmeex_cloud/config_flow.py`: `AtmeexConfigFlow.async_step_user()` - user setup
- Platform entries: `sensor.py`, `switch.py`, `fan.py`, etc. - entity creation per platform

**Configuration:**
- `custom_components/atmeex_cloud/const.py`: Domain name, platforms, API URLs, constants
- `custom_components/atmeex_cloud/strings.json`: UI schema and translatable strings
- `custom_components/atmeex_cloud/manifest.json`: Integration metadata, dependencies, version
- `custom_components/atmeex_cloud/services.yaml`: Custom service definitions

**Core Logic:**
- `custom_components/atmeex_cloud/api.py`: `AtmeexApi` client, `AtmeexDevice`, `AtmeexState` data classes
- `custom_components/atmeex_cloud/coordinator.py`: `AtmeexCoordinator`, `AtmeexCoordinatorData` TypedDict
- `custom_components/atmeex_cloud/__init__.py`: `AtmeexRuntimeData`, update logic, WebSocket integration

**Entity Implementations:**
- `custom_components/atmeex_cloud/sensor.py`: Diagnostic sensor, CO2, inlet temp, humidity sensors
- `custom_components/atmeex_cloud/switch.py`: Power on/off, sleep mode switches
- `custom_components/atmeex_cloud/fan.py`: Fan speed control (0-7 with percentage conversion)
- `custom_components/atmeex_cloud/climate.py`: Thermostat with temperature control
- `custom_components/atmeex_cloud/binary_sensor.py`: Online/offline status indicator
- `custom_components/atmeex_cloud/select.py`: Mode selection (supply_ventilation, recirculation, etc.)

**Utilities:**
- `custom_components/atmeex_cloud/entity_base.py`: `AtmeexEntityMixin` for shared entity logic
- `custom_components/atmeex_cloud/helpers.py`: Unit conversion (fan speed, temperature, humidity)
- `custom_components/atmeex_cloud/websocket.py`: `WebSocketManager` for real-time updates
- `custom_components/atmeex_cloud/diagnostics.py`: Diagnostic data snapshots
- `custom_components/atmeex_cloud/logbook.py`: Custom event logging

**Testing:**
- `tests/conftest.py`: Fixtures, mocks, setup for all tests
- `tests/test_init.py`: Setup/unload, runtime data initialization
- `tests/test_api.py`: API client methods, error handling, retries
- `tests/test_race_protection.py`: Pending commands, device locks, state consistency
- `tests/test_websocket_manager.py`: Connection lifecycle, auth, message handling

## Naming Conventions

**Files:**
- Entity modules: lowercase with underscores (`sensor.py`, `switch.py`, `fan.py`)
- Utilities: lowercase with underscores (`helpers.py`, `websocket.py`)
- Tests: `test_{module_name}.py` (e.g., `test_sensor.py`, `test_api.py`)
- Configuration: constants in lowercase (`const.py`, `config_flow.py`)

**Directories:**
- Package directory: lowercase domain name (`atmeex_cloud`)
- Asset directories: lowercase plural (`brands/`, `translations/`, `tests/`)

**Classes:**
- CamelCase with domain prefix: `AtmeexApi`, `AtmeexDevice`, `AtmeexState`, `AtmeexCoordinator`, `AtmeexEntityMixin`
- Flow classes: `AtmeexConfigFlow`, `AtmeexOptionsFlowHandler`
- Entity classes: Specific to type: `AtmeexCO2Sensor`, `AtmeexPowerSwitch`, `AtmeexFanEntity`, etc.

**Functions/Methods:**
- Private functions: Leading underscore `_async_update_data()`, `_refresh_device_once()`, `_apply_websocket_message()`
- Async functions: `async_` prefix (`async_setup_entry()`, `async_turn_on()`)
- Conversion helpers: Descriptive names (`api_to_fan_speed()`, `deci_to_c()`, `c_to_deci()`)

**Variables:**
- Instance attributes: Underscore prefix for "private" (`_api`, `_token`, `_lock`)
- Dict keys: Lowercase with underscores (`"device_map"`, `"fan_speed"`, `"pwr_on"`)
- Constants: UPPERCASE (`DOMAIN`, `PLATFORMS`, `API_BASE_URL`)

## Where to Add New Code

**New Sensor/Switch/Entity:**
- Primary code: `custom_components/atmeex_cloud/{sensor|switch|fan|climate}.py`
  - Create entity class inheriting `CoordinatorEntity` + `AtmeexEntityMixin`
  - Implement `@property` for state/value from `_device_state` dict
  - Use `async_turn_on()`, `async_set_percentage()` etc. for commands
  - Call `_refresh()` after command to fetch updated state
  - Add to `async_setup_entry()` to register with coordinator
- Tests: `tests/test_{entity_type}.py`
  - Create fixtures in conftest
  - Test state reading, state changes, error handling
  - Use mocked coordinator with test data

**New API Method:**
- Implementation: `custom_components/atmeex_cloud/api.py`
  - Add method to `AtmeexApi` class
  - Use `self._session.post()/get()` with error handling
  - Distinguish 401/403 auth errors from other errors
  - Raise `ApiError` with optional status code
- Tests: `tests/test_api.py`
  - Mock `aiohttp.ClientSession`
  - Test success, auth failure, network failure cases

**New Conversion/Helper:**
- Location: `custom_components/atmeex_cloud/helpers.py`
  - Add function near related conversions
  - Document with docstring including example values
  - Add validation for edge cases (None, invalid types)
- Tests: In relevant entity test file or as standalone test

**New Configuration Option:**
- Constants: `custom_components/atmeex_cloud/const.py`
  - Add `CONF_*` and `DEFAULT_*` constants
- Schema: `custom_components/atmeex_cloud/strings.json`
  - Add schema entry under `config` → `step` → `options`
- Implementation: `custom_components/atmeex_cloud/config_flow.py`
  - Add to `_resolve_*_option()` function or add new function
  - Use in `async_setup_entry()` to apply option value

**New Integration Feature (Service/Event):**
- Service definition: `custom_components/atmeex_cloud/services.yaml`
  - Define service with parameters and description
- Implementation: `custom_components/atmeex_cloud/__init__.py`
  - Add handler function in `async_setup_entry()`
  - Register via `hass.services.async_register()`
- Events: Fire via `hass.bus.async_fire(EVENT_TYPE, data)`
  - Define event constant in `const.py`
  - Fire from coordinator updates or WebSocket messages

## Special Directories

**`__pycache__/`:**
- Purpose: Python bytecode cache
- Generated: Yes (automatic)
- Committed: No (.gitignore)

**`brands/atmeex_cloud/`:**
- Purpose: Brand assets for HACS visibility
- Generated: No (manual)
- Committed: Yes

**`translations/`:**
- Purpose: UI localization (en.json, ru.json, etc.)
- Generated: No (manual)
- Committed: Yes

**`.venv/`:**
- Purpose: Python virtual environment
- Generated: Yes (via `python -m venv .venv`)
- Committed: No (.gitignore)

**`.pytest_cache/`:**
- Purpose: Pytest cache for test discovery
- Generated: Yes (automatic)
- Committed: No (.gitignore)

**`.coverage`:**
- Purpose: Test coverage data
- Generated: Yes (via pytest-cov)
- Committed: No (.gitignore)

---

*Structure analysis: 2026-03-28*
