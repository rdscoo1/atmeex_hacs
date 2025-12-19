# tests/test_config_flow.py
import pytest
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from types import SimpleNamespace
from homeassistant import data_entry_flow
from custom_components.atmeex_cloud.config_flow import AtmeexConfigFlow, AtmeexOptionsFlowHandler
from custom_components.atmeex_cloud.api import ApiError


def _make_flow() -> AtmeexConfigFlow:
    """Создать инстанс config flow с минимальным hass-заглушкой."""
    flow = AtmeexConfigFlow()
    # Здесь не нужен настоящий HomeAssistant, достаточно любого объекта,
    # так как async_get_clientsession мы замокаем.
    flow.hass = object()
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
async def test_options_flow_sets_update_interval():
    entry = SimpleNamespace(options={"update_interval": 30})
    flow = AtmeexOptionsFlowHandler(entry)

    result = await flow.async_step_init(user_input={"update_interval": 60})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["update_interval"] == 60