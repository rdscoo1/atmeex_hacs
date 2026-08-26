# Atmeex HACS Test Suite — Refactor + Critical Gaps

## Context

The test suite has 263 tests across 19 files (~213 KB) and is fully green, but a recent QA review surfaced two distinct problems that need to be addressed in order:

1. **Structural redundancy.** `tests/test_init.py` is 2,077 LOC with **32 inline `FakeApi` and 32 inline `DummyCoordinator` classes**. Conftest already exports lighter versions, but they have drifted out of sync. The same pending-state and `ApiError → HomeAssistantError` patterns are re-tested across every entity platform. `tests/test_api_fallback_extra.py` duplicates `tests/test_api.py:254-295`. `tests/test_race_protection.py` contains 8 pure-unit tests that belong in `tests/test_runtime.py`.

2. **Behavioral gaps.** Diagnostics never asserts that `access_token` / `refresh_token` (listed in `TO_REDACT`) are actually redacted. Config-flow duplicate abort is mocked away rather than verified. The v0.9.3 power-switch fix is asserted only by "set_power was called" without proving other APIs are not called. No platform tests `async_added_to_hass` / `async_will_remove_from_hass`. `_fire_api_error_event` throttling, `WS-disconnect-raises-during-unload`, and real WS-vs-set interleaving are uncovered.

User chose **aggressive split** for layout and **all 9 critical gaps** for Phase 2.

Outcome: a smaller, focused-by-concern test suite (~210→235 tests after dedupe + new gap tests) with no per-platform mixin duplication and explicit coverage for the high-risk behaviors above.

---

## PHASE 1 — Refactor / delete redundant tests

Run `.venv/bin/python -m pytest -q` after every step. Suite must stay green at every checkpoint.

### 1.A — Upgrade `tests/conftest.py` (additive only)

- Extend `DummyCoordinator` with: `_ws_device_update_ts: dict = {}`, `_refresh_device_update_ts: dict = {}`, and an opt-in `setup_update(*, api, fire_logbook_event)` method that injects real `AtmeexCoordinator._fetch_devices_safely`, `_fire_api_error_event`, `_async_update_data` via `types.MethodType`. Pattern source: `tests/test_refresh_device.py:65-94`.
- Make `DummyCoordinator.__init__` tolerant of `data=`, `last_success_ts=`, `last_api_error=` kwargs so [tests/test_sensor.py:17-28](tests/test_sensor.py#L17-L28) can drop its local copy.
- Add `make_runtime(hass, entry, api, coordinator, *, websocket_manager=None) -> AtmeexRuntimeData` factory. Use the real dataclass from [custom_components/atmeex_cloud/runtime.py](custom_components/atmeex_cloud/runtime.py).

**Risk:** existing minimal `DummyCoordinator` callers must keep working. Mitigation: every new attribute has a safe default; `setup_update` is opt-in.

### 1.B — Delete `tests/test_api_fallback_extra.py`

Both tests duplicate scenarios already in [tests/test_api.py:254-295](tests/test_api.py#L254-L295).

### 1.C — Migrate `tests/test_sensor.py` + `tests/test_refresh_device.py` to conftest stubs

Replace local `DummyCoordinator` definitions with `from tests.conftest import DummyCoordinator`.

### 1.D — Create `tests/test_helpers.py` (NEW)

Move pure-function tests from `test_init.py`:
- `test_to_bool` (line 33)
- `test_normalize_device_state_basic` (line 37)
- `test_normalize_device_state_uses_settings_and_fan_fallback` (line 65)
- `test_resolve_update_interval_invalid_input_falls_back_to_default` (line 725)

Keep originals in `test_init.py` until step 1.H.

### 1.E — Create `tests/test_setup.py` (NEW)

Move `async_setup_entry` tests from `test_init.py`. Use `make_fake_api_class()` from conftest for happy paths; keep inline FakeApi subclasses only when a test needs custom side-effects (e.g. `set_devices_calls` counter, `login.side_effect=ApiError`):
- `test_async_setup_entry_happy_path` (95)
- `test_async_setup_entry_uses_options_update_interval` (261)
- `test_setup_entry_raises_auth_failed_on_invalid_credentials` (730)
- `test_setup_entry_raises_not_ready_on_non_auth_error` (758)
- `test_setup_entry_uses_fallback_devices_and_hydration_fallback` (786)
- `test_setup_entry_reauth_on_fallback_auth_error` (874)
- `test_setup_entry_registers_reload_listener` (938)
- `test_refresh_token_persistence_failure_is_logged_not_raised` (1011)
- `test_setup_entry_websocket_skipped_without_ws_connect` (1099)
- `test_setup_entry_websocket_skipped_without_token` (1167)

### 1.F — Create `tests/test_unload.py` (NEW)

Move from `test_init.py`:
- `test_async_unload_entry_clears_data` (197)
- `test_unload_entry_hung_task_does_not_block_unload` (215)
- `test_async_remove_config_entry_device_drops_per_device_state` (2022)
- `test_async_remove_config_entry_device_handles_missing_runtime` (2052)
- `test_async_remove_config_entry_device_unknown_device_is_noop` (2064)

This file is also where Phase 2 task #6 lands.

### 1.G — Create `tests/test_websocket_integration.py` (NEW)

Move from `test_init.py`:
- `test_websocket_batch_message_updates_coordinator_once` (571)
- `test_setup_entry_websocket_auth_failure_starts_reauth` (1236)
- `test_ws_reauth_can_trigger_again_after_successful_reconnect` (1340)
- `test_websocket_settings_message_updates_state` (1476)
- `test_websocket_logbook_device_events_are_throttled` (1614)
- `test_refresh_device_coalesces_parallel_requests` (357)
- `test_refresh_device_hung_task_times_out_for_second_caller` (463)
- All `test_apply_condition_update_*` / `test_apply_settings_update_*` (1873-2018)

### 1.H — Delete `tests/test_init.py`

After 1.D-1.G everything is migrated. Verify with full suite run; expected count = 263 − 2 (`test_api_fallback_extra.py` deletion) before Phase 1.I/J/K reductions.

### 1.I — Create `tests/test_entity_base.py` (NEW), then delete platform duplicates

Two parametrized tests in the new file:

1. `test_pending_state_via_mixin` — covers climate ([test_climate.py:341](tests/test_climate.py#L341)), switch ([test_switch.py:127](tests/test_switch.py#L127), [134](tests/test_switch.py#L134), [188](tests/test_switch.py#L188)), select pending tests.
2. `test_api_error_translates_to_home_assistant_error` — covers climate ([test_climate.py:387](tests/test_climate.py#L387), [398](tests/test_climate.py#L398), [409](tests/test_climate.py#L409)), [test_fan.py:60](tests/test_fan.py#L60), [test_switch.py:78](tests/test_switch.py#L78), [105](tests/test_switch.py#L105), [211](tests/test_switch.py#L211), [test_select.py:80](tests/test_select.py#L80), [117](tests/test_select.py#L117).

Then **delete** the equivalent tests from each per-platform file. Keep platform-specific behavior tests (HVAC mapping, fan signature, switch v0.9.3, select truth tables).

### 1.J — Parametrize HVAC triplet in [tests/test_climate.py:178-205](tests/test_climate.py#L178-L205)

Replace 3 sibling tests with one `@pytest.mark.parametrize` matrix over `(hvac_mode, initial_pwr_on, expected_call)`.

### 1.K — Reorganize `tests/test_race_protection.py`

Delete (already covered by [tests/test_runtime.py](tests/test_runtime.py)): lines 60, 71, 80, 91, 102, 122, 137, 150, 157, 171, 188.

Keep: line 209 (`test_set_percentage_records_pending`), line 247 (`test_lock_serializes_operations` — the genuine race).

Rewrite line 224 (`test_rapid_changes_use_latest_value`) as part of Phase 2 task #7 (real WS-vs-set interleaving).

**Phase 1 verification:** `.venv/bin/python -m pytest -q` — full green. Expected ~210-220 tests.

---

## PHASE 2 — Fill all 9 critical gaps

### 2.1 — Diagnostics token redaction → [tests/test_diagnostics.py](tests/test_diagnostics.py)

Set `runtime.api._token = "SECRET_ACCESS_TOKEN_SENTINEL"`, `runtime.api.refresh_token = "SECRET_REFRESH_TOKEN_SENTINEL"`. Assert neither sentinel appears in `json.dumps(diag)`. Source: [diagnostics.py:17-25](custom_components/atmeex_cloud/diagnostics.py#L17-L25) `TO_REDACT` already lists both.

### 2.2 — Switch v0.9.3 power isolation → [tests/test_switch.py:195](tests/test_switch.py#L195)

Add to `test_power_switch_turn_on_calls_api`:
```
api.set_auto_mode.assert_not_awaited()
api.set_sleep_mode.assert_not_awaited()
api.set_breezer_mode.assert_not_awaited()
```
Same for `test_power_switch_turn_off_calls_api` (line 203). Verify `_make_power_switch` exposes those AsyncMocks; augment if not.

### 2.3 — Phone normalization → [tests/test_config_flow.py](tests/test_config_flow.py)

New parametrized test of `_clean_phone` from [config_flow.py](custom_components/atmeex_cloud/config_flow.py). Inputs: `"+7 (495) 123-45-67"`, `"84951234567"`, `"+7-495-123-45-67"`, leading/trailing whitespace, parens.

### 2.4 — Config flow duplicate abort → [tests/test_config_flow.py](tests/test_config_flow.py)

New test that does NOT mock `_abort_if_unique_id_configured`. Uses real `hass` fixture (from `pytest_homeassistant_custom_component`) with a pre-registered `MockConfigEntry(unique_id=_email_unique_id("user@example.com"))`. Re-submits same email; asserts `result["type"] == FlowResultType.ABORT, reason == "already_configured"`. Cannot use `_make_flow()` helper at line 27 (sets `flow.hass = object()`).

### 2.5 — `_fire_api_error_event` throttling → [tests/test_coordinator.py](tests/test_coordinator.py)

Wire conftest `DummyCoordinator` with `setup_update` and a `MagicMock fire_logbook_event`. Call `coordinator._fire_api_error_event(...)` twice within `WS_LOGBOOK_MIN_INTERVAL_SEC` (from [const.py](custom_components/atmeex_cloud/const.py)); assert mock called once. Optionally monkeypatch `time.monotonic` to advance, then call again, assert second event fires.

### 2.6 — WS disconnect raises during unload → `tests/test_unload.py` (created in 1.F)

Build runtime via `make_runtime` with `websocket_manager.disconnect = AsyncMock(side_effect=RuntimeError("boom"))`. Run `async_unload_entry` under `caplog.at_level(logging.ERROR)`. Assert returns `True`; error logged.

### 2.7 — Real WS-vs-set interleaving → [tests/test_race_protection.py](tests/test_race_protection.py)

Replaces the rewritten line 224 test:
- Make `api.set_fan_speed` slow (`await asyncio.sleep(0.05)`).
- `task = asyncio.create_task(fan.async_set_percentage(75))` (speed 5).
- Concurrently call `apply_settings_update` for same device with `fan_speed=3` (stale).
- Await task; assert `fan.percentage` reflects the pending value (5) until API confirms.

### 2.8 — Climate `async_setup_entry` + service registration → [tests/test_climate.py](tests/test_climate.py)

Use real `hass` fixture + `MockConfigEntry` + `make_runtime`. Call [climate.async_setup_entry()](custom_components/atmeex_cloud/climate.py). Read [climate.py:84-93](custom_components/atmeex_cloud/climate.py#L84-L93) for exact service names; assert `hass.services.has_service(DOMAIN, name)` for each (e.g. `set_breezer_mode`, `set_humidifier_stage`).

### 2.9 — Entity lifecycle → `tests/test_entity_base.py` (created in 1.I)

Parametrized over a small per-platform factory (climate, fan, switch, select, sensor, binary_sensor). For each entity:
- Mock `coordinator.async_add_listener` to return a remove callback mock.
- `await ent.async_added_to_hass()` → assert `async_add_listener` called once.
- `await ent.async_will_remove_from_hass()` → assert remove callback called once.

**Phase 2 verification:**
- `.venv/bin/python -m pytest -q` — full green.
- `.venv/bin/python -m pytest -q --cov=custom_components.atmeex_cloud --cov-report=term-missing` — confirms gap-fill tests reach previously uncovered branches in `diagnostics.py`, `coordinator.py` throttle path, `config_flow.py:_clean_phone`.

---

## Critical files

- [tests/conftest.py](tests/conftest.py) — upgrade DummyCoordinator, add make_runtime
- [tests/test_init.py](tests/test_init.py) — split into 4 new files, then deleted
- [tests/test_race_protection.py](tests/test_race_protection.py) — heavily reduced + WS-vs-set test
- [tests/test_refresh_device.py](tests/test_refresh_device.py) — deduplicate against conftest
- [tests/test_sensor.py](tests/test_sensor.py) — drop local DummyCoordinator
- [tests/test_climate.py](tests/test_climate.py) — parametrize HVAC triplet, drop mixin tests, add setup_entry test
- [tests/test_fan.py](tests/test_fan.py), [tests/test_switch.py](tests/test_switch.py), [tests/test_select.py](tests/test_select.py) — drop mixin duplicates; switch gets v0.9.3 hardening
- [tests/test_config_flow.py](tests/test_config_flow.py) — duplicate abort + phone normalization
- [tests/test_diagnostics.py](tests/test_diagnostics.py) — token redaction
- [tests/test_coordinator.py](tests/test_coordinator.py) — throttle test
- New: `tests/test_helpers.py`, `tests/test_setup.py`, `tests/test_unload.py`, `tests/test_websocket_integration.py`, `tests/test_entity_base.py`
- Delete: `tests/test_api_fallback_extra.py`, `tests/test_init.py`

## Existing utilities to reuse

- `make_fake_api_class()` in [tests/conftest.py](tests/conftest.py) — basic FakeApi factory.
- `make_hass_stub()`, `make_entry_stub()` in [tests/conftest.py](tests/conftest.py).
- `setup_update()` pattern in [tests/test_refresh_device.py:75-83](tests/test_refresh_device.py#L75-L83) — promote to conftest.
- `pytest_homeassistant_custom_component.common.MockConfigEntry` — for tests #2.4 and #2.8 that need a real config entry.
- Per-platform `_make_*_entity()` helpers — keep; reused by `test_entity_base.py` lifecycle test.

## Verification (end-to-end)

After Phase 1:
```
.venv/bin/python -m pytest -q                     # full suite green
.venv/bin/python -m pytest -q --collect-only | tail -1   # ~210-220 tests
```

After Phase 2:
```
.venv/bin/python -m pytest -q                     # full suite green, ~225-235 tests
.venv/bin/python -m pytest -q --cov=custom_components.atmeex_cloud --cov-report=term-missing
```

Spot checks:
```
.venv/bin/python -m pytest -q tests/test_entity_base.py -v   # mixin contracts
.venv/bin/python -m pytest -q tests/test_diagnostics.py::test_redacts_tokens -v
.venv/bin/python -m pytest -q -k "duplicate" tests/test_config_flow.py
.venv/bin/python -m pytest -q tests/test_race_protection.py -v   # 3 real-race tests only
```

## Risk register

| Risk | Mitigation |
|---|---|
| `DummyCoordinator` shape change breaks minimal callers | All new attrs have safe defaults; `setup_update` opt-in. Verified by full suite run after step 1.A. |
| Specialized FakeApi variants (gates, side-effect counters) regress when consolidated | Don't consolidate them — keep inline subclasses extending the conftest base. |
| Real-interleaving test #2.7 flakes on CI | Use `asyncio.sleep(0)` to yield event loop control; rely on scheduling order, not wall time. |
| Config-flow duplicate test (#2.4) needs real `hass` | Use `pytest_homeassistant_custom_component` `hass` fixture; do not use the homemade `_make_flow()`. |
| Mid-stream `test_init.py` deletion leaves dangling imports | None: only `test_init.py` itself imports `atmeex_init` as `__init__` module. Confirmed safe. |
| Climate `async_setup_entry` test (#2.8) requires service-registration side effects on real hass | Use `MockConfigEntry` + real hass fixture; assert via `hass.services.has_service`. |
