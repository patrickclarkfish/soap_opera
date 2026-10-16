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
import os
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
# Both live in the HA config directory, resolved via hass.config.path().
AUDIT_LOG_FILENAME = "soap_opera_audit.jsonl"

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


# Current cumulative shapes of `medication` and `regimen`, separate from
# migration 002's frozen snapshot for the same reason as subjects_table.
# Column meanings are documented in m002_medication_regimen.py.
medication_table = sa.Table(
    "medication",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
    sa.Column("generic_name", sa.Text, nullable=False),
    sa.Column("brand_name", sa.Text, nullable=True),
    sa.Column("strength", sa.Float, nullable=True),
    sa.Column("unit", sa.Text, nullable=False),
    sa.Column("form", sa.Text, nullable=False),
    sa.Column("concentration", sa.Float, nullable=True),
    sa.Column("route", sa.Text, nullable=False),
    sa.Column("requires_refrigeration", sa.Boolean, nullable=False),
    sa.Column("shake_well", sa.Boolean, nullable=False),
    sa.Column("with_food", sa.Boolean, nullable=True),
    sa.Column("created_at", sa.Text, nullable=False),
)

regimen_table = sa.Table(
    "regimen",
    sa.MetaData(),
    sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
    # No sa.ForeignKey here: subjects_table lives in a different MetaData,
    # and the constraint itself is the database's job (migration 002
    # created it). This copy is for querying and writing.
    sa.Column("subject_id", sa.Uuid(), nullable=False),
    sa.Column("medication_id", sa.Uuid(), nullable=False),
    sa.Column("config_subentry_id", sa.Text, nullable=True),
    sa.Column("dose_units", sa.Float, nullable=False),
    sa.Column("unit_strength", sa.Float, nullable=False),
    sa.Column("frequency", sa.Text, nullable=True),
    sa.Column("scheduled_times", sa.JSON, nullable=True),
    sa.Column("prn", sa.Boolean, nullable=False),
    sa.Column("min_interval_seconds", sa.Integer, nullable=True),
    sa.Column("dose_variable", sa.Boolean, nullable=False),
    sa.Column("dose_increment", sa.Float, nullable=True),
    sa.Column("dose_max_single", sa.Float, nullable=True),
    sa.Column("dose_max_daily", sa.Float, nullable=True),
    sa.Column("indication", sa.Text, nullable=True),
    sa.Column("prescribing_vet", sa.Text, nullable=True),
    sa.Column("start_date", sa.Date, nullable=False),
    sa.Column("end_date", sa.Date, nullable=True),
    sa.Column("supersedes_id", sa.Uuid(), nullable=True),
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

    A fresh install (0) has nothing to lose, and an up-to-date database has
    nothing about to change.
    """
    return 0 < current_version < target_version


def _back_up_database(
    engine: sa.engine.Engine, db_path: str, current_version: int
) -> None:
    """Snapshot the database to <db_path>.v<current_version>.backup. Blocking.

    VACUUM INTO, not checkpoint-then-copy: it reads through SQLite itself,
    so the snapshot includes whatever is still only in the -wal sidecar, is
    written atomically, and raises on any failure rather than producing a
    silently incomplete copy.

    Never overwrites. An existing backup for this same source version can
    only have come from an earlier attempt at this same migration -- one
    that failed, so the live database may now be partially migrated or
    otherwise damaged, and the earlier backup is the trustworthy one. Keep
    it and go ahead with the migration. (VACUUM INTO also refuses an
    existing destination itself, so a backup appearing between this check
    and the VACUUM still can't be clobbered; it would fail setup loudly
    instead.)
    """
    backup_path = f"{db_path}.v{current_version}.backup"
    if os.path.exists(backup_path):
        _LOGGER.warning(
            "A backup from an earlier attempt to migrate the soap_opera "
            "database from schema version %s already exists at %s; keeping "
            "it rather than replacing it with a copy of the current database",
            current_version,
            backup_path,
        )
        return

    # Straight on the DBAPI connection: VACUUM cannot run inside a
    # transaction, and any SQLAlchemy-level execute would first fire
    # _sqlite_begin_explicit's BEGIN IMMEDIATE. The connect listener has
    # already put pysqlite in autocommit (isolation_level=None), so this
    # runs outside one.
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        try:
            cursor.execute("VACUUM INTO ?", (backup_path,))
        finally:
            cursor.close()
    finally:
        raw.close()
    _LOGGER.info(
        "Backed up soap_opera database to %s before migrating from version %s",
        backup_path,
        current_version,
    )


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

        On any failure the engine is disposed and self.engine stays unset.
        """
        db_path = self._hass.config.path(DB_FILENAME)
        engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
        _configure_sqlite_engine(engine)

        try:
            current = _get_current_version(engine)
            if _should_back_up_before_migrating(current, SCHEMA_VERSION):
                _back_up_database(engine, db_path, current)
            run_migrations(engine)
        except BaseException:
            # Nothing else holds a reference to this engine yet, so if it
            # isn't disposed here its pooled connections (and their file
            # handles and WAL locks) leak for the life of the process.
            engine.dispose()
            raise
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
