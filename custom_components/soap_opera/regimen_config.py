"""Regimen config subentries: their stored data format, and syncing them into
the database.

A regimen subentry's data is how the regimen was collected, not the record
of it. async_setup_entry calls sync_entry_regimens every time it runs, and
the update listener reloads the entry whenever a subentry is added, edited
or removed, so each of those reaches the database through this one path:

- added:   the medication (if defined inline) and the regimen are inserted.
- edited:  a dosing change supersedes at the effective date the flow asked
           for; a non-dosing change updates the current row in place.
- removed: the regimen is end-dated as of today. Nothing is deleted.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import date
from typing import Any

import sqlalchemy as sa

from .storage import regimens
from .storage.database import subjects_table
from .storage.regimens import REGIMEN_FIELDS, RegimenSpec

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_REGIMEN = "regimen"

# Subentry data keys beyond the regimen's own columns (which are stored
# under their column names; see regimens.REGIMEN_FIELDS).
DATA_MEDICATION_ID = "medication_id"
DATA_NEW_MEDICATION = "new_medication"
DATA_START_DATE = "start_date"
DATA_END_DATE = "end_date"
DATA_EFFECTIVE_DATE = "effective_date"


def _date_or_none(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def spec_from_subentry_data(data: Mapping[str, Any]) -> RegimenSpec:
    start_date = date.fromisoformat(data[DATA_START_DATE])
    medication_id = data.get(DATA_MEDICATION_ID)
    return RegimenSpec(
        fields={name: data.get(name) for name in REGIMEN_FIELDS},
        start_date=start_date,
        end_date=_date_or_none(data.get(DATA_END_DATE)),
        effective_date=_date_or_none(data.get(DATA_EFFECTIVE_DATE)) or start_date,
        medication_id=uuid.UUID(medication_id) if medication_id else None,
        new_medication=data.get(DATA_NEW_MEDICATION),
    )


def sync_entry_regimens(
    engine: sa.engine.Engine,
    subject_id: uuid.UUID,
    subentries: Mapping[str, Mapping[str, Any]],
    today: date,
) -> dict[str, str]:
    """Sync every regimen subentry of one animal, then end-date regimens
    whose subentry is gone. Blocking.

    Each subentry syncs in its own transaction, so one that can't be applied
    (an overlap, say) is logged and skipped without blocking the others or
    failing setup for the whole animal. Returns {subentry_id: error}.
    """
    errors: dict[str, str] = {}
    for subentry_id, data in subentries.items():
        try:
            with engine.begin() as conn:
                regimens.sync_regimen(
                    conn,
                    subject_id=subject_id,
                    config_subentry_id=subentry_id,
                    spec=spec_from_subentry_data(data),
                )
        except (ValueError, LookupError, sa.exc.IntegrityError) as err:
            _LOGGER.error(
                "Could not sync regimen subentry %s to the database: %s",
                subentry_id,
                err,
            )
            errors[subentry_id] = str(err)

    with engine.begin() as conn:
        ended = regimens.end_regimens_for_removed_subentries(
            conn,
            subject_id=subject_id,
            current_subentry_ids=set(subentries),
            today=today,
        )
    for regimen_id in ended:
        _LOGGER.info("Ended regimen %s: its subentry was removed", regimen_id)
    return errors


def subject_id_for_entry(engine: sa.engine.Engine, entry_id: str) -> uuid.UUID | None:
    """Blocking."""
    with engine.connect() as conn:
        return conn.execute(
            sa.select(subjects_table.c.id).where(
                subjects_table.c.config_entry_id == entry_id
            )
        ).scalar_one_or_none()


def medication_label(medication: Mapping[str, Any]) -> str:
    """e.g. "gabapentin 100 mg capsule", "meloxicam 1.5 mg/mL liquid"."""
    name = medication["generic_name"]
    if medication.get("brand_name"):
        name = f"{name} ({medication['brand_name']})"
    if medication["form"] == regimens.MedicationForm.LIQUID:
        amount = f"{medication['concentration']:g} {medication['unit']}/mL"
    else:
        amount = f"{medication['strength']:g} {medication['unit']}"
    return f"{name} {amount} {medication['form']}"


def list_medications(engine: sa.engine.Engine) -> list[dict[str, Any]]:
    """Blocking."""
    with engine.connect() as conn:
        return [row._asdict() for row in regimens.list_medications(conn)]


def would_overlap(
    engine: sa.engine.Engine,
    *,
    entry_id: str,
    medication_id: uuid.UUID,
    start_date: date,
    end_date: date | None,
    ignore_subentry_id: str | None = None,
) -> bool:
    """Whether a regimen over [start_date, end_date) would clash with one
    already recorded for this animal and medication, ignoring the chain of
    the subentry being edited. Blocking."""
    subject_id = subject_id_for_entry(engine, entry_id)
    if subject_id is None:
        return False
    with engine.connect() as conn:
        return regimens.find_overlap(
            conn,
            subject_id=subject_id,
            medication_id=medication_id,
            start_date=start_date,
            end_date=end_date,
            exclude_config_subentry_id=ignore_subentry_id,
        )


def medication_id_of_subentry(
    engine: sa.engine.Engine, subentry_id: str
) -> uuid.UUID | None:
    """The medication a subentry's regimen was recorded against. Blocking."""
    with engine.connect() as conn:
        head = regimens.chain_head(conn, subentry_id)
    return None if head is None else head.medication_id
