"""Config flow for Hik-Connect Local."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_ACCOUNT,
    CONF_BASE_URL,
    CONF_PASSWORD,
    CONF_SERVER,
    DEFAULT_BASE_URL,
    DOMAIN,
    SERVER_CUSTOM,
    SERVERS,
)
from .hikconnect_api import HikConnectAuthError, HikConnectClient


def _resolve_base_url(user_input: dict[str, Any]) -> str | None:
    """Map the selected server (or custom override) to a base URL."""
    server = user_input.get(CONF_SERVER, DEFAULT_BASE_URL)
    if server != SERVER_CUSTOM:
        return server
    custom = (user_input.get(CONF_BASE_URL) or "").strip().rstrip("/")
    if not custom:
        return None
    if not custom.startswith(("http://", "https://")):
        custom = f"https://{custom}"
    return custom


def _server_defaults(base_url: str) -> tuple[str, str]:
    """(server-select default, custom-url default) for a stored base URL."""
    if base_url in SERVERS:
        return base_url, ""
    return SERVER_CUSTOM, base_url or ""


def _schema(account: str, server: str, base_url: str, password_optional: bool) -> vol.Schema:
    options = [SelectOptionDict(value=u, label=l) for u, l in SERVERS.items()]
    options.append(SelectOptionDict(value=SERVER_CUSTOM, label="Custom…"))
    password = (
        vol.Optional(CONF_PASSWORD) if password_optional else vol.Required(CONF_PASSWORD)
    )
    return vol.Schema(
        {
            vol.Required(CONF_ACCOUNT, default=account): str,
            password: str,
            vol.Required(CONF_SERVER, default=server): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            ),
            vol.Optional(CONF_BASE_URL, default=base_url): str,
        }
    )


class HikConnectLocalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Hik-Connect account login."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _resolve_base_url(user_input)
            if base_url is None:
                errors["base"] = "custom_url_required"
            else:
                client = HikConnectClient(
                    user_input[CONF_ACCOUNT], user_input[CONF_PASSWORD], base_url
                )
                try:
                    await self.hass.async_add_executor_job(client.login)
                except HikConnectAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_input[CONF_ACCOUNT].lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input[CONF_ACCOUNT],
                        data={
                            CONF_ACCOUNT: user_input[CONF_ACCOUNT],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_BASE_URL: base_url,
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=_schema("", DEFAULT_BASE_URL, "", password_optional=False),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change server/account/password without removing the integration."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            base_url = _resolve_base_url(user_input)
            if base_url is None:
                errors["base"] = "custom_url_required"
            else:
                # blank password keeps the existing one
                password = user_input.get(CONF_PASSWORD) or entry.data[CONF_PASSWORD]
                client = HikConnectClient(
                    user_input[CONF_ACCOUNT], password, base_url
                )
                try:
                    await self.hass.async_add_executor_job(client.login)
                except HikConnectAuthError:
                    errors["base"] = "invalid_auth"
                except Exception:  # noqa: BLE001
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_input[CONF_ACCOUNT].lower())
                    self._abort_if_unique_id_mismatch(reason="account_mismatch")
                    return self.async_update_reload_and_abort(
                        entry,
                        data={
                            CONF_ACCOUNT: user_input[CONF_ACCOUNT],
                            CONF_PASSWORD: password,
                            CONF_BASE_URL: base_url,
                        },
                    )
        server, base_url = _server_defaults(
            entry.data.get(CONF_BASE_URL, DEFAULT_BASE_URL)
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(
                entry.data.get(CONF_ACCOUNT, ""), server, base_url,
                password_optional=True,
            ),
            errors=errors,
        )
