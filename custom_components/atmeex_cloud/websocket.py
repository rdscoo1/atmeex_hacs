"""WebSocket client for real-time updates from Atmeex Cloud API.

This module implements a WebSocket connection to the Atmeex Cloud API for
receiving real-time device state updates, reducing the need for frequent polling.

Architecture:
- WebSocketManager: Main class managing the WebSocket connection
- Automatic reconnection with exponential backoff
- Integration with DataUpdateCoordinator for state updates
- Graceful fallback to polling if WebSocket fails
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
import json
import logging
import time
from typing import Any

import aiohttp
from aiohttp import ClientWebSocketResponse, WSMsgType

from .api import ApiError

_LOGGER = logging.getLogger(__name__)

# WebSocket configuration
WS_BASE_URL = "wss://ws.iot.atmeex.com"  # Verified working endpoint
WS_RECONNECT_DELAY_MIN = 1.0  # seconds
WS_RECONNECT_DELAY_MAX = 60.0  # seconds
WS_PING_INTERVAL = 30.0  # seconds
WS_PING_TIMEOUT = 10.0  # seconds
WS_MAX_UNAUTHORIZED_BEFORE_REAUTH = 5

MessageCallback = Callable[[dict[str, Any]], bool]
TokenRecoveryCallback = Callable[[], Awaitable[None]]
TaskFactory = Callable[
    [Coroutine[Any, Any, None], str], asyncio.Task[None]
]


try:
    from aiohttp import ClientWSTimeout

    def _ws_handshake_timeout(seconds: float) -> Any:
        """Build the timeout object required by current aiohttp."""
        return ClientWSTimeout(ws_close=seconds)

except ImportError:

    def _ws_handshake_timeout(seconds: float) -> float:
        """Use the float timeout required by Home Assistant 2024.8."""
        return seconds


@dataclass
class WebSocketConfig:
    """Configuration for WebSocket connection."""

    base_url: str = WS_BASE_URL
    reconnect_delay_min: float = WS_RECONNECT_DELAY_MIN
    reconnect_delay_max: float = WS_RECONNECT_DELAY_MAX
    ping_interval: float = WS_PING_INTERVAL
    ping_timeout: float = WS_PING_TIMEOUT


class WebSocketManager:
    """Manages WebSocket connection to Atmeex Cloud API.

    Features:
    - Automatic connection and authentication
    - Exponential backoff reconnection strategy
    - Message handling and routing
    - Graceful shutdown
    """

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
        """Initialize WebSocket manager.

        Args:
            session: aiohttp ClientSession for WebSocket connection
            token_getter: Callable returning the current authentication token.
            on_message: Callback function for received messages
            config: Optional WebSocket configuration
            task_factory: Entry-owned task creation callback.
            on_auth_failure: Callback that starts config-entry reauthentication.
            on_token_refresh: Async callback that refreshes the API token.
        """
        self._session = session
        self._token_getter = token_getter
        self._on_message = on_message
        self._task_factory = task_factory
        self._on_auth_failure = on_auth_failure
        self._on_token_refresh = on_token_refresh
        self._config = config or WebSocketConfig()

        self._ws: ClientWebSocketResponse | None = None
        self._running = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._listen_task: asyncio.Task[None] | None = None

        self._reconnect_delay = self._config.reconnect_delay_min
        self._last_message_time = 0.0
        self._transport_failures = 0
        self._application_unauthorized_count = 0
        self._reauth_requested = False

    async def connect(self) -> bool:
        """Start WebSocket connection and keep reconnect loop active on failures."""
        if self.is_connected:
            _LOGGER.debug("WebSocket already connected")
            return True

        self._running = True
        connected = await self._connect_once()
        if not connected:
            self._ensure_reconnect_task()
        return connected

    async def _connect_once(self, *, allow_recovery: bool = True) -> bool:
        """Try exactly one WebSocket connection attempt."""
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
        listen_coro = self._listen(ws)
        self._listen_task = None
        try:
            self._listen_task = self._task_factory(
                listen_coro,
                "atmeex websocket listener",
            )
        except BaseException:
            listen_coro.close()
            try:
                if not ws.closed:
                    await ws.close()
            finally:
                if self._ws is ws:
                    self._ws = None
            raise
        return True

    def _request_reauth(self) -> None:
        """Stop reconnects and request reauthentication at most once."""
        self._running = False
        if self._reauth_requested:
            return
        self._reauth_requested = True
        self._on_auth_failure()

    def _record_transport_failure(self) -> None:
        """Record one transport failure and update transport backoff."""
        self._transport_failures += 1
        if self._transport_failures == 1:
            self._reconnect_delay = min(
                self._config.reconnect_delay_min,
                self._config.reconnect_delay_max,
            )
            return
        self._reconnect_delay = min(
            max(
                self._reconnect_delay * 2,
                self._config.reconnect_delay_min,
            ),
            self._config.reconnect_delay_max,
        )

    def _next_reconnect_delay(self) -> float:
        """Return the larger transport or application-auth backoff."""
        auth_delay = self._config.reconnect_delay_min
        if self._application_unauthorized_count:
            auth_delay = min(
                self._config.reconnect_delay_min
                * (2**self._application_unauthorized_count),
                self._config.reconnect_delay_max,
            )
        return max(self._reconnect_delay, auth_delay)

    def _ensure_reconnect_task(self) -> None:
        """Start reconnect loop if not running already."""
        if not self._running:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        reconnect_coro = self._reconnect()
        self._reconnect_task = None
        try:
            self._reconnect_task = self._task_factory(
                reconnect_coro,
                "atmeex websocket reconnect",
            )
        except BaseException:
            reconnect_coro.close()
            raise

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        """Cancel background task and swallow cancellation errors."""
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def disconnect(self) -> None:
        """Disconnect from WebSocket server gracefully."""
        _LOGGER.info("Disconnecting from WebSocket")
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
        _LOGGER.info("WebSocket disconnected")

    async def _listen(self, ws: ClientWebSocketResponse) -> None:
        """Listen on one captured WebSocket until it closes."""
        try:
            async for msg in ws:
                if not self._running:
                    break

                if msg.type == WSMsgType.TEXT:
                    await self._handle_message(msg.data)

                elif msg.type == WSMsgType.BINARY:
                    _LOGGER.debug("Received binary message (ignored)")

                elif msg.type == WSMsgType.PING:
                    _LOGGER.debug("Received ping")

                elif msg.type == WSMsgType.PONG:
                    _LOGGER.debug("Received pong")

                elif msg.type == WSMsgType.CLOSE:
                    _LOGGER.warning("WebSocket closed by server")
                    break

                elif msg.type == WSMsgType.ERROR:
                    _LOGGER.error("WebSocket transport reported an error")
                    break

        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError):
            self._record_transport_failure()
        finally:
            try:
                if not ws.closed:
                    await ws.close()
            finally:
                if self._ws is ws:
                    self._ws = None
                if self._running:
                    self._ensure_reconnect_task()

    async def _handle_message(self, data: str) -> None:
        """Validate and handle one raw WebSocket message."""
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

    async def _close_active_socket(self) -> None:
        """Close the currently active socket without changing ownership."""
        ws = self._ws
        if ws is not None and not ws.closed:
            await ws.close()

    @staticmethod
    def _is_valid_state_message(message: dict[str, Any]) -> bool:
        """Return whether a message contains a usable device state delta."""
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

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
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

    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._running and self._ws is not None and not self._ws.closed

    @property
    def last_message_age(self) -> float:
        """Get time since last message received (seconds)."""
        if self._last_message_time == 0:
            return float("inf")
        return time.monotonic() - self._last_message_time
