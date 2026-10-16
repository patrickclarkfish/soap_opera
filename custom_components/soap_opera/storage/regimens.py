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
from dataclasses import dataclass
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

# Which edits are a new version of the prescription and which are a
# correction to the record of it. A design decision, not an implementation
# detail: changing a field in DOSING_FIELDS goes through supersede_regimen
# (end-date and insert, with an effective date), so "before vs. after the
# change" stays answerable. Changing a field in NON_DOSING_FIELDS updates
# the current row in place.
#
# Dosing covers anything that changes what the animal is given, how much,
# or when: the dose and strength, frequency and scheduled times (an evening
# dose moved to the morning is a clinical change, not a typo), PRN versus
# scheduled, the minimum interval, and the variable-dose bounds. Only who
# prescribed it and why are non-dosing.
DOSING_FIELDS = frozenset(
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
    }
)
NON_DOSING_FIELDS = frozenset({"indication", "prescribing_vet"})
assert DOSING_FIELDS | NON_DOSING_FIELDS == REGIMEN_FIELDS
assert not DOSING_FIELDS & NON_DOSING_FIELDS

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


class _Inherit:
    """Sentinel type: supersede_regimen's end_date defaults to the current
    row's, and None already means "open-ended"."""


INHERIT = _Inherit()


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
    exclude_id: uuid.UUID | None = None,
    exclude_config_subentry_id: str | None = None,
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
    if exclude_id is not None:
        query = query.where(r.id != exclude_id)
    if exclude_config_subentry_id is not None:
        query = query.where(
            sa.or_(
                r.config_subentry_id.is_(None),
                r.config_subentry_id != exclude_config_subentry_id,
            )
        )
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
    end_date: date | None | _Inherit = INHERIT,
    **changes: Any,
) -> uuid.UUID:
    """End-date the current regimen at `effective` and insert its successor.

    The successor copies every field of the current row, then applies
    `changes` (REGIMEN_FIELDS only). It is in force from `effective` until
    `end_date`, which by default is the current row's own end_date: a dose
    change partway through a fixed course keeps the course end. Returns the
    new row's id.

    The effective date must fall within the current row's range: not before
    it starts, and before it ends if it has an end. An effective date equal
    to its start_date is allowed: the current row is left with an empty
    range, i.e. it never came into force, which is the honest record of a
    prescription changed before its first day.
    """
    _validate_regimen_fields(changes)
    current = get_regimen(conn, current_id)
    if current is None:
        raise LookupError(f"no regimen {current_id}")
    if current.end_date is not None and effective >= current.end_date:
        raise RegimenNotSupersedableError(
            f"regimen {current_id} has ended by {effective} "
            f"(end date {current.end_date})"
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
        end_date=current.end_date if isinstance(end_date, _Inherit) else end_date,
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


def update_regimen_in_place(
    conn: Connection, regimen_id: uuid.UUID, **changes: Any
) -> None:
    """Correct NON_DOSING_FIELDS on one row. Refuses dosing fields: those
    must go through supersede_regimen."""
    dosing = set(changes) & DOSING_FIELDS
    if dosing:
        raise ValueError(
            f"dosing fields {sorted(dosing)} must be changed by superseding"
        )
    _validate_regimen_fields(changes)
    if changes:
        conn.execute(
            regimen_table.update()
            .where(regimen_table.c.id == regimen_id)
            .values(**changes)
        )


def set_regimen_end_date(
    conn: Connection, regimen_id: uuid.UUID, end_date: date | None
) -> None:
    """Move (or remove) the planned end of a regimen, e.g. a course
    extended by the vet. Unlike end_regimen this can extend, so it rechecks
    for overlap against every other regimen of the same drug."""
    current = get_regimen(conn, regimen_id)
    if current is None:
        raise LookupError(f"no regimen {regimen_id}")
    if end_date is not None and end_date < current.start_date:
        raise ValueError("end_date is before start_date")
    _check_no_overlap(
        conn,
        subject_id=current.subject_id,
        medication_id=current.medication_id,
        start_date=current.start_date,
        end_date=end_date,
        exclude_id=regimen_id,
    )
    conn.execute(
        regimen_table.update()
        .where(regimen_table.c.id == regimen_id)
        .values(end_date=end_date)
    )


def find_overlap(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    medication_id: uuid.UUID,
    start_date: date,
    end_date: date | None,
    exclude_config_subentry_id: str | None = None,
) -> bool:
    """Read-only form of the overlap check, for validating input early.
    Optionally ignores every row of one subentry's chain (the one being
    edited)."""
    try:
        _check_no_overlap(
            conn,
            subject_id=subject_id,
            medication_id=medication_id,
            start_date=start_date,
            end_date=end_date,
            exclude_config_subentry_id=exclude_config_subentry_id,
        )
    except RegimenOverlapError:
        return True
    return False


def chain_head(conn: Connection, config_subentry_id: str) -> Row[Any] | None:
    """The newest version of the regimen collected through a subentry: the
    row in its chain that nothing supersedes."""
    r = regimen_table.c
    successor = regimen_table.alias("successor")
    return conn.execute(
        sa.select(regimen_table)
        .where(r.config_subentry_id == config_subentry_id)
        .where(~sa.exists().where(successor.c.supersedes_id == r.id))
    ).first()


def list_medications(conn: Connection) -> list[Row[Any]]:
    return list(
        conn.execute(
            sa.select(medication_table).order_by(
                medication_table.c.generic_name, medication_table.c.strength
            )
        ).all()
    )


@dataclass(frozen=True)
class RegimenSpec:
    """What a regimen subentry says the regimen should be, already parsed.

    Exactly one of medication_id (an existing medication) or new_medication
    (insert_medication keyword arguments) is set. effective_date is when the
    most recent dosing edit takes effect; start_date when the regimen first
    did.
    """

    fields: dict[str, Any]
    start_date: date
    end_date: date | None
    effective_date: date
    medication_id: uuid.UUID | None = None
    new_medication: dict[str, Any] | None = None


def sync_regimen(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    config_subentry_id: str,
    spec: RegimenSpec,
) -> uuid.UUID:
    """Bring the database in line with one regimen subentry. Returns the
    id of the chain head afterwards.

    - No rows yet: insert the medication if it is new, then the regimen.
    - A dosing field differs from the head: supersede at
      spec.effective_date.
    - Otherwise: correct non-dosing fields and the end date in place.

    The medication is fixed for the life of a subentry; switching drugs is
    a new regimen, not a version of this one. Idempotent: a second call
    with the same spec changes nothing.
    """
    head = chain_head(conn, config_subentry_id)
    if head is None:
        medication_id = spec.medication_id
        if medication_id is None:
            if spec.new_medication is None:
                raise ValueError("spec names no medication")
            medication_id = insert_medication(conn, **spec.new_medication)
        return insert_regimen(
            conn,
            subject_id=subject_id,
            medication_id=medication_id,
            config_subentry_id=config_subentry_id,
            start_date=spec.start_date,
            end_date=spec.end_date,
            **spec.fields,
        )

    dosing_changed = any(
        getattr(head, name) != spec.fields[name]
        for name in DOSING_FIELDS
        if name in spec.fields
    )
    if dosing_changed:
        return supersede_regimen(
            conn,
            head.id,
            effective=spec.effective_date,
            end_date=spec.end_date,
            **spec.fields,
        )

    non_dosing = {
        name: spec.fields[name]
        for name in NON_DOSING_FIELDS
        if name in spec.fields and getattr(head, name) != spec.fields[name]
    }
    update_regimen_in_place(conn, head.id, **non_dosing)
    if head.end_date != spec.end_date:
        set_regimen_end_date(conn, head.id, spec.end_date)
    return head.id


def end_regimens_for_removed_subentries(
    conn: Connection,
    *,
    subject_id: uuid.UUID,
    current_subentry_ids: set[str],
    today: date,
) -> list[uuid.UUID]:
    """End-date (never delete) every still-running regimen of this subject
    whose subentry no longer exists. Returns the ids it ended."""
    r = regimen_table.c
    rows = conn.execute(
        sa.select(r.id, r.config_subentry_id).where(
            r.subject_id == subject_id,
            r.config_subentry_id.is_not(None),
            sa.or_(r.end_date.is_(None), r.end_date > today),
        )
    ).all()
    ended = []
    for row in rows:
        if row.config_subentry_id not in current_subentry_ids:
            end_regimen(conn, row.id, today)
            ended.append(row.id)
    return ended
