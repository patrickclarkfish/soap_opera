"""Tests for Database.connect_and_migrate's pre-migration backup and its
cleanup on failure.

These patch in a synthetic migration 2 so the 0 < current < target branch is
actually reachable -- with only migration 001 shipped, no real install can
hit it yet, and the first time one does is the first time a real database is
at stake.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.storage import database
from custom_components.soap_opera.storage.database import DB_FILENAME, Database
from custom_components.soap_opera.storage.migrations import MIGRATIONS
from sqlalchemy.engine import Connection


def _fake_hass(config_dir: Path) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(path=lambda *parts: str(config_dir.joinpath(*parts)))
    )


def _create_v1_database(db_path: Path) -> None:
    """A real, already-migrated version-1 database with one subject in it.

    The subject is written through a WAL-mode engine that is left *open*
    while the backup runs, so it is still sitting in the -wal sidecar
    rather than the main file -- a plain file copy of the main file would
    miss it.
    """
    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    database.run_migrations(engine)
    database.upsert_subject(
        engine,
        config_entry_id="entry-1",
        name="Biscuit",
        species="Dog",
        breed=None,
        date_of_birth=None,
    )
    _OPEN_ENGINES.append(engine)


_OPEN_ENGINES: list[sa.engine.Engine] = []


@pytest.fixture(autouse=True)
def _dispose_open_engines() -> Any:
    yield
    while _OPEN_ENGINES:
        _OPEN_ENGINES.pop().dispose()


def _install_migration_2(
    monkeypatch: pytest.MonkeyPatch, apply: Callable[[Connection], None]
) -> None:
    monkeypatch.setattr(database, "MIGRATIONS", {**MIGRATIONS, 2: apply})
    monkeypatch.setattr(database, "SCHEMA_VERSION", 2)


def _create_probe_table(conn: Connection) -> None:
    conn.exec_driver_sql("CREATE TABLE probe (id INTEGER PRIMARY KEY)")


def _subject_names(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT name FROM subjects")]


def test_backup_filename_carries_the_source_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / DB_FILENAME
    _create_v1_database(db_path)
    _install_migration_2(monkeypatch, _create_probe_table)

    db = Database(_fake_hass(tmp_path))
    db.connect_and_migrate()
    db.close()

    assert (tmp_path / f"{DB_FILENAME}.v1.backup").exists()
    assert not (tmp_path / f"{DB_FILENAME}.backup").exists()


def test_backup_is_a_readable_consistent_pre_migration_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / DB_FILENAME
    _create_v1_database(db_path)
    _install_migration_2(monkeypatch, _create_probe_table)

    db = Database(_fake_hass(tmp_path))
    db.connect_and_migrate()
    db.close()

    backup = tmp_path / f"{DB_FILENAME}.v1.backup"
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (
            1,
        )
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "probe" not in tables
    # Committed but still only in the -wal file when the backup was taken.
    assert _subject_names(backup) == ["Biscuit"]


def test_existing_backup_for_the_same_version_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / DB_FILENAME
    _create_v1_database(db_path)
    _install_migration_2(monkeypatch, _create_probe_table)
    backup = tmp_path / f"{DB_FILENAME}.v1.backup"
    backup.write_bytes(b"backup from an earlier attempt")

    db = Database(_fake_hass(tmp_path))
    db.connect_and_migrate()
    db.close()

    assert backup.read_bytes() == b"backup from an earlier attempt"
    assert "keeping it" in caplog.text
    # Migration still went ahead.
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT max(version) FROM schema_version").fetchone() == (
            2,
        )


def test_retry_after_failed_migration_keeps_the_original_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scenario the old `.backup` + copy2 got exactly wrong: a migration
    fails partway, something about the live database is now off, HA
    restarts, and the retry must not back up the damaged database over the
    only good pre-migration copy.
    """
    db_path = tmp_path / DB_FILENAME
    _create_v1_database(db_path)

    def failing_migration(conn: Connection) -> None:
        raise RuntimeError("migration 2 blew up")

    _install_migration_2(monkeypatch, failing_migration)
    db = Database(_fake_hass(tmp_path))
    with pytest.raises(RuntimeError, match="blew up"):
        db.connect_and_migrate()

    # Damage the live database the way a non-transactional partial failure
    # could, then restart with a migration that now succeeds.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO subjects (id, config_entry_id, name, species, created_at) "
            "VALUES (x'00', 'damaged', 'Damaged', 'Dog', 'now')"
        )
    _install_migration_2(monkeypatch, _create_probe_table)
    db = Database(_fake_hass(tmp_path))
    db.connect_and_migrate()
    db.close()

    assert _subject_names(tmp_path / f"{DB_FILENAME}.v1.backup") == ["Biscuit"]


def test_engine_is_disposed_when_migration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[sa.engine.Engine] = []
    real_create_engine = sa.create_engine

    def recording_create_engine(*args: Any, **kwargs: Any) -> sa.engine.Engine:
        engine = real_create_engine(*args, **kwargs)
        engine.dispose = Mock(wraps=engine.dispose)  # type: ignore[method-assign]
        created.append(engine)
        return engine

    def failing_migration(conn: Connection) -> None:
        raise RuntimeError("migration blew up")

    monkeypatch.setattr(database.sa, "create_engine", recording_create_engine)
    monkeypatch.setattr(database, "MIGRATIONS", {1: failing_migration})

    db = Database(_fake_hass(tmp_path))
    with pytest.raises(RuntimeError, match="blew up"):
        db.connect_and_migrate()

    assert len(created) == 1
    created[0].dispose.assert_called_once()  # type: ignore[attr-defined]
    with pytest.raises(RuntimeError, match="not been set up"):
        _ = db.engine
