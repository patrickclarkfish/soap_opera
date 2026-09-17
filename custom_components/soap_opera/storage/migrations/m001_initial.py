"""Schema version 1: version tracking plus the subject table.

Deliberately minimal. This migration exists to prove the migration runner
works end to end -- sequential application, per-step commit, downgrade guard
-- while there is no real data at stake yet. The rest of the data model
(regimens, doses, observations, inventory, ...) arrives in migrations 002 and
later, each adding to what is already here. Never edit this file once it has
shipped; add a new migration instead.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection


def apply(conn: Connection) -> None:
    metadata = sa.MetaData()

    sa.Table(
        "schema_version",
        metadata,
        # Internal migration bookkeeping, not a domain record -- stays a
        # plain autoincrement counter rather than following the UUID
        # convention below.
        sa.Column("change_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("applied_at", sa.Text, nullable=False),
    )

    sa.Table(
        "subjects",
        metadata,
        # UUIDv7 (time-ordered, so inserts still append rather than
        # fragmenting SQLite's B-tree the way random UUIDv4 keys would) is
        # the convention for every domain record's primary key from here on,
        # not just this table. It also means the id an animal gets in this
        # SQLite database is the same id it gets in the (future) Postgres
        # mirror, with no remapping step when the mirror is rebuilt.
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        sa.Column("config_entry_id", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("species", sa.Text, nullable=False),
        sa.Column("breed", sa.Text, nullable=True),
        sa.Column("date_of_birth", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    metadata.create_all(conn)
