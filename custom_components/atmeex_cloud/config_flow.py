from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.data_entry_flow import FlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AtmeexApi, ApiError
from .const import (
    DOMAIN,
    CONF_UPDATE_INTERVAL,
    CONF_ENABLE_WEBSOCKET,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_ENABLE_WEBSOCKET,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Схема формы первого шага конфиг-флоу:
# пользователь вводит email и пароль от облака Atmeex.
DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AtmeexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow для интеграции Atmeex Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Первый (и единственный) шаг мастера настройки.

        На этом шаге:
        * запрашиваем логин/пароль;
        * проверяем, что авторизация удачна и API отвечает;
        * создаём ConfigEntry с указанным email в качестве title.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            # Приводим поведение к __init__.py
            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                # 1. Проверяем логин/пароль и получаем токен.
                await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])

                # 2. Делаем небольшой sanity-check: хотя бы один успешный вызов API.
                await api.get_devices()

                # 3. Делаем email уникальным идентификатором конфигурации,
                #    чтобы не создавать дубликаты интеграции для одного и того же аккаунта.
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()

                # 4. Создаём конфигурационную запись.
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

            except ApiError as err:
                # Ошибка авторизации / сети → показываем стандартную ошибку cannot_connect.
                status = getattr(err, "status", None)
                errors["base"] = "invalid_auth" if status in (401, 403) else "cannot_connect"
            except Exception as err:  # noqa: BLE001 — хотим залогировать вообще всё
                _LOGGER.exception(
                    "Unexpected error during Atmeex config flow: %s",
                    err,
                )
                errors["base"] = "unknown"

        # Если это первый заход или произошла ошибка — показываем форму снова.
        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Handle re-authentication when credentials become invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm re-authentication with new credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                await api.get_devices()

                # Update the existing config entry with new credentials
                if self._reauth_entry:
                    self.hass.config_entries.async_update_entry(
                        self._reauth_entry,
                        data={
                            **self._reauth_entry.data,
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                        },
                    )
                    await self.hass.config_entries.async_reload(
                        self._reauth_entry.entry_id
                    )
                    return self.async_abort(reason="reauth_successful")

                return self.async_abort(reason="reauth_successful")

            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = "invalid_auth" if status in (401, 403) else "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error during Atmeex reauth flow: %s",
                    err,
                )
                errors["base"] = "unknown"

        # Pre-fill email from existing entry if available
        suggested_email = ""
        if self._reauth_entry:
            suggested_email = self._reauth_entry.data.get(CONF_EMAIL, "")

        reauth_schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL, default=suggested_email): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=reauth_schema,
            errors=errors,
        )


class AtmeexOptionsFlowHandler(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None) -> FlowResult:
        if user_input is not None:
            interval = int(user_input[CONF_UPDATE_INTERVAL])
            interval = max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, interval))
            enable_ws = user_input.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)
            
            return self.async_create_entry(
                title="",
                data={
                    CONF_UPDATE_INTERVAL: interval,
                    CONF_ENABLE_WEBSOCKET: enable_ws,
                },
            )

        options = getattr(self._config_entry, "options", {}) or {}
        current_interval = options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        current_ws = options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)

        schema = vol.Schema(
            {
                vol.Optional(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int),
                    vol.Clamp(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                ),
                vol.Optional(CONF_ENABLE_WEBSOCKET, default=current_ws): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)