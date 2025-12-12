import pytest

from custom_components.atmeex_cloud.api import AtmeexApi, ApiError, API_BASE_URL, AtmeexDevice


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
async def test_login_error_raises_apierror():
    session = FakeSession()
    session.queue_response(FakeResponse(401, text_data="unauthorized"))

    api = AtmeexApi(session)

    with pytest.raises(ApiError) as exc:
        await api.login("user@example.com", "wrong")

    # формат сообщения сохраняем
    assert "Auth failed 401" in str(exc.value)


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

    # Сообщение вида "get_devices 500: error" — можно при желании проверить
    assert "get_devices 500" in str(exc.value)


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
    assert url == f"{API_BASE_URL}/devices/1"
    assert headers["Authorization"] == "Bearer t"


@pytest.mark.asyncio
async def test_get_device_error_raises():
    session = FakeSession()
    session.queue_response(FakeResponse(404, text_data="not found"))

    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.get_device(123)

    assert "GET /devices/123 404" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method_name, body",
    [
        ("set_power", {"u_pwr_on": True}),
        ("set_target_temperature", {"u_temp_room": 215}),
        ("set_fan_speed", {"u_fan_speed": 3}),
        ("set_brizer_mode", {"u_damp_pos": 2}),
        ("set_humid_stage", {"u_hum_stg": 1}),
    ],
)
async def test_setters_success(method_name, body):
    session = FakeSession()
    session.queue_response(FakeResponse(200))

    api = AtmeexApi(session)
    api._token = "t"

    method = getattr(api, method_name)
    if method_name == "set_target_temperature":
        await method(1, 21.5)
    elif method_name == "set_power":
        await method(1, True)
    else:
        # для остальных уже int
        await method(1, list(body.values())[0])

    req = session.requests[0]
    assert req[0] == "PUT"
    assert req[1].startswith(f"{API_BASE_URL}/devices/1/params")
    assert req[2] == body


@pytest.mark.asyncio
async def test_setter_error_raises():
    session = FakeSession()
    session.queue_response(FakeResponse(500))

    api = AtmeexApi(session)
    api._token = "t"

    with pytest.raises(ApiError) as exc:
        await api.set_power(1, True)

    assert "set_power 500" in str(exc.value)
