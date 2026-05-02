# tests/test_config_flow.py
import pytest
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from types import SimpleNamespace
from homeassistant import data_entry_flow
from custom_components.atmeex_cloud.config_flow import AtmeexConfigFlow, AtmeexOptionsFlowHandler
from custom_components.atmeex_cloud.api import ApiError
from custom_components.atmeex_cloud.const import (
    CONF_ENABLE_WEBSOCKET,
    CONF_UPDATE_INTERVAL,
    DEFAULT_ENABLE_WEBSOCKET,
    DEFAULT_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
    CONF_AUTH_METHOD,
    CONF_PHONE,
    CONF_PHONE_CODE,
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_PHONE,
)


def _make_flow() -> AtmeexConfigFlow:
    """Создать инстанс config flow с минимальным hass-заглушкой."""
    flow = AtmeexConfigFlow()
    # Здесь не нужен настоящий HomeAssistant, достаточно любого объекта,
    # так как async_get_clientsession мы замокаем.
    flow.hass = object()
    return flow


def _make_reauth_flow(entry_data: dict | None = None) -> AtmeexConfigFlow:
    flow = AtmeexConfigFlow()
    entry = SimpleNamespace(
        entry_id="entry1",
        data=entry_data or {CONF_EMAIL: "old@example.com", CONF_PASSWORD: "oldpwd"},
    )
    flow.hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda _entry_id: entry),
    )
    flow.context = {"entry_id": "entry1"}
    return flow


def _make_phone_reauth_flow(phone: str = "+79991234567") -> AtmeexConfigFlow:
    """Reauth flow targeting a phone-account config entry."""
    return _make_reauth_flow(
        entry_data={
            CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
            CONF_PHONE: phone,
        }
    )


@pytest.mark.asyncio
async def test_user_step_shows_email_phone_menu():
    """async_step_user routes to a menu picking email vs phone."""
    flow = _make_flow()
    result = await flow.async_step_user(user_input=None)

    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "user"
    assert AUTH_METHOD_EMAIL in result["menu_options"]
    assert AUTH_METHOD_PHONE in result["menu_options"]


@pytest.mark.asyncio
async def test_email_step_success_creates_entry_with_auth_method():
    """Email step creates an entry tagged with CONF_AUTH_METHOD=email."""
    flow = _make_flow()

    user_input = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls, patch.object(
        flow, "async_set_unique_id", AsyncMock()
    ) as mock_set_uid, patch.object(
        flow, "_abort_if_unique_id_configured"
    ) as mock_abort:
        get_session.return_value = object()
        mock_abort.return_value = None

        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])

        result = await flow.async_step_email(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == user_input[CONF_EMAIL]
    assert result["data"] == {
        CONF_AUTH_METHOD: AUTH_METHOD_EMAIL,
        CONF_EMAIL: user_input[CONF_EMAIL],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
    }

    mock_set_uid.assert_awaited_once_with(user_input[CONF_EMAIL])
    mock_abort.assert_called_once()


@pytest.mark.asyncio
async def test_email_step_cannot_connect():
    """Network ApiError → cannot_connect on the email form."""
    flow = _make_flow()

    user_input = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()

        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login.side_effect = ApiError("fail")

        result = await flow.async_step_email(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "email"
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_email_step_invalid_auth():
    flow = _make_flow()
    user_input = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login.side_effect = ApiError("invalid", status=401)

        result = await flow.async_step_email(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_email_step_unknown_error():
    flow = _make_flow()

    user_input = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "pwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()

        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login.side_effect = RuntimeError("boom")

        result = await flow.async_step_email(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


@pytest.mark.asyncio
async def test_phone_step_sends_sms_then_advances_to_phone_code():
    """Submitting a phone number requests an SMS and advances to phone_code."""
    flow = _make_flow()

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.request_sms_code = AsyncMock()

        result = await flow.async_step_phone(
            user_input={CONF_PHONE: " +7 (999) 123-45-67 "}
        )

    # Phone is normalized for storage and SMS request
    api.request_sms_code.assert_awaited_once_with("+79991234567")
    # Flow advances to the phone_code form
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "phone_code"
    assert flow._pending_phone == "+79991234567"


@pytest.mark.asyncio
async def test_phone_step_sms_request_failure_shows_error_on_phone_form():
    flow = _make_flow()

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.request_sms_code = AsyncMock(side_effect=ApiError("rate limited", status=429))

        result = await flow.async_step_phone(user_input={CONF_PHONE: "+79991234567"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "phone"
    assert result["errors"]["base"] == "cannot_connect"
    # Don't advance until SMS succeeds
    assert flow._pending_phone is None


@pytest.mark.asyncio
async def test_phone_code_step_success_creates_entry_with_phone_auth():
    """Submitting a valid SMS code creates a CONF_AUTH_METHOD=phone entry."""
    flow = _make_flow()
    flow._pending_phone = "+79991234567"

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls, patch.object(
        flow, "async_set_unique_id", AsyncMock()
    ) as mock_set_uid, patch.object(
        flow, "_abort_if_unique_id_configured"
    ) as mock_abort:
        get_session.return_value = object()
        mock_abort.return_value = None

        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login_phone = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])
        # The flow persists refresh_token if available
        type(api).refresh_token = property(lambda _self: "rt-from-login")

        result = await flow.async_step_phone_code(
            user_input={CONF_PHONE_CODE: "1234"}
        )

    api.login_phone.assert_awaited_once_with("+79991234567", "1234")
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "+79991234567"
    assert result["data"] == {
        CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
        CONF_PHONE: "+79991234567",
        "refresh_token": "rt-from-login",
    }
    mock_set_uid.assert_awaited_once_with("+79991234567")
    mock_abort.assert_called_once()


@pytest.mark.asyncio
async def test_phone_code_step_invalid_code():
    flow = _make_flow()
    flow._pending_phone = "+79991234567"

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login_phone = AsyncMock(side_effect=ApiError("bad code", status=401))

        result = await flow.async_step_phone_code(
            user_input={CONF_PHONE_CODE: "wrong"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "phone_code"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_phone_code_step_aborts_when_state_lost():
    """Defensive: directly hitting phone_code without _pending_phone aborts."""
    flow = _make_flow()
    assert flow._pending_phone is None

    result = await flow.async_step_phone_code(user_input=None)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "phone_state_lost"


@pytest.mark.asyncio
async def test_options_flow_sets_update_interval():
    entry = SimpleNamespace(options={"update_interval": 30})
    flow = AtmeexOptionsFlowHandler()
    flow._config_entry = entry

    result = await flow.async_step_init(
        user_input={CONF_UPDATE_INTERVAL: 60, CONF_ENABLE_WEBSOCKET: True}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_UPDATE_INTERVAL] == 60
    assert result["data"][CONF_ENABLE_WEBSOCKET] is True


@pytest.mark.asyncio
async def test_options_flow_clamps_interval_and_parses_invalid():
    entry = SimpleNamespace(options={})
    flow = AtmeexOptionsFlowHandler()
    flow._config_entry = entry

    result_low = await flow.async_step_init(
        user_input={CONF_UPDATE_INTERVAL: "bad", CONF_ENABLE_WEBSOCKET: False}
    )
    assert result_low["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result_low["data"][CONF_UPDATE_INTERVAL] == DEFAULT_UPDATE_INTERVAL
    assert result_low["data"][CONF_ENABLE_WEBSOCKET] is False

    result_high = await flow.async_step_init(
        user_input={CONF_UPDATE_INTERVAL: MAX_UPDATE_INTERVAL + 999}
    )
    assert result_high["data"][CONF_UPDATE_INTERVAL] == MAX_UPDATE_INTERVAL
    assert result_high["data"][CONF_ENABLE_WEBSOCKET] == DEFAULT_ENABLE_WEBSOCKET

    result_min = await flow.async_step_init(
        user_input={CONF_UPDATE_INTERVAL: MIN_UPDATE_INTERVAL - 999}
    )
    assert result_min["data"][CONF_UPDATE_INTERVAL] == MIN_UPDATE_INTERVAL


@pytest.mark.asyncio
async def test_options_flow_show_form_with_current_values():
    entry = SimpleNamespace(
        options={
            CONF_UPDATE_INTERVAL: 45,
            CONF_ENABLE_WEBSOCKET: False,
        }
    )
    flow = AtmeexOptionsFlowHandler()
    flow._config_entry = entry

    result = await flow.async_step_init(user_input=None)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"


@pytest.mark.asyncio
async def test_reauth_step_shows_confirm_form():
    flow = _make_reauth_flow()
    result = await flow.async_step_reauth(entry_data={})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"


@pytest.mark.asyncio
async def test_reauth_confirm_success_updates_entry():
    flow = _make_reauth_flow()
    user_input = {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls, patch.object(
        flow, "async_set_unique_id", AsyncMock()
    ) as set_uid, patch.object(
        flow, "_abort_if_unique_id_mismatch"
    ) as abort_mismatch, patch.object(
        flow, "async_update_reload_and_abort"
    ) as update_reload:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])
        update_reload.return_value = {
            "type": data_entry_flow.FlowResultType.ABORT,
            "reason": "reauth_successful",
        }

        await flow.async_step_reauth(entry_data={})
        result = await flow.async_step_reauth_confirm(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    set_uid.assert_awaited_once_with("new@example.com")
    abort_mismatch.assert_called_once()
    update_reload.assert_called_once()


@pytest.mark.asyncio
async def test_reauth_confirm_invalid_auth():
    flow = _make_reauth_flow()
    user_input = {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login.side_effect = ApiError("bad creds", status=403)

        await flow.async_step_reauth(entry_data={})
        result = await flow.async_step_reauth_confirm(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_reauth_confirm_unknown_error():
    flow = _make_reauth_flow()
    user_input = {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login.side_effect = RuntimeError("boom")

        await flow.async_step_reauth(entry_data={})
        result = await flow.async_step_reauth_confirm(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


@pytest.mark.asyncio
async def test_phone_reauth_routes_to_phone_confirm_step():
    """Reauth on a phone-account entry shows the phone confirm form, not email."""
    flow = _make_phone_reauth_flow()
    result = await flow.async_step_reauth(entry_data={})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "reauth_phone_confirm"


@pytest.mark.asyncio
async def test_phone_reauth_does_not_send_sms_until_user_confirms():
    """The phone reauth confirm step must NOT request an SMS just by being shown."""
    flow = _make_phone_reauth_flow()

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.request_sms_code = AsyncMock()

        # Just showing the form (no user_input) must not hit the network.
        await flow.async_step_reauth(entry_data={})

    api.request_sms_code.assert_not_called()


@pytest.mark.asyncio
async def test_phone_reauth_sends_sms_when_user_submits_confirmation():
    """After the user submits the confirm form, SMS is requested and we advance."""
    flow = _make_phone_reauth_flow()
    await flow.async_step_reauth(entry_data={})

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.request_sms_code = AsyncMock()

        result = await flow.async_step_reauth_phone_confirm(user_input={})

    api.request_sms_code.assert_awaited_once_with("+79991234567")
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "phone_code"
    assert flow._pending_phone == "+79991234567"


@pytest.mark.asyncio
async def test_phone_reauth_code_step_updates_entry_with_new_refresh_token():
    """Successful phone reauth code submission updates the entry."""
    flow = _make_phone_reauth_flow()
    await flow.async_step_reauth(entry_data={})
    flow._pending_phone = "+79991234567"

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls, patch.object(
        flow, "async_set_unique_id", AsyncMock()
    ) as mock_set_uid, patch.object(
        flow, "_abort_if_unique_id_mismatch"
    ) as mock_abort_mismatch, patch.object(
        flow, "async_update_reload_and_abort"
    ) as mock_update_reload:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login_phone = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])
        type(api).refresh_token = property(lambda _self: "new-rt")

        mock_update_reload.return_value = {
            "type": data_entry_flow.FlowResultType.ABORT,
            "reason": "reauth_successful",
        }

        result = await flow.async_step_phone_code(
            user_input={CONF_PHONE_CODE: "5678"}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    mock_set_uid.assert_awaited_once_with("+79991234567")
    mock_abort_mismatch.assert_called_once()
    update_call = mock_update_reload.call_args
    assert update_call.kwargs["data_updates"] == {
        CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
        CONF_PHONE: "+79991234567",
        "refresh_token": "new-rt",
    }


@pytest.mark.asyncio
async def test_reauth_confirm_success_without_entry_aborts():
    flow = _make_flow()
    flow._reauth_entry = None
    user_input = {CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpwd"}

    with patch(
        "custom_components.atmeex_cloud.config_flow.async_get_clientsession"
    ) as get_session, patch(
        "custom_components.atmeex_cloud.config_flow.AtmeexApi"
    ) as api_cls:
        get_session.return_value = object()
        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])

        result = await flow.async_step_reauth_confirm(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
