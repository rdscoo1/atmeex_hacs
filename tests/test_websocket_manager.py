import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import WSMsgType

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
        self.headers: list[dict[str, str]] = []

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        self.headers.append(kwargs.get("headers", {}))
        if self.calls <= self._failures:
            raise aiohttp.ClientError("boom")
        return _FakeWebSocket()


class _HandshakeFailureSession:
    def __init__(self, status: int) -> None:
        self.status = status
        self.calls = 0

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        raise aiohttp.WSServerHandshakeError(
            None,
            tuple(),
            status=self.status,
            message="auth failed",
            headers=None,
        )


class _ScriptedWebSocket:
    def __init__(self, messages):
        self._messages = list(messages)
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


@pytest.mark.asyncio
async def test_connect_bootstraps_reconnect_until_success(monkeypatch):
    session = _FlakySession(failures=2)
    manager = WebSocketManager(
        session=session,
        token_getter="token",
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
        token_getter="token",
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


@pytest.mark.asyncio
async def test_reconnect_uses_fresh_token_from_getter(monkeypatch):
    session = _FlakySession(failures=1)
    token_state = {"value": "old"}

    manager = WebSocketManager(
        session=session,
        token_getter=lambda: token_state["value"],
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

    async def fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fast_sleep)

    assert await manager.connect() is False
    token_state["value"] = "fresh"

    for _ in range(20):
        if manager.is_connected:
            break
        await original_sleep(0)

    assert manager.is_connected is True
    assert len(session.headers) >= 2
    assert session.headers[0]["Authorization"] == "Bearer old"
    assert session.headers[1]["Authorization"] == "Bearer fresh"

    await manager.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_handshake_auth_error_triggers_callback_and_stops_reconnect(status):
    callback = MagicMock()
    session = _HandshakeFailureSession(status=status)
    manager = WebSocketManager(
        session=session,
        token_getter="token",
        on_message=lambda _msg: None,
        on_auth_failure=callback,
        config=WebSocketConfig(
            reconnect_delay_min=0.5,
            reconnect_delay_max=2.0,
        ),
    )

    assert await manager.connect() is False
    callback.assert_called_once()
    assert manager._running is False
    assert manager._reconnect_task is None
    assert session.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg_type",
    [WSMsgType.BINARY, WSMsgType.PING, WSMsgType.PONG],
)
async def test_listen_ignores_non_text_messages_and_reconnects(msg_type):
    seen: list[dict] = []
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda payload: seen.append(payload),
        config=WebSocketConfig(),
    )
    manager._running = True
    manager._ws = _ScriptedWebSocket(
        [
            SimpleNamespace(type=msg_type, data=b"x"),
            SimpleNamespace(type=WSMsgType.CLOSE, data=None),
        ]
    )
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen()

    assert seen == []
    manager._ensure_reconnect_task.assert_called_once()


@pytest.mark.asyncio
async def test_listen_handles_text_message_and_close():
    received: list[dict] = []
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda payload: received.append(payload),
        config=WebSocketConfig(),
    )
    manager._running = True
    manager._ws = _ScriptedWebSocket(
        [
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"k": "v"})),
            SimpleNamespace(type=WSMsgType.CLOSE, data=None),
        ]
    )
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen()

    assert received == [{"k": "v"}]
    manager._ensure_reconnect_task.assert_called_once()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_listen_breaks_on_error_message_and_reconnects():
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(),
    )
    manager._running = True
    manager._ws = _ScriptedWebSocket(
        [
            SimpleNamespace(type=WSMsgType.ERROR, data="boom"),
        ]
    )
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen()

    manager._ensure_reconnect_task.assert_called_once()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_listen_does_not_reconnect_when_stopped():
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(),
    )
    manager._running = False
    manager._ws = _ScriptedWebSocket(
        [
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"k": "v"})),
        ]
    )
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen()

    manager._ensure_reconnect_task.assert_not_called()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_handle_message_invalid_json_is_ignored():
    called = False

    def on_message(_payload):
        nonlocal called
        called = True

    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=on_message,
        config=WebSocketConfig(),
    )

    await manager._handle_message("{invalid-json")
    assert called is False


@pytest.mark.asyncio
async def test_handle_message_callback_errors_are_swallowed():
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")),
        config=WebSocketConfig(),
    )

    await manager._handle_message(json.dumps({"ok": True}))


@pytest.mark.asyncio
async def test_reconnect_backoff_caps_at_max(monkeypatch):
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(
            reconnect_delay_min=1.0,
            reconnect_delay_max=4.0,
        ),
    )
    manager._running = True
    manager._ws = None
    manager._connect_once = AsyncMock(return_value=False)

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 4:
            manager._running = False

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fake_sleep)

    await manager._reconnect()

    assert delays == [1.0, 2.0, 4.0, 4.0]
    assert manager._reconnect_task is None


@pytest.mark.asyncio
async def test_reconnect_stops_after_success(monkeypatch):
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(
            reconnect_delay_min=0.5,
            reconnect_delay_max=2.0,
        ),
    )
    manager._running = True
    manager._ws = None
    manager._connect_once = AsyncMock(side_effect=[False, True])

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fake_sleep)

    await manager._reconnect()

    assert manager._connect_once.await_count == 2
    assert delays == [0.5, 1.0]
    assert manager._reconnect_task is None


@pytest.mark.asyncio
async def test_unauthorized_message_triggers_backoff_and_token_refresh():
    """Unauthorized WS message should bump reconnect delay and call on_token_refresh."""
    refresh_called = asyncio.Event()

    async def fake_refresh():
        refresh_called.set()

    on_message_calls: list[dict] = []
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda payload: on_message_calls.append(payload),
        config=WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0),
        on_token_refresh=fake_refresh,
    )
    manager._running = True
    fake_ws = _FakeWebSocket()
    manager._ws = fake_ws

    await manager._handle_message(json.dumps({"type": "unauthorized", "data": None}))

    # Token refresh was called
    assert refresh_called.is_set()
    # Counter incremented
    assert manager._consecutive_auth_failures == 1
    # Delay bumped: min * 2^1 = 2.0
    assert manager._reconnect_delay == 2.0
    # WS closed
    assert fake_ws.closed is True
    # Message NOT forwarded to on_message callback
    assert on_message_calls == []
    # Manager still running (below threshold)
    assert manager._running is True


@pytest.mark.asyncio
async def test_unauthorized_counter_resets_on_successful_data_message():
    """After unauthorized attempts, a real data message should reset the failure counter."""
    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0),
    )
    manager._running = True
    manager._ws = _FakeWebSocket()

    # Simulate 3 unauthorized attempts
    for _ in range(3):
        manager._ws = _FakeWebSocket()
        await manager._handle_message(json.dumps({"type": "unauthorized", "data": None}))

    assert manager._consecutive_auth_failures == 3
    assert manager._reconnect_delay == 8.0  # 1.0 * 2^3

    # Now a real data message arrives after successful reconnect
    manager._ws = _FakeWebSocket()
    await manager._handle_message(json.dumps({"type": "condition", "data": []}))

    # Counter reset
    assert manager._consecutive_auth_failures == 0


@pytest.mark.asyncio
async def test_unauthorized_triggers_reauth_after_max_failures():
    """After WS_MAX_UNAUTHORIZED_BEFORE_REAUTH consecutive failures, on_auth_failure is called."""
    auth_failure_called = MagicMock()
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1

    manager = WebSocketManager(
        session=_FlakySession(failures=0),
        token_getter="token",
        on_message=lambda _payload: None,
        config=WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0),
        on_auth_failure=auth_failure_called,
        on_token_refresh=fake_refresh,
    )
    manager._running = True

    max_attempts = websocket_mod.WS_MAX_UNAUTHORIZED_BEFORE_REAUTH

    # Send unauthorized messages up to the threshold
    for i in range(max_attempts):
        manager._ws = _FakeWebSocket()
        await manager._handle_message(json.dumps({"type": "unauthorized", "data": None}))

    # on_auth_failure called on the last attempt
    auth_failure_called.assert_called_once()
    # Manager stopped
    assert manager._running is False
    # Counter equals max
    assert manager._consecutive_auth_failures == max_attempts
    # Token refresh was called for attempts 1..max-1 (not on the final one that triggers reauth)
    assert refresh_calls == max_attempts - 1


@pytest.mark.asyncio
async def test_reconnect_backoff_resets_on_success_even_after_prior_auth_failure():
    """Backoff delay must reset to minimum on every successful connection.

    The bug: `if self._consecutive_auth_failures == 0: reset delay` means
    that after any auth failure the counter is > 0 and the backoff is never
    reset — even if subsequent reconnects succeed cleanly.
    """
    connected = asyncio.Event()

    class _SuccessfulSession:
        async def ws_connect(self, *args, **kwargs):
            connected.set()
            return _FakeWebSocket()

    cfg = WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0)
    manager = WebSocketManager(
        session=_SuccessfulSession(),
        token_getter=lambda: "token",
        on_message=AsyncMock(),
        config=cfg,
    )

    # Simulate that a prior auth failure bumped the counter and the backoff
    manager._consecutive_auth_failures = 2
    manager._reconnect_delay = 30.0  # elevated from prior failures

    result = await manager._connect_once()

    assert result is True
    # After a successful connection, BOTH counters must reset
    assert manager._reconnect_delay == cfg.reconnect_delay_min, (
        f"Backoff not reset: {manager._reconnect_delay} != {cfg.reconnect_delay_min}"
    )
    assert manager._consecutive_auth_failures == 0, (
        f"Auth failure counter not reset after successful connect: {manager._consecutive_auth_failures}"
    )
