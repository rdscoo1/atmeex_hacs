from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
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


RefreshTokenChangedCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class _TokenSnapshot:
    token: str
    token_type: str
    generation: int
    recovery_attempt: int


@dataclass(frozen=True, slots=True)
class _RecoveryFailure:
    generation: int
    recovery_attempt: int
    error_type: type[AtmeexApiError]
    operation: str
    status: int | None
    retry_after: float | None

    @classmethod
    def from_error(
        cls,
        rejected: _TokenSnapshot,
        error: AtmeexApiError,
    ) -> _RecoveryFailure:
        return cls(
            generation=rejected.generation,
            recovery_attempt=rejected.recovery_attempt,
            error_type=type(error),
            operation=error.operation,
            status=error.status,
            retry_after=(
                error.retry_after
                if isinstance(error, AtmeexRateLimitError)
                else None
            ),
        )

    def to_error(self) -> AtmeexApiError:
        if self.error_type is AtmeexRateLimitError:
            return AtmeexRateLimitError(
                self.operation,
                "authentication recovery failed",
                status=self.status,
                retry_after=self.retry_after,
            )
        return self.error_type(
            self.operation,
            "authentication recovery failed",
            status=self.status,
        )


class AtmeexApi:
    """Клиент для облачного API Atmeex.

    Работает поверх aiohttp.ClientSession, предоставленной Home Assistant.
    """

    def __init__(
        self,
        session: ClientSession,
        *,
        on_refresh_token_changed: RefreshTokenChangedCallback | None = None,
    ) -> None:
        self._session = session
        self._token: str | None = None
        self._token_type = "Bearer"
        self._token_generation = 0
        self._recovery_attempt = 0
        self._recovery_failure: _RecoveryFailure | None = None
        self._refresh_token: str | None = None
        self._on_refresh_token_changed = on_refresh_token_changed
        self._email: str | None = None
        self._password: str | None = None
        self._phone: str | None = None
        self._phone_code: str | None = None
        self._retry_count = 0
        self._token_expires_at: float | None = None
        self._lock = asyncio.Lock()

    async def async_init(self) -> None:
        """No-op для обратной совместимости."""
        return

    # ---------- helpers ----------

    @property
    def token(self) -> str:
        return self._token or ""

    @property
    def token_generation(self) -> int:
        return self._token_generation

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def retry_count(self) -> int:
        """Return the cumulative count of bounded HTTP retry attempts."""
        return self._retry_count

    def _token_snapshot(self) -> _TokenSnapshot:
        if not self._token:
            raise AtmeexAuthenticationError(
                "request", "access token is unavailable"
            )
        return _TokenSnapshot(
            self._token,
            self._token_type,
            self._token_generation,
            self._recovery_attempt,
        )

    def _headers_for(self, snapshot: _TokenSnapshot) -> dict[str, str]:
        headers = self._unauth_headers()
        headers["Authorization"] = f"{snapshot.token_type} {snapshot.token}"
        return headers

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
        if self._token_is_valid():
            return
        rejected = (
            self._token_snapshot()
            if self._token
            else _TokenSnapshot(
                "",
                self._token_type,
                self._token_generation,
                self._recovery_attempt,
            )
        )
        async with self._lock:
            if (
                self._token_is_valid()
                and self._token_generation > rejected.generation
            ):
                return
            await self._recover_locked(rejected, 401)

    @staticmethod
    def _unauth_headers() -> dict[str, str]:
        """Headers for endpoints that don't carry a bearer token (auth calls)."""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }

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

    def _apply_token_response(self, data: Any, operation: str) -> None:
        if not isinstance(data, dict):
            raise AtmeexProtocolError(operation, "token response is not an object")
        token = data.get("access_token") or data.get("token")
        if not isinstance(token, str) or not token:
            raise AtmeexProtocolError(operation, "access token is missing")

        token_type = str(data.get("token_type") or "Bearer")
        expires_in = data.get("expires_in")
        expires_at: float | None = None
        if isinstance(expires_in, (int, float)):
            try:
                expires_at = time.time() + int(expires_in)
            except (OverflowError, ValueError) as err:
                raise AtmeexProtocolError(
                    operation,
                    "token expiry is invalid",
                ) from err
            if not math.isfinite(expires_at):
                raise AtmeexProtocolError(
                    operation,
                    "token expiry is invalid",
                )
        rotated = data.get("refresh_token")
        refresh_changed = (
            isinstance(rotated, str)
            and bool(rotated)
            and rotated != self._refresh_token
        )
        callback = self._on_refresh_token_changed if refresh_changed else None

        self._token = token
        self._token_type = token_type
        self._token_generation += 1
        self._token_expires_at = expires_at
        if refresh_changed:
            self._refresh_token = rotated
        if callback is not None:
            try:
                callback(rotated)
            except Exception:
                _LOGGER.warning(
                    "Atmeex refresh-token persistence callback failed"
                )

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
        refresh_token = self._refresh_token
        if not refresh_token:
            raise AtmeexAuthenticationError(
                "refresh_token", "refresh token is unavailable"
            )
        try:
            data = await self._auth_post(
                "refresh_token",
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                expect_json=True,
            )
        except AtmeexAuthenticationError:
            self._refresh_token = None
            raise
        self._apply_token_response(data, "refresh_token")

    async def _recover_locked(
        self,
        rejected: _TokenSnapshot,
        status: int,
    ) -> None:
        if self._token and self._token_generation > rejected.generation:
            return
        failure = self._recovery_failure
        if (
            failure is not None
            and failure.generation == rejected.generation
            and failure.recovery_attempt >= rejected.recovery_attempt
        ):
            raise failure.to_error()
        try:
            await self._attempt_recovery_locked(status)
        except AtmeexApiError as err:
            self._recovery_attempt += 1
            self._recovery_failure = _RecoveryFailure.from_error(rejected, err)
            raise
        else:
            self._recovery_attempt += 1
            self._recovery_failure = None

    async def _attempt_recovery_locked(self, status: int) -> None:
        refresh_error: AtmeexAuthenticationError | None = None
        if self._refresh_token:
            try:
                await self._signin_refresh()
                return
            except AtmeexAuthenticationError as err:
                refresh_error = err
        if self._email and self._password:
            await self._sign_in()
            return
        if refresh_error is not None:
            raise refresh_error
        raise AtmeexAuthenticationError(
            "authenticated_request",
            "authentication recovery exhausted",
            status=status,
        )

    async def async_refresh_access_token(self) -> None:
        """Recover credentials once under the shared token lock for HTTP or WS."""
        observed = (
            self._token_snapshot()
            if self._token
            else _TokenSnapshot(
                "",
                self._token_type,
                self._token_generation,
                self._recovery_attempt,
            )
        )
        async with self._lock:
            if self._token and self._token_generation > observed.generation:
                return
            await self._recover_locked(observed, 401)
    
    async def _request_once(
        self,
        method: str,
        path: str | URL,
        *,
        operation: str,
        snapshot: _TokenSnapshot,
        json_body: Any | None,
        expect_json: bool,
    ) -> Any:
        request_url = path if isinstance(path, URL) else f"{API_BASE_URL}{path}"
        try:
            async with self._session.request(
                method,
                request_url,
                json=json_body,
                headers=self._headers_for(snapshot),
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
        max_attempts = RETRY_MAX_ATTEMPTS if method == "GET" else 2
        retry_delay = RETRY_BASE_DELAY_SEC
        recovered_auth = False

        for attempt in range(1, max_attempts + 1):
            snapshot = self._token_snapshot()
            try:
                return await self._request_once(
                    method,
                    path,
                    operation=operation,
                    snapshot=snapshot,
                    json_body=json_body,
                    expect_json=expect_json,
                )
            except AtmeexAuthenticationError as err:
                if err.status != 401:
                    raise
                if recovered_auth or attempt == max_attempts:
                    raise AtmeexAuthenticationError(
                        operation,
                        "authentication recovery exhausted",
                        status=err.status,
                    ) from err
                async with self._lock:
                    await self._recover_locked(snapshot, err.status or 401)
                recovered_auth = True
            except (AtmeexConnectionError, AtmeexRateLimitError) as err:
                if method != "GET" or attempt == max_attempts:
                    raise
                self._retry_count += 1
                wait = (
                    err.retry_after
                    if isinstance(err, AtmeexRateLimitError)
                    and err.retry_after is not None
                    else retry_delay
                )
                await asyncio.sleep(min(wait, RETRY_MAX_DELAY_SEC))
                retry_delay = min(retry_delay * 2, RETRY_MAX_DELAY_SEC)
        raise AtmeexConnectionError(operation, "retry budget exhausted")

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
