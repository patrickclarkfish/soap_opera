"""Tests for the storage migration runner.

These exercise the runner directly against a bare SQLAlchemy engine -- no
Home Assistant instance needed -- so this stays cheap to run as more
migrations are added.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.storage import database
from custom_components.soap_opera.storage.migrations import MIGRATIONS, SCHEMA_VERSION


def _engine() -> sa.engine.Engine:
    return sa.create_engine("sqlite:///:memory:", future=True)


def test_migrate_from_zero_walks_to_current_version() -> None:
    engine = _engine()
    assert database._get_current_version(engine) == 0

    result = database.run_migrations(engine)

    assert result == SCHEMA_VERSION
    assert database._get_current_version(engine) == SCHEMA_VERSION

    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    assert "schema_version" in tables
    assert "subjects" in tables


def test_migrate_is_idempotent_once_current() -> None:
    engine = _engine()
    database.run_migrations(engine)

    result = database.run_migrations(engine)

    assert result == SCHEMA_VERSION
    with engine.connect() as conn:
        row_count = conn.execute(
            sa.select(sa.func.count()).select_from(database.schema_version_table)
        ).scalar_one()
    assert row_count == SCHEMA_VERSION


def test_downgrade_guard_raises_when_db_is_newer_than_code() -> None:
    engine = _engine()
    database.run_migrations(engine)

    with engine.begin() as conn:
        conn.execute(
            database.schema_version_table.insert().values(
                version=SCHEMA_VERSION + 1, applied_at="future"
            )
        )

    with pytest.raises(database.SchemaTooNewError):
        database.run_migrations(engine)


def test_recovers_when_crash_leaves_ddl_applied_but_no_version_row() -> None:
    """A crash between a migration's DDL and its version-row commit must be
    resumable: the next run should recognize the DDL already happened and
    just catch the version row up, not blow up or double-apply.
    """
    engine = _engine()

    # Simulate the crash: migration 1's DDL ran, but the process died before
    # the version row was ever written.
    with engine.connect() as conn:
        MIGRATIONS[1](conn)
    assert database._get_current_version(engine) == 0

    result = database.run_migrations(engine)

    assert result == SCHEMA_VERSION
    assert database._get_current_version(engine) == SCHEMA_VERSION
    inspector = sa.inspect(engine)
    assert "subjects" in inspector.get_table_names()


def test_subjects_table_matches_what_migration_001_actually_creates() -> None:
    """database.py deliberately keeps its own copy of the `subjects` shape
    rather than sharing migration 001's (see tables.py and database.py for
    why -- unlike schema_version, this table can be altered by a later
    migration, and sharing one object would break the incremental-migration
    model the moment that happens). Nothing enforces the two copies match
    except a code comment; this test is that enforcement.
    """
    engine = _engine()
    database.run_migrations(engine)

    inspector = sa.inspect(engine)
    reflected = {col["name"]: col for col in inspector.get_columns("subjects")}
    declared = {col.name: col for col in database.subjects_table.columns}

    assert set(reflected) == set(declared)
    for name, declared_col in declared.items():
        assert reflected[name]["nullable"] == declared_col.nullable, name


def test_migration_ddl_does_not_survive_a_rolled_back_transaction() -> None:
    """The real bug behind the scenario above: pysqlite autocommits DDL
    before it even reaches the transaction, so `with engine.begin(): ...`
    around a migration is not actually atomic for CREATE TABLE. This test
    isolates that directly -- unlike the recovery test above, it cannot be
    masked by metadata.create_all()'s checkfirst=True, because it never
    re-runs the DDL; it only checks whether a rollback undid it.

    Before the fix: fails (the tables exist despite the rollback).
    After the fix: passes.
    """

    class SimulatedCrash(Exception):
        pass

    engine = _engine()
    # Normally applied by run_migrations() itself; done explicitly here since
    # this test calls a migration directly to isolate the DDL/transaction
    # interaction from run_migrations' own recovery logic.
    database._configure_sqlite_engine(engine)

    with pytest.raises(SimulatedCrash), engine.begin() as conn:
        MIGRATIONS[1](conn)
        raise SimulatedCrash("crash after DDL, before the version row commits")

    inspector = sa.inspect(engine)
    tables = inspector.get_table_names()
    assert "subjects" not in tables
    assert "schema_version" not in tables
