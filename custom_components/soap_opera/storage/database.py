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
from datetime import UTC, datetime

import sqlalchemy as sa
from homeassistant.core import HomeAssistant

from .migrations import MIGRATIONS, SCHEMA_VERSION

_LOGGER = logging.getLogger(__name__)

DB_FILENAME = "soap_opera.db"

# This table's own shape is pinned to what migration 1 creates (see
# m001_initial.py). It is defined again here, separately, because this is the
# read-side view used to query "what version is this database at" -- if a
# future migration ever needs to change schema_version itself, that happens
# as a normal additive migration and this definition is updated to match the
# latest shape, same as any other table.
schema_version_table = sa.Table(
    "schema_version",
    sa.MetaData(),
    sa.Column("change_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("applied_at", sa.Text, nullable=False),
)


class SchemaTooNewError(RuntimeError):
    """The database's schema version is newer than this code understands.

    Forward-only: we refuse to guess at a downgrade path.
    """


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
        """Open the engine and run any pending migrations. Blocking."""
        engine = sa.create_engine(
            f"sqlite:///{self._hass.config.path(DB_FILENAME)}", future=True
        )
        run_migrations(engine)
        self._engine = engine

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
