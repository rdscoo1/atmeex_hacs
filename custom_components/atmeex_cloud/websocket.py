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
import json
import logging
import time
from contextlib import suppress
from typing import Any, Callable, Optional
from dataclasses import dataclass

import aiohttp
from aiohttp import WSMsgType, ClientWebSocketResponse

_LOGGER = logging.getLogger(__name__)

# WebSocket configuration
WS_BASE_URL = "wss://ws.iot.atmeex.com"  # Verified working endpoint
WS_RECONNECT_DELAY_MIN = 1.0  # seconds
WS_RECONNECT_DELAY_MAX = 60.0  # seconds
WS_PING_INTERVAL = 30.0  # seconds
WS_PING_TIMEOUT = 10.0  # seconds
WS_MAX_UNAUTHORIZED_BEFORE_REAUTH = 5  # trigger reauth after this many consecutive failures


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
    
    Usage:
        manager = WebSocketManager(token_getter, on_message_callback)
        await manager.connect()
        # ... manager runs in background ...
        await manager.disconnect()
    """
    
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token_getter: Callable[[], str] | str,
        on_message: Callable[[dict[str, Any]], None],
        config: Optional[WebSocketConfig] = None,
        on_auth_failure: Optional[Callable[[], None]] = None,
        on_token_refresh: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Initialize WebSocket manager.

        Args:
            session: aiohttp ClientSession for WebSocket connection
            token_getter: Callable returning current auth token, or static token string.
                Use a callable so reconnects pick up refreshed tokens.
            on_message: Callback function for received messages
            config: Optional WebSocket configuration
            on_auth_failure: Optional callback invoked when the server rejects the token
                with HTTP 401 or 403.  After calling this callback the manager stops
                all reconnect attempts so the caller can start a reauth flow.
            on_token_refresh: Optional async/sync callback to request a token refresh
                (e.g. coordinator.async_request_refresh).  Called on 'unauthorized'
                messages so the next reconnect picks up a fresh token.
        """
        self._session = session
        if callable(token_getter):
            self._token_getter: Callable[[], str] = token_getter
        else:
            static_token = token_getter
            self._token_getter = lambda: static_token
        self._on_message = on_message
        self._on_auth_failure = on_auth_failure
        self._on_token_refresh = on_token_refresh
        self._config = config or WebSocketConfig()
        
        self._ws: Optional[ClientWebSocketResponse] = None
        self._running = False
        self._reconnect_task: Optional[asyncio.Task[None]] = None
        self._listen_task: Optional[asyncio.Task[None]] = None
        
        self._reconnect_delay = self._config.reconnect_delay_min
        self._last_message_time = 0.0
        self._consecutive_auth_failures = 0
        
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

    async def _connect_once(self) -> bool:
        """Try exactly one WebSocket connection attempt."""
        try:
            _LOGGER.info("Connecting to Atmeex WebSocket: %s", self._config.base_url)
            token = self._token_getter() or ""
            headers = {"Authorization": f"Bearer {token}"}
            self._ws = await self._session.ws_connect(
                self._config.base_url,
                headers=headers,
                heartbeat=self._config.ping_interval,
                timeout=self._config.ping_timeout,
            )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                _LOGGER.warning(
                    "WebSocket authentication failed (HTTP %s) — stopping reconnect",
                    err.status,
                )
                # Permanently stop reconnect; let the caller trigger a reauth flow.
                self._running = False
                if self._on_auth_failure is not None:
                    self._on_auth_failure()
            else:
                _LOGGER.warning("WebSocket handshake error: %s", err)
            self._ws = None
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Failed to connect to WebSocket: %s", err)
            self._ws = None
            return False

        self._reconnect_delay = self._config.reconnect_delay_min
        self._consecutive_auth_failures = 0
        await self._cancel_task(self._listen_task)
        self._listen_task = asyncio.create_task(self._listen())
        _LOGGER.info("WebSocket connected successfully")
        return True

    def _ensure_reconnect_task(self) -> None:
        """Start reconnect loop if not running already."""
        if not self._running:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _cancel_task(self, task: Optional[asyncio.Task[None]]) -> None:
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

        await self._cancel_task(self._listen_task)
        await self._cancel_task(self._reconnect_task)
        self._listen_task = None
        self._reconnect_task = None

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        _LOGGER.info("WebSocket disconnected")
    
    async def _listen(self) -> None:
        """Listen for incoming WebSocket messages."""
        if not self._ws:
            return
        
        try:
            async for msg in self._ws:
                if not self._running:
                    break
                
                self._last_message_time = time.monotonic()
                
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
                    _LOGGER.error("WebSocket error: %s", msg.data)
                    break
                    
        except asyncio.CancelledError:
            _LOGGER.debug("WebSocket listen task cancelled")
            raise
            
        except Exception as err:
            _LOGGER.error("Error in WebSocket listen loop: %s", err)
            
        finally:
            self._ws = None
            # Connection lost - attempt reconnection if still running.
            if self._running:
                _LOGGER.info("WebSocket connection lost, scheduling reconnect")
                self._ensure_reconnect_task()
    
    async def _handle_message(self, data: str) -> None:
        """Handle incoming WebSocket message.
        
        Args:
            data: Raw message data (JSON string)
        """
        try:
            message = json.loads(data)
            
            _LOGGER.debug("WebSocket message received: %s", message)

            # Handle server-level 'unauthorized' rejection.
            # The TCP handshake succeeds but the server immediately sends
            # {"type": "unauthorized"} and closes the connection.  Without
            # this check the manager would reconnect every ~1 s with the
            # same stale token.  We apply exponential backoff and wait for
            # the HTTP polling cycle to refresh the token.
            if message.get("type") == "unauthorized":
                self._consecutive_auth_failures += 1

                if self._consecutive_auth_failures >= WS_MAX_UNAUTHORIZED_BEFORE_REAUTH:
                    _LOGGER.warning(
                        "WebSocket received %d consecutive 'unauthorized' "
                        "messages — stopping reconnect and requesting reauth",
                        self._consecutive_auth_failures,
                    )
                    self._running = False
                    if self._on_auth_failure is not None:
                        self._on_auth_failure()
                    if self._ws and not self._ws.closed:
                        await self._ws.close()
                    return

                delay = min(
                    self._config.reconnect_delay_min
                    * (2 ** self._consecutive_auth_failures),
                    self._config.reconnect_delay_max,
                )
                self._reconnect_delay = delay
                _LOGGER.warning(
                    "WebSocket received 'unauthorized' message "
                    "(attempt %d/%d), requesting token refresh, "
                    "next reconnect in %.1f s",
                    self._consecutive_auth_failures,
                    WS_MAX_UNAUTHORIZED_BEFORE_REAUTH,
                    delay,
                )

                # Ask the coordinator to refresh data (and the token)
                # so the next reconnect picks up a valid token.
                if self._on_token_refresh is not None:
                    try:
                        result = self._on_token_refresh()
                        if asyncio.isfuture(result) or asyncio.iscoroutine(result):
                            await result
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "Token refresh request failed: %s", err
                        )

                if self._ws and not self._ws.closed:
                    await self._ws.close()
                return

            # Successful data message — reset auth failure counter.
            self._consecutive_auth_failures = 0

            # Call the message handler callback
            if callable(self._on_message):
                try:
                    self._on_message(message)
                except Exception as err:
                    _LOGGER.error("Error in message handler: %s", err)
                    
        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse WebSocket message: %s", err)
            
        except Exception as err:
            _LOGGER.error("Error handling WebSocket message: %s", err)
    
    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        while self._running and not self.is_connected:
            _LOGGER.info(
                "Attempting WebSocket reconnect in %.1f seconds",
                self._reconnect_delay
            )
            
            await asyncio.sleep(self._reconnect_delay)
            
            if not self._running or self.is_connected:
                break

            success = await self._connect_once()
            if success:
                _LOGGER.info("WebSocket reconnected successfully")
                break

            self._reconnect_delay = min(
                self._reconnect_delay * 2,
                self._config.reconnect_delay_max,
            )
            _LOGGER.warning(
                "WebSocket reconnect failed, next attempt in %.1f seconds",
                self._reconnect_delay,
            )

        self._reconnect_task = None
    
    @property
    def is_connected(self) -> bool:
        """Check if WebSocket is currently connected."""
        return self._running and self._ws is not None and not self._ws.closed
    
    @property
    def last_message_age(self) -> float:
        """Get time since last message received (seconds)."""
        if self._last_message_time == 0:
            return float('inf')
        return time.monotonic() - self._last_message_time
