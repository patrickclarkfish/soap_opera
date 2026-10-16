"""Schema version 3: administration and capture_event.

administration is the medical record of a dose event: permanent, never
purged. capture_event is raw capture telemetry -- one row per call into the
log_dose funnel, including duplicates that collapsed into an existing
administration. It is the one table here that is NOT part of the record;
like the JSONL audit log it is bounded telemetry, with retention arriving in
a later phase.

Frozen snapshot of what this migration creates. Never import these Table
objects anywhere else, and never edit this file once it has shipped.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection

# Closed on purpose and complete from the start, so the CHECK below never
# needs widening (which SQLite could only do by rebuilding the table).
# given_late and missed are computed by the system from a schedule; the
# funnel refuses them from callers.
STATUSES = (
    "given",
    "given_late",
    "refused",
    "vomited_within_30min",
    "held_per_vet",
    "skipped_other",
    "missed",
)


def apply(conn: Connection) -> None:
    metadata = sa.MetaData()
    # Referenced by administration.regimen_id. Only the column the foreign
    # key needs; create_all below is restricted to the new tables.
    regimen = sa.Table(
        "regimen", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )

    administration = sa.Table(
        "administration",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        sa.Column("regimen_id", sa.Uuid(), sa.ForeignKey(regimen.c.id), nullable=False),
        # Timestamps are ISO 8601 UTC text with microseconds, so they sort
        # and compare correctly as strings. Null for PRN doses.
        sa.Column("scheduled_for", sa.Text, nullable=True),
        sa.Column("administered_at", sa.Text, nullable=False),
        # Recorded three ways on purpose: what was counted out of the bottle
        # (dose_units), what each unit was (unit_strength), and the
        # canonical amount (dose_mg = dose_units * unit_strength, in the
        # medication's unit). Units alone silently change meaning when a
        # refill arrives at a different strength; the amount alone loses
        # what was physically given.
        sa.Column("dose_units", sa.Float, nullable=False),
        sa.Column("unit_strength", sa.Float, nullable=False),
        sa.Column("dose_mg", sa.Float, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("administered_by", sa.Text, nullable=True),
        sa.Column("capture_method", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        # How many capture events collapsed into this administration (1 +
        # the number of duplicates).
        sa.Column("source_event_count", sa.Integer, nullable=False),
        # Null when no guardrail tripped. Otherwise a JSON list, one object
        # per tripped guardrail, recording its limit, the actual value, and
        # whether the caller overrode it. A warning is clinical data either
        # way.
        sa.Column("guardrail_flags", sa.JSON, nullable=True),
        sa.Column(
            "amends_id", sa.Uuid(), sa.ForeignKey("administration.id"), nullable=True
        ),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_administration_status",
        ),
        sa.Index("ix_administration_regimen_time", "regimen_id", "administered_at"),
    )

    sa.Table(
        "capture_event",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        # UNIQUE is the idempotency mechanism: the funnel inserts and treats
        # an IntegrityError as "already recorded". Null on the telemetry row
        # written for a duplicate (SQLite UNIQUE allows many NULLs); that
        # row's payload carries the key it collided on.
        sa.Column("idempotency_key", sa.Text, nullable=True, unique=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_ref", sa.Text, nullable=True),
        sa.Column("received_at", sa.Text, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column(
            "collapsed_into_administration_id",
            sa.Uuid(),
            sa.ForeignKey(administration.c.id),
            nullable=True,
        ),
        sa.Column("was_duplicate", sa.Boolean, nullable=False),
    )

    metadata.create_all(
        conn,
        tables=[metadata.tables["administration"], metadata.tables["capture_event"]],
    )
