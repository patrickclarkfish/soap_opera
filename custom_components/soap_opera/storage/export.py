"""Export every table to CSV or JSON.

This is the disaster recovery, the safety net for every later schema change,
and the owner's way to get their animal's history out. So it exports all
of it -- every table, the record and the telemetry and the schema version
alike -- not a curated view.

Guarantees:
- One consistent snapshot: every table is read inside one transaction.
- Atomic: files are written to a sibling ".partial" directory and renamed
  into place, so a failed export never leaves a half-written directory
  that looks complete.
- Never overwrites: the destination must not already exist.

Blocking: call via hass.async_add_executor_job.
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from .database import (
    administration_table,
    capture_event_table,
    medication_table,
    regimen_table,
    subjects_table,
)
from .migrations import SCHEMA_VERSION
from .tables import schema_version_table

# Every table in the database, as database.py's current-shape definitions,
# so values come out typed (UUIDs with dashes, dates as ISO strings, JSON as
# JSON) rather than as SQLite's storage forms. test_export checks this
# covers every table the migrations create: a migration adding a table must
# add it here too.
EXPORT_TABLES: tuple[sa.Table, ...] = (
    schema_version_table,
    subjects_table,
    medication_table,
    regimen_table,
    administration_table,
    capture_event_table,
)

MANIFEST_FILENAME = "manifest.json"


class ExportFormat(StrEnum):
    CSV = "csv"
    JSON = "json"


class ExportDestinationExistsError(FileExistsError):
    """Export never overwrites."""


def _plain(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _csv_cell(value: Any) -> Any:
    value = _plain(value)
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _read_all(engine: sa.engine.Engine) -> dict[str, list[dict[str, Any]]]:
    with engine.begin() as conn:
        return {
            table.name: [
                row._asdict()
                for row in conn.execute(
                    sa.select(table).order_by(*table.primary_key.columns)
                )
            ]
            for table in EXPORT_TABLES
        }


def _write_csv(directory: Path, tables: dict[str, list[dict[str, Any]]]) -> None:
    by_name = {table.name: table for table in EXPORT_TABLES}
    for name, rows in tables.items():
        columns = [column.name for column in by_name[name].columns]
        with (directory / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for row in rows:
                writer.writerow([_csv_cell(row[column]) for column in columns])
            f.flush()
            os.fsync(f.fileno())


def _write_json(path: Path, document: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, default=_plain)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def export_database(
    engine: sa.engine.Engine, destination: Path, export_format: ExportFormat
) -> dict[str, int]:
    """Write every table under `destination` (a new directory). Returns
    {table: row_count}.

    CSV: one <table>.csv per table, plus manifest.json. JSON: tables.json
    holding every table, plus manifest.json. The manifest records the schema
    version, so an export can be read back correctly after later migrations
    have changed the shape.
    """
    if destination.exists():
        raise ExportDestinationExistsError(f"{destination} already exists")
    partial = destination.with_name(destination.name + ".partial")
    if partial.exists():
        # Left behind by an export that failed partway; never a real export.
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    try:
        tables = _read_all(engine)
        counts = {name: len(rows) for name, rows in tables.items()}
        if export_format is ExportFormat.CSV:
            _write_csv(partial, tables)
        else:
            _write_json(partial / "tables.json", {"tables": tables})
        _write_json(
            partial / MANIFEST_FILENAME,
            {
                "integration": "soap_opera",
                "schema_version": SCHEMA_VERSION,
                "exported_at": datetime.now(UTC).isoformat(),
                "format": str(export_format),
                "row_counts": counts,
            },
        )
        partial.rename(destination)
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    return counts
