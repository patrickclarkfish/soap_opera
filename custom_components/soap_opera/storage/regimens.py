"""Medication and regimen persistence, including the regimen versioning rule.

A regimen row is one version of a prescription. A dosing change never
UPDATEs it: supersede_regimen end-dates the current row and INSERTs a new one
whose supersedes_id points back at it, so the full dosing history stays
queryable ("did the increase from 60mg to 90mg change anything?").

Regimens are effective-dated, not flagged active. A regimen is in force on
date d when start_date <= d and (end_date is null or d < end_date) -- a
half-open range, so a row ending on d and its successor starting on d never
both apply. A future-dated change is simply a row whose start_date has not
arrived yet.

At most one regimen may be in force per (subject, medication) on any date.
SQLite cannot express non-overlapping ranges as a constraint, so
insert_regimen checks it. The check is a SELECT followed by an INSERT, which
is only safe because every write transaction here starts with BEGIN
IMMEDIATE (see database._sqlite_begin_explicit): a second writer waits for
the first to commit instead of both passing the check.

Every function takes a Connection and leaves transaction control to the
caller, so a supersede (end-date plus insert) commits or rolls back as one
unit. All of them are blocking.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Row

from .database import medication_table, regimen_table


class MedicationForm(StrEnum):
    TABLET = "tablet"
    CAPSULE = "capsule"
    LIQUID = "liquid"
    TRANSDERMAL = "transdermal"


class RegimenOverlapError(ValueError):
    """The new regimen would be in force alongside another one for the same
    subject and medication on at least one date."""


class RegimenNotSupersedableError(ValueError):
    """The regimen can't be superseded as asked: it has already ended, or
    the effective date is before it starts."""


# Fields a caller may set on a regimen. subject_id, medication_id,
# config_subentry_id, the dates and supersedes_id are structural, handled
# explicitly by insert_regimen / supersede_regimen.
REGIMEN_FIELDS = frozenset(
    {
        "dose_units",
        "unit_strength",
        "frequency",
        "scheduled_times",
        "prn",
        "min_interval_seconds",
        "dose_variable",
        "dose_increment",
        "dose_max_single",
        "dose_max_daily",
        "indication",
        "prescribing_vet",
    }
)

_REGIMEN_DEFAULTS: dict[str, Any] = {
    "frequency": None,
    "scheduled_times": None,
    "prn": False,
    "min_interval_seconds": None,
    "dose_variable": False,
    "dose_increment": None,
    "dose_max_single": None,
    "dose_max_daily": None,
    "indication": None,
    "prescribing_vet": None,
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def insert_medication(
    conn: Connection,
    *,
    generic_name: str,
    unit: str,
    form: MedicationForm | str,
    route: str,
    brand_name: str | None = None,
    strength: float | None = None,
    concentration: float | None = None,
    requires_refrigeration: bool = False,
    shake_well: bool = False,
    with_food: bool | None = None,
) -> uuid.UUID:
    """Insert a medication and return its id."""
    form = MedicationForm(form)
    if not generic_name.strip():
        raise ValueError("generic_name is required")
    if form is MedicationForm.LIQUID:
        if concentration is None or concentration <= 0:
            raise ValueError("a liquid medication needs a positive concentration")
    elif strength is None or strength <= 0:
        raise ValueError(f"a {form} medication needs a positive strength")

    medication_id = uuid.uuid7()
    conn.execute(
        medication_table.insert().values(
            id=medication_id,
            generic_name=generic_name.strip(),
            brand_name=brand_name,
            strength=strength,
            unit=unit,
            form=str(form),
            concentration=concentration,
            route=route,
            requires_refrigeration=requires_refrigeration,
            shake_well=shake_well,
            with_food=with_food,
            created_at=_now(),
        )
    )
    return medication_id


def _validate_regimen_fields(fields: dict[str, Any]) -> None:
    unknown = set(fields) - REGIMEN_FIELDS
    if unknown:
        raise ValueError(f"not regimen fields: {sorted(unknown)}")
    for name in ("dose_units", "unit_strength"):
        if name in fields and not fields[name] > 0:
            raise ValueError(f"{name} must be positive")
    for name in ("dose_increment", "dose_max_single", "dose_max_daily"):
        if fields.get(name) is not None and not fields[name] > 0:
            raise ValueError(f"{name} must be positive when set")
    interval = fields.get("min_interval_seconds")
    if interval is not None and interval < 0:
        raise ValueError("min_interval_seconds cannot be negative")


def _check_no_overlap(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    medication_id: uuid.UUID,
    start_date: date,
    end_date: date | None,
) -> None:
    if end_date is not None and end_date == start_date:
        return  # empty range: never in force, can't overlap anything
    r = regimen_table.c
    # Ranges [a, b) and [c, d) overlap iff a < d and c < b, with a null end
    # meaning unbounded. Existing empty ranges (start == end) are skipped.
    query = sa.select(r.id).where(
        r.subject_id == subject_id,
        r.medication_id == medication_id,
        sa.or_(r.end_date.is_(None), r.start_date < r.end_date),
        sa.or_(r.end_date.is_(None), r.end_date > start_date),
    )
    if end_date is not None:
        query = query.where(r.start_date < end_date)
    clash = conn.execute(query.limit(1)).first()
    if clash is not None:
        raise RegimenOverlapError(
            f"regimen {clash.id} for this subject and medication is already "
            f"in force during [{start_date}, {end_date or 'open'})"
        )


def insert_regimen(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    medication_id: uuid.UUID,
    start_date: date,
    end_date: date | None = None,
    config_subentry_id: str | None = None,
    supersedes_id: uuid.UUID | None = None,
    **fields: Any,
) -> uuid.UUID:
    """Insert one regimen row, refusing any overlap with an existing one.

    `fields` takes REGIMEN_FIELDS; dose_units and unit_strength are required.
    """
    _validate_regimen_fields(fields)
    for name in ("dose_units", "unit_strength"):
        if name not in fields:
            raise ValueError(f"{name} is required")
    if end_date is not None and end_date < start_date:
        raise ValueError("end_date is before start_date")
    _check_no_overlap(
        conn,
        subject_id=subject_id,
        medication_id=medication_id,
        start_date=start_date,
        end_date=end_date,
    )

    regimen_id = uuid.uuid7()
    conn.execute(
        regimen_table.insert().values(
            {
                **_REGIMEN_DEFAULTS,
                **fields,
                "id": regimen_id,
                "subject_id": subject_id,
                "medication_id": medication_id,
                "config_subentry_id": config_subentry_id,
                "start_date": start_date,
                "end_date": end_date,
                "supersedes_id": supersedes_id,
                "created_at": _now(),
            }
        )
    )
    return regimen_id


def get_regimen(conn: Connection, regimen_id: uuid.UUID) -> Row[Any] | None:
    return conn.execute(
        sa.select(regimen_table).where(regimen_table.c.id == regimen_id)
    ).first()


def supersede_regimen(
    conn: Connection,
    current_id: uuid.UUID,
    *,
    effective: date,
    end_date: date | None = None,
    **changes: Any,
) -> uuid.UUID:
    """End-date the current regimen at `effective` and insert its successor.

    The successor copies every field of the current row, then applies
    `changes` (REGIMEN_FIELDS only). It is in force from `effective` until
    `end_date` (default: open-ended). Returns the new row's id.

    Only the head of a chain -- a row with no end_date -- can be superseded,
    and not with an effective date before it starts. An effective date equal
    to its start_date is allowed: the current row is left with an empty
    range, i.e. it never came into force, which is the honest record of a
    prescription changed before its first day.
    """
    _validate_regimen_fields(changes)
    current = get_regimen(conn, current_id)
    if current is None:
        raise LookupError(f"no regimen {current_id}")
    if current.end_date is not None:
        raise RegimenNotSupersedableError(
            f"regimen {current_id} already ended on {current.end_date}"
        )
    if effective < current.start_date:
        raise RegimenNotSupersedableError(
            f"effective date {effective} is before regimen {current_id} "
            f"starts on {current.start_date}"
        )

    conn.execute(
        regimen_table.update()
        .where(regimen_table.c.id == current_id)
        .values(end_date=effective)
    )
    carried = {name: getattr(current, name) for name in REGIMEN_FIELDS}
    return insert_regimen(
        conn,
        subject_id=current.subject_id,
        medication_id=current.medication_id,
        config_subentry_id=current.config_subentry_id,
        start_date=effective,
        end_date=end_date,
        supersedes_id=current_id,
        **{**carried, **changes},
    )


def end_regimen(conn: Connection, regimen_id: uuid.UUID, end_date: date) -> None:
    """Stop a regimen being in force from `end_date` onward.

    Never deletes: the row and anything referencing it stay in the record.
    Can only shorten a regimen, never extend it, so it can't create an
    overlap. Ending a regimen before it starts leaves it with an empty range.
    """
    current = get_regimen(conn, regimen_id)
    if current is None:
        raise LookupError(f"no regimen {regimen_id}")
    end_date = max(end_date, current.start_date)
    if current.end_date is not None and current.end_date <= end_date:
        return  # already ended by then
    conn.execute(
        regimen_table.update()
        .where(regimen_table.c.id == regimen_id)
        .values(end_date=end_date)
    )


def regimen_in_force(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    medication_id: uuid.UUID,
    on: date,
) -> Row[Any] | None:
    """The regimen in force for this subject and medication on date `on`."""
    r = regimen_table.c
    rows = conn.execute(
        sa.select(regimen_table).where(
            r.subject_id == subject_id,
            r.medication_id == medication_id,
            r.start_date <= on,
            sa.or_(r.end_date.is_(None), r.end_date > on),
        )
    ).all()
    if len(rows) > 1:
        # insert_regimen makes this impossible; if it happens, something
        # wrote around it, and guessing which one is meant would be worse
        # than failing.
        raise RegimenOverlapError(
            f"{len(rows)} regimens in force on {on} for subject {subject_id}, "
            f"medication {medication_id}"
        )
    return rows[0] if rows else None
