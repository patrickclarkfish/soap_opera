"""The log_dose funnel end to end: service, button, event entity, bus event,
audit log, attribution, idempotency and guardrails as seen by a caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
import voluptuous as vol
from custom_components.soap_opera.const import (
    CONF_SPECIES,
    DOMAIN,
    EVENT_DOSE_LOGGED,
)
from custom_components.soap_opera.regimen_config import SUBENTRY_TYPE_REGIMEN
from custom_components.soap_opera.storage.database import (
    AUDIT_LOG_FILENAME,
    administration_table,
)
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

BUTTON = "button.biscuit_gabapentin_100_mg_capsule_log_dose"
EVENT = "event.biscuit_gabapentin_100_mg_capsule_administration"


@pytest.fixture(autouse=True)
def _frozen_now(freezer: Any) -> None:
    freezer.move_to("2026-03-02 12:00:00+00:00")


async def _animal_with_regimen(hass: HomeAssistant, **regimen: Any) -> MockConfigEntry:
    data: dict[str, Any] = {
        "medication_id": None,
        "new_medication": {
            "generic_name": "gabapentin",
            "form": "capsule",
            "strength": 100,
            "concentration": None,
            "unit": "mg",
            "route": "oral",
            "requires_refrigeration": False,
            "shake_well": False,
            "with_food": None,
        },
        "dose_units": 3,
        "unit_strength": 100,
        "prn": False,
        "dose_variable": False,
        "start_date": "2026-03-01",
        "effective_date": "2026-03-01",
        **regimen,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Biscuit",
        data={"name": "Biscuit", CONF_SPECIES: "Dog"},
        subentries_data=[
            ConfigSubentryData(
                data=data,
                subentry_type=SUBENTRY_TYPE_REGIMEN,
                title="gabapentin 100 mg capsule",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _log(
    hass: HomeAssistant, context: Context | None = None, **data: Any
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "log_dose",
        {"regimen": BUTTON, **data},
        blocking=True,
        return_response=True,
        context=context,
    )
    assert response is not None
    return dict(response)


def _administrations(hass: HomeAssistant) -> list[Any]:
    engine = hass.data[DOMAIN]["shared"].database.engine
    with engine.connect() as conn:
        return list(conn.execute(sa.select(administration_table)).all())


async def test_entities_exist_per_regimen(hass: HomeAssistant) -> None:
    await _animal_with_regimen(hass)

    assert hass.states.get(BUTTON) is not None
    assert hass.states.get(EVENT) is not None


async def test_service_logs_and_attributes_to_the_caller(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    await _animal_with_regimen(hass)

    response = await _log(hass, Context(user_id=hass_admin_user.id))

    (row,) = _administrations(hass)
    assert response["administration_id"] == str(row.id)
    assert response["duplicate"] is False
    assert {g["guardrail"] for g in response["guardrails"]} == {
        "min_interval",
        "max_single_dose",
        "max_daily_total",
    }
    assert row.administered_by == hass_admin_user.name
    assert row.capture_method == "service"
    assert (row.dose_units, row.dose_mg) == (3, 300)


async def test_explicit_administered_by_wins(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    await _animal_with_regimen(hass)

    await _log(hass, Context(user_id=hass_admin_user.id), administered_by="sitter")

    assert _administrations(hass)[0].administered_by == "sitter"


@pytest.mark.parametrize("status", ["given_late", "missed"])
async def test_system_only_statuses_are_rejected(
    hass: HomeAssistant, status: str
) -> None:
    await _animal_with_regimen(hass)

    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await _log(hass, status=status)

    assert _administrations(hass) == []


async def test_button_press_logs_standard_dose_and_fires_everything(
    hass: HomeAssistant, hass_admin_user: User, hass_config_dir: str
) -> None:
    await _animal_with_regimen(hass)
    bus_events = async_capture_events(hass, EVENT_DOSE_LOGGED)

    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": BUTTON},
        blocking=True,
        context=Context(user_id=hass_admin_user.id),
    )
    await hass.async_block_till_done()

    (row,) = _administrations(hass)
    assert row.capture_method == "button"
    assert row.administered_by == hass_admin_user.name
    assert row.dose_units == 3

    state = hass.states.get(EVENT)
    assert state is not None
    assert state.attributes["event_type"] == "given"
    assert state.attributes["administration_id"] == str(row.id)

    assert len(bus_events) == 1
    assert bus_events[0].data["administration_id"] == str(row.id)

    lines = Path(hass_config_dir, AUDIT_LOG_FILENAME).read_text().splitlines()
    (audit,) = [json.loads(line) for line in lines]
    assert audit["event"] == "dose_logged"
    assert audit["data"]["id"] == str(row.id)


async def test_duplicate_returns_original_and_fires_nothing(
    hass: HomeAssistant,
) -> None:
    await _animal_with_regimen(hass)
    bus_events = async_capture_events(hass, EVENT_DOSE_LOGGED)

    first = await _log(hass, idempotency_key="abc")
    second = await _log(hass, idempotency_key="abc", dose_units=1)
    await hass.async_block_till_done()

    assert second["duplicate"] is True
    assert second["administration_id"] == first["administration_id"]
    (row,) = _administrations(hass)
    assert row.source_event_count == 2
    assert len(bus_events) == 1


async def test_double_tapped_button_collapses(
    hass: HomeAssistant, hass_admin_user: User
) -> None:
    await _animal_with_regimen(hass)

    for _ in range(2):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": BUTTON},
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )

    (row,) = _administrations(hass)
    assert row.source_event_count == 2


async def test_tripped_guardrail_is_returned_and_override_recorded(
    hass: HomeAssistant,
) -> None:
    await _animal_with_regimen(hass, dose_max_single=200)

    warned = await _log(hass, idempotency_key="1")
    overridden = await _log(hass, idempotency_key="2", override_guardrails=True)

    def single(response: dict[str, Any]) -> dict[str, Any]:
        return next(
            g for g in response["guardrails"] if g["guardrail"] == "max_single_dose"
        )

    assert single(warned)["tripped"] and not single(warned)["overridden"]
    assert single(overridden)["overridden"]
    assert len(_administrations(hass)) == 2  # neither was blocked


async def test_backfill_resolves_the_version_in_force_then(
    hass: HomeAssistant,
) -> None:
    await _animal_with_regimen(hass)

    with pytest.raises(ServiceValidationError, match="not in force"):
        await _log(hass, administered_at="2026-02-01T08:00:00")


async def test_unknown_regimen_is_a_validation_error(hass: HomeAssistant) -> None:
    await _animal_with_regimen(hass)

    with pytest.raises(ServiceValidationError):
        await _log(hass, regimen="nope")


async def test_regimen_can_be_named_by_subentry_id(hass: HomeAssistant) -> None:
    entry = await _animal_with_regimen(hass)
    (subentry_id,) = entry.subentries

    await _log(hass, regimen=subentry_id)

    assert len(_administrations(hass)) == 1
