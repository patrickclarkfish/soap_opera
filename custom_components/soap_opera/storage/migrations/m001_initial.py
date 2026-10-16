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

from ..tables import schema_version_table


def apply(conn: Connection) -> None:
    # Shared with database.py -- see tables.py for why this one table is
    # safe to share instead of each owning its own frozen snapshot.
    schema_version_table.create(conn, checkfirst=True)

    metadata = sa.MetaData()
    sa.Table(
        "subjects",
        metadata,
        # UUIDv7 is the convention for every domain record's primary key
        # from here on, not just this table. Mainly because the id an animal
        # gets in this SQLite database is the same id it gets in the
        # (future) Postgres mirror, with no remapping step when the mirror
        # is rebuilt. Being time-ordered also helps, but less than it might
        # look: a non-INTEGER primary key does not order a SQLite table --
        # the table stays a rowid B-tree, and `id` gets a separate unique
        # index. It is that index whose inserts append rather than fragment
        # the way random UUIDv4 keys would. (Not a reason to reach for
        # WITHOUT ROWID: the table's own B-tree is already append-only.)
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        sa.Column("config_entry_id", sa.Text, nullable=False, unique=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("species", sa.Text, nullable=False),
        sa.Column("breed", sa.Text, nullable=True),
        sa.Column("date_of_birth", sa.Text, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )
    metadata.create_all(conn)
