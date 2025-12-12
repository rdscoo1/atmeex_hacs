import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.atmeex_cloud.fan import AtmeexFanEntity
from custom_components.atmeex_cloud.api import AtmeexDevice


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

    dev = AtmeexDevice.from_raw(
        {"id": 1, "name": "Dev1", "model": "m", "online": True}
    )

    fan = AtmeexFanEntity(coordinator, api, "entry1", dev)
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
    coord.async_request_refresh.assert_awaited_once()
