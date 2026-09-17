"""The schema_version table, defined exactly once.

Needed by both database.py (to read the current version) and
migrations/m001_initial.py (to create it). It lives here, in a module that
depends on neither, to avoid a circular import: database.py imports
migrations.MIGRATIONS, and m001_initial would need to import database.py's
copy of this table otherwise.

This table is the one exception to "each migration owns a frozen snapshot of
what it creates, and database.py owns a separate copy for the current
cumulative shape" (see database.py's subjects_table for that normal case).
Sharing one definition is safe here specifically because schema_version is
locked in shape forever -- internal migration bookkeeping, not a domain
record, never altered by a later migration (see CLAUDE.md). A domain table
must NOT be shared this way: the moment a later migration alters it, a fresh
install would create the post-alteration shape in migration 001 via this
shared object and skip straight past whatever ALTER TABLE a later migration
was supposed to apply.
"""

from __future__ import annotations

import sqlalchemy as sa

schema_version_table = sa.Table(
    "schema_version",
    sa.MetaData(),
    sa.Column("change_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("applied_at", sa.Text, nullable=False),
)
