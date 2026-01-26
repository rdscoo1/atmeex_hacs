import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.components.climate import ClimateEntityFeature, HVACMode
from homeassistant.const import ATTR_TEMPERATURE
from typing import Any

from custom_components.atmeex_cloud.climate import (
    AtmeexClimateEntity,
    quantize_humidity,
    HUM_ALLOWED,
    BRIZER_SWING_MODES,
)
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.api import AtmeexDevice


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
        set_brizer_mode=AsyncMock(),
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


def test_climate_basic_properties():
    ent, cond, api = _make_entity()

    assert ent.available is True

    # поддерживаем TARGET_HUMIDITY при наличии hum_stg
    assert ent.supported_features & ClimateEntityFeature.TARGET_HUMIDITY

    assert ent.hvac_mode == HVACMode.FAN_ONLY

    assert ent.current_temperature == pytest.approx(21.5)
    assert ent.target_temperature == pytest.approx(22.5)

    assert ent.current_humidity == 45
    assert ent.target_humidity == HUM_ALLOWED[1]  # 33

    assert ent.fan_mode == "3"
    assert ent.swing_mode == BRIZER_SWING_MODES[2]

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
    # нет цели, есть текущая температура
    ent, cond, api = _make_entity({"u_temp_room": None})
    assert ent.target_temperature == pytest.approx(21.5)

    # нет ни цели, ни текущей — дефолт 20.0
    ent2, cond2, api2 = _make_entity({"u_temp_room": None, "temp_room": None})
    assert ent2.target_temperature == 20.0


def test_climate_fan_mode_invalid():
    ent, cond, api = _make_entity({"fan_speed": 10})
    assert ent.fan_mode is None


def test_climate_swing_mode_invalid():
    ent, cond, api = _make_entity({"damp_pos": 5})
    assert ent.swing_mode is None


@pytest.mark.asyncio
async def test_async_set_hvac_mode_calls_api_and_refresh():
    ent, cond, api = _make_entity()
    ent._refresh = AsyncMock()

    await ent.async_set_hvac_mode(HVACMode.FAN_ONLY)
    api.set_power.assert_awaited_once_with(1, True)
    ent._refresh.assert_awaited_once()

    ent._refresh.reset_mock()
    api.set_power.reset_mock()

    await ent.async_set_hvac_mode(HVACMode.OFF)
    api.set_power.assert_awaited_once_with(1, False)
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

    mode = BRIZER_SWING_MODES[1]
    await ent.async_set_swing_mode(mode)
    api.set_brizer_mode.assert_awaited_once_with(1, 1)
    ent._refresh.assert_awaited_once()

    api.set_brizer_mode.reset_mock()
    ent._refresh.reset_mock()

    await ent.async_set_swing_mode("неизвестный режим")
    assert api.set_brizer_mode.await_count == 0
    ent._refresh.assert_not_awaited()
