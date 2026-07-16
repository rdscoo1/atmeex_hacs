import asyncio
import logging
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock

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
    _retry_after_seconds,
)
from custom_components.atmeex_cloud.const import (
    API_AUTH_TIMEOUT_SEC,
    API_REQUEST_TIMEOUT_SEC,
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
async def test_get_retry_redacts_transport_exception_and_sanitizes_operation(
    caplog, monkeypatch
):
    session = FakeSession()
    for _index in range(3):
        session.queue_response(ClientError("household-secret-response"))
    api = AtmeexApi(session)
    api._token = "access"

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    with caplog.at_level(logging.WARNING, logger="custom_components.atmeex_cloud.api"):
        with pytest.raises(AtmeexConnectionError) as raised:
            await api._request(
                "GET",
                "/devices",
                operation="get devices\n",
            )

    assert raised.value.operation == "get_devices"
    assert "household-secret-response" not in str(raised.value)
    assert "household-secret-response" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type", "expected_attempts"),
    [
        (401, AtmeexAuthenticationError, 1),
        (403, AtmeexAuthenticationError, 1),
        (429, AtmeexRateLimitError, 3),
        (500, AtmeexConnectionError, 3),
        (404, AtmeexProtocolError, 1),
    ],
)
async def test_get_devices_maps_status_without_exposing_response_body(
    status, error_type, expected_attempts, monkeypatch
):
    session = FakeSession()
    for _index in range(3):
        session.queue_response(
            FakeResponse(status, text_data="household-secret-response")
        )
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(error_type) as raised:
        await api.get_devices()

    assert "household-secret-response" not in str(raised.value)
    assert len(session.requests) == expected_attempts


def test_retry_after_http_date_and_invalid_values():
    assert _retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT") > 0
    assert _retry_after_seconds("Thu, 01 Jan 1970 00:00:00 GMT") == 0
    assert _retry_after_seconds("not-a-date") is None


class FakeResponse:
    def __init__(
        self,
        status: int,
        json_data=None,
        text_data: str = "",
        *,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status = status
        self._json = json_data
        self._body = text_data.encode()
        self.headers = headers or {}
        self._json_error = json_error
        self.read_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, *, content_type=None):
        if self._json_error is not None:
            raise self._json_error
        return self._json

    async def read(self):
        self.read_called = True
        return self._body


class FakeSession:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, object, dict[str, str] | None, object]] = []
        self._responses: list[FakeResponse | Exception] = []

    def queue_response(self, response: FakeResponse | Exception) -> None:
        self._responses.append(response)

    def request(self, method, url, json=None, headers=None, timeout=None):
        self.requests.append((method, url, json, headers, timeout))
        assert self._responses, "No queued response"
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, url, json=None, headers=None, timeout=None):
        return self.request("POST", url, json=json, headers=headers, timeout=timeout)


@pytest.mark.asyncio
async def test_429_maps_retry_after_without_exposing_body(monkeypatch):
    session = FakeSession()
    responses = [
        FakeResponse(
            429,
            text_data="household-secret-response",
            headers={"Retry-After": "12"},
        )
        for _index in range(3)
    ]
    for response in responses:
        session.queue_response(response)
    api = AtmeexApi(session)
    api._token = "access"
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(AtmeexRateLimitError) as caught:
        await api.get_device("7")

    assert caught.value.status == 429
    assert caught.value.retry_after == 12.0
    assert "household-secret-response" not in str(caught.value)
    assert all(response.read_called for response in responses)


@pytest.mark.asyncio
async def test_malformed_success_json_is_consumed_and_sanitized():
    session = FakeSession()
    response = FakeResponse(
        200,
        json_error=ValueError("household-secret-response"),
    )
    session.queue_response(response)
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexProtocolError) as raised:
        await api.get_devices()

    assert raised.value.operation == "get_devices"
    assert "household-secret-response" not in str(raised.value)
    assert response.read_called is True
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_get_transport_failure_uses_exactly_three_bounded_attempts(monkeypatch):
    session = FakeSession()
    session.queue_response(asyncio.TimeoutError())
    session.queue_response(asyncio.TimeoutError())
    session.queue_response(asyncio.TimeoutError())
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexConnectionError, match="get_devices"):
        await api.get_devices()

    assert len(session.requests) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [1.0, 2.0]
    assert all(
        request[4] == API_REQUEST_TIMEOUT_SEC
        for request in session.requests
    )


@pytest.mark.asyncio
async def test_put_204_empty_body_is_success_and_is_not_retried():
    session = FakeSession()
    response = FakeResponse(204)
    session.queue_response(response)
    api = AtmeexApi(session)
    api._token = "access"

    await api.set_power("7", True)

    assert len(session.requests) == 1
    assert response.read_called is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("device_id", "encoded_id"),
    [
        ("zone/a b", "zone%2Fa%20b"),
        (".", "%2E"),
        ("..", "%2E%2E"),
    ],
)
async def test_put_preserves_opaque_device_id_segment(device_id, encoded_id):
    session = FakeSession()
    session.queue_response(FakeResponse(204))
    api = AtmeexApi(session)
    api._token = "access"

    await api.set_power(device_id, True)

    assert str(session.requests[0][1]).endswith(
        f"/devices/{encoded_id}/params"
    )


@pytest.mark.asyncio
async def test_sms_transport_failure_is_not_retried_and_has_timeout():
    session = FakeSession()
    session.queue_response(asyncio.TimeoutError())
    api = AtmeexApi(session)

    with pytest.raises(AtmeexConnectionError, match="request_sms_code"):
        await api.request_sms_code("+79991234567")

    assert len(session.requests) == 1
    assert session.requests[0][4] == API_AUTH_TIMEOUT_SEC


@pytest.mark.asyncio
async def test_get_reauth_consumes_shared_three_get_attempt_budget(monkeypatch):
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="expired"))
    session.queue_response(FakeResponse(200, json_data={"access_token": "new"}))
    for _index in range(3):
        session.queue_response(FakeResponse(500, text_data="unavailable"))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = AtmeexApi(session)
    api._token = "old"
    api._email = "user@example.com"
    api._password = "password"

    with pytest.raises(AtmeexConnectionError) as raised:
        await api.get_devices()

    assert raised.value.operation == "get_devices"
    assert [request[0] for request in session.requests].count("GET") == 3
    assert [request[0] for request in session.requests].count("POST") == 1


@pytest.mark.asyncio
async def test_get_does_not_retry_failed_auth_recovery(monkeypatch):
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="expired"))
    session.queue_response(asyncio.TimeoutError())
    session.queue_response(FakeResponse(200, json_data=[]))
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)
    api = AtmeexApi(session)
    api._token = "old"
    api._email = "user@example.com"
    api._password = "password"

    with pytest.raises(AtmeexConnectionError) as raised:
        await api.get_devices()

    assert raised.value.operation == "login"
    assert [request[0] for request in session.requests] == ["GET", "POST"]
    sleep.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [None, [], "token", {"access_token": 7}, {"access_token": object()}],
)
async def test_login_rejects_malformed_success_payload(payload):
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=payload))
    api = AtmeexApi(session)

    with pytest.raises(AtmeexProtocolError) as raised:
        await api.login("user@example.com", "password")

    assert raised.value.operation == "login"


@pytest.mark.parametrize(
    "expires_in",
    [float("nan"), float("inf"), 10**400],
    ids=["nan", "infinity", "overflow"],
)
def test_invalid_token_expiry_does_not_mutate_any_token_state(expires_in):
    persisted = MagicMock()
    api = AtmeexApi(FakeSession(), on_refresh_token_changed=persisted)
    api._token = "old-access"
    api._token_type = "OldType"
    api._token_generation = 7
    api._token_expires_at = 12345.0
    api._refresh_token = "old-refresh"

    with pytest.raises(AtmeexProtocolError, match="refresh_token"):
        api._apply_token_response(
            {
                "access_token": "new-access",
                "token_type": "NewType",
                "expires_in": expires_in,
                "refresh_token": "new-refresh",
            },
            "refresh_token",
        )

    assert api._token == "old-access"
    assert api._token_type == "OldType"
    assert api.token_generation == 7
    assert api._token_expires_at == 12345.0
    assert api.refresh_token == "old-refresh"
    persisted.assert_not_called()


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


def test_device_retains_immutable_nested_section_provenance():
    omitted = AtmeexDevice.from_raw({"id": 1})
    null_sections = AtmeexDevice.from_raw(
        {"id": 2, "condition": None, "settings": None}
    )
    explicit = AtmeexDevice.from_raw(
        {"id": 3, "condition": {}, "settings": {}}
    )

    assert omitted.raw["condition"] == omitted.raw["settings"] == {}
    assert omitted.condition_present is False
    assert omitted.settings_present is False
    assert null_sections.condition_present is False
    assert null_sections.settings_present is False
    assert explicit.condition_present is True
    assert explicit.settings_present is True
    with pytest.raises(FrozenInstanceError):
        explicit.condition_present = False


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
async def test_get_device_rejects_response_for_a_different_canonical_id():
    session = FakeSession()
    session.queue_response(
        FakeResponse(200, json_data={"id": "8", "condition": {}, "settings": {}})
    )
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexProtocolError, match="device id does not match request"):
        await api.get_device("0007")

    assert str(session.requests[0][1]).endswith("/devices/7")


def test_restore_refresh_token_seeds_persisted_value():
    api = AtmeexApi(FakeSession())

    api.restore_refresh_token("persisted-refresh")

    assert api.refresh_token == "persisted-refresh"


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
    method, url, payload, _headers, timeout = session.requests[0]
    assert method == "POST"
    assert url == f"{API_BASE_URL}/auth/signin"
    # теперь login отправляет grant_type="basic"
    assert payload["email"] == "user@example.com"
    assert payload["password"] == "pwd"
    assert payload["grant_type"] == "basic"


@pytest.mark.asyncio
async def test_user_agent_header_set_on_signin():
    """Sign-in advertises the integration name and version."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={"access_token": "t"}))

    api = AtmeexApi(session)
    await api.login("user@example.com", "pwd")

    _method, _url, _payload, headers, timeout = session.requests[0]
    assert headers["User-Agent"] == "AtmeexCloudHomeAssistant/0.9.5"


@pytest.mark.asyncio
async def test_user_agent_header_set_on_authorized_requests():
    """Authenticated GET carries the integration User-Agent."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=[]))

    api = AtmeexApi(session)
    api._token = "t"

    await api.get_devices()

    _method, _url, _payload, headers, timeout = session.requests[0]
    assert headers["User-Agent"] == "AtmeexCloudHomeAssistant/0.9.5"


@pytest.mark.asyncio
async def test_request_sms_code_posts_signup_with_phone_code_grant():
    """SMS request must hit /auth/signup with grant_type=phone_code."""
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data={}))

    api = AtmeexApi(session)
    await api.request_sms_code("+79991234567")

    method, url, payload, headers, timeout = session.requests[0]
    assert method == "POST"
    assert url == f"{API_BASE_URL}/auth/signup"
    assert payload == {"grant_type": "phone_code", "phone": "+79991234567"}
    assert headers["User-Agent"] == "AtmeexCloudHomeAssistant/0.9.5"


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

    method, url, payload, _headers, timeout = session.requests[0]
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
async def test_concurrent_401_uses_one_refresh_and_new_generation():
    all_old_requests_started = asyncio.Event()

    class BarrierResponse(FakeResponse):
        async def read(self):
            await all_old_requests_started.wait()
            return await super().read()

    class Concurrent401Session:
        def __init__(self) -> None:
            self.requests = []
            self.old_get_count = 0

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.requests.append((method, url, json, headers, timeout))
            if method == "POST":
                return FakeResponse(
                    200,
                    json_data={"access_token": "new", "refresh_token": "rotated"},
                )
            authorization = (headers or {}).get("Authorization")
            if authorization == "Bearer new":
                return FakeResponse(200, json_data=[])
            self.old_get_count += 1
            if self.old_get_count == 5:
                all_old_requests_started.set()
            return BarrierResponse(401, text_data="private-body")

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = Concurrent401Session()
    persisted = MagicMock()
    api = AtmeexApi(session, on_refresh_token_changed=persisted)
    api._token = "old"
    api._refresh_token = "refresh"

    results = await asyncio.gather(*(api.get_devices() for _index in range(5)))

    assert results == [[], [], [], [], []]
    refresh_posts = [request for request in session.requests if request[0] == "POST"]
    assert len(refresh_posts) == 1
    assert api.token == "new"
    assert api.token_generation == 1
    persisted.assert_called_once_with("rotated")


@pytest.mark.asyncio
async def test_concurrent_401_shares_transient_refresh_failure_and_later_retries():
    all_old_requests_started = asyncio.Event()

    class BarrierResponse(FakeResponse):
        async def read(self):
            await all_old_requests_started.wait()
            return await super().read()

    class ConcurrentTransientRefreshSession:
        def __init__(self) -> None:
            self.requests = []
            self.old_get_count = 0
            self.allow_refresh_success = False

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.requests.append((method, url, json, headers, timeout))
            if method == "POST":
                if self.allow_refresh_success:
                    return FakeResponse(
                        200,
                        json_data={"access_token": "new-access"},
                    )
                return FakeResponse(503, text_data="private-outage-body")
            if (headers or {}).get("Authorization") == "Bearer new-access":
                return FakeResponse(200, json_data=[])
            self.old_get_count += 1
            if self.old_get_count == 5:
                all_old_requests_started.set()
            if self.old_get_count <= 5:
                return BarrierResponse(401, text_data="private-auth-body")
            return FakeResponse(401, text_data="private-auth-body")

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = ConcurrentTransientRefreshSession()
    api = AtmeexApi(session)
    api._token = "old-access"
    api._refresh_token = "keep-refresh"

    results = await asyncio.gather(
        *(api.get_devices() for _index in range(5)),
        return_exceptions=True,
    )

    assert all(isinstance(result, AtmeexConnectionError) for result in results)
    assert all(result.operation == "refresh_token" for result in results)
    assert all(result.status == 503 for result in results)
    assert [request[0] for request in session.requests].count("POST") == 1
    assert api.refresh_token == "keep-refresh"

    session.allow_refresh_success = True

    assert await api.get_devices() == []
    assert [request[0] for request in session.requests].count("POST") == 2


@pytest.mark.asyncio
async def test_delayed_stale_401_shares_newest_failure_for_same_generation():
    delayed_request_started = asyncio.Event()
    release_delayed_response = asyncio.Event()

    class Delayed401Response(FakeResponse):
        async def read(self):
            delayed_request_started.set()
            await release_delayed_response.wait()
            return await super().read()

    class InterleavedFailureSession:
        def __init__(self) -> None:
            self.requests = []
            self.get_count = 0

        def request(self, method, url, json=None, headers=None, timeout=None):
            self.requests.append((method, url, json, headers, timeout))
            if method == "POST":
                return FakeResponse(503, text_data="private-outage-body")
            self.get_count += 1
            if self.get_count == 1:
                return Delayed401Response(401, text_data="private-auth-body")
            return FakeResponse(401, text_data="private-auth-body")

        def post(self, url, json=None, headers=None, timeout=None):
            return self.request("POST", url, json=json, headers=headers, timeout=timeout)

    session = InterleavedFailureSession()
    api = AtmeexApi(session)
    api._token = "old-access"
    api._refresh_token = "keep-refresh"

    delayed_request = asyncio.create_task(api.get_devices())
    await delayed_request_started.wait()

    for _index in range(2):
        with pytest.raises(AtmeexConnectionError):
            await api.get_devices()

    assert [request[0] for request in session.requests].count("POST") == 2

    release_delayed_response.set()
    with pytest.raises(AtmeexConnectionError):
        await delayed_request

    assert [request[0] for request in session.requests].count("POST") == 2


@pytest.mark.asyncio
async def test_phone_reactive_401_refreshes_without_replaying_sms_code():
    session = FakeSession()
    session.queue_response(FakeResponse(401))
    session.queue_response(FakeResponse(200, json_data={"access_token": "new-phone"}))
    session.queue_response(FakeResponse(200, json_data=[]))
    api = AtmeexApi(session)
    api._token = "old-phone"
    api._refresh_token = "phone-refresh"
    api._phone = "+79991234567"
    api._phone_code = "already-consumed"

    assert await api.get_devices() == []

    signin_payloads = [request[2] for request in session.requests if request[0] == "POST"]
    assert signin_payloads == [
        {"grant_type": "refresh_token", "refresh_token": "phone-refresh"}
    ]


@pytest.mark.asyncio
async def test_refresh_transient_failure_retains_refresh_token():
    session = FakeSession()
    session.queue_response(FakeResponse(503, text_data="private-outage-body"))
    api = AtmeexApi(session)
    api._refresh_token = "keep-me"

    with pytest.raises(AtmeexConnectionError, match="refresh_token"):
        await api.async_refresh_access_token()

    assert api.refresh_token == "keep-me"


@pytest.mark.asyncio
async def test_definitive_refresh_rejection_clears_runtime_token_only():
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="private-auth-body"))
    api = AtmeexApi(session)
    api._refresh_token = "invalid"

    with pytest.raises(AtmeexAuthenticationError, match="refresh_token"):
        await api.async_refresh_access_token()

    assert api.refresh_token is None


@pytest.mark.asyncio
async def test_refresh_403_clears_runtime_refresh_token():
    session = FakeSession()
    session.queue_response(FakeResponse(403, text_data="private-auth-body"))
    api = AtmeexApi(session)
    api._refresh_token = "invalid"

    with pytest.raises(AtmeexAuthenticationError) as raised:
        await api.async_refresh_access_token()

    assert raised.value.operation == "refresh_token"
    assert raised.value.status == 403
    assert api.refresh_token is None


@pytest.mark.asyncio
async def test_transient_refresh_failure_does_not_fall_back_to_email_login():
    session = FakeSession()
    session.queue_response(FakeResponse(503, text_data="private-outage-body"))
    session.queue_response(FakeResponse(200, json_data={"access_token": "email"}))
    api = AtmeexApi(session)
    api._token = "old-access"
    api._refresh_token = "keep-refresh"
    api._email = "user@example.com"
    api._password = "password"

    with pytest.raises(AtmeexConnectionError) as raised:
        await api.async_refresh_access_token()

    assert raised.value.operation == "refresh_token"
    assert [request[2]["grant_type"] for request in session.requests] == [
        "refresh_token"
    ]
    assert api.refresh_token == "keep-refresh"


@pytest.mark.asyncio
async def test_cancelled_refresh_releases_lock_without_poisoning_next_attempt():
    refresh_started = asyncio.Event()
    keep_refresh_pending = asyncio.Event()

    class BlockingRefreshResponse(FakeResponse):
        async def json(self, *, content_type=None):
            refresh_started.set()
            await keep_refresh_pending.wait()
            return await super().json(content_type=content_type)

    session = FakeSession()
    session.queue_response(
        BlockingRefreshResponse(200, json_data={"access_token": "cancelled"})
    )
    session.queue_response(FakeResponse(200, json_data={"access_token": "new-access"}))
    api = AtmeexApi(session)
    api._refresh_token = "keep-refresh"

    cancelled_refresh = asyncio.create_task(api.async_refresh_access_token())
    await refresh_started.wait()
    cancelled_refresh.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_refresh

    assert api._recovery_attempt == 0
    assert api._recovery_failure is None

    await api.async_refresh_access_token()

    assert api.token == "new-access"
    assert api._recovery_attempt == 1
    assert [request[0] for request in session.requests] == ["POST", "POST"]


def test_unchanged_or_empty_refresh_token_does_not_notify_callback():
    persisted = MagicMock()
    api = AtmeexApi(FakeSession(), on_refresh_token_changed=persisted)
    api._refresh_token = "same-refresh"

    api._apply_token_response(
        {"access_token": "first", "refresh_token": "same-refresh"},
        "refresh_token",
    )
    api._apply_token_response(
        {"access_token": "second", "refresh_token": ""},
        "refresh_token",
    )

    assert api.refresh_token == "same-refresh"
    persisted.assert_not_called()


def test_refresh_callback_exception_is_tolerated_without_logging_secret(caplog):
    secret = "private-rotated-refresh"
    persisted = MagicMock(side_effect=RuntimeError(secret))
    api = AtmeexApi(FakeSession(), on_refresh_token_changed=persisted)
    api._token = "old-access"
    api._refresh_token = "old-refresh"

    with caplog.at_level(logging.WARNING, logger="custom_components.atmeex_cloud.api"):
        api._apply_token_response(
            {
                "access_token": "new-access",
                "token_type": "Custom",
                "expires_in": 60,
                "refresh_token": secret,
            },
            "refresh_token",
        )

    assert api.token == "new-access"
    assert api._token_type == "Custom"
    assert api.token_generation == 1
    assert api.refresh_token == secret
    persisted.assert_called_once_with(secret)
    assert "persistence callback failed" in caplog.text
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_put_401_replays_once_with_identical_encoded_url():
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="expired"))
    session.queue_response(FakeResponse(200, json_data={"access_token": "new"}))
    session.queue_response(FakeResponse(204))
    api = AtmeexApi(session)
    api._token = "old"
    api._refresh_token = "refresh"

    await api.set_power("..", True)

    assert [request[0] for request in session.requests] == ["PUT", "POST", "PUT"]
    put_requests = [request for request in session.requests if request[0] == "PUT"]
    assert put_requests[0][1] is put_requests[1][1]
    assert str(put_requests[0][1]).endswith("/devices/%2E%2E/params")
    assert put_requests[0][2] == put_requests[1][2] == {"u_pwr_on": True}


@pytest.mark.asyncio
async def test_put_403_does_not_refresh_or_replay():
    session = FakeSession()
    session.queue_response(FakeResponse(403, text_data="forbidden"))
    session.queue_response(FakeResponse(200, json_data={"access_token": "new"}))
    session.queue_response(FakeResponse(204))
    api = AtmeexApi(session)
    api._token = "old"
    api._refresh_token = "refresh"

    with pytest.raises(AtmeexAuthenticationError) as raised:
        await api.set_power("..", True)

    assert raised.value.status == 403
    assert [request[0] for request in session.requests] == ["PUT"]
    assert api.token == "old"
    assert api.refresh_token == "refresh"


@pytest.mark.asyncio
async def test_request_sms_code_429_raises_typed_rate_limit():
    session = FakeSession()
    session.queue_response(
        FakeResponse(429, text_data="private-body", headers={"Retry-After": "4"})
    )
    api = AtmeexApi(session)

    with pytest.raises(AtmeexRateLimitError) as caught:
        await api.request_sms_code("+79991234567")

    assert caught.value.operation == "request_sms_code"
    assert caught.value.status == 429
    assert caught.value.retry_after == 4.0
    assert len(session.requests) == 1


@pytest.mark.asyncio
async def test_login_401_raises_typed_authentication_error():
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="private-body"))
    api = AtmeexApi(session)

    with pytest.raises(AtmeexAuthenticationError) as caught:
        await api.login("user@example.com", "wrong")

    assert caught.value.operation == "login"
    assert caught.value.status == 401


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

    method, url, _payload, headers, timeout = session.requests[0]
    assert method == "GET"
    assert url == f"{API_BASE_URL}/devices"
    assert headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_get_devices_500_retries_without_relogin(monkeypatch):
    session = FakeSession()
    for _index in range(3):
        session.queue_response(FakeResponse(500, text_data="private-body"))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    api = AtmeexApi(session)
    api._token = "access"
    api._email = "user@example.com"
    api._password = "password"

    with pytest.raises(AtmeexConnectionError) as caught:
        await api.get_devices()

    assert caught.value.operation == "get_devices"
    assert caught.value.status == 500
    assert len(session.requests) == 3
    assert all(request[0] == "GET" for request in session.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [[], {"items": []}])
async def test_empty_inventory_is_valid_authoritative_success(payload):
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=payload))
    api = AtmeexApi(session)
    api._token = "access"

    assert await api.get_devices() == []
    assert len(session.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"items": {}}, {"devices": []}, "not-a-collection", None],
)
async def test_malformed_inventory_shape_is_protocol_failure(payload):
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=payload))
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexProtocolError, match="get_devices"):
        await api.get_devices()


@pytest.mark.asyncio
async def test_nonempty_all_invalid_inventory_is_protocol_failure():
    session = FakeSession()
    session.queue_response(FakeResponse(200, json_data=[{"name": "missing-id"}, 7]))
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexProtocolError, match="no valid device items"):
        await api.get_devices()


@pytest.mark.asyncio
async def test_get_devices_rejects_duplicate_canonical_ids():
    session = FakeSession()
    session.queue_response(
        FakeResponse(
            200,
            json_data=[
                {"id": 7, "condition": {}, "settings": {}},
                {"id": "0007", "condition": {}, "settings": {}},
            ],
        )
    )
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(
        AtmeexProtocolError,
        match="duplicate canonical device id",
    ) as caught:
        await api.get_devices()

    assert str(caught.value) == (
        "get_devices: duplicate canonical device id"
    )


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

    method, url, _payload, headers, timeout = session.requests[0]
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
    assert str(req[1]).startswith(f"{API_BASE_URL}/devices/1/params")
    assert req[2] == expected_body


@pytest.mark.asyncio
async def test_setter_500_raises_without_retry():
    session = FakeSession()
    session.queue_response(FakeResponse(500, text_data="private-body"))
    api = AtmeexApi(session)
    api._token = "access"

    with pytest.raises(AtmeexConnectionError) as caught:
        await api.set_power("1", True)

    assert caught.value.operation == "set_power"
    assert caught.value.status == 500
    assert len(session.requests) == 1


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
async def test_set_breezer_mode_invalid_raises_protocol_error():
    api = AtmeexApi(FakeSession())
    api._token = "access"

    with pytest.raises(AtmeexProtocolError, match="set_breezer_mode"):
        await api.set_breezer_mode("1", 4)


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
