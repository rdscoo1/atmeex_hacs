# Atmeex HACS — Claude Working Guide

## What this project is

Home Assistant custom integration for Atmeex Cloud ventilation devices. It uses HTTP polling + optional WebSocket for real-time updates, and tracks optimistic pending state for set commands so the UI doesn't flicker.

Python 3.12, `asyncio`-native, ~3,961 LOC across 19 source files. No build step — HA loads it directly from `custom_components/atmeex_cloud/`.

---

## Running tests

The virtualenv is at `.venv/`. Always use it:

```bash
# Full suite (191 tests, should always be green)
.venv/bin/python -m pytest -q

# Single file
.venv/bin/python -m pytest tests/test_coordinator.py -v

# Single test
.venv/bin/python -m pytest tests/test_api.py::test_concurrent_401_sign_in_called_once -v

# Pattern match
.venv/bin/python -m pytest -k "reauth" -v

# With coverage
.venv/bin/python -m pytest --cov=custom_components/atmeex_cloud --cov-report=html
```

`pytest.ini` sets `asyncio_mode = auto`, so `@pytest.mark.asyncio` is optional but present by convention. `pythonpath = .` means `custom_components` is importable without install.

---

## Project structure

```
custom_components/atmeex_cloud/
├── __init__.py        # Entry point: async_setup_entry, async_unload_entry,
│                      #   refresh_device closure, WS message drain, state locks
├── coordinator.py     # AtmeexCoordinator — typed DataUpdateCoordinator subclass
│                      #   with _async_update_data, _fetch_devices_safely
├── api.py             # AtmeexApi HTTP client, AtmeexDevice + AtmeexState dataclasses,
│                      #   ApiError, retry logic, token management
├── websocket.py       # WebSocketManager — connection lifecycle, exponential backoff,
│                      #   token refresh on unauthorized
├── runtime.py         # AtmeexRuntimeData dataclass — holds api, coordinator, locks,
│                      #   pending_commands, device_locks, refresh_device callback
├── entity_base.py     # AtmeexEntityMixin — shared entity logic:
│                      #   _execute_command, _state_with_pending, device_info, available
├── helpers.py         # Unit conversion, state normalization,
│                      #   apply_condition_update / apply_settings_update (WS partials)
├── climate.py         # Climate entity (largest platform, ~448 lines)
├── fan.py             # Fan entity
├── sensor.py          # Sensor entities
├── binary_sensor.py   # Online binary sensor + diagnostics
├── switch.py          # Switch entities (auto-nanny, sleep mode)
├── select.py          # Select entity
├── config_flow.py     # UI config flow (email + password)
├── diagnostics.py     # HA diagnostics snapshot
├── const.py           # Constants, API URLs, defaults
└── logbook.py         # Custom logbook events

tests/
├── conftest.py        # DummyCoordinator, make_fake_api_class, make_hass_stub,
│                      #   make_entry_stub — shared across all tests
├── test_init.py       # Integration lifecycle (setup, unload, WS reauth, refresh_device)
├── test_api.py        # AtmeexApi HTTP client + auth + retry
├── test_coordinator.py # AtmeexCoordinator._async_update_data + race guards
├── test_websocket_manager.py # WebSocketManager connection/reconnect/auth
├── test_refresh_device.py    # Per-device targeted refresh + task coalescing
├── test_race_protection.py   # Concurrent command / WS / poll interleave
├── test_climate.py    # Climate entity state machine
└── test_*.py          # One file per platform
```

---

## Architecture in one paragraph

`async_setup_entry` in `__init__.py` wires everything together: it creates an `AtmeexCoordinator`, does the first HTTP poll, optionally connects a `WebSocketManager`, and stores all runtime state in `AtmeexRuntimeData` on the config entry. Entities read from `coordinator.data` (a `AtmeexCoordinatorData` TypedDict with `device_map`, `states`, `devices`). Set commands go through `AtmeexEntityMixin._execute_command` which acquires a per-device lock, calls the API, then triggers a targeted `refresh_device`. WS updates land in a bounded `deque(maxlen=500)`, drained serially under `state_update_lock`.

Full architecture: [.planning/codebase/ARCHITECTURE.md](.planning/codebase/ARCHITECTURE.md)

---

## Key concurrency facts (read before touching coordinator or __init__)

Three write paths compete on `coordinator.data`:

| Path | Lock held? | Protection mechanism |
|------|-----------|----------------------|
| `_async_update_data` (poll) | ✗ | Reads `poll_start_mono` before network; preserves fresher WS/refresh state post-poll |
| `_apply_websocket_message` (WS drain) | `state_update_lock` | Records `_ws_device_update_ts[did]` after writing |
| `_refresh_device_once` (targeted refresh) | `state_update_lock` | Records `_refresh_device_update_ts[did]` after writing |

The timestamp dicts (`_ws_device_update_ts`, `_refresh_device_update_ts`) on `AtmeexCoordinator` are the single mechanism preventing the poller from overwriting fresher state. If you add a new write path, record a timestamp and add a preservation block in `_async_update_data`.

---

## DummyCoordinator — keep it in sync

`tests/conftest.py` has a `DummyCoordinator` used by most integration tests. It must have every attribute that `__init__.py` accesses on the real coordinator. Currently required:

```python
self._ws_device_update_ts = {}
self._refresh_device_update_ts = {}
self.last_api_error = None
self.last_success_ts = None
```

`tests/test_refresh_device.py` has its own local `DummyCoordinator` with the same requirement. **Whenever you add an attribute to `AtmeexCoordinator.__init__`, add it to both DummyCoordinator definitions.**

---

## Test doubles quick reference

| Double | Where defined | Purpose |
|--------|--------------|---------|
| `DummyCoordinator` | `tests/conftest.py` (and `test_refresh_device.py`) | Coordinator stub without HA machinery |
| `FakeApi` / `make_fake_api_class()` | `tests/conftest.py` | Configurable API stub |
| `make_hass_stub()` | `tests/conftest.py` | Minimal `hass` SimpleNamespace |
| `make_entry_stub()` | `tests/conftest.py` | Minimal config entry |
| `FakeSession` | `tests/test_api.py` | aiohttp session with response queue |
| `_FakeWebSocket` / `_ScriptedWebSocket` | `tests/test_websocket_manager.py` | WS connection stubs |

---

## TDD workflow (enforced by superpowers skill)

1. **RED** — write the failing test, run it, confirm it fails for the right reason
2. **GREEN** — write the minimal production code to pass
3. **REFACTOR** — clean up while staying green

Never write production code before seeing a test fail. The `superpowers:test-driven-development` skill is always active.

---

## Coding conventions

- `from __future__ import annotations` first in every file
- `_LOGGER = logging.getLogger(__name__)` at module top
- Private methods: leading `_`; constants: `UPPER_SNAKE_CASE` in `const.py`
- Device IDs accept `int | str` everywhere; internally keyed as `str` (`str(device_id)`)
- `# noqa: BLE001` on intentional bare `except Exception` blocks
- Russian comments exist in older code — new code uses English

Full conventions: [.planning/codebase/CONVENTIONS.md](.planning/codebase/CONVENTIONS.md)

---

## Serena (semantic code navigation)

Serena is configured for this project at [.serena/](.serena/). Project name: `atmeex_hacs`, language: `python`.

**Always call `mcp__serena__initial_instructions` at the start of a session before doing any symbol lookups.**

Useful Serena tools:
- `mcp__serena__find_symbol` — find a class/function/variable by name
- `mcp__serena__find_referencing_symbols` — find all callers of a symbol
- `mcp__serena__get_symbols_overview` — list top-level symbols in a file
- `mcp__serena__find_implementations` — find concrete implementations of an abstract method

Use Serena for navigation (finding where things are defined or referenced) rather than `grep` when the call graph matters.

---

## Where codebase knowledge lives

| Document | What it covers |
|----------|---------------|
| [.planning/codebase/ARCHITECTURE.md](.planning/codebase/ARCHITECTURE.md) | Layers, data flow, key abstractions, entry points |
| [.planning/codebase/STACK.md](.planning/codebase/STACK.md) | Python version, dependencies, API URLs, WS config |
| [.planning/codebase/TESTING.md](.planning/codebase/TESTING.md) | Test patterns, mocking strategy, fixture examples |
| [.planning/codebase/CONVENTIONS.md](.planning/codebase/CONVENTIONS.md) | Naming, style, error handling, logging patterns |
| [.planning/codebase/CONCERNS.md](.planning/codebase/CONCERNS.md) | Known issues (all resolved as of 2026-03-28) |
| [.planning/codebase/STRUCTURE.md](.planning/codebase/STRUCTURE.md) | File-by-file breakdown |
| [.planning/codebase/INTEGRATIONS.md](.planning/codebase/INTEGRATIONS.md) | External API and HA integration details |

---

## Common gotchas

**`asyncio.wait_for` vs `asyncio.wait`** — `wait_for` blocks until the task finishes/cancels, which deadlocks if the task has a slow `finally`. Use `asyncio.wait({task}, timeout=N)` when you need a timeout that doesn't block on cleanup (e.g., `async_unload_entry`).

**`@cached_property` on entities** — kills live updates. Use plain `@property` for anything that reads from `coordinator.data`.

**Stale token in WS reconnect** — `_token_getter` is a callable, not a value. Always pass a lambda so reconnects pick up the refreshed token.

**`_request()` 401/403 retry path** — must acquire `self._lock` before calling `_sign_in()` and re-check `_token_is_valid()` after acquiring (double-checked locking), otherwise concurrent requests each fire their own login.

**Set commands don't retry** — `_put_params` calls `_request` directly (no `_with_retries`). This is intentional: retrying a non-idempotent PUT after a timeout can race with user input.
