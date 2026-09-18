"""Shared SQLite database: connection handling, schema version, migration runner.

Modeled on Home Assistant's own recorder migration flow
(homeassistant/components/recorder/migration.py): the current version lives in
a table inside the database itself (never cached on a config entry, since a
second target such as Postgres will track its own version independently),
migrations are applied one increment at a time, and each step's version row is
committed immediately so a crash mid-chain resumes where it left off.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from homeassistant.core import HomeAssistant
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.pool import ConnectionPoolEntry

from .migrations import MIGRATIONS, SCHEMA_VERSION
from .tables import schema_version_table

_LOGGER = logging.getLogger(__name__)

DB_FILENAME = "soap_opera.db"

# The current cumulative shape of `subjects`, for querying and writing --
# deliberately a *separate* definition from what migration 001 creates (see
# tables.py's docstring for why schema_version is the one table safe to
# share instead). If a later migration adds a column here, update this copy
# in the same change that adds the migration.
subjects_table = sa.Table(
    "subjects",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
    sa.Column("config_entry_id", sa.Text, nullable=False, unique=True),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("species", sa.Text, nullable=False),
    sa.Column("breed", sa.Text, nullable=True),
    sa.Column("date_of_birth", sa.Text, nullable=True),
    sa.Column("created_at", sa.Text, nullable=False),
)


class SchemaTooNewError(RuntimeError):
    """The database's schema version is newer than this code understands.

    Forward-only: we refuse to guess at a downgrade path.
    """


def _sqlite_set_isolation_level(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    """Hand transaction control entirely to `_sqlite_begin_explicit` below.

    pysqlite opens its own implicit transaction before DML but never before
    DDL, so a CREATE TABLE (or ALTER TABLE) autocommits the instant it runs
    regardless of SQLAlchemy's "BEGIN"/"COMMIT" bracketing -- `with
    engine.begin(): ...` around a migration is not actually atomic for DDL
    without this. This is SQLAlchemy's documented recipe for it (see the
    SQLite dialect docs, "Serializable isolation / Savepoints / Transactional
    DDL").
    """
    dbapi_connection.isolation_level = None


def _sqlite_begin_explicit(conn: Connection) -> None:
    # IMMEDIATE, not a plain deferred BEGIN: upsert_subject does a SELECT
    # then conditionally writes inside the same transaction, and a deferred
    # BEGIN only takes a shared (read) lock up front, so two connections
    # doing that concurrently can each hold a shared lock and then fail to
    # upgrade to a write lock -- "database is locked" even with busy_timeout
    # set, since that's a lock-upgrade conflict, not a wait-for-the-writer
    # one. IMMEDIATE takes the write lock at BEGIN, so a second connection
    # just waits for it (bounded by busy_timeout) instead of racing to
    # upgrade.
    conn.exec_driver_sql("BEGIN IMMEDIATE")


def _sqlite_set_pragmas(
    dbapi_connection: DBAPIConnection, connection_record: ConnectionPoolEntry
) -> None:
    cursor = dbapi_connection.cursor()
    try:
        # Must be set before migration 002 adds any foreign keys: SQLite
        # silently ignores FK constraints with this off, which would ship a
        # schema that looks referentially safe and is not.
        cursor.execute("PRAGMA foreign_keys=ON")
        # Executor jobs run on separate threads, each holding its own pooled
        # connection. In the default rollback-journal mode, a second writer
        # just gets "database is locked"; WAL allows concurrent readers
        # alongside a single writer.
        cursor.execute("PRAGMA journal_mode=WAL")
        # Paired with WAL: wait for a lock instead of failing immediately.
        # 5s is long enough to ride out a concurrent writer, short enough to
        # fail fast if something is actually stuck.
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _configure_sqlite_engine(engine: sa.engine.Engine) -> None:
    """Make SQLite behave: real transactional DDL, enforced foreign keys,
    WAL, and a busy timeout. No-op for any other dialect -- none of this is
    needed (or correct) for the future Postgres mirror.

    Idempotent: safe to call more than once on the same engine.
    """
    if engine.dialect.name != "sqlite":
        return
    if not event.contains(engine, "connect", _sqlite_set_isolation_level):
        event.listen(engine, "connect", _sqlite_set_isolation_level)
    if not event.contains(engine, "begin", _sqlite_begin_explicit):
        event.listen(engine, "begin", _sqlite_begin_explicit)
    if not event.contains(engine, "connect", _sqlite_set_pragmas):
        event.listen(engine, "connect", _sqlite_set_pragmas)


def _should_back_up_before_migrating(current_version: int, target_version: int) -> bool:
    """True for an existing, non-fresh database about to be migrated.

    Not reachable through connect_and_migrate() in practice yet: with only
    migration 001, SCHEMA_VERSION is 1, so current_version is always either 0
    (fresh install, nothing to lose) or already == target_version (nothing
    to migrate) -- this starts actually triggering the moment migration 002
    ships. Kept as its own function so the boundary conditions are testable
    now, with synthetic version numbers, rather than waiting for a second
    migration to exist to test it for real.
    """
    return 0 < current_version < target_version


def _get_current_version(engine: sa.engine.Engine) -> int:
    """Return the schema version recorded in the database, or 0 if unset."""
    inspector = sa.inspect(engine)
    if "schema_version" not in inspector.get_table_names():
        return 0
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(schema_version_table.c.version)
            .order_by(schema_version_table.c.change_id.desc())
            .limit(1)
        ).first()
    return row[0] if row is not None else 0


def run_migrations(engine: sa.engine.Engine) -> int:
    """Bring the database up to SCHEMA_VERSION, one step at a time.

    Blocking. Callers outside tests must run this via
    hass.async_add_executor_job.
    """
    _configure_sqlite_engine(engine)

    current = _get_current_version(engine)

    if current > SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"Database schema version {current} is newer than this version "
            f"of the soap_opera integration supports (max {SCHEMA_VERSION}). "
            "Update the integration before opening this database."
        )

    for target in range(current + 1, SCHEMA_VERSION + 1):
        _LOGGER.info("Migrating soap_opera database to schema version %s", target)
        apply_update = MIGRATIONS[target]
        with engine.begin() as conn:
            apply_update(conn)
            conn.execute(
                schema_version_table.insert().values(
                    version=target,
                    applied_at=datetime.now(UTC).isoformat(),
                )
            )

    return SCHEMA_VERSION


def upsert_subject(
    engine: sa.engine.Engine,
    *,
    config_entry_id: str,
    name: str,
    species: str,
    breed: str | None,
    date_of_birth: str | None,
) -> None:
    """Create or update the subject row for a config entry. Blocking.

    Keyed on config_entry_id so a renamed entry (or any edited field)
    updates the existing row instead of creating a duplicate.
    """
    with engine.begin() as conn:
        existing_id = conn.execute(
            sa.select(subjects_table.c.id).where(
                subjects_table.c.config_entry_id == config_entry_id
            )
        ).first()

        if existing_id is None:
            conn.execute(
                subjects_table.insert().values(
                    config_entry_id=config_entry_id,
                    name=name,
                    species=species,
                    breed=breed,
                    date_of_birth=date_of_birth,
                    created_at=datetime.now(UTC).isoformat(),
                )
            )
        else:
            conn.execute(
                subjects_table.update()
                .where(subjects_table.c.config_entry_id == config_entry_id)
                .values(
                    name=name,
                    species=species,
                    breed=breed,
                    date_of_birth=date_of_birth,
                )
            )


class Database:
    """Owns the single shared SQLite database used by every config entry.

    Set up by whichever entry loads first, refcounted, torn down when the
    last entry unloads. See async_setup_entry / async_unload_entry in
    __init__.py.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._engine: sa.engine.Engine | None = None

    def connect_and_migrate(self) -> None:
        """Open the engine, back up a non-fresh database before migrating it,
        and run any pending migrations. Blocking.
        """
        db_path = self._hass.config.path(DB_FILENAME)
        engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
        _configure_sqlite_engine(engine)

        current = _get_current_version(engine)
        if _should_back_up_before_migrating(current, SCHEMA_VERSION):
            # WAL mode (enabled above) can leave recently-committed data
            # sitting in the -wal sidecar file rather than in the main file.
            # Checkpoint it into the main file first so a plain copy of that
            # one file is a complete, self-consistent snapshot -- otherwise
            # the backup can silently miss recent writes.
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
            backup_path = f"{db_path}.backup"
            shutil.copy2(db_path, backup_path)
            _LOGGER.info(
                "Backed up soap_opera database to %s before migrating from version %s",
                backup_path,
                current,
            )

        run_migrations(engine)
        self._engine = engine

    def upsert_subject(
        self,
        config_entry_id: str,
        name: str,
        species: str,
        breed: str | None,
        date_of_birth: str | None,
    ) -> None:
        """Create or update the subject row for a config entry. Blocking."""
        upsert_subject(
            self.engine,
            config_entry_id=config_entry_id,
            name=name,
            species=species,
            breed=breed,
            date_of_birth=date_of_birth,
        )

    def close(self) -> None:
        """Dispose of the engine. Blocking."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    @property
    def engine(self) -> sa.engine.Engine:
        if self._engine is None:
            raise RuntimeError("Database has not been set up yet")
        return self._engine
