from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AtmeexApi, ApiError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AtmeexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Atmeex Cloud."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            try:
                # Проверяем, что логин/пароль валидны и токен рабочий
                await api.login(user_input[CONF_EMAIL], user_input[CONF_PASSWORD])
                await api.get_devices()  # sanity-check

                # Уникальность по email, чтобы не плодить дубликаты
                await self.async_set_unique_id(user_input[CONF_EMAIL])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )

            except ApiError:
                errors["base"] = "cannot_connect"
            except Exception as err:  # noqa: BLE001 — хотим залогировать всё
                _LOGGER.exception(
                    "Unexpected error during Atmeex config flow: %s",
                    err,
                )
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )