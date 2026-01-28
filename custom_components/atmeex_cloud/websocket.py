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
import logging
import time
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
        manager = WebSocketManager(token, on_message_callback)
        await manager.connect()
        # ... manager runs in background ...
        await manager.disconnect()
    """
    
    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: str,
        on_message: Callable[[dict[str, Any]], None],
        config: Optional[WebSocketConfig] = None,
    ) -> None:
        """Initialize WebSocket manager.
        
        Args:
            session: aiohttp ClientSession for WebSocket connection
            token: Authentication token from API login
            on_message: Callback function for received messages
            config: Optional WebSocket configuration
        """
        self._session = session
        self._token = token
        self._on_message = on_message
        self._config = config or WebSocketConfig()
        
        self._ws: Optional[ClientWebSocketResponse] = None
        self._running = False
        self._reconnect_task: Optional[asyncio.Task] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        
        self._reconnect_delay = self._config.reconnect_delay_min
        self._last_message_time = 0.0
        
    async def connect(self) -> bool:
        """Connect to WebSocket server.
        
        Returns:
            True if connection successful, False otherwise
        """
        if self._running:
            _LOGGER.warning("WebSocket already running")
            return True
        
        try:
            _LOGGER.info("Connecting to Atmeex WebSocket: %s", self._config.base_url)
            
            # Verified working method: Authorization header (not URL token)
            # WebSocket requires Bearer token in Authorization header
            headers = {
                "Authorization": f"Bearer {self._token}",
            }
            
            self._ws = await self._session.ws_connect(
                self._config.base_url,
                headers=headers,
                heartbeat=self._config.ping_interval,
                timeout=self._config.ping_timeout,
            )
            
            _LOGGER.info("WebSocket connected successfully")
            self._running = True
            self._reconnect_delay = self._config.reconnect_delay_min
            
            # Start listening for messages
            self._listen_task = asyncio.create_task(self._listen())
            
            return True
            
        except Exception as err:
            _LOGGER.error("Failed to connect to WebSocket: %s", err)
            self._running = False
            return False
    
    async def disconnect(self) -> None:
        """Disconnect from WebSocket server gracefully."""
        _LOGGER.info("Disconnecting from WebSocket")
        self._running = False
        
        # Cancel background tasks
        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        
        if self._ping_task and not self._ping_task.done():
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
        
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        
        # Close WebSocket connection
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
            # Connection lost - attempt reconnection if still running
            if self._running:
                _LOGGER.info("WebSocket connection lost, scheduling reconnect")
                self._reconnect_task = asyncio.create_task(self._reconnect())
    
    async def _handle_message(self, data: str) -> None:
        """Handle incoming WebSocket message.
        
        Args:
            data: Raw message data (JSON string)
        """
        try:
            import json
            message = json.loads(data)
            
            _LOGGER.debug("WebSocket message received: %s", message)
            
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
        while self._running:
            _LOGGER.info(
                "Attempting WebSocket reconnect in %.1f seconds",
                self._reconnect_delay
            )
            
            await asyncio.sleep(self._reconnect_delay)
            
            if not self._running:
                break
            
            # Check if already reconnected (e.g., by another task)
            if self.is_connected:
                _LOGGER.debug("WebSocket already reconnected, skipping reconnect attempt")
                break
            
            success = await self.connect()
            
            if success:
                _LOGGER.info("WebSocket reconnected successfully")
                break
            else:
                # Exponential backoff
                self._reconnect_delay = min(
                    self._reconnect_delay * 2,
                    self._config.reconnect_delay_max
                )
                _LOGGER.warning(
                    "WebSocket reconnect failed, next attempt in %.1f seconds",
                    self._reconnect_delay
                )
    
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
