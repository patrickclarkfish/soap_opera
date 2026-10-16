"""Tests for the export: every table, typed values, both formats, atomic
and non-overwriting, and the admin-only service around it.
"""

from __future__ import annotations

import csv
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.const import CONF_SPECIES, DOMAIN
from custom_components.soap_opera.storage import (
    administrations,
    database,
    export,
    regimens,
)
from custom_components.soap_opera.storage.database import AUDIT_LOG_FILENAME
from custom_components.soap_opera.storage.export import (
    ExportDestinationExistsError,
    ExportFormat,
)
from custom_components.soap_opera.storage.migrations import SCHEMA_VERSION
from homeassistant.auth.models import User
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from pytest_homeassistant_custom_component.common import MockConfigEntry


@pytest.fixture
def engine() -> sa.engine.Engine:
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    database.run_migrations(engine)
    subject_id = database.upsert_subject(
        engine,
        config_entry_id="entry",
        name="Biscuit",
        species="Dog",
        breed=None,
        date_of_birth="2020-01-01",
    )
    with engine.begin() as conn:
        medication_id = regimens.insert_medication(
            conn,
            generic_name="gabapentin",
            strength=100,
            unit="mg",
            form="capsule",
            route="oral",
            with_food=True,
        )
        regimen_id = regimens.insert_regimen(
            conn,
            subject_id=subject_id,
            medication_id=medication_id,
            start_date=date(2026, 1, 1),
            dose_units=3,
            unit_strength=100,
            scheduled_times=["08:00", "20:00"],
            dose_max_single=200,
        )
    administrations.log_dose(
        engine,
        administrations.DoseRequest(
            regimen_id=regimen_id,
            administered_at=datetime(2026, 3, 2, 8, tzinfo=UTC),
            capture_method="service",
        ),
        bucket_seconds=120,
    )
    return engine


def test_export_covers_every_table_the_migrations_create() -> None:
    """A migration that adds a table must add it to EXPORT_TABLES too, or
    the export silently stops being complete."""
    engine = sa.create_engine("sqlite:///:memory:", future=True)
    database.run_migrations(engine)

    exported = {table.name for table in export.EXPORT_TABLES}
    assert exported == set(sa.inspect(engine).get_table_names())


def test_json_export_is_complete_and_typed(
    engine: sa.engine.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "out"

    counts = export.export_database(engine, destination, ExportFormat.JSON)

    assert counts == {
        "schema_version": SCHEMA_VERSION,
        "subjects": 1,
        "medication": 1,
        "regimen": 1,
        "administration": 1,
        "capture_event": 1,
    }
    tables = json.loads((destination / "tables.json").read_text())["tables"]
    regimen = tables["regimen"][0]
    uuid.UUID(regimen["id"])  # a real UUID string, not 32-char storage hex
    assert "-" in regimen["id"]
    assert regimen["start_date"] == "2026-01-01"
    assert regimen["scheduled_times"] == ["08:00", "20:00"]
    assert tables["medication"][0]["with_food"] is True
    administration = tables["administration"][0]
    assert administration["dose_mg"] == 300
    assert administration["guardrail_flags"][0]["guardrail"] == "max_single_dose"
    assert administration["regimen_id"] == regimen["id"]

    manifest = json.loads((destination / "manifest.json").read_text())
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["row_counts"] == counts


def test_csv_export_writes_one_file_per_table(
    engine: sa.engine.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "out"

    export.export_database(engine, destination, ExportFormat.CSV)

    files = {path.name for path in destination.iterdir()}
    assert files == {f"{t.name}.csv" for t in export.EXPORT_TABLES} | {"manifest.json"}
    with (destination / "regimen.csv").open(newline="") as f:
        (row,) = list(csv.DictReader(f))
    assert list(row) == [c.name for c in database.regimen_table.columns]
    assert row["prn"] == "false"
    assert row["end_date"] == ""
    assert json.loads(row["scheduled_times"]) == ["08:00", "20:00"]
    uuid.UUID(row["subject_id"])


def test_never_overwrites(engine: sa.engine.Engine, tmp_path: Path) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    (destination / "keep.txt").write_text("mine")

    with pytest.raises(ExportDestinationExistsError):
        export.export_database(engine, destination, ExportFormat.JSON)

    assert [p.name for p in destination.iterdir()] == ["keep.txt"]


def test_failed_export_leaves_nothing_that_looks_complete(
    engine: sa.engine.Engine, tmp_path: Path
) -> None:
    destination = tmp_path / "out"

    with (
        patch.object(export, "_write_json", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        export.export_database(engine, destination, ExportFormat.JSON)

    assert not destination.exists()
    assert not (tmp_path / "out.partial").exists()


def test_stale_partial_directory_is_replaced(
    engine: sa.engine.Engine, tmp_path: Path
) -> None:
    stale = tmp_path / "out.partial"
    stale.mkdir()
    (stale / "half.csv").write_text("x")

    export.export_database(engine, tmp_path / "out", ExportFormat.CSV)

    assert not stale.exists()
    assert not (tmp_path / "out" / "half.csv").exists()


# --- the service ------------------------------------------------------------


@pytest.fixture
async def loaded(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _export(hass: HomeAssistant, user: User, **data: Any) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "export",
        data,
        blocking=True,
        return_response=True,
        context=Context(user_id=user.id),
    )
    assert response is not None
    return dict(response)


async def test_service_exports_into_the_config_directory(
    hass: HomeAssistant,
    loaded: MockConfigEntry,
    hass_admin_user: User,
    hass_config_dir: str,
) -> None:
    response = await _export(hass, hass_admin_user, format="csv")

    path = Path(response["path"])
    assert path.is_relative_to(Path(hass_config_dir).resolve() / "soap_opera_exports")
    assert (path / "subjects.csv").exists()
    assert response["row_counts"]["subjects"] == 1
    audit = Path(hass_config_dir, AUDIT_LOG_FILENAME).read_text().splitlines()
    assert json.loads(audit[-1])["event"] == "export"


@pytest.mark.parametrize("path", ["../outside", "/etc/soap", "."])
async def test_service_refuses_paths_outside_the_config_directory(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_admin_user: User, path: str
) -> None:
    with pytest.raises(ServiceValidationError, match="inside"):
        await _export(hass, hass_admin_user, path=path)


async def test_service_refuses_an_existing_path(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_admin_user: User
) -> None:
    await _export(hass, hass_admin_user, path="exports/one")

    with pytest.raises(ServiceValidationError, match="already exists"):
        await _export(hass, hass_admin_user, path="exports/one")


async def test_service_is_admin_only(
    hass: HomeAssistant, loaded: MockConfigEntry, hass_read_only_user: User
) -> None:
    with pytest.raises(Unauthorized):
        await _export(hass, hass_read_only_user)
