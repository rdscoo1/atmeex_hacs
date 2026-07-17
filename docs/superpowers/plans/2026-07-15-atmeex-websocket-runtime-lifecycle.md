# Atmeex WebSocket and Runtime Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each Atmeex config entry deterministic ownership of its WebSocket, refresh work, message buffer, and background tasks so authentication recovers correctly and setup/unload leave no leaked work or post-unload mutations.

**Architecture:** AtmeexRuntimeData is the entry ownership root and tracks every background task created through a Home Assistant compatibility helper. WebSocketManager owns only transport, authentication counters, socket closure, listener/reconnect transfer, and structural message validation; the composition root owns bounded buffering, coalescing, AtmeexStateStore mutation, publication, overflow resync, setup rollback, and unload ordering. Plan 3's AtmeexCommandExecutor continues to receive the same shielded RecoveryRefresh callback.

**Tech Stack:** Python 3.12+, asyncio, aiohttp, Home Assistant 2024.8+ config-entry/task APIs, pytest, pytest-asyncio, unittest.mock

---

## Exact file map

- Create custom_components/atmeex_cloud/compat.py — feature-detected Home Assistant background-task creation without version comparisons.
- Modify custom_components/atmeex_cloud/runtime.py — typed entry-owned dependencies, stopping state, task registry, named startup/drain/watchdog/resync tasks, refresh owners, and overflow counter.
- Modify custom_components/atmeex_cloud/websocket.py — explicit token recovery, separate transport/application-auth counters, strict message validation, reconnect ownership transfer, and exact-socket closure.
- Modify custom_components/atmeex_cloud/__init__.py — shielded targeted refresh, bounded WebSocket buffering/coalescing, transactional setup, rollback, and ordered unload.
- Create tests/test_compat.py — Home Assistant 2024.8/current task-signature compatibility.
- Modify tests/test_runtime.py — task tracking and typed ownership.
- Modify tests/test_websocket_manager.py — token recovery, auth cycles, malformed messages, reconnect transfer, and socket closure.
- Modify tests/test_websocket_integration.py — coalescing, overflow resync, stopping guard, and refresh-waiter shielding.
- Modify tests/test_setup.py — post-platform startup, startup coroutine cleanup, and rollback.
- Modify tests/test_unload.py — unload-false preservation and successful zero-task cleanup.

## Locked interfaces

Use Plan 2's FieldRevisionBaseline, StateStoreUpdate, and AtmeexStateStore methods exactly. A targeted GET captures store.capture_device(device_id) before I/O and applies store.apply_refresh(device, baseline). A coalesced WebSocket device delta calls store.apply_websocket_delta once for that device, and one drain turn publishes store.data at most once.

Keep Plan 3's names unchanged:

- CommandCoroutineFactory = Callable[[], Awaitable[None]]
- RecoveryRefresh = Callable[[int | str], Awaitable[None]]
- AtmeexCommandExecutor.async_execute, value_with_pending, confirm, and allow_recovery_confirmation
- AtmeexCommandExecutor(refresh_device, pending_ttl=10.0)

Plan 1 provides AtmeexApi.async_refresh_access_token() -> None. It is the WebSocket token-recovery callback; coordinator.async_request_refresh is not an authentication callback.

## Execution gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require all
tests to pass. Tasks 1–5 may retain only the already-recorded WebSocket-startup
RuntimeWarning; Task 6 must remove it, and Tasks 6–8 must pass with runtime
warnings promoted to errors. The pytest-asyncio loop-scope notice remains owned
by Plan 6.

### Task 1: Add compatible task creation and runtime supervision

**Files:**

- Create: custom_components/atmeex_cloud/compat.py
- Modify: custom_components/atmeex_cloud/runtime.py
- Create: tests/test_compat.py
- Modify: tests/test_runtime.py

- [ ] **Step 1: Write RED compatibility and task-registry tests**

Create tests/test_compat.py:

~~~python
import asyncio

import pytest

from custom_components.atmeex_cloud.compat import async_create_background_task


@pytest.mark.asyncio
@pytest.mark.parametrize("supports_name", [False, True])
async def test_async_create_background_task_supports_both_ha_signatures(
    supports_name,
):
    names: list[str | None] = []

    class FakeHass:
        def async_create_task(self, coro, **kwargs):
            if not supports_name and kwargs:
                raise TypeError("name is unsupported")
            names.append(kwargs.get("name"))
            return asyncio.create_task(coro)

    async def work() -> None:
        return

    task = async_create_background_task(FakeHass(), work(), "atmeex-test")
    await task
    assert names == (["atmeex-test"] if supports_name else [None])


@pytest.mark.asyncio
async def test_async_create_background_task_prefers_ha_background_ownership():
    calls: list[str] = []

    class FakeHass:
        def async_create_background_task(self, coro, name):
            calls.append(name)
            return asyncio.create_task(coro)

        def async_create_task(self, coro, **kwargs):
            raise AssertionError("foreground task helper must not be selected")

    async def work() -> None:
        return

    task = async_create_background_task(FakeHass(), work(), "atmeex-background")
    await task
    assert calls == ["atmeex-background"]
~~~

Append to tests/test_runtime.py:

~~~python
@pytest.mark.asyncio
async def test_runtime_tracks_and_discards_completed_tasks():
    runtime = AtmeexRuntimeData(
        api=None,
        coordinator=None,
        state_store=None,
        command_executor=None,
        refresh_device=None,
    )
    task = runtime.track_task(asyncio.create_task(asyncio.sleep(0)))
    assert task in runtime.tasks
    await task
    await asyncio.sleep(0)
    assert runtime.tasks == set()
~~~

- [ ] **Step 2: Run the tests and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_compat.py tests/test_runtime.py::test_runtime_tracks_and_discards_completed_tasks
~~~

Expected: collection fails because compat.py and AtmeexRuntimeData.track_task do not exist.

- [ ] **Step 3: Implement the compatibility helper**

Create custom_components/atmeex_cloud/compat.py:

~~~python
"""Small Home Assistant compatibility surface."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

from homeassistant.core import HomeAssistant

_T = TypeVar("_T")


def async_create_background_task(
    hass: HomeAssistant,
    coro: Coroutine[Any, Any, _T],
    name: str,
) -> asyncio.Task[_T]:
    """Create a task on old and current supported Home Assistant releases."""
    background_creator = getattr(hass, "async_create_background_task", None)
    if callable(background_creator):
        return background_creator(coro, name)
    try:
        return hass.async_create_task(coro, name=name)
    except TypeError:
        return hass.async_create_task(coro)
~~~

- [ ] **Step 4: Add typed runtime ownership and tracking**

In custom_components/atmeex_cloud/runtime.py, retain Plan 3's compatibility delegates and use this field block and method:

~~~python
@dataclass
class AtmeexRuntimeData:
    # Keep Plan 3's constructor defaults through this lifecycle migration so
    # unrelated platform fakes stay valid at every task gate. Production setup
    # supplies all five; Plan 5 tightens them only after all fakes are migrated.
    api: AtmeexApi | None
    coordinator: AtmeexCoordinator | None
    refresh_device: RecoveryRefresh | None
    state_store: AtmeexStateStore | None = None
    command_executor: AtmeexCommandExecutor | None = None
    websocket_manager: WebSocketManager | None = None
    stopping: bool = False
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    refresh_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    websocket_start_task: asyncio.Task[Any] | None = None
    websocket_message_task: asyncio.Task[None] | None = None
    inventory_watchdog_task: asyncio.Task[None] | None = None
    websocket_resync_task: asyncio.Task[None] | None = None
    websocket_overflow_count: int = 0

    def track_task(self, task: asyncio.Task[Any]) -> asyncio.Task[Any]:
        self.tasks.add(task)

        def task_done(done: asyncio.Task[Any]) -> None:
            self.tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                _LOGGER.warning(
                    "Atmeex background task failed: %s",
                    type(error).__name__,
                )

        task.add_done_callback(task_done)
        return task
~~~

Put AtmeexApi, AtmeexCoordinator, AtmeexStateStore, AtmeexCommandExecutor, and WebSocketManager imports under TYPE_CHECKING to avoid runtime cycles on Home Assistant 2024.8. Keep the concrete annotations because from __future__ import annotations is active.

- [ ] **Step 5: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_compat.py tests/test_runtime.py
~~~

Expected: all selected tests pass.

Run `.venv/bin/python -m pytest -q` before the commit. In particular,
`tests/test_select.py` still constructs the compatibility shape and must remain
green; do not make the five production dependencies required in this task.

~~~bash
git add custom_components/atmeex_cloud/compat.py custom_components/atmeex_cloud/runtime.py tests/test_compat.py tests/test_runtime.py
git commit -m "refactor: supervise Atmeex entry tasks"
~~~

### Task 2: Make WebSocket authentication explicit and cycle-safe

**Files:**

- Modify: custom_components/atmeex_cloud/websocket.py
- Modify: tests/test_websocket_manager.py

- [ ] **Step 1: Add RED authentication and validation tests**

Append to tests/test_websocket_manager.py:

~~~python
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
async def test_application_auth_counter_survives_handshakes_until_valid_data():
    manager = _manager(_FlakySession(failures=0))
    manager._running = True

    for expected in (1, 2, 3):
        manager._ws = _FakeWebSocket()
        await manager._handle_message('{"type":"unauthorized"}')
        assert manager._application_unauthorized_count == expected

    await manager._handle_message(
        '{"type":"condition","data":[{"id":1,"condition":{"pwr_on":1}}]}'
    )
    assert manager._application_unauthorized_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        '{"type":"condition","data":"bad"}',
        '{"type":"settings","data":[{"id":1,"settings":"bad"}]}',
        '{"type":"unknown","data":[]}',
    ],
)
async def test_malformed_or_unknown_messages_do_not_reset_auth(payload):
    manager = _manager(_FlakySession(failures=0))
    manager._application_unauthorized_count = 2
    await manager._handle_message(payload)
    assert manager._application_unauthorized_count == 2


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
~~~

Add these complete helpers above the tests:

~~~python
def _task_factory(coro, name):
    return asyncio.create_task(coro, name=name)


def _manager(
    session,
    *,
    on_token_refresh=None,
    on_auth_failure=None,
):
    return WebSocketManager(
        session=session,
        token_getter=lambda: "token",
        on_message=lambda message: True,
        config=WebSocketConfig(
            reconnect_delay_min=0.01,
            reconnect_delay_max=0.02,
        ),
        on_auth_failure=on_auth_failure or MagicMock(),
        on_token_refresh=on_token_refresh or AsyncMock(),
        task_factory=_task_factory,
    )


class _HandshakeThenSuccessSession:
    def __init__(self):
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
~~~

Replace the existing test_reconnect_backoff_resets_on_success_even_after_prior_auth_failure assertion so it expects _transport_failures == 0, _reconnect_delay == reconnect_delay_min, and _application_unauthorized_count == 2 after a successful handshake. Replace every remaining _consecutive_auth_failures reference with _application_unauthorized_count. Every direct WebSocketManager constructor in this file must pass task_factory=_task_factory, on_token_refresh=AsyncMock(), and on_auth_failure=MagicMock(); callbacks that accept a valid state message must return True, for example lambda payload: received.append(payload) or True.

Also migrate the older message fixtures to the structural contract in this task:
`test_listen_handles_text_message_and_close` and the callback-error test use a
non-empty `condition` message with an ID; the successful-reset test uses
`{"type":"condition","data":[{"id":1,"condition":{"pwr_on":1}}]}` rather
than an empty list. Update the old handshake-401 test to expect one token
recovery, two rejected handshake calls, and one reauth callback after the second
rejection. Do not retain an assertion that an arbitrary `{"k":"v"}` object is
forwarded or resets authentication state.
For the older unauthorized-delay assertion, check
`manager._next_reconnect_delay()` instead of `_reconnect_delay`: transport and
application-auth backoff are deliberately calculated from separate counters.

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_manager.py -k "handshake_auth_recovers or application_auth_counter or malformed_or_unknown or ws_connect_timeout"
~~~

Expected: FAIL because handshake rejection immediately starts reauth, successful handshakes reset the application counter, and malformed messages are forwarded.

- [ ] **Step 3: Implement explicit recovery and validated acceptance**

Use these constructor types and fields in WebSocketManager:

~~~python
MessageCallback = Callable[[dict[str, Any]], bool]
TokenRecoveryCallback = Callable[[], Awaitable[None]]
TaskFactory = Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]]

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_getter: Callable[[], str],
        on_message: MessageCallback,
        *,
        task_factory: TaskFactory,
        on_auth_failure: Callable[[], None],
        on_token_refresh: TokenRecoveryCallback,
        config: WebSocketConfig | None = None,
    ) -> None:
        self._session = session
        self._token_getter = token_getter
        self._on_message = on_message
        self._task_factory = task_factory
        self._on_auth_failure = on_auth_failure
        self._on_token_refresh = on_token_refresh
        self._config = config or WebSocketConfig()
        self._ws: ClientWebSocketResponse | None = None
        self._running = False
        self._listen_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._reconnect_delay = self._config.reconnect_delay_min
        self._last_message_time = 0.0
        self._transport_failures = 0
        self._application_unauthorized_count = 0
        self._reauth_requested = False
~~~

Replace handshake handling with:

~~~python
    async def _connect_once(self, *, allow_recovery: bool = True) -> bool:
        try:
            token = self._token_getter() or ""
            ws = await self._session.ws_connect(
                self._config.base_url,
                headers={"Authorization": f"Bearer {token}"},
                heartbeat=self._config.ping_interval,
                timeout=_ws_handshake_timeout(self._config.ping_timeout),
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                if allow_recovery:
                    try:
                        await self._on_token_refresh()
                    except ApiError:
                        self._request_reauth()
                        return False
                    return await self._connect_once(allow_recovery=False)
                self._request_reauth()
                return False
            self._record_transport_failure()
            return False
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._record_transport_failure()
            return False

        self._transport_failures = 0
        self._reconnect_delay = self._config.reconnect_delay_min
        self._ws = ws
        self._listen_task = self._task_factory(
            self._listen(ws),
            "atmeex websocket listener",
        )
        return True

    def _request_reauth(self) -> None:
        if self._reauth_requested:
            return
        self._reauth_requested = True
        self._running = False
        self._on_auth_failure()

    def _record_transport_failure(self) -> None:
        self._transport_failures += 1
        self._reconnect_delay = min(
            self._config.reconnect_delay_min
            * (2 ** (self._transport_failures - 1)),
            self._config.reconnect_delay_max,
        )

    def _next_reconnect_delay(self) -> float:
        auth_delay = self._config.reconnect_delay_min
        if self._application_unauthorized_count:
            auth_delay = min(
                self._config.reconnect_delay_min
                * (2 ** self._application_unauthorized_count),
                self._config.reconnect_delay_max,
            )
        return max(self._reconnect_delay, auth_delay)
~~~

Add this module-level helper above `WebSocketManager` (with `Any` from
`typing`) so the handshake timeout follows aiohttp's current contract through
feature detection — aiohttp with typed WebSocket timeouts deprecates the bare
float, while the aiohttp shipped with Home Assistant 2024.8 requires it:

~~~python
try:
    from aiohttp import ClientWSTimeout

    def _ws_handshake_timeout(seconds: float) -> Any:
        return ClientWSTimeout(ws_close=seconds)
except ImportError:
    def _ws_handshake_timeout(seconds: float) -> float:
        return seconds
~~~

Replace message handling with:

~~~python
    async def _handle_message(self, data: str) -> None:
        try:
            message = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(message, dict):
            return
        if message.get("type") == "unauthorized":
            self._application_unauthorized_count += 1
            if (
                self._application_unauthorized_count
                >= WS_MAX_UNAUTHORIZED_BEFORE_REAUTH
            ):
                self._request_reauth()
            else:
                try:
                    await self._on_token_refresh()
                except ApiError:
                    self._request_reauth()
            await self._close_active_socket()
            return
        if not self._is_valid_state_message(message):
            return
        try:
            accepted = self._on_message(message)
        except Exception:
            return
        if accepted:
            self._application_unauthorized_count = 0
            self._last_message_time = time.monotonic()

    @staticmethod
    def _is_valid_state_message(message: dict[str, Any]) -> bool:
        message_type = message.get("type")
        field = "condition" if message_type == "condition" else "settings"
        if message_type not in ("condition", "settings"):
            return False
        data = message.get("data")
        if not isinstance(data, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("id") is not None
            and isinstance(item.get(field), dict)
            and bool(item[field])
            for item in data
        )
~~~

Import ApiError from .api. Never log raw message data or token values.

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_manager.py -k "auth or malformed or unauthorized"
~~~

Expected: all selected tests pass; reauth is invoked at most once per loaded manager.

~~~bash
git add custom_components/atmeex_cloud/websocket.py tests/test_websocket_manager.py
git commit -m "fix: recover WebSocket authentication by cycle"
~~~

### Task 3: Transfer reconnect ownership and close the captured socket

**Files:**

- Modify: custom_components/atmeex_cloud/websocket.py
- Modify: tests/test_websocket_manager.py

- [ ] **Step 1: Add RED immediate-close and blocked-receive tests**

Append:

~~~python
@pytest.mark.asyncio
async def test_immediate_close_transfers_reconnect_ownership(monkeypatch):
    session = _ImmediateCloseSession()
    manager = _manager(session)
    original_sleep = asyncio.sleep

    async def fast_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr(websocket_mod.asyncio, "sleep", fast_sleep)

    assert await manager.connect() is True
    for _ in range(20):
        if session.calls >= 3:
            break
        await original_sleep(0)

    assert session.calls == 3
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
~~~

Add these complete fakes above the tests:

~~~python
class _BlockingWebSocket:
    def __init__(self):
        self.closed = False
        self.receive_started = asyncio.Event()
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.receive_started.set()
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True
        self._closed.set()


class _ImmediateCloseSession:
    def __init__(self):
        self.calls = 0
        self.stable_socket = _BlockingWebSocket()

    async def ws_connect(self, *args, **kwargs):
        self.calls += 1
        if self.calls < 3:
            return _FakeWebSocket()
        return self.stable_socket


class _SingleSocketSession:
    def __init__(self, socket):
        self.socket = socket

    async def ws_connect(self, *args, **kwargs):
        return self.socket
~~~

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_manager.py -k "immediate_close or blocked_in_receive"
~~~

Expected: immediate close loses reconnect ownership and disconnect can miss self._ws after listener cleanup.

- [ ] **Step 3: Implement exact-socket listening and reconnect handoff**

Replace the relevant methods:

~~~python
    async def _close_active_socket(self) -> None:
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    async def _listen(self, ws: ClientWebSocketResponse) -> None:
        try:
            async for message in ws:
                if not self._running:
                    break
                if message.type == WSMsgType.TEXT:
                    await self._handle_message(message.data)
                elif message.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._record_transport_failure()
        finally:
            if not ws.closed:
                await ws.close()
            if self._ws is ws:
                self._ws = None
            if self._running:
                self._ensure_reconnect_task()

    def _ensure_reconnect_task(self) -> None:
        if not self._running:
            return
        task = self._reconnect_task
        if task is not None and not task.done():
            return
        self._reconnect_task = self._task_factory(
            self._reconnect(),
            "atmeex websocket reconnect",
        )

    async def _reconnect(self) -> None:
        current = asyncio.current_task()
        try:
            while self._running and not self.is_connected:
                await asyncio.sleep(self._next_reconnect_delay())
                if not self._running:
                    return
                connected = await self._connect_once()
                if connected:
                    await asyncio.sleep(0)
                    if self.is_connected:
                        return
        finally:
            if self._reconnect_task is current:
                self._reconnect_task = None
            if self._running and not self.is_connected:
                self._ensure_reconnect_task()

    async def disconnect(self) -> None:
        self._running = False
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()
        await self._cancel_task(self._listen_task)
        await self._cancel_task(self._reconnect_task)
        self._listen_task = None
        self._reconnect_task = None
        if self._ws is ws:
            self._ws = None
~~~

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_manager.py
~~~

Expected: all manager tests pass.

~~~bash
git add custom_components/atmeex_cloud/websocket.py tests/test_websocket_manager.py
git commit -m "fix: transfer WebSocket reconnect ownership"
~~~

### Task 4: Shield and supervise targeted refresh owners

**Files:**

- Modify: custom_components/atmeex_cloud/__init__.py
- Modify: tests/test_websocket_integration.py

- [ ] **Step 1: Replace timeout eviction with RED waiter-shielding tests**

Add:

~~~python
@pytest.mark.asyncio
async def test_cancelled_refresh_waiter_does_not_cancel_owner(monkeypatch):
    runtime, _callback, _hass = await _build_ws_runtime(monkeypatch)
    api = runtime.api
    api.started = asyncio.Event()
    api.release = asyncio.Event()
    api.get_device_calls = 0

    async def blocked_get_device(device_id):
        api.get_device_calls += 1
        api.started.set()
        await api.release.wait()
        return api._devices[0]

    api.get_device.reset_mock()
    api.get_device.side_effect = blocked_get_device
    owner_waiter = asyncio.create_task(runtime.refresh_device(1))
    await api.started.wait()
    cancelled_waiter = asyncio.create_task(runtime.refresh_device(1))
    await asyncio.sleep(0)
    cancelled_waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert api.get_device_calls == 1
    assert runtime.refresh_tasks["1"].cancelled() is False

    monkeypatch.setattr(atmeex_init, "_REFRESH_TASK_TIMEOUT_SEC", 0.01)
    with pytest.raises(asyncio.TimeoutError):
        await runtime.refresh_device(1)
    assert api.get_device_calls == 1
    assert runtime.refresh_tasks["1"].cancelled() is False

    api.release.set()
    await owner_waiter
    assert runtime.refresh_tasks == {}


@pytest.mark.asyncio
async def test_failed_confirmation_schedules_tracked_authoritative_recovery(monkeypatch):
    runtime, _callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.api.get_device.side_effect = AtmeexConnectionError(
        "get_device", "confirmation unavailable"
    )
    runtime.coordinator.async_request_refresh = AsyncMock()

    with pytest.raises(AtmeexConnectionError):
        await runtime.refresh_device(1)
    await asyncio.gather(*tuple(runtime.tasks))

    runtime.coordinator.async_request_refresh.assert_awaited_once_with()
~~~

Extend the existing _FakeApi inside _build_ws_runtime to retain its device list as self._devices = [dev], so blocked_get_device returns the exact existing AtmeexDevice. Events, not elapsed sleeps, control correctness.
Import `AtmeexConnectionError` from
`custom_components.atmeex_cloud.api` for the recovery test.

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_integration.py::test_cancelled_refresh_waiter_does_not_cancel_owner
~~~

Expected: FAIL because the refresh owner is a raw task and wait_for cancellation reaches it.

- [ ] **Step 3: Implement the shielded owner**

After runtime_data is constructed in async_setup_entry, add the task helper below. It is reused by refresh, WebSocket, drain, resync, startup, and watchdog work:

~~~python
    def _create_entry_task(
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        try:
            task = async_create_background_task(hass, coro, name)
        except BaseException:
            coro.close()
            raise
        return runtime_data.track_task(task)
~~~

Then make refresh_device close over runtime_data and use:

~~~python
    async def _recover_after_targeted_failure(
        device_id: int | str,
    ) -> None:
        await coordinator.async_request_refresh()
        if runtime_data is None or runtime_data.command_executor is None:
            return
        runtime_data.command_executor.allow_recovery_confirmation(device_id)
        # The coordinator published before confirmation tickets were enabled;
        # notify once more without replacing the comparable snapshot.
        coordinator.async_update_listeners()

    async def _refresh_device_once(device_id: int | str) -> None:
        if runtime_data is None or runtime_data.stopping:
            return
        baseline = runtime_data.state_store.capture_device(device_id)
        try:
            device = await api.get_device(device_id)
        except AtmeexApiError as err:
            coordinator._fire_api_error_event(
                {
                    "message": str(err),
                    "operation": err.operation,
                    "status": err.status,
                    "source": "refresh_device",
                    "device_id": str(device_id),
                }
            )
            _create_entry_task(
                _recover_after_targeted_failure(device_id),
                "atmeex targeted-refresh recovery",
            )
            raise
        if runtime_data.stopping:
            return
        update = runtime_data.state_store.apply_refresh(device, baseline)
        if update.changed:
            coordinator.async_set_updated_data(update.data)

    async def refresh_device(device_id: int | str) -> None:
        if runtime_data is None or runtime_data.stopping:
            return
        key = str(device_id)
        owner = runtime_data.refresh_tasks.get(key)
        if owner is None or owner.done():
            owner = _create_entry_task(
                _refresh_device_once(device_id),
                f"atmeex refresh {key}",
            )
            runtime_data.refresh_tasks[key] = owner

            def remove_owner(done: asyncio.Task[None]) -> None:
                if runtime_data.refresh_tasks.get(key) is done:
                    runtime_data.refresh_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            owner.add_done_callback(remove_owner)
        await asyncio.wait_for(
            asyncio.shield(owner),
            timeout=_REFRESH_TASK_TIMEOUT_SEC,
        )
~~~

Do not evict a still-running owner after waiter timeout. Plan 3's executor continues to receive this exact refresh_device function.
Import `AtmeexApiError` from `.api`; its string and operation fields are already
sanitized by Plan 1, so the existing API-error event contract remains safe.
Extend the failed-confirmation integration test to assert that pending remains
before the full recovery finishes, exactly one recovery task is tracked, and a
matching executed generation retires only after
`allow_recovery_confirmation` and the final listener notification. Include an
ABA case whose queued value equals the pre-command snapshot; it must remain
pending until its factory executes and its own confirmation completes.

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_integration.py -k "refresh_device"
~~~

Expected: all refresh tests pass; cancellation and timeout affect only the waiter.

~~~bash
git add custom_components/atmeex_cloud/__init__.py tests/test_websocket_integration.py
git commit -m "fix: shield shared Atmeex refresh owners"
~~~

### Task 5: Bound, coalesce, and resynchronize WebSocket messages

**Files:**

- Modify: custom_components/atmeex_cloud/__init__.py
- Modify: tests/test_websocket_integration.py

- [ ] **Step 1: Add RED burst, overflow, and stopping tests**

Add:

~~~python
@pytest.mark.asyncio
async def test_burst_coalesces_later_fields_into_one_publication(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    coordinator = runtime.coordinator
    coordinator.update_calls = 0

    for speed in range(0, 7):
        assert callback({
            "type": "condition",
            "data": [{"id": 1, "condition": {"fan_speed": speed}}],
        })
    await runtime.websocket_message_task

    assert coordinator.update_calls == 1
    assert coordinator.data["states"]["1"]["fan_speed"] == 7


@pytest.mark.asyncio
async def test_queue_overflow_schedules_one_authoritative_resync(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.coordinator.async_request_refresh = AsyncMock()

    for speed in range(501):
        callback({
            "type": "condition",
            "data": [{"id": 1, "condition": {"fan_speed": speed}}],
        })
    await asyncio.gather(*tuple(runtime.tasks))

    assert runtime.websocket_overflow_count == 1
    runtime.coordinator.async_request_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_callback_refuses_work_after_stopping(monkeypatch):
    runtime, callback, _hass = await _build_ws_runtime(monkeypatch)
    runtime.stopping = True
    assert callback({
        "type": "condition",
        "data": [{"id": 1, "condition": {"pwr_on": 1}}],
    }) is False
    assert runtime.websocket_message_task is None
~~~

Update every FakeWebSocketManager constructor in tests/test_websocket_integration.py to accept task_factory=None as a keyword after on_token_refresh. Add self.async_refresh_access_token = AsyncMock() to each WebSocket-enabled FakeApi. Production setup still requires and passes the real task factory and API callback.

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_integration.py -k "burst_coalesces or queue_overflow or refuses_work"
~~~

Expected: multiple publications occur, deque silently evicts the oldest message, and stopping still permits drain creation.

- [ ] **Step 3: Implement bounded batching and one publication**

In async_setup_entry, replace deque(maxlen=500) callback logic with:

~~~python
    websocket_messages: deque[dict[str, Any]] = deque()
    websocket_limit = 500
    overflow_resync_running = False

    async def _overflow_resync() -> None:
        nonlocal overflow_resync_running
        try:
            await coordinator.async_request_refresh()
        finally:
            overflow_resync_running = False
            if runtime_data is not None:
                runtime_data.websocket_resync_task = None

    def _schedule_overflow_resync() -> None:
        nonlocal overflow_resync_running
        if overflow_resync_running or runtime_data is None:
            return
        overflow_resync_running = True
        runtime_data.websocket_resync_task = _create_entry_task(
            _overflow_resync(),
            "atmeex websocket overflow resync",
        )

    async def _drain_websocket_messages() -> None:
        try:
            if runtime_data is None or runtime_data.stopping:
                return
            batch = list(websocket_messages)
            websocket_messages.clear()
            working = {
                key: dict(value)
                for key, value in runtime_data.state_store.data["states"].items()
            }
            state_deltas: dict[str, dict[str, Any]] = {}
            device_deltas: dict[str, dict[str, Any]] = {}

            def merge_device_delta(
                target: dict[str, Any],
                incoming: dict[str, Any],
            ) -> None:
                for field, value in incoming.items():
                    if field in ("condition", "settings") and isinstance(value, dict):
                        target.setdefault(field, {}).update(value)
                    else:
                        target[field] = value

            for message in batch:
                message_type = message["type"]
                source_name = (
                    "condition" if message_type == "condition" else "settings"
                )
                for item in message["data"]:
                    if not isinstance(item, dict):
                        continue
                    if item.get("id") is None:
                        continue
                    source = item.get(source_name)
                    if not isinstance(source, dict) or not source:
                        continue
                    key = str(item["id"])
                    if key not in working:
                        continue
                    state_delta, device_delta = (
                        normalize_condition_delta(source)
                        if message_type == "condition"
                        else normalize_settings_delta(source, working[key])
                    )
                    # Retain accepted same-value observations. Their revisions
                    # must still advance so an older HTTP response cannot win.
                    state_deltas.setdefault(key, {}).update(state_delta)
                    merge_device_delta(
                        device_deltas.setdefault(key, {}),
                        device_delta,
                    )
                    working[key].update(state_delta)
            changed = False
            for key, state_delta in state_deltas.items():
                update = runtime_data.state_store.apply_websocket_delta(
                    key,
                    state_delta=state_delta,
                    device_delta=device_deltas.get(key),
                )
                changed = changed or update.changed
            if changed and not runtime_data.stopping:
                coordinator.async_set_updated_data(
                    runtime_data.state_store.data
                )
        finally:
            if runtime_data is not None:
                runtime_data.websocket_message_task = None
                if websocket_messages and not runtime_data.stopping:
                    runtime_data.websocket_message_task = _create_entry_task(
                        _drain_websocket_messages(),
                        "atmeex websocket drain",
                    )

    def on_websocket_message(message: dict[str, Any]) -> bool:
        if runtime_data is None or runtime_data.stopping:
            return False
        if message.get("type") not in ("condition", "settings"):
            return False
        if not isinstance(message.get("data"), list):
            return False
        if len(websocket_messages) >= websocket_limit:
            runtime_data.websocket_overflow_count += 1
            _schedule_overflow_resync()
            return False
        websocket_messages.append(message)
        task = runtime_data.websocket_message_task
        if task is None or task.done():
            runtime_data.websocket_message_task = _create_entry_task(
                _drain_websocket_messages(),
                "atmeex websocket drain",
            )
        return True
~~~

Later values overwrite earlier fields in deltas, each device reaches AtmeexStateStore once per batch, and coordinator publication occurs once. The 501st queued message is rejected before eviction and causes one full resync.
Import `normalize_condition_delta` and `normalize_settings_delta` from
`.helpers`; do not use the compatibility `apply_*_update` wrappers in this
drain. Passing accepted unchanged fields through to the store is required for
the same-value revision race covered by Plan 2.

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_websocket_integration.py
~~~

Expected: all integration tests pass.

~~~bash
git add custom_components/atmeex_cloud/__init__.py tests/test_websocket_integration.py
git commit -m "fix: coalesce bounded WebSocket updates"
~~~

### Task 6: Make setup transactional and startup coroutine-safe

**Files:**

- Modify: custom_components/atmeex_cloud/__init__.py
- Modify: tests/test_setup.py

- [ ] **Step 1: Add RED setup-order and rollback tests**

Add:

~~~python
@pytest.mark.asyncio
async def test_websocket_starts_only_after_platforms(monkeypatch):
    runtime_order: list[str] = []
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, runtime_order)
    hass.config_entries.async_forward_entry_setups.side_effect = (
        lambda entry, platforms: runtime_order.append("platforms")
    )
    manager.connect.side_effect = lambda: runtime_order.append("websocket") or True

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    await entry.runtime_data.websocket_start_task
    assert runtime_order == ["platforms", "websocket"]


@pytest.mark.asyncio
async def test_platform_failure_rolls_back_runtime(monkeypatch):
    entry, hass, manager = _setup_lifecycle_fakes(monkeypatch, [])
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
        "platform failed"
    )

    with pytest.raises(RuntimeError, match="platform failed"):
        await atmeex_init.async_setup_entry(hass, entry)

    manager.disconnect.assert_awaited_once()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        entry,
        PLATFORMS,
    )
    assert entry.runtime_data is None


@pytest.mark.asyncio
async def test_start_task_creation_failure_closes_coroutine(monkeypatch):
    entry, hass, _manager = _setup_lifecycle_fakes(monkeypatch, [])
    monkeypatch.setattr(
        atmeex_init,
        "async_create_background_task",
        MagicMock(side_effect=RuntimeError("scheduler stopped")),
    )

    with pytest.raises(RuntimeError, match="scheduler stopped"):
        await atmeex_init.async_setup_entry(hass, entry)

    assert entry.runtime_data is None
~~~

Add this complete helper above the tests:

~~~python
def _setup_lifecycle_fakes(monkeypatch, order):
    import custom_components.atmeex_cloud.websocket as websocket_mod
    from custom_components.atmeex_cloud.state_store import AtmeexStateStore

    device = AtmeexDevice.from_raw({
        "id": 1,
        "name": "Device",
        "model": "m",
        "online": True,
        "condition": {"pwr_on": 1},
        "settings": {},
    })

    class FakeApi:
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = None
            self.token = "token"
            self.get_devices = AsyncMock(return_value=[device])
            self.get_device = AsyncMock(return_value=device)
            self.async_refresh_access_token = AsyncMock()

    class FakeCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.data = {
                "devices": [device.to_ha_dict()],
                "device_map": {"1": device},
                "states": {"1": {"pwr_on": True}},
            }
            self.state_store = AtmeexStateStore(self.data)
            self.async_request_refresh = AsyncMock()

        def setup_update(self, *, api, state_store, fire_logbook_event):
            self.api = api
            self.state_store = state_store

        async def async_config_entry_first_refresh(self):
            return

        def async_set_updated_data(self, data):
            self.data = data

    manager = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", FakeCoordinator)
    monkeypatch.setattr(
        websocket_mod,
        "WebSocketManager",
        lambda **kwargs: manager,
    )
    monkeypatch.setattr(
        atmeex_init,
        "async_get_clientsession",
        lambda hass: SimpleNamespace(ws_connect=AsyncMock()),
    )

    config_entries = SimpleNamespace(
        async_forward_entry_setups=AsyncMock(),
        async_unload_platforms=AsyncMock(return_value=True),
    )
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
        config_entries=config_entries,
        async_create_task=lambda coro, **kwargs: asyncio.create_task(coro),
    )
    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "secret"},
        options={"enable_websocket": True},
        entry_id="entry1",
        runtime_data=None,
        add_update_listener=lambda callback: lambda: None,
        async_on_unload=lambda callback: None,
        async_start_reauth=MagicMock(),
    )
    return entry, hass, manager
~~~

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_setup.py -k "starts_only_after or rolls_back_runtime or creation_failure"
~~~

Expected: WebSocket work starts before forwarding, platform failure leaves runtime work behind, and scheduler failure emits an un-awaited startup coroutine warning.

- [ ] **Step 3: Implement entry task creation, wiring, and rollback**

Use this task helper after runtime_data is constructed:

~~~python
    def _create_entry_task(
        coro: Coroutine[Any, Any, Any],
        name: str,
    ) -> asyncio.Task[Any]:
        try:
            task = async_create_background_task(hass, coro, name)
        except BaseException:
            coro.close()
            raise
        return runtime_data.track_task(task)
~~~

Add the exact one-shot reauth callback and manager construction:

~~~python
    websocket_reauth_started = False

    def _on_websocket_auth_failure() -> None:
        nonlocal websocket_reauth_started
        if websocket_reauth_started or runtime_data.stopping:
            return
        websocket_reauth_started = True
        entry.async_start_reauth(hass)

    runtime_data.websocket_manager = WebSocketManager(
        session=session,
        token_getter=lambda: api.token,
        on_message=on_websocket_message,
        task_factory=_create_entry_task,
        on_auth_failure=_on_websocket_auth_failure,
        on_token_refresh=api.async_refresh_access_token,
    )
~~~

Add the cleanup routine here so both setup rollback and Task 7 unload use the same implementation:

~~~python
async def _async_cleanup_runtime(runtime: AtmeexRuntimeData) -> None:
    runtime.stopping = True
    manager = runtime.websocket_manager
    if manager is not None:
        try:
            await manager.disconnect()
        except Exception as err:
            _LOGGER.warning(
                "Atmeex WebSocket cleanup failed: %s",
                type(err).__name__,
            )
    current = asyncio.current_task()
    tasks = {
        task
        for task in runtime.tasks
        if task is not current and not task.done()
    }
    for task in tasks:
        task.cancel()
    if tasks:
        done, pending = await asyncio.wait(
            tasks,
            timeout=_UNLOAD_TASK_TIMEOUT_SEC,
        )
        for task in done:
            if not task.cancelled():
                task.exception()
        runtime.tasks.difference_update(done)
        if pending:
            _LOGGER.warning(
                "%d Atmeex tasks exceeded the unload timeout",
                len(pending),
            )
    runtime.refresh_tasks.clear()
    runtime.websocket_start_task = None
    runtime.websocket_message_task = None
    runtime.websocket_resync_task = None
    runtime.inventory_watchdog_task = None
~~~

Then use this setup tail:

~~~python
    entry.runtime_data = runtime_data
    platform_forward_attempted = False
    try:
        platform_forward_attempted = True
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        if runtime_data.websocket_manager is not None:
            runtime_data.websocket_start_task = _create_entry_task(
                runtime_data.websocket_manager.connect(),
                "atmeex websocket startup",
            )
        watchdog = getattr(
            coordinator,
            "async_inventory_watchdog",
            None,
        )
        if callable(watchdog):
            runtime_data.inventory_watchdog_task = _create_entry_task(
                watchdog(),
                "atmeex inventory watchdog",
            )
    except BaseException:
        try:
            if platform_forward_attempted:
                await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception as err:
            _LOGGER.warning(
                "Atmeex platform rollback failed: %s",
                type(err).__name__,
            )
        finally:
            await _async_cleanup_runtime(runtime_data)
            entry.runtime_data = None
        raise
    return True
~~~

The watchdog hook is concrete feature detection for Plan 5; on this plan's code it remains None. No version-string comparison is allowed.

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q -W error::RuntimeWarning tests/test_setup.py
~~~

Expected: all setup tests pass with no un-awaited coroutine warning.

~~~bash
git add custom_components/atmeex_cloud/__init__.py tests/test_setup.py
git commit -m "fix: make Atmeex setup transactional"
~~~

### Task 7: Preserve communications on unload failure and clean everything on success

**Files:**

- Modify: custom_components/atmeex_cloud/__init__.py
- Modify: tests/test_unload.py

- [ ] **Step 1: Add RED unload-order tests**

Add:

~~~python
@pytest.mark.asyncio
async def test_platform_unload_false_leaves_runtime_operational():
    runtime, entry, hass = _loaded_runtime(unload_result=False)
    task = runtime.track_task(asyncio.create_task(asyncio.Event().wait()))

    assert await atmeex_init.async_unload_entry(hass, entry) is False
    assert runtime.stopping is False
    assert task.cancelled() is False
    runtime.websocket_manager.disconnect.assert_not_awaited()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_successful_unload_closes_socket_and_awaits_all_tasks():
    runtime, entry, hass = _loaded_runtime(unload_result=True)
    stopped = asyncio.Event()

    async def producer() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    runtime.track_task(asyncio.create_task(producer()))
    await asyncio.sleep(0)

    assert await atmeex_init.async_unload_entry(hass, entry) is True
    assert stopped.is_set()
    runtime.websocket_manager.disconnect.assert_awaited_once()
    assert runtime.tasks == set()
    assert entry.runtime_data is None
~~~

Add this complete helper above the tests:

~~~python
def _loaded_runtime(*, unload_result):
    manager = SimpleNamespace(disconnect=AsyncMock())

    async def refresh_device(device_id):
        return

    runtime = AtmeexRuntimeData(
        api=MagicMock(),
        coordinator=MagicMock(),
        state_store=MagicMock(),
        command_executor=MagicMock(),
        refresh_device=refresh_device,
        websocket_manager=manager,
    )
    entry = SimpleNamespace(entry_id="entry1", runtime_data=runtime)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_unload_platforms=AsyncMock(
                return_value=unload_result
            ),
        ),
    )
    return runtime, entry, hass
~~~

- [ ] **Step 2: Run and verify RED**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_unload.py -k "unload_false or awaits_all_tasks"
~~~

Expected: unload-false currently disconnects first, and successful unload does not own refresh/reconnect/watchdog tasks or clear runtime_data.

- [ ] **Step 3: Use the shared bounded cleanup in ordered unload**

Keep Task 6's _async_cleanup_runtime implementation unchanged and replace async_unload_entry:

~~~python
async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    runtime: AtmeexRuntimeData = entry.runtime_data
    unloaded = await hass.config_entries.async_unload_platforms(
        entry,
        PLATFORMS,
    )
    if not unloaded:
        return False
    await _async_cleanup_runtime(runtime)
    entry.runtime_data = None
    return True
~~~

Platform entities therefore retain usable communications until Home Assistant confirms their unload. After stopping becomes true, callback and drain guards prevent coordinator mutation.

- [ ] **Step 4: Run and commit**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_unload.py
~~~

Expected: all unload tests pass.

~~~bash
git add custom_components/atmeex_cloud/__init__.py tests/test_unload.py
git commit -m "fix: own Atmeex unload lifecycle"
~~~

### Task 8: Run lifecycle and public-contract gates

**Files:**

- Verify only; no production changes expected.

- [ ] **Step 1: Run the lifecycle subsystem with warnings promoted**

Run:

~~~bash
.venv/bin/python -m pytest -q -W error::RuntimeWarning tests/test_compat.py tests/test_runtime.py tests/test_websocket_manager.py tests/test_websocket_integration.py tests/test_setup.py tests/test_unload.py tests/test_refresh_device.py
~~~

Expected: 0 failures and 0 RuntimeWarning failures.

- [ ] **Step 2: Run the complete suite**

Run:

~~~bash
.venv/bin/python -m pytest -q -W error::RuntimeWarning
~~~

Expected: the complete suite passes.

- [ ] **Step 3: Compile production modules**

Run:

~~~bash
.venv/bin/python -m compileall -q custom_components/atmeex_cloud
~~~

Expected: exit status 0 and no output.

- [ ] **Step 4: Confirm the preserved Home Assistant surface**

Run:

~~~bash
.venv/bin/python -m pytest -q tests/test_climate.py tests/test_fan.py tests/test_select.py tests/test_switch.py tests/test_setup.py -k "unique_id or service or options or setup_entry"
~~~

Expected: all selected tests pass; entity IDs, service names and schemas, options, translations, and automation inputs are unchanged.

## Completion criteria

- Home Assistant 2024.8 and current task signatures both use compat.async_create_background_task.
- Runtime owns API, coordinator, state store, command executor, RecoveryRefresh, optional WebSocket manager, stopping state, every entry task, and named startup/drain/watchdog/resync references.
- Handshake 401/403 performs one serialized AtmeexApi.async_refresh_access_token attempt; repeated rejection or exhausted recovery requests reauth once.
- Transport backoff resets on upgrade, while application unauthorized count resets only after an accepted condition/settings message.
- Immediate close transfers reconnect ownership, and disconnect closes the captured socket before listener cleanup can erase its reference.
- Refresh waiters use asyncio.shield; cancellation or timeout cannot cancel or evict the shared owner.
- The 500-message buffer detects overflow before eviction, increments its counter, schedules one authoritative resync, coalesces later field values, and publishes once per drain.
- Platform-forward or startup failure closes coroutines, cleans runtime, and clears entry.runtime_data.
- Platform unload False leaves communications operational; successful unload stops callbacks, closes sockets, cancels and awaits all tracked work, and prevents post-unload publication.
- The WebSocket handshake timeout is passed as `ClientWSTimeout` when aiohttp provides it and as a float otherwise; no deprecated bare-float timeout reaches a typed-timeout aiohttp.
- Existing public entity, service, option, translation, and automation contracts remain unchanged.
