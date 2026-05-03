from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import AtmeexApi, ApiError
from .const import (
    DOMAIN,
    CONF_UPDATE_INTERVAL,
    CONF_ENABLE_WEBSOCKET,
    CONF_ENABLE_CO2,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_ENABLE_WEBSOCKET,
    DEFAULT_ENABLE_CO2,
    MIN_UPDATE_INTERVAL,
    MAX_UPDATE_INTERVAL,
    CONF_AUTH_METHOD,
    CONF_PHONE,
    CONF_PHONE_CODE,
    AUTH_METHOD_EMAIL,
    AUTH_METHOD_PHONE,
)

_LOGGER = logging.getLogger(__name__)

# Email auth schema (step "email" + reauth_confirm for email accounts).
DATA_SCHEMA_EMAIL = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

# Phone number entry — step "phone".
DATA_SCHEMA_PHONE = vol.Schema(
    {
        vol.Required(CONF_PHONE): str,
    }
)

# SMS code entry — step "phone_code".
DATA_SCHEMA_PHONE_CODE = vol.Schema(
    {
        vol.Required(CONF_PHONE_CODE): str,
    }
)

# Reauth confirmation for phone accounts has no fields — submitting the
# empty form authorizes us to re-request an SMS code.
DATA_SCHEMA_REAUTH_PHONE_CONFIRM = vol.Schema({})


def _clean_email(email: str) -> str:
    """Normalize user input for storage/login."""
    return email.strip()


def _email_unique_id(email: str) -> str:
    """Normalize email for stable unique_id checks."""
    return _clean_email(email).casefold()


def _clean_phone(phone: str) -> str:
    """Strip everything except leading + and digits.

    The cloud accepts whatever it gets; we just normalize so cosmetic
    differences (spaces, dashes, parentheses) don't create duplicate entries.
    """
    cleaned = "".join(c for c in phone.strip() if c == "+" or c.isdigit())
    return cleaned


def _phone_unique_id(phone: str) -> str:
    """Stable unique_id for phone accounts."""
    return _clean_phone(phone)


class AtmeexConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow для интеграции Atmeex Cloud."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reauth_entry: ConfigEntry | None = None
        # Carries the phone number across phone → phone_code steps.
        self._pending_phone: str | None = None

    def _abort_if_unique_id_mismatch(self) -> None:
        """Abort if unique_id doesn't match the entry being reauthenticated.

        Added in HA 2024.x; shim here so older versions stay compatible.
        """
        try:
            super()._abort_if_unique_id_mismatch()  # type: ignore[misc]
        except AttributeError:
            pass

    @staticmethod
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AtmeexOptionsFlowHandler:
        """Return the options flow handler."""
        return AtmeexOptionsFlowHandler()

    # ---------- step routing ----------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry point: pick auth method (email vs phone)."""
        return self.async_show_menu(
            step_id="user",
            menu_options=[AUTH_METHOD_EMAIL, AUTH_METHOD_PHONE],
        )

    # ---------- email path ----------

    async def async_step_email(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Email/password sign-in for initial setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = _clean_email(user_input[CONF_EMAIL])
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.login(email, password)
                await api.get_devices()

                await self.async_set_unique_id(_email_unique_id(email))
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=email,
                    data={
                        CONF_AUTH_METHOD: AUTH_METHOD_EMAIL,
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    },
                )

            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = "invalid_auth" if status in (401, 403) else "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error during Atmeex email config flow: %s", err
                )
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="email",
            data_schema=DATA_SCHEMA_EMAIL,
            errors=errors,
        )

    # ---------- phone path ----------

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1 of phone flow: collect phone, request SMS code."""
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = _clean_phone(user_input[CONF_PHONE])
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.request_sms_code(phone)
            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = (
                    "invalid_auth" if status in (401, 403) else "cannot_connect"
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error requesting SMS code: %s", err
                )
                errors["base"] = "unknown"
            else:
                self._pending_phone = phone
                return await self.async_step_phone_code()

        suggested_phone = self._pending_phone or (
            _clean_phone(user_input[CONF_PHONE]) if user_input else ""
        )
        schema = vol.Schema(
            {vol.Required(CONF_PHONE, default=suggested_phone): str}
        )
        return self.async_show_form(
            step_id="phone",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_phone_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2 of phone flow: verify SMS code, create or update entry."""
        errors: dict[str, str] = {}

        if not self._pending_phone:
            # Defensive: should never happen — phone step always sets this
            # before advancing here.
            return self.async_abort(reason="phone_state_lost")

        if user_input is not None:
            code = str(user_input[CONF_PHONE_CODE]).strip()
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.login_phone(self._pending_phone, code)
                await api.get_devices()

                entry_data: dict[str, Any] = {
                    CONF_AUTH_METHOD: AUTH_METHOD_PHONE,
                    CONF_PHONE: self._pending_phone,
                }
                # Only the refresh_token survives between sessions for phone
                # accounts — there are no replayable creds to fall back on.
                if api.refresh_token:
                    entry_data["refresh_token"] = api.refresh_token

                if self._reauth_entry:
                    await self.async_set_unique_id(
                        _phone_unique_id(self._pending_phone)
                    )
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates=entry_data,
                        reason="reauth_successful",
                    )

                await self.async_set_unique_id(
                    _phone_unique_id(self._pending_phone)
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._pending_phone,
                    data=entry_data,
                )

            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = (
                    "invalid_auth" if status in (401, 403) else "cannot_connect"
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error verifying SMS code: %s", err
                )
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="phone_code",
            data_schema=DATA_SCHEMA_PHONE_CODE,
            errors=errors,
            description_placeholders={"phone": self._pending_phone},
        )

    # ---------- reauth ----------

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
        """Branch reauth based on the entry's stored auth method."""
        if self._reauth_entry is not None:
            auth_method = self._reauth_entry.data.get(
                CONF_AUTH_METHOD, AUTH_METHOD_EMAIL
            )
            if auth_method == AUTH_METHOD_PHONE:
                return await self.async_step_reauth_phone_confirm(user_input)

        return await self._async_step_reauth_email(user_input)

    async def _async_step_reauth_email(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reauth for email accounts — same form as initial email setup."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = _clean_email(user_input[CONF_EMAIL])
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.login(email, password)
                await api.get_devices()

                if self._reauth_entry:
                    await self.async_set_unique_id(_email_unique_id(email))
                    self._abort_if_unique_id_mismatch()

                    return self.async_update_reload_and_abort(
                        self._reauth_entry,
                        data_updates={
                            CONF_AUTH_METHOD: AUTH_METHOD_EMAIL,
                            CONF_EMAIL: email,
                            CONF_PASSWORD: password,
                        },
                        reason="reauth_successful",
                    )

                return self.async_abort(reason="reauth_successful")

            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = "invalid_auth" if status in (401, 403) else "cannot_connect"
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error during Atmeex reauth flow: %s", err
                )
                errors["base"] = "unknown"

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

    async def async_step_reauth_phone_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Phone reauth: explicit confirm before re-sending SMS.

        Avoids surprise SMS messages when reauth fires unexpectedly during a
        transient outage.
        """
        errors: dict[str, str] = {}
        phone = (
            self._reauth_entry.data.get(CONF_PHONE, "")
            if self._reauth_entry
            else ""
        )

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = AtmeexApi(session)

            if hasattr(api, "async_init"):
                await api.async_init()

            try:
                await api.request_sms_code(phone)
            except ApiError as err:
                status = getattr(err, "status", None)
                errors["base"] = (
                    "invalid_auth" if status in (401, 403) else "cannot_connect"
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception(
                    "Unexpected error sending reauth SMS: %s", err
                )
                errors["base"] = "unknown"
            else:
                self._pending_phone = phone
                return await self.async_step_phone_code()

        return self.async_show_form(
            step_id="reauth_phone_confirm",
            data_schema=DATA_SCHEMA_REAUTH_PHONE_CONFIRM,
            errors=errors,
            description_placeholders={"phone": phone},
        )


class AtmeexOptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow handler for Atmeex Cloud.

    Modern HA provides ``self.config_entry`` automatically — no need to
    accept it in ``__init__``.
    """

    @property
    def config_entry(self) -> ConfigEntry:
        """Return the config entry. Shim for HA versions prior to 2024.x."""
        return self._config_entry  # type: ignore[attr-defined]

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            try:
                interval = int(user_input[CONF_UPDATE_INTERVAL])
            except (TypeError, ValueError):
                interval = DEFAULT_UPDATE_INTERVAL
            interval = max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, interval))
            enable_ws = user_input.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)
            enable_co2 = user_input.get(CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2)

            return self.async_create_entry(
                title="",
                data={
                    CONF_UPDATE_INTERVAL: interval,
                    CONF_ENABLE_WEBSOCKET: enable_ws,
                    CONF_ENABLE_CO2: enable_co2,
                },
            )

        options = self.config_entry.options or {}
        current_interval = options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)
        current_ws = options.get(CONF_ENABLE_WEBSOCKET, DEFAULT_ENABLE_WEBSOCKET)
        current_co2 = options.get(CONF_ENABLE_CO2, DEFAULT_ENABLE_CO2)

        schema = vol.Schema(
            {
                vol.Optional(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int),
                    vol.Clamp(min=MIN_UPDATE_INTERVAL, max=MAX_UPDATE_INTERVAL),
                ),
                vol.Optional(CONF_ENABLE_WEBSOCKET, default=current_ws): bool,
                vol.Optional(CONF_ENABLE_CO2, default=current_co2): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
