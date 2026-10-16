"""Schema version 2: medication and regimen.

A medication is the drug itself, shared across animals. A regimen is one
version of a prescription of that drug for one subject. Regimens are never
UPDATEd on a dosing change: the current row is end-dated and a new row
inserted with supersedes_id pointing back at it (see storage/regimens.py).

Frozen snapshot of what this migration creates. Never import these Table
objects anywhere else, and never edit this file once it has shipped; add a
new migration instead. database.py keeps its own copy of the current
cumulative shape.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.engine import Connection


def apply(conn: Connection) -> None:
    metadata = sa.MetaData()
    # Referenced by regimen.subject_id. Only the column the foreign key
    # needs; this is not a definition of `subjects` and creates nothing
    # (create_all below is restricted to the two new tables).
    subjects = sa.Table(
        "subjects", metadata, sa.Column("id", sa.Uuid(), primary_key=True)
    )

    medication = sa.Table(
        "medication",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        sa.Column("generic_name", sa.Text, nullable=False),
        sa.Column("brand_name", sa.Text, nullable=True),
        # Amount of active ingredient per tablet / capsule / patch, in
        # `unit`. Null for liquids, which use `concentration` instead.
        sa.Column("strength", sa.Float, nullable=True),
        sa.Column("unit", sa.Text, nullable=False),
        # No CHECK constraint on form or route, deliberately: both are open
        # vocabularies that will grow, and SQLite cannot alter a CHECK
        # without rebuilding the table. Validated in code instead.
        sa.Column("form", sa.Text, nullable=False),
        # Liquids only: `unit` per mL.
        sa.Column("concentration", sa.Float, nullable=True),
        sa.Column("route", sa.Text, nullable=False),
        sa.Column("requires_refrigeration", sa.Boolean, nullable=False),
        sa.Column("shake_well", sa.Boolean, nullable=False),
        # Tri-state: true = give with food, false = give without food,
        # null = no preference.
        sa.Column("with_food", sa.Boolean, nullable=True),
        sa.Column("created_at", sa.Text, nullable=False),
    )

    sa.Table(
        "regimen",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True, default=uuid.uuid7),
        sa.Column(
            "subject_id", sa.Uuid(), sa.ForeignKey(subjects.c.id), nullable=False
        ),
        sa.Column(
            "medication_id", sa.Uuid(), sa.ForeignKey(medication.c.id), nullable=False
        ),
        # The config subentry this regimen was collected through. Shared by
        # every row in one supersede chain, so not unique.
        sa.Column("config_subentry_id", sa.Text, nullable=True, index=True),
        # Standard dose, in dosage units (tablets, capsules, mL, patches).
        sa.Column("dose_units", sa.Float, nullable=False),
        # Amount of active ingredient per dosage unit, in the medication's
        # unit, as prescribed. Copied here rather than read through from
        # medication so the prescription records what it actually meant.
        sa.Column("unit_strength", sa.Float, nullable=False),
        sa.Column("frequency", sa.Text, nullable=True),
        # JSON list of "HH:MM" local times. Consumed by scheduling (phase 2).
        sa.Column("scheduled_times", sa.JSON, nullable=True),
        sa.Column("prn", sa.Boolean, nullable=False),
        sa.Column("min_interval_seconds", sa.Integer, nullable=True),
        sa.Column("dose_variable", sa.Boolean, nullable=False),
        # In dosage units, like dose_units (e.g. 0.5 for half a tablet).
        sa.Column("dose_increment", sa.Float, nullable=True),
        # Ceilings are in the medication's unit (mg, nearly always), not
        # dosage units, so they mean the same thing across strengths.
        sa.Column("dose_max_single", sa.Float, nullable=True),
        sa.Column("dose_max_daily", sa.Float, nullable=True),
        sa.Column("indication", sa.Text, nullable=True),
        sa.Column("prescribing_vet", sa.Text, nullable=True),
        # Half-open [start_date, end_date): in force on d when
        # start_date <= d and (end_date is null or d < end_date).
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=True),
        sa.Column(
            "supersedes_id", sa.Uuid(), sa.ForeignKey("regimen.id"), nullable=True
        ),
        sa.Column("created_at", sa.Text, nullable=False),
        sa.Index("ix_regimen_subject_medication", "subject_id", "medication_id"),
    )

    metadata.create_all(
        conn, tables=[metadata.tables["medication"], metadata.tables["regimen"]]
    )
