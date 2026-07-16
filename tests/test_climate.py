import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.components.climate.const import PRESET_BOOST, PRESET_NONE, PRESET_SLEEP
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.exceptions import HomeAssistantError
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
        (HVACMode.FAN_ONLY, False, "set_power_and_heat"),
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
        api.set_power.assert_not_awaited()
        api.set_power_and_heat.assert_not_awaited()
    else:
        api.set_power_and_heat.assert_awaited_once_with(1, True, None)
        api.set_power.assert_not_awaited()
        api.set_heater_off.assert_not_awaited()
    ent._refresh.assert_awaited_once()





@pytest.mark.asyncio
async def test_async_set_temperature_turns_on_if_needed():
    # кондишн с выключенным устройством
    ent, cond, api = _make_entity({"pwr_on": False})
    ent._refresh = AsyncMock()

    await ent.async_set_temperature(**{ATTR_TEMPERATURE: 23.0})

    api.set_power.assert_awaited_once_with(1, True)
    api.set_target_temperature.assert_awaited_once_with(1, 23.0)
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
async def test_async_set_temperature_ignores_missing_value():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    await ent.async_set_temperature()

    # ничего не должно происходить
    assert api.set_target_temperature.await_count == 0
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
async def test_async_set_humidity_no_humidifier_noop():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()
    # убираем признак увлажнителя
    cond.pop("hum_stg", None)
    ent.coordinator.data["states"]["1"] = cond

    await ent.async_set_humidity(50)

    assert api.set_humid_stage.await_count == 0
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

    # нечисловой режим — предупреждение и no-op
    await ent.async_set_fan_mode("invalid")
    assert api.set_fan_speed.await_count == 0
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

    await ent.async_set_swing_mode("неизвестный режим")
    assert api.set_breezer_mode.await_count == 0
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
async def test_service_set_humidifier_stage_no_humidifier_is_noop():
    """When the device has no humidifier, the API is never called."""
    # Remove hum_stg from state so _has_humidifier() returns False
    ent, _cond, api = _make_entity({})
    del ent.coordinator.data["states"]["1"]["hum_stg"]
    await ent.async_set_humidifier_stage(1)
    api.set_humid_stage.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,expected", [(-1, 0), (0, 0), (3, 3), (4, 3)])
async def test_service_set_humidifier_stage_clamps_value(stage, expected):
    """Stage is clamped to 0–3 before being sent to the API."""
    ent, _cond, api = _make_entity({"hum_stg": 1})
    await ent.async_set_humidifier_stage(stage)
    api.set_humid_stage.assert_awaited_once_with(1, expected)


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
    api.set_power_and_heat.assert_awaited_once_with(1, True, 20.0)


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_off_uses_last_heat_temp():
    """HEAT from off with a prior heat temp remembered uses that temp."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False, "u_temp_room": -1000})
    ent._last_heat_temp = 24.0
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power_and_heat.assert_awaited_once_with(1, True, 24.0)


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_off_uses_current_u_temp_room():
    """HEAT from off with a valid u_temp_room in state uses that temp."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False, "u_temp_room": 230})
    await ent.async_set_hvac_mode(HVACMode.HEAT)
    api.set_power_and_heat.assert_awaited_once_with(1, True, 23.0)


@pytest.mark.asyncio
async def test_set_hvac_mode_fan_only_from_on_calls_heater_off():
    """FAN_ONLY when device is already on calls set_heater_off, not set_power."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": True, "u_temp_room": 225})
    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.set_heater_off.assert_awaited_once_with(1)
    api.set_power.assert_not_awaited()
    api.set_power_and_heat.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_hvac_mode_fan_only_from_off_calls_power_and_heat_none():
    """FAN_ONLY when device is off uses set_power_and_heat(True, None)."""
    ent, cond, api, runtime = _make_entity_with_runtime({"pwr_on": False})
    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.set_power_and_heat.assert_awaited_once_with(1, True, None)


@pytest.mark.asyncio
async def test_set_hvac_mode_heat_from_on_calls_set_target_temperature():
    """HEAT when device is already on (fan-only -> heat) sends only the target.

    A multi-field PUT that repeats the unchanged u_pwr_on makes the device
    drop u_temp_room, so the heater never engages. When already on, the
    single-field set_target_temperature path must be used instead.
    """
    ent, cond, api, runtime = _make_entity_with_runtime(
        {"pwr_on": True, "u_temp_room": -1000}
    )
    ent._last_heat_temp = 22.0
    await ent.async_set_hvac_mode(HVACMode.HEAT)
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


def test_target_temperature_updates_last_heat_temp():
    ent, _, _ = _make_entity({"u_temp_room": 235})
    _ = ent.target_temperature  # access the property
    assert ent._last_heat_temp == pytest.approx(23.5)
