"""Integration-level services: log_dose and export."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SERVICE_EXPORT, SERVICE_LOG_DOSE
from .dosing import async_log_dose, resolve_regimen, response_data
from .storage.administrations import CALLER_STATUSES, AdministrationStatus
from .storage.export import ExportDestinationExistsError, ExportFormat, export_database

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


EXPORT_SCHEMA = vol.Schema(
    {
        vol.Optional("format", default=ExportFormat.JSON.value): vol.In(
            [fmt.value for fmt in ExportFormat]
        ),
        vol.Optional("path"): cv.string,
    }
)

# Default export location, relative to the config directory; each export
# gets its own timestamped directory inside it.
DEFAULT_EXPORT_DIR = "soap_opera_exports"


def _export_destination(hass: HomeAssistant, path: str | None) -> Path:
    """Resolve the requested path, which must stay inside the config
    directory."""
    config_dir = Path(hass.config.config_dir).resolve()
    if path is None:
        stamp = dt_util.utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = f"{DEFAULT_EXPORT_DIR}/{stamp}"
    destination = (config_dir / path).resolve()
    if not destination.is_relative_to(config_dir) or destination == config_dir:
        raise ServiceValidationError(
            f"Export path must be a new directory inside {config_dir}"
        )
    return destination


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

    async def export(call: ServiceCall) -> ServiceResponse:
        shared = hass.data.get(DOMAIN, {}).get("shared")
        if shared is None:
            raise ServiceValidationError("SOAP Opera is not loaded")
        destination = _export_destination(hass, call.data.get("path"))
        try:
            counts = await hass.async_add_executor_job(
                export_database,
                shared.database.engine,
                destination,
                ExportFormat(call.data["format"]),
            )
        except ExportDestinationExistsError as err:
            raise ServiceValidationError(str(err)) from err
        await hass.async_add_executor_job(
            shared.audit_log.capture_event,
            "export",
            {"path": str(destination), "format": call.data["format"], **counts},
        )
        row_counts: dict[str, Any] = dict(counts)
        return {"path": str(destination), "row_counts": row_counts}

    # Admin-only: it writes the whole medical record to disk.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_EXPORT,
        export,
        schema=EXPORT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LOG_DOSE,
        log_dose,
        schema=LOG_DOSE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
