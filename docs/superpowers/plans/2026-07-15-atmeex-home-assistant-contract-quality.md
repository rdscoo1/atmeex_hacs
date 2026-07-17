# Atmeex Home Assistant Contract and Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the hardening program with a version-compatible Home Assistant contract, privacy-safe support surfaces, low-churn entities/events, and release gates that preserve every existing entity ID, service, option, translation, and automation input.

**Architecture:** A feature-detected `compat.py` isolates the small Home Assistant 2024.8/current API differences. Config flows explicitly own reloads, entity services register once through Home Assistant's supported entity-platform helper, diagnostics are assembled from a safe whitelist, and CI tests the minimum and current supported environments independently. Authentication failures continue through Home Assistant reauthentication; transient cloud failures remain coordinator availability events, so this plan intentionally does not add repairs or issue-registry entries.

**Tech Stack:** Python 3.12/3.14, Home Assistant 2024.8.0 and 2026.7.2, voluptuous, pytest, pytest-asyncio, pytest-cov/coverage, hassfest, HACS validation, GitHub Actions.

---

## File Responsibility Map

### Production and metadata

- Create: `custom_components/atmeex_cloud/compat.py` — options-entry access, explicit update/reload, no-reload persistence, and task creation selected by feature detection.
- Create: `custom_components/atmeex_cloud/privacy.py` — stable-within-run anonymized device labels and safe error fields.
- Modify: `custom_components/atmeex_cloud/__init__.py` — persist rotated refresh tokens without reload and remove the generic update listener.
- Modify: `custom_components/atmeex_cloud/config_flow.py` — real options manager, pre-network duplicate checks, phone token validation, reauth, and identity-preserving reconfigure.
- Modify: `custom_components/atmeex_cloud/entity_base.py` — translated Home Assistant/service-validation errors.
- Modify: `custom_components/atmeex_cloud/{climate,fan,select,switch,sensor,binary_sensor}.py` — explicit `PARALLEL_UPDATES = 0`; climate registers each entity service once through `EntityPlatform`.
- Modify: `custom_components/atmeex_cloud/diagnostics.py` — whitelist-only entry/device diagnostics.
- Modify: `custom_components/atmeex_cloud/logbook.py` — suppress routine technical rendering while retaining event identifiers and payload keys.
- Modify: `custom_components/atmeex_cloud/sensor.py` — newly created diagnostics entities disabled by default and no timing-only writes.
- Modify: `custom_components/atmeex_cloud/{api,coordinator,websocket}.py` — operation/count/anonymized-label logging only.
- Modify: `custom_components/atmeex_cloud/runtime.py` — concrete runtime types and a 2024.8-safe config-entry alias.
- Modify: `custom_components/atmeex_cloud/manifest.json` — remove Core-provided `aiohttp` requirement.
- Modify: `custom_components/atmeex_cloud/{services.yaml,strings.json,translations/en.json,translations/ru.json}` — preserve schemas and add flow/exception translations.
- Modify: `hacs.json` — retain `homeassistant: 2024.8.0`.
- Modify: `README.md`, `README.en.md` — final behavior, privacy, troubleshooting, and migration-free upgrade contract.

### Quality infrastructure and tests

- Create: `tests/test_compat.py`, `tests/test_contract.py`, `tests/test_privacy.py`, `tests/test_quality_gate.py`.
- Modify: `tests/test_config_flow.py`, `tests/test_setup.py`, `tests/test_diagnostics.py`, `tests/test_logbook.py`, and platform tests.
- Create: `requirements-ci-ha-2024.8.txt`, `requirements-ci-current.txt`, `.coveragerc`, `scripts/check_module_coverage.py`.
- Modify: `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.github/workflows/validate.yml`.
- Delete: `.coverage` — tracked runtime artifact; future databases are ignored.

## Immutable Public Contract

- Unique-ID suffixes remain `climate`, `fan`, `hum_mode`, `breezer_mode`, `auto_nanny`, `sleep_mode`, `power`, `co2`, `inlet_temp`, `humidity`, `online`, and `no_water`.
- Services remain `atmeex_cloud.set_breezer_mode(mode)` with the four current string values and `atmeex_cloud.set_humidifier_stage(stage)` with integer `0..3`, using the existing climate target selector.
- Options remain `update_interval`, `enable_websocket`, and `enable_co2`; events remain `atmeex_cloud_api_error` and `atmeex_cloud_device_updated` with current automation payload keys.
- No config-entry or entity-registry migration is introduced.

## Execution Gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require the
entire repository to pass. Tasks 1–6 may retain only the previously recorded
pytest-asyncio loop-scope notice; Task 7 owns its removal and then requires zero
runtime warnings and zero repository-owned deprecation warnings. A focused
GREEN command never replaces this full-suite gate. Every commit block below is
conditional on an immediately preceding full-suite run, even when the focused
command is the one printed inside the task.

### Task 1: Isolate Version Differences and Make Reload Ownership Explicit

**Files:**
- Create: `custom_components/atmeex_cloud/compat.py`
- Create: `tests/test_compat.py`
- Modify: `custom_components/atmeex_cloud/config_flow.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`

- [ ] **Step 1: Add complete failing compatibility/reload tests**

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.compat import (
    async_create_background_task,
    async_update_entry_and_reload,
    options_flow_config_entry,
)


@pytest.mark.asyncio
async def test_real_options_manager_updates_and_reloads_once(
    hass, enable_custom_integrations, monkeypatch
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, options={"update_interval": 30})
    entry.add_to_hass(hass)
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "update_interval": 45,
            "enable_websocket": False,
            "enable_co2": True,
        },
    )
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "options_updated"
    assert entry.options["update_interval"] == 45
    reload_entry.assert_awaited_once_with(entry.entry_id)


def test_refresh_token_persistence_updates_without_reload() -> None:
    from custom_components.atmeex_cloud import _make_refresh_token_callback

    entry = SimpleNamespace(data={"refresh_token": "old"})
    config_entries = SimpleNamespace(
        async_update_entry=MagicMock(), async_reload=AsyncMock()
    )
    callback = _make_refresh_token_callback(
        SimpleNamespace(config_entries=config_entries), entry
    )
    callback("rotated")
    config_entries.async_update_entry.assert_called_once_with(
        entry, data={"refresh_token": "rotated"}
    )
    config_entries.async_reload.assert_not_called()


def test_compat_feature_detection_covers_modern_and_legacy_paths() -> None:
    entry = MockConfigEntry(domain=DOMAIN)

    class ModernFlow:
        @property
        def config_entry(self):
            return entry

    assert options_flow_config_entry(ModernFlow()) is entry
    assert options_flow_config_entry(SimpleNamespace(_config_entry=entry)) is entry
    with pytest.raises(RuntimeError, match="no config entry"):
        options_flow_config_entry(SimpleNamespace())

    task = object()
    modern = SimpleNamespace(
        async_create_background_task=MagicMock(return_value=task),
        async_create_task=MagicMock(),
    )
    modern_coro = AsyncMock()()
    assert async_create_background_task(modern, modern_coro, "modern") is task
    modern.async_create_background_task.assert_called_once_with(modern_coro, "modern")
    modern_coro.close()

    legacy = SimpleNamespace(async_create_task=MagicMock(return_value=task))
    legacy_coro = AsyncMock()()
    assert async_create_background_task(legacy, legacy_coro, "legacy") is task
    legacy.async_create_task.assert_called_once_with(legacy_coro, name="legacy")
    legacy_coro.close()


@pytest.mark.asyncio
async def test_explicit_data_update_reloads_once() -> None:
    entry = SimpleNamespace(entry_id="entry-1")
    config_entries = SimpleNamespace(
        async_update_entry=MagicMock(), async_reload=AsyncMock()
    )
    hass = SimpleNamespace(config_entries=config_entries)
    await async_update_entry_and_reload(hass, entry, data={"key": "value"})
    config_entries.async_update_entry.assert_called_once_with(
        entry, data={"key": "value"}
    )
    config_entries.async_reload.assert_awaited_once_with("entry-1")
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_compat.py -q`

Expected: FAIL during collection because `compat.py` and `_make_refresh_token_callback` do not exist; after the module is created, the old options flow still fails the one-reload assertion.

- [ ] **Step 3: Implement the complete compatibility surface**

```python
# custom_components/atmeex_cloud/compat.py
from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Mapping
from typing import Any, TypeVar, cast

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import HomeAssistant

_T = TypeVar("_T")


def options_flow_config_entry(flow: OptionsFlow) -> ConfigEntry:
    descriptor = getattr(type(flow), "config_entry", None)
    if isinstance(descriptor, property):
        try:
            return cast(ConfigEntry, descriptor.__get__(flow, type(flow)))
        except AttributeError:
            pass
    entry = getattr(flow, "_config_entry", None)
    if isinstance(entry, ConfigEntry):
        return entry
    raise RuntimeError("Options flow has no config entry")


def update_entry_data_without_reload(
    hass: HomeAssistant, entry: ConfigEntry, updates: Mapping[str, Any]
) -> None:
    hass.config_entries.async_update_entry(entry, data={**entry.data, **updates})


async def async_update_entry_and_reload(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    data: Mapping[str, Any] | None = None,
    options: Mapping[str, Any] | None = None,
) -> None:
    updates: dict[str, Any] = {}
    if data is not None:
        updates["data"] = data
    if options is not None:
        updates["options"] = options
    hass.config_entries.async_update_entry(entry, **updates)
    await hass.config_entries.async_reload(entry.entry_id)


def async_create_background_task(
    hass: HomeAssistant, coro: Coroutine[Any, Any, _T], name: str
) -> asyncio.Task[_T]:
    creator = getattr(hass, "async_create_background_task", None)
    if callable(creator):
        return cast(asyncio.Task[_T], creator(coro, name))
    return cast(asyncio.Task[_T], hass.async_create_task(coro, name=name))
```

In the options flow, set `entry = options_flow_config_entry(self)`, call `await async_update_entry_and_reload(self.hass, entry, options=normalized_options)`, then return `self.async_abort(reason="options_updated")`. Remove the custom `config_entry` property and the generic `entry.add_update_listener` from setup.

Add the no-reload API callback in `__init__.py`:

```python
def _make_refresh_token_callback(hass: HomeAssistant, entry: ConfigEntry):
    def persist(refresh_token: str) -> None:
        update_entry_data_without_reload(
            hass, entry, {"refresh_token": refresh_token}
        )
    return persist
```

Construct the client as `AtmeexApi(session, on_refresh_token_changed=_make_refresh_token_callback(hass, entry))`; do not reload because the API already owns the live token.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_compat.py tests/test_setup.py -q`

Expected: PASS; `tests/test_compat.py` reports `4 passed`, both feature-detection branches execute, and each explicit update reloads exactly once.

```bash
git add custom_components/atmeex_cloud/compat.py custom_components/atmeex_cloud/config_flow.py custom_components/atmeex_cloud/__init__.py tests/test_compat.py tests/test_setup.py
git commit -m "fix: make config entry reloads explicit"
```

### Task 2: Validate Identity Before Side Effects and Add Reconfigure

**Files:**
- Modify: `custom_components/atmeex_cloud/config_flow.py`
- Modify: `tests/test_config_flow.py`
- Modify: `custom_components/atmeex_cloud/{strings.json,translations/en.json,translations/ru.json}`

- [ ] **Step 1: Add complete RED cases**

```python
@pytest.mark.asyncio
async def test_duplicate_phone_aborts_before_sms(hass, enable_custom_integrations):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="+79991234567")
    entry.add_to_hass(hass)
    with patch("custom_components.atmeex_cloud.config_flow.AtmeexApi") as api_cls:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": "phone"},
            data={CONF_PHONE: "+7 (999) 123-45-67"},
        )
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    api_cls.assert_not_called()


@pytest.mark.asyncio
async def test_phone_code_requires_refresh_token():
    flow = _make_flow()
    flow._pending_phone = "+79991234567"
    with patch("custom_components.atmeex_cloud.config_flow.AtmeexApi") as api_cls, patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession",
        return_value=object(),
    ):
        api_cls.return_value.async_init = AsyncMock()
        api_cls.return_value.login_phone = AsyncMock()
        type(api_cls.return_value).refresh_token = property(lambda self: None)
        result = await flow.async_step_phone_code({CONF_PHONE_CODE: "1234"})
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "missing_refresh_token"


@pytest.mark.asyncio
async def test_reconfigure_rejects_account_switch_before_login():
    flow = _make_reauth_flow()
    flow.hass.config_entries.async_get_entry("entry1").unique_id = "old@example.com"
    flow.context["source"] = "reconfigure"
    with patch("custom_components.atmeex_cloud.config_flow.AtmeexApi") as api_cls:
        result = await flow.async_step_reconfigure(
            {CONF_EMAIL: "different@example.com", CONF_PASSWORD: "new"}
        )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "account_mismatch"
    api_cls.assert_not_called()


@pytest.mark.asyncio
async def test_email_reauth_rejects_identity_before_api_construction():
    flow = _make_reauth_flow()
    entry = flow.hass.config_entries.async_get_entry("entry1")
    entry.unique_id = "old@example.com"
    flow._reauth_entry = entry
    with patch("custom_components.atmeex_cloud.config_flow.AtmeexApi") as api_cls:
        result = await flow.async_step_reauth_confirm(
            {CONF_EMAIL: "different@example.com", CONF_PASSWORD: "new"}
        )
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
    api_cls.assert_not_called()


@pytest.mark.asyncio
async def test_phone_reauth_checks_stored_identity_before_sms():
    flow = _make_reauth_flow(auth_method=AUTH_METHOD_PHONE)
    entry = flow.hass.config_entries.async_get_entry("entry1")
    entry.unique_id = "+70000000000"
    entry.data = {**entry.data, CONF_PHONE: "+79991234567"}
    flow._reauth_entry = entry
    with patch("custom_components.atmeex_cloud.config_flow.AtmeexApi") as api_cls:
        result = await flow.async_step_reauth_phone_confirm({})
    assert result["type"] is data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
    api_cls.assert_not_called()
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_config_flow.py -q`

Expected: FAIL because duplicate phone checking follows SMS, tokenless phone login creates an entry, and `async_step_reconfigure` is absent.

- [ ] **Step 3: Implement identity-first flow rules**

Before constructing `AtmeexApi` in email/phone setup, normalize the identifier, call `await self.async_set_unique_id(identifier)`, then `_abort_if_unique_id_configured()`. In email reauth, set/check the submitted identifier against the reauth entry **before** obtaining a session or constructing the API. In phone reauth, compare the normalized phone stored in entry data with `entry.unique_id` before the explicit-confirm branch constructs an API or sends SMS; abort `account_mismatch` on either mismatch. After `login_phone`, require a non-empty string token before `get_devices` or entry creation:

```python
refresh_token = api.refresh_token
if not isinstance(refresh_token, str) or not refresh_token.strip():
    errors["base"] = "missing_refresh_token"
else:
    entry_data = {
        CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
        CONF_PHONE: self._pending_phone,
        "refresh_token": refresh_token,
    }
```

Implement reconfigure for the existing auth method only; compare normalized input to `entry.unique_id` before network I/O, validate credentials, then call `async_update_entry_and_reload` once and abort with `reconfigure_successful`. Track reauth and reconfigure in distinct fields/enum so the shared phone-code step cannot accidentally use `reauth_successful` for a reconfigure flow. Phone reconfigure reuses the explicit-confirm/SMS-code path and applies the same mandatory refresh-token check.

Add real flow-manager tests (not direct context mutation) for successful email
reconfigure and the complete phone confirm → SMS code path. Each must assert:
the unique ID is unchanged, entry data changes only after credential
validation, `async_reload(entry_id)` is awaited exactly once, and the abort
reason is `reconfigure_successful`. Add a phone missing-refresh-token variant
that stays on the code form, performs zero entry updates/reloads, and reports
`missing_refresh_token`. Retain existing successful/mismatched reauth tests and
assert email login/device fetch and phone SMS/login are never called on an
identity mismatch. Add `account_mismatch`, `missing_refresh_token`,
`reconfigure_successful`, and `options_updated` to all three translation files.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_config_flow.py -q`

Expected: PASS, including real manager, duplicate-before-network, phone token, reauth, and reconfigure cases.

```bash
git add custom_components/atmeex_cloud/config_flow.py custom_components/atmeex_cloud/strings.json custom_components/atmeex_cloud/translations/en.json custom_components/atmeex_cloud/translations/ru.json tests/test_config_flow.py
git commit -m "feat: add identity-safe credential reconfigure"
```

### Task 3: Register Stable Services Once and Translate Entity Errors

**Files:**
- Create: `tests/test_contract.py`
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/fan.py`
- Modify: `custom_components/atmeex_cloud/select.py`
- Modify: `custom_components/atmeex_cloud/switch.py`
- Modify: `custom_components/atmeex_cloud/sensor.py`
- Modify: `custom_components/atmeex_cloud/binary_sensor.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Modify: `custom_components/atmeex_cloud/services.yaml`
- Modify: `custom_components/atmeex_cloud/strings.json`
- Modify: `custom_components/atmeex_cloud/translations/en.json`
- Modify: `custom_components/atmeex_cloud/translations/ru.json`

- [ ] **Step 1: Add the public-contract RED test**

```python
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import voluptuous as vol
import yaml
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import Context
from pytest_homeassistant_custom_component.common import MockUser

import custom_components.atmeex_cloud.climate as climate_module
from custom_components.atmeex_cloud.const import DOMAIN

ROOT = Path(__file__).parents[1]
EXPECTED_UNIQUE_ID_SUFFIXES = {
    "climate", "fan", "hum_mode", "breezer_mode", "auto_nanny",
    "sleep_mode", "power", "co2", "inlet_temp", "humidity", "online",
    "no_water",
}


def test_service_option_translation_contract_is_unchanged():
    services = yaml.safe_load(
        (ROOT / "custom_components/atmeex_cloud/services.yaml").read_text()
    )
    assert set(services) == {"set_breezer_mode", "set_humidifier_stage"}
    assert services["set_breezer_mode"]["fields"]["mode"]["selector"]["select"]["options"] == [
        "supply_ventilation", "recirculation", "mixed_mode", "supply_valve"
    ]
    assert services["set_humidifier_stage"]["fields"]["stage"]["selector"]["number"] == {
        "min": 0, "max": 3, "mode": "slider"
    }
    strings = json.loads(
        (ROOT / "custom_components/atmeex_cloud/strings.json").read_text()
    )
    assert set(strings["options"]["step"]["init"]["data"]) == {
        "update_interval", "enable_websocket", "enable_co2"
    }


def test_every_platform_declares_parallel_updates_zero():
    for module in ("climate", "fan", "select", "switch", "sensor", "binary_sensor"):
        imported = __import__(f"custom_components.atmeex_cloud.{module}", fromlist=[module])
        assert imported.PARALLEL_UPDATES == 0


@pytest.mark.asyncio
async def test_services_register_once_through_entity_platform(
    hass, monkeypatch, climate_entry_and_add_entities
):
    platform = MagicMock()

    def register(name, schema, method):
        platform.calls.append((name, schema, method))
        hass.services.async_register(DOMAIN, name, AsyncMock(), schema=schema)

    platform.calls = []
    platform.async_register_entity_service.side_effect = register
    monkeypatch.setattr(climate_module, "async_get_current_platform", lambda: platform)
    entry, add_entities = climate_entry_and_add_entities

    await climate_module.async_setup_entry(hass, entry, add_entities)
    await climate_module.async_setup_entry(hass, entry, add_entities)

    assert [call[0] for call in platform.calls] == [
        "set_breezer_mode", "set_humidifier_stage"
    ]
    with pytest.raises(vol.Invalid):
        vol.Schema(platform.calls[0][1])({"mode": "invalid"})
    with pytest.raises(vol.Invalid):
        vol.Schema(platform.calls[1][1])({"stage": 4})


@pytest.mark.asyncio
async def test_entity_service_helper_filters_restricted_user_target(
    hass, registered_climate_entity
):
    user = MockUser().add_to_hass(hass)
    user.mock_policy({})
    await hass.services.async_call(
        DOMAIN,
        "set_breezer_mode",
        {ATTR_ENTITY_ID: registered_climate_entity.entity_id, "mode": "mixed_mode"},
        blocking=True,
        context=Context(user_id=user.id),
    )
    registered_climate_entity.async_set_breezer_mode.assert_not_awaited()
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_contract.py -q`

Expected: FAIL because climate does not guard registration at the integration
service registry, restricted-user coverage is absent, and platforms do not
define `PARALLEL_UPDATES`.

- [ ] **Step 3: Register once through Home Assistant's entity-platform helper**

Keep registration in `climate.async_setup_entry`, where Home Assistant exposes
the supported `EntityPlatform` API. Guard it by the integration service
registry so multiple entries do not repeat even the registration call:

```python
    platform = async_get_current_platform()
    if not hass.services.has_service(DOMAIN, "set_breezer_mode"):
        platform.async_register_entity_service(
            "set_breezer_mode",
            {vol.Required("mode"): vol.In(BREEZER_MODES)},
            "async_set_breezer_mode",
        )
    if not hass.services.has_service(DOMAIN, "set_humidifier_stage"):
        platform.async_register_entity_service(
            "set_humidifier_stage",
            {
                vol.Required("stage"): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=3)
                )
            },
            "async_set_humidifier_stage",
        )
```

Do not call `service.async_extract_entities` or entity methods manually. The
platform helper provides entity permissions, call context, serialization,
concurrent multi-entity handling, availability/feature filtering, and polling
behavior on both supported Home Assistant versions.

Add this constant to every platform:

```python
PARALLEL_UPDATES = 0
```

Keep the translated exception contract introduced by Plan 3; do not rename its
keys in this final plan:

```python
raise ServiceValidationError(
    translation_domain=DOMAIN,
    translation_key="invalid_command_value",
    translation_placeholders={"field": field, "value": str(value)},
)

raise ServiceValidationError(
    translation_domain=DOMAIN,
    translation_key="unsupported_device_feature",
    translation_placeholders={"feature": feature},
)
```

Execution failures use `HomeAssistantError` with `translation_domain=DOMAIN`, the command executor's supplied `translation_key`, and sanitized placeholders. Add matching `exceptions` keys in strings/en/ru. Keep `services.yaml` values byte-for-byte equivalent.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_contract.py tests/test_climate.py tests/test_entity_base.py -q`

Expected: PASS; services register once, invalid values are rejected by schema, and all platforms report `PARALLEL_UPDATES == 0`.

```bash
git add custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/fan.py custom_components/atmeex_cloud/select.py custom_components/atmeex_cloud/switch.py custom_components/atmeex_cloud/sensor.py custom_components/atmeex_cloud/binary_sensor.py custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/services.yaml custom_components/atmeex_cloud/strings.json custom_components/atmeex_cloud/translations/en.json custom_components/atmeex_cloud/translations/ru.json tests/test_contract.py tests/test_climate.py tests/test_entity_base.py
git commit -m "fix: stabilize Home Assistant service contract"
```

### Task 4: Whitelist Diagnostics and Redact Support Logs

**Files:**
- Create: `custom_components/atmeex_cloud/privacy.py`
- Create: `tests/test_privacy.py`
- Modify: `custom_components/atmeex_cloud/diagnostics.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/api.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/websocket.py`
- Modify: `custom_components/atmeex_cloud/helpers.py`
- Modify: `custom_components/atmeex_cloud/config_flow.py`
- Modify: `custom_components/atmeex_cloud/command_executor.py`
- Modify: `custom_components/atmeex_cloud/{climate,fan}.py`

- [ ] **Step 1: Add a complete sentinel privacy test**

```python
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers.json import json_dumps
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.diagnostics import (
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)
from custom_components.atmeex_cloud.websocket import WebSocketConfig, WebSocketManager


@pytest.mark.asyncio
async def test_diagnostics_and_logs_omit_every_private_sentinel(hass, caplog):
    sentinels = {
        "PRIVATE_PHONE", "PRIVATE_EMAIL", "PRIVATE_PASSWORD", "PRIVATE_ACCESS",
        "PRIVATE_REFRESH", "PRIVATE_TITLE", "PRIVATE_NAME", "PRIVATE_DEVICE_ID",
        "PRIVATE_REGISTRY_ID", "PRIVATE_AREA", "PRIVATE_RAW", "PRIVATE_SERVER_ERROR",
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="PRIVATE_TITLE",
        data={"phone": "PRIVATE_PHONE", "email": "PRIVATE_EMAIL", "password": "PRIVATE_PASSWORD", "refresh_token": "PRIVATE_REFRESH"},
        options={"update_interval": 30, "enable_websocket": True, "enable_co2": True},
    )
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        data={"devices": [{"id": "PRIVATE_DEVICE_ID", "name": "PRIVATE_NAME", "registry_id": "PRIVATE_REGISTRY_ID", "area_id": "PRIVATE_AREA", "future": "PRIVATE_RAW", "server_error": "PRIVATE_SERVER_ERROR"}], "device_map": {}, "states": {}},
        last_update_success=True, last_success_ts=1.0, avg_latency_ms=12.3,
        request_retries=1, last_api_error=SimpleNamespace(operation="get_devices", status=500),
    )
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator,
        api=SimpleNamespace(token="PRIVATE_ACCESS"),
        websocket_manager=None,
    )
    device_entry = SimpleNamespace(
        id="PRIVATE_REGISTRY_ID",
        name="PRIVATE_NAME",
        identifiers={(DOMAIN, "PRIVATE_DEVICE_ID")},
        manufacturer="PRIVATE_RAW",
        model="PRIVATE_RAW",
        sw_version=None,
        hw_version=None,
        area_id="PRIVATE_AREA",
    )
    with caplog.at_level(logging.DEBUG, logger="custom_components.atmeex_cloud"):
        manager = WebSocketManager(
            session=object(),
            token_getter=lambda: "PRIVATE_ACCESS",
            on_message=lambda _message: True,
            config=WebSocketConfig(),
            on_auth_failure=MagicMock(),
            on_token_refresh=AsyncMock(),
            task_factory=lambda coro, name: asyncio.create_task(coro, name=name),
        )
        await manager._handle_message(json.dumps({
            "type": "condition",
            "data": [{
                "id": "PRIVATE_DEVICE_ID",
                "condition": {"pwr_on": 1, "future": "PRIVATE_RAW"},
            }],
        }))
        entry_diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        device_diagnostics = await async_get_device_diagnostics(
            hass, entry, device_entry
        )
    serialized = (
        json_dumps(entry_diagnostics)
        + json_dumps(device_diagnostics)
        + caplog.text
    )
    assert not any(value in serialized for value in sentinels)
    assert entry_diagnostics["integration"]["update_interval"] == 30
    assert entry_diagnostics["coordinator"]["device_count"] == 1
    assert device_diagnostics["device"]["capabilities"] == {
        "has_condition": True,
        "has_settings": False,
    }
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_privacy.py tests/test_diagnostics.py -q`

Expected: FAIL because diagnostics currently include entry title/data, raw devices/states, names, IDs, and future payload fields.

- [ ] **Step 3: Replace recursive redaction with a safe schema**

Diagnostics may return only: integration version, auth-method category, option booleans, poll interval, coordinator success, device/entity counts, rounded latency/retry/overflow counters, error operation/status, WebSocket configured/connected state, coarse message age, and capability booleans. Never copy `entry.title`, `entry.data`, coordinator raw data, registry IDs, names, areas, coordinates, or error messages.

Implement stable-within-run anonymous labels for logs:

```python
# privacy.py
import hashlib
import secrets

_RUN_KEY = secrets.token_bytes(32)


def anonymous_device_label(device_id: int | str) -> str:
    digest = hashlib.blake2s(
        str(device_id).encode(), key=_RUN_KEY, digest_size=4
    ).hexdigest()
    return f"device-{digest}"
```

Replace payload logging with operation, message type, item count, status, and
anonymous labels. Apply the audit to `__init__.py`, `api.py`, `coordinator.py`,
`websocket.py`, `helpers.py`, `config_flow.py`, `command_executor.py`,
`climate.py`, and `fan.py`, not just the transport classes. In particular:

- remove `WebSocket message received: %s`, raw JSON parse data,
  condition/settings values, credentials, response bodies, and exception text
  from unknown config-flow/setup failures; log only the exception class and a
  fixed operation label;
- replace every device ID in a log line **and in entry-owned task names** with
  `anonymous_device_label`; public event payload IDs remain unchanged for
  automation compatibility but must never be copied into diagnostics/logs;
- ensure helper normalization logs report only accepted/ignored field counts;
  entity validation logs report only field/operation names, not supplied
  values;
- add parameterized calls in `tests/test_privacy.py` for malformed inventory,
  targeted-refresh failure, helper condition/settings normalization, unknown
  config-flow exception, and invalid climate/fan input. Feed a different
  `PRIVATE_*` sentinel through each path and assert none occurs in `caplog.text`;
  inspect `runtime.tasks` and assert no task name contains
  `PRIVATE_DEVICE_ID`;
- call both diagnostics entry points as shown above. Their output is built from
  fresh whitelisted dictionaries; never recursively redact a copy of entry,
  registry, device, state, or future payload data.

Finish with:

```bash
rg -n '_LOGGER\.(debug|info|warning|error|exception)' custom_components/atmeex_cloud
```

Review every match; no variable representing credentials, IDs, payloads,
values, raw errors, or server messages may be formatted into the record.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_privacy.py tests/test_diagnostics.py tests/test_api.py tests/test_websocket_manager.py -q`

Expected: PASS; `tests/test_privacy.py` reports `1 passed` and no sentinel appears in diagnostics or captured logs.

```bash
git add custom_components/atmeex_cloud/privacy.py custom_components/atmeex_cloud/diagnostics.py custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/websocket.py custom_components/atmeex_cloud/helpers.py custom_components/atmeex_cloud/config_flow.py custom_components/atmeex_cloud/command_executor.py custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/fan.py tests/test_privacy.py tests/test_diagnostics.py tests/test_api.py tests/test_websocket_manager.py tests/test_helpers.py tests/test_config_flow.py tests/test_climate.py tests/test_fan.py
git commit -m "fix: whitelist diagnostics and redact logs"
```

### Task 5: Reduce Recorder and Logbook Churn Without Removing Events

**Files:**
- Modify: `custom_components/atmeex_cloud/sensor.py`
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/logbook.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `tests/test_sensor.py`
- Modify: `tests/test_climate.py`
- Modify: `tests/test_logbook.py`
- Modify: `tests/test_websocket_integration.py`
- Modify: `tests/test_coordinator.py`

- [ ] **Step 1: Add RED churn tests**

```python
def test_diagnostics_entity_is_disabled_by_default_for_new_registry_entries():
    from custom_components.atmeex_cloud.sensor import AtmeexDiagnosticsSensor
    assert AtmeexDiagnosticsSensor._attr_entity_registry_enabled_default is False


def test_device_updates_remain_bus_events_but_are_not_logbook_described():
    registered = {}
    async_describe_events(
        SimpleNamespace(),
        lambda domain, event, describer: registered.setdefault(event, describer),
    )
    assert EVENT_API_ERROR in registered
    assert EVENT_DEVICE_UPDATED not in registered
```

Extend the existing WebSocket event-contract test to prove valid condition and
settings batches still fire `EVENT_DEVICE_UPDATED` with the same source and
payload keys for automations. Add a coordinator Event-barrier test that ages a
previously successful snapshot past `MAX_INVENTORY_AGE`, completes one
successful inventory refresh, and asserts exactly one public event with
`{"source": "recovery", "device_ids": ["1"]}` **and** one call to
`homeassistant.components.logbook.async_log_entry(hass, "Atmeex device",
"Device recovered", domain=DOMAIN)`. The next healthy poll emits neither
another recovery event nor another logbook entry.

Append to `tests/test_climate.py`, reusing its existing entity factory:

```python
def test_climate_attributes_exclude_volatile_timing_fields():
    ent, _cond, _api, _runtime = _make_entity_with_runtime(
        {"temp_room": 215, "u_temp_room": 225}
    )

    attrs = ent.extra_state_attributes
    assert attrs["room_temp_c"] == 21.5
    assert attrs["target_temp_c"] == 22.5
    for volatile in ("avg_latency_ms", "last_success_ts", "last_success_utc"):
        assert volatile not in attrs
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_sensor.py tests/test_logbook.py tests/test_climate.py -q`

Expected: FAIL because diagnostics are enabled by default, routine WebSocket updates are rendered in logbook, and climate still exposes volatile timing attributes.

- [ ] **Step 3: Implement low-churn rendering**

Set `_attr_entity_registry_enabled_default = False` on
`AtmeexDiagnosticsSensor`; Home Assistant applies this only to newly created
registry entries. Keep all existing diagnostic attributes, but rely on Plan
5's comparable coordinator snapshots so timing-only property changes never
notify listeners.

In `climate.py`, delete the volatile diagnostics block from
`extra_state_attributes` — `avg_latency_ms`, `last_success_ts`, and
`last_success_utc` change on every poll and would force a recorder write for
every climate entity even when nothing about the device changed. Plan 2's
snapshot already stopped carrying these keys, so today they would vanish as an
unstated side effect of the shape change; remove them deliberately so the
recorder contract is explicit and tested. Keep `room_temp_c`, `target_temp_c`,
and `has_humidifier`, which change only with real device state. The same
timing data remains available on the diagnostics sensor and in downloadable
diagnostics.

A logbook event describer must always return a mapping; Home Assistant indexes
the return value immediately. Routine device-update events are also part of the
automation contract, so do not suppress or rename them. Instead, stop
registering `EVENT_DEVICE_UPDATED` with the logbook platform. The bus event and
all existing payload keys continue to fire, but logbook/recorder no longer
subscribe to every technical update through this integration. Keep the
privacy-safe API-error describer registered.

When a snapshot was stale immediately before a successful authoritative poll,
fire the normal public recovery event and call `logbook.async_log_entry` once
with the fixed recovery text above. Clear the degraded flag so later healthy
polls are silent. This produces a meaningful logbook record without making a
conditional describer return `None` and without changing automation inputs.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_sensor.py tests/test_logbook.py tests/test_websocket_integration.py tests/test_climate.py -q`

Expected: PASS with no routine technical logbook entry, no volatile climate attributes, and unchanged event constants/payload compatibility.

```bash
git add custom_components/atmeex_cloud/sensor.py custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/logbook.py custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/coordinator.py tests/test_sensor.py tests/test_climate.py tests/test_logbook.py tests/test_websocket_integration.py tests/test_coordinator.py
git commit -m "perf: reduce recorder and logbook churn"
```

### Task 6: Lock Metadata, Runtime Annotations, and the Public Surface

**Files:**
- Modify: `custom_components/atmeex_cloud/manifest.json`
- Modify: `hacs.json`
- Modify: `custom_components/atmeex_cloud/runtime.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`
- Modify: `custom_components/atmeex_cloud/api.py`
- Modify: `custom_components/atmeex_cloud/config_flow.py`
- Modify: `custom_components/atmeex_cloud/coordinator.py`
- Modify: `custom_components/atmeex_cloud/entity_base.py`
- Modify: `custom_components/atmeex_cloud/climate.py`
- Modify: `custom_components/atmeex_cloud/select.py`
- Modify: `tests/test_contract.py`, `tests/test_runtime.py`, `tests/test_api.py`, `tests/test_config_flow.py`, `tests/test_coordinator.py`, `tests/test_entity_base.py`, `tests/test_climate.py`, `tests/test_select.py`, `tests/test_setup.py`

- [ ] **Step 1: Extend RED contract assertions**

```python
def test_manifest_and_hacs_metadata():
    manifest = json.loads((ROOT / "custom_components/atmeex_cloud/manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert manifest.get("requirements", []) == []
    assert hacs["homeassistant"] == "2024.8.0"


def test_unique_id_and_event_snapshot():
    from custom_components.atmeex_cloud.const import EVENT_API_ERROR, EVENT_DEVICE_UPDATED
    assert EVENT_API_ERROR == "atmeex_cloud_api_error"
    assert EVENT_DEVICE_UPDATED == "atmeex_cloud_device_updated"
    source = "\n".join(
        (ROOT / f"custom_components/atmeex_cloud/{module}.py").read_text()
        for module in ("climate", "fan", "select", "switch", "sensor", "binary_sensor")
    )
    literal_suffixes = set(re.findall(r'f"\{device\.id\}_([a-z0-9_]+)"', source))
    sensor_suffixes = set(re.findall(r'unique_suffix="([a-z0-9_]+)"', source))
    assert literal_suffixes | sensor_suffixes == EXPECTED_UNIQUE_ID_SUFFIXES
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_contract.py -q`

Expected: FAIL because the manifest redundantly lists `aiohttp`.

- [ ] **Step 3: Remove redundant metadata and narrow runtime annotations**

Remove `requirements` from `manifest.json`; retain version, integration type, IoT class, documentation, issue tracker, and HACS minimum. Define `AtmeexConfigEntry` behind `TYPE_CHECKING` so `ConfigEntry[AtmeexRuntimeData]` is never evaluated on HA 2024.8, and replace the runtime dependency fields with API/coordinator/store/executor/WebSocket annotations. This task claims import-safe, inspectable annotations—not repository-wide static-type completeness; `compileall` is not presented as a type checker.

Preserve `AtmeexApiError(operation: str, message: str, *, status: int | None = None)` and `AtmeexApi(session, *, on_refresh_token_changed: Callable[[str], None] | None = None)`. Migrate all internal imports from compatibility `ApiError` to typed errors and all production pending/lock access to `AtmeexCommandExecutor`. Keep `ApiError = AtmeexApiError`, the `PendingCommand` re-export, old runtime delegates, and any already-unused timestamp shim in place for now. Task 8 removes only those retired aliases after both compatibility jobs pass. Never remove `compat.py`; it is the intentional permanent version boundary.

- [ ] **Step 4: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_contract.py tests/test_runtime.py -q`

Expected: PASS. Run `rg -n "\bApiError\b|_ws_device_update_ts|_refresh_device_update_ts" custom_components/atmeex_cloud` and `rg -n "device_locks|get_device_lock|get_pending|set_pending|clear_pending" custom_components/atmeex_cloud/runtime.py`; matches are allowed only at explicitly marked compatibility definitions, never production consumers. Private locks inside `AtmeexCommandExecutor` are intentional.

```bash
git add custom_components/atmeex_cloud/manifest.json hacs.json custom_components/atmeex_cloud/runtime.py custom_components/atmeex_cloud/__init__.py custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/config_flow.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/entity_base.py custom_components/atmeex_cloud/climate.py custom_components/atmeex_cloud/select.py tests/test_contract.py tests/test_runtime.py tests/test_api.py tests/test_config_flow.py tests/test_coordinator.py tests/test_entity_base.py tests/test_climate.py tests/test_select.py tests/test_setup.py
git commit -m "refactor: finalize typed Home Assistant contract"
```

### Task 7: Enforce Warning-Free Per-Module Coverage in Both Supported Environments

**Files:**
- Create: `requirements-ci-ha-2024.8.txt`, `requirements-ci-current.txt`, `.coveragerc`, `scripts/check_module_coverage.py`, `tests/test_quality_gate.py`
- Modify: `requirements-dev.txt`, `pytest.ini`, `.gitignore`, `.github/workflows/validate.yml`
- Modify: every `tests/test_*.py` file identified by the first per-module
  coverage report; add behavior tests rather than exclusions.
- Delete: `.coverage`

- [ ] **Step 1: Add the complete coverage-gate unit test**

```python
import json

import pytest

from scripts.check_module_coverage import failing_modules


def test_each_source_module_must_exceed_95_percent(tmp_path):
    report = tmp_path / "coverage.json"
    report.write_text(json.dumps({"files": {
        "custom_components/atmeex_cloud/api.py": {"summary": {"percent_covered": 95.0}},
        "custom_components/atmeex_cloud/coordinator.py": {"summary": {"percent_covered": 95.1}},
    }}))
    assert failing_modules(report) == [("custom_components/atmeex_cloud/api.py", 95.0)]
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_quality_gate.py -q`

Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.check_module_coverage'`.

- [ ] **Step 3: Implement exact coverage/warning configuration**

```python
# scripts/check_module_coverage.py
from __future__ import annotations

import json
import sys
from pathlib import Path


def failing_modules(report: Path) -> list[tuple[str, float]]:
    files = json.loads(report.read_text())["files"]
    return sorted(
        (path, float(data["summary"]["percent_covered"]))
        for path, data in files.items()
        if path.startswith("custom_components/atmeex_cloud/")
        and path.endswith(".py")
        and float(data["summary"]["percent_covered"]) <= 95.0
    )


if __name__ == "__main__":
    failures = failing_modules(Path(sys.argv[1]))
    for path, percent in failures:
        print(f"{path}: {percent:.2f}% (must be >95%)")
    raise SystemExit(bool(failures))
```

Use this warning/coverage configuration:

```ini
# pytest.ini additions
filterwarnings =
    error::RuntimeWarning
    error::DeprecationWarning:^(custom_components\.atmeex_cloud|tests)(\.|$)
    error::pytest.PytestDeprecationWarning

# .coveragerc
[run]
branch = True
source = custom_components/atmeex_cloud

[report]
skip_empty = False
```

Generate `coverage.json`. Ignore `.coverage`, `.coverage.*`, `htmlcov/`, `.pytest_cache/`, and `coverage.json`; remove the tracked `.coverage` file.

Pin minimum stack to the exact dependency set published by
`pytest-homeassistant-custom-component==0.13.152`:
`homeassistant==2024.8.0`, `pytest==8.3.1`,
`pytest-asyncio==0.23.8`, `pytest-cov==5.0.0`, and `coverage==7.6.0`;
make `requirements-dev.txt` include it with
`-r requirements-ci-ha-2024.8.txt`. Pin current stack to the exact set
published by `pytest-homeassistant-custom-component==0.13.346`:
`homeassistant==2026.7.2`, `pytest==9.0.3`,
`pytest-asyncio==1.4.0`, `pytest-cov==7.1.0`, and `coverage==7.14.1`.
Do not override exact transitive pins from either test-helper package.

`asyncio_default_fixture_loop_scope` does not exist in the minimum stack's
pytest-asyncio 0.23.8, so do not put it in shared `pytest.ini`. The 2026.7
matrix row alone passes
`-o asyncio_default_fixture_loop_scope=function`; the minimum row passes no
such option. The minimum plugin does not emit the newer unset-scope notice.

- [ ] **Step 4: Run the real per-module gate and confirm coverage RED**

Run:

```bash
.venv/bin/python -m pytest --cov=custom_components/atmeex_cloud --cov-report=json:coverage.json -q
.venv/bin/python scripts/check_module_coverage.py coverage.json
```

Expected: the suite itself passes, then the script lists every production
module at or below 95%. Save that exact list in the task notes; it is the work
queue for the next step.

- [ ] **Step 5: Close every measured branch gap with behavior tests**

For each listed module, inspect `coverage.json`'s missing lines and branches and
add a focused test to the matching test module (or a narrowly named new one).
Cover success, typed failure, cancellation/cleanup, minimum-HA compatibility,
and empty-boundary behavior. Do not add `pragma: no cover`, omit a source file,
lower the threshold, or test private lines only to increase a number. Re-run
the two commands after each module until the failure list is empty. The strict
condition is **greater than** 95.0% for every Python file, including
`__init__.py`, not merely aggregate coverage.

- [ ] **Step 6: Replace CI with the two deliberate compatibility jobs**

Use a matrix with `Python 3.12 + requirements-ci-ha-2024.8.txt` and no extra
pytest option, plus `Python 3.14.2 + requirements-ci-current.txt` and
`pytest_args: -o asyncio_default_fixture_loop_scope=function`. Both run:

```bash
.venv/bin/python -m pytest $PYTEST_ARGS --cov=custom_components/atmeex_cloud --cov-report=json:coverage.json -q
.venv/bin/python scripts/check_module_coverage.py coverage.json
.venv/bin/python -m compileall -q custom_components/atmeex_cloud
```

In Actions, omit `.venv/bin/` because the selected interpreter is active. Pin `actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0` (v7.0.0), `actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1` (v6.3.0), `home-assistant/actions/hassfest@342664e951e2fd5f00bc990fc968342388252475`, and `hacs/action@d556e736723344f83838d08488c983a15381059a` (22.5.0); keep hassfest and HACS as separate jobs.

- [ ] **Step 7: Run GREEN and commit**

Run: `.venv/bin/python -m pytest tests/test_quality_gate.py -q && .venv/bin/python -m pytest --cov=custom_components/atmeex_cloud --cov-report=json:coverage.json -q && .venv/bin/python scripts/check_module_coverage.py coverage.json`

Expected: `tests/test_quality_gate.py` reports `1 passed`; the suite has zero warnings; every integration module prints no failure and exceeds 95%.

```bash
git rm .coverage
git add requirements-dev.txt requirements-ci-ha-2024.8.txt requirements-ci-current.txt .coveragerc pytest.ini .gitignore .github/workflows/validate.yml scripts/check_module_coverage.py tests
git commit -m "ci: enforce supported Home Assistant matrix"
```

### Task 8: Document the Final Contract and Run Release Gates

**Files:**
- Modify: `README.md`, `README.en.md`
- Modify: `tests/test_contract.py`
- Modify: `custom_components/atmeex_cloud/api.py`
- Modify: `custom_components/atmeex_cloud/runtime.py`
- Modify: `custom_components/atmeex_cloud/__init__.py`

- [ ] **Step 1: Add a documentation contract test**

```python
def test_both_readmes_document_final_support_contract():
    common = {
        "Home Assistant 2024.8", "WebSocket", "HTTP", "set_breezer_mode",
        "set_humidifier_stage", "update_interval", "enable_websocket",
        "enable_co2",
    }
    language = {
        "README.md": {"диагностика", "конфиденциальность", "без миграции"},
        "README.en.md": {"diagnostics", "privacy", "no migration"},
    }
    for name, specific in language.items():
        text = (ROOT / name).read_text().lower()
        assert {item.lower() for item in common | specific}.issubset(text)
```

- [ ] **Step 2: Run RED**

Run: `.venv/bin/python -m pytest tests/test_contract.py::test_both_readmes_document_final_support_contract -q`

Expected: FAIL because both READMEs omit the final diagnostic privacy and migration-free upgrade language.

- [ ] **Step 3: Update both READMEs with matching complete sections**

Document minimum/current support, list-versus-detail polling, maximum inventory age, valid empty accounts, outage availability, two-success removal, WebSocket fallback, supported capabilities, all three options, both services and their exact accepted values, reauth/reconfigure, diagnostic whitelist/privacy, troubleshooting, limitations, development commands, and this upgrade note: existing config entries, entity registry choices, entity IDs, services, options, and automations require no migration.

- [ ] **Step 4: Run every final gate**

Run: `.venv/bin/python -m pytest -q`

Expected: all tests pass with zero runtime warnings and zero repository-owned deprecation warnings.

Run: `.venv/bin/python -m pytest --cov=custom_components/atmeex_cloud --cov-report=json:coverage.json -q && .venv/bin/python scripts/check_module_coverage.py coverage.json`

Expected: exit code 0 and every integration module above 95%.

Run: `.venv/bin/python -m compileall -q custom_components/atmeex_cloud && git diff --check`

Expected: exit code 0 with no output.

- [ ] **Step 5: Verify both CI environments with compatibility aliases intact**

Run the `ha-2024.8-python-3.12`, `current-python-3.14`, hassfest, and HACS
jobs. Expected: all four jobs green while the temporary `ApiError`,
`PendingCommand`, runtime-delegate, and timestamp aliases still exist and all
production consumers have already migrated.

- [ ] **Step 6: Remove only proven-retired internal aliases**

Re-run the Task 6 `rg` audits. If and only if they show no production/test
consumer, remove `ApiError = AtmeexApiError`, the package-level
`PendingCommand` re-export, old runtime lock/pending delegates, and unused
timestamp merge attributes. Do not remove `compat.py`, feature-detection
branches, or any identifier in the immutable Home Assistant public contract.
Update tests to import the owning typed modules directly.

- [ ] **Step 7: Re-run release gates after alias removal**

Run every Step 4 command again, then rerun both Home Assistant matrix jobs,
hassfest, and HACS. Expected: all local and remote gates remain green. If either
version fails, restore the specific alias in the implementation commit and
keep its zero-consumer audit as follow-up evidence; do not weaken the matrix.

- [ ] **Step 8: Commit the final documentation and retired aliases**

```bash
git add README.md README.en.md tests/test_contract.py custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/runtime.py custom_components/atmeex_cloud/__init__.py tests
git commit -m "chore: finalize hardened integration contract"
```
