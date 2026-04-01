import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from homeassistant.exceptions import HomeAssistantError

from custom_components.atmeex_cloud import AtmeexRuntimeData
from custom_components.atmeex_cloud.select import (
    AtmeexHumidificationSelect,
    AtmeexBreezerSelect,
    HUM_OPTIONS,
    BREEZER_OPTIONS,
    async_setup_entry,
)
from custom_components.atmeex_cloud.api import ApiError, AtmeexDevice


def _make_selects(cond_overrides: dict | None = None):
    cond = {
        "hum_stg": 1,
        "damp_pos": 2,
    }
    if cond_overrides:
        cond.update(cond_overrides)

    coordinator = SimpleNamespace(
        data={"states": {"1": cond}},
        last_update_success=True,
        async_request_refresh=AsyncMock(),
    )

    api = MagicMock()
    api.set_humid_stage = AsyncMock()
    api.set_breezer_mode = AsyncMock()

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )

    hum = AtmeexHumidificationSelect(coordinator, api, dev)
    breezer = AtmeexBreezerSelect(coordinator, api, dev)

    return hum, breezer, cond, api, coordinator


def test_humidification_select_current_option_from_hum_stg():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": 2})
    assert getattr(hum, "_attr_name", None) is None
    assert hum.current_option == HUM_OPTIONS[2]
    cond["hum_stg"] = 0
    assert hum.current_option == "off"


def test_humidification_select_fallback_to_cached_option():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": "bad"})
    # нет корректного hum_stg — используется _attr_current_option (по умолчанию "off")
    assert hum.current_option == "off"
    hum._attr_current_option = "2"
    assert hum.current_option == "2"


@pytest.mark.asyncio
async def test_humidification_select_async_select_option():
    hum, breezer, cond, api, coord = _make_selects({"hum_stg": 0})
    await hum.async_select_option("3")

    api.set_humid_stage.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert hum._attr_current_option == "3"


@pytest.mark.asyncio
async def test_humidification_select_invalid_option_noop():
    hum, breezer, cond, api, coord = _make_selects()
    await hum.async_select_option("invalid")
    api.set_humid_stage.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_humidification_select_raises_homeassistant_error_on_api_failure():
    hum, breezer, cond, api, coord = _make_selects()
    api.set_humid_stage.side_effect = ApiError("boom", status=500)

    with pytest.raises(HomeAssistantError, match="Failed to set humidification stage"):
        await hum.async_select_option("2")


def test_breezer_select_current_option_from_damp_pos():
    hum, breezer, cond, api, coord = _make_selects({"damp_pos": 1})
    assert getattr(breezer, "_attr_name", None) is None
    assert breezer.current_option == BREEZER_OPTIONS[1]

    cond["damp_pos"] = 10
    # некорректное значение — берём кэш или первую опцию
    assert breezer.current_option == BREEZER_OPTIONS[0]


@pytest.mark.asyncio
async def test_breezer_select_async_select_option():
    hum, breezer, cond, api, coord = _make_selects()
    await breezer.async_select_option(BREEZER_OPTIONS[3])

    api.set_breezer_mode.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert breezer._attr_current_option == BREEZER_OPTIONS[3]


@pytest.mark.asyncio
async def test_breezer_select_invalid_option_noop():
    hum, breezer, cond, api, coord = _make_selects()
    await breezer.async_select_option("неизвестно")
    api.set_breezer_mode.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_breezer_select_raises_homeassistant_error_on_api_failure():
    hum, breezer, cond, api, coord = _make_selects()
    api.set_breezer_mode.side_effect = ApiError("boom", status=500)

    with pytest.raises(HomeAssistantError, match="Failed to set breezer mode"):
        await breezer.async_select_option(BREEZER_OPTIONS[1])


@pytest.mark.asyncio
async def test_async_setup_entry_skips_hum_entities_until_capability_detected():
    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )
    listeners: list = []
    coordinator = SimpleNamespace(
        data={
            "device_map": {"1": dev},
            "states": {"1": {"damp_pos": 2}},
        },
        async_add_listener=lambda listener: listeners.append(listener) or (lambda: None),
    )
    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=coordinator,
        refresh_device=AsyncMock(),
    )
    entry = SimpleNamespace(
        entry_id="entry1",
        runtime_data=runtime,
        async_on_unload=lambda cb: None,
    )

    entities: list = []

    def _add_entities(new_entities):
        entities.extend(new_entities)

    await async_setup_entry(None, entry, _add_entities)

    assert [type(entity) for entity in entities] == [AtmeexBreezerSelect]
    assert len(listeners) == 1

    coordinator.data["states"]["1"]["hum_stg"] = 1
    listeners[0]()

    assert {type(entity) for entity in entities} == {
        AtmeexBreezerSelect,
        AtmeexHumidificationSelect,
    }
