import asyncio
import logging

import pytest
from aiohttp import ClientError

from custom_components.atmeex_cloud.api import (
    API_BASE_URL,
    ApiError,
    AtmeexApi,
    AtmeexApiError,
    AtmeexAuthenticationError,
    AtmeexConnectionError,
    AtmeexDevice,
    AtmeexProtocolError,
    AtmeexRateLimitError,
    AtmeexState,
)


def test_typed_api_errors_expose_only_sanitized_context():
    error = AtmeexRateLimitError(
        "get_devices",
        "rate limited",
        status=429,
        retry_after=7.5,
    )

    assert isinstance(error, AtmeexApiError)
    assert error.operation == "get_devices"
    assert error.status == 429
    assert error.retry_after == 7.5
    assert str(error) == "get_devices: rate limited (status=429, retry_after=7.5s)"
    assert "household-secret-response" not in str(error)


def test_api_error_remains_exact_compatibility_alias():
    assert ApiError is AtmeexApiError
    assert issubclass(AtmeexAuthenticationError, AtmeexApiError)
    assert issubclass(AtmeexConnectionError, AtmeexApiError)
    assert issubclass(AtmeexProtocolError, AtmeexApiError)


@pytest.mark.asyncio
async def test_retry_log_redacts_transport_exception_and_sanitizes_operation(
    caplog, monkeypatch
):
    api = AtmeexApi(FakeSession())

    async def fail_request():
        raise ClientError("household-secret-response")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with caplog.at_level(logging.WARNING, logger="custom_components.atmeex_cloud.api"):
        with pytest.raises(AtmeexConnectionError) as raised:
            await api._with_retries(fail_request, "get devices\n")

    assert raised.value.operation == "get_devices"
    assert "household-secret-response" not in str(raised.value)
    assert "household-secret-response" not in caplog.text
    assert "get_devices failed on attempt" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AtmeexAuthenticationError),
        (403, AtmeexAuthenticationError),
        (429, AtmeexRateLimitError),
        (500, AtmeexConnectionError),
        (404, AtmeexProtocolError),
    ],
)
async def test_get_devices_maps_status_without_exposing_response_body(
    status, error_type
):
    session = FakeSession()
    session.queue_response(
        FakeResponse(status, text_data="household-secret-response")
    )
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(error_type) as raised:
        await api.get_devices()

    assert "household-secret-response" not in str(raised.value)


class FakeResponse:
    def __init__(self, status: int, json_data=None, text_data=""):
        self.status = status
        self._json = json_data
        self._text = text_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._json

    async def text(self):
        return self._text


class FakeSession:
    def __init__(self):
        self.requests = []
        self._responses = []

    def queue_response(self, resp: FakeResponse):
        self._responses.append(resp)

    def _pop_response(self):
        assert self._responses, "No queued response"
        return self._responses.pop(0)

    # Новый универсальный метод, который использует AtmeexApi через _authorized_request
    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append((method, url, json, headers))
        return self._pop_response()

    # login() внутри Api использует session.post(...)
    def post(self, url, json=None, headers=None, timeout=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    # На всякий случай оставляем get/put как обёртки
    def get(self, url, headers=None, timeout=None):
        return self.request("GET", url, json=None, headers=headers, timeout=timeout)

    def put(self, url, json=None, headers=None, timeout=None):
        return self.request("PUT", url, json=json, headers=headers, timeout=timeout)


def test_device_normalization_overwrites_raw_id_and_rejects_bad_online_literal():
    device = AtmeexDevice.from_raw(
        {"id": 42, "name": "Breezer", "online": "off", "condition": {}, "settings": {}}
    )

    assert device.id == 42
    assert device.raw["id"] == 42
    assert device.online is False
    assert device.to_ha_dict()["id"] == 42

    legacy = AtmeexDevice.from_raw({"id": "0007", "condition": {}, "settings": {}})
    assert legacy.id == 7
    assert f"{legacy.id}_fan" == "7_fan"

    with pytest.raises(AtmeexProtocolError, match="parse_device"):
        AtmeexDevice.from_raw({"id": 42, "online": "connected"})


@pytest.mark.parametrize("field", ["condition", "settings"])
@pytest.mark.parametrize("value", [[], "", 0, False, ["bad"], "bad", 1, True])
def test_device_rejects_non_object_nested_fields(field, value):
    raw = {"id": 42, "condition": {}, "settings": {}}
    raw[field] = value

    with pytest.raises(AtmeexProtocolError, match="parse_device"):
        AtmeexDevice.from_raw(raw)


@pytest.mark.parametrize(
    "device",
    [
        [],
        "bad",
        {"id": 1, "condition": [], "settings": {}},
        {"id": 1, "condition": 1, "settings": {}},
        {"id": 1, "condition": {}, "settings": ""},
        {"id": 1, "condition": {}, "settings": True},
    ],
)
def test_state_wraps_every_malformed_nested_shape(device):
    with pytest.raises(AtmeexProtocolError, match="normalize_device_state"):
        AtmeexState.from_device_dict(device)


@pytest.mark.asyncio
async def test_get_device_percent_encodes_opaque_string_id():
    session = FakeSession()
    session.queue_response(
        FakeResponse(
            200,
            json_data={"id": "zone/a b", "condition": {}, "settings": {}},
        )
    )
    api = AtmeexApi(session)
    api._token = "access"

    device = await api.get_device("zone/a b")

    assert device.id == "zone/a b"
    assert str(session.requests[0][1]).endswith("/devices/zone%2Fa%20b")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_id", "encoded_id"),
    [(".", "%2E"), ("..", "%2E%2E")],
)
async def test_get_device_preserves_opaque_dot_segment_id(device_id, encoded_id):
    session = FakeSession()
    session.queue_response(
        FakeResponse(
            200,
            json_data={"id": device_id, "condition": {}, "settings": {}},
        )
    )
    api = AtmeexApi(session)
    api._token = "access"

    device = await api.get_device(device_id)

    assert device.id == device_id
    assert str(session.requests[0][1]).endswith(f"/devices/{encoded_id}")


@pytest.mark.asyncio
async def test_login_success():
    session = FakeSession()
    # token_type не обязателен, но добавим для реалистичности
    session.queue_response(
        FakeResponse(200, json_data={"access_token": "token123", "token_type": "Bearer"})
    )

    api = AtmeexApi(session)
    await api.async_init()
    await api.login("user@example.com", "pwd")

    assert api._token == "token123"
    method, url, payload, _headers = session.requests[0]
    assert method == "POST"
    assert url == f"{API_BASE_URL}/auth/signin"
    # теперь login отправляет grant_type="basic"
    assert payload["email"] == "user@example.com"
    assert payload["password"] == "pwd"
    assert payload["grant_type"] == "basic"


@pytest.mark.asyncio
async def test_user_agent_header_set_on_signin():
    """Match the official mobile app's User-Agent so the server doesn't single us out."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={"access_token": "t"}))

    api = AtmeexApi(session)
    await api.login("user@example.com", "pwd")

    _method, _url, _payload, headers = session.requests[0]
    assert headers["User-Agent"] == "okhttp/3.14.9"


@pytest.mark.asyncio
async def test_user_agent_header_set_on_authorized_requests():
    """Authenticated GET should also carry the okhttp User-Agent."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=[]))

    api = AtmeexApi(session)
    api._token = "t"

    await api.get_devices()

    _method, _url, _payload, headers = session.requests[0]
    assert headers["User-Agent"] == "okhttp/3.14.9"


@pytest.mark.asyncio
async def test_request_sms_code_posts_signup_with_phone_code_grant():
    """SMS request must hit /auth/signup with grant_type=phone_code."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={}))

    api = AtmeexApi(session)
    await api.request_sms_code("+79991234567")

    method, url, payload, headers = session.requests[0]
    assert method == "POST"
    assert url == f"{API_BASE_URL}/auth/signup"
    assert payload == {"grant_type": "phone_code", "phone": "+79991234567"}
    assert headers["User-Agent"] == "okhttp/3.14.9"


@pytest.mark.asyncio
async def test_login_phone_posts_signin_with_phone_code():
    """Phone signin must send phone+phone_code under grant_type=phone_code and store tokens."""
    session = FakeSession()
    session.queue_response(
        FakeResponse(200, json_data={"access_token": "tok", "refresh_token": "rt"})
    )

    api = AtmeexApi(session)
    await api.login_phone("+79991234567", "1234")

    assert api._token == "tok"
    assert api.refresh_token == "rt"

    method, url, payload, _headers = session.requests[0]
    assert method == "POST"
    assert url == f"{API_BASE_URL}/auth/signin"
    assert payload == {
        "grant_type": "phone_code",
        "phone": "+79991234567",
        "phone_code": "1234",
    }


@pytest.mark.asyncio
async def test_login_phone_clears_email_credentials():
    """A successful phone login must wipe any stale email/password.

    Otherwise a previous email login on the same AtmeexApi instance could
    silently resurrect itself as the 401-relogin fallback.
    """
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={"access_token": "t"}))

    api = AtmeexApi(session)
    api._email = "stale@example.com"
    api._password = "stalepw"

    await api.login_phone("+79991234567", "1234")

    assert api._email is None
    assert api._password is None
    assert api._has_replayable_credentials() is False


@pytest.mark.asyncio
async def test_phone_account_does_not_replay_signin_on_401():
    """Phone accounts cannot replay the SMS code, so 401 must not trigger _sign_in."""
    sign_in_called = 0

    class Phone401Session:
        def __init__(self):
            self.requests = []

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.requests.append((method, url))
            return FakeResponse(401, text_data="unauthorized")

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = Phone401Session()
    api = AtmeexApi(session)
    api._token = "t"
    api._phone = "+79991234567"
    api._phone_code = "1234"  # already consumed but stored

    async def _counted_sign_in():
        nonlocal sign_in_called
        sign_in_called += 1

    api._sign_in = _counted_sign_in

    status, _ = await api._request("GET", "/devices")

    assert status == 401
    assert sign_in_called == 0
    # Only the one GET — no relogin attempt
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_request_sms_code_error_raises_apierror():
    """Server-side error on SMS request must surface as ApiError with status."""
    session = FakeSession()
    session.queue_response(FakeResponse(429, text_data="too many requests"))

    api = AtmeexApi(session)

    with pytest.raises(ApiError) as exc:
        await api.request_sms_code("+79991234567")

    assert exc.value.status == 429
    assert str(exc.value) == "request_sms_code: rate limited (status=429)"


@pytest.mark.asyncio
async def test_login_error_raises_apierror():
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="unauthorized"))

    api = AtmeexApi(session)

    with pytest.raises(ApiError) as exc:
        await api.login("user@example.com", "wrong")

    assert str(exc.value) == "login: authentication rejected (status=401)"


@pytest.mark.asyncio
async def test_get_devices_success():
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=[{"id": 1}]))

    api = AtmeexApi(session)
    # токен уже есть → _authorized_request не будет логиниться
    api._token = "t"

    result = await api.get_devices()
    assert len(result) == 1
    dev = result[0]
    assert isinstance(dev, AtmeexDevice)
    assert dev.id == 1
    assert dev.raw["id"] == 1  # если хочется проверить "сырой" dict

    method, url, _payload, headers = session.requests[0]
    assert method == "GET"
    assert url == f"{API_BASE_URL}/devices"
    assert headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_get_devices_error_no_fallback():
    session = FakeSession()
    session.queue_response(FakeResponse(500, text_data="error"))

    api = AtmeexApi(session)
    api._token = "t"  # чтобы не упереться в "credentials not set"

    with pytest.raises(ApiError) as exc:
        await api.get_devices()

    assert str(exc.value) == "get_devices: cloud service unavailable (status=500)"


@pytest.mark.asyncio
async def test_get_devices_500_does_not_trigger_relogin():
    session = FakeSession()
    session.queue_response(FakeResponse(500, text_data="server error"))

    api = AtmeexApi(session)
    api._token = "t"
    api._email = "user@example.com"
    api._password = "pwd"

    with pytest.raises(ApiError) as exc:
        await api.get_devices()

    assert str(exc.value) == "get_devices: cloud service unavailable (status=500)"
    assert len(session.requests) == 1
    assert session.requests[0][0] == "GET"


@pytest.mark.asyncio
async def test_get_devices_error_with_fallback_returns_empty_list():
    session = FakeSession()
    session.queue_response(FakeResponse(500, text_data="error"))

    api = AtmeexApi(session)
    api._token = "t"

    result = await api.get_devices(fallback=True)
    assert result == []  # HTTP-ошибка в fallback-режиме → пустой список


@pytest.mark.asyncio
async def test_get_device_success():
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={"id": 1, "name": "Device"}))

    api = AtmeexApi(session)
    api._token = "t"

    dev = await api.get_device(1)
    assert isinstance(dev, AtmeexDevice)
    assert dev.id == 1
    assert dev.raw["id"] == 1

    method, url, _payload, headers = session.requests[0]
    assert method == "GET"
    assert str(url) == f"{API_BASE_URL}/devices/1"
    assert headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_get_device_error_raises():
    session = FakeSession()
    session.queue_response(FakeResponse(404, text_data="not found"))

    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.get_device(123)

    assert str(exc.value) == "get_device: request rejected (status=404)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name, ha_value, expected_body",
    [
        ("set_power", True, {"u_pwr_on": True}),
        ("set_target_temperature", 21.5, {"u_temp_room": 215}),
        ("set_fan_speed", 3, {"u_fan_speed": 2}),  # HA speed 3 → API speed 2
        ("set_humid_stage", 1, {"u_hum_stg": 1}),
    ],
)
async def test_setters_success(method_name, ha_value, expected_body):
    """Test that setters convert HA values to API values correctly.
    
    For fan_speed: HA uses 1-7, API uses 0-6, so HA speed 3 → API speed 2.
    """
    session = FakeSession()
    session.queue_response(FakeResponse(200))

    api = AtmeexApi(session)
    api._token = "t"

    method = getattr(api, method_name)
    await method(1, ha_value)

    req = session.requests[0]
    assert req[0] == "PUT"
    assert req[1].startswith(f"{API_BASE_URL}/devices/1/params")
    assert req[2] == expected_body


@pytest.mark.asyncio
async def test_setter_error_raises():
    session = FakeSession()
    session.queue_response(FakeResponse(500))

    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.set_power(1, True)

    assert str(exc.value) == "set_power: cloud service unavailable (status=500)"


@pytest.mark.asyncio
async def test_set_commands_not_retried_on_network_timeout():
    """PUT set-commands must fail immediately on timeout, not be retried.

    Retrying a non-idempotent command after a timeout can apply the change
    multiple times or race against a user's next intent.
    """
    import aiohttp

    call_count = 0

    class TimeoutSession:
        def request(self, method, url, json=None, headers=None, timeout=None):
            nonlocal call_count
            call_count += 1
            return self

        async def __aenter__(self):
            raise asyncio.TimeoutError()

        async def __aexit__(self, *args):
            pass

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = TimeoutSession()
    api = AtmeexApi(session)
    await api.async_init()
    api._token = "t"

    with pytest.raises((ApiError, asyncio.TimeoutError)):
        await api.set_power(1, True)

    # The critical invariant: the PUT was issued exactly ONCE, not retried
    assert call_count == 1, f"set_power made {call_count} requests; expected 1 (no retry on network timeout)"


@pytest.mark.asyncio
async def test_concurrent_401_sign_in_called_once():
    """Concurrent _request() calls that each receive 401 must only trigger _sign_in once.

    Without serialization under self._lock, N concurrent callers each call
    _sign_in independently — last writer wins, stale token can escape.
    """
    sign_in_calls = 0
    new_token = "new-token"

    class SlowAuthSession:
        """Returns 401 until sign_in sets a valid token, then returns 200."""

        def __init__(self):
            self.requests = []

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.requests.append((method, url, headers))
            auth = (headers or {}).get("Authorization", "")
            if new_token in auth:
                return FakeResponse(200, json_data=[])
            return FakeResponse(401, text_data="unauthorized")

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = SlowAuthSession()
    api = AtmeexApi(session)
    await api.async_init()
    api._token = "old-token"
    api._email = "u@example.com"
    api._password = "pw"

    original_sign_in = api._sign_in

    async def _counted_sign_in():
        nonlocal sign_in_calls
        sign_in_calls += 1
        await asyncio.sleep(0)  # yield so other coroutines can proceed
        api._token = new_token

    api._sign_in = _counted_sign_in

    # Fire 5 concurrent GET /devices calls — all will see 401 with old token
    tasks = [asyncio.create_task(api._request("GET", "/devices")) for _ in range(5)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # All requests should ultimately succeed (200 with new token)
    for r in results:
        assert not isinstance(r, Exception), f"Unexpected exception: {r}"

    # The critical invariant: sign-in was called exactly once, not 5 times
    assert sign_in_calls == 1, f"_sign_in called {sign_in_calls} times, expected 1"


# ---------------------------------------------------------------------------
# set_breezer_mode — new multi-field behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected_body",
    [
        (0, {"u_pwr_on": True, "u_damp_pos": 0}),
        (1, {"u_pwr_on": True, "u_damp_pos": 1}),
        (2, {"u_pwr_on": True, "u_damp_pos": 2}),
        (3, {"u_pwr_on": False, "u_damp_pos": 0}),
    ],
)
async def test_set_breezer_mode_body(mode, expected_body):
    session = FakeSession()
    session.queue_response(FakeResponse(200))

    api = AtmeexApi(session)
    api._token = "t"
    await api.set_breezer_mode(1, mode)

    req = session.requests[0]
    assert req[0] == "PUT"
    assert req[2] == expected_body


@pytest.mark.asyncio
async def test_set_breezer_mode_invalid_raises():
    api = AtmeexApi(FakeSession())
    api._token = "t"
    with pytest.raises(ApiError, match="invalid mode"):
        await api.set_breezer_mode(1, 4)


# ---------------------------------------------------------------------------
# set_heater_off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_set_heater_off():
    session = FakeSession()
    session.queue_response(FakeResponse(200))

    api = AtmeexApi(session)
    api._token = "t"
    await api.set_heater_off(1)

    req = session.requests[0]
    assert req[0] == "PUT"
    assert req[2] == {"u_temp_room": -1000}


# ---------------------------------------------------------------------------
# set_power_and_heat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pwr_on, temp_c, expected_body",
    [
        (True, 22.5, {"u_pwr_on": True, "u_temp_room": 225}),
        (True, None, {"u_pwr_on": True, "u_temp_room": -1000}),
        (False, 20.0, {"u_pwr_on": False, "u_temp_room": 200}),
        (False, None, {"u_pwr_on": False, "u_temp_room": -1000}),
    ],
)
async def test_set_power_and_heat_body(pwr_on, temp_c, expected_body):
    session = FakeSession()
    session.queue_response(FakeResponse(200))

    api = AtmeexApi(session)
    api._token = "t"
    await api.set_power_and_heat(1, pwr_on, temp_c)

    req = session.requests[0]
    assert req[0] == "PUT"
    assert req[2] == expected_body
