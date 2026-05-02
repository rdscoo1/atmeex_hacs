# External Integrations

**Analysis Date:** 2026-05-02

## APIs & External Services

**Atmeex Cloud API (Primary):**
- **Service:** Atmeex Cloud REST API — Cloud-based control and monitoring of AirNanny/Atmeex ventilation devices
  - SDK/Client: `aiohttp.ClientSession` (generic async HTTP client)
  - Implementation: Custom `AtmeexApi` class in `custom_components/atmeex_cloud/api.py` wrapping aiohttp
  - Auth: OAuth 2.0 Bearer token (email/password login, with refresh-token path)
  - Base URL: `https://api.iot.atmeex.com` (from `const.API_BASE_URL`)
  - Timeout: 20 seconds default (`API_TIMEOUT_DEFAULT` in `const.py`)
  - Key endpoints:
    - `POST /auth/signin` — login (grant_type: `basic` or `refresh_token`)
    - `GET /devices` — list all devices
    - `GET /devices/{id}` — get single device with full condition+settings
    - `PUT /devices/{id}/params` — update device parameters (power, fan speed, temperature, mode, humidifier stage)

**Atmeex Cloud WebSocket:**
- **Service:** Real-time device state updates via WebSocket
  - SDK/Client: `aiohttp` WebSocket support (`ClientSession.ws_connect`)
  - Implementation: `WebSocketManager` class in `custom_components/atmeex_cloud/websocket.py`
  - Base URL: `wss://ws.iot.atmeex.com`
  - Purpose: Optional real-time push updates to reduce polling frequency
  - Message types handled: `condition` (sensor readings), `settings` (user-set parameters), `unauthorized`
  - Fallback: HTTP polling if WebSocket unavailable or disabled via `CONF_ENABLE_WEBSOCKET`

## Data Storage

**Databases:**
- None — integration does not use a persistent database
- State storage: Home Assistant's built-in entity state storage (handled by HA core)

**File Storage:**
- Local filesystem only — no external file storage used
- Integration data stored in Home Assistant's standard config directories
- Refresh token persisted across restarts by writing back to the config entry data via `hass.config_entries.async_update_entry`

**Caching:**
- In-memory caching via `DataUpdateCoordinator` pattern
- `AtmeexCoordinator` (in `custom_components/atmeex_cloud/coordinator.py`) manages periodic polling and state caching
- Default update interval: 30 seconds (configurable 10–300 seconds via `CONF_UPDATE_INTERVAL`)
- Pending command tracking: Per-device pending command cache in `AtmeexRuntimeData` (`runtime.py`) to prevent stale state overwrites during the set→poll race
- Stale-state protection: `AtmeexCoordinator._ws_device_update_ts` and `_refresh_device_update_ts` (both `dict[str, float]`) record per-device monotonic timestamps so that `_async_update_data` does not overwrite a fresher WebSocket or targeted-refresh state during a concurrent poll
- WebSocket message queue: `collections.deque(maxlen=500)` in `__init__.py` serializes message processing through a single drain task

## Authentication & Identity

**Auth Provider:**
- Custom Atmeex Cloud OAuth 2.0 via email/password
  - Implementation: `_sign_in()` and `_signin_refresh()` methods in `api.py`
  - Token type: Bearer token
  - Token expiration: Server-provided `expires_in` (checked with 60-second buffer before actual expiration via `TOKEN_REFRESH_BUFFER_SEC`)
  - Credentials stored in: Home Assistant config entry (encrypted by HA)
  - Env vars: Uses `CONF_EMAIL` and `CONF_PASSWORD` from `homeassistant.const`
  - Refresh token: Persisted in config entry data as `"refresh_token"` key; attempted first on token expiry before falling back to full login

**Re-authentication Flow:**
- Automatic token refresh when expired (refresh token tried first, then full login)
- Manual re-auth flow available in UI when credentials change (`config_flow.py`)
- WebSocket auth failure (HTTP 401/403 handshake, or 5 consecutive `{"type":"unauthorized"}` messages) triggers `_on_ws_auth_failure()` in `__init__.py`
- WS reauth prompts are rate-limited to at most once every 5 minutes (`_WS_REAUTH_COOLDOWN_SEC = 300.0`)

## Monitoring & Observability

**Error Tracking:**
- None (no external error tracking service)
- Error logging via Python standard `logging` module
- Logger name: `custom_components.atmeex_cloud`
- Registered in `manifest.json` `loggers` field for Home Assistant log viewer

**Logs:**
- Python standard logging to Home Assistant's unified logging system
- Log level configurable in HA UI
- Debug logs available for pending command lifecycle, WS state merge, poll-vs-WS timestamp comparisons, and API retry attempts
- Logbook integration for automation event logging (`logbook.py`)

**Diagnostics:**
- Custom diagnostics platform (`custom_components/atmeex_cloud/diagnostics.py`) for device and API state inspection
- Diagnostic sensor entity (`sensor.atmeex_diagnostics`) with API statistics
- `AtmeexOnlineSensor` (`binary_sensor.py`) carries `EntityCategory.DIAGNOSTIC` and implements a stale-availability check: marks itself unavailable if the coordinator has been silent for more than 3× the configured update interval
- Logbook events for API errors and device updates

## CI/CD & Deployment

**Hosting:**
- GitHub repository: `https://github.com/rdscoo1/atmeex_hacs`
- Installation: HACS (Home Assistant Community Store) or manual copy to `custom_components/`

**CI Pipeline:**
- GitHub Actions workflow: `.github/workflows/validate.yml`
- HACS validation badge in README indicates automated compatibility checks

## Environment Configuration

**Required env vars:**
- None stored as environment variables (Home Assistant config entry stores credentials)

**Secrets location:**
- Home Assistant config entry storage (encrypted by HA core)
- Secrets never exposed in logs (redacted by `async_redact_data()` in `diagnostics.py`)
- Configuration flow: User inputs email/password via UI, stored securely

## Webhooks & Callbacks

**Incoming:**
- None — integration does not expose webhooks for external services to call

**Outgoing:**
- **WebSocket re-auth callback:** `on_auth_failure` callback from `WebSocketManager`
  - Triggered when: HTTP 401/403 on WS handshake, or 5 consecutive `"unauthorized"` message-level rejections
  - Purpose: Notify integration to stop reconnection attempts and trigger re-auth flow
  - Handler: `_on_ws_auth_failure()` closure in `__init__.py`
  - Rate-limiting: `_WS_REAUTH_COOLDOWN_SEC = 300.0` prevents prompt spam if the connection is noisy

**Event Publishing:**
- **Logbook events:** Via Home Assistant event system
  - `EVENT_API_ERROR` (`"atmeex_cloud_api_error"`) — API failures
  - `EVENT_DEVICE_UPDATED` (`"atmeex_cloud_device_updated"`) — device state changes from WebSocket or targeted refresh
  - Min interval: 5 seconds (`WS_LOGBOOK_MIN_INTERVAL_SEC`) to prevent log spam; suppressed events are batched into the next fired payload

## Rate Limiting & Throttling

**API Rate Limits:**
- Retry strategy: Exponential backoff with cap — network errors only
  - Base delay: 1.0 second (`RETRY_BASE_DELAY_SEC`)
  - Max delay: 32.0 seconds (`RETRY_MAX_DELAY_SEC`)
  - Max attempts: 3 (`RETRY_MAX_ATTEMPTS`)
  - PUT (set-command) calls are intentionally **not retried** to avoid double-applying device commands
- Polling interval: 10–300 seconds (user configurable via `CONF_UPDATE_INTERVAL`)
- Token refresh: Proactive refresh 60 seconds before expiration (prevents mid-request failures)

**WebSocket Reconnection:**
- Reconnect delay: Exponential backoff
  - Min: 1.0 second (`WS_RECONNECT_DELAY_MIN`)
  - Max: 60.0 seconds (`WS_RECONNECT_DELAY_MAX`)
  - Backoff **unconditionally resets to min** on each successful `_connect_once()` call (both initial connect and reconnect paths)
- Ping/pong mechanism: 30-second ping interval (`WS_PING_INTERVAL`), 10-second timeout (`WS_PING_TIMEOUT`)
- Auth failure threshold: `WS_MAX_UNAUTHORIZED_BEFORE_REAUTH = 5` consecutive message-level rejections before halting reconnects

---

*Integration audit: 2026-05-02*
