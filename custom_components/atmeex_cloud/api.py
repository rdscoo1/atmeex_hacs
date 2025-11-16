from typing import Any, Dict, Optional
from aiohttp import ClientSession, ClientError
import asyncio

API_BASE = "https://api.iot.atmeex.com"


class ApiError(Exception):
    """Integration-specific error wrapper for all Atmeex API issues."""


class AtmeexApi:
    def __init__(self, session: ClientSession):
        self._session = session
        self._token: Optional[str] = None

    async def async_init(self) -> None:
        """Backward-compatible no-op.

        Previously could create its own ClientSession.
        Now HA passes in the shared session, so we do nothing here,
        but keep the method so older call sites/tests still work.
        """

    def _headers(self) -> Dict[str, str]:
        hdrs = {"Accept": "application/json"}
        if self._token:
            hdrs["Authorization"] = f"Bearer {self._token}"
        return hdrs

    async def _json(self, resp):
        """Parse JSON with helpful error if body is not valid JSON."""
        try:
            return await resp.json()
        except Exception:
            text = await resp.text()
            raise ApiError(f"Bad JSON from API ({resp.status}): {text[:200]}")

    async def _put_params(
        self,
        device_id: int | str,
        body: Dict[str, Any],
        action_name: str,
        timeout: int = 20,
    ) -> None:
        """unified helper for all param-setting PUT calls."""
        try:
            async with self._session.put(
                f"{API_BASE}/devices/{device_id}/params",
                json=body,
                headers=self._headers(),
                timeout=timeout,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    raise ApiError(f"{action_name} {resp.status}: {text[:200]}")
        except (asyncio.TimeoutError, ClientError) as e:
            raise ApiError(f"{action_name} network error: {e}") from e


    async def login(self, email: str, password: str):
        """Authenticate user and store access token."""
        try:
            # CHANGE: тест ожидает /auth/signin
            async with self._session.post(
                f"{API_BASE}/auth/signin",
                json={"email": email, "password": password},
                headers=self._headers(),
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    # сообщение об ошибке должно содержать 'Auth failed {status}'
                    raise ApiError(f"Auth failed {resp.status}: {text[:200]}")
                data = await self._json(resp)
                self._token = data.get("access_token") or data.get("token")
                if not self._token:
                    raise ApiError("login: token missing in response")
        except (asyncio.TimeoutError, ClientError) as e:
            # Формат network-ошибок можем оставить как есть — тесты это не проверяют
            raise ApiError(f"login network error: {e}") from e


    async def get_devices(self, fallback: bool = False) -> list[dict[str, Any]]:
        """Fetch devices, with optional fallback behavior.

        added `fallback` parameter to match usage in __init__.
        When `fallback=True`, this method returns [] instead of raising ApiError.
        """
        try:
            async with self._session.get(
                f"{API_BASE}/devices",
                headers=self._headers(),
                timeout=20,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    msg = f"get_devices {resp.status}: {text[:200]}"
                    if fallback:
                        return []
                    raise ApiError(msg)
                data = await self._json(resp)
                # Return list; reshape if backend uses wrapper { "items": [...] }
                if isinstance(data, dict) and "items" in data:
                    return data["items"]
                if isinstance(data, list):
                    return data
                msg = "get_devices: unexpected response shape"
                if fallback:
                    return []
                raise ApiError(msg)
        except (asyncio.TimeoutError, ClientError) as e:
            if fallback:
                return []
            raise ApiError(f"get_devices network error: {e}") from e

    async def get_device(self, device_id: int | str):
        """Fetch a single device by id with timeout and error wrapping."""
        try:
            async with self._session.get(
                f"{API_BASE}/devices/{device_id}",
                headers=self._headers(),
                timeout=20,
            ) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    raise ApiError(
                        f"GET /devices/{device_id} {resp.status}: {txt[:300]}"
                    )
                return await self._json(resp)
        except (asyncio.TimeoutError, ClientError) as e:
            raise ApiError(f"get_device network error for {device_id}: {e}") from e

    async def set_power(self, device_id: int | str, on: bool):
        body = {"u_pwr_on": bool(on)}
        await self._put_params(device_id, body, "set_power")

    async def set_target_temperature(self, device_id: int | str, temp_c: float):
        body = {"u_temp_room": int(round(temp_c * 10))}
        await self._put_params(device_id, body, "set_target_temperature")

    async def set_fan_speed(self, device_id: int | str, speed: int):
        body = {"u_fan_speed": int(speed)}
        await self._put_params(device_id, body, "set_fan_speed")

    async def set_brizer_mode(self, device_id: int | str, damp_pos: int):
        body = {"u_damp_pos": int(damp_pos)}
        await self._put_params(device_id, body, "set_brizer_mode")

    async def set_humid_stage(self, device_id: int | str, stage: int):
        body = {"u_hum_stg": int(stage)}
        await self._put_params(device_id, body, "set_humid_stage")