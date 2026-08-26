# Technology Stack

> ⚠️ **Stale (2026-07-17):** this document predates the state-store / command-executor rework (v0.10.0). Concurrency is now handled by per-field revisions in `state_store.py`; commands run through `command_executor.py`. See `CLAUDE.md` and `CONCERNS.md` (addendum) for current facts; where this file disagrees with the code, the code wins.

**Analysis Date:** 2026-05-02

## Languages

**Primary:**
- Python 3.12 - Custom Home Assistant component (integration)

## Runtime

**Environment:**
- Home Assistant 2024.8.0+ (minimum version requirement from `custom_components/atmeex_cloud/manifest.json`)
- Runs as custom component within Home Assistant Python environment

**Package Manager:**
- pip (Python package manager)
- Lockfile: Not used (pinned via `manifest.json` requirements)

## Frameworks

**Core:**
- Home Assistant Core 2024.8.0+ - Smart home automation platform providing component infrastructure
- Home Assistant Config Entries - Configuration and state management for the integration

**HTTP/WebSocket:**
- aiohttp 3.9.0+ - Async HTTP client and WebSocket support for API communication and real-time updates
- asyncio - Python standard library for async/await patterns

**Testing:**
- pytest - Test runner for unit and integration tests
- pytest-asyncio - Async test support (enabled via `asyncio_mode = auto` in `pytest.ini`)
- pytest-homeassistant-custom-component - Home Assistant testing helpers and fixtures

**Build/Dev:**
- No explicit build tool required (custom component loaded directly by Home Assistant)

## Key Dependencies

**Critical:**
- aiohttp 3.9.0+ - Handles all REST API and WebSocket communication to Atmeex Cloud API. Essential for device control and real-time state updates.

**Infrastructure:**
- asyncio (stdlib) - Enables concurrent handling of multiple devices, WebSocket connections, and API retries
- dataclasses (stdlib) - Used for `AtmeexDevice` (`slots=True`), `AtmeexState`, `WebSocketConfig`, `PendingCommand`, and `AtmeexRuntimeData` typed data classes
- collections.deque (stdlib) - Bounded websocket message queue (maxlen=500) in `__init__.py`

**Home Assistant Helpers:**
- `homeassistant.helpers.aiohttp_client` - Provides shared HTTP session for entire HA instance
- `homeassistant.helpers.update_coordinator` - `DataUpdateCoordinator` pattern for periodic polling
- `homeassistant.helpers.device_registry` - Device management across HA
- `homeassistant.helpers.entity` - `EntityCategory.DIAGNOSTIC` used in `binary_sensor.py`
- `homeassistant.config_entries` - Configuration flow and entry management

## Configuration

**Environment:**
- Credentials stored in Home Assistant config entry (email/password for Atmeex Cloud API)
- No `.env` file used (Home Assistant handles secrets natively)
- Configuration options (all in `custom_components/atmeex_cloud/const.py`):
  - `CONF_UPDATE_INTERVAL` (default: 30 seconds, range: 10–300)
  - `CONF_ENABLE_WEBSOCKET` (default: True)
  - `CONF_ENABLE_CO2` (default: True)

**Build:**
- No build step required
- Component loaded directly from `custom_components/atmeex_cloud/`
- Entry point: `custom_components/atmeex_cloud/__init__.py`

## Integration Version

- Current version: `0.8.5` (`manifest.json`)
- Integration type: `hub`
- IoT class: `cloud_polling`

## API & External Service Configuration

**Atmeex Cloud API:**
- Base URL: `https://api.iot.atmeex.com` (constant `API_BASE_URL` in `const.py`)
- Auth: Bearer token-based (OAuth 2.0 flow, obtained via email/password login, with refresh token support)
- Timeout: 20 seconds (default, per-request in `api.py`)
- Retry strategy: Exponential backoff (base 1.0s, max 32.0s, up to 3 attempts) — network errors only; PUT commands intentionally not retried
- Token refresh buffer: 60 seconds before expiration (`TOKEN_REFRESH_BUFFER_SEC` in `const.py`)

**WebSocket:**
- Base URL: `wss://ws.iot.atmeex.com` (`WS_BASE_URL` in `websocket.py`)
- Purpose: Real-time device state updates (optional, falls back to HTTP polling)
- Reconnect: Exponential backoff (min 1.0s, max 60.0s); backoff **unconditionally resets to min** on successful connection
- Ping interval: 30 seconds; ping timeout: 10 seconds
- Auth failure threshold: 5 consecutive `{"type":"unauthorized"}` messages trigger reauth flow
- Reauth cooldown: `_WS_REAUTH_COOLDOWN_SEC = 300.0` (5 minutes) defined in `__init__.py` — limits how often the WS auth failure triggers a config-entry reauth prompt

**Task lifecycle constants (module-level in `__init__.py` for testability):**
- `_UNLOAD_TASK_TIMEOUT_SEC = 5.0` — max wait for WS tasks to cancel during entry unload
- `_REFRESH_TASK_TIMEOUT_SEC = 65.0` — max wait for a coalesced in-flight `refresh_device` task (covers 3 retries × 20s + headroom)

## Platform Requirements

**Development:**
- Python 3.12 with Home Assistant development environment
- Test dependencies: `pytest`, `pytest-asyncio`, `pytest-homeassistant-custom-component`, `aiohttp` (see `requirements-dev.txt`)
- `pythonpath = .` set in `pytest.ini` so `custom_components` is importable as a package
- Test suite: 166 test functions across 18 test files in `tests/`

**Production:**
- Home Assistant installation (Docker, QEMU, native, etc.)
- Network access to `api.iot.atmeex.com` and `wss://ws.iot.atmeex.com`
- HTTPS/WSS capable (TLS 1.2+)

---

*Stack analysis: 2026-05-02*
