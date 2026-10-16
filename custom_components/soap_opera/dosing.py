"""The dose-logging funnel's Home Assistant half.

async_log_dose is the one entry point for recording a dose, whatever
captured it: the log_dose service, the log-dose button, and later NFC and
actionable notifications. It resolves the regimen and the person, hands off
to storage.administrations.log_dose (idempotency, guardrails, persistence),
then writes the audit log, fires soap_opera_dose_logged, and notifies the
regimen's event entity. Nothing else should write an administration.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from functools import partial
from typing import Any

import sqlalchemy as sa
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EVENT_DOSE_LOGGED,
    IDEMPOTENCY_BUCKET_SECONDS,
    SIGNAL_DOSE_LOGGED,
)
from .storage.administrations import (
    AdministrationStatus,
    DoseRequest,
    DoseResult,
    SystemOnlyStatusError,
    log_dose,
)
from .storage.regimens import regimen_in_force_for_subentry


def resolve_regimen(hass: HomeAssistant, regimen: str) -> tuple[str, str]:
    """Map a regimen reference to (config_entry_id, config_subentry_id).

    Accepts a regimen subentry id -- the stable handle across dose changes
    -- or the entity id of one of the regimen's entities, which is what an
    automation author can actually see.
    """
    subentry_id = regimen
    registry_entry = er.async_get(hass).async_get(regimen)
    if registry_entry is not None and registry_entry.platform == DOMAIN:
        subentry_id = registry_entry.config_subentry_id or ""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if subentry_id in entry.subentries:
            return entry.entry_id, subentry_id
    raise ServiceValidationError(f"No loaded SOAP Opera regimen matches {regimen!r}")


def _in_force_id(
    engine: sa.engine.Engine, subentry_id: str, on: Any
) -> uuid.UUID | None:
    with engine.connect() as conn:
        row = regimen_in_force_for_subentry(conn, subentry_id, on)
    return None if row is None else row.id


async def async_log_dose(
    hass: HomeAssistant,
    *,
    entry_id: str,
    subentry_id: str,
    capture_method: str,
    context: Context,
    status: AdministrationStatus = AdministrationStatus.GIVEN,
    dose_units: float | None = None,
    administered_at: datetime | None = None,
    administered_by: str | None = None,
    note: str | None = None,
    idempotency_key: str | None = None,
    override_guardrails: bool = False,
) -> DoseResult:
    shared = hass.data.get(DOMAIN, {}).get("shared")
    if shared is None:
        raise ServiceValidationError("SOAP Opera is not loaded")
    engine = shared.database.engine

    if administered_at is None:
        administered_at = dt_util.utcnow()
    elif administered_at.tzinfo is None:
        administered_at = administered_at.replace(
            tzinfo=dt_util.get_default_time_zone()
        )

    # Attribution is automatic: whoever made the call, unless the caller
    # says otherwise (a sitter without an account, say).
    user_id = context.user_id
    if administered_by is None and user_id is not None:
        user = await hass.auth.async_get_user(user_id)
        administered_by = user.name if user is not None else None

    # Which version of the regimen applied is decided by the date the dose
    # was given, so a backfilled dose lands on the dose in force back then.
    on = dt_util.as_local(administered_at).date()
    regimen_id = await hass.async_add_executor_job(
        _in_force_id, engine, subentry_id, on
    )
    if regimen_id is None:
        raise ServiceValidationError(
            f"This regimen was not in force on {on.isoformat()}"
        )

    request = DoseRequest(
        regimen_id=regimen_id,
        administered_at=administered_at,
        capture_method=capture_method,
        status=status,
        dose_units=dose_units,
        administered_by=administered_by,
        note=note,
        idempotency_key=idempotency_key,
        # Per regimen and per person: two people logging the same dose a
        # minute apart may each really have given it, which the interval
        # guardrail should flag rather than the key silently merge.
        source_ref=f"{subentry_id}:{user_id or administered_by or ''}",
        override_guardrails=override_guardrails,
    )
    try:
        result = await hass.async_add_executor_job(
            partial(
                log_dose, engine, request, bucket_seconds=IDEMPOTENCY_BUCKET_SECONDS
            )
        )
    except (SystemOnlyStatusError, ValueError, LookupError) as err:
        raise ServiceValidationError(str(err)) from err

    summary = {
        **result.record,
        "administration_id": str(result.administration_id),
        "config_entry_id": entry_id,
        "config_subentry_id": subentry_id,
        "duplicate": result.duplicate,
        "guardrails": [outcome.as_dict() for outcome in result.guardrails],
    }
    # The JSONL backstop. After the database commit, never instead of it;
    # AuditLog logs rather than raises if the write fails.
    await hass.async_add_executor_job(
        shared.audit_log.capture_event,
        "dose_duplicate" if result.duplicate else "dose_logged",
        summary,
    )
    if not result.duplicate:
        hass.bus.async_fire(EVENT_DOSE_LOGGED, summary, context=context)
        async_dispatcher_send(hass, SIGNAL_DOSE_LOGGED.format(subentry_id), result)
    return result


def response_data(result: DoseResult) -> dict[str, Any]:
    return {
        "administration_id": str(result.administration_id),
        "duplicate": result.duplicate,
        "guardrails": [outcome.as_dict() for outcome in result.guardrails],
    }
