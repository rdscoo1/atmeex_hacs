# Atmeex API and Authentication Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every Atmeex HTTP and authentication outcome explicit, bounded, sanitized, recoverable, and distinguishable from a valid empty account without changing any Home Assistant entity, service, option, or automation identifier.

**Architecture:** Keep `AtmeexApi` as the sole HTTP transport and token owner, but replace status/body tuples and fallback empties with typed failures and an operation-driven request policy. Normalize IDs and authoritative booleans at the API boundary, use a token generation under one lock for proactive and reactive recovery, and persist rotated refresh tokens through a callback that cannot trigger an entry reload. The coordinator consumes one authoritative `/devices` response and maps typed failures to Home Assistant's existing setup/update semantics.

**Tech Stack:** Python 3.12+, `asyncio`, `aiohttp`, Home Assistant `DataUpdateCoordinator` APIs compatible with 2024.8+, `pytest`, `pytest-asyncio`, `unittest.mock`.

---

## File Map

- Modify `custom_components/atmeex_cloud/const.py:12-20` — publish the integration/version User-Agent and explicit auth/request timeout constants.
- Modify `custom_components/atmeex_cloud/helpers.py:137-145` — define canonical device-ID and strict Atmeex boolean parsers shared by HTTP normalization and the next state-convergence plan.
- Modify `custom_components/atmeex_cloud/api.py:1-600` — add the typed error hierarchy, strict response parsing, bounded retry policy, generation-aware authentication, refresh rotation callback, normalized devices, and successful empty-body writes.
- Modify `custom_components/atmeex_cloud/coordinator.py:14-174` — remove the duplicate fallback inventory request and map typed API failures to `ConfigEntryAuthFailed` or `UpdateFailed`.
- Modify `custom_components/atmeex_cloud/__init__.py:72-149` — wire refresh-token persistence without reload and map typed setup authentication/connection failures.
- Modify `tests/test_helpers.py:15-34` — lock accepted boolean literals, rejected literals, and canonical string IDs.
- Modify `tests/test_api.py:1-545` — replace the HTTP fake with a response-lifecycle-aware fake and cover error typing, timeouts, retry classification, auth generations, phone recovery, refresh rotation, and payload validation.
- Modify `tests/test_coordinator.py:11-149` — cover one authoritative inventory call and typed coordinator failure mapping.
- Modify `tests/conftest.py:83-112` — keep the shared API fake constructor identical to production.
- Modify `tests/test_setup.py:19-570` — adapt API fakes to the callback constructor and prove token persistence does not reload the entry.
- Modify `tests/test_refresh_device.py:13-29` and `tests/test_websocket_integration.py:46-1050` — migrate integration-local API fakes that are instantiated by production setup.
- Modify `tests/test_climate.py:535-579`, `tests/test_config_flow.py:156-465`, `tests/test_diagnostics.py:98`, `tests/test_entity_base.py:93-141`, and `tests/test_sensor.py:27-177` — migrate test-only `ApiError` constructions to the required two-string compatibility-alias constructor.

## Compatibility Invariants

- `ApiError` remains importable as an exact alias of `AtmeexApiError` until the final hardening plan removes internal shims.
- Entity unique IDs remain byte-for-byte unchanged. Numeric cloud IDs retain
  their legacy outward integer representation (`"0007"` remains public ID `7`),
  while only internal map/store/request keys use canonical strings.
- `set_breezer_mode`, `set_humidifier_stage`, all options keys, translations, and event identifiers remain unchanged.
- A valid `[]` or `{"items": []}` inventory is success. Transport, authentication, rate-limit, malformed-shape, and all-invalid-item responses raise typed errors.
- Only GET transport/rate-limit failures are retried, with no more than three HTTP attempts. A write may be sent again only after an unambiguous 401/403 recovery; it is never retried after timeout or connector failure.
- Raw response bodies, credentials, tokens, phone numbers, email addresses, and raw device payloads never enter error strings or retry logs.

## Execution Gate

Before every task commit, run `.venv/bin/python -m pytest -q` and require all
tests to pass. Until Plans 4 and 6 remove the two documented baseline warnings,
the pre-commit run may still report only the existing un-awaited WebSocket
startup coroutine and unset `asyncio_default_fixture_loop_scope`; it must not
introduce any additional warning.

### Task 1: Establish the Typed, Sanitized API Error Contract

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py:1-23`
- Test: `tests/test_api.py:1-5`
- Test: `tests/test_climate.py:535-579`
- Test: `tests/test_config_flow.py:156-465`
- Test: `tests/test_diagnostics.py:98`
- Test: `tests/test_entity_base.py:93-141`
- Test: `tests/test_sensor.py:27-177`
- Test: `tests/test_coordinator.py:54-62`
- Test: `tests/test_setup.py:214-365`

- [ ] **Step 1: Add focused error-contract tests**

Replace the import at the top of `tests/test_api.py` and add these tests immediately after it:

```python
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
```

- [ ] **Step 2: Run the error-contract tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_typed_api_errors_expose_only_sanitized_context tests/test_api.py::test_api_error_remains_exact_compatibility_alias`

Expected: FAIL during collection with `ImportError: cannot import name 'AtmeexApiError' from 'custom_components.atmeex_cloud.api'`.

- [ ] **Step 3: Implement the complete error hierarchy**

Replace `ApiError` in `custom_components/atmeex_cloud/api.py` with this block and add `import re` with the standard-library imports:

```python
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
```

- [ ] **Step 4: Migrate every repository test that constructs `ApiError`**

Use these exact operation labels while retaining each test's existing message and status:

```python
ApiError("test_climate_command", "network", status=503)
ApiError("test_climate_command", "timeout", status=None)
ApiError("test_config_flow", "fail")
ApiError("test_config_flow", "invalid", status=401)
ApiError("test_config_flow", "rate limited", status=429)
ApiError("test_config_flow", "bad code", status=401)
ApiError("test_config_flow", "bad creds", status=403)
ApiError("test_diagnostics", "boom", status=500)
ApiError("test_entity_command", "boom", status=500)
ApiError("test_sensor", "some error")
ApiError("test_sensor", "boom", status=500)
ApiError("test_coordinator", "unauthorized", status=401)
ApiError("test_setup", "bad creds", status=401)
ApiError("test_setup", "server down", status=500)
ApiError("test_setup", "primary failed", status=500)
ApiError("test_setup", "partial failure", status=500)
ApiError("test_setup", "token expired", status=401)
```

Run: `rg -n 'ApiError\([^,\n]+(?:, status=|\))' tests custom_components/atmeex_cloud`

Expected: only the two-argument constructor tests from this plan and exception-catching references remain; no one-string compatibility-error construction is printed.

- [ ] **Step 5: Convert existing non-status raises to typed two-string errors**

Before running any existing API path, replace every old one-string production raise with the corresponding complete typed raise below:

```python
raise AtmeexAuthenticationError("ensure_token", "credentials are unavailable")
raise AtmeexProtocolError(
    "decode_json",
    "response is not valid JSON",
    status=resp.status,
)
raise AtmeexConnectionError(action_name, "transport failed") from last_exc
raise AtmeexProtocolError("login", "access token is missing")
raise AtmeexAuthenticationError("login", "email credentials are unavailable")
raise AtmeexAuthenticationError("login_phone", "phone credentials are unavailable")
raise AtmeexAuthenticationError("refresh_token", "refresh token is unavailable")
raise AtmeexAuthenticationError(
    "authenticate_phone",
    "refresh token is unavailable",
    status=401,
)
raise AtmeexProtocolError("get_devices", "unexpected collection shape")
raise AtmeexProtocolError("get_device", "device response is not an object")
raise AtmeexProtocolError("set_target_temperature", "invalid temperature")
raise AtmeexProtocolError("set_breezer_mode", "invalid mode")
raise AtmeexProtocolError("set_power_and_heat", "invalid temperature")
```

- [ ] **Step 6: Convert existing HTTP-status branches to typed errors**

For each existing `resp.status >= 400` branch in `_sign_in`, `_sign_in_phone`, `_signin_refresh`, `request_sms_code`, `get_devices`, `get_device`, and `_put_params`, use this exact status-only mapping and do not concatenate `text`, `data`, credentials, or payload values into the message:

```python
if resp.status in (401, 403):
    raise AtmeexAuthenticationError(operation, "authentication rejected", status=resp.status)
if resp.status == 429:
    raise AtmeexRateLimitError(operation, "rate limited", status=resp.status)
if resp.status >= 500:
    raise AtmeexConnectionError(operation, "cloud service unavailable", status=resp.status)
raise AtmeexProtocolError(operation, "request rejected", status=resp.status)
```

Use the literal operation names `login`, `login_phone`, `refresh_token`, `request_sms_code`, `get_devices`, `get_device`, and the existing sanitized `action_name` at their respective call sites.

- [ ] **Step 7: Run the error-contract and migrated consumer tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_typed_api_errors_expose_only_sanitized_context tests/test_api.py::test_api_error_remains_exact_compatibility_alias tests/test_climate.py tests/test_config_flow.py tests/test_diagnostics.py tests/test_entity_base.py tests/test_sensor.py tests/test_coordinator.py tests/test_setup.py`

Expected: PASS; the summary ends with `passed` and contains no collection error or warning.

- [ ] **Step 8: Commit the typed error contract**

```bash
git add custom_components/atmeex_cloud/api.py tests/test_api.py tests/test_climate.py tests/test_config_flow.py tests/test_diagnostics.py tests/test_entity_base.py tests/test_sensor.py tests/test_coordinator.py tests/test_setup.py
git commit -m "refactor: add typed Atmeex API errors"
```

### Task 2: Normalize Device IDs and Authoritative Boolean Values

**Files:**
- Modify: `custom_components/atmeex_cloud/helpers.py:137-145`
- Modify: `custom_components/atmeex_cloud/api.py:24-92`
- Test: `tests/test_helpers.py:15-34`
- Test: `tests/test_api.py:231-315`

- [ ] **Step 1: Replace permissive normalization expectations with strict boundary tests**

Replace the `to_bool` import and test in `tests/test_helpers.py` with this complete block:

```python
from custom_components.atmeex_cloud.helpers import (
    _normalize_device_state,
    normalize_device_id,
    parse_atmeex_bool,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (1, True),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("on", True),
        ("yes", True),
        (False, False),
        (0, False),
        ("0", False),
        ("false", False),
        ("OFF", False),
        ("no", False),
        ("", False),
    ],
)
def test_parse_atmeex_bool_accepts_only_documented_literals(value, expected):
    assert parse_atmeex_bool(value) is expected


@pytest.mark.parametrize("value", [None, "enabled", "2", 2, object()])
def test_parse_atmeex_bool_rejects_unknown_literals(value):
    with pytest.raises(ValueError, match="unsupported Atmeex boolean literal"):
        parse_atmeex_bool(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, "1"), ("1", "1"), (" 0007 ", "7"), (123456789, "123456789")],
)
def test_normalize_device_id_returns_stable_string_key(value, expected):
    assert normalize_device_id(value) == expected


@pytest.mark.parametrize("value", [None, True, False, 1.0, "", "   "])
def test_normalize_device_id_rejects_missing_or_boolean_ids(value):
    with pytest.raises(ValueError, match="invalid Atmeex device id"):
        normalize_device_id(value)
```

Add this API regression test to `tests/test_api.py`:

```python
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
    assert session.requests[0][1].endswith("/devices/zone%2Fa%20b")
```

- [ ] **Step 2: Run normalization tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_helpers.py::test_parse_atmeex_bool_accepts_only_documented_literals tests/test_helpers.py::test_parse_atmeex_bool_rejects_unknown_literals tests/test_helpers.py::test_normalize_device_id_returns_stable_string_key tests/test_helpers.py::test_normalize_device_id_rejects_missing_or_boolean_ids tests/test_api.py::test_device_normalization_overwrites_raw_id_and_rejects_bad_online_literal`

Expected: FAIL during collection because `normalize_device_id` and `parse_atmeex_bool` do not exist.

- [ ] **Step 3: Add the strict shared normalization helpers**

Replace `to_bool` in `custom_components/atmeex_cloud/helpers.py` with this block:

```python
_TRUE_LITERALS = frozenset({"1", "true", "on", "yes"})
_FALSE_LITERALS = frozenset({"", "0", "false", "off", "no"})


def normalize_device_id(value: Any) -> str:
    """Return the canonical string key used for all internal device maps."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid Atmeex device id")
    normalized = str(value).strip()
    if not normalized:
        raise ValueError("invalid Atmeex device id")
    try:
        return str(int(normalized, 10))
    except ValueError:
        pass
    return normalized


def parse_atmeex_bool(value: Any) -> bool:
    """Parse the finite boolean vocabulary accepted by the Atmeex protocol."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        raise ValueError("unsupported Atmeex boolean literal")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_LITERALS:
            return True
        if normalized in _FALSE_LITERALS:
            return False
    raise ValueError("unsupported Atmeex boolean literal")


def to_bool(value: Any) -> bool:
    """Compatibility name for strict protocol boolean parsing."""
    return parse_atmeex_bool(value)
```

In `_normalize_device_state`, normalize the remaining authoritative boolean instead of copying its raw literal:

```python
    if "no_water" in cond:
        out["no_water"] = parse_atmeex_bool(cond["no_water"])
```

Place this block immediately after `out = dict(cond) if cond else {}` so a bad non-empty `no_water` literal propagates as a protocol error through `AtmeexState.from_device_dict`.

- [ ] **Step 4: Make `AtmeexDevice` and `AtmeexState` canonical and protocol-strict**

Import `quote` from `urllib.parse` plus `normalize_device_id` and
`parse_atmeex_bool` in `api.py`, then replace the two dataclasses with this
code:

```python
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
            condition = raw.get("condition") or {}
            settings = raw.get("settings") or {}
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
            raise AtmeexProtocolError("normalize_device_state", "invalid device state") from err
        return cls(id=device_id, raw=normalized)

    def to_ha_dict(self) -> dict[str, Any]:
        return dict(self.raw)
```

In the existing `get_device`, build the path with
`quote(normalize_device_id(device_id), safe="")`; opaque string IDs must never
inject a slash, query, or traversal segment into the request URL. Retain this
encoding when Task 5 replaces the method.

- [ ] **Step 5: Update existing ID assertions and run normalization tests GREEN**

Keep outward-ID assertions compatible while asserting canonical map keys:

```python
assert dev.id == 1
assert dev.raw["id"] == 1
assert data["device_map"]["1"].id == 1
assert runtime.coordinator.data["devices"][0]["id"] == 1
```

Run: `.venv/bin/python -m pytest -q tests/test_helpers.py tests/test_api.py::test_device_normalization_overwrites_raw_id_and_rejects_bad_online_literal tests/test_api.py::test_get_devices_success tests/test_api.py::test_get_device_success tests/test_coordinator.py::test_update_data_builds_states tests/test_setup.py::test_async_setup_entry_happy_path`

Expected: PASS; every selected node is reported as passed.

- [ ] **Step 6: Commit canonical boundary normalization**

```bash
git add custom_components/atmeex_cloud/helpers.py custom_components/atmeex_cloud/api.py tests/test_helpers.py tests/test_api.py tests/test_coordinator.py tests/test_setup.py
git commit -m "fix: normalize Atmeex IDs and booleans"
```

### Task 3: Enforce Response Expectations, Typed HTTP Mapping, and Retry Limits

**Files:**
- Modify: `custom_components/atmeex_cloud/const.py:12-20`
- Modify: `custom_components/atmeex_cloud/api.py:94-394`
- Test: `tests/test_api.py:7-52`
- Test: `tests/test_api.py:202-410`

- [ ] **Step 1: Replace the HTTP fakes with lifecycle- and header-aware test doubles**

Replace `FakeResponse` and `FakeSession` in `tests/test_api.py` with this complete code:

```python
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
```

Update every existing four-target request unpack in the same test file to retain the captured timeout:

```python
method, url, payload, headers, timeout = session.requests[0]
```

Assertions that index `session.requests[0][0]`, `[1]`, or `[2]` remain valid.

- [ ] **Step 2: Add RED tests for status typing, Retry-After, GET limits, and empty writes**

Add these complete tests to `tests/test_api.py`:

```python
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
    assert all(request[4] == 20 for request in session.requests)


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
async def test_sms_transport_failure_is_not_retried_and_has_timeout():
    session = FakeSession()
    session.queue_response(asyncio.TimeoutError())
    api = AtmeexApi(session)

    with pytest.raises(AtmeexConnectionError, match="request_sms_code"):
        await api.request_sms_code("+79991234567")

    assert len(session.requests) == 1
    assert session.requests[0][4] == 20
```

Add `from unittest.mock import AsyncMock` to the imports.

- [ ] **Step 3: Run response-policy tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_429_maps_retry_after_without_exposing_body tests/test_api.py::test_get_transport_failure_uses_exactly_three_bounded_attempts tests/test_api.py::test_put_204_empty_body_is_success_and_is_not_retried tests/test_api.py::test_sms_transport_failure_is_not_retried_and_has_timeout`

Expected: FAIL because the current client includes raw bodies, returns status tuples, retries SMS, and parses the 204 body as JSON.

- [ ] **Step 4: Define the integration User-Agent and timeout constants**

Replace `USER_AGENT` and timeout constants in `custom_components/atmeex_cloud/const.py` with:

```python
INTEGRATION_VERSION = "0.9.5"
USER_AGENT = f"AtmeexCloudHomeAssistant/{INTEGRATION_VERSION}"
API_REQUEST_TIMEOUT_SEC = 20
API_AUTH_TIMEOUT_SEC = 20
RETRY_MAX_ATTEMPTS = 3
RETRY_BASE_DELAY_SEC = 1.0
RETRY_MAX_DELAY_SEC = 8.0
TOKEN_REFRESH_BUFFER_SEC = 60
```

> **Decision note (User-Agent):** this replaces the deliberately chosen
> `okhttp/3.14.9` mimicry — the current `const.py` comment warns the cloud may
> single out non-native clients. Before merging this task, verify a real
> account still signs in and lists devices under the new User-Agent. If the
> cloud rejects or throttles it, keep `USER_AGENT = "okhttp/3.14.9"`, update
> the two User-Agent assertions in Step 8 to expect that value, and drop the
> okhttp pattern from Task 6 Step 5's forbidden-pattern check.

- [ ] **Step 5: Implement the complete response and retry primitives**

Add `from collections.abc import Awaitable, Callable`, `from datetime import datetime, timezone`, and `from email.utils import parsedate_to_datetime` to `api.py`. Extend the `.const` import to include `API_REQUEST_TIMEOUT_SEC`, `API_AUTH_TIMEOUT_SEC`, `RETRY_MAX_ATTEMPTS`, `RETRY_BASE_DELAY_SEC`, `RETRY_MAX_DELAY_SEC`, and `TOKEN_REFRESH_BUFFER_SEC`, then add these module-level helpers:

```python
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
        return AtmeexAuthenticationError(operation, "authentication rejected", status=status)
    if status == 429:
        return AtmeexRateLimitError(
            operation,
            "rate limited",
            status=status,
            retry_after=retry_after,
        )
    if status >= 500:
        return AtmeexConnectionError(operation, "cloud service unavailable", status=status)
    return AtmeexProtocolError(operation, "request rejected", status=status)
```

Replace `_json` and `_with_retries` with these methods:

```python
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
    ) -> Any:
        delay = RETRY_BASE_DELAY_SEC
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                return await call()
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
```

- [ ] **Step 6: Route auth, SMS, GET, and writes through explicit response expectations**

Use these exact signatures and bodies for the low-level calls; Task 4 will add generation-aware 401 recovery around `_request_once`:

```python
    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: Any | None = None,
        expect_json: bool,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{API_BASE_URL}{path}",
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
        path: str,
        *,
        operation: str,
        json_body: Any | None = None,
        expect_json: bool = True,
    ) -> Any:
        await self._ensure_token()

        async def call(*, retry_auth: bool = True) -> Any:
            try:
                return await self._request_once(
                    method,
                    path,
                    operation=operation,
                    json_body=json_body,
                    expect_json=expect_json,
                )
            except AtmeexAuthenticationError:
                # Preserve the existing email-account 401 behavior until Task 4
                # replaces this temporary token comparison with generations.
                if not retry_auth or not self._has_replayable_credentials():
                    raise
                stale_token = self._token
                async with self._lock:
                    if self._token == stale_token:
                        self._token_expires_at = None
                        await self._sign_in()
                return await call(retry_auth=False)

        if method == "GET":
            return await self._call_with_get_retries(operation, call)
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
```

Replace the auth and SMS bodies with these complete methods:

```python
    async def _sign_in(self) -> None:
        if not self._email or not self._password:
            raise AtmeexAuthenticationError("login", "email credentials are unavailable")
        data = await self._auth_post(
            "login",
            {
                "grant_type": "basic",
                "email": self._email,
                "password": self._password,
            },
            expect_json=True,
        )
        self._apply_token_response(data)

    async def _sign_in_phone(self) -> None:
        if not self._phone or not self._phone_code:
            raise AtmeexAuthenticationError("login_phone", "phone credentials are unavailable")
        data = await self._auth_post(
            "login_phone",
            {
                "grant_type": "phone_code",
                "phone": self._phone,
                "phone_code": self._phone_code,
            },
            expect_json=True,
        )
        self._apply_token_response(data)

    async def _signin_refresh(self) -> None:
        if not self._refresh_token:
            raise AtmeexAuthenticationError("refresh_token", "refresh token is unavailable")
        data = await self._auth_post(
            "refresh_token",
            {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            },
            expect_json=True,
        )
        self._apply_token_response(data)

    async def request_sms_code(self, phone: str) -> None:
        await self._auth_post(
            "request_sms_code",
            {"grant_type": "phone_code", "phone": phone},
            expect_json=False,
        )

    async def _put_params(
        self,
        device_id: int | str,
        body: dict[str, Any],
        action_name: str,
        timeout: int = API_REQUEST_TIMEOUT_SEC,
    ) -> None:
        del timeout
        canonical_id = normalize_device_id(device_id)
        await self._request(
            "PUT",
            f"/devices/{canonical_id}/params",
            operation=action_name,
            json_body=body,
            expect_json=False,
        )

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
            raise AtmeexProtocolError("get_devices", "unexpected collection shape")
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
        canonical_id = normalize_device_id(device_id)
        payload = await self._request(
            "GET",
            f"/devices/{quote(canonical_id, safe='')}",
            operation="get_device",
            expect_json=True,
        )
        if not isinstance(payload, dict):
            raise AtmeexProtocolError("get_device", "device response is not an object")
        return AtmeexDevice.from_raw(payload)
```

Do not wrap any auth, SMS, code-exchange, or PUT method in `_call_with_get_retries`.

- [ ] **Step 7: Replace legacy error-message tests with typed-error assertions**

Delete `test_get_devices_error_no_fallback` and `test_get_devices_500_does_not_trigger_relogin`, then replace the remaining affected tests with this complete code:

```python
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
async def test_set_breezer_mode_invalid_raises_protocol_error():
    api = AtmeexApi(FakeSession())
    api._token = "access"

    with pytest.raises(AtmeexProtocolError, match="set_breezer_mode"):
        await api.set_breezer_mode("1", 4)
```

- [ ] **Step 8: Update User-Agent assertions and run response-policy tests GREEN**

Replace both old User-Agent assertions in `tests/test_api.py` with:

```python
assert headers["User-Agent"] == "AtmeexCloudHomeAssistant/0.9.5"
```

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_429_maps_retry_after_without_exposing_body tests/test_api.py::test_get_transport_failure_uses_exactly_three_bounded_attempts tests/test_api.py::test_put_204_empty_body_is_success_and_is_not_retried tests/test_api.py::test_sms_transport_failure_is_not_retried_and_has_timeout tests/test_api.py::test_user_agent_header_set_on_signin tests/test_api.py::test_user_agent_header_set_on_authorized_requests tests/test_api.py::test_set_commands_not_retried_on_network_timeout`

Expected: PASS; three GET attempts, one SMS attempt, one PUT attempt, and no raw-body text appear in failures.

- [ ] **Step 9: Commit request policy and response expectations**

```bash
git add custom_components/atmeex_cloud/const.py custom_components/atmeex_cloud/api.py tests/test_api.py
git commit -m "fix: classify Atmeex HTTP failures and retries"
```

### Task 4: Add Generation-Aware Token Recovery and Refresh Rotation

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py:94-446`
- Test: `tests/test_api.py:121-229`
- Test: `tests/test_api.py:413-465`

- [ ] **Step 1: Replace legacy 401 tests with deterministic recovery and rotation tests**

Delete `test_phone_account_does_not_replay_signin_on_401` and `test_concurrent_401_sign_in_called_once`, then add these imports and complete replacement tests to `tests/test_api.py`:

```python
from unittest.mock import MagicMock


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
```

- [ ] **Step 2: Run token tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_concurrent_401_uses_one_refresh_and_new_generation tests/test_api.py::test_phone_reactive_401_refreshes_without_replaying_sms_code tests/test_api.py::test_refresh_transient_failure_retains_refresh_token tests/test_api.py::test_definitive_refresh_rejection_clears_runtime_token_only`

Expected: FAIL because `AtmeexApi.__init__` has no callback, `token_generation` and `async_refresh_access_token` do not exist, and reactive phone 401 is not recovered.

- [ ] **Step 3: Add the exact token snapshot and callback interface**

Add these declarations above `AtmeexApi`:

```python
RefreshTokenChangedCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class _TokenSnapshot:
    token: str
    token_type: str
    generation: int
```

Replace the constructor and token properties with:

```python
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
        self._refresh_token: str | None = None
        self._on_refresh_token_changed = on_refresh_token_changed
        self._email: str | None = None
        self._password: str | None = None
        self._phone: str | None = None
        self._phone_code: str | None = None
        self._retry_count = 0
        self._token_expires_at: float | None = None
        self._lock = asyncio.Lock()

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
            raise AtmeexAuthenticationError("request", "access token is unavailable")
        return _TokenSnapshot(self._token, self._token_type, self._token_generation)

    def _headers_for(self, snapshot: _TokenSnapshot) -> dict[str, str]:
        headers = self._unauth_headers()
        headers["Authorization"] = f"{snapshot.token_type} {snapshot.token}"
        return headers
```

- [ ] **Step 4: Apply token responses atomically and notify only on rotation**

Replace `_apply_token_response` with this complete method:

```python
    def _apply_token_response(self, data: Any, operation: str) -> None:
        if not isinstance(data, dict):
            raise AtmeexProtocolError(operation, "token response is not an object")
        token = data.get("access_token") or data.get("token")
        if not isinstance(token, str) or not token:
            raise AtmeexProtocolError(operation, "access token is missing")

        self._token = token
        self._token_type = str(data.get("token_type") or "Bearer")
        self._token_generation += 1
        expires_in = data.get("expires_in")
        self._token_expires_at = (
            time.time() + int(expires_in)
            if isinstance(expires_in, (int, float))
            else None
        )

        rotated = data.get("refresh_token")
        if isinstance(rotated, str) and rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            callback = self._on_refresh_token_changed
            if callback is not None:
                try:
                    callback(rotated)
                except Exception:
                    _LOGGER.warning("Atmeex refresh-token persistence callback failed")
```

Pass the sanitized operation (`"login"`, `"login_phone"`, or `"refresh_token"`) at all three call sites.

- [ ] **Step 5: Implement serialized proactive and reactive recovery**

Delete `_headers` and `_call_with_get_retries`; generation snapshots and the final `_request` loop replace both. Then replace `_ensure_token`, `_signin_refresh`, and the low-level authenticated request methods with these final methods:

```python
    async def _signin_refresh(self) -> None:
        refresh_token = self._refresh_token
        if not refresh_token:
            raise AtmeexAuthenticationError("refresh_token", "refresh token is unavailable")
        try:
            data = await self._auth_post(
                "refresh_token",
                {"grant_type": "refresh_token", "refresh_token": refresh_token},
                expect_json=True,
            )
        except AtmeexAuthenticationError:
            self._refresh_token = None
            raise
        self._apply_token_response(data, "refresh_token")

    async def _recover_locked(self, rejected: _TokenSnapshot, status: int) -> None:
        if self._token and self._token_generation > rejected.generation:
            return
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

    async def _ensure_token(self) -> None:
        if self._token_is_valid():
            return
        observed_generation = self._token_generation
        async with self._lock:
            if self._token_is_valid() and self._token_generation > observed_generation:
                return
            rejected = _TokenSnapshot(
                self._token or "",
                self._token_type,
                observed_generation,
            )
            await self._recover_locked(rejected, 401)

    async def async_refresh_access_token(self) -> None:
        """Recover credentials once under the shared token lock for HTTP or WS."""
        observed = self._token_snapshot() if self._token else _TokenSnapshot("", self._token_type, self._token_generation)
        async with self._lock:
            if self._token and self._token_generation > observed.generation:
                return
            await self._recover_locked(observed, 401)

    async def _request_once(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        snapshot: _TokenSnapshot,
        json_body: Any | None,
        expect_json: bool,
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{API_BASE_URL}{path}",
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
        path: str,
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
                    if isinstance(err, AtmeexRateLimitError) and err.retry_after is not None
                    else retry_delay
                )
                await asyncio.sleep(min(wait, RETRY_MAX_DELAY_SEC))
                retry_delay = min(retry_delay * 2, RETRY_MAX_DELAY_SEC)
        raise AtmeexConnectionError(operation, "retry budget exhausted")
```

- [ ] **Step 6: Run auth-generation tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_concurrent_401_uses_one_refresh_and_new_generation tests/test_api.py::test_phone_reactive_401_refreshes_without_replaying_sms_code tests/test_api.py::test_refresh_transient_failure_retains_refresh_token tests/test_api.py::test_definitive_refresh_rejection_clears_runtime_token_only tests/test_api.py::test_login_phone_clears_email_credentials`

Expected: PASS; the concurrent test records one refresh POST, phone recovery records no `phone_code` POST, and the transient 503 retains `keep-me`.

- [ ] **Step 7: Commit generation-aware authentication**

```bash
git add custom_components/atmeex_cloud/api.py tests/test_api.py
git commit -m "fix: serialize Atmeex token recovery"
```

### Task 5: Make Inventory Authoritative and Persist Rotated Refresh Tokens Without Reload

**Files:**
- Modify: `custom_components/atmeex_cloud/api.py:448-501`
- Modify: `custom_components/atmeex_cloud/coordinator.py:14-174`
- Modify: `custom_components/atmeex_cloud/__init__.py:72-149`
- Test: `tests/test_api.py:231-315`
- Test: `tests/test_coordinator.py:43-149`
- Test: `tests/conftest.py:83-112`
- Test: `tests/test_setup.py:19-570`
- Test: `tests/test_refresh_device.py:13-29`
- Test: `tests/test_websocket_integration.py:46-1050`

- [ ] **Step 1: Replace fallback-empty coverage with RED inventory contract tests**

Delete `test_get_devices_error_with_fallback_returns_empty_list`, then add these complete tests to `tests/test_api.py`:

```python
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
```

- [ ] **Step 2: Add RED coordinator single-call and typed-mapping tests**

Replace the old fallback tests in `tests/test_coordinator.py` with:

```python
@pytest.mark.asyncio
async def test_fetch_devices_uses_one_authoritative_inventory_call():
    coord, api = _make_coordinator(devices=[])

    assert await coord._fetch_devices_safely() == []
    api.get_devices.assert_awaited_once_with()
    api.get_device.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError],
)
async def test_update_data_maps_transient_typed_errors_to_update_failed(error_type):
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(side_effect=error_type("get_devices", "failed"))

    with pytest.raises(UpdateFailed, match="Atmeex API update failed"):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_update_data_maps_typed_auth_error_to_config_entry_auth_failed():
    coord, api = _make_coordinator()
    api.get_devices = AsyncMock(
        side_effect=AtmeexAuthenticationError(
            "get_devices",
            "authentication rejected",
            status=401,
        )
    )

    with pytest.raises(ConfigEntryAuthFailed, match="Atmeex authentication failed"):
        await coord._async_update_data()
```

Import all four typed errors, `ConfigEntryAuthFailed`, and `UpdateFailed` at module scope.

- [ ] **Step 3: Adapt every production-instantiated API fake to the fixed callback constructor**

Change `make_fake_api_class` in `tests/conftest.py`, every `FakeApi.__init__`
in `tests/test_setup.py`, the module-level fake in
`tests/test_refresh_device.py`, and every local API fake in
`tests/test_websocket_integration.py` to accept and retain the callback without
invoking it:

```python
        def __init__(self, session, *, on_refresh_token_changed=None):
            self.session = session
            self.on_refresh_token_changed = on_refresh_token_changed
```

Keep each fake's existing field initialization immediately after these two assignments. For fakes that currently name the first argument `_session`, retain `_session` as the assignment source:

```python
        def __init__(self, _session, *, on_refresh_token_changed=None):
            self.session = _session
            self.on_refresh_token_changed = on_refresh_token_changed
```

Run this constructor audit before continuing:

```bash
rg -n 'class FakeApi|def __init__\(self, _?session\)' tests/conftest.py tests/test_setup.py tests/test_refresh_device.py tests/test_websocket_integration.py
```

Expected: every API fake displayed beneath those classes accepts
`*, on_refresh_token_changed=None`; no production-instantiated one-argument
fake remains. Direct `AtmeexApi(session)` use in API unit tests remains valid
because the callback is optional.

- [ ] **Step 4: Add RED setup test for rotation persistence without reload**

Add this helper and self-contained test to `tests/test_setup.py`:

```python
def _setup_test_coordinator_class():
    class DummyCoordinator:
        def __init__(self, hass, logger, name, update_interval, **kwargs):
            self.hass = hass
            self.data = None
            self.last_update_success = False
            self.last_api_error = None
            self.last_success_ts = None
            self._ws_device_update_ts = {}
            self._refresh_device_update_ts = {}

        def setup_update(self, *, api, fire_logbook_event):
            import types

            from custom_components.atmeex_cloud.coordinator import (
                AtmeexCoordinator as RealCoordinator,
            )

            self._api = api
            self._fire_logbook_event = fire_logbook_event
            self._api_error_last_ts = float("-inf")
            self._api_error_suppressed = 0
            for method_name in (
                "_fetch_devices_safely",
                "_fire_api_error_event",
                "_async_update_data",
            ):
                method = getattr(RealCoordinator, method_name)
                setattr(self, method_name, types.MethodType(method, self))

        async def async_config_entry_first_refresh(self):
            self.data = await self._async_update_data()
            self.last_update_success = True

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            self.data = await self._async_update_data()

    return DummyCoordinator


async def test_rotated_refresh_token_persists_without_entry_reload(monkeypatch):
    callbacks: list[object] = []

    class FakeApi:
        def __init__(self, _session, *, on_refresh_token_changed=None):
            callbacks.append(on_refresh_token_changed)
            self.async_init = AsyncMock()
            self.login = AsyncMock()
            self.refresh_token = "stored"
            self._refresh_token = "stored"
            self.token = "access"
            device = AtmeexDevice.from_raw({"id": 1, "condition": {}, "settings": {}})
            self.get_devices = AsyncMock(return_value=[device])
            self.get_device = AsyncMock(return_value=device)

    monkeypatch.setattr(atmeex_init, "AtmeexApi", FakeApi)
    monkeypatch.setattr(atmeex_init, "async_get_clientsession", lambda _hass: object())
    monkeypatch.setattr(atmeex_init, "AtmeexCoordinator", _setup_test_coordinator_class())
    update_entry = MagicMock()
    reload_entry = AsyncMock()
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_update_entry=update_entry,
            async_reload=reload_entry,
        ),
    )
    captured_listener = {}

    def add_update_listener(callback):
        captured_listener["callback"] = callback
        return lambda: None

    entry = SimpleNamespace(
        data={"email": "user@example.com", "password": "pw", "refresh_token": "stored"},
        options={"enable_websocket": False},
        entry_id="entry1",
        add_update_listener=add_update_listener,
        async_on_unload=lambda _callback: None,
    )

    assert await atmeex_init.async_setup_entry(hass, entry) is True
    callbacks[0]("rotated")
    entry.data = {**entry.data, "refresh_token": "rotated"}
    await captured_listener["callback"](hass, entry)

    update_entry.assert_called_once_with(
        entry,
        data={"email": "user@example.com", "password": "pw", "refresh_token": "rotated"},
    )
    reload_entry.assert_not_awaited()
```

- [ ] **Step 5: Run inventory and persistence tests and confirm RED**

Run: `.venv/bin/python -m pytest -q tests/test_api.py::test_empty_inventory_is_valid_authoritative_success tests/test_api.py::test_malformed_inventory_shape_is_protocol_failure tests/test_api.py::test_nonempty_all_invalid_inventory_is_protocol_failure tests/test_coordinator.py::test_fetch_devices_uses_one_authoritative_inventory_call tests/test_coordinator.py::test_update_data_maps_transient_typed_errors_to_update_failed tests/test_coordinator.py::test_update_data_maps_typed_auth_error_to_config_entry_auth_failed tests/test_setup.py::test_rotated_refresh_token_persists_without_entry_reload`

Expected: FAIL because fallback empties remain, the coordinator calls `/devices` twice, and setup does not supply the rotation callback.

- [ ] **Step 6: Implement strict one-response inventory parsing**

Replace `get_devices` with:

```python
    async def get_devices(self) -> list[AtmeexDevice]:
        payload = await self._request(
            "GET",
            "/devices",
            operation="get_devices",
            expect_json=True,
        )
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        else:
            raise AtmeexProtocolError("get_devices", "unexpected collection shape")
        if not items:
            return []

        devices: list[AtmeexDevice] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                devices.append(AtmeexDevice.from_raw(item))
            except AtmeexProtocolError:
                continue
        if not devices:
            raise AtmeexProtocolError("get_devices", "non-empty collection has no valid device items")
        return devices

    async def get_device(self, device_id: int | str) -> AtmeexDevice:
        canonical_id = normalize_device_id(device_id)
        payload = await self._request(
            "GET",
            f"/devices/{canonical_id}",
            operation="get_device",
            expect_json=True,
        )
        if not isinstance(payload, dict):
            raise AtmeexProtocolError("get_device", "device response is not an object")
        device = AtmeexDevice.from_raw(payload)
        if normalize_device_id(device.id) != canonical_id:
            raise AtmeexProtocolError("get_device", "device id does not match request")
        return device
```

- [ ] **Step 7: Make coordinator failure handling typed and authoritative**

Replace `_fetch_devices_safely` and its API exception branch with:

```python
    async def _fetch_devices_safely(self) -> list[AtmeexDevice]:
        api = self._api
        if api is None:
            raise AtmeexProtocolError("get_devices", "coordinator API is not configured")
        devices = await api.get_devices()
        hydrated: list[AtmeexDevice] = []
        for device in devices:
            try:
                hydrated.append(await api.get_device(device.id))
            except AtmeexAuthenticationError:
                raise
            except (AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError):
                hydrated.append(device)
        return hydrated
```

```python
        except AtmeexAuthenticationError as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {"message": str(err), "status": err.status, "source": "coordinator_update"}
            )
            raise ConfigEntryAuthFailed("Atmeex authentication failed") from err
        except (AtmeexConnectionError, AtmeexRateLimitError, AtmeexProtocolError) as err:
            self.last_api_error = err
            self._fire_api_error_event(
                {"message": str(err), "status": err.status, "source": "coordinator_update"}
            )
            raise UpdateFailed("Atmeex API update failed") from err
```

Delete the old status inspection and both `fallback=` calls. Keep the unexpected-exception branch so programming errors are still visible as `UpdateFailed` with the original cause.

- [ ] **Step 8: Wire rotation persistence and option-only reloads in setup**

Move `stored_refresh_token` above API construction and use this exact callback and constructor:

```python
    stored_refresh_token = entry.data.get("refresh_token")

    def _persist_refresh_token(refresh_token: str) -> None:
        if entry.data.get("refresh_token") == refresh_token:
            return
        new_data = {**entry.data, "refresh_token": refresh_token}
        try:
            hass.config_entries.async_update_entry(entry, data=new_data)
        except Exception:
            _LOGGER.warning("Failed to persist rotated Atmeex refresh token")

    api = AtmeexApi(
        session,
        on_refresh_token_changed=_persist_refresh_token,
    )
    await api.async_init()
    if stored_refresh_token:
        api.restore_refresh_token(stored_refresh_token)
```

Add this public method to `AtmeexApi` beside the `refresh_token` property so the composition root never assigns the private attribute:

```python
    def restore_refresh_token(self, refresh_token: str) -> None:
        """Seed the refresh token persisted on the config entry before any request."""
        self._refresh_token = refresh_token
```

Delete the post-login persistence block at current lines 114-120. Replace `_update_listener` and listener registration with:

```python
    options_snapshot = dict(entry.options)

    async def _update_listener(hass: HomeAssistant, updated_entry: ConfigEntry) -> None:
        nonlocal options_snapshot
        current_options = dict(updated_entry.options)
        if current_options == options_snapshot:
            return
        options_snapshot = current_options
        await hass.config_entries.async_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_update_listener))
```

Catch `AtmeexAuthenticationError` as `ConfigEntryAuthFailed`; catch `AtmeexConnectionError`, `AtmeexRateLimitError`, and `AtmeexProtocolError` as `ConfigEntryNotReady`. Do not include credential values or server bodies in either Home Assistant exception message.

- [ ] **Step 9: Run inventory, coordinator, and setup tests GREEN**

Run: `.venv/bin/python -m pytest -q tests/test_api.py tests/test_coordinator.py tests/test_setup.py`

Expected: PASS; a valid empty collection uses one request, malformed/all-invalid collections raise protocol errors, and rotation persistence records one config-entry update with zero reloads.

- [ ] **Step 10: Commit authoritative inventory and persistence wiring**

```bash
git add custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests/test_api.py tests/test_coordinator.py tests/conftest.py tests/test_setup.py tests/test_refresh_device.py tests/test_websocket_integration.py
git commit -m "fix: distinguish empty inventory from cloud failure"
```

### Task 6: Verify the Shippable API/Auth Subproject

**Files:**
- Verify: `custom_components/atmeex_cloud/api.py`
- Verify: `custom_components/atmeex_cloud/helpers.py`
- Verify: `custom_components/atmeex_cloud/coordinator.py`
- Verify: `custom_components/atmeex_cloud/__init__.py`
- Verify: `tests/`

- [ ] **Step 1: Run the focused API/auth subsystem**

Run: `.venv/bin/python -m pytest -q tests/test_api.py tests/test_helpers.py tests/test_coordinator.py tests/test_setup.py`

Expected: PASS; the final line contains `passed`, with zero failures or errors.
The existing pytest-asyncio fixture-loop-scope notice remains assigned to Plan 6.

- [ ] **Step 2: Run all auth/error consumers**

Run: `.venv/bin/python -m pytest -q tests/test_climate.py tests/test_config_flow.py tests/test_diagnostics.py tests/test_entity_base.py tests/test_sensor.py tests/test_switch.py`

Expected: PASS; no consumer fails because `ApiError` is the exact compatibility alias of `AtmeexApiError`.

- [ ] **Step 3: Run the complete repository suite**

Run: `.venv/bin/python -m pytest -q`

Expected: PASS; the final summary reports zero failed tests. Only the previously
recorded WebSocket-startup RuntimeWarning and pytest-asyncio configuration notice
may remain; Plans 4 and 6 remove them respectively.

- [ ] **Step 4: Compile the integration package**

Run: `.venv/bin/python -m compileall -q custom_components/atmeex_cloud`

Expected: exit status 0 and no output.

- [ ] **Step 5: Verify forbidden fallback and privacy patterns are absent**

Run: `rg -n 'fallback=True|okhttp/3\.14\.9|await resp\.text\(|str\(data\)|text\[:|api\._refresh_token' custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py`

Expected: no matches and exit status 1.

- [ ] **Step 6: Commit any verification-only correction**

If Steps 1-5 required a code correction, stage only the files changed by that correction and commit them with:

```bash
git add custom_components/atmeex_cloud/api.py custom_components/atmeex_cloud/helpers.py custom_components/atmeex_cloud/coordinator.py custom_components/atmeex_cloud/__init__.py tests
git commit -m "test: close Atmeex API reliability regressions"
```

If Steps 1-5 required no correction, do not create an empty commit.
