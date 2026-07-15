from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import re
import time
from typing import Any
from urllib.parse import quote

from aiohttp import ClientSession, ClientError, ClientResponse
from yarl import URL

from .helpers import (
    _normalize_device_state,
    c_to_deci,
    fan_speed_to_api,
    normalize_device_id,
    parse_atmeex_bool,
)
from .const import (
    API_AUTH_TIMEOUT_SEC,
    API_BASE_URL,
    API_REQUEST_TIMEOUT_SEC,
    RETRY_BASE_DELAY_SEC,
    RETRY_MAX_ATTEMPTS,
    RETRY_MAX_DELAY_SEC,
    TOKEN_REFRESH_BUFFER_SEC,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)


_OPERATION_CHARS = re.compile(r"[^a-zA-Z0-9_.:/()-]+")


def _sanitize_operation(operation: str) -> str:
    """Return a bounded log-safe operation label."""
    sanitized = _OPERATION_CHARS.sub("_", str(operation)).strip("_")
    return sanitized[:80] or "atmeex_request"


class AtmeexApiError(Exception):
    """Base class for sanitized Atmeex API failures."""

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        status: int | None = None,
    ) -> None:
        self.operation = _sanitize_operation(operation)
        self.status = status
        suffix = f" (status={status})" if status is not None else ""
        super().__init__(f"{self.operation}: {message}{suffix}")


class AtmeexAuthenticationError(AtmeexApiError):
    """Credentials are absent, invalid, or exhausted."""


class AtmeexConnectionError(AtmeexApiError):
    """The request could not obtain an authoritative cloud response."""


class AtmeexProtocolError(AtmeexApiError):
    """The cloud response violates the documented payload contract."""


class AtmeexRateLimitError(AtmeexApiError):
    """The cloud asked the caller to reduce request rate."""

    def __init__(
        self,
        operation: str,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        safe_operation = _sanitize_operation(operation)
        suffix_parts: list[str] = []
        if status is not None:
            suffix_parts.append(f"status={status}")
        if retry_after is not None:
            suffix_parts.append(f"retry_after={retry_after:g}s")
        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
        self.operation = safe_operation
        self.status = status
        Exception.__init__(self, f"{safe_operation}: {message}{suffix}")


ApiError = AtmeexApiError


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _error_for_status(
    operation: str,
    status: int,
    retry_after: float | None,
) -> AtmeexApiError:
    if status in (401, 403):
        return AtmeexAuthenticationError(
            operation, "authentication rejected", status=status
        )
    if status == 429:
        return AtmeexRateLimitError(
            operation,
            "rate limited",
            status=status,
            retry_after=retry_after,
        )
    if status >= 500:
        return AtmeexConnectionError(
            operation, "cloud service unavailable", status=status
        )
    return AtmeexProtocolError(operation, "request rejected", status=status)


def _device_url(device_id: int | str, suffix: str = "") -> URL:
    """Build a URL whose device identifier remains one opaque path segment."""
    canonical_id = normalize_device_id(device_id)
    encoded_id = quote(canonical_id, safe="").replace(".", "%2E")
    return URL(
        f"{API_BASE_URL}/devices/{encoded_id}{suffix}",
        encoded=True,
    )

@dataclass(slots=True)
class AtmeexDevice:
    """Validated device metadata with a legacy-compatible outward ID."""

    id: int | str
    name: str
    model: str
    online: bool
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "AtmeexDevice":
        if not isinstance(raw, dict):
            raise AtmeexProtocolError("parse_device", "device item is not an object")
        try:
            device_key = normalize_device_id(raw.get("id"))
            try:
                device_id: int | str = int(device_key, 10)
            except ValueError:
                device_id = device_key
            condition_raw = raw.get("condition")
            settings_raw = raw.get("settings")
            condition = {} if condition_raw is None else condition_raw
            settings = {} if settings_raw is None else settings_raw
            if not isinstance(condition, dict) or not isinstance(settings, dict):
                raise ValueError("condition/settings must be objects")
            online_raw = raw.get("online")
            online = (
                parse_atmeex_bool(online_raw)
                if online_raw is not None
                else bool(condition.get("time"))
            )
        except ValueError as err:
            raise AtmeexProtocolError("parse_device", "invalid device fields") from err

        normalized_raw = dict(raw)
        normalized_raw["id"] = device_id
        normalized_raw["condition"] = dict(condition)
        normalized_raw["settings"] = dict(settings)
        return cls(
            id=device_id,
            name=str(raw.get("name") or f"Device {device_id}"),
            model=str(raw.get("model") or "unknown"),
            online=online,
            raw=normalized_raw,
        )

    @property
    def condition(self) -> dict[str, Any]:
        return dict(self.raw["condition"])

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self.raw["settings"])

    def to_ha_dict(self) -> dict[str, Any]:
        data = dict(self.raw)
        data["id"] = self.id
        data["name"] = self.name
        data["model"] = self.model
        data["online"] = self.online
        data["condition"] = self.condition
        data["settings"] = self.settings
        return data


@dataclass(slots=True)
class AtmeexState:
    """Normalized state retaining the legacy outward device ID."""

    id: int | str
    raw: dict[str, Any] = field(repr=False)

    @classmethod
    def from_device_dict(cls, device: dict[str, Any]) -> "AtmeexState":
        try:
            normalized = _normalize_device_state(device)
            device_key = normalize_device_id(device.get("id"))
            try:
                device_id: int | str = int(device_key, 10)
            except ValueError:
                device_id = device_key
        except ValueError as err:
            raise AtmeexProtocolError(
                "normalize_device_state", "invalid device state"
            ) from err
        return cls(id=device_id, raw=normalized)

    def to_ha_dict(self) -> dict[str, Any]:
        """State dict stored in coordinator.data['states'][id]."""
        return dict(self.raw)

class AtmeexApi:
    """Клиент для облачного API Atmeex.

    Работает поверх aiohttp.ClientSession, предоставленной Home Assistant.
    """

    def __init__(self, session: ClientSession):
        """Сохранить сессию Home Assistant и проинициализировать состояние."""
        self._session = session
        self._token: str | None = None
        self._token_type: str = "Bearer"
        self._refresh_token: str | None = None
        # Two credential kinds are supported:
        #   email/password — `grant_type=basic` (long-lived: re-login on token expiry)
        #   phone/phone_code — `grant_type=phone_code` (short-lived: SMS code can't be
        #     reused, so once tokens expire and refresh fails, we surface ConfigEntryAuthFailed
        #     and let the user re-request SMS via reauth)
        self._email: str | None = None
        self._password: str | None = None
        self._phone: str | None = None
        self._phone_code: str | None = None
        self._retry_count: int = 0  # суммарное число сетевых ретраев
        self._token_expires_at: float | None = None  # unix-time
        self._lock = asyncio.Lock()

    async def async_init(self) -> None:
        """No-op для обратной совместимости."""
        return

    # ---------- helpers ----------

    @property
    def token(self) -> str:
        """Return the current auth token (empty string if not set)."""
        return self._token or ""

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token (None if not set)."""
        return self._refresh_token

    def _token_is_valid(self) -> bool:
        """Проверить, что токен ещё жив и не протухнет прямо сейчас."""
        if not self._token:
            return False
        if self._token_expires_at is None:
            # сервер не прислал срок жизни — считаем токен валидным,
            # пока не получим ошибку авторизации
            return True
        # обновляем токен чуть заранее — за 60 секунд до истечения
        return time.time() < self._token_expires_at - TOKEN_REFRESH_BUFFER_SEC

    def _has_replayable_credentials(self) -> bool:
        """True if we can re-acquire a token without user interaction.

        Email/password can be replayed indefinitely; phone_code is single-use,
        so phone accounts must rely on refresh_token alone.
        """
        return bool(self._email and self._password)

    async def _ensure_token(self) -> None:
        """Гарантировать, что у нас есть валидный токен.

        Tries refresh token first (cheaper), falls back to full login.
        Использует блокировку, чтобы не логиниться параллельно из разных корутин.
        """
        if self._token_is_valid():
            return

        async with self._lock:
            # второй раз проверяем внутри lock — вдруг кто-то уже залогинился
            if self._token_is_valid():
                return

            # Try refresh token first — it's cheaper than full login
            if self._refresh_token:
                try:
                    await self._signin_refresh()
                    return
                except ApiError:
                    _LOGGER.debug("Refresh token failed, falling back to basic login")

            if not self._has_replayable_credentials():
                raise AtmeexAuthenticationError(
                    "ensure_token", "credentials are unavailable"
                )

            await self._sign_in()

    @staticmethod
    def _unauth_headers() -> dict[str, str]:
        """Headers for endpoints that don't carry a bearer token (auth calls)."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

    def _headers(self) -> dict[str, str]:
        """Сформировать заголовки запроса с учётом токена авторизации."""
        headers = self._unauth_headers()
        if self._token:
            headers["Authorization"] = f"{self._token_type} {self._token}"
        return headers

    async def _consume_response(
        self,
        response: ClientResponse,
        operation: str,
        *,
        expect_json: bool,
    ) -> Any:
        retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
        if response.status >= 400:
            await response.read()
            raise _error_for_status(operation, response.status, retry_after)
        if not expect_json:
            await response.read()
            return None
        try:
            return await response.json(content_type=None)
        except (ClientError, TypeError, ValueError) as err:
            await response.read()
            raise AtmeexProtocolError(
                operation,
                "response is not valid JSON",
                status=response.status,
            ) from err

    async def _call_with_get_retries(
        self,
        operation: str,
        call: Callable[[], Awaitable[Any]],
        recover_auth: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        delay = RETRY_BASE_DELAY_SEC
        auth_recovered = False
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                return await call()
            except AtmeexAuthenticationError:
                if (
                    recover_auth is None
                    or auth_recovered
                    or attempt == RETRY_MAX_ATTEMPTS
                ):
                    raise
                # Authentication recovery is not a retryable GET transport
                # failure. Its own error must propagate, while the following
                # GET consumes the next slot in the same three-attempt budget.
                await recover_auth()
                auth_recovered = True
                continue
            except AtmeexRateLimitError as err:
                retryable: AtmeexApiError = err
            except AtmeexConnectionError as err:
                retryable = err
            except (asyncio.TimeoutError, ClientError) as err:
                retryable = AtmeexConnectionError(operation, "transport failed")
                retryable.__cause__ = err
            if attempt == RETRY_MAX_ATTEMPTS:
                raise retryable
            self._retry_count += 1
            wait = (
                retryable.retry_after
                if isinstance(retryable, AtmeexRateLimitError)
                and retryable.retry_after is not None
                else delay
            )
            await asyncio.sleep(min(wait, RETRY_MAX_DELAY_SEC))
            delay = min(delay * 2, RETRY_MAX_DELAY_SEC)
        raise AtmeexConnectionError(operation, "retry budget exhausted")

    def _apply_token_response(self, data: Any, operation: str) -> None:
        """Extract and store token data from an auth response."""
        if not isinstance(data, dict):
            raise AtmeexProtocolError(
                operation,
                "token response is not an object",
            )
        token = data.get("access_token") or data.get("token")
        if not isinstance(token, str) or not token:
            raise AtmeexProtocolError(operation, "access token is missing")
        self._token = token
        self._token_type = str(data.get("token_type") or "Bearer")

        # Store refresh token if provided
        rt = data.get("refresh_token")
        if isinstance(rt, str) and rt:
            self._refresh_token = rt

        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            self._token_expires_at = time.time() + int(expires_in)
        else:
            self._token_expires_at = None

    async def _sign_in(self) -> None:
        if not self._email or not self._password:
            raise AtmeexAuthenticationError(
                "login", "email credentials are unavailable"
            )
        data = await self._auth_post(
            "login",
            {
                "grant_type": "basic",
                "email": self._email,
                "password": self._password,
            },
            expect_json=True,
        )
        self._apply_token_response(data, "login")

    async def _sign_in_phone(self) -> None:
        if not self._phone or not self._phone_code:
            raise AtmeexAuthenticationError(
                "login_phone", "phone credentials are unavailable"
            )
        data = await self._auth_post(
            "login_phone",
            {
                "grant_type": "phone_code",
                "phone": self._phone,
                "phone_code": self._phone_code,
            },
            expect_json=True,
        )
        self._apply_token_response(data, "login_phone")

    async def _signin_refresh(self) -> None:
        if not self._refresh_token:
            raise AtmeexAuthenticationError(
                "refresh_token", "refresh token is unavailable"
            )
        data = await self._auth_post(
            "refresh_token",
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            expect_json=True,
        )
        self._apply_token_response(data, "refresh_token")
    
    async def _request_once(
        self,
        method: str,
        path: str | URL,
        *,
        operation: str,
        json_body: Any | None = None,
        expect_json: bool,
    ) -> Any:
        request_url = path if isinstance(path, URL) else f"{API_BASE_URL}{path}"
        try:
            async with self._session.request(
                method,
                request_url,
                json=json_body,
                headers=self._headers(),
                timeout=API_REQUEST_TIMEOUT_SEC,
            ) as response:
                return await self._consume_response(
                    response,
                    operation,
                    expect_json=expect_json,
                )
        except AtmeexApiError:
            raise
        except (asyncio.TimeoutError, ClientError) as err:
            raise AtmeexConnectionError(operation, "transport failed") from err

    async def _request(
        self,
        method: str,
        path: str | URL,
        *,
        operation: str,
        json_body: Any | None = None,
        expect_json: bool = True,
    ) -> Any:
        await self._ensure_token()
        attempted_token: str | None = None

        async def call() -> Any:
            nonlocal attempted_token
            attempted_token = self._token
            return await self._request_once(
                method,
                path,
                operation=operation,
                json_body=json_body,
                expect_json=expect_json,
            )

        async def recover_auth() -> None:
            async with self._lock:
                if self._token == attempted_token:
                    self._token_expires_at = None
                    await self._sign_in()

        if method == "GET":
            return await self._call_with_get_retries(
                operation,
                call,
                recover_auth=(
                    recover_auth
                    if self._has_replayable_credentials()
                    else None
                ),
            )

        try:
            return await call()
        except AtmeexAuthenticationError:
            if not self._has_replayable_credentials():
                raise
            await recover_auth()
            return await call()

    async def _auth_post(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        expect_json: bool,
    ) -> Any:
        try:
            async with self._session.post(
                f"{API_BASE_URL}/auth/{'signup' if operation == 'request_sms_code' else 'signin'}",
                json=payload,
                headers=self._unauth_headers(),
                timeout=API_AUTH_TIMEOUT_SEC,
            ) as response:
                return await self._consume_response(
                    response,
                    operation,
                    expect_json=expect_json,
                )
        except AtmeexApiError:
            raise
        except (asyncio.TimeoutError, ClientError) as err:
            raise AtmeexConnectionError(operation, "transport failed") from err


    # ---------- публичные методы ----------

    async def login(self, email: str, password: str) -> None:
        """Выполнить логин по email/паролю и сохранить токен доступа."""
        self._email = email
        self._password = password
        # Clear any stale phone credentials so _ensure_token doesn't get confused
        # if a single AtmeexApi instance is repurposed across login methods.
        self._phone = None
        self._phone_code = None
        await self._sign_in()

    async def request_sms_code(self, phone: str) -> None:
        await self._auth_post(
            "request_sms_code",
            {"grant_type": "phone_code", "phone": phone},
            expect_json=False,
        )

    async def login_phone(self, phone: str, phone_code: str) -> None:
        """Exchange a phone + SMS code for tokens (single-use code)."""
        self._phone = phone
        self._phone_code = phone_code
        # Clear any stale email/password so a previous email login on this
        # instance can't accidentally resurrect itself as a fallback.
        self._email = None
        self._password = None
        await self._sign_in_phone()

    async def authenticate_phone(self) -> None:
        """Boot-time auth for phone accounts: refresh the access token only.

        Phone accounts cannot replay a sign-in (SMS codes are single-use), so
        the refresh_token persisted on the config entry is the only way to
        re-acquire an access token across restarts. If the refresh fails or
        no token is stored, the caller must surface a reauth prompt.
        """
        if not self._refresh_token:
            raise AtmeexAuthenticationError(
                "authenticate_phone",
                "refresh token is unavailable",
                status=401,
            )
        await self._signin_refresh()

    async def get_devices(self, fallback: bool = False) -> list[AtmeexDevice]:
        try:
            payload = await self._request(
                "GET",
                "/devices",
                operation="get_devices",
                expect_json=True,
            )
        except AtmeexAuthenticationError:
            raise
        except (AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError):
            if fallback:
                return []
            raise
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        elif fallback:
            return []
        else:
            raise AtmeexProtocolError(
                "get_devices", "unexpected collection shape"
            )
        devices: list[AtmeexDevice] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                devices.append(AtmeexDevice.from_raw(item))
            except AtmeexProtocolError:
                continue
        return devices

    async def get_device(self, device_id: int | str) -> AtmeexDevice:
        payload = await self._request(
            "GET",
            _device_url(device_id),
            operation="get_device",
            expect_json=True,
        )
        if not isinstance(payload, dict):
            raise AtmeexProtocolError(
                "get_device", "device response is not an object"
            )
        return AtmeexDevice.from_raw(payload)

    async def _put_params(
        self,
        device_id: int | str,
        body: dict[str, Any],
        action_name: str,
        timeout: int = API_REQUEST_TIMEOUT_SEC,
    ) -> None:
        del timeout
        await self._request(
            "PUT",
            _device_url(device_id, "/params"),
            operation=action_name,
            json_body=body,
            expect_json=False,
        )

    async def set_power(self, device_id: int | str, on: bool) -> None:
        """Установить состояние питания (вкл/выкл) через поле u_pwr_on."""
        body = {"u_pwr_on": bool(on)}
        await self._put_params(device_id, body, "set_power")

    async def set_target_temperature(self, device_id: int | str, temp_c: float) -> None:
        """Установить целевую температуру в °C (в API отправляется в деци-°C)."""
        value = c_to_deci(temp_c)
        if value is None:
            raise AtmeexProtocolError(
                "set_target_temperature", "invalid temperature"
            )

        body = {"u_temp_room": value}
        await self._put_params(device_id, body, "set_target_temperature")

    async def set_fan_speed(self, device_id: int | str, speed: int) -> None:
        """Установить дискретную скорость вентилятора 1..7 (конвертируется в API 0..6).
        
        HA uses speed 1-7, but API expects 0-6.
        Speed 0 = off, Speed 1-7 → API 0-6
        """
        speed_int = int(speed)
        api_speed = fan_speed_to_api(speed_int)
        
        _LOGGER.debug(
            "API set_fan_speed: device=%s HA_speed=%s → API_speed=%s",
            device_id, speed_int, api_speed
        )
        
        body = {"u_fan_speed": api_speed}
        await self._put_params(device_id, body, "set_fan_speed")

    async def set_breezer_mode(self, device_id: int | str, mode_index: int) -> None:
        """Set work mode 0..3.

        Modes 0/1/2 are physical damper positions and force u_pwr_on=True.
        Mode 3 (supply_valve) is a virtual mode: u_pwr_on=False + u_damp_pos=0.
        Matches the official mobile app's behavior.
        """
        mode = int(mode_index)
        if mode == 3:
            body: dict[str, Any] = {"u_pwr_on": False, "u_damp_pos": 0}
        elif mode in (0, 1, 2):
            body = {"u_pwr_on": True, "u_damp_pos": mode}
        else:
            raise AtmeexProtocolError("set_breezer_mode", "invalid mode")
        await self._put_params(device_id, body, "set_breezer_mode")

    async def set_heater_off(self, device_id: int | str) -> None:
        """Disable the heater by sending the device's off-sentinel target temp."""
        await self._put_params(device_id, {"u_temp_room": -1000}, "set_heater_off")

    async def set_power_and_heat(
        self, device_id: int | str, pwr_on: bool, temp_c: float | None
    ) -> None:
        """Atomic multi-field PUT for HEAT entry/exit transitions.

        temp_c=None means heater off (sends -1000).
        """
        body: dict[str, Any] = {"u_pwr_on": bool(pwr_on)}
        if temp_c is None:
            body["u_temp_room"] = -1000
        else:
            value = c_to_deci(temp_c)
            if value is None:
                raise AtmeexProtocolError(
                    "set_power_and_heat", "invalid temperature"
                )
            body["u_temp_room"] = value
        await self._put_params(device_id, body, "set_power_and_heat")

    async def set_humid_stage(self, device_id: int | str, stage: int) -> None:
        """Установить ступень работы увлажнителя 0..3."""
        body = {"u_hum_stg": int(stage)}
        await self._put_params(device_id, body, "set_humid_stage")

    async def set_auto_mode(self, device_id: int | str, enabled: bool) -> None:
        """Включить/выключить режим AutoNanny."""
        body = {"u_auto": bool(enabled)}
        await self._put_params(device_id, body, "set_auto_mode")

    async def set_sleep_mode(self, device_id: int | str, enabled: bool) -> None:
        """Включить/выключить ночной режим (Sleep Mode)."""
        body = {"u_night": bool(enabled)}
        await self._put_params(device_id, body, "set_sleep_mode")
