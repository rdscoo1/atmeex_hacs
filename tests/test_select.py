import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud.select import (
    AtmeexHumidificationSelect,
    AtmeexBrizerSelect,
    HUM_OPTIONS,
    BRIZER_OPTIONS,
)
from custom_components.atmeex_cloud.const import DOMAIN
from custom_components.atmeex_cloud.api import AtmeexDevice


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
    api.set_brizer_mode = AsyncMock()

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )

    hum = AtmeexHumidificationSelect(coordinator, api, dev, "Hum mode")
    brizer = AtmeexBrizerSelect(coordinator, api, dev, "Brizer mode")

    # hass не нужен, т.к. эти классы не используют _refresh
    hum.hass = SimpleNamespace(data={DOMAIN: {}})
    brizer.hass = SimpleNamespace(data={DOMAIN: {}})

    return hum, brizer, cond, api, coordinator


def test_humidification_select_current_option_from_hum_stg():
    hum, brizer, cond, api, coord = _make_selects({"hum_stg": 2})
    assert hum.current_option == HUM_OPTIONS[2]
    cond["hum_stg"] = 0
    assert hum.current_option == "off"


def test_humidification_select_fallback_to_cached_option():
    hum, brizer, cond, api, coord = _make_selects({"hum_stg": "bad"})
    # нет корректного hum_stg — используется _attr_current_option (по умолчанию "off")
    assert hum.current_option == "off"
    hum._attr_current_option = "2"
    assert hum.current_option == "2"


@pytest.mark.asyncio
async def test_humidification_select_async_select_option():
    hum, brizer, cond, api, coord = _make_selects({"hum_stg": 0})
    await hum.async_select_option("3")

    api.set_humid_stage.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert hum._attr_current_option == "3"


@pytest.mark.asyncio
async def test_humidification_select_invalid_option_noop():
    hum, brizer, cond, api, coord = _make_selects()
    await hum.async_select_option("invalid")
    api.set_humid_stage.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()


def test_brizer_select_current_option_from_damp_pos():
    hum, brizer, cond, api, coord = _make_selects({"damp_pos": 1})
    assert brizer.current_option == BRIZER_OPTIONS[1]

    cond["damp_pos"] = 10
    # некорректное значение — берём кэш или первую опцию
    assert brizer.current_option == BRIZER_OPTIONS[0]


@pytest.mark.asyncio
async def test_brizer_select_async_select_option():
    hum, brizer, cond, api, coord = _make_selects()
    await brizer.async_select_option(BRIZER_OPTIONS[3])

    api.set_brizer_mode.assert_awaited_once_with(1, 3)
    coord.async_request_refresh.assert_awaited_once()
    assert brizer._attr_current_option == BRIZER_OPTIONS[3]


@pytest.mark.asyncio
async def test_brizer_select_invalid_option_noop():
    hum, brizer, cond, api, coord = _make_selects()
    await brizer.async_select_option("неизвестно")
    api.set_brizer_mode.assert_not_awaited()
    coord.async_request_refresh.assert_not_awaited()
