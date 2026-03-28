# Technology Stack

**Analysis Date:** 2026-03-28

## Languages

**Primary:**
- Python 3.12 - Custom Home Assistant component (integration)

## Runtime

**Environment:**
- Home Assistant 2024.8.0+ (minimum version requirement from `manifest.json`)
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
- aiohttp 3.9.0+ - Why it matters: Handles all REST API and WebSocket communication to Atmeex Cloud API. Essential for device control and real-time state updates.

**Infrastructure:**
- asyncio (stdlib) - Enables concurrent handling of multiple devices, WebSocket connections, and API retries
- voluptuous (inferred from config_flow.py) - Schema validation for configuration UI input

**Home Assistant Helpers:**
- homeassistant.helpers.aiohttp_client - Provides shared HTTP session for entire HA instance
- homeassistant.helpers.update_coordinator - DataUpdateCoordinator pattern for periodic polling
- homeassistant.helpers.device_registry - Device management across HA
- homeassistant.config_entries - Configuration flow and entry management

## Configuration

**Environment:**
- Credentials stored in Home Assistant config entry (email/password for Atmeex Cloud API)
- No `.env` file used (Home Assistant handles secrets natively)
- Configuration options:
  - `CONF_UPDATE_INTERVAL` (default: 30 seconds, range: 10-300)
  - `CONF_ENABLE_WEBSOCKET` (default: True)

**Build:**
- No build step required
- Component loaded directly from `custom_components/atmeex_cloud/`
- Entry point: `custom_components/atmeex_cloud/__init__.py`

## API & External Service Configuration

**Atmeex Cloud API:**
- Base URL: `https://api.iot.atmeex.com` (from `const.py`)
- Auth: Bearer token-based (OAuth 2.0 flow, obtained via email/password login)
- Timeout: 20 seconds (default, adjustable)
- Retry strategy: Exponential backoff (base 1.0s, max 32.0s, up to 3 attempts)
- Token refresh buffer: 60 seconds before expiration

**WebSocket:**
- Base URL: `wss://ws.iot.atmeex.com`
- Purpose: Real-time device state updates (optional, falls back to HTTP polling)
- Reconnect: Exponential backoff (min 1.0s, max 60.0s)
- Ping interval: 30 seconds

## Platform Requirements

**Development:**
- Python 3.12 with Home Assistant development environment
- Git for repository management
- Test environment: pytest with Home Assistant testing helpers

**Production:**
- Home Assistant installation (Docker, QEMU, native, etc.)
- Network access to `api.iot.atmeex.com` and `wss://ws.iot.atmeex.com`
- HTTPS/WSS capable (TLS 1.2+)

---

*Stack analysis: 2026-03-28*
