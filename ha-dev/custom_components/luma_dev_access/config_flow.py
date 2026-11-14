"""Set up Luma dev access: where the TV is, which phone approves, what to hand out.

Finishing shows the dev token (once) and Home Assistant's management public key, which the
owner appends to the TV's /home/root/.ssh/authorized_keys.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.storage import Store

from . import keys, tv
from .const import (
    CONF_CF_CLIENT_ID,
    CONF_CF_CLIENT_SECRET,
    CONF_FORGET_HOST_KEY,
    CONF_MANAGEMENT_KEY,
    CONF_NOTIFY_SERVICE,
    CONF_ROTATE_TOKEN,
    CONF_TOKEN_HASH,
    CONF_TUNNEL_HOSTNAME,
    CONF_TV_HOST,
    CONF_TV_PORT,
    DEFAULT_TUNNEL_HOSTNAME,
    DEFAULT_TV_PORT,
    DOMAIN,
)


def _phones(hass: HomeAssistant) -> list[str]:
    return sorted(
        f"notify.{name}"
        for name in hass.services.async_services_for_domain("notify")
        if name.startswith("mobile_app_")
    )


def _schema(hass: HomeAssistant, current: dict[str, Any], *, options: bool) -> vol.Schema:
    phones = _phones(hass)
    chosen = current.get(CONF_NOTIFY_SERVICE)
    if chosen and chosen not in phones:
        phones.append(chosen)
    secret = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
    fields: dict[Any, Any] = {
        vol.Required(CONF_TV_HOST, default=current.get(CONF_TV_HOST, "")): str,
        vol.Required(
            CONF_TV_PORT, default=current.get(CONF_TV_PORT, DEFAULT_TV_PORT)
        ): NumberSelector(NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)),
        vol.Required(
            CONF_TUNNEL_HOSTNAME,
            default=current.get(CONF_TUNNEL_HOSTNAME, DEFAULT_TUNNEL_HOSTNAME),
        ): str,
        vol.Required(CONF_NOTIFY_SERVICE, default=chosen or vol.UNDEFINED): SelectSelector(
            SelectSelectorConfig(
                options=phones, mode=SelectSelectorMode.DROPDOWN, custom_value=True
            )
        ),
        vol.Optional(
            CONF_CF_CLIENT_ID,
            description={"suggested_value": current.get(CONF_CF_CLIENT_ID)},
        ): str,
        vol.Optional(
            CONF_CF_CLIENT_SECRET,
            description={"suggested_value": current.get(CONF_CF_CLIENT_SECRET)},
        ): secret,
    }
    if options:
        fields[vol.Optional(CONF_ROTATE_TOKEN, default=False)] = BooleanSelector()
        fields[vol.Optional(CONF_FORGET_HOST_KEY, default=False)] = BooleanSelector()
    return vol.Schema(fields)


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_TV_HOST: user_input[CONF_TV_HOST].strip(),
        CONF_TV_PORT: int(user_input[CONF_TV_PORT]),
        CONF_TUNNEL_HOSTNAME: user_input[CONF_TUNNEL_HOSTNAME].strip(),
        CONF_NOTIFY_SERVICE: user_input[CONF_NOTIFY_SERVICE].strip(),
        CONF_CF_CLIENT_ID: (user_input.get(CONF_CF_CLIENT_ID) or "").strip(),
        CONF_CF_CLIENT_SECRET: (user_input.get(CONF_CF_CLIENT_SECRET) or "").strip(),
    }


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not user_input[CONF_NOTIFY_SERVICE].startswith("notify.mobile_app_"):
        errors[CONF_NOTIFY_SERVICE] = "not_a_phone"
    if bool(user_input.get(CONF_CF_CLIENT_ID)) != bool(user_input.get(CONF_CF_CLIENT_SECRET)):
        errors["base"] = "cloudflare_pair"
    return errors


def authorized_keys_line(management_key: str) -> str:
    algorithm, blob = tv.public_key_of(management_key).split()[:2]
    return f"{keys.OPTIONS} {algorithm} {blob} luma-dev-manager"


class LumaDevAccessConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._token = ""
        self._management_key = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> LumaDevAccessOptionsFlow:
        return LumaDevAccessOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                self._options = _clean(user_input)
                self._token = keys.new_token()
                generated = await self.hass.async_add_executor_job(
                    tv.generate_key, "luma-dev-manager"
                )
                self._management_key = generated.private_key
                return await self.async_step_finish()
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(self.hass, user_input or {}, options=False),
            errors=errors,
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                title="Luma dev access",
                data={
                    CONF_MANAGEMENT_KEY: self._management_key,
                    CONF_TOKEN_HASH: keys.token_hash(self._token),
                },
                options=self._options,
            )
        line = await self.hass.async_add_executor_job(authorized_keys_line, self._management_key)
        return self.async_show_form(
            step_id="finish",
            description_placeholders={"token": self._token, "authorized_keys_line": line},
        )


class LumaDevAccessOptionsFlow(OptionsFlow):
    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._token = ""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                self._options = _clean(user_input)
                if user_input.get(CONF_FORGET_HOST_KEY):
                    store: Store[dict[str, Any]] = Store(self.hass, 1, f"{DOMAIN}.{entry.entry_id}")
                    stored = await store.async_load() or {}
                    stored["tv_host_key"] = None
                    await store.async_save(stored)
                if user_input.get(CONF_ROTATE_TOKEN):
                    self._token = keys.new_token()
                    self.hass.config_entries.async_update_entry(
                        entry, data={**entry.data, CONF_TOKEN_HASH: keys.token_hash(self._token)}
                    )
                    return await self.async_step_new_token()
                return self.async_create_entry(data=self._options)
        line = await self.hass.async_add_executor_job(
            authorized_keys_line, entry.data[CONF_MANAGEMENT_KEY]
        )
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(self.hass, user_input or dict(entry.options), options=True),
            errors=errors,
            description_placeholders={"authorized_keys_line": line},
        )

    async def async_step_new_token(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=self._options)
        return self.async_show_form(
            step_id="new_token", description_placeholders={"token": self._token}
        )
