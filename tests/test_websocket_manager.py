import asyncio
from unittest.mock import AsyncMock

import aiohttp
import pytest

import custom_components.atmeex_cloud.websocket as websocket_mod
from custom_components.atmeex_cloud.websocket import WebSocketConfig, WebSocketManager


class _FakeWebSocket:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class _FlakySession:
    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self._failures:
            raise aiohttp.ClientError("boom")
        return _FakeWebSocket()


@pytest.mark.asyncio
async def test_connect_bootstraps_reconnect_until_success(monkeypatch):
    session = _FlakySession(failures=2)
    manager = WebSocketManager(
        session=session,
        token="token",
        on_message=lambda _msg: None,
        config=WebSocketConfig(
            reconnect_delay_min=0.5,
            reconnect_delay_max=2.0,
            ping_interval=30.0,
            ping_timeout=10.0,
        ),
    )
    manager._listen = AsyncMock(return_value=None)

    original_sleep = asyncio.sleep
    delays: list[float] = []

    async def fast_sleep(delay: float) -> None:
        delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fast_sleep)

    assert await manager.connect() is False

    for _ in range(20):
        if manager.is_connected:
            break
        await original_sleep(0)

    assert manager.is_connected is True
    assert session.calls == 3
    assert delays[:2] == [0.5, 1.0]

    await manager.disconnect()
    assert manager.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_cancels_reconnect_loop(monkeypatch):
    session = _FlakySession(failures=100)
    manager = WebSocketManager(
        session=session,
        token="token",
        on_message=lambda _msg: None,
        config=WebSocketConfig(
            reconnect_delay_min=1.0,
            reconnect_delay_max=2.0,
            ping_interval=30.0,
            ping_timeout=10.0,
        ),
    )
    manager._listen = AsyncMock(return_value=None)

    original_sleep = asyncio.sleep
    sleep_started = asyncio.Event()

    async def blocking_sleep(_delay: float) -> None:
        sleep_started.set()
        await asyncio.Future()

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", blocking_sleep)

    assert await manager.connect() is False

    for _ in range(20):
        if sleep_started.is_set():
            break
        await original_sleep(0)
    assert sleep_started.is_set()

    await manager.disconnect()
    assert manager._reconnect_task is None
    assert manager.is_connected is False
