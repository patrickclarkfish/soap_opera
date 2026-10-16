"""Integration-level services."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SERVICE_LOG_DOSE
from .dosing import async_log_dose, resolve_regimen, response_data
from .storage.administrations import CALLER_STATUSES, AdministrationStatus

# given_late and missed are absent from the accepted statuses on purpose:
# the system computes them from a schedule, a caller never asserts them.
LOG_DOSE_SCHEMA = vol.Schema(
    {
        vol.Required("regimen"): cv.string,
        vol.Optional("dose_units"): vol.All(
            vol.Coerce(float), vol.Range(min=0, min_included=False)
        ),
        vol.Optional("status", default=AdministrationStatus.GIVEN.value): vol.In(
            [status.value for status in CALLER_STATUSES]
        ),
        vol.Optional("administered_at"): cv.datetime,
        vol.Optional("administered_by"): cv.string,
        vol.Optional("note"): cv.string,
        vol.Optional("idempotency_key"): cv.string,
        vol.Optional("override_guardrails", default=False): cv.boolean,
    }
)


@callback
def async_register_services(hass: HomeAssistant) -> None:
    async def log_dose(call: ServiceCall) -> ServiceResponse:
        entry_id, subentry_id = resolve_regimen(hass, call.data["regimen"])
        result = await async_log_dose(
            hass,
            entry_id=entry_id,
            subentry_id=subentry_id,
            capture_method="service",
            context=call.context,
            status=AdministrationStatus(call.data["status"]),
            dose_units=call.data.get("dose_units"),
            administered_at=call.data.get("administered_at"),
            administered_by=call.data.get("administered_by"),
            note=call.data.get("note"),
            idempotency_key=call.data.get("idempotency_key"),
            override_guardrails=call.data["override_guardrails"],
        )
        return response_data(result)

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_DOSE,
        log_dose,
        schema=LOG_DOSE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
