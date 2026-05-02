# Codebase Structure

**Analysis Date:** 2026-05-02

## Directory Layout

```
atmeex_hacs/
├── custom_components/atmeex_cloud/    # Main integration package
│   ├── __init__.py                    # async_setup_entry, async_unload_entry, refresh_device closure
│   ├── runtime.py                     # PendingCommand + AtmeexRuntimeData dataclasses
│   ├── api.py                         # AtmeexApi HTTP client, AtmeexDevice, AtmeexState, ApiError
│   ├── coordinator.py                 # AtmeexCoordinator, AtmeexCoordinatorData TypedDict
│   ├── entity_base.py                 # AtmeexEntityMixin, setup_dynamic_device_entities, supports_humidifier
│   ├── config_flow.py                 # AtmeexConfigFlow, AtmeexOptionsFlowHandler
│   ├── helpers.py                     # Conversion utils: fan speed, temp, humidity_to_stage, WS state merge
│   ├── const.py                       # DOMAIN, PLATFORMS, API URLs, event names, option keys
│   ├── websocket.py                   # WebSocketManager, WebSocketConfig
│   ├── diagnostics.py                 # Diagnostic data provider (redacted snapshots)
│   ├── logbook.py                     # Custom event logging
│   ├── manifest.json                  # Integration metadata and dependencies
│   ├── services.yaml                  # Custom service definitions
│   ├── strings.json                   # UI strings and config schema
│   ├── sensor.py                      # Sensor entities (CO2, temperature, humidity, diagnostic)
│   ├── switch.py                      # Switch entities (AtmeexAutoNannySwitch, AtmeexSleepModeSwitch)
│   ├── fan.py                         # Fan entity (AtmeexFanEntity, speed 1-7 ↔ percentage)
│   ├── climate.py                     # Climate/thermostat entity with humidifier support
│   ├── binary_sensor.py               # AtmeexOnlineSensor (DIAGNOSTIC), AtmeexNoWaterSensor
│   ├── select.py                      # Select entity (breezer mode)
│   ├── brands/                        # Brand/icon assets for HACS
│   ├── translations/                  # Localization files (en.json, ru.json, …)
│   └── __pycache__/                   # Python bytecode cache (not committed)
├── tests/                             # Pytest test suite
│   ├── conftest.py                    # Fixtures, mocks, DummyCoordinator, shared setup
│   ├── test_api.py                    # AtmeexApi client tests
│   ├── test_init.py                   # Setup/unload, runtime data initialization
│   ├── test_config_flow.py            # Config flow tests
│   ├── test_sensor.py                 # Sensor entity tests
│   ├── test_switch.py                 # Switch entity tests
│   ├── test_fan.py                    # Fan entity tests
│   ├── test_climate.py                # Climate entity tests
│   ├── test_binary_sensor.py          # Binary sensor tests
│   ├── test_select.py                 # Select entity tests
│   ├── test_websocket_manager.py      # WebSocket connection lifecycle tests
│   ├── test_refresh_device.py         # Device refresh coalescing tests
│   ├── test_race_protection.py        # Pending command / race condition tests
│   ├── test_api_fallback_extra.py     # API fallback scenario tests
│   ├── test_diagnostics.py            # Diagnostics provider tests
│   ├── test_logbook.py                # Event logging tests
│   └── __pycache__/                   # Not committed
├── scripts/                           # Manual development/debugging utilities
│   ├── test_api_interactive.py
│   ├── test_api_manual.py
│   ├── test_websocket_connection.py
│   ├── test_websocket_debug.py
│   └── test_websocket_live.py
├── pytest.ini                         # Pytest configuration
├── requirements-dev.txt               # Development dependencies
├── README.en.md                       # English documentation
├── README.md                          # Russian documentation
├── HACS_SETUP.md                      # HACS installation guide
├── LICENSE                            # MIT license
└── hacs.json                          # HACS metadata
```

## Directory Purposes

**`custom_components/atmeex_cloud/`:**
- Purpose: Main Home Assistant integration package containing all source code
- Contains: API client, coordinator, runtime data, entities, config flow, WebSocket handler
- Key files: `__init__.py` (lifecycle), `runtime.py` (state container), `coordinator.py` (polling), `api.py` (HTTP)

**`tests/`:**
- Purpose: Comprehensive pytest test suite
- Contains: Unit and integration tests for all entity types, API, refresh coalescing, race conditions
- Key files: `conftest.py` (fixtures + `DummyCoordinator`), `test_race_protection.py`, `test_refresh_device.py`

**`scripts/`:**
- Purpose: Manual testing and debugging utilities; NOT imported by the main integration
- Contains: Interactive API and WebSocket debugging scripts for development only

**`brands/atmeex_cloud/`:**
- Purpose: Integration branding assets (icons/logos) for HACS visibility
- Generated: No (manual)
- Committed: Yes

**`translations/`:**
- Purpose: UI localization strings per language
- Generated: No (manual)
- Committed: Yes

## Key File Locations

**Entry Points:**
- `custom_components/atmeex_cloud/__init__.py`: `async_setup_entry()`, `async_unload_entry()` — integration lifecycle
- `custom_components/atmeex_cloud/config_flow.py`: `AtmeexConfigFlow.async_step_user()` — user setup
- Platform entries: `sensor.py`, `switch.py`, `fan.py`, `climate.py`, `binary_sensor.py`, `select.py` — entity creation per platform

**Configuration:**
- `custom_components/atmeex_cloud/const.py`: `DOMAIN`, `PLATFORMS`, `API_BASE_URL`, event names, option keys, retry constants
- `custom_components/atmeex_cloud/strings.json`: UI schema and translatable strings
- `custom_components/atmeex_cloud/manifest.json`: Integration metadata, dependencies, version
- `custom_components/atmeex_cloud/services.yaml`: Custom service definitions

**Core Logic:**
- `custom_components/atmeex_cloud/runtime.py`: `PendingCommand`, `AtmeexRuntimeData` — moved from `__init__.py`; pure data, no HA deps
- `custom_components/atmeex_cloud/api.py`: `AtmeexApi`, `AtmeexDevice`, `AtmeexState`, `ApiError`
- `custom_components/atmeex_cloud/coordinator.py`: `AtmeexCoordinator`, `AtmeexCoordinatorData`, `_ws_device_update_ts`, `_refresh_device_update_ts`
- `custom_components/atmeex_cloud/__init__.py`: `refresh_device()` closure (coalescing, 65 s timeout), WebSocket plumbing, `ws_reauth_last_ts` float throttle

**Entity Implementations:**
- `custom_components/atmeex_cloud/sensor.py`: Diagnostic sensor, CO2, inlet temp, humidity sensors
- `custom_components/atmeex_cloud/switch.py`: `AtmeexAutoNannySwitch`, `AtmeexSleepModeSwitch` — use `_execute_command` with `pending_attr`/`pending_value`; `is_on` uses `_state_with_pending`
- `custom_components/atmeex_cloud/fan.py`: `AtmeexFanEntity` — fan speed 1–7 ↔ percentage, optimistic `is_on` and `percentage` via `_state_with_pending`
- `custom_components/atmeex_cloud/climate.py`: Thermostat with optional humidifier support
- `custom_components/atmeex_cloud/binary_sensor.py`: `AtmeexOnlineSensor` (`EntityCategory.DIAGNOSTIC`; `available` checks `last_success_ts` vs `update_interval*3`), `AtmeexNoWaterSensor`
- `custom_components/atmeex_cloud/select.py`: Breezer mode selection

**Utilities:**
- `custom_components/atmeex_cloud/entity_base.py`: `AtmeexEntityMixin` (`device_info` is plain `@property`), `setup_dynamic_device_entities`, `supports_humidifier`
- `custom_components/atmeex_cloud/helpers.py`: `fan_speed_to_percent`, `api_to_fan_speed`, `fan_speed_to_api`, `deci_to_c`, `c_to_deci`, `quantize_humidity`, `humidity_to_stage` (new — returns HUM_ALLOWED stage index 0–3), `apply_condition_update`, `apply_settings_update`, `_normalize_device_state`
- `custom_components/atmeex_cloud/websocket.py`: `WebSocketManager`, `WebSocketConfig`
- `custom_components/atmeex_cloud/diagnostics.py`: Redacted diagnostic snapshots
- `custom_components/atmeex_cloud/logbook.py`: Custom logbook event helpers

**Testing:**
- `tests/conftest.py`: Fixtures, mocks, `DummyCoordinator` helper
- `tests/test_init.py`: Setup/unload, runtime data initialization, task cancellation
- `tests/test_api.py`: API client methods, error handling, retry logic
- `tests/test_race_protection.py`: Pending commands, device locks, state consistency
- `tests/test_refresh_device.py`: Coalescing, timeout, `_refresh_device_update_ts` recording
- `tests/test_websocket_manager.py`: Connection lifecycle, auth, reconnect backoff

## Naming Conventions

**Files:**
- Entity modules: lowercase with underscores (`sensor.py`, `binary_sensor.py`)
- Utilities: lowercase with underscores (`helpers.py`, `websocket.py`, `runtime.py`)
- Tests: `test_{module_name}.py` (e.g. `test_sensor.py`, `test_refresh_device.py`)

**Directories:**
- Package directory: lowercase domain name (`atmeex_cloud`)
- Asset directories: lowercase plural (`brands/`, `translations/`)

**Classes:**
- CamelCase with domain prefix: `AtmeexApi`, `AtmeexDevice`, `AtmeexState`, `AtmeexCoordinator`, `AtmeexRuntimeData`, `AtmeexEntityMixin`
- Entity classes: `AtmeexCO2Sensor`, `AtmeexAutoNannySwitch`, `AtmeexFanEntity`, `AtmeexOnlineSensor`, `_BaseSwitch` (private base)

**Functions/Methods:**
- Private: leading underscore (`_async_update_data`, `_refresh_device_once`, `_apply_websocket_message`, `_connect_once`)
- Async: `async_` prefix (`async_setup_entry`, `async_turn_on`)
- Conversion helpers: descriptive names (`api_to_fan_speed`, `deci_to_c`, `humidity_to_stage`)

**Variables:**
- Instance attributes: underscore prefix for private (`_api`, `_token`, `_lock`)
- Dict keys: lowercase with underscores (`"device_map"`, `"fan_speed"`, `"pwr_on"`)
- Constants: UPPERCASE (`DOMAIN`, `PLATFORMS`, `API_BASE_URL`, `_REFRESH_TASK_TIMEOUT_SEC`)

## Where to Add New Code

**New Entity Type:**
- Primary code: `custom_components/atmeex_cloud/{entity_type}.py`
  - Inherit from `CoordinatorEntity` + `AtmeexEntityMixin`
  - Accept `runtime: AtmeexRuntimeData` as constructor param and store as `self._runtime`
  - Read state from `self._device_state` dict
  - Use `await self._execute_command(self.api.set_*(…), pending_attr=…, pending_value=…)` for all commands
  - Use `self._state_with_pending(attribute, confirmed, tolerance=_PENDING_TTL)` for optimistic display
  - Register via `setup_dynamic_device_entities()` in `async_setup_entry`
- Tests: `tests/test_{entity_type}.py`

**New API Method:**
- Location: `custom_components/atmeex_cloud/api.py` in `AtmeexApi` class
  - GET/list: use `self._with_retries(lambda: self._request(…), "action_name")`
  - SET/PUT: use `await self._put_params(device_id, body, "action_name")` directly — do NOT wrap in `_with_retries`
  - Raise `ApiError` with `status=` for HTTP errors; caller distinguishes 401/403

**New Conversion/Helper:**
- Location: `custom_components/atmeex_cloud/helpers.py`
  - Add near related conversions
  - Include docstring with example values and edge cases (None, invalid types)
  - If used in WebSocket incremental updates, also add to `apply_condition_update` and/or `apply_settings_update`

**New Configuration Option:**
- Constants: `custom_components/atmeex_cloud/const.py` — add `CONF_*` and `DEFAULT_*`
- Schema: `custom_components/atmeex_cloud/strings.json` — add under `config` → `step` → `options`
- Implementation: `custom_components/atmeex_cloud/config_flow.py`
- Apply in: `custom_components/atmeex_cloud/__init__.py` `async_setup_entry()`

**New Integration Feature (Service/Event):**
- Service definition: `custom_components/atmeex_cloud/services.yaml`
- Handler: `custom_components/atmeex_cloud/__init__.py` — register via `hass.services.async_register()`
- Event constants: `custom_components/atmeex_cloud/const.py`
- Fire via `_fire_logbook_event(EVENT_TYPE, data)` local helper

**New Runtime Data Field:**
- Location: `custom_components/atmeex_cloud/runtime.py`
- Add to `AtmeexRuntimeData` dataclass with `field(default=…)` or `field(default_factory=…)`
- No HA imports in this module — keep it pure data

## Special Directories

**`__pycache__/`:**
- Purpose: Python bytecode cache
- Generated: Yes
- Committed: No

**`brands/atmeex_cloud/`:**
- Purpose: Brand assets for HACS
- Generated: No
- Committed: Yes

**`translations/`:**
- Purpose: UI localization (en.json, ru.json, …)
- Generated: No
- Committed: Yes

**`.venv/`:**
- Purpose: Python virtual environment
- Generated: Yes
- Committed: No

**`.pytest_cache/`:**
- Purpose: Pytest test cache
- Generated: Yes
- Committed: No

**`.planning/codebase/`:**
- Purpose: GSD codebase map documents (ARCHITECTURE.md, STRUCTURE.md, etc.)
- Generated: Yes (by `/gsd-map-codebase`)
- Committed: Yes

---

*Structure analysis: 2026-05-02*
