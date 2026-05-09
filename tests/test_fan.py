import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.fan import async_setup_entry
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice


def _make_fan_entity():
    # В интеграции fan_speed — дискрет 0..7, который мы мапим в проценты
    # speed=3 → percentage ≈ 43 (3 * 100 / 7)
    cond = {
        "pwr_on": True,
        "fan_speed": 3,
    }
    coordinator = SimpleNamespace(
        data={"states": {"1": cond}},
        async_request_refresh=AsyncMock(),
    )
    api = MagicMock()
    api.set_fan_speed = AsyncMock()
    api.set_power = AsyncMock()

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )

    # runtime=None for backward compatibility in simple tests
    fan = AtmeexFanEntity(coordinator, api, "entry1", dev, runtime=None)
    return fan, cond, api, coordinator


def test_fan_basic_properties():
    fan, cond, api, coord = _make_fan_entity()

    # is_on берётся из pwr_on
    assert fan.is_on is True

    # percentage — уже отображение fan_speed (3) в проценты
    # 3 * 100 / 7 ≈ 42.857 → round → 43
    assert fan.percentage == 43


@pytest.mark.asyncio
async def test_fan_async_set_percentage():
    fan, cond, api, coord = _make_fan_entity()

    # Просим 75% — внутри оно мапится в скоростной дискрет 1..7
    # 75 * 7 / 100 = 5.25 → round → 5
    await fan.async_set_percentage(75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_not_awaited()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_async_set_percentage_turns_on_when_currently_off():
    fan, cond, api, coord = _make_fan_entity()
    cond["pwr_on"] = False

    await fan.async_set_percentage(75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    assert coord.async_request_refresh.await_count == 2



@pytest.mark.asyncio
async def test_fan_async_turn_on_no_percentage_only_calls_set_power():
    fan, cond, api, coord = _make_fan_entity()

    await fan.async_turn_on()

    api.set_power.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_not_awaited()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_async_turn_on_with_percentage_sets_speed_then_power():
    fan, cond, api, coord = _make_fan_entity()

    # 75% → speed 5 (same mapping checked in test_fan_async_set_percentage)
    await fan.async_turn_on(percentage=75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    assert coord.async_request_refresh.await_count == 2


@pytest.mark.asyncio
async def test_fan_async_turn_off_uses_set_power():
    fan, cond, api, coord = _make_fan_entity()

    await fan.async_turn_off()

    api.set_power.assert_awaited_once_with(1, False)
    api.set_fan_speed.assert_not_awaited()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_async_setup_entry_adds_entities_for_new_devices():
    dev1 = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    dev2 = AtmeexDevice.from_raw(
        {"id": 2, "name": "Dev2", "model": "m", "online": True}
    )
    listeners: list = []
    coordinator = SimpleNamespace(
        data={
            "device_map": {"1": dev1},
            "states": {"1": {"pwr_on": True, "fan_speed": 3}},
        },
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    api = MagicMock()
    runtime = AtmeexRuntimeData(
        api=api,
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=runtime,
        async_on_unload=lambda cb: None,
    )

    entities: list[AtmeexFanEntity] = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(None, entry, _add_entities)

    assert [entity.unique_id for entity in entities] == ["1_fan"]
    assert len(listeners) == 1

    coordinator.data["device_map"]["2"] = dev2
    coordinator.data["states"]["2"] = {"pwr_on": False, "fan_speed": 1}
    listeners[0]()

    assert sorted(entity.unique_id for entity in entities) == ["1_fan", "2_fan"]

    listeners[0]()
    assert len(entities) == 2
