"""Shared entity mixin contract tests."""
from __future__ import annotations

import json
from pathlib import Path
from string import Formatter
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.api import AtmeexDevice
from custom_components.atmeex_cloud.binary_sensor import AtmeexOnlineSensor
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.sensor import AtmeexCO2Sensor
from custom_components.atmeex_cloud.select import AtmeexBreezerSelect, BREEZER_OPTIONS
from custom_components.atmeex_cloud.runtime import AtmeexRuntimeData
from tests.test_climate import _make_entity, _make_entity_with_runtime
from tests.test_fan import _make_fan_entity
from tests.test_select import _make_selects
from tests.test_switch import _make_power_switch, _make_switches


def _make_auto_switch():
    auto, _sleep, api, refresh_cb = _make_switches()
    return auto, None, api, refresh_cb


def _make_sleep_switch():
    _auto, sleep, api, refresh_cb = _make_switches()
    return sleep, None, api, refresh_cb


def _make_power_switch_standard():
    power, api, refresh_cb = _make_power_switch()
    return power, None, api, refresh_cb


def _make_humidification_select():
    hum, _breezer, _cond, api, coord = _make_selects()
    return hum, None, api, coord


def _make_breezer_select():
    _hum, breezer, _cond, api, coord = _make_selects()
    return breezer, None, api, coord


@pytest.mark.parametrize(
    ("make_entity", "set_pending", "read_value", "expected"),
    [
        (
            lambda: _make_entity_with_runtime({"pwr_on": False}),
            lambda ent, runtime: runtime.set_pending(1, "pwr_on", True),
            lambda ent: ent.hvac_mode,
            HVACMode.HEAT,
        ),
        (
            lambda: (_make_switches({"online": True, "u_auto": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_auto", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_switches({"online": True, "u_night": False}, with_runtime=True)[1],),
            lambda ent, runtime: ent._runtime.set_pending(42, "u_night", True),
            lambda ent: ent.is_on,
            True,
        ),
        (
            lambda: (_make_power_switch({"online": True, "pwr_on": False}, with_runtime=True)[0],),
            lambda ent, runtime: ent._runtime.set_pending(42, "pwr_on", True),
            lambda ent: ent.is_on,
            True,
        ),
    ],
)
def test_pending_state_via_mixin(make_entity, set_pending, read_value, expected):
    made = make_entity()
    ent = made[0]
    runtime = getattr(ent, "_runtime", made[3] if len(made) > 3 else None)

    set_pending(ent, runtime)

    assert read_value(ent) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_entity", "configure_failure", "action", "expected_action"),
    [
        (
            _make_auto_switch,
            lambda ent, api: setattr(
                api.set_auto_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "enable AutoNanny",
        ),
        (
            _make_sleep_switch,
            lambda ent, api: setattr(
                api.set_sleep_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "enable sleep mode",
        ),
        (
            _make_power_switch_standard,
            lambda ent, api: setattr(
                api.set_power,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_turn_on(),
            "turn on the device",
        ),
        (
            _make_humidification_select,
            lambda ent, api: setattr(
                api.set_humid_stage,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_select_option("2"),
            "set humidification stage",
        ),
        (
            _make_breezer_select,
            lambda ent, api: setattr(
                api.set_breezer_mode,
                "side_effect",
                ApiError("test_entity_command", "boom", status=500),
            ),
            lambda ent: ent.async_select_option(BREEZER_OPTIONS[1]),
            "set breezer mode",
        ),
    ],
)
async def test_api_error_translates_to_home_assistant_error(
    make_entity, configure_failure, action, expected_action
):
    made = make_entity()
    ent = made[0]
    api = made[2]
    configure_failure(ent, api)

    with pytest.raises(HomeAssistantError) as raised:
        await action(ent)

    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == "command_failed"
    assert raised.value.translation_placeholders == {
        "action": expected_action
    }


@pytest.mark.asyncio
async def test_entity_passes_a_factory_to_the_entry_executor():
    ent, _cond, api, runtime = _make_entity_with_runtime()
    operation_started = False
    captured_factory = None

    async def capture_execute(
        device_id,
        operation,
        *,
        pending,
        translation_key,
        translation_placeholders=None,
    ):
        nonlocal captured_factory
        captured_factory = operation
        assert device_id == 1
        assert pending == {"fan_speed": 5}
        assert translation_key == "command_failed"
        return False

    runtime.command_executor.async_execute = AsyncMock(side_effect=capture_execute)

    async def set_fan_speed(device_id, speed):
        nonlocal operation_started
        operation_started = True

    api.set_fan_speed.side_effect = set_fan_speed
    confirmation_success = await ent._execute_command(
        lambda: api.set_fan_speed(1, 5),
        pending={"fan_speed": 5},
        translation_key="command_failed",
    )

    assert confirmation_success is False
    assert operation_started is False
    assert callable(captured_factory)
    await captured_factory()
    assert operation_started is True


@pytest.mark.asyncio
async def test_entity_reuses_one_fallback_executor():
    ent, _cond, _api = _make_entity()

    async def operation() -> None:
        return

    await ent._execute_command(
        operation,
        pending={"fan_speed": 3},
        translation_key="command_failed",
    )
    first = ent._fallback_command_executor
    await ent._execute_command(
        operation,
        pending={"fan_speed": 5},
        translation_key="command_failed",
    )

    assert ent._fallback_command_executor is first


def _make_lifecycle_coordinator():
    remove_listener = MagicMock()
    coordinator = MagicMock()
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    coordinator.data = {
        "device_map": {"1": dev},
        "states": {
            "1": {
                "online": True,
                "pwr_on": True,
                "fan_speed": 3,
                "damp_pos": 2,
                "hum_stg": 1,
                "co2_ppm": 420,
            }
        },
    }
    coordinator.async_add_listener = MagicMock(return_value=remove_listener)
    return coordinator, dev, remove_listener


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_factory",
    [
        lambda coord, dev: _make_entity()[0],
        lambda coord, dev: AtmeexFanEntity(coord, MagicMock(), "entry1", dev),
        lambda coord, dev: _make_power_switch()[0],
        lambda coord, dev: AtmeexBreezerSelect(coord, MagicMock(), dev),
        lambda coord, dev: AtmeexCO2Sensor(coord, dev, "entry1"),
        lambda coord, dev: AtmeexOnlineSensor(coord, dev, "entry1"),
    ],
)
async def test_entity_lifecycle_registers_and_removes_coordinator_listener(entity_factory):
    coordinator, dev, remove_listener = _make_lifecycle_coordinator()
    ent = entity_factory(coordinator, dev)
    if getattr(ent, "coordinator", None) is not coordinator:
        ent.coordinator.async_add_listener = coordinator.async_add_listener
        remove_listener = coordinator.async_add_listener.return_value

    await ent.async_added_to_hass()
    ent._call_on_remove_callbacks()
    await ent.async_will_remove_from_hass()

    ent.coordinator.async_add_listener.assert_called_once()
    remove_listener.assert_called_once()


def test_command_exception_translations_match():
    root = Path(__file__).parents[1] / "custom_components" / "atmeex_cloud"
    paths = [
        root / "strings.json",
        root / "translations" / "en.json",
        root / "translations" / "ru.json",
    ]
    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in paths
    ]
    expected_placeholders = {
        "command_failed": {"action"},
        "invalid_command_value": {"field", "value"},
        "unsupported_device_feature": {"feature"},
    }

    exception_keys = [set(document["exceptions"]) for document in documents]
    assert all(keys == exception_keys[0] for keys in exception_keys[1:])
    assert set(expected_placeholders) <= exception_keys[0]

    formatter = Formatter()
    for document in documents:
        for key, placeholders in expected_placeholders.items():
            message = document["exceptions"][key]["message"]
            assert isinstance(message, str) and message
            actual_placeholders = {
                field_name
                for _literal, field_name, _format_spec, _conversion
                in formatter.parse(message)
                if field_name is not None
            }
            assert actual_placeholders == placeholders


# --- Coordinator-aware availability truth table (Plan 5) ---
from types import SimpleNamespace  # noqa: E402

from custom_components.atmeex_cloud.entity_base import AtmeexEntityMixin  # noqa: E402


class _BareEntity(AtmeexEntityMixin):
    def __init__(self, coordinator, device_id, meta):
        self.coordinator = coordinator
        self._device_id = device_id
        self._device_meta = meta


def _availability_coordinator(*, success: bool, online: bool):
    dev = AtmeexDevice.from_raw(
        {"id": 1, "online": online, "condition": {}, "settings": {}}
    )
    return SimpleNamespace(
        data={"states": {"1": {"online": online}}, "device_map": {"1": dev}},
        last_update_success=success,
    ), dev


@pytest.mark.parametrize(
    ("success", "online", "expected"),
    [
        (True, True, True),    # healthy + online -> available
        (True, False, False),  # healthy + offline -> unavailable
        (False, True, False),  # coordinator unhealthy -> unavailable regardless
        (False, False, False),
    ],
)
def test_available_honors_coordinator_health_and_online(success, online, expected):
    coordinator, dev = _availability_coordinator(success=success, online=online)
    entity = _BareEntity(coordinator, 1, dev)
    assert entity.available is expected
