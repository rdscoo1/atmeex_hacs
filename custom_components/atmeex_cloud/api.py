from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional
from dataclasses import dataclass, field

from aiohttp import ClientSession, ClientError, ClientResponse

from .helpers import c_to_deci
from .const import API_BASE_URL, RETRY_MAX_DELAY_SEC, RETRY_MAX_ATTEMPTS, RETRY_BASE_DELAY_SEC

_LOGGER = logging.getLogger(__name__)


class ApiError(Exception):
    """Обёртка для всех ошибок работы с облачным API Atmeex."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

@dataclass(slots=True)
class AtmeexDevice:
    """Типизированный wrapper вокруг сырого JSON устройства."""
    id: int
    name: str
    model: str
    online: bool
    raw: Dict[str, Any]

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "AtmeexDevice":
        """Собрать устройство из сырого ответа API, с дефолтами."""
        did = int(raw["id"])
        name = str(raw.get("name") or f"Device {did}")
        model = str(raw.get("model") or "unknown")
        # Don't default to True - if API doesn't provide online status, check condition
        online_raw = raw.get("online")
        if online_raw is not None:
            online = bool(online_raw)
        else:
            # Check if device has fresh condition data
            cond = raw.get("condition") or {}
            online = bool(cond and cond.get("time"))
        
        return cls(
            id=did,
            name=name,
            model=model,
            online=online,
            raw=raw,
        )

    @property
    def condition(self) -> Dict[str, Any]:
        return dict(self.raw.get("condition") or {})

    @property
    def settings(self) -> Dict[str, Any]:
        return dict(self.raw.get("settings") or {})

    def to_ha_dict(self) -> Dict[str, Any]:
        """Форма, в которой coordinator будет хранить устройство."""
        # Важно не потерять лишние поля из raw — поэтому делаем копию
        data = dict(self.raw)
        data.setdefault("id", self.id)
        data.setdefault("name", self.name)
        data.setdefault("model", self.model)
        data.setdefault("online", self.online)
        # гарантируем наличие condition/settings как словарей
        data["condition"] = self.condition
        data["settings"] = self.settings
        return data
    
@dataclass(slots=True)
class AtmeexState:
    """Нормализованное состояние устройства (condition + settings)."""
    id: int
    online: bool
    pwr_on: bool
    fan_speed: int | None
    damp_pos: int | None
    hum_stg: int | None
    u_temp_room: int | None  # деци-°C
    hum_room: int | None
    temp_room: int | None
    raw: Dict[str, Any] = field(repr=False)

    @classmethod
    def from_device_dict(cls, device: Dict[str, Any]) -> "AtmeexState":
        """Использует уже существующую _normalize_device_state."""
        from . import _normalize_device_state  # импорт внутри, чтобы не ловить циклы

        normalized = _normalize_device_state(device)
        did = int(device["id"])
        return cls(
            id=did,
            online=bool(normalized.get("online", True)),
            pwr_on=bool(normalized.get("pwr_on", False)),
            fan_speed=normalized.get("fan_speed"),
            damp_pos=normalized.get("damp_pos"),
            hum_stg=normalized.get("hum_stg"),
            u_temp_room=normalized.get("u_temp_room"),
            hum_room=normalized.get("hum_room"),
            temp_room=normalized.get("temp_room"),
            raw=normalized,
        )

    def to_ha_dict(self) -> Dict[str, Any]:
        """Форма, которая будет лежать в coordinator.data['states'][id]."""
        return dict(self.raw)

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
        self._token_expires_at: float | None = None  # unix-time
        self._lock = asyncio.Lock()

    async def async_init(self) -> None:
        """No-op для обратной совместимости."""
        return

    # ---------- helpers ----------

    def _token_is_valid(self) -> bool:
        """Проверить, что токен ещё жив и не протухнет прямо сейчас."""
        if not self._token:
            return False
        if self._token_expires_at is None:
            # сервер не прислал срок жизни — считаем токен валидным,
            # пока не получим ошибку авторизации
            return True
        # обновляем токен чуть заранее — за 60 секунд до истечения
        return time.time() < self._token_expires_at - 60

    async def _ensure_token(self) -> None:
        """Гарантировать, что у нас есть валидный токен.

        Использует блокировку, чтобы не логиниться параллельно из разных корутин.
        """
        if self._token_is_valid():
            return

        async with self._lock:
            # второй раз проверяем внутри lock — вдруг кто-то уже залогинился
            if self._token_is_valid():
                return

            if not self._email or not self._password:
                raise ApiError("login: credentials not set")

            await self._sign_in()

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
        delay = RETRY_BASE_DELAY_SEC
        last_exc: Exception | None = None

        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                return await coro_factory()
            except (asyncio.TimeoutError, ClientError) as e:
                last_exc = e
                self._retry_count += 1

                if attempt >= RETRY_MAX_ATTEMPTS:
                    if use_fallback:
                        return fallback_value
                    # Формат оставляем прежним, чтобы не ломать тесты/логи
                    raise ApiError(f"{action_name} network error: {e}") from e

                _LOGGER.warning(
                    "Atmeex: %s failed on attempt %d/%d: %s",
                    action_name,
                    attempt,
                    RETRY_MAX_ATTEMPTS,
                    e,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, RETRY_MAX_DELAY_SEC)

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
                f"{API_BASE_URL}/auth/signin",
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

                expires_in = data.get("expires_in")
                if isinstance(expires_in, (int, float)):
                    self._token_expires_at = time.time() + int(expires_in)
                else:
                    self._token_expires_at = None

        # Формат network-ошибки сохраняем: 'login network error: {e}'
        await self._with_retries(_do_login, "login")
    
    async def _request(
    self,
    method: str,
    path: str,
    *,
    timeout: int = 20,
    json: Any | None = None,
    headers: Dict[str, str] | None = None,
) -> tuple[int, Any]:
        """Запрос с токеном + 1 auto-relogin по 401/403/500.
        Возвращает (status, payload), где payload = json (если <400) иначе text.
        """
        await self._ensure_token()
        url = f"{API_BASE_URL}{path}"

        async def _do(retry_auth: bool = True) -> tuple[int, Any]:
            req_headers = self._headers()
            if headers:
                req_headers.update(headers)

            async with self._session.request(
                method, url, json=json, headers=req_headers, timeout=timeout
            ) as resp:
                if resp.status in (401, 403, 500) and retry_auth and self._email and self._password:
                    self._token_expires_at = None
                    await self._sign_in()
                    return await _do(retry_auth=False)

                if resp.status < 400:
                    return resp.status, await self._json(resp)
                return resp.status, await resp.text()

        return await _do()


    # ---------- публичные методы ----------

    async def login(self, email: str, password: str) -> None:
        """Выполнить логин по email/паролю и сохранить токен доступа."""
        self._email = email
        self._password = password
        await self._sign_in()

    async def get_devices(self, fallback: bool = False) -> list[AtmeexDevice]:
        """Получить список устройств.

        Параметр fallback:
        - False (по умолчанию): любые ошибки (HTTP/JSON/сеть) → ApiError.
        - True: HTTP-ошибки и неожиданный формат → пустой список,
          сетевые ошибки после всех ретраев тоже приводят к пустому списку,
          а не к исключению.
        """

        async def _do_get():
            status, data = await self._request("GET", "/devices", timeout=20)

            if status >= 400:
                if fallback:
                    return []
                raise ApiError(f"get_devices {status}: {str(data)[:200]}", status=status)
            
            if isinstance(data, dict) and "items" in data:
                raw_list = data["items"] if isinstance(data["items"], list) else []
            elif isinstance(data, list):
                raw_list = data
            else:
                if fallback:
                    return []
                raise ApiError("get_devices: unexpected response shape")

            devices: list[AtmeexDevice] = []
            for raw in raw_list:
                try:
                    devices.append(AtmeexDevice.from_raw(raw))
                except Exception:
                    _LOGGER.debug("Failed to parse device %s", raw)
            return devices

        return await self._with_retries(_do_get, "get_devices", use_fallback=fallback, fallback_value=[])

    async def get_device(self, device_id: int | str) -> AtmeexDevice:
        """Получить полное описание одного устройства как AtmeexDevice."""
        async def _do_get():
            status, data = await self._request("GET", f"/devices/{device_id}", timeout=20)

            if status != 200:
                raise ApiError(
                    f"GET /devices/{device_id} {status}: {str(data)[:300]}",
                    status=status,
                )

            if not isinstance(data, dict):
                raise ApiError(f"get_device: unexpected payload for {device_id}")

            return AtmeexDevice.from_raw(data)

        return await self._with_retries(_do_get, f"get_device({device_id})")

    async def _put_params(
        self,
        device_id: int | str,
        body: Dict[str, Any],
        action_name: str,
        timeout: int = 20,
    ) -> None:
        """Унифицированный помощник для всех PUT-запросов изменения параметров."""

        async def _do_request():
            status, data = await self._request("PUT", f"/devices/{device_id}/params", json=body, timeout=timeout)
            if status >= 400:
                raise ApiError(f"{action_name} {status}: {str(data)[:200]}", status=status)

        await self._with_retries(_do_request, action_name)

    async def set_power(self, device_id: int | str, on: bool) -> None:
        """Установить состояние питания (вкл/выкл) через поле u_pwr_on."""
        body = {"u_pwr_on": bool(on)}
        await self._put_params(device_id, body, "set_power")

    async def set_target_temperature(self, device_id: int | str, temp_c: float) -> None:
        """Установить целевую температуру в °C (в API отправляется в деци-°C)."""
        value = c_to_deci(temp_c)
        if value is None:
            raise ApiError(f"set_target_temperature: invalid temperature {temp_c!r}")

        body = {"u_temp_room": value}
        await self._put_params(device_id, body, "set_target_temperature")

    async def set_fan_speed(self, device_id: int | str, speed: int) -> None:
        """Установить дискретную скорость вентилятора 1..7 (конвертируется в API 0..6).
        
        HA uses speed 1-7, but API expects 0-6.
        Speed 0 = off, Speed 1-7 → API 0-6
        """
        from .helpers import fan_speed_to_api
        
        speed_int = int(speed)
        api_speed = fan_speed_to_api(speed_int)
        
        _LOGGER.debug(
            "API set_fan_speed: device=%s HA_speed=%s → API_speed=%s",
            device_id, speed_int, api_speed
        )
        
        body = {"u_fan_speed": api_speed}
        await self._put_params(device_id, body, "set_fan_speed")

    async def set_brizer_mode(self, device_id: int | str, damp_pos: int) -> None:
        """Установить режим бризера (положение заслонки) 0..3."""
        body = {"u_damp_pos": int(damp_pos)}
        await self._put_params(device_id, body, "set_brizer_mode")

    async def set_humid_stage(self, device_id: int | str, stage: int) -> None:
        """Установить ступень работы увлажнителя 0..3."""
        body = {"u_hum_stg": int(stage)}
        await self._put_params(device_id, body, "set_humid_stage")
