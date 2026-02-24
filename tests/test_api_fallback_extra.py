import pytest
from aiohttp import ClientError

import custom_components.atmeex_cloud.api as api_mod
from custom_components.atmeex_cloud.api import AtmeexApi, ApiError, API_BASE_URL


class ErrorSession:
    """Сессия, которая всегда бросает сетевую ошибку."""

    def request(self, method, url, json=None, headers=None, timeout=None):
        raise ClientError("boom")


@pytest.mark.asyncio
async def test_get_devices_fallback_network_error_returns_empty_list(monkeypatch):
    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", _fast_sleep)

    session = ErrorSession()
    api = AtmeexApi(session)
    api._token = "t"  # токен есть, чтобы не упираться в отсутствие логина

    result = await api.get_devices(fallback=True)
    # В fallback-режиме сетевые ошибки → пустой список, без исключения
    assert result == []


@pytest.mark.asyncio
async def test_get_devices_no_fallback_raises_on_network_error(monkeypatch):
    async def _fast_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(api_mod.asyncio, "sleep", _fast_sleep)

    session = ErrorSession()
    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.get_devices(fallback=False)

    # Сообщение в стиле: "get_devices network error: ..."
    assert "get_devices network error" in str(exc.value)
