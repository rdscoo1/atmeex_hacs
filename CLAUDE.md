# Atmeex HACS — Claude Working Guide

## What this project is

Home Assistant custom integration for Atmeex Cloud ventilation devices. It uses HTTP polling + optional WebSocket for real-time updates, and tracks optimistic pending state for set commands so the UI doesn't flicker.

Python 3.12, `asyncio`-native, ~6,700 LOC across 21 source files. No build step — HA loads it directly from `custom_components/atmeex_cloud/`.

---

## Running tests

The virtualenv is at `.venv/`. Always use it:

```bash
# Full suite (~660 tests, should always be green)
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
│                      #   refresh_device closure, WS message drain, overflow resync,
│                      #   bounded runtime cleanup, device-removal gate
├── coordinator.py     # AtmeexCoordinator — authoritative inventory poll,
│                      #   detail hydration (≤3 workers), device retirement,
│                      #   inventory-age watchdog, throttled API-error events
├── state_store.py     # AtmeexStateStore — canonical copy-on-write snapshot with
│                      #   per-field revisions; merges poll/WS/refresh writes
├── command_executor.py # AtmeexCommandExecutor — per-device serialized commands,
│                      #   generation-tracked optimistic pending values (TTL 10s)
├── api.py             # AtmeexApi HTTP client, AtmeexDevice + AtmeexState dataclasses,
│                      #   typed sanitized errors, token recovery, bounded retries
├── websocket.py       # WebSocketManager — connection lifecycle, exponential backoff,
│                      #   token refresh on unauthorized, one-shot reauth
├── runtime.py         # AtmeexRuntimeData dataclass — api, coordinator, state_store,
│                      #   command_executor, task tracking, migration shims
├── entity_base.py     # AtmeexEntityMixin — _execute_command, _state_with_pending,
│                      #   device_info, available; dynamic entity discovery
├── helpers.py         # Unit conversion, state normalization, WS delta normalization
├── privacy.py         # anonymous_device_label — run-stable anonymized log labels
├── compat.py          # async_create_background_task across HA versions
├── climate.py         # Climate entity (largest platform, ~670 lines)
├── fan.py             # Fan entity
├── sensor.py          # Sensor entities (CO2, inlet temp, humidity, diagnostics)
├── binary_sensor.py   # Online + no-water binary sensors
├── switch.py          # Switch entities (auto-nanny, sleep mode, power)
├── select.py          # Select entities (humidification, breezer mode)
├── config_flow.py     # UI config flow (email + phone/SMS), reauth, reconfigure, options
├── diagnostics.py     # Whitelist-only HA diagnostics
├── const.py           # Constants, API URLs, defaults, INTEGRATION_VERSION
└── logbook.py         # Custom logbook event descriptions

tests/                          (~660 tests, one file per module/behavior)
├── conftest.py                 # DummyCoordinator, make_fake_api_class,
│                               #   make_hass_stub, make_entry_stub, make_runtime
├── test_setup.py               # Entry setup lifecycle, rollback, WS reauth wiring
├── test_unload.py              # Bounded unload semantics
├── test_state_store.py         # Per-field revision merge semantics
├── test_command_executor.py    # Command serialization + pending generations
├── test_inventory_*.py         # Inventory semantics, age watchdog, removal
├── test_detail_hydration.py    # List-item hydration against real HA hass fixture
├── test_api.py                 # HTTP client, auth recovery, retries, sanitization
├── test_websocket_manager.py   # WS connect/reconnect/auth
├── test_websocket_integration.py # WS drain → store → coordinator publication
├── test_refresh_device.py      # Targeted refresh, coalescing, failure recovery
├── test_race_protection.py     # Concurrent command / WS / poll interleave
├── test_manifest.py            # manifest.json ↔ const.INTEGRATION_VERSION guard
└── test_<platform>.py          # One file per entity platform + config flow
```

---

## Architecture in one paragraph

`async_setup_entry` in `__init__.py` wires everything together: it creates an `AtmeexStateStore`, an `AtmeexCoordinator` bound to it, does the first HTTP poll, optionally connects a `WebSocketManager`, and stores all runtime state (including an `AtmeexCommandExecutor`) in `AtmeexRuntimeData` on the config entry. Entities read from `coordinator.data` (an `AtmeexCoordinatorData` TypedDict with `device_map`, `states`, `devices`) — always a snapshot published by the state store. Set commands go through `AtmeexEntityMixin._execute_command` → `AtmeexCommandExecutor.async_execute`, which serializes per device, installs generation-tracked optimistic pending values, calls the API, then confirms via a targeted `refresh_device`. WS messages are queued (bounded at 500, overflow triggers an authoritative resync), drained in same-turn batches, and merged through `state_store.apply_websocket_delta`.

Full architecture: [.planning/codebase/ARCHITECTURE.md](.planning/codebase/ARCHITECTURE.md) *(predates the 2026-07 state-store rework — trust this file and the code over it where they disagree)*

---

## Key concurrency facts (read before touching coordinator, state_store, or __init__)

Three write paths converge on the store; there are **no locks around store writes** — every `apply_*` call is synchronous (no `await` inside), so the single event loop makes each merge atomic:

| Path | Store API | Stale-write protection |
|------|-----------|------------------------|
| `_async_update_data` (poll) | `apply_inventory(devices, baselines)` | Captures per-field revision baselines *before* the network call; skips any field whose revision moved during the poll |
| WS drain (`__init__.py`) | `apply_websocket_delta(...)` | Bumps per-field revisions on every accepted field (even same-value), so an older HTTP snapshot can't overwrite it |
| `_refresh_device_once` (targeted refresh) | `apply_refresh(device, baseline)` | Same baseline mechanism as the poll, captured per device |

The per-field revision map inside `AtmeexStateStore` is the single mechanism preventing a slower authoritative response from overwriting fresher pushed state. If you add a new write path: capture a baseline before any `await`, merge through the store, and never mutate `coordinator.data` directly.

Other invariants:

- A device is retired only after **two consecutive successful** polls report it absent (`_absence_counts`); a failed poll can never retire a device.
- `async_set_updated_data` (WS/refresh publications) reschedules HA's poll timer, so busy WS traffic would postpone polls forever — the **inventory watchdog** (`async_inventory_watchdog`) forces an authoritative refresh when the inventory is older than the update interval.
- Poll failures are typed: auth → `ConfigEntryAuthFailed`, transport/rate-limit/protocol → `UpdateFailed`. Never mask one as the other.

---

## DummyCoordinator — keep it in sync

`tests/conftest.py` has the single `DummyCoordinator` used by integration-level tests (other files import it from there). It must have every attribute that `__init__.py` and diagnostics access on the real coordinator (`last_api_error`, `last_success_ts`, `last_inventory_success_mono`, `avg_latency_ms`, `request_retries`, the `_api_error_*` throttle fields, `_last_detail_*`), and it borrows several real `AtmeexCoordinator` methods via `getattr`. **Whenever you add an attribute to `AtmeexCoordinator.__init__`, mirror it in `DummyCoordinator`.**

---

## Test doubles quick reference

| Double | Where defined | Purpose |
|--------|--------------|---------|
| `DummyCoordinator` | `tests/conftest.py` (imported elsewhere) | Coordinator stub without HA machinery |
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

> ⚠️ The `.planning/codebase/` documents below were written 2026-05-02 and predate the state-store/command-executor rework. Where they disagree with this file or the code, the code wins.

| Document | What it covers |
|----------|---------------|
| [.planning/codebase/ARCHITECTURE.md](.planning/codebase/ARCHITECTURE.md) | Layers, data flow, key abstractions, entry points |
| [.planning/codebase/STACK.md](.planning/codebase/STACK.md) | Python version, dependencies, API URLs, WS config |
| [.planning/codebase/TESTING.md](.planning/codebase/TESTING.md) | Test patterns, mocking strategy, fixture examples |
| [.planning/codebase/CONVENTIONS.md](.planning/codebase/CONVENTIONS.md) | Naming, style, error handling, logging patterns |
| [.planning/codebase/CONCERNS.md](.planning/codebase/CONCERNS.md) | Known issues ledger (see the 2026-07-17 addendum for current status) |
| [.planning/codebase/STRUCTURE.md](.planning/codebase/STRUCTURE.md) | File-by-file breakdown |
| [.planning/codebase/INTEGRATIONS.md](.planning/codebase/INTEGRATIONS.md) | External API and HA integration details |

---

## Common gotchas

**`asyncio.wait_for` vs `asyncio.wait`** — `wait_for` blocks until the task finishes/cancels, which deadlocks if the task has a slow `finally`. Use `asyncio.wait({task}, timeout=N)` when you need a timeout that doesn't block on cleanup (e.g., `async_unload_entry`).

**`@cached_property` on entities** — kills live updates. Use plain `@property` for anything that reads from `coordinator.data`.

**Stale token in WS reconnect** — `_token_getter` is a callable, not a value. Always pass a lambda so reconnects pick up the refreshed token.

**`_request()` 401 recovery path** — token recovery goes through `_recover_locked(snapshot, status)` under `self._lock`, keyed by the token *generation* captured before the failure. After acquiring the lock, a newer generation means someone else already recovered — return without a new sign-in. Never call `_sign_in()`/`_signin_refresh()` outside this mechanism.

**Set commands don't retry on transport errors** — non-GET requests in `_request` use `max_attempts = 2`, and the transport/rate-limit retry branch is GET-only. The second attempt exists solely for the single 401→recover→replay cycle. This is intentional: retrying a non-idempotent PUT after a timeout can race with user input.

**Version lives in two places** — `manifest.json` and `const.INTEGRATION_VERSION` must be bumped together; `tests/test_manifest.py` fails if they drift.

**Options flow needs the entry attached** — HA's `OptionsFlowManager` never sets `_config_entry` on the handler; `async_get_options_flow` must attach it (regression: broken Configure dialog). `test_options_flow_via_flow_manager_resolves_config_entry` drives the real manager to guard this.

**Device IDs in logs** — use `privacy.anonymous_device_label(device_id)` in log messages, never the raw cloud ID. Event-bus payloads (automations) and diagnostics counters are exempt; log text is not.
