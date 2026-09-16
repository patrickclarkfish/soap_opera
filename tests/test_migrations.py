"""Tests for the storage migration runner.

These exercise the runner directly against a bare SQLAlchemy engine -- no
Home Assistant instance needed -- so this stays cheap to run as more
migrations are added.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.storage import database
from custom_components.soap_opera.storage.migrations import SCHEMA_VERSION


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
