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


@pytest.mark.asyncio
async def test_config_flow_success():
    """Успешный проход конфиг-флоу с созданием config entry."""
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
        # Сессия нам не важна, просто возвращаем объект-заглушку
        get_session.return_value = object()
        # Не прерывать флоу из-за уже существующей интеграции
        mock_abort.return_value = None

        api = api_cls.return_value
        api.async_init = AsyncMock()
        api.login = AsyncMock()
        api.get_devices = AsyncMock(return_value=[])

        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == user_input[CONF_EMAIL]
    assert result["data"] == user_input

    mock_set_uid.assert_awaited_once_with(user_input[CONF_EMAIL])
    mock_abort.assert_called_once()


@pytest.mark.asyncio
async def test_config_flow_cannot_connect():
    """ApiError → cannot_connect и форма с ошибкой."""
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

        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "cannot_connect"


@pytest.mark.asyncio
async def test_config_flow_invalid_auth():
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

        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "invalid_auth"


@pytest.mark.asyncio
async def test_config_flow_unknown_error():
    """Любая неожиданная ошибка → base=unknown и форма."""
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

        result = await flow.async_step_user(user_input=user_input)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"]["base"] == "unknown"


@pytest.mark.asyncio
async def test_config_flow_show_form_without_input():
    flow = _make_flow()
    result = await flow.async_step_user(user_input=None)

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_options_flow_sets_update_interval():
    entry = SimpleNamespace(options={"update_interval": 30})
    flow = AtmeexOptionsFlowHandler(entry)

    result = await flow.async_step_init(
        user_input={CONF_UPDATE_INTERVAL: 60, CONF_ENABLE_WEBSOCKET: True}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_UPDATE_INTERVAL] == 60
    assert result["data"][CONF_ENABLE_WEBSOCKET] is True


@pytest.mark.asyncio
async def test_options_flow_clamps_interval_and_parses_invalid():
    entry = SimpleNamespace(options={})
    flow = AtmeexOptionsFlowHandler(entry)

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
    flow = AtmeexOptionsFlowHandler(entry)

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
