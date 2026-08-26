import asyncio
import math

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

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
    api.set_power.assert_awaited_once_with(1, True)
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_async_turn_on_no_percentage_only_calls_set_power():
    fan, cond, api, coord = _make_fan_entity()

    await fan.async_turn_on()

    api.set_power.assert_awaited_once_with(1, True)
    api.set_fan_speed.assert_not_awaited()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set_percentage", "turn_on"])
async def test_fan_speed_and_power_are_one_command_with_one_refresh(method):
    fan, cond, api, coord = _make_fan_entity()
    cond["pwr_on"] = False

    if method == "set_percentage":
        await fan.async_set_percentage(75)
    else:
        await fan.async_turn_on(percentage=75)

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "percentage"),
    [
        ("set_percentage", 101),
        ("turn_on", 0),
        ("set_percentage", True),
        ("turn_on", False),
        ("set_percentage", None),
        ("turn_on", "75"),
        ("set_percentage", object()),
        ("turn_on", math.nan),
        ("set_percentage", math.inf),
        ("turn_on", -math.inf),
        pytest.param("set_percentage", 10**10000, id="huge-integer"),
        ("set_percentage", -1),
        ("turn_on", 100.1),
    ],
)
async def test_fan_rejects_invalid_percentage_without_leaking_type_errors(
    method, percentage
):
    fan, _cond, api, coordinator = _make_fan_entity()

    with pytest.raises(ServiceValidationError) as raised:
        if method == "set_percentage":
            await fan.async_set_percentage(percentage)
        else:
            await fan.async_turn_on(percentage=percentage)

    assert raised.value.translation_domain == "atmeex_cloud"
    assert raised.value.translation_key == "invalid_command_value"
    assert raised.value.translation_placeholders["field"] == "percentage"
    api.set_fan_speed.assert_not_awaited()
    api.set_power.assert_not_awaited()
    coordinator.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("percentage", "expected_speed"),
    [(1, 1), (50, 4), (75.9, 5), (100, 7)],
)
async def test_fan_valid_percentage_preserves_speed_conversion(
    percentage, expected_speed
):
    fan, _cond, api, _coordinator = _make_fan_entity()

    await fan.async_set_percentage(percentage)

    api.set_fan_speed.assert_awaited_once_with(1, expected_speed)


@pytest.mark.asyncio
async def test_fan_zero_percentage_is_one_turn_off_command():
    fan, _cond, api, coordinator = _make_fan_entity()

    await fan.async_set_percentage(0)

    api.set_power.assert_awaited_once_with(1, False)
    api.set_fan_speed.assert_not_awaited()
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("percentage", [0.1, 0.5, 0.999])
async def test_fan_positive_sub_percent_maps_to_minimum_speed(percentage):
    fan, _cond, api, coordinator = _make_fan_entity()

    await fan.async_set_percentage(percentage)

    api.set_fan_speed.assert_awaited_once_with(1, 1)
    api.set_power.assert_awaited_once_with(1, True)
    coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_fan_partial_compound_api_failure_recovers_once_and_clears_pending():
    fan, cond, api, coordinator = _make_fan_entity()
    cond["pwr_on"] = False
    api.set_power.side_effect = ApiError(
        "set_power", "power write failed", status=503
    )

    with pytest.raises(HomeAssistantError) as raised:
        await fan.async_set_percentage(75)

    assert raised.value.translation_key == "command_failed"
    assert raised.value.__cause__ is api.set_power.side_effect
    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    coordinator.async_request_refresh.assert_awaited_once()
    executor = fan._fallback_command_executor
    assert executor.get_pending(1, "fan_speed") is None
    assert executor.get_pending(1, "pwr_on") is None


@pytest.mark.asyncio
async def test_fan_partial_compound_cancellation_recovers_once_and_clears_pending():
    fan, cond, api, coordinator = _make_fan_entity()
    cond["pwr_on"] = False
    power_write_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_power_write(device_id, power):
        power_write_started.set()
        await never_release.wait()

    api.set_power.side_effect = blocked_power_write
    task = asyncio.create_task(fan.async_set_percentage(75))
    await power_write_started.wait()
    executor = fan._fallback_command_executor
    assert executor.get_pending(1, "fan_speed").value == 5
    assert executor.get_pending(1, "pwr_on").value is True
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    api.set_fan_speed.assert_awaited_once_with(1, 5)
    api.set_power.assert_awaited_once_with(1, True)
    coordinator.async_request_refresh.assert_awaited_once()
    assert executor.get_pending(1, "fan_speed") is None
    assert executor.get_pending(1, "pwr_on") is None


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
