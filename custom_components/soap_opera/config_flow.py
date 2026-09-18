"""Config flow for SOAP Opera.

One config entry per animal. Adding a second animal means adding a second
config entry; regimens will later be config subentries of an animal's entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_NAME

from .const import CONF_BREED, CONF_DATE_OF_BIRTH, CONF_SPECIES, DOMAIN

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_SPECIES): str,
        vol.Optional(CONF_BREED): str,
        vol.Optional(CONF_DATE_OF_BIRTH): str,
    }
)


class SoapOperaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SOAP Opera."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect an animal's name, species, and optional breed / date of birth."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            user_input[CONF_NAME] = name

            existing_names = {
                entry.data[CONF_NAME].casefold()
                for entry in self._async_current_entries()
            }
            if not name:
                errors["base"] = "name_required"
            elif name.casefold() in existing_names:
                errors["base"] = "name_exists"
            else:
                return self.async_create_entry(title=name, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )
