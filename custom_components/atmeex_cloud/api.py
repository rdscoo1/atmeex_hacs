from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from aiohttp import ClientSession, ClientError, ClientResponse

API_BASE = "https://api.iot.atmeex.com"

# Максимальное число повторов для сетевых операций
_MAX_RETRIES = 3
# Базовая задержка между повторами (экспоненциальный рост: 1, 2, 4…)
_RETRY_BASE_DELAY = 1.0  # секунды

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Обёртка для всех ошибок работы с облачным API Atmeex."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AtmeexApi:
    """Клиент для облачного API Atmeex.

    Работает поверх aiohttp.ClientSession, предоставленной Home Assistant.
    """

    def __init__(self, session: ClientSession):
        """Сохранить сессию Home Assistant и проинициализировать состояние."""
        self._session = session
        self._token: Optional[str] = None
        self._token_type: str = "Bearer"
        self._email: Optional[str] = None
        self._password: Optional[str] = None
        self._retry_count: int = 0  # суммарное число сетевых ретраев

    async def async_init(self) -> None:
        """No-op для обратной совместимости."""
        return

    # ---------- helpers ----------

    def _headers(self) -> Dict[str, str]:
        """Сформировать заголовки запроса с учётом токена авторизации."""
        headers: Dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"{self._token_type} {self._token}"
        return headers

    async def _json(self, resp: ClientResponse) -> Any:
        """Разобрать JSON-ответ, логично сообщив об ошибке при невалидном JSON."""
        try:
            return await resp.json()
        except Exception:  # noqa: BLE001
            text = await resp.text()
            raise ApiError(f"Bad JSON from API ({resp.status}): {text[:200]}")

    async def _with_retries(
        self,
        coro_factory,
        action_name: str,
        *,
        fallback_value: Any = None,
        use_fallback: bool = False,
    ) -> Any:
        """Выполнить coro_factory() с ограниченным числом ретраев по сетевым ошибкам.

        Повторяем только при:
        - asyncio.TimeoutError
        - aiohttp.ClientError

        Если use_fallback=True и после всех попыток остаётся сетевая ошибка,
        возвращаем fallback_value вместо поднятия ApiError.
        Для HTTP-ошибок (4xx/5xx) ретраи НЕ делаем — они считаются логическими.
        """
        delay = _RETRY_BASE_DELAY
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except (asyncio.TimeoutError, ClientError) as e:
                last_exc = e
                self._retry_count += 1

                if attempt >= _MAX_RETRIES:
                    if use_fallback:
                        return fallback_value
                    # Формат оставляем прежним, чтобы не ломать тесты/логи
                    raise ApiError(f"{action_name} network error: {e}") from e

                _LOGGER.warning(
                    "Atmeex: %s failed on attempt %d/%d: %s",
                    action_name,
                    attempt,
                    _MAX_RETRIES,
                    e,
                )
                await asyncio.sleep(delay)
                delay *= 2

        if use_fallback:
            return fallback_value
        if last_exc:
            raise ApiError(f"{action_name} network error: {last_exc}") from last_exc
        return fallback_value

    async def _sign_in(self) -> None:
        """Выполнить логин по уже сохранённым email/паролю и сохранить токен доступа."""
        if not self._email or not self._password:
            raise ApiError("login: credentials not set")

        async def _do_login():
            async with self._session.post(
                f"{API_BASE}/auth/signin",
                json={
                    "grant_type": "basic",
                    "email": self._email,
                    "password": self._password,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    # сообщение об ошибке должно содержать 'Auth failed {status}'
                    raise ApiError(
                        f"Auth failed {resp.status}: {text[:200]}",
                        status=resp.status,
                    )
                data = await self._json(resp)
                token = data.get("access_token") or data.get("token")
                token_type = data.get("token_type") or "Bearer"
                if not token:
                    # строка, на которую уже есть тесты/логи
                    raise ApiError("login: token missing in response")
                self._token = token
                self._token_type = token_type

        # Формат network-ошибки сохраняем: 'login network error: {e}'
        await self._with_retries(_do_login, "login")

    async def _authorized_request(
        self,
        method: str,
        path: str,
        *,
        timeout: int = 20,
        json: Any | None = None,
        headers: Dict[str, str] | None = None,
    ) -> ClientResponse:
        """Выполнить запрос с токеном, с одним авто-перелогином по 401/403."""
        if self._token is None:
            # Если уже знаем креды — логинимся прозрачно
            if self._email and self._password:
                await self._sign_in()
            else:
                raise ApiError("login: credentials not set")

        url = f"{API_BASE}{path}"

        async def _do_request(retry_auth: bool = True) -> ClientResponse:
            req_headers = self._headers()
            if headers:
                req_headers.update(headers)
            async with self._session.request(
                method,
                url,
                json=json,
                headers=req_headers,
                timeout=timeout,
            ) as resp:
                if resp.status in (401, 403) and retry_auth:
                    # токен протух → пробуем перелогиниться один раз
                    self._token = None
                    if self._email and self._password:
                        await self._sign_in()
                        return await _do_request(retry_auth=False)
                return resp

        return await _do_request()

    # ---------- публичные методы ----------

    async def login(self, email: str, password: str) -> None:
        """Выполнить логин по email/паролю и сохранить токен доступа."""
        self._email = email
        self._password = password
        await self._sign_in()

    async def get_devices(self, fallback: bool = False) -> list[dict[str, Any]]:
        """Получить список устройств.

        Параметр fallback:
        - False (по умолчанию): любые ошибки (HTTP/JSON/сеть) → ApiError.
        - True: HTTP-ошибки и неожиданный формат → пустой список,
          сетевые ошибки после всех ретраев тоже приводят к пустому списку,
          а не к исключению.
        """

        async def _do_get():
            resp = await self._authorized_request("GET", "/devices", timeout=20)
            if resp.status >= 400:
                text = await resp.text()
                msg = f"get_devices {resp.status}: {text[:200]}"
                if fallback:
                    # В fallback-режиме HTTP-ошибки превращаем в пустой список
                    return []
                raise ApiError(msg, status=resp.status)
            data = await self._json(resp)
            # Бэкенд может вернуть как список, так и обёртку {"items": [...]}
            if isinstance(data, dict) and "items" in data:
                items = data["items"]
                return items if isinstance(items, list) else []
            if isinstance(data, list):
                return data
            msg = "get_devices: unexpected response shape"
            if fallback:
                return []
            raise ApiError(msg)

        result = await self._with_retries(
            _do_get,
            "get_devices",
            use_fallback=fallback,
            fallback_value=[],
        )
        return result  # type: ignore[no-any-return]

    async def get_device(self, device_id: int | str) -> dict[str, Any]:
        """Получить полное описание одного устройства по его ID."""

        async def _do_get():
            resp = await self._authorized_request(
                "GET",
                f"/devices/{device_id}",
                timeout=20,
            )
            if resp.status != 200:
                txt = await resp.text()
                raise ApiError(
                    f"GET /devices/{device_id} {resp.status}: {txt[:300]}",
                    status=resp.status,
                )
            return await self._json(resp)

        # Формат текста network-ошибки сохраняем: 'get_device network error for {id}: ...'
        return await self._with_retries(
            _do_get,
            f"get_device network error for {device_id}",
        )

    async def _put_params(
        self,
        device_id: int | str,
        body: Dict[str, Any],
        action_name: str,
        timeout: int = 20,
    ) -> None:
        """Унифицированный помощник для всех PUT-запросов изменения параметров."""

        async def _do_request():
            resp = await self._authorized_request(
                "PUT",
                f"/devices/{device_id}/params",
                json=body,
                timeout=timeout,
            )
            if resp.status >= 400:
                text = await resp.text()
                # Сообщение в формате: '<action_name> <status>: <обрезанный текст>'
                raise ApiError(
                    f"{action_name} {resp.status}: {text[:200]}",
                    status=resp.status,
                )

        await self._with_retries(_do_request, action_name)

    async def set_power(self, device_id: int | str, on: bool) -> None:
        """Установить состояние питания (вкл/выкл) через поле u_pwr_on."""
        body = {"u_pwr_on": bool(on)}
        await self._put_params(device_id, body, "set_power")

    async def set_target_temperature(self, device_id: int | str, temp_c: float) -> None:
        """Установить целевую температуру в °C (в API отправляется в деци-°C)."""
        body = {"u_temp_room": int(round(temp_c * 10))}
        await self._put_params(device_id, body, "set_target_temperature")

    async def set_fan_speed(self, device_id: int | str, speed: int) -> None:
        """Установить дискретную скорость вентилятора 0..7."""
        body = {"u_fan_speed": int(speed)}
        await self._put_params(device_id, body, "set_fan_speed")

    async def set_brizer_mode(self, device_id: int | str, damp_pos: int) -> None:
        """Установить режим бризера (положение заслонки) 0..3."""
        body = {"u_damp_pos": int(damp_pos)}
        await self._put_params(device_id, body, "set_brizer_mode")

    async def set_humid_stage(self, device_id: int | str, stage: int) -> None:
        """Установить ступень работы увлажнителя 0..3."""
        body = {"u_hum_stg": int(stage)}
        await self._put_params(device_id, body, "set_humid_stage")
