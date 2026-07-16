import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import WSMsgType

import custom_components.atmeex_cloud.websocket as websocket_mod
from custom_components.atmeex_cloud.api import ApiError
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


class _HandshakeThenSuccessSession:
    def __init__(self) -> None:
        self.calls = 0

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise aiohttp.WSServerHandshakeError(
                None,
                tuple(),
                status=401,
                message="unauthorized",
                headers=None,
            )
        return _FakeWebSocket()


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


class _BlockingWebSocket:
    def __init__(self) -> None:
        self.closed = False
        self.receive_started = asyncio.Event()
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.receive_started.set()
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


class _ImmediateCloseSession:
    def __init__(self) -> None:
        self.calls = 0
        self.stable_socket = _BlockingWebSocket()

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        if self.calls < 3:
            return _FakeWebSocket()
        return self.stable_socket


class _SingleSocketSession:
    def __init__(self, socket) -> None:
        self.socket = socket

    async def ws_connect(self, *args, **kwargs):
        return self.socket


def _task_factory(coro, name):
    return asyncio.create_task(coro, name=name)


def _manager(
    session,
    *,
    token_getter=lambda: "token",
    on_message=None,
    on_token_refresh=None,
    on_auth_failure=None,
    config=None,
    task_factory=_task_factory,
):
    return WebSocketManager(
        session=session,
        token_getter=token_getter,
        on_message=on_message or (lambda _message: True),
        task_factory=task_factory,
        on_auth_failure=(
            on_auth_failure if on_auth_failure is not None else MagicMock()
        ),
        on_token_refresh=(
            on_token_refresh if on_token_refresh is not None else AsyncMock()
        ),
        config=config
        or WebSocketConfig(
            reconnect_delay_min=0.01,
            reconnect_delay_max=0.02,
        ),
    )


@pytest.mark.asyncio
async def test_immediate_close_transfers_reconnect_ownership(monkeypatch):
    session = _ImmediateCloseSession()
    manager = _manager(session)
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fast_sleep)

    assert await manager.connect() is True
    try:
        for _ in range(20):
            if session.calls >= 3:
                break
            await original_sleep(0)

        assert session.calls == 3
        assert manager._ws is session.stable_socket
    finally:
        await manager.disconnect()


@pytest.mark.asyncio
async def test_reconnect_retains_owner_until_success_stays_connected(
    monkeypatch,
):
    manager = _manager(_FlakySession(failures=0))
    manager._running = True
    stable_socket = _BlockingWebSocket()
    attempts = 0
    original_sleep = asyncio.sleep

    async def connect_once() -> bool:
        nonlocal attempts
        attempts += 1
        manager._ws = None if attempts == 1 else stable_socket
        return True

    async def fast_sleep(_delay):
        await original_sleep(0)

    manager._connect_once = AsyncMock(side_effect=connect_once)
    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fast_sleep)

    await manager._reconnect()

    assert attempts == 2
    assert manager._ws is stable_socket
    await manager.disconnect()


@pytest.mark.asyncio
async def test_disconnect_closes_socket_blocked_in_receive():
    socket = _BlockingWebSocket()
    manager = _manager(_SingleSocketSession(socket))

    assert await manager.connect() is True
    await socket.receive_started.wait()
    await manager.disconnect()

    assert socket.closed is True
    assert manager._listen_task is None
    assert manager._reconnect_task is None


@pytest.mark.asyncio
async def test_old_listener_cleanup_preserves_newer_socket_identity():
    old_socket = _FakeWebSocket()
    newer_socket = _FakeWebSocket()
    manager = _manager(_SingleSocketSession(newer_socket))
    manager._running = False
    manager._ws = newer_socket

    await manager._listen(old_socket)

    assert old_socket.closed is True
    assert manager._ws is newer_socket


@pytest.mark.asyncio
async def test_handshake_auth_recovers_once_before_reauth():
    session = _HandshakeThenSuccessSession()
    recovered = AsyncMock()
    reauth = MagicMock()
    manager = _manager(
        session,
        on_token_refresh=recovered,
        on_auth_failure=reauth,
    )

    assert await manager.connect() is True
    recovered.assert_awaited_once()
    reauth.assert_not_called()
    assert session.calls == 2
    await manager.disconnect()


@pytest.mark.asyncio
async def test_handshake_recovery_api_error_requests_one_shot_reauth(caplog):
    recovered = AsyncMock(
        side_effect=ApiError(
            "async_refresh_access_token",
            "private refresh response",
            status=503,
        )
    )
    reauth = MagicMock()
    session = _HandshakeFailureSession(status=401)
    manager = _manager(
        session,
        on_token_refresh=recovered,
        on_auth_failure=reauth,
    )

    assert await manager.connect() is False
    recovered.assert_awaited_once()
    reauth.assert_called_once()
    assert session.calls == 1
    assert manager._running is False
    assert manager._ws is None
    assert manager._listen_task is None
    assert manager._reconnect_task is None
    assert "private refresh response" not in caplog.text


@pytest.mark.asyncio
async def test_application_auth_counter_survives_handshakes_until_valid_data():
    manager = _manager(_FlakySession(failures=0))
    manager._running = True

    for expected in (1, 2, 3):
        manager._ws = _FakeWebSocket()
        await manager._handle_message('{"type":"unauthorized"}')
        assert manager._application_unauthorized_count == expected

    assert await manager._connect_once() is True
    assert manager._application_unauthorized_count == 3

    await manager._handle_message(
        '{"type":"condition","data":'
        '[{"id":1,"condition":{"pwr_on":1}}]}'
    )
    assert manager._application_unauthorized_count == 0
    await manager.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"type":"condition","data":"bad"}',
        '{"type":"condition","data":[]}',
        '{"type":"condition","data":[{"id":1,"condition":{}}]}',
        '{"type":"settings","data":[{"id":1,"settings":"bad"}]}',
        '{"type":"unknown","data":[]}',
    ],
)
async def test_malformed_or_unknown_messages_do_not_reset_auth(payload):
    callback = MagicMock(return_value=True)
    manager = _manager(
        _FlakySession(failures=0),
        on_message=callback,
    )
    manager._application_unauthorized_count = 2

    await manager._handle_message(payload)

    assert manager._application_unauthorized_count == 2
    assert manager._last_message_time == 0.0
    callback.assert_not_called()


@pytest.mark.asyncio
async def test_valid_message_rejected_by_callback_does_not_reset_auth():
    callback = MagicMock(return_value=False)
    manager = _manager(
        _FlakySession(failures=0),
        on_message=callback,
    )
    manager._application_unauthorized_count = 2

    await manager._handle_message(
        '{"type":"settings","data":'
        '[{"id":1,"settings":{"sound":1}}]}'
    )

    callback.assert_called_once()
    assert manager._application_unauthorized_count == 2
    assert manager._last_message_time == 0.0


@pytest.mark.asyncio
async def test_message_callback_error_does_not_reset_auth_or_log_detail(caplog):
    callback = MagicMock(side_effect=RuntimeError("private callback data"))
    manager = _manager(
        _FlakySession(failures=0),
        on_message=callback,
    )
    manager._application_unauthorized_count = 2

    await manager._handle_message(
        '{"type":"condition","data":'
        '[{"id":1,"condition":{"pwr_on":1}}]}'
    )

    callback.assert_called_once()
    assert manager._application_unauthorized_count == 2
    assert manager._last_message_time == 0.0
    assert "private callback data" not in caplog.text


@pytest.mark.asyncio
async def test_reauth_request_is_idempotent_across_repeated_unauthorized():
    reauth = MagicMock()
    manager = _manager(
        _FlakySession(failures=0),
        on_auth_failure=reauth,
    )
    manager._running = True

    for _ in range(websocket_mod.WS_MAX_UNAUTHORIZED_BEFORE_REAUTH + 2):
        manager._ws = _FakeWebSocket()
        await manager._handle_message('{"type":"unauthorized"}')

    reauth.assert_called_once()
    assert manager._running is False

    manager._running = True
    manager._request_reauth()

    reauth.assert_called_once()
    assert manager._running is False


@pytest.mark.asyncio
async def test_listener_is_created_by_supplied_task_factory():
    created_names: list[str] = []

    def owned_task_factory(coro, name):
        created_names.append(name)
        return asyncio.create_task(coro, name=name)

    manager = _manager(
        _FlakySession(failures=0),
        task_factory=owned_task_factory,
    )

    assert await manager.connect() is True
    assert created_names == ["atmeex websocket listener"]
    await manager.disconnect()


@pytest.mark.asyncio
async def test_listener_factory_failure_closes_coroutine_and_socket():
    socket = _FakeWebSocket()
    created_coroutines = []

    class _SingleSocketSession:
        async def ws_connect(self, *args, **kwargs):
            return socket

    def failing_task_factory(coro, name):
        created_coroutines.append(coro)
        raise RuntimeError("task ownership failed")

    manager = _manager(
        _SingleSocketSession(),
        task_factory=failing_task_factory,
    )

    with pytest.raises(RuntimeError, match="task ownership failed"):
        await manager._connect_once()

    assert len(created_coroutines) == 1
    assert created_coroutines[0].cr_frame is None
    assert socket.closed is True
    assert manager._ws is None
    assert manager._listen_task is None


@pytest.mark.asyncio
async def test_listener_factory_cancelled_error_closes_coroutine_and_socket():
    socket = _FakeWebSocket()
    created_coroutines = []

    class _SingleSocketSession:
        async def ws_connect(self, *args, **kwargs):
            return socket

    def cancelling_task_factory(coro, name):
        created_coroutines.append(coro)
        raise asyncio.CancelledError

    manager = _manager(
        _SingleSocketSession(),
        task_factory=cancelling_task_factory,
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await manager._connect_once()

        assert len(created_coroutines) == 1
        assert created_coroutines[0].cr_frame is None
        assert socket.closed is True
        assert manager._ws is None
        assert manager._listen_task is None
    finally:
        for coro in created_coroutines:
            if coro.cr_frame is not None:
                coro.close()
        if not socket.closed:
            await socket.close()


@pytest.mark.asyncio
async def test_reconnect_is_created_by_supplied_task_factory():
    created_names: list[str] = []

    def owned_task_factory(coro, name):
        created_names.append(name)
        return asyncio.create_task(coro, name=name)

    manager = _manager(
        _FlakySession(failures=100),
        task_factory=owned_task_factory,
    )
    manager._running = True

    manager._ensure_reconnect_task()

    assert created_names == ["atmeex websocket reconnect"]
    await manager.disconnect()


def test_reconnect_factory_failure_closes_unscheduled_coroutine():
    created_coroutines = []

    def failing_task_factory(coro, name):
        created_coroutines.append(coro)
        raise RuntimeError("reconnect ownership failed")

    manager = _manager(
        _FlakySession(failures=100),
        task_factory=failing_task_factory,
    )
    manager._running = True

    with pytest.raises(RuntimeError, match="reconnect ownership failed"):
        manager._ensure_reconnect_task()

    assert len(created_coroutines) == 1
    assert created_coroutines[0].cr_frame is None
    assert manager._reconnect_task is None


def test_reconnect_factory_cancelled_error_closes_unscheduled_coroutine():
    created_coroutines = []

    def cancelling_task_factory(coro, name):
        created_coroutines.append(coro)
        raise asyncio.CancelledError

    manager = _manager(
        _FlakySession(failures=100),
        task_factory=cancelling_task_factory,
    )
    manager._running = True

    try:
        with pytest.raises(asyncio.CancelledError):
            manager._ensure_reconnect_task()

        assert len(created_coroutines) == 1
        assert created_coroutines[0].cr_frame is None
        assert manager._reconnect_task is None
    finally:
        for coro in created_coroutines:
            if coro.cr_frame is not None:
                coro.close()


@pytest.mark.asyncio
async def test_ws_connect_timeout_type_matches_aiohttp_capability():
    captured = {}

    class _CapturingSession:
        async def ws_connect(self, *args, **kwargs):
            captured.update(kwargs)
            return _FakeWebSocket()

    manager = _manager(_CapturingSession())
    assert await manager.connect() is True
    try:
        from aiohttp import ClientWSTimeout
    except ImportError:
        assert isinstance(captured["timeout"], float)
    else:
        assert isinstance(captured["timeout"], ClientWSTimeout)
    await manager.disconnect()


@pytest.mark.asyncio
async def test_connect_bootstraps_reconnect_until_success(monkeypatch):
    session = _FlakySession(failures=2)
    manager = _manager(
        session,
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
    manager = _manager(
        session,
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

    manager = _manager(
        session,
        token_getter=lambda: token_state["value"],
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
    recovered = AsyncMock()
    session = _HandshakeFailureSession(status=status)
    manager = _manager(
        session,
        on_auth_failure=callback,
        on_token_refresh=recovered,
        config=WebSocketConfig(
            reconnect_delay_min=0.5,
            reconnect_delay_max=2.0,
        ),
    )

    assert await manager.connect() is False
    callback.assert_called_once()
    recovered.assert_awaited_once()
    assert manager._running is False
    assert manager._reconnect_task is None
    assert manager._listen_task is None
    assert manager._ws is None
    assert session.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "msg_type",
    [WSMsgType.BINARY, WSMsgType.PING, WSMsgType.PONG],
)
async def test_listen_ignores_non_text_messages_and_reconnects(msg_type):
    seen: list[dict] = []
    manager = _manager(
        _FlakySession(failures=0),
        on_message=lambda payload: seen.append(payload) or True,
    )
    manager._running = True
    socket = _ScriptedWebSocket(
        [
            SimpleNamespace(type=msg_type, data=b"x"),
            SimpleNamespace(type=WSMsgType.CLOSE, data=None),
        ]
    )
    manager._ws = socket
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen(socket)

    assert seen == []
    manager._ensure_reconnect_task.assert_called_once()


@pytest.mark.asyncio
async def test_listen_handles_text_message_and_close():
    received: list[dict] = []
    manager = _manager(
        _FlakySession(failures=0),
        on_message=lambda payload: received.append(payload) or True,
    )
    manager._running = True
    socket = _ScriptedWebSocket(
        [
            SimpleNamespace(
                type=WSMsgType.TEXT,
                data=json.dumps(
                    {
                        "type": "condition",
                        "data": [{"id": 1, "condition": {"pwr_on": 1}}],
                    }
                ),
            ),
            SimpleNamespace(type=WSMsgType.CLOSE, data=None),
        ]
    )
    manager._ws = socket
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen(socket)

    assert received == [
        {
            "type": "condition",
            "data": [{"id": 1, "condition": {"pwr_on": 1}}],
        }
    ]
    manager._ensure_reconnect_task.assert_called_once()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_listen_breaks_on_error_message_and_reconnects():
    manager = _manager(_FlakySession(failures=0))
    manager._running = True
    socket = _ScriptedWebSocket(
        [
            SimpleNamespace(type=WSMsgType.ERROR, data="boom"),
        ]
    )
    manager._ws = socket
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen(socket)

    manager._ensure_reconnect_task.assert_called_once()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_listen_does_not_reconnect_when_stopped():
    manager = _manager(_FlakySession(failures=0))
    manager._running = False
    socket = _ScriptedWebSocket(
        [
            SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({"k": "v"})),
        ]
    )
    manager._ws = socket
    manager._ensure_reconnect_task = MagicMock()

    await manager._listen(socket)

    manager._ensure_reconnect_task.assert_not_called()
    assert manager._ws is None


@pytest.mark.asyncio
async def test_handle_message_invalid_json_is_ignored():
    called = False

    def on_message(_payload):
        nonlocal called
        called = True

    manager = _manager(
        _FlakySession(failures=0),
        on_message=on_message,
    )

    await manager._handle_message("{invalid-json")
    assert called is False


@pytest.mark.asyncio
async def test_handle_message_callback_errors_are_swallowed():
    manager = _manager(
        _FlakySession(failures=0),
        on_message=lambda _payload: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    await manager._handle_message(
        json.dumps(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"pwr_on": 1}}],
            }
        )
    )


@pytest.mark.asyncio
async def test_reconnect_backoff_caps_at_max(monkeypatch):
    manager = _manager(
        _FlakySession(failures=0),
        config=WebSocketConfig(
            reconnect_delay_min=1.0,
            reconnect_delay_max=4.0,
        ),
    )
    manager._running = True
    manager._ws = None
    manager._record_transport_failure()

    async def fail_connect() -> bool:
        manager._record_transport_failure()
        return False

    manager._connect_once = AsyncMock(side_effect=fail_connect)

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= 4:
            manager._running = False

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fake_sleep)

    await manager._reconnect()

    assert delays == [1.0, 2.0, 4.0, 4.0]
    assert manager._reconnect_task is None


def test_transport_backoff_large_counter_remains_capped():
    manager = _manager(
        _FlakySession(failures=0),
        config=WebSocketConfig(
            reconnect_delay_min=1.0,
            reconnect_delay_max=4.0,
        ),
    )
    manager._transport_failures = 1025
    manager._reconnect_delay = manager._config.reconnect_delay_max

    manager._record_transport_failure()

    assert manager._transport_failures == 1026
    assert manager._reconnect_delay == manager._config.reconnect_delay_max


@pytest.mark.asyncio
async def test_reconnect_stops_after_success(monkeypatch):
    manager = _manager(
        _FlakySession(failures=0),
        config=WebSocketConfig(
            reconnect_delay_min=0.5,
            reconnect_delay_max=2.0,
        ),
    )
    manager._running = True
    manager._ws = None
    manager._record_transport_failure()
    attempts = 0

    async def connect_after_one_failure() -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            manager._record_transport_failure()
            return False
        manager._ws = _FakeWebSocket()
        return True

    manager._connect_once = AsyncMock(side_effect=connect_after_one_failure)

    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        if delay:
            delays.append(delay)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fake_sleep)

    await manager._reconnect()

    assert manager._connect_once.await_count == 2
    assert delays == [0.5, 1.0]
    assert manager._reconnect_task is None
    await manager.disconnect()


@pytest.mark.asyncio
async def test_unauthorized_message_triggers_backoff_and_token_refresh():
    """Unauthorized WS message should bump reconnect delay and call on_token_refresh."""
    refresh_called = asyncio.Event()

    async def fake_refresh():
        refresh_called.set()

    on_message_calls: list[dict] = []
    manager = _manager(
        _FlakySession(failures=0),
        on_message=lambda payload: on_message_calls.append(payload) or True,
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
    assert manager._application_unauthorized_count == 1
    # Application-auth backoff is separate from transport backoff.
    assert manager._next_reconnect_delay() == 2.0
    # WS closed
    assert fake_ws.closed is True
    # Message NOT forwarded to on_message callback
    assert on_message_calls == []
    # Manager still running (below threshold)
    assert manager._running is True


@pytest.mark.asyncio
async def test_unauthorized_counter_resets_on_successful_data_message():
    """After unauthorized attempts, a real data message should reset the failure counter."""
    manager = _manager(
        _FlakySession(failures=0),
        config=WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0),
    )
    manager._running = True
    manager._ws = _FakeWebSocket()

    # Simulate 3 unauthorized attempts
    for _ in range(3):
        manager._ws = _FakeWebSocket()
        await manager._handle_message(json.dumps({"type": "unauthorized", "data": None}))

    assert manager._application_unauthorized_count == 3
    assert manager._next_reconnect_delay() == 8.0

    # Now a real data message arrives after successful reconnect
    manager._ws = _FakeWebSocket()
    await manager._handle_message(
        json.dumps(
            {
                "type": "condition",
                "data": [{"id": 1, "condition": {"pwr_on": 1}}],
            }
        )
    )

    # Counter reset
    assert manager._application_unauthorized_count == 0


@pytest.mark.asyncio
async def test_unauthorized_triggers_reauth_after_max_failures():
    """After WS_MAX_UNAUTHORIZED_BEFORE_REAUTH consecutive failures, on_auth_failure is called."""
    auth_failure_called = MagicMock()
    refresh_calls = 0

    async def fake_refresh():
        nonlocal refresh_calls
        refresh_calls += 1

    manager = _manager(
        _FlakySession(failures=0),
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
    assert manager._application_unauthorized_count == max_attempts
    # Token refresh was called for attempts 1..max-1 (not on the final one that triggers reauth)
    assert refresh_calls == max_attempts - 1


@pytest.mark.asyncio
async def test_reconnect_backoff_resets_on_success_even_after_prior_auth_failure():
    """Backoff delay must reset to minimum on every successful connection.

    Transport backoff resets independently from application authentication.
    """
    connected = asyncio.Event()

    class _SuccessfulSession:
        async def ws_connect(self, *args, **kwargs):
            connected.set()
            return _FakeWebSocket()

    cfg = WebSocketConfig(reconnect_delay_min=1.0, reconnect_delay_max=60.0)
    manager = _manager(
        _SuccessfulSession(),
        on_message=AsyncMock(),
        config=cfg,
    )

    # Simulate that a prior auth failure bumped the counter and the backoff
    manager._application_unauthorized_count = 2
    manager._transport_failures = 3
    manager._reconnect_delay = 30.0  # elevated from prior failures

    result = await manager._connect_once()

    assert result is True
    # A successful transport does not prove application authentication.
    assert manager._reconnect_delay == cfg.reconnect_delay_min, (
        f"Backoff not reset: {manager._reconnect_delay} != {cfg.reconnect_delay_min}"
    )
    assert manager._transport_failures == 0
    assert manager._application_unauthorized_count == 2

    await manager.disconnect()
