import pytest
from aiohttp import ClientError

from custom_components.atmeex_cloud.api import AtmeexApi, ApiError


class ErrorSession:
    """Session, которая всегда падает с ClientError при get()."""

    def get(self, url, headers=None, timeout=None):
        # Имитируем сетевую ошибку до открытия контекст-менеджера
        raise ClientError("network boom")


@pytest.mark.asyncio
async def test_get_devices_fallback_network_error_returns_empty_list():
    session = ErrorSession()
    api = AtmeexApi(session)
    api._token = "t"

    result = await api.get_devices(fallback=True)

    assert result == []


@pytest.mark.asyncio
async def test_get_devices_no_fallback_raises_on_network_error():
    session = ErrorSession()
    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.get_devices(fallback=False)

    msg = str(exc.value)
    # Достаточно того, что это наш ApiError по сети
    assert "network error" in msg or "ClientError" in msg
