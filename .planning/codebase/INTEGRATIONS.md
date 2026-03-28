# External Integrations

**Analysis Date:** 2026-03-28

## APIs & External Services

**Atmeex Cloud API (Primary):**
- **Service:** Atmeex Cloud REST API - Cloud-based control and monitoring of AirNanny/Atmeex ventilation devices
  - SDK/Client: `aiohttp.ClientSession` (generic async HTTP client)
  - Implementation: Custom `AtmeexApi` class in `api.py` wrapping aiohttp
  - Auth: OAuth 2.0 Bearer token (email/password login flow)
  - Base URL: `https://api.iot.atmeex.com` (from `const.API_BASE_URL`)
  - Timeout: 20 seconds default (`const.API_TIMEOUT_DEFAULT`)

**Atmeex Cloud WebSocket:**
- **Service:** Real-time device state updates via WebSocket
  - SDK/Client: `aiohttp` WebSocket support
  - Implementation: `WebSocketManager` class in `websocket.py`
  - Base URL: `wss://ws.iot.atmeex.com`
  - Purpose: Optional real-time updates to reduce polling frequency
  - Fallback: HTTP polling if WebSocket unavailable or disabled

## Data Storage

**Databases:**
- None - Integration does not use persistent database
- State storage: Home Assistant's built-in entity state storage (handled by HA core)

**File Storage:**
- Local filesystem only - No external file storage used
- Integration data stored in Home Assistant's standard config directories

**Caching:**
- In-memory caching via DataUpdateCoordinator pattern
- `AtmeexCoordinator` (in `coordinator.py`) manages periodic polling and state caching
- Default update interval: 30 seconds (configurable 10-300 seconds)
- Pending command tracking: Per-device command cache to prevent stale state overwrites (race condition protection)

## Authentication & Identity

**Auth Provider:**
- Custom Atmeex Cloud OAuth 2.0 via email/password
  - Implementation: `_sign_in()` method in `api.py`
  - Token type: Bearer token
  - Token expiration: Server-provided (checked with 60-second buffer before actual expiration)
  - Credentials stored in: Home Assistant config entry (encrypted by HA)
  - Env vars: Uses `CONF_EMAIL` and `CONF_PASSWORD` from `homeassistant.const`

**Re-authentication Flow:**
- Automatic token refresh when expired
- Manual re-auth flow available in UI when credentials change (config_flow.py)
- WebSocket auth failure triggers optional callback (`on_auth_failure`) for re-auth prompts

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)
- Error logging via Python standard `logging` module
- Logger name: `custom_components.atmeex_cloud`
- Registered in manifest for Home Assistant log viewer

**Logs:**
- Python standard logging to Home Assistant's unified logging system
- Log level configurable in HA UI
- Debug logs available for troubleshooting (race condition protection, API state, WebSocket flow)
- Logbook integration for automation event logging (`logbook.py`)

**Diagnostics:**
- Custom diagnostics platform (`diagnostics.py`) for device and API state inspection
- Diagnostic sensor entity (`sensor.atmeex_diagnostics`) with API statistics
- Logbook events for API errors and device updates

## CI/CD & Deployment

**Hosting:**
- GitHub repository: `https://github.com/rdscoo1/atmeex_hacs`
- Installation: HACS (Home Assistant Community Store) or manual copy to custom_components/

**CI Pipeline:**
- GitHub Actions workflows present (`.github/workflows/`)
- Validation: HACS validation badge in README indicates automated checks

## Environment Configuration

**Required env vars:**
- None stored as environment variables (Home Assistant config entry stores credentials)

**Secrets location:**
- Home Assistant config entry storage (encrypted by HA core)
- Secrets never exposed in logs (redacted by `async_redact_data()` in diagnostics)
- Configuration flow: User inputs email/password via UI, stored securely

## Webhooks & Callbacks

**Incoming:**
- None - Integration does not expose webhooks for external services to call

**Outgoing:**
- **WebSocket re-auth callback:** `on_auth_failure` callback from `WebSocketManager`
  - Triggered when: Server rejects token with 401/403
  - Purpose: Notify integration to stop reconnection attempts and trigger re-auth flow
  - Handler: `_on_ws_auth_failure()` in `__init__.py`

**Event Publishing:**
- **Logbook events:** Via Home Assistant event system
  - `EVENT_API_ERROR` - API failures (constant: `atmeex_cloud_api_error`)
  - `EVENT_DEVICE_UPDATED` - Device state changes from WebSocket (constant: `atmeex_cloud_device_updated`)
  - Min interval: 5 seconds to prevent log spam

## Rate Limiting & Throttling

**API Rate Limits:**
- Retry strategy: Exponential backoff with cap
  - Base delay: 1.0 second
  - Max delay: 32.0 seconds
  - Max attempts: 3 per request
- Polling interval: 10-300 seconds (user configurable)
- Token refresh: Proactive refresh 60 seconds before expiration (prevents mid-request failures)

**WebSocket Reconnection:**
- Reconnect delay: Exponential backoff
  - Min: 1.0 second
  - Max: 60.0 seconds
- Ping/pong mechanism: 30-second ping interval, 10-second timeout

---

*Integration audit: 2026-03-28*
