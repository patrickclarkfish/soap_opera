"""Regimens as config subentries: the flows, and their sync into the database
on setup (add, dosing edit -> supersede, non-dosing edit -> in place,
remove -> end-date).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from custom_components.soap_opera.const import CONF_SPECIES, DOMAIN
from custom_components.soap_opera.regimen_config import SUBENTRY_TYPE_REGIMEN
from custom_components.soap_opera.storage.database import (
    medication_table,
    regimen_table,
)
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

TODAY = date(2026, 3, 2)

GABAPENTIN = {
    "generic_name": "gabapentin",
    "form": "capsule",
    "strength": 100,
    "unit": "mg",
    "route": "oral",
    "requires_refrigeration": False,
    "shake_well": False,
    "with_food": "no_preference",
}


@pytest.fixture(autouse=True)
def _frozen_today(freezer: Any) -> None:
    freezer.move_to("2026-03-02 12:00:00")


async def _animal(hass: HomeAssistant, name: str = "Biscuit") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title=name, data={"name": name, CONF_SPECIES: "Dog"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _engine(hass: HomeAssistant) -> sa.engine.Engine:
    return hass.data[DOMAIN]["shared"].database.engine


def _regimen_rows(hass: HomeAssistant) -> list[Any]:
    with _engine(hass).connect() as conn:
        return list(
            conn.execute(
                sa.select(regimen_table).order_by(regimen_table.c.created_at)
            ).all()
        )


def _medication_count(hass: HomeAssistant) -> int:
    with _engine(hass).connect() as conn:
        return conn.execute(
            sa.select(sa.func.count()).select_from(medication_table)
        ).scalar_one()


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_REGIMEN), context={"source": SOURCE_USER}
    )


async def _add_gabapentin_regimen(
    hass: HomeAssistant, entry: MockConfigEntry, **regimen: Any
) -> str:
    before = set(entry.subentries)
    result = await _start(hass, entry)
    if result["step_id"] == "user":
        result = await hass.config_entries.subentries.async_configure(
            result["flow_id"], {"medication": "new"}
        )
    assert result["step_id"] == "medication"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GABAPENTIN
    )
    assert result["step_id"] == "regimen"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "dose_units": 3,
            "unit_strength": 100,
            "start_date": TODAY.isoformat(),
            "dose_max_daily": 900,
            **regimen,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "gabapentin 100 mg capsule"
    (subentry_id,) = set(entry.subentries) - before
    return subentry_id


async def _reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str, **changes: Any
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_REGIMEN),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    data = dict(entry.subentries[subentry_id].data)
    form = {
        key: data[key]
        for key in (
            "dose_units",
            "unit_strength",
            "prn",
            "dose_variable",
            "dose_max_daily",
            "indication",
            "prescribing_vet",
        )
        if data.get(key) is not None
    }
    form["effective_date"] = TODAY.isoformat()
    form.update(changes)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], form
    )
    await hass.async_block_till_done()
    return result


async def test_add_regimen_with_inline_medication(hass: HomeAssistant) -> None:
    entry = await _animal(hass)

    subentry_id = await _add_gabapentin_regimen(hass, entry)

    rows = _regimen_rows(hass)
    assert len(rows) == 1
    assert rows[0].config_subentry_id == subentry_id
    assert rows[0].dose_units == 3
    assert rows[0].dose_max_daily == 900
    assert rows[0].start_date == TODAY
    assert rows[0].end_date is None
    assert _medication_count(hass) == 1


async def test_second_regimen_can_pick_the_existing_medication(
    hass: HomeAssistant,
) -> None:
    biscuit = await _animal(hass, "Biscuit")
    await _add_gabapentin_regimen(hass, biscuit)
    waffles = await _animal(hass, "Waffles")

    result = await _start(hass, waffles)
    assert result["step_id"] == "user"
    options = result["data_schema"].schema["medication"].config["options"]
    existing = next(opt["value"] for opt in options if opt["value"] != "new")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"medication": existing}
    )
    assert result["step_id"] == "regimen"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"dose_units": 1, "unit_strength": 100, "start_date": TODAY.isoformat()},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _medication_count(hass) == 1
    assert len({row.medication_id for row in _regimen_rows(hass)}) == 1
    assert len(_regimen_rows(hass)) == 2


async def test_overlapping_regimen_for_same_animal_and_drug_is_rejected(
    hass: HomeAssistant,
) -> None:
    entry = await _animal(hass)
    await _add_gabapentin_regimen(hass, entry)

    result = await _start(hass, entry)
    options = result["data_schema"].schema["medication"].config["options"]
    existing = next(opt["value"] for opt in options if opt["value"] != "new")
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"medication": existing}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"dose_units": 1, "unit_strength": 100, "start_date": "2026-04-01"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "overlap"}


async def test_dosing_edit_supersedes_at_the_effective_date(
    hass: HomeAssistant,
) -> None:
    entry = await _animal(hass)
    subentry_id = await _add_gabapentin_regimen(hass, entry)
    monday = date(2026, 3, 9)

    result = await _reconfigure(
        hass, entry, subentry_id, dose_units=2, effective_date=monday.isoformat()
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    old, new = _regimen_rows(hass)
    assert old.dose_units == 3
    assert old.end_date == monday
    assert new.dose_units == 2
    assert new.start_date == monday
    assert new.supersedes_id == old.id
    assert new.config_subentry_id == subentry_id


async def test_non_dosing_edit_updates_in_place(hass: HomeAssistant) -> None:
    entry = await _animal(hass)
    subentry_id = await _add_gabapentin_regimen(hass, entry, indication="arthritus")

    result = await _reconfigure(
        hass,
        entry,
        subentry_id,
        indication="arthritis",
        # Ignored: nothing dosing changed.
        effective_date="2026-05-01",
    )

    assert result["type"] is FlowResultType.ABORT
    (row,) = _regimen_rows(hass)
    assert row.indication == "arthritis"
    assert row.end_date is None
    assert entry.subentries[subentry_id].data["effective_date"] == TODAY.isoformat()


async def test_dosing_edit_cannot_take_effect_before_current_version(
    hass: HomeAssistant,
) -> None:
    entry = await _animal(hass)
    subentry_id = await _add_gabapentin_regimen(hass, entry)

    result = await _reconfigure(
        hass, entry, subentry_id, dose_units=2, effective_date="2026-01-01"
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"effective_date": "effective_before_current"}
    assert len(_regimen_rows(hass)) == 1


async def test_removing_a_subentry_end_dates_but_never_deletes(
    hass: HomeAssistant,
) -> None:
    entry = await _animal(hass)
    subentry_id = await _add_gabapentin_regimen(hass, entry)

    hass.config_entries.async_remove_subentry(entry, subentry_id)
    await hass.async_block_till_done()

    (row,) = _regimen_rows(hass)
    assert row.end_date == TODAY
    assert _medication_count(hass) == 1


async def test_reload_without_changes_is_a_no_op(hass: HomeAssistant) -> None:
    entry = await _animal(hass)
    await _add_gabapentin_regimen(hass, entry)
    before = _regimen_rows(hass)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert _regimen_rows(hass) == before


async def test_scheduled_times_are_validated_and_normalized(
    hass: HomeAssistant,
) -> None:
    entry = await _animal(hass)
    result = await _start(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], GABAPENTIN
    )

    bad = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "dose_units": 1,
            "unit_strength": 100,
            "start_date": TODAY.isoformat(),
            "scheduled_times": ["8am"],
        },
    )
    assert bad["errors"] == {"scheduled_times": "invalid_time"}

    good = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "dose_units": 1,
            "unit_strength": 100,
            "start_date": TODAY.isoformat(),
            "scheduled_times": ["20:00", "8:00"],
        },
    )
    await hass.async_block_till_done()
    assert good["type"] is FlowResultType.CREATE_ENTRY
    assert _regimen_rows(hass)[0].scheduled_times == ["08:00", "20:00"]


async def test_liquid_needs_a_concentration(hass: HomeAssistant) -> None:
    entry = await _animal(hass)
    result = await _start(hass, entry)

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {**GABAPENTIN, "form": "liquid"}
    )

    assert result["errors"] == {"concentration": "concentration_required"}


async def test_unsyncable_subentry_does_not_fail_setup(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Two subentries for the same drug over the same dates -- the flow
    prevents this, but subentry data can be edited by other means. The
    second is logged and skipped; the animal still loads."""
    entry = await _animal(hass)
    first = await _add_gabapentin_regimen(hass, entry)
    rows = _regimen_rows(hass)
    data = {
        **entry.subentries[first].data,
        "medication_id": str(rows[0].medication_id),
        "new_medication": None,
    }
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=data,
            subentry_type=SUBENTRY_TYPE_REGIMEN,
            title="duplicate",
            unique_id=None,
        ),
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert len(_regimen_rows(hass)) == 1
    assert "Could not sync regimen subentry" in caplog.text
