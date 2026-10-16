"""Tests for the dose-logging funnel's storage half: three-way dose
recording, database-enforced idempotency, guardrails that warn and record
rather than block, and refusal of system-only statuses.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.storage import administrations, database, regimens
from custom_components.soap_opera.storage.administrations import (
    AdministrationStatus,
    DoseRequest,
    Guardrail,
    SystemOnlyStatusError,
)

BUCKET = 120
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


def _setup(engine: sa.engine.Engine, **regimen: Any) -> uuid.UUID:
    database.run_migrations(engine)
    with engine.begin() as conn:
        subject_id = uuid.uuid7()
        conn.execute(
            database.subjects_table.insert().values(
                id=subject_id,
                config_entry_id="entry",
                name="Biscuit",
                species="Dog",
                created_at="now",
            )
        )
        medication_id = regimens.insert_medication(
            conn,
            generic_name="gabapentin",
            strength=100,
            unit="mg",
            form="capsule",
            route="oral",
        )
        return regimens.insert_regimen(
            conn,
            subject_id=subject_id,
            medication_id=medication_id,
            config_subentry_id="sub",
            start_date=date(2026, 1, 1),
            **{"dose_units": 3, "unit_strength": 100, **regimen},
        )


@pytest.fixture
def engine() -> sa.engine.Engine:
    return sa.create_engine("sqlite:///:memory:", future=True)


def _log(
    engine: sa.engine.Engine, regimen_id: uuid.UUID, **kwargs: Any
) -> administrations.DoseResult:
    request = DoseRequest(
        regimen_id=regimen_id,
        administered_at=kwargs.pop("at", T0),
        capture_method=kwargs.pop("capture_method", "service"),
        **kwargs,
    )
    return administrations.log_dose(engine, request, bucket_seconds=BUCKET)


def _rows(engine: sa.engine.Engine, table: sa.Table) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(sa.select(table)).all())


def _outcome(result: administrations.DoseResult, guardrail: Guardrail) -> Any:
    return next(o for o in result.guardrails if o.guardrail is guardrail)


# --- recording --------------------------------------------------------------


def test_defaults_record_the_standard_dose_three_ways(engine) -> None:
    regimen_id = _setup(engine)

    result = _log(engine, regimen_id, source_ref="r1")

    (row,) = _rows(engine, database.administration_table)
    assert row.id == result.administration_id
    assert row.status == "given"
    assert (row.dose_units, row.unit_strength, row.dose_mg) == (3, 100, 300)
    assert row.source_event_count == 1
    assert row.guardrail_flags is None
    (capture,) = _rows(engine, database.capture_event_table)
    assert capture.collapsed_into_administration_id == result.administration_id
    assert capture.was_duplicate is False


def test_explicit_dose_units_are_recorded(engine) -> None:
    regimen_id = _setup(engine)

    _log(engine, regimen_id, dose_units=1.5)

    (row,) = _rows(engine, database.administration_table)
    assert (row.dose_units, row.dose_mg) == (1.5, 150)


@pytest.mark.parametrize(
    "status", [AdministrationStatus.GIVEN_LATE, AdministrationStatus.MISSED]
)
def test_system_only_statuses_are_refused_from_callers(engine, status) -> None:
    regimen_id = _setup(engine)

    with pytest.raises(SystemOnlyStatusError):
        _log(engine, regimen_id, status=status)

    assert _rows(engine, database.capture_event_table) == []


def test_status_is_constrained_by_the_database(engine) -> None:
    regimen_id = _setup(engine)
    result = _log(engine, regimen_id)

    with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
        conn.execute(
            database.administration_table.update()
            .where(database.administration_table.c.id == result.administration_id)
            .values(status="probably_given")
        )


def test_unknown_regimen_leaves_nothing_behind(engine) -> None:
    _setup(engine)

    with pytest.raises(LookupError):
        _log(engine, uuid.uuid7())

    assert _rows(engine, database.capture_event_table) == []


def test_naive_timestamps_are_refused(engine) -> None:
    regimen_id = _setup(engine)

    with pytest.raises(ValueError, match="timezone-aware"):
        _log(engine, regimen_id, at=datetime(2026, 3, 2, 8, 0))


# --- idempotency ------------------------------------------------------------


def test_same_key_collapses_into_one_administration(engine) -> None:
    regimen_id = _setup(engine)

    first = _log(engine, regimen_id, idempotency_key="k")
    second = _log(engine, regimen_id, idempotency_key="k", dose_units=1)

    assert second.duplicate is True
    assert second.administration_id == first.administration_id
    (row,) = _rows(engine, database.administration_table)
    assert row.source_event_count == 2
    assert row.dose_units == 3  # the duplicate changes nothing recorded
    captures = _rows(engine, database.capture_event_table)
    assert [c.was_duplicate for c in captures] == [False, True]
    assert captures[1].idempotency_key is None
    assert captures[1].payload["collided_with_key"] == "k"
    assert {c.collapsed_into_administration_id for c in captures} == {
        first.administration_id
    }


def test_generated_key_collapses_within_a_bucket_only(engine) -> None:
    regimen_id = _setup(engine)
    bucket_start = datetime.fromtimestamp(
        int(T0.timestamp()) // BUCKET * BUCKET, tz=UTC
    )

    a = _log(engine, regimen_id, source_ref="tag", at=bucket_start)
    b = _log(engine, regimen_id, source_ref="tag", at=bucket_start + timedelta(59))
    assert a.administration_id != b.administration_id  # 59 days later

    c = _log(
        engine, regimen_id, source_ref="tag", at=bucket_start + timedelta(seconds=30)
    )
    assert c.duplicate and c.administration_id == a.administration_id

    d = _log(
        engine,
        regimen_id,
        source_ref="tag",
        at=bucket_start + timedelta(seconds=BUCKET),
    )
    assert not d.duplicate

    e = _log(engine, regimen_id, source_ref="other", at=bucket_start)
    assert not e.duplicate


def test_concurrent_duplicates_record_exactly_one_administration(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}", future=True)
    regimen_id = _setup(engine)
    barrier = threading.Barrier(4)
    results: list[administrations.DoseResult] = []
    errors: list[BaseException] = []

    def log() -> None:
        barrier.wait()
        try:
            results.append(_log(engine, regimen_id, idempotency_key="same"))
        except BaseException as err:  # noqa: BLE001 - asserted below
            errors.append(err)

    threads = [threading.Thread(target=log) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(r.duplicate for r in results) == [False, True, True, True]
    assert len({r.administration_id for r in results}) == 1
    (row,) = _rows(engine, database.administration_table)
    assert row.source_event_count == 4
    engine.dispose()


# --- guardrails -------------------------------------------------------------


def test_min_interval_warns_records_and_does_not_block(engine) -> None:
    regimen_id = _setup(engine, min_interval_seconds=8 * 3600)
    _log(engine, regimen_id)

    result = _log(engine, regimen_id, at=T0 + timedelta(hours=2))

    outcome = _outcome(result, Guardrail.MIN_INTERVAL)
    assert outcome.tripped and not outcome.overridden
    assert outcome.limit == 8 * 3600 and outcome.actual == 2 * 3600
    rows = _rows(engine, database.administration_table)
    assert len(rows) == 2  # recorded anyway
    assert rows[1].guardrail_flags == [outcome.as_dict()]


def test_override_is_recorded_with_the_guardrail_it_bypassed(engine) -> None:
    regimen_id = _setup(engine, dose_max_single=200)

    result = _log(engine, regimen_id, override_guardrails=True)

    outcome = _outcome(result, Guardrail.MAX_SINGLE_DOSE)
    assert outcome.tripped and outcome.overridden
    assert (outcome.limit, outcome.actual) == (200, 300)
    (row,) = _rows(engine, database.administration_table)
    assert row.guardrail_flags == [
        {
            "guardrail": "max_single_dose",
            "tripped": True,
            "limit": 200,
            "actual": 300,
            "overridden": True,
        }
    ]


def test_untripped_guardrails_are_not_marked_overridden(engine) -> None:
    regimen_id = _setup(engine, dose_max_single=1000)

    result = _log(engine, regimen_id, override_guardrails=True)

    assert not any(o.overridden for o in result.guardrails)
    assert _rows(engine, database.administration_table)[0].guardrail_flags is None


def test_daily_total_is_rolling_24_hours(engine) -> None:
    regimen_id = _setup(engine, dose_max_daily=900)
    _log(engine, regimen_id, at=T0)
    _log(engine, regimen_id, at=T0 + timedelta(hours=8))

    third = _log(engine, regimen_id, at=T0 + timedelta(hours=16))
    assert not _outcome(third, Guardrail.MAX_DAILY_TOTAL).tripped  # exactly 900

    fourth = _log(engine, regimen_id, at=T0 + timedelta(hours=23))
    outcome = _outcome(fourth, Guardrail.MAX_DAILY_TOTAL)
    assert outcome.tripped and outcome.actual == 1200

    # 24h after the first dose it drops out of the window.
    fifth = _log(engine, regimen_id, at=T0 + timedelta(hours=32, minutes=1))
    assert _outcome(fifth, Guardrail.MAX_DAILY_TOTAL).actual == 900


def test_refused_doses_neither_trip_nor_count(engine) -> None:
    regimen_id = _setup(engine, min_interval_seconds=3600, dose_max_daily=300)
    _log(engine, regimen_id, status=AdministrationStatus.REFUSED)

    refused_again = _log(
        engine,
        regimen_id,
        status=AdministrationStatus.REFUSED,
        at=T0 + timedelta(minutes=10),
    )
    assert not any(o.tripped for o in refused_again.guardrails)

    given = _log(engine, regimen_id, at=T0 + timedelta(minutes=20))
    assert not any(o.tripped for o in given.guardrails)


def test_guardrails_span_a_supersede(engine) -> None:
    """A dose change mid-day doesn't reset the interval or the 24h total."""
    regimen_id = _setup(engine, min_interval_seconds=8 * 3600)
    _log(engine, regimen_id, at=T0)
    with engine.begin() as conn:
        new_id = regimens.supersede_regimen(
            conn, regimen_id, effective=date(2026, 3, 2), dose_units=2
        )

    result = _log(engine, new_id, at=T0 + timedelta(hours=1))

    assert _outcome(result, Guardrail.MIN_INTERVAL).tripped


def test_backfilled_dose_is_checked_against_later_doses(engine) -> None:
    regimen_id = _setup(engine, min_interval_seconds=8 * 3600)
    _log(engine, regimen_id, at=T0)

    backfilled = _log(engine, regimen_id, at=T0 - timedelta(hours=1))

    assert _outcome(backfilled, Guardrail.MIN_INTERVAL).tripped


def test_amended_administrations_are_not_counted(engine) -> None:
    regimen_id = _setup(engine, min_interval_seconds=8 * 3600)
    wrong = _log(engine, regimen_id, at=T0)
    with engine.begin() as conn:
        # Stand-in for the amend path (not built yet): a correcting row
        # pointing at the wrong one, recorded as refused.
        conn.execute(
            database.administration_table.insert().values(
                id=uuid.uuid7(),
                regimen_id=regimen_id,
                administered_at=administrations.iso(T0),
                dose_units=3,
                unit_strength=100,
                dose_mg=300,
                status="refused",
                capture_method="test",
                source_event_count=1,
                amends_id=wrong.administration_id,
                created_at="now",
            )
        )

    result = _log(engine, regimen_id, at=T0 + timedelta(hours=1))

    assert not _outcome(result, Guardrail.MIN_INTERVAL).tripped
