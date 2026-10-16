"""Tests for storage/regimens.py: the supersede rule, effective dating, and
the one-regimen-in-force-per-subject-and-medication invariant.
"""

from __future__ import annotations

import threading
import uuid
from datetime import date
from pathlib import Path

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.storage import database, regimens
from custom_components.soap_opera.storage.regimens import (
    MedicationForm,
    RegimenNotSupersedableError,
    RegimenOverlapError,
)
from sqlalchemy.engine import Connection

JAN_1 = date(2026, 1, 1)
JAN_10 = date(2026, 1, 10)
JAN_15 = date(2026, 1, 15)
JAN_20 = date(2026, 1, 20)
FEB_1 = date(2026, 2, 1)


@pytest.fixture
def engine() -> sa.engine.Engine:
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    database.run_migrations(engine)
    return engine


@pytest.fixture
def conn(engine: sa.engine.Engine) -> Connection:
    with engine.begin() as conn:
        yield conn


def _subject(conn: Connection) -> uuid.UUID:
    subject_id = uuid.uuid7()
    conn.execute(
        database.subjects_table.insert().values(
            id=subject_id,
            config_entry_id=str(subject_id),
            name="Biscuit",
            species="Dog",
            created_at="now",
        )
    )
    return subject_id


def _gabapentin(conn: Connection) -> uuid.UUID:
    return regimens.insert_medication(
        conn,
        generic_name="gabapentin",
        strength=100,
        unit="mg",
        form=MedicationForm.CAPSULE,
        route="oral",
    )


@pytest.fixture
def ids(conn: Connection) -> tuple[uuid.UUID, uuid.UUID]:
    return _subject(conn), _gabapentin(conn)


def _regimen(
    conn: Connection,
    ids: tuple[uuid.UUID, uuid.UUID],
    start: date,
    end: date | None = None,
    dose_units: float = 1,
) -> uuid.UUID:
    subject_id, medication_id = ids
    return regimens.insert_regimen(
        conn,
        subject_id=subject_id,
        medication_id=medication_id,
        start_date=start,
        end_date=end,
        dose_units=dose_units,
        unit_strength=100,
    )


def _in_force(conn: Connection, ids: tuple[uuid.UUID, uuid.UUID], on: date) -> object:
    row = regimens.regimen_in_force(
        conn, subject_id=ids[0], medication_id=ids[1], on=on
    )
    return None if row is None else row.id


# --- in force on date d -----------------------------------------------------


def test_in_force_is_half_open(conn: Connection, ids) -> None:
    regimen_id = _regimen(conn, ids, JAN_10, JAN_20)

    assert _in_force(conn, ids, JAN_1) is None
    assert _in_force(conn, ids, JAN_10) == regimen_id  # start is inclusive
    assert _in_force(conn, ids, JAN_15) == regimen_id
    assert _in_force(conn, ids, JAN_20) is None  # end is exclusive


def test_open_ended_regimen_stays_in_force(conn: Connection, ids) -> None:
    regimen_id = _regimen(conn, ids, JAN_1)

    assert _in_force(conn, ids, date(2030, 1, 1)) == regimen_id


def test_future_dated_change_is_not_in_force_yet(conn: Connection, ids) -> None:
    """The vet says "start the new dose Monday": today still resolves to the
    old dose, Monday onward to the new one.
    """
    current = _regimen(conn, ids, JAN_1, dose_units=1)
    monday = JAN_20

    new = regimens.supersede_regimen(conn, current, effective=monday, dose_units=2)

    assert _in_force(conn, ids, JAN_15) == current
    assert _in_force(conn, ids, monday) == new
    assert _in_force(conn, ids, FEB_1) == new


def test_scoped_to_subject_and_medication(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_1)
    other_subject = _subject(conn)

    assert (
        regimens.regimen_in_force(
            conn, subject_id=other_subject, medication_id=ids[1], on=JAN_15
        )
        is None
    )


# --- the supersede rule -----------------------------------------------------


def test_supersede_never_updates_dosing_on_the_old_row(conn: Connection, ids) -> None:
    current = _regimen(conn, ids, JAN_1, dose_units=0.6)

    new = regimens.supersede_regimen(conn, current, effective=JAN_15, dose_units=0.9)

    old_row = regimens.get_regimen(conn, current)
    new_row = regimens.get_regimen(conn, new)
    assert old_row is not None and new_row is not None
    assert old_row.dose_units == 0.6
    assert old_row.end_date == JAN_15
    assert new_row.dose_units == 0.9
    assert new_row.start_date == JAN_15
    assert new_row.end_date is None
    assert new_row.supersedes_id == current
    # Everything not changed carries over.
    assert new_row.unit_strength == old_row.unit_strength
    assert new_row.subject_id == old_row.subject_id


def test_supersede_chain_is_walkable(conn: Connection, ids) -> None:
    first = _regimen(conn, ids, JAN_1, dose_units=1)
    second = regimens.supersede_regimen(conn, first, effective=JAN_10, dose_units=2)
    third = regimens.supersede_regimen(conn, second, effective=JAN_20, dose_units=3)

    chain = []
    cursor: uuid.UUID | None = third
    while cursor is not None:
        row = regimens.get_regimen(conn, cursor)
        assert row is not None
        chain.append(row.dose_units)
        cursor = row.supersedes_id
    assert chain == [3, 2, 1]


def test_cannot_supersede_an_ended_regimen(conn: Connection, ids) -> None:
    first = _regimen(conn, ids, JAN_1)
    regimens.supersede_regimen(conn, first, effective=JAN_10, dose_units=2)

    with pytest.raises(RegimenNotSupersedableError):
        regimens.supersede_regimen(conn, first, effective=JAN_15, dose_units=3)


def test_supersede_partway_through_a_course_keeps_the_course_end(
    conn: Connection, ids
) -> None:
    course = _regimen(conn, ids, JAN_1, FEB_1, dose_units=1)

    new = regimens.supersede_regimen(conn, course, effective=JAN_15, dose_units=2)

    old_row = regimens.get_regimen(conn, course)
    new_row = regimens.get_regimen(conn, new)
    assert old_row is not None and old_row.end_date == JAN_15
    assert new_row is not None and new_row.end_date == FEB_1
    assert _in_force(conn, ids, JAN_20) == new
    assert _in_force(conn, ids, FEB_1) is None


def test_supersede_can_reopen_or_move_the_end(conn: Connection, ids) -> None:
    course = _regimen(conn, ids, JAN_1, FEB_1)

    new = regimens.supersede_regimen(
        conn, course, effective=JAN_15, end_date=None, dose_units=2
    )

    new_row = regimens.get_regimen(conn, new)
    assert new_row is not None and new_row.end_date is None


def test_cannot_supersede_on_or_after_the_course_end(conn: Connection, ids) -> None:
    course = _regimen(conn, ids, JAN_1, JAN_20)

    with pytest.raises(RegimenNotSupersedableError):
        regimens.supersede_regimen(conn, course, effective=JAN_20, dose_units=2)


def test_cannot_supersede_before_the_current_regimen_starts(
    conn: Connection, ids
) -> None:
    current = _regimen(conn, ids, JAN_10)

    with pytest.raises(RegimenNotSupersedableError):
        regimens.supersede_regimen(conn, current, effective=JAN_1, dose_units=2)


def test_supersede_on_start_day_leaves_an_empty_never_in_force_row(
    conn: Connection, ids
) -> None:
    current = _regimen(conn, ids, JAN_10)

    new = regimens.supersede_regimen(conn, current, effective=JAN_10, dose_units=2)

    assert _in_force(conn, ids, JAN_10) == new
    old_row = regimens.get_regimen(conn, current)
    assert old_row is not None and old_row.start_date == old_row.end_date == JAN_10


def test_supersede_rejects_structural_fields(conn: Connection, ids) -> None:
    current = _regimen(conn, ids, JAN_1)

    with pytest.raises(ValueError, match="not regimen fields"):
        regimens.supersede_regimen(
            conn, current, effective=JAN_10, subject_id=uuid.uuid7()
        )


# --- at most one in force per (subject, medication) -------------------------


def test_overlap_identical_ranges(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_10, JAN_20)
    with pytest.raises(RegimenOverlapError):
        _regimen(conn, ids, JAN_10, JAN_20)


def test_overlap_new_range_covers_existing_start(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_10, JAN_20)
    with pytest.raises(RegimenOverlapError):
        _regimen(conn, ids, JAN_1, JAN_15)


def test_overlap_new_range_covers_existing_end(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_10, JAN_20)
    with pytest.raises(RegimenOverlapError):
        _regimen(conn, ids, JAN_15, FEB_1)


def test_overlap_new_start_precedes_open_ended_current(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_10)
    with pytest.raises(RegimenOverlapError):
        _regimen(conn, ids, JAN_1)


def test_overlap_new_open_ended_after_open_ended_current(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_1)
    with pytest.raises(RegimenOverlapError):
        _regimen(conn, ids, FEB_1)


def test_adjacent_ranges_do_not_overlap(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_1, JAN_10)
    _regimen(conn, ids, JAN_10, JAN_20)  # starts the day the first ends
    _regimen(conn, ids, JAN_20)


def test_same_range_for_another_medication_is_fine(conn: Connection, ids) -> None:
    _regimen(conn, ids, JAN_1)
    other_medication = _gabapentin(conn)
    _regimen(conn, (ids[0], other_medication), JAN_1)


def test_overlap_rejection_rolls_back_cleanly(engine: sa.engine.Engine) -> None:
    with engine.begin() as conn:
        ids = (_subject(conn), _gabapentin(conn))
        _regimen(conn, ids, JAN_1)

    with pytest.raises(RegimenOverlapError), engine.begin() as conn:
        _regimen(conn, ids, JAN_10)

    with engine.connect() as conn:
        count = conn.execute(
            sa.select(sa.func.count()).select_from(database.regimen_table)
        ).scalar_one()
    assert count == 1


# --- ending -----------------------------------------------------------------


def test_end_regimen_end_dates_without_deleting(conn: Connection, ids) -> None:
    regimen_id = _regimen(conn, ids, JAN_1)

    regimens.end_regimen(conn, regimen_id, JAN_15)

    row = regimens.get_regimen(conn, regimen_id)
    assert row is not None and row.end_date == JAN_15
    assert _in_force(conn, ids, JAN_15) is None


def test_end_regimen_never_extends(conn: Connection, ids) -> None:
    regimen_id = _regimen(conn, ids, JAN_1, JAN_10)

    regimens.end_regimen(conn, regimen_id, FEB_1)

    row = regimens.get_regimen(conn, regimen_id)
    assert row is not None and row.end_date == JAN_10


# --- medication -------------------------------------------------------------


def test_liquid_needs_concentration_not_strength(conn: Connection) -> None:
    with pytest.raises(ValueError, match="concentration"):
        regimens.insert_medication(
            conn, generic_name="meloxicam", unit="mg", form="liquid", route="oral"
        )
    regimens.insert_medication(
        conn,
        generic_name="meloxicam",
        unit="mg",
        form="liquid",
        concentration=1.5,
        route="oral",
        shake_well=True,
    )


def test_unknown_form_is_rejected(conn: Connection) -> None:
    with pytest.raises(ValueError):
        regimens.insert_medication(
            conn,
            generic_name="x",
            strength=1,
            unit="mg",
            form="suppository",
            route="rectal",
        )


def test_foreign_keys_are_enforced(conn: Connection) -> None:
    with pytest.raises(sa.exc.IntegrityError):
        regimens.insert_regimen(
            conn,
            subject_id=uuid.uuid7(),
            medication_id=uuid.uuid7(),
            start_date=JAN_1,
            dose_units=1,
            unit_strength=100,
        )


def test_concurrent_overlapping_inserts_admit_exactly_one(tmp_path: Path) -> None:
    """The overlap check is a SELECT then an INSERT. Two writers racing it
    must not both pass: BEGIN IMMEDIATE makes the second wait for the first
    to commit, so it sees the first's row and is rejected.
    """
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'race.db'}", future=True)
    database.run_migrations(engine)
    with engine.begin() as conn:
        ids = (_subject(conn), _gabapentin(conn))

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def insert() -> None:
        barrier.wait()
        try:
            with engine.begin() as conn:
                _regimen(conn, ids, JAN_1)
                # Hold the transaction open long enough that, without
                # IMMEDIATE, the other thread would run its check meanwhile.
                threading.Event().wait(0.1)
            outcomes.append("inserted")
        except RegimenOverlapError:
            outcomes.append("rejected")

    threads = [threading.Thread(target=insert) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    assert sorted(outcomes) == ["inserted", "rejected"]


# --- sync from config, and the dosing / non-dosing split --------------------


def test_dosing_and_non_dosing_fields_partition_the_regimen_fields() -> None:
    assert regimens.DOSING_FIELDS | regimens.NON_DOSING_FIELDS == (
        regimens.REGIMEN_FIELDS
    )
    assert not regimens.DOSING_FIELDS & regimens.NON_DOSING_FIELDS


def test_in_place_update_refuses_dosing_fields(conn: Connection, ids) -> None:
    regimen_id = _regimen(conn, ids, JAN_1)

    with pytest.raises(ValueError, match="superseding"):
        regimens.update_regimen_in_place(conn, regimen_id, dose_units=2)


def test_extending_an_end_date_into_another_regimen_is_refused(
    conn: Connection, ids
) -> None:
    course = _regimen(conn, ids, JAN_1, JAN_10)
    _regimen(conn, ids, JAN_15)

    regimens.set_regimen_end_date(conn, course, JAN_15)  # adjacent: fine
    with pytest.raises(RegimenOverlapError):
        regimens.set_regimen_end_date(conn, course, JAN_20)


def _spec(
    medication_id: uuid.UUID, effective: date = JAN_1, **fields: object
) -> regimens.RegimenSpec:
    base: dict[str, object] = dict.fromkeys(regimens.REGIMEN_FIELDS) | {
        "dose_units": 1,
        "unit_strength": 100,
        "prn": False,
        "dose_variable": False,
    }
    return regimens.RegimenSpec(
        fields={**base, **fields},
        start_date=JAN_1,
        end_date=None,
        effective_date=effective,
        medication_id=medication_id,
    )


def test_sync_regimen_is_idempotent(conn: Connection, ids) -> None:
    spec = _spec(ids[1])
    first = regimens.sync_regimen(
        conn, subject_id=ids[0], config_subentry_id="s1", spec=spec
    )
    second = regimens.sync_regimen(
        conn, subject_id=ids[0], config_subentry_id="s1", spec=spec
    )

    assert first == second
    assert (
        conn.execute(
            sa.select(sa.func.count()).select_from(database.regimen_table)
        ).scalar_one()
        == 1
    )


def test_sync_regimen_supersedes_on_dosing_and_corrects_otherwise(
    conn: Connection, ids
) -> None:
    head = regimens.sync_regimen(
        conn, subject_id=ids[0], config_subentry_id="s1", spec=_spec(ids[1])
    )

    same_head = regimens.sync_regimen(
        conn,
        subject_id=ids[0],
        config_subentry_id="s1",
        spec=_spec(ids[1], indication="pain"),
    )
    assert same_head == head

    new_head = regimens.sync_regimen(
        conn,
        subject_id=ids[0],
        config_subentry_id="s1",
        spec=_spec(ids[1], indication="pain", dose_units=2, effective=JAN_15),
    )
    assert new_head != head
    row = regimens.get_regimen(conn, new_head)
    assert row is not None
    assert row.supersedes_id == head and row.start_date == JAN_15


def test_removed_subentries_are_end_dated(conn: Connection, ids) -> None:
    kept = regimens.sync_regimen(
        conn, subject_id=ids[0], config_subentry_id="kept", spec=_spec(ids[1])
    )
    other_medication = _gabapentin(conn)
    removed = regimens.sync_regimen(
        conn,
        subject_id=ids[0],
        config_subentry_id="removed",
        spec=_spec(other_medication),
    )

    ended = regimens.end_regimens_for_removed_subentries(
        conn, subject_id=ids[0], current_subentry_ids={"kept"}, today=JAN_15
    )

    assert ended == [removed]
    removed_row = regimens.get_regimen(conn, removed)
    kept_row = regimens.get_regimen(conn, kept)
    assert removed_row is not None and removed_row.end_date == JAN_15
    assert kept_row is not None and kept_row.end_date is None
