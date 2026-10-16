"""The dose-logging funnel's storage half: idempotency, guardrails, and
persistence of one administration, all in one transaction.

Every capture path (the log_dose service, the log-dose button, and later NFC
and notifications) ends up in log_dose here, so these rules are implemented
exactly once:

- Idempotency is enforced by the database. Each call inserts a
  capture_event row whose idempotency_key is UNIQUE; an IntegrityError means
  "this action is already recorded". There is deliberately no
  select-then-insert check, which two concurrent calls could both pass.
- Guardrails warn and record; they never block. log_dose records something
  that has already happened, and refusing to record a dose that was given
  would make the record wrong. Every guardrail returns an outcome; any that
  tripped are stored on the administration, along with whether the caller
  overrode it. The override is itself clinical data.
- given_late and missed are computed by the system from a schedule and are
  refused from callers.

Blocking throughout: call via hass.async_add_executor_job.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Row

from .database import administration_table, capture_event_table, regimen_table


class AdministrationStatus(StrEnum):
    GIVEN = "given"
    GIVEN_LATE = "given_late"
    REFUSED = "refused"
    VOMITED_WITHIN_30MIN = "vomited_within_30min"
    HELD_PER_VET = "held_per_vet"
    SKIPPED_OTHER = "skipped_other"
    MISSED = "missed"


# Computed from a schedule (phase 2). Never accepted from a caller.
SYSTEM_ONLY_STATUSES = frozenset(
    {AdministrationStatus.GIVEN_LATE, AdministrationStatus.MISSED}
)
CALLER_STATUSES = tuple(
    s for s in AdministrationStatus if s not in SYSTEM_ONLY_STATUSES
)

# Statuses where the drug went in, at least partly, so they count toward the
# interval and 24-hour guardrails. Vomiting within 30 minutes counts: a
# re-dose after it is exactly the case a vet may approve, and the guardrail
# should ask for that approval (an override) rather than assume it.
DOSE_TAKEN_STATUSES = frozenset(
    {
        AdministrationStatus.GIVEN,
        AdministrationStatus.GIVEN_LATE,
        AdministrationStatus.VOMITED_WITHIN_30MIN,
    }
)


class Guardrail(StrEnum):
    MIN_INTERVAL = "min_interval"
    MAX_SINGLE_DOSE = "max_single_dose"
    MAX_DAILY_TOTAL = "max_daily_total"


class SystemOnlyStatusError(ValueError):
    """A caller tried to record given_late or missed."""


@dataclass(frozen=True)
class GuardrailOutcome:
    """One guardrail's verdict on one dose.

    limit is None when the regimen sets no such limit (then tripped is
    always False). actual is the value compared against it: seconds since
    the nearest other dose, this dose's amount, or the 24-hour total
    including this dose.
    """

    guardrail: Guardrail
    tripped: bool
    limit: float | None
    actual: float | None
    overridden: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "guardrail": str(self.guardrail)}


@dataclass(frozen=True)
class DoseRequest:
    regimen_id: uuid.UUID
    administered_at: datetime
    capture_method: str
    status: AdministrationStatus = AdministrationStatus.GIVEN
    dose_units: float | None = None
    administered_by: str | None = None
    note: str | None = None
    idempotency_key: str | None = None
    # What the capture path itself identifies the action by (a button, a
    # user, later an NFC tag). Part of the generated idempotency key.
    source_ref: str | None = None
    override_guardrails: bool = False
    scheduled_for: datetime | None = None


@dataclass(frozen=True)
class DoseResult:
    administration_id: uuid.UUID
    duplicate: bool
    guardrails: list[GuardrailOutcome]
    # The administration as stored, JSON-ready, for the audit log and event.
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def tripped(self) -> list[GuardrailOutcome]:
        return [outcome for outcome in self.guardrails if outcome.tripped]


def iso(moment: datetime) -> str:
    """The storage form of a timestamp: UTC, always with microseconds, so
    string order is time order."""
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(UTC).isoformat(timespec="microseconds")


def idempotency_key(
    source: str, source_ref: str | None, at: datetime, bucket_seconds: int
) -> str:
    """A stable hash of (source, source_ref, time bucket).

    Two captures from the same source and reference whose administered_at
    falls in the same bucket are the same action. Buckets are fixed
    windows, so two captures either side of a boundary don't collapse; the
    min-interval guardrail is what catches those.
    """
    bucket = int(at.timestamp()) // bucket_seconds
    material = "\x1f".join((source, source_ref or "", str(bucket)))
    return hashlib.sha256(material.encode()).hexdigest()


def _counted_doses(subject_id: uuid.UUID, medication_id: uuid.UUID) -> sa.Select:
    """Administrations of this drug to this animal that guardrails count:
    across every regimen (so a supersede doesn't reset the interval or the
    24-hour total), drug actually taken, and not since amended."""
    a = administration_table.c
    amended_by = administration_table.alias("amended_by")
    return (
        sa.select(a.administered_at, a.dose_mg)
        .select_from(
            administration_table.join(regimen_table, regimen_table.c.id == a.regimen_id)
        )
        .where(
            regimen_table.c.subject_id == subject_id,
            regimen_table.c.medication_id == medication_id,
            a.status.in_([str(s) for s in DOSE_TAKEN_STATUSES]),
            ~sa.exists().where(amended_by.c.amends_id == a.id),
        )
    )


def evaluate_guardrails(
    conn: Connection,
    regimen: Row[Any],
    *,
    administered_at: datetime,
    dose_mg: float,
    status: AdministrationStatus,
    override: bool,
) -> list[GuardrailOutcome]:
    """One outcome per guardrail, tripped or not. Read-only."""
    counts = status in DOSE_TAKEN_STATUSES
    doses = _counted_doses(regimen.subject_id, regimen.medication_id).subquery()

    # Minimum interval: the nearest counted dose either side, so a
    # backfilled dose is checked against the one logged after it too.
    interval = regimen.min_interval_seconds
    nearest: float | None = None
    if interval and counts:
        window = timedelta(seconds=interval)
        times = conn.execute(
            sa.select(doses.c.administered_at).where(
                doses.c.administered_at > iso(administered_at - window),
                doses.c.administered_at < iso(administered_at + window),
            )
        ).scalars()
        gaps = [
            abs((datetime.fromisoformat(t) - administered_at).total_seconds())
            for t in times
        ]
        nearest = min(gaps) if gaps else None

    # Rolling 24 hours ending at this dose, including it.
    daily_total: float | None = None
    if regimen.dose_max_daily is not None and counts:
        earlier = conn.execute(
            sa.select(sa.func.coalesce(sa.func.sum(doses.c.dose_mg), 0.0)).where(
                doses.c.administered_at > iso(administered_at - timedelta(hours=24)),
                doses.c.administered_at <= iso(administered_at),
            )
        ).scalar_one()
        daily_total = earlier + dose_mg

    def outcome(
        guardrail: Guardrail, limit: float | None, actual: float | None, tripped: bool
    ) -> GuardrailOutcome:
        return GuardrailOutcome(
            guardrail=guardrail,
            tripped=tripped,
            limit=limit,
            actual=actual,
            overridden=override and tripped,
        )

    max_single = regimen.dose_max_single
    return [
        outcome(
            Guardrail.MIN_INTERVAL,
            interval,
            nearest,
            bool(interval) and nearest is not None and nearest < interval,
        ),
        outcome(
            Guardrail.MAX_SINGLE_DOSE,
            max_single,
            dose_mg if counts else None,
            counts and max_single is not None and dose_mg > max_single,
        ),
        outcome(
            Guardrail.MAX_DAILY_TOTAL,
            regimen.dose_max_daily,
            daily_total,
            daily_total is not None and daily_total > regimen.dose_max_daily,
        ),
    ]


def _record(row: Row[Any]) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, uuid.UUID) else value
        for key, value in row._asdict().items()
    }


def _payload(request: DoseRequest) -> dict[str, Any]:
    return {
        "regimen_id": str(request.regimen_id),
        "administered_at": iso(request.administered_at),
        "status": str(request.status),
        "dose_units": request.dose_units,
        "administered_by": request.administered_by,
        "capture_method": request.capture_method,
        "note": request.note,
        "override_guardrails": request.override_guardrails,
        "idempotency_key": request.idempotency_key,
    }


def _record_duplicate(
    conn: Connection, key: str, request: DoseRequest, received_at: str
) -> DoseResult:
    original = conn.execute(
        sa.select(capture_event_table.c.collapsed_into_administration_id).where(
            capture_event_table.c.idempotency_key == key
        )
    ).scalar_one()
    a = administration_table.c
    conn.execute(
        capture_event_table.insert().values(
            id=uuid.uuid7(),
            idempotency_key=None,
            source=request.capture_method,
            source_ref=request.source_ref,
            received_at=received_at,
            payload={**_payload(request), "collided_with_key": key},
            collapsed_into_administration_id=original,
            was_duplicate=True,
        )
    )
    conn.execute(
        administration_table.update()
        .where(a.id == original)
        .values(source_event_count=a.source_event_count + 1)
    )
    row = conn.execute(sa.select(administration_table).where(a.id == original)).one()
    return DoseResult(
        administration_id=original,
        duplicate=True,
        guardrails=[
            GuardrailOutcome(**{**flag, "guardrail": Guardrail(flag["guardrail"])})
            for flag in row.guardrail_flags or []
        ],
        record=_record(row),
    )


def log_dose(
    engine: sa.engine.Engine,
    request: DoseRequest,
    *,
    bucket_seconds: int,
    system: bool = False,
) -> DoseResult:
    """Record one administration, or recognise it as already recorded.

    `system=True` is for the scheduler alone (phase 2), the only writer
    allowed to record given_late or missed.
    """
    if request.status in SYSTEM_ONLY_STATUSES and not system:
        raise SystemOnlyStatusError(
            f"{request.status} is computed from the schedule and can't be logged"
        )
    if request.dose_units is not None and not request.dose_units > 0:
        raise ValueError("dose_units must be positive")
    administered_at = iso(request.administered_at)
    received_at = iso(datetime.now(UTC))
    key = request.idempotency_key or idempotency_key(
        request.capture_method,
        request.source_ref,
        request.administered_at,
        bucket_seconds,
    )

    with engine.begin() as conn:
        # The idempotency check *is* this insert. The savepoint keeps an
        # IntegrityError from poisoning the outer transaction, which the
        # duplicate path still needs.
        try:
            with conn.begin_nested():
                capture_id = uuid.uuid7()
                conn.execute(
                    capture_event_table.insert().values(
                        id=capture_id,
                        idempotency_key=key,
                        source=request.capture_method,
                        source_ref=request.source_ref,
                        received_at=received_at,
                        payload=_payload(request),
                        collapsed_into_administration_id=None,
                        was_duplicate=False,
                    )
                )
        except sa.exc.IntegrityError:
            return _record_duplicate(conn, key, request, received_at)

        regimen = conn.execute(
            sa.select(regimen_table).where(regimen_table.c.id == request.regimen_id)
        ).first()
        if regimen is None:
            raise LookupError(f"no regimen {request.regimen_id}")

        dose_units = (
            request.dose_units if request.dose_units is not None else regimen.dose_units
        )
        unit_strength = regimen.unit_strength
        dose_mg = dose_units * unit_strength
        outcomes = evaluate_guardrails(
            conn,
            regimen,
            administered_at=request.administered_at,
            dose_mg=dose_mg,
            status=request.status,
            override=request.override_guardrails,
        )
        flags = [o.as_dict() for o in outcomes if o.tripped] or None

        administration_id = uuid.uuid7()
        conn.execute(
            administration_table.insert().values(
                id=administration_id,
                regimen_id=regimen.id,
                scheduled_for=iso(request.scheduled_for)
                if request.scheduled_for
                else None,
                administered_at=administered_at,
                dose_units=dose_units,
                unit_strength=unit_strength,
                dose_mg=dose_mg,
                status=str(request.status),
                administered_by=request.administered_by,
                capture_method=request.capture_method,
                note=request.note,
                source_event_count=1,
                guardrail_flags=flags,
                amends_id=None,
                created_at=received_at,
            )
        )
        conn.execute(
            capture_event_table.update()
            .where(capture_event_table.c.id == capture_id)
            .values(collapsed_into_administration_id=administration_id)
        )
        row = conn.execute(
            sa.select(administration_table).where(
                administration_table.c.id == administration_id
            )
        ).one()

    return DoseResult(
        administration_id=administration_id,
        duplicate=False,
        guardrails=outcomes,
        record=_record(row),
    )
