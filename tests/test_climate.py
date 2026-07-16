import asyncio
import math

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import PRESET_BOOST, PRESET_NONE, PRESET_SLEEP
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from typing import Any

from custom_components.atmeex_cloud import AtmeexRuntimeData
import custom_components.atmeex_cloud.command_executor as command_executor_module
from custom_components.atmeex_cloud.api import ApiError
import custom_components.atmeex_cloud.climate as climate_module
from custom_components.atmeex_cloud.climate import (
    AtmeexClimateEntity,
    PRESET_AUTO,
    quantize_humidity,
    HUM_ALLOWED,
    BREEZER_SWING_MODES,
)
from custom_components.atmeex_cloud.helpers import humidity_to_stage
from custom_components.atmeex_cloud.const import DOMAIN, BREEZER_MODES
from custom_components.atmeex_cloud.api import AtmeexDevice
from pytest_homeassistant_custom_component.common import MockConfigEntry


def test_quantize_humidity():
    assert quantize_humidity(None) == 0
    assert quantize_humidity(-5) == 0
    assert quantize_humidity(10) == 0
    assert quantize_humidity(20) == 33
    assert quantize_humidity(40) == 33
    assert quantize_humidity(60) == 66
    assert quantize_humidity(80) == 66
    assert quantize_humidity(95) == 100
    assert set(HUM_ALLOWED) == {0, 33, 66, 100}


def _make_entity(overrides: dict[str, Any] | None = None):
    cond = {
        "online": True,
        "pwr_on": True,
        "fan_speed": 3,
        "damp_pos": 2,
        "hum_stg": 1,
        "hum_room": 45,
        "temp_room": 215,
        "u_temp_room": 225,
    }
    if overrides:
        cond.update(overrides)

    coordinator = SimpleNamespace(
        data={
            "states": {"1": cond},
        },
        last_update_success=True,
        async_request_refresh=AsyncMock(),
    )

    api = SimpleNamespace(
        set_power=AsyncMock(),
        set_target_temperature=AsyncMock(),
        set_humid_stage=AsyncMock(),
        set_fan_speed=AsyncMock(),
        set_breezer_mode=AsyncMock(),
        set_sleep_mode=AsyncMock(),
        set_auto_mode=AsyncMock(),
        set_heater_off=AsyncMock(),
        set_power_and_heat=AsyncMock(),
    )

    # Минимальный raw для устройства
    raw = {
        "id": 1,
        "name": "Test Device",
        "model": "test-model",
        "online": cond.get("online", True),
        "condition": {},  # нам в тестах всё равно, состояние берём из coordinator.data["states"]
        "settings": {},
    }
    dev = AtmeexDevice.from_raw(raw)

    # Чтобы self._device тоже мог найтись по device_map
    coordinator.data["device_map"] = {"1": dev}

    entry_id = "entry1"

    ent = AtmeexClimateEntity(
        coordinator=coordinator,
        api=api,
        entry_id=entry_id,
        device=dev,
        # refresh_device_cb в этих тестах не нужен
        refresh_device_cb=None,
        runtime=None,
    )

    return ent, cond, api


def _make_entity_with_runtime(overrides: dict[str, Any] | None = None):
    ent, cond, api = _make_entity(overrides)
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=ent.coordinator,
        refresh_device=AsyncMock(),
    )
    ent._runtime = runtime
    ent._refresh_device_cb = runtime.refresh_device
    return ent, cond, api, runtime


@pytest.mark.asyncio
async def test_async_setup_entry_registers_climate_services(monkeypatch, hass):
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    coordinator = SimpleNamespace(
        data={"device_map": {"1": dev}, "states": {"1": {"pwr_on": True}}},
        async_add_listener=lambda listener: (lambda: None),
    )
    entry = MockConfigEntry(domain=DOMAIN, entry_id="entry1")
    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    entry.runtime_data = runtime

    registered: list[str] = []
    platform = SimpleNamespace(
        async_register_entity_service=lambda name, schema, method: registered.append(name)
    )
    monkeypatch.setattr(climate_module, "async_get_current_platform", lambda: platform)

    await climate_module.async_setup_entry(hass, entry, lambda entities: None)

    assert registered == ["set_breezer_mode", "set_humidifier_stage"]


def test_climate_basic_properties():
    ent, cond, api = _make_entity()

    assert ent.available is True

    # поддерживаем TARGET_HUMIDITY при наличии hum_stg
    assert ent.supported_features & ClimateEntityFeature.TARGET_HUMIDITY

    # pwr_on=True, u_temp_room=225 (valid), damp_pos=2 → HEAT
    assert ent.hvac_mode == HVACMode.HEAT

    assert ent.current_temperature == pytest.approx(21.5)
    assert ent.target_temperature == pytest.approx(22.5)
    assert ent.precision == 0.5

    assert ent.current_humidity == 45
    assert ent.target_humidity == HUM_ALLOWED[1]  # 33

    assert ent.fan_mode == "3"
    assert ent.swing_mode == BREEZER_SWING_MODES[2]

    attrs = ent.extra_state_attributes
    assert attrs["room_temp_c"] == pytest.approx(21.5)
    assert attrs["target_temp_c"] == pytest.approx(22.5)
    assert attrs["has_humidifier"] is True


def test_climate_no_humidifier():
    ent, cond, api = _make_entity()
    # имитируем устройство БЕЗ увлажнителя — удаляем ключ полностью
    cond.pop("hum_stg", None)
    ent.coordinator.data["states"]["1"] = cond

    assert ent.target_humidity is None
    assert not (ent.supported_features & ClimateEntityFeature.TARGET_HUMIDITY)

    attrs = ent.extra_state_attributes
    assert attrs["has_humidifier"] is False


def test_climate_target_temp_fallbacks():
    # нет цели → None (no fake target from current_temperature)
    ent, cond, api = _make_entity({"u_temp_room": None})
    assert ent.target_temperature is None

    # нет ни цели, ни текущей — тоже None
    ent2, cond2, api2 = _make_entity({"u_temp_room": None, "temp_room": None})
    assert ent2.target_temperature is None

    # sentinel value out of range → None
    ent3, cond3, api3 = _make_entity({"u_temp_room": -1000})
    assert ent3.target_temperature is None


def test_climate_fan_mode_invalid():
    ent, cond, api = _make_entity({"fan_speed": 10})
    assert ent.fan_mode is None


def test_climate_swing_mode_invalid():
    ent, cond, api = _make_entity({"damp_pos": 5})
    assert ent.swing_mode is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hvac_mode", "initial_pwr_on", "expected_call"),
    [
        (HVACMode.OFF, True, "set_power"),
        (HVACMode.FAN_ONLY, True, "set_heater_off"),
        (HVACMode.FAN_ONLY, False, "set_heater_off"),
    ],
)
async def test_async_set_hvac_mode_calls_expected_api(
    hvac_mode, initial_pwr_on, expected_call
):
    ent, cond, api = _make_entity({"pwr_on": initial_pwr_on})
    ent._refresh = AsyncMock()

    await ent.async_set_hvac_mode(hvac_mode)

    if expected_call == "set_power":
        api.set_power.assert_awaited_once_with(1, False)
        api.set_heater_off.assert_not_awaited()
        api.set_power_and_heat.assert_not_awaited()
    elif expected_call == "set_heater_off":
        api.set_heater_off.assert_awaited_once_with(1)
        api.set_power.assert_awaited_once_with(1, True)
        api.set_power_and_heat.assert_not_awaited()
    ent._refresh.assert_awaited_once()





@pytest.mark.asyncio
async def test_async_set_temperature_turns_on_if_needed():
    # кондишн с выключенным устройством
    ent, cond, api = _make_entity({"pwr_on": False})
    ent._refresh = AsyncMock()

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    api.set_power_and_heat.assert_not_awaited()
    ent._refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_temperature_on_off_device_tracks_pwr_on_pending():
    """Implicit power-on in async_set_temperature must be tracked as pending.

    Without pending tracking a coordinator poll arriving between set_power()
    and the refresh can show the device as OFF — breaking optimistic UI state.
    """
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    ent._refresh_device_cb = AsyncMock()

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 22.0})

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 22.0)
    api.set_power_and_heat.assert_not_awaited()
    # pwr_on=True must be pending so hvac_mode shows device as on (not OFF)
    # u_temp_room=225 (valid) + damp_pos=2 → HEAT
    assert ent.hvac_mode == HVACMode.HEAT


def test_device_info_reflects_name_updates():
    """device_info must use the live coordinator data, not a cached-at-init snapshot.

    @cached_property freezes name/model for the entity's lifetime; a rename in
    the Atmeex app would not show up in HA until the entry is reloaded.
    """
    ent, cond, api = _make_entity()
    # Confirm initial name
    assert ent.device_info["name"] == "Test Device"

    # Simulate the device being renamed in coordinator data
    updated_dev = AtmeexDevice.from_raw({
        "id": 1,
        "name": "Renamed Device",
        "model": "new-model",
        "online": True,
        "condition": {},
        "settings": {},
    })
    ent.coordinator.data["device_map"]["1"] = updated_dev
    ent._device_meta = updated_dev  # _device_meta drives device_info

    # device_info must reflect the new name immediately (no cache hit)
    assert ent.device_info["name"] == "Renamed Device"


@pytest.mark.asyncio
async def test_async_set_temperature_rejects_missing_value():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_temperature()

    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == "temperature"
    api.set_power_and_heat.assert_not_awaited()
    api.set_target_temperature.assert_not_awaited()
    ent._refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_set_humidity_quantizes_and_calls_api():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    await ent.async_set_humidity(50)

    # 50 → ближайшее 66 → индекс 2
    api.set_humid_stage.assert_awaited_once_with(1, 2)
    ent._refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_set_humidity_no_humidifier_is_unsupported():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()
    # убираем признак увлажнителя
    cond.pop("hum_stg", None)
    ent.coordinator.data["states"]["1"] = cond

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_humidity(50)

    assert raised.value.translation_key == "unsupported_device_feature"
    api.set_humid_stage.assert_not_awaited()
    ent._refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_set_fan_mode_valid_and_invalid():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    await ent.async_set_fan_mode("4")
    api.set_fan_speed.assert_awaited_once_with(1, 4)
    ent._refresh.assert_awaited_once()

    api.set_fan_speed.reset_mock()
    ent._refresh.reset_mock()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_fan_mode("invalid")
    assert raised.value.translation_key == "invalid_command_value"
    api.set_fan_speed.assert_not_awaited()
    ent._refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_async_set_swing_mode_valid_and_invalid():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    mode = BREEZER_SWING_MODES[1]
    await ent.async_set_swing_mode(mode)
    api.set_breezer_mode.assert_awaited_once_with(1, 1)
    ent._refresh.assert_awaited_once()

    api.set_breezer_mode.reset_mock()
    ent._refresh.reset_mock()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_swing_mode("неизвестный режим")
    assert raised.value.translation_key == "invalid_command_value"
    api.set_breezer_mode.assert_not_awaited()
    ent._refresh.assert_not_awaited()



def test_climate_hvac_mode_clears_expired_pending(monkeypatch):
    now = 100.0
    monkeypatch.setattr(command_executor_module.time, "monotonic", lambda: now)
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    runtime.set_pending(1, "pwr_on", True)

    now = 120.0

    assert ent.hvac_mode == HVACMode.OFF
    assert runtime.get_pending(1, "pwr_on") is None


def test_climate_hvac_mode_uses_entry_executor():
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": True})
    runtime.command_executor.value_with_pending = MagicMock(
        wraps=runtime.command_executor.value_with_pending
    )

    # pwr_on=True, u_temp_room=225 (valid), damp_pos=2 → HEAT
    assert ent.hvac_mode == HVACMode.HEAT
    # hvac_mode checks pending for pwr_on first, then u_temp_room
    calls = runtime.command_executor.value_with_pending.call_args_list
    assert any(c == ((1, "pwr_on", True), {}) for c in calls)


def test_climate_fan_mode_uses_and_clears_pending():
    ent, cond, api, runtime = _make_entity_with_runtime({"fan_speed": 3})
    runtime.set_pending(1, "fan_speed", 5)

    assert ent.fan_mode == "5"
    cond["fan_speed"] = 5
    assert ent.fan_mode == "5"
    assert runtime.get_pending(1, "fan_speed") is None





@pytest.mark.asyncio
async def test_preset_sleep_then_restore_previous_fan_mode():
    ent, cond, api = _make_entity({"fan_speed": 4})
    ent.async_write_ha_state = lambda: None

    # Simulate device state update after API call
    async def _set_sleep(dev_id, enabled):
        cond["u_night"] = enabled
    api.set_sleep_mode.side_effect = _set_sleep

    await ent.async_set_preset_mode(PRESET_SLEEP)
    assert ent.preset_mode == PRESET_SLEEP
    assert ent._saved_fan_mode == "4"
    api.set_sleep_mode.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_awaited_once_with(1, 2)

    api.set_fan_speed.reset_mock()
    api.set_sleep_mode.reset_mock()
    cond["fan_speed"] = 2

    await ent.async_set_preset_mode(PRESET_NONE)
    assert ent.preset_mode == PRESET_NONE
    assert ent._saved_fan_mode is None
    api.set_sleep_mode.assert_awaited_once_with(1, False)
    api.set_fan_speed.assert_awaited_once_with(1, 4)


@pytest.mark.asyncio
async def test_preset_boost_then_restore_previous_fan_mode():
    ent, cond, api = _make_entity({"fan_speed": 3})
    ent.async_write_ha_state = lambda: None

    await ent.async_set_preset_mode(PRESET_BOOST)
    assert ent.preset_mode == PRESET_BOOST
    assert ent._is_boost is True
    assert ent._saved_fan_mode == "3"
    api.set_fan_speed.assert_awaited_once_with(1, 7)

    api.set_fan_speed.reset_mock()
    cond["fan_speed"] = 7

    await ent.async_set_preset_mode(PRESET_NONE)
    assert ent.preset_mode == PRESET_NONE
    assert ent._is_boost is False
    assert ent._saved_fan_mode is None
    api.set_fan_speed.assert_awaited_once_with(1, 3)


@pytest.mark.asyncio
async def test_preset_auto_calls_api():
    """PRESET_AUTO activates auto mode via the API."""
    ent, cond, api = _make_entity({"fan_speed": 3})
    ent.async_write_ha_state = lambda: None

    async def _set_auto(dev_id, enabled):
        cond["u_auto"] = enabled
    api.set_auto_mode.side_effect = _set_auto

    await ent.async_set_preset_mode(PRESET_AUTO)
    assert ent.preset_mode == PRESET_AUTO
    api.set_auto_mode.assert_awaited_once_with(1, True)
    # Fan speed should not change in auto mode
    api.set_fan_speed.assert_not_called()

    api.set_auto_mode.reset_mock()
    await ent.async_set_preset_mode(PRESET_NONE)
    assert ent.preset_mode == PRESET_NONE
    api.set_auto_mode.assert_awaited_once_with(1, False)


@pytest.mark.asyncio
async def test_preset_mode_reads_from_device_state():
    """preset_mode property reflects u_auto and u_night from device state."""
    ent, cond, _api = _make_entity({"fan_speed": 3})
    assert ent.preset_mode == PRESET_NONE

    cond["u_night"] = True
    assert ent.preset_mode == PRESET_SLEEP

    cond["u_night"] = False
    cond["u_auto"] = True
    assert ent.preset_mode == PRESET_AUTO

    cond["u_auto"] = False
    assert ent.preset_mode == PRESET_NONE

    # BOOST is client-side
    ent._is_boost = True
    ent._local_preset = PRESET_BOOST
    assert ent.preset_mode == PRESET_BOOST
    # BOOST takes priority even if u_auto is set
    cond["u_auto"] = True
    assert ent.preset_mode == PRESET_BOOST


# ---------------------------------------------------------------------------
# Service: set_breezer_mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_set_breezer_mode_calls_api():
    """set_breezer_mode delegates to async_set_swing_mode → api.set_breezer_mode."""
    ent, _cond, api = _make_entity()
    mode = BREEZER_MODES[1]  # "recirculation"
    await ent.async_set_breezer_mode(mode)
    api.set_breezer_mode.assert_awaited_once_with(1, 1)


@pytest.mark.asyncio
async def test_service_set_breezer_mode_all_valid_modes():
    """Every entry in BREEZER_MODES maps to the correct API index."""
    for idx, mode in enumerate(BREEZER_MODES):
        ent, _cond, api = _make_entity()
        await ent.async_set_breezer_mode(mode)
        api.set_breezer_mode.assert_awaited_once_with(1, idx)


@pytest.mark.asyncio
async def test_service_set_breezer_mode_raises_on_api_error():
    """ApiError from set_breezer_mode is re-raised as HomeAssistantError."""
    ent, _cond, api = _make_entity()
    api.set_breezer_mode.side_effect = ApiError(
        "test_climate_command", "network", status=503
    )
    with pytest.raises(HomeAssistantError):
        await ent.async_set_breezer_mode(BREEZER_MODES[0])


# ---------------------------------------------------------------------------
# Service: set_humidifier_stage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_service_set_humidifier_stage_calls_api():
    """set_humidifier_stage calls api.set_humid_stage with the requested stage."""
    ent, _cond, api = _make_entity({"hum_stg": 0})
    await ent.async_set_humidifier_stage(2)
    api.set_humid_stage.assert_awaited_once_with(1, 2)


@pytest.mark.asyncio
async def test_service_set_humidifier_stage_no_humidifier_is_unsupported():
    """When the device has no humidifier, reject the unsupported service."""
    # Remove hum_stg from state so _has_humidifier() returns False
    ent, _cond, api = _make_entity({})
    del ent.coordinator.data["states"]["1"]["hum_stg"]
    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_humidifier_stage(1)
    assert raised.value.translation_key == "unsupported_device_feature"
    api.set_humid_stage.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [0, 3])
async def test_service_set_humidifier_stage_accepts_boundary_values(stage):
    ent, _cond, api = _make_entity({"hum_stg": 1})
    await ent.async_set_humidifier_stage(stage)
    api.set_humid_stage.assert_awaited_once_with(1, stage)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [-1, 4])
async def test_service_set_humidifier_stage_rejects_out_of_range(stage):
    ent, _cond, api = _make_entity({"hum_stg": 1})

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_humidifier_stage(stage)

    assert raised.value.translation_key == "invalid_command_value"
    api.set_humid_stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_set_humidifier_stage_raises_on_api_error():
    """ApiError from set_humid_stage is re-raised as HomeAssistantError."""
    ent, _cond, api = _make_entity({"hum_stg": 1})
    api.set_humid_stage.side_effect = ApiError(
        "test_climate_command", "timeout", status=None
    )
    with pytest.raises(HomeAssistantError):
        await ent.async_set_humidifier_stage(1)


@pytest.mark.parametrize("val,expected_stage", [
    (0, 0),
    (16, 0),    # midpoint is 16.5, so 16 rounds to stage 0
    (17, 1),    # 17 is closer to 33 than to 0
    (33, 1),
    (50, 2),    # closest to 66
    (66, 2),
    (84, 3),    # closest to 100
    (100, 3),
    (None, 0),
])
def test_humidity_to_stage_returns_index(val, expected_stage):
    """humidity_to_stage must return the HUM_ALLOWED index, never raise ValueError."""
    assert humidity_to_stage(val) == expected_stage


# ---------------------------------------------------------------------------
# HEAT mode — hvac_mode truth table and transitions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "pwr_on, u_temp_room, damp_pos, expected_mode",
    [
        (False, 225, 2, HVACMode.OFF),
        (True, 225, 2, HVACMode.HEAT),          # valid temp, not recirculation
        (True, 225, 1, HVACMode.FAN_ONLY),       # recirculation blocks heater
        (True, -1000, 2, HVACMode.FAN_ONLY),     # sentinel = heater off
    ],
)
def test_hvac_mode_truth_table(pwr_on, u_temp_room, damp_pos, expected_mode):
    ent, _, _ = _make_entity({"pwr_on": pwr_on, "u_temp_room": u_temp_room, "damp_pos": damp_pos})
    assert ent.hvac_mode == expected_mode


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_off_uses_default_20():
    """HEAT from off with no prior heat temp uses 20.0°C default."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False, "u_temp_room": -1000})
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 20.0)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_off_uses_last_heat_temp():
    """HEAT from off with a prior heat temp remembered uses that temp."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False, "u_temp_room": -1000})
    ent._last_heat_temp = 24.0
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 24.0)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_off_uses_current_u_temp_room():
    """HEAT from off with a valid u_temp_room in state uses that temp."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False, "u_temp_room": 230})
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_fan_only_from_on_calls_heater_off():
    """FAN_ONLY idempotently powers on before disabling the heater."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": True, "u_temp_room": 225})
    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.set_heater_off.assert_awaited_once_with(1)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_fan_only_from_off_uses_safe_sequential_writes():
    """FAN_ONLY powers on before sending the heater-off sentinel."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_heater_off.assert_awaited_once_with(1)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_on_calls_set_target_temperature():
    """HEAT when reported on idempotently powers on, then sends only the target.

    A multi-field PUT that repeats the unchanged u_pwr_on makes the device
    drop u_temp_room, so the safe retry uses two single-field writes. Repeating
    power first also recovers if an earlier OFF confirmation was stale.
    """
    ent, cond, api, runtime = _make_entity_with_runtime(
        {"pwr_on": True, "u_temp_room": -1000}
    )
    ent._last_heat_temp = 22.0
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 22.0)
    api.set_power_and_heat.assert_not_awaited()
    # u_temp_room pending must still be recorded so hvac_mode shows HEAT
    pending = runtime.get_pending(1, "u_temp_room")
    assert pending is not None and pending.value == 220


def test_resolve_heat_target_prefers_current_u_temp_room():
    ent, _, _ = _make_entity({"u_temp_room": 245})
    assert ent._resolve_heat_target() == pytest.approx(24.5)


def test_resolve_heat_target_falls_back_to_last_heat_temp():
    ent, _, _ = _make_entity({"u_temp_room": -1000})
    ent._last_heat_temp = 21.0
    assert ent._resolve_heat_target() == pytest.approx(21.0)


def test_resolve_heat_target_defaults_to_20():
    ent, _, _ = _make_entity({"u_temp_room": -1000})
    assert ent._resolve_heat_target() == pytest.approx(20.0)


def test_remember_confirmed_heat_target_updates_last_heat_temp():
    ent, _, _ = _make_entity({"u_temp_room": 235})
    _ = ent.target_temperature
    assert ent._last_heat_temp is None

    ent._remember_confirmed_heat_target()
    assert ent._last_heat_temp == pytest.approx(23.5)


@pytest.mark.asyncio
async def test_temperature_from_off_is_one_logical_command_with_one_refresh():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    api.set_power_and_heat.assert_not_awaited()
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent.hvac_mode == HVACMode.HEAT


@pytest.mark.asyncio
async def test_fan_only_from_off_tracks_power_and_heater_fields():
    ent, _cond, _api, runtime = _make_entity_with_runtime({"pwr_on": False})

    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)

    assert runtime.command_executor.value_with_pending(1, "pwr_on", False) is True
    assert runtime.command_executor.value_with_pending(
        1, "u_temp_room", 225
    ) == -1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "field"),
    [
        (lambda ent: ent.async_set_temperature(temperature="bad"), "temperature"),
        (lambda ent: ent.async_set_fan_mode("8"), "fan_mode"),
        (lambda ent: ent.async_set_swing_mode("bad"), "swing_mode"),
        (lambda ent: ent.async_set_humidifier_stage(4), "humidifier_stage"),
    ],
)
async def test_climate_invalid_inputs_raise_translated_validation_error(call, field):
    ent, _cond, api, _runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await call(ent)

    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == field
    api.set_power.assert_not_awaited()
    api.set_fan_speed.assert_not_awaited()
    api.set_breezer_mode.assert_not_awaited()
    api.set_humid_stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_humidifier_raises_unsupported_feature():
    ent, cond, api, _runtime = _make_entity_with_runtime()
    cond.pop("hum_stg")

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_humidity(50)

    assert raised.value.translation_key == "unsupported_device_feature"
    api.set_humid_stage.assert_not_awaited()


def test_target_temperature_property_is_pure_and_hook_records_target():
    ent, _cond, _api, _runtime = _make_entity_with_runtime(
        {"pwr_on": True, "u_temp_room": 225}
    )

    assert ent.target_temperature == 22.5
    assert ent._last_heat_temp is None

    ent._remember_confirmed_heat_target()
    assert ent._last_heat_temp == 22.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "field"),
    [
        (lambda ent: ent.async_set_temperature(temperature=True), "temperature"),
        (lambda ent: ent.async_set_temperature(temperature=math.nan), "temperature"),
        (lambda ent: ent.async_set_temperature(temperature=math.inf), "temperature"),
        (lambda ent: ent.async_set_temperature(temperature=10**10000), "temperature"),
        (lambda ent: ent.async_set_temperature(temperature=object()), "temperature"),
        (lambda ent: ent.async_set_humidity(True), "humidity"),
        (lambda ent: ent.async_set_humidity(math.nan), "humidity"),
        (lambda ent: ent.async_set_humidity(math.inf), "humidity"),
        (lambda ent: ent.async_set_humidity(10**10000), "humidity"),
        (lambda ent: ent.async_set_humidity(object()), "humidity"),
        (lambda ent: ent.async_set_fan_mode(True), "fan_mode"),
        (lambda ent: ent.async_set_swing_mode(object()), "swing_mode"),
        (lambda ent: ent.async_set_humidifier_stage(True), "humidifier_stage"),
        (lambda ent: ent.async_set_humidifier_stage(10**10000), "humidifier_stage"),
        (lambda ent: ent.async_set_hvac_mode("bad"), "hvac_mode"),
    ],
)
async def test_climate_malformed_values_only_raise_translated_validation(
    call, field
):
    ent, _cond, api, runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await call(ent)

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == field
    for api_method in (
        api.set_power,
        api.set_power_and_heat,
        api.set_target_temperature,
        api.set_humid_stage,
        api.set_fan_speed,
        api.set_breezer_mode,
        api.set_heater_off,
    ):
        api_method.assert_not_awaited()
    runtime.refresh_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_temperature_pending_map_includes_power_when_already_on():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": True})

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    assert runtime.get_pending(1, "pwr_on").value is True
    assert runtime.get_pending(1, "u_temp_room").value == 230


@pytest.mark.asyncio
async def test_queued_temperature_succeeds_after_owner_power_write_failure():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    first_write_started = asyncio.Event()
    release_first_write = asyncio.Event()
    legacy_power_calls = 0

    async def set_power(device_id, power):
        nonlocal legacy_power_calls
        legacy_power_calls += 1
        if legacy_power_calls == 1:
            first_write_started.set()
            await release_first_write.wait()
            raise ApiError("set_power", "first write failed", status=503)

    api.set_power.side_effect = set_power

    first = asyncio.create_task(
        ent.async_set_temperature(**{ATTR_TEMPERATURE: 20.0})
    )
    await first_write_started.wait()
    first_generation = runtime.get_pending(1, "pwr_on").generation
    second = asyncio.create_task(
        ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if runtime.get_pending(1, "pwr_on").generation != first_generation:
            break

    assert runtime.get_pending(1, "pwr_on").generation != first_generation
    release_first_write.set()
    with pytest.raises(HomeAssistantError):
        await first
    await second

    assert api.set_power.await_args_list == [call(1, True), call(1, True)]
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("next_action", "expected_method", "expected_value"),
    [
        (
            lambda ent: ent.async_set_temperature(temperature=23.0),
            "set_target_temperature",
            23.0,
        ),
        (
            lambda ent: ent.async_set_hvac_mode(HVACMode.HEAT),
            "set_target_temperature",
            22.5,
        ),
        (
            lambda ent: ent.async_set_hvac_mode(HVACMode.FAN_ONLY),
            "set_heater_off",
            None,
        ),
    ],
)
async def test_climate_power_on_action_survives_stale_confirmation_after_off(
    next_action, expected_method, expected_value
):
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"pwr_on": True, "u_temp_room": 225}
    )
    first_confirmation_started = asyncio.Event()
    release_first_confirmation = asyncio.Event()
    refresh_count = 0

    async def refresh_device(device_id):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 1:
            first_confirmation_started.set()
            await release_first_confirmation.wait()
            raise ApiError("get_device", "confirmation failed", status=503)

    runtime.refresh_device.side_effect = refresh_device

    turn_off = asyncio.create_task(ent.async_set_hvac_mode(HVACMode.OFF))
    await first_confirmation_started.wait()
    queued = asyncio.create_task(next_action(ent))
    await asyncio.sleep(0)

    assert runtime.get_pending(1, "pwr_on").value is True
    release_first_confirmation.set()
    await asyncio.gather(turn_off, queued)

    assert api.set_power.await_args_list == [call(1, False), call(1, True)]
    api.set_power_and_heat.assert_not_awaited()
    if expected_method == "set_target_temperature":
        api.set_target_temperature.assert_awaited_once_with(1, expected_value)
        api.set_heater_off.assert_not_awaited()
    else:
        api.set_heater_off.assert_awaited_once_with(1)
        api.set_target_temperature.assert_not_awaited()
    assert refresh_count == 2


@pytest.mark.asyncio
async def test_temperature_avoids_compound_write_after_stale_power_on_confirmation():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    first_confirmation_started = asyncio.Event()
    release_first_confirmation = asyncio.Event()
    refresh_count = 0

    async def refresh_device(device_id):
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 1:
            first_confirmation_started.set()
            await release_first_confirmation.wait()
            raise ApiError("get_device", "confirmation failed", status=503)

    runtime.refresh_device.side_effect = refresh_device

    first = asyncio.create_task(
        ent.async_set_temperature(**{ATTR_TEMPERATURE: 20.0})
    )
    await first_confirmation_started.wait()
    queued = asyncio.create_task(
        ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    )
    await asyncio.sleep(0)

    assert runtime.get_pending(1, "pwr_on").value is True
    release_first_confirmation.set()
    await asyncio.gather(first, queued)

    assert api.set_power.await_args_list == [call(1, True), call(1, True)]
    assert api.set_target_temperature.await_args_list == [
        call(1, 20.0),
        call(1, 23.0),
    ]
    api.set_power_and_heat.assert_not_awaited()
    assert refresh_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "failing_method", "pending_field"),
    [
        (
            lambda ent: ent.async_set_temperature(temperature=23.0),
            "set_target_temperature",
            "u_temp_room",
        ),
        (
            lambda ent: ent.async_set_hvac_mode(HVACMode.FAN_ONLY),
            "set_heater_off",
            "u_temp_room",
        ),
    ],
)
async def test_climate_partial_second_write_failure_recovers_and_clears_pending(
    action, failing_method, pending_field
):
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": True})
    api_error = ApiError(failing_method, "second write failed", status=503)
    getattr(api, failing_method).side_effect = api_error

    with pytest.raises(HomeAssistantError) as raised:
        await action(ent)

    api.set_power.assert_awaited_once_with(1, True)
    getattr(api, failing_method).assert_awaited_once()
    runtime.refresh_device.assert_awaited_once_with(1)
    assert raised.value.translation_key == "command_failed"
    assert raised.value.__cause__ is api_error
    assert runtime.get_pending(1, "pwr_on") is None
    assert runtime.get_pending(1, pending_field) is None


@pytest.mark.asyncio
async def test_climate_partial_second_write_cancellation_recovers_and_clears_pending():
    ent, _cond, api, runtime = _make_entity_with_runtime({"pwr_on": True})
    target_write_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_target_write(device_id, target):
        target_write_started.set()
        await never_release.wait()

    api.set_target_temperature.side_effect = blocked_target_write
    task = asyncio.create_task(
        ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})
    )
    await target_write_started.wait()

    assert runtime.get_pending(1, "pwr_on").value is True
    assert runtime.get_pending(1, "u_temp_room").value == 230
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert runtime.get_pending(1, "pwr_on") is None
    assert runtime.get_pending(1, "u_temp_room") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_method", "action", "expected_action"),
    [
        (
            "set_power",
            lambda ent: ent.async_set_hvac_mode(HVACMode.OFF),
            "turn off climate control",
        ),
        (
            "set_fan_speed",
            lambda ent: ent.async_set_fan_mode("5"),
            "set climate fan mode",
        ),
        (
            "set_breezer_mode",
            lambda ent: ent.async_set_swing_mode(BREEZER_SWING_MODES[1]),
            "set breezer mode",
        ),
    ],
)
async def test_climate_api_failures_preserve_translated_error_metadata(
    api_method, action, expected_action
):
    ent, _cond, api, runtime = _make_entity_with_runtime()
    api_error = ApiError(api_method, "write failed", status=503)
    getattr(api, api_method).side_effect = api_error

    with pytest.raises(HomeAssistantError) as raised:
        await action(ent)

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "command_failed"
    assert raised.value.translation_placeholders == {"action": expected_action}
    assert raised.value.__cause__ is api_error
    runtime.refresh_device.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_sleep_preset_uses_one_refresh_for_mode_and_speed():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    ent.async_write_ha_state = MagicMock()

    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_sleep_mode.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_awaited_once_with(1, 2)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert runtime.command_executor.value_with_pending(
        1, "u_night", False
    ) is True
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_preset_mode_reads_pending_flags_during_delayed_confirmation():
    ent, _cond, _api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def blocked_refresh(_device_id):
        refresh_started.set()
        await release_refresh.wait()

    runtime.refresh_device.side_effect = blocked_refresh
    task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await refresh_started.wait()

    assert ent.preset_mode == PRESET_SLEEP

    release_refresh.set()
    await task


@pytest.mark.asyncio
async def test_rapid_sleep_to_auto_transition_uses_pending_previous_mode():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    first_refresh_started = asyncio.Event()
    release_first_refresh = asyncio.Event()
    refresh_calls = 0

    async def controlled_refresh(_device_id):
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            first_refresh_started.set()
            await release_first_refresh.wait()

    runtime.refresh_device.side_effect = controlled_refresh
    sleep_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await first_refresh_started.wait()
    auto_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_AUTO))
    await asyncio.sleep(0)

    assert ent.preset_mode == PRESET_AUTO

    release_first_refresh.set()
    await asyncio.gather(sleep_task, auto_task)
    api.set_sleep_mode.assert_has_awaits([call(1, True), call(1, False)])
    api.set_auto_mode.assert_has_awaits([call(1, False), call(1, True)])
    api.set_fan_speed.assert_has_awaits([call(1, 2), call(1, 4)])
    assert refresh_calls == 2
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_preset_partial_failure_refreshes_once_before_error():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False}
    )
    ent.async_write_ha_state = MagicMock()
    api.set_fan_speed.side_effect = ApiError(
        "test_preset", "second write failed", status=503
    )

    with pytest.raises(HomeAssistantError):
        await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_sleep_mode.assert_awaited_once_with(1, True)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_invalid_preset_raises_service_validation_error():
    ent, _cond, api, runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_preset_mode("invalid")

    assert raised.value.translation_key == "invalid_command_value"
    api.set_auto_mode.assert_not_awaited()
    api.set_sleep_mode.assert_not_awaited()
    api.set_fan_speed.assert_not_awaited()
    runtime.refresh_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_auto_waits_for_sleep_restore_state_commit():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    sleep_speed_started = asyncio.Event()
    release_sleep_speed = asyncio.Event()

    async def controlled_fan_speed(device_id, speed):
        if speed == 2:
            sleep_speed_started.set()
            await release_sleep_speed.wait()

    api.set_fan_speed.side_effect = controlled_fan_speed
    sleep_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await sleep_speed_started.wait()
    auto_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_AUTO))
    await asyncio.sleep(0)

    assert ent.preset_mode == PRESET_AUTO
    assert ent._saved_fan_mode is None
    release_sleep_speed.set()
    await asyncio.gather(sleep_task, auto_task)

    api.set_sleep_mode.assert_has_awaits([call(1, True), call(1, False)])
    api.set_fan_speed.assert_has_awaits([call(1, 2), call(1, 4)])
    api.set_auto_mode.assert_has_awaits([call(1, False), call(1, True)])
    assert runtime.refresh_device.await_count == 2
    assert ent._saved_fan_mode is None
    assert ent._is_boost is False
    assert ent._local_preset is None


@pytest.mark.asyncio
async def test_preset_second_write_cancellation_keeps_local_state_uncommitted():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    speed_write_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_fan_speed(device_id, speed):
        speed_write_started.set()
        await never_release.wait()

    api.set_fan_speed.side_effect = blocked_fan_speed
    task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await speed_write_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    api.set_sleep_mode.assert_awaited_once_with(1, True)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent._saved_fan_mode is None
    assert ent._is_boost is False
    assert ent._local_preset is None
    assert runtime.get_pending(1, "u_night") is None
    assert runtime.get_pending(1, "u_auto") is None
    assert runtime.get_pending(1, "local_preset") is None


class _BadPresetString:
    def __str__(self) -> str:
        raise RuntimeError("must not escape validation")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_preset",
    [None, True, 1, object(), _BadPresetString()],
)
async def test_malformed_preset_only_raises_translated_validation(invalid_preset):
    ent, _cond, api, runtime = _make_entity_with_runtime()

    with pytest.raises(ServiceValidationError) as raised:
        await ent.async_set_preset_mode(invalid_preset)

    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == "preset_mode"
    api.set_auto_mode.assert_not_awaited()
    api.set_sleep_mode.assert_not_awaited()
    api.set_fan_speed.assert_not_awaited()
    runtime.refresh_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_boost_exit_preserves_complete_local_restore_state():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 7, "u_night": False, "u_auto": False}
    )
    ent._saved_fan_mode = "3"
    ent._is_boost = True
    ent._local_preset = PRESET_BOOST
    api.set_fan_speed.side_effect = ApiError(
        "set_fan_speed", "restore failed", status=503
    )

    with pytest.raises(HomeAssistantError):
        await ent.async_set_preset_mode(PRESET_AUTO)

    api.set_sleep_mode.assert_awaited_once_with(1, False)
    api.set_auto_mode.assert_awaited_once_with(1, True)
    runtime.refresh_device.assert_awaited_once_with(1)
    assert ent._saved_fan_mode == "3"
    assert ent._is_boost is True
    assert ent._local_preset == PRESET_BOOST


@pytest.mark.asyncio
async def test_pending_auto_overrides_committed_boost_during_restore_write():
    ent, _cond, api, _runtime = _make_entity_with_runtime(
        {"fan_speed": 7, "u_night": False, "u_auto": False}
    )
    ent._saved_fan_mode = "3"
    ent._is_boost = True
    ent._local_preset = PRESET_BOOST
    restore_started = asyncio.Event()
    release_restore = asyncio.Event()

    async def blocked_restore(device_id, speed):
        restore_started.set()
        await release_restore.wait()

    api.set_fan_speed.side_effect = blocked_restore
    task = asyncio.create_task(ent.async_set_preset_mode(PRESET_AUTO))
    await restore_started.wait()

    assert ent.preset_mode == PRESET_AUTO

    release_restore.set()
    await task
    assert ent._saved_fan_mode is None
    assert ent._is_boost is False
    assert ent._local_preset is None


@pytest.mark.asyncio
async def test_queued_sleep_disables_auto_after_predecessor_disable_fails():
    ent, _cond, api, _runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": True}
    )
    first_disable_started = asyncio.Event()
    release_first_disable = asyncio.Event()
    disable_calls = 0

    async def controlled_auto_mode(device_id: int | str, enabled: bool) -> None:
        nonlocal disable_calls
        if enabled:
            return
        disable_calls += 1
        if disable_calls == 1:
            first_disable_started.set()
            await release_first_disable.wait()
            raise ApiError("set_auto_mode", "disable failed", status=503)

    api.set_auto_mode.side_effect = controlled_auto_mode
    none_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_NONE))
    await first_disable_started.wait()
    sleep_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await asyncio.sleep(0)

    release_first_disable.set()
    none_result, sleep_result = await asyncio.gather(
        none_task,
        sleep_task,
        return_exceptions=True,
    )

    assert isinstance(none_result, HomeAssistantError)
    assert sleep_result is None
    api.set_auto_mode.assert_has_awaits([call(1, False), call(1, False)])
    api.set_sleep_mode.assert_awaited_with(1, True)


@pytest.mark.asyncio
async def test_queued_none_retries_failed_boost_restore_and_enforces_flags():
    ent, _cond, api, _runtime = _make_entity_with_runtime(
        {"fan_speed": 7, "u_night": False, "u_auto": False}
    )
    ent._saved_fan_mode = "3"
    ent._is_boost = True
    ent._local_preset = PRESET_BOOST
    first_restore_started = asyncio.Event()
    release_first_restore = asyncio.Event()
    restore_calls = 0

    async def controlled_fan_speed(device_id: int | str, speed: int) -> None:
        nonlocal restore_calls
        if speed != 3:
            return
        restore_calls += 1
        if restore_calls == 1:
            first_restore_started.set()
            await release_first_restore.wait()
            raise ApiError("set_fan_speed", "restore failed", status=503)

    api.set_fan_speed.side_effect = controlled_fan_speed
    auto_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_AUTO))
    await first_restore_started.wait()
    none_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_NONE))
    await asyncio.sleep(0)

    release_first_restore.set()
    auto_result, none_result = await asyncio.gather(
        auto_task,
        none_task,
        return_exceptions=True,
    )

    assert isinstance(auto_result, HomeAssistantError)
    assert none_result is None
    api.set_auto_mode.assert_awaited_with(1, False)
    api.set_sleep_mode.assert_awaited_with(1, False)
    api.set_fan_speed.assert_has_awaits([call(1, 3), call(1, 3)])
    assert ent._saved_fan_mode is None
    assert ent._is_boost is False
    assert ent._local_preset is None


@pytest.mark.asyncio
async def test_sleep_reentry_keeps_restore_speed_after_exit_confirmation_failure():
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 2, "u_night": True, "u_auto": False}
    )
    ent._saved_fan_mode = "4"
    runtime.refresh_device.side_effect = [
        ApiError("refresh", "confirmation failed", status=503),
        None,
    ]

    await ent.async_set_preset_mode(PRESET_NONE)
    assert ent._saved_fan_mode is None

    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_fan_speed.assert_has_awaits([call(1, 4), call(1, 2)])
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_sleep_reentry_keeps_restore_speed_after_pending_ttl_expires(
    monkeypatch,
):
    now = 100.0
    monkeypatch.setattr(
        command_executor_module.time,
        "monotonic",
        lambda: now,
    )
    ent, cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 2, "u_night": True, "u_auto": False}
    )
    ent._saved_fan_mode = "4"
    runtime.refresh_device.side_effect = [
        ApiError("refresh", "confirmation failed", status=503),
        None,
    ]

    await ent.async_set_preset_mode(PRESET_NONE)
    assert ent._saved_fan_mode is None

    now = 120.0
    cond["u_night"] = False
    assert runtime.get_pending(1, "fan_speed") is None
    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_fan_speed.assert_has_awaits([call(1, 4), call(1, 2)])
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_restore_intent_survives_ttl_expiry_during_failed_confirmation(
    monkeypatch,
):
    now = 100.0
    monkeypatch.setattr(
        command_executor_module.time,
        "monotonic",
        lambda: now,
    )
    ent, cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 2, "u_night": True, "u_auto": False}
    )
    ent._saved_fan_mode = "4"
    confirmation_started = asyncio.Event()
    release_confirmation = asyncio.Event()
    refresh_calls = 0

    async def controlled_refresh(_device_id: int | str) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            confirmation_started.set()
            await release_confirmation.wait()
            raise ApiError("refresh", "confirmation failed", status=503)

    runtime.refresh_device.side_effect = controlled_refresh
    exit_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_NONE))
    await confirmation_started.wait()

    now = 120.0
    assert runtime.get_pending(1, "fan_speed") is None
    release_confirmation.set()
    await exit_task

    cond["u_night"] = False
    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_fan_speed.assert_has_awaits([call(1, 4), call(1, 2)])
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_restore_intent_survives_cancellation_during_confirmation_recovery():
    ent, cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 2, "u_night": True, "u_auto": False}
    )
    ent._saved_fan_mode = "4"
    confirmation_started = asyncio.Event()
    recovery_started = asyncio.Event()
    never_release = asyncio.Event()
    refresh_calls = 0

    async def controlled_refresh(_device_id: int | str) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            confirmation_started.set()
            await never_release.wait()
        elif refresh_calls == 2:
            recovery_started.set()
            await never_release.wait()

    runtime.refresh_device.side_effect = controlled_refresh
    exit_task = asyncio.create_task(ent.async_set_preset_mode(PRESET_NONE))
    await confirmation_started.wait()
    exit_task.cancel()
    await recovery_started.wait()
    exit_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await exit_task

    cond["u_night"] = False
    await ent.async_set_preset_mode(PRESET_SLEEP)

    api.set_fan_speed.assert_has_awaits([call(1, 4), call(1, 2)])
    assert ent._saved_fan_mode == "4"


@pytest.mark.asyncio
async def test_manual_fan_intent_supersedes_old_restore_after_failed_confirmation(
    monkeypatch,
):
    now = 100.0
    monkeypatch.setattr(
        command_executor_module.time,
        "monotonic",
        lambda: now,
    )
    ent, _cond, api, runtime = _make_entity_with_runtime(
        {"fan_speed": 3, "u_night": False, "u_auto": False}
    )
    ent._unconfirmed_restore_fan_mode = "4"
    runtime.refresh_device.side_effect = [
        ApiError("refresh", "manual confirmation failed", status=503),
        None,
        None,
    ]

    await ent.async_set_fan_mode("5")
    now = 120.0
    assert runtime.get_pending(1, "fan_speed") is None
    await ent.async_set_preset_mode(PRESET_SLEEP)
    await ent.async_set_preset_mode(PRESET_NONE)

    api.set_fan_speed.assert_has_awaits(
        [call(1, 5), call(1, 2), call(1, 5)]
    )
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_manual_fan_confirmation_clears_durable_intent():
    ent, cond, _api, runtime = _make_entity_with_runtime(
        {"fan_speed": 3, "u_night": False, "u_auto": False}
    )
    ent._unconfirmed_restore_fan_mode = "4"

    async def confirmed_refresh(_device_id: int | str) -> None:
        cond["fan_speed"] = 5

    runtime.refresh_device.side_effect = confirmed_refresh

    await ent.async_set_fan_mode("5")

    assert ent._unconfirmed_restore_fan_mode is None


@pytest.mark.asyncio
async def test_manual_fan_intent_survives_cancelled_confirmation():
    ent, _cond, _api, runtime = _make_entity_with_runtime(
        {"fan_speed": 3, "u_night": False, "u_auto": False}
    )
    ent._unconfirmed_restore_fan_mode = "4"
    confirmation_started = asyncio.Event()
    never_release = asyncio.Event()
    refresh_calls = 0

    async def controlled_refresh(_device_id: int | str) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        if refresh_calls == 1:
            confirmation_started.set()
            await never_release.wait()

    runtime.refresh_device.side_effect = controlled_refresh
    task = asyncio.create_task(ent.async_set_fan_mode("5"))
    await confirmation_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert ent._unconfirmed_restore_fan_mode == "5"


@pytest.mark.asyncio
async def test_manual_fan_after_failed_queued_sleep_supersedes_restore_baseline():
    ent, _cond, api, _runtime = _make_entity_with_runtime(
        {"fan_speed": 3, "u_night": False, "u_auto": False}
    )
    ent._unconfirmed_restore_fan_mode = "4"
    first_sleep_flag_started = asyncio.Event()
    release_first_sleep_flag = asyncio.Event()
    auto_flag_calls = 0

    async def controlled_auto_mode(device_id: int | str, enabled: bool) -> None:
        nonlocal auto_flag_calls
        auto_flag_calls += 1
        if auto_flag_calls == 1:
            assert enabled is False
            first_sleep_flag_started.set()
            await release_first_sleep_flag.wait()
            raise ApiError("set_auto_mode", "sleep setup failed", status=503)

    api.set_auto_mode.side_effect = controlled_auto_mode
    failed_sleep = asyncio.create_task(
        ent.async_set_preset_mode(PRESET_SLEEP)
    )
    await first_sleep_flag_started.wait()
    manual_fan = asyncio.create_task(ent.async_set_fan_mode("5"))
    await asyncio.sleep(0)
    assert ent.preset_mode == PRESET_SLEEP

    release_first_sleep_flag.set()
    sleep_result, fan_result = await asyncio.gather(
        failed_sleep,
        manual_fan,
        return_exceptions=True,
    )

    assert isinstance(sleep_result, HomeAssistantError)
    assert fan_result is None
    await ent.async_set_preset_mode(PRESET_SLEEP)
    await ent.async_set_preset_mode(PRESET_NONE)

    api.set_fan_speed.assert_has_awaits(
        [call(1, 5), call(1, 2), call(1, 5)]
    )
    assert ent._saved_fan_mode is None


@pytest.mark.asyncio
async def test_sleep_fan_target_is_pending_during_blocked_confirmation():
    ent, _cond, _api, runtime = _make_entity_with_runtime(
        {"fan_speed": 4, "u_night": False, "u_auto": False}
    )
    confirmation_started = asyncio.Event()
    release_confirmation = asyncio.Event()

    async def blocked_refresh(_device_id: int | str) -> None:
        confirmation_started.set()
        await release_confirmation.wait()

    runtime.refresh_device.side_effect = blocked_refresh
    task = asyncio.create_task(ent.async_set_preset_mode(PRESET_SLEEP))
    await confirmation_started.wait()

    fan_mode_during_confirmation = ent.fan_mode

    release_confirmation.set()
    await task
    assert fan_mode_during_confirmation == "2"
