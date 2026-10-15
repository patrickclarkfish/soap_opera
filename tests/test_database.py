"""Tests for storage/database.py beyond the migration runner itself:
the subjects upsert and the SQLite connection PRAGMAs.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from custom_components.soap_opera.storage import database


def _engine() -> sa.engine.Engine:
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    database.run_migrations(engine)
    return engine


def _file_engine(tmp_path: Path) -> sa.engine.Engine:
    # WAL is a no-op on :memory: databases (they silently stay in "memory"
    # journal mode), so this test needs a real file to mean anything.
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)
    database.run_migrations(engine)
    return engine


def test_upsert_subject_inserts_new_row() -> None:
    engine = _engine()

    database.upsert_subject(
        engine,
        config_entry_id="entry-1",
        name="Biscuit",
        species="Dog",
        breed="Corgi",
        date_of_birth="2020-01-01",
    )

    with engine.connect() as conn:
        row = conn.execute(
            sa.select(database.subjects_table).where(
                database.subjects_table.c.config_entry_id == "entry-1"
            )
        ).one()

    assert row.name == "Biscuit"
    assert row.species == "Dog"
    assert row.breed == "Corgi"


def test_upsert_subject_updates_existing_row_on_rename() -> None:
    engine = _engine()
    database.upsert_subject(
        engine,
        config_entry_id="entry-1",
        name="Biscuit",
        species="Dog",
        breed="Corgi",
        date_of_birth=None,
    )

    database.upsert_subject(
        engine,
        config_entry_id="entry-1",
        name="Biscuit II",
        species="Dog",
        breed="Corgi",
        date_of_birth=None,
    )

    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(database.subjects_table).where(
                database.subjects_table.c.config_entry_id == "entry-1"
            )
        ).all()

    assert len(rows) == 1
    assert rows[0].name == "Biscuit II"


def test_sqlite_pragmas_are_set_on_connect(tmp_path: Path) -> None:
    engine = _file_engine(tmp_path)

    with engine.connect() as conn:
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()

    assert foreign_keys == 1
    assert journal_mode == "wal"


def test_should_back_up_before_migrating() -> None:
    # Fresh install: nothing to lose.
    assert database._should_back_up_before_migrating(0, 1) is False
    # Already current: nothing about to be migrated.
    assert database._should_back_up_before_migrating(1, 1) is False
    # The actual case this exists for: an existing database about to change.
    assert database._should_back_up_before_migrating(1, 2) is True
