from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AtmeexApi, ApiError
from .const import DOMAIN

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

    async def async_step_user(self, user_input=None):
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
                if status in (401, 403):
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
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


class AtmeexOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow для Atmeex Cloud (пока минимальная заглушка).

    Сейчас не даёт дополнительных опций, но:
    * позволяет HA открывать диалог «Настройки интеграции»;
    * в будущем сюда легко добавить реальные настройки (флаги, интервалы и т.п.).
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Сохраняем ссылку на ConfigEntry для будущих расширений."""
        self._config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Единственный шаг options flow.

        Сейчас:
        * при первом открытии показываем пустую форму;
        * при submit просто создаём запись без изменений (data = {}).

        Позже здесь можно добавить:
        * чекбоксы (например, enable_diagnostics_sensor);
        * выбор интервала опроса и т.п.
        """
        if user_input is not None:
            # Пока никаких опций не сохраняем — просто завершаем поток.
            return self.async_create_entry(title="", data={})

        # Пустая форма — просто кнопка «Сохранить».
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({}),
        )
