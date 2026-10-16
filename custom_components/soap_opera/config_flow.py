"""Config flow for SOAP Opera.

One config entry per animal. Adding a second animal means adding a second
config entry. Each regimen is a config subentry of its animal's entry.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from datetime import date
from functools import partial
from typing import Any

import sqlalchemy as sa
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .const import CONF_BREED, CONF_DATE_OF_BIRTH, CONF_SPECIES, DOMAIN
from .regimen_config import (
    DATA_EFFECTIVE_DATE,
    DATA_END_DATE,
    DATA_MEDICATION_ID,
    DATA_NEW_MEDICATION,
    DATA_START_DATE,
    SUBENTRY_TYPE_REGIMEN,
    list_medications,
    medication_id_of_subentry,
    medication_label,
    would_overlap,
)
from .storage.regimens import DOSING_FIELDS, REGIMEN_FIELDS, MedicationForm

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): str,
        vol.Required(CONF_SPECIES): str,
        vol.Optional(CONF_BREED): str,
        vol.Optional(CONF_DATE_OF_BIRTH): str,
    }
)


class SoapOperaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for SOAP Opera."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect an animal's name, species, and optional breed / date of birth."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            user_input[CONF_NAME] = name

            existing_names = {
                entry.data[CONF_NAME].casefold()
                for entry in self._async_current_entries()
            }
            if not name:
                errors["base"] = "name_required"
            elif name.casefold() in existing_names:
                errors["base"] = "name_exists"
            else:
                return self.async_create_entry(title=name, data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_TYPE_REGIMEN: RegimenSubentryFlowHandler}


NEW_MEDICATION = "new"
CONF_MEDICATION = "medication"
CONF_MIN_INTERVAL_HOURS = "min_interval_hours"
WITH_FOOD_REQUIRED = "required"
WITH_FOOD_AVOID = "avoid"
WITH_FOOD_NO_PREFERENCE = "no_preference"
_WITH_FOOD_TO_DB = {
    WITH_FOOD_REQUIRED: True,
    WITH_FOOD_AVOID: False,
    WITH_FOOD_NO_PREFERENCE: None,
}
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _number(min_value: float = 0) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(min=min_value, step="any", mode=NumberSelectorMode.BOX)
    )


MEDICATION_SCHEMA = vol.Schema(
    {
        vol.Required("generic_name"): TextSelector(),
        vol.Optional("brand_name"): TextSelector(),
        vol.Required("form"): SelectSelector(
            SelectSelectorConfig(
                options=[form.value for form in MedicationForm],
                translation_key="medication_form",
                mode=SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional("strength"): _number(),
        vol.Optional("concentration"): _number(),
        vol.Required("unit", default="mg"): TextSelector(),
        vol.Required("route", default="oral"): TextSelector(),
        vol.Required("requires_refrigeration", default=False): BooleanSelector(),
        vol.Required("shake_well", default=False): BooleanSelector(),
        vol.Required("with_food", default=WITH_FOOD_NO_PREFERENCE): SelectSelector(
            SelectSelectorConfig(
                options=list(_WITH_FOOD_TO_DB),
                translation_key="with_food",
                mode=SelectSelectorMode.LIST,
            )
        ),
    }
)


def _regimen_schema(*, editing: bool) -> vol.Schema:
    """The regimen form. Adding asks for start_date; editing asks for the
    effective date of a dosing change instead (the start is history)."""
    schema: dict[vol.Marker, Any] = {
        vol.Required("dose_units"): _number(),
        vol.Required("unit_strength"): _number(),
        vol.Optional("frequency"): TextSelector(),
        vol.Optional("scheduled_times"): TextSelector(
            TextSelectorConfig(multiple=True)
        ),
        vol.Required("prn", default=False): BooleanSelector(),
        vol.Optional(CONF_MIN_INTERVAL_HOURS): _number(),
        vol.Required("dose_variable", default=False): BooleanSelector(),
        vol.Optional("dose_increment"): _number(),
        vol.Optional("dose_max_single"): _number(),
        vol.Optional("dose_max_daily"): _number(),
        vol.Optional("indication"): TextSelector(),
        vol.Optional("prescribing_vet"): TextSelector(),
    }
    if editing:
        schema[vol.Required(DATA_EFFECTIVE_DATE)] = DateSelector()
    else:
        schema[vol.Required(DATA_START_DATE)] = DateSelector()
    schema[vol.Optional(DATA_END_DATE)] = DateSelector()
    return vol.Schema(schema)


def _parse_regimen_input(
    user_input: dict[str, Any], start_date: date
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate the regimen form into REGIMEN_FIELDS plus end_date.

    Returns (fields, errors). Optional fields left blank become None, so
    clearing a field on edit really clears it.
    """
    errors: dict[str, str] = {}
    fields: dict[str, Any] = {
        name: user_input.get(name) or None
        for name in REGIMEN_FIELDS
        if name not in ("prn", "dose_variable", "min_interval_seconds")
    }
    fields["prn"] = bool(user_input.get("prn"))
    fields["dose_variable"] = bool(user_input.get("dose_variable"))
    hours = user_input.get(CONF_MIN_INTERVAL_HOURS)
    fields["min_interval_seconds"] = round(hours * 3600) if hours else None

    for name in ("dose_units", "unit_strength"):
        if not user_input.get(name):
            errors[name] = "must_be_positive"

    times = [t.strip() for t in user_input.get("scheduled_times") or [] if t.strip()]
    if any(not _TIME_RE.match(t) for t in times):
        errors["scheduled_times"] = "invalid_time"
    else:
        fields["scheduled_times"] = (
            sorted(f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}" for t in times)
            or None
        )

    if fields["dose_variable"] and not fields["dose_increment"]:
        errors["dose_increment"] = "increment_required"

    end_date = user_input.get(DATA_END_DATE)
    if end_date and date.fromisoformat(end_date) <= start_date:
        errors[DATA_END_DATE] = "end_before_start"
    fields[DATA_END_DATE] = end_date or None
    return fields, errors


def _regimen_form_values(data: Mapping[str, Any]) -> dict[str, Any]:
    """Subentry data back into form values, for pre-filling an edit."""
    values = {k: v for k, v in data.items() if v is not None}
    seconds = data.get("min_interval_seconds")
    if seconds:
        values[CONF_MIN_INTERVAL_HOURS] = seconds / 3600
    return values


class RegimenSubentryFlowHandler(ConfigSubentryFlow):
    """Add or edit one regimen of an animal.

    Only reads the database (to list medications and check for overlaps).
    Every write happens when the entry reloads, in regimen_config's sync,
    the same path whether a regimen was added, edited or removed.
    """

    def __init__(self) -> None:
        self._medication_id: str | None = None
        self._new_medication: dict[str, Any] | None = None
        self._medication_title: str = ""
        self._default_strength: float | None = None

    def _engine(self) -> sa.engine.Engine | None:
        shared = self.hass.data.get(DOMAIN, {}).get("shared")
        return None if shared is None else shared.database.engine

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Pick an existing medication, or choose to define a new one."""
        engine = self._engine()
        if engine is None:
            return self.async_abort(reason="not_loaded")
        medications = await self.hass.async_add_executor_job(list_medications, engine)
        if not medications:
            return await self.async_step_medication()

        by_id = {str(med["id"]): med for med in medications}
        if user_input is not None:
            choice = user_input[CONF_MEDICATION]
            if choice == NEW_MEDICATION:
                return await self.async_step_medication()
            medication = by_id[choice]
            self._medication_id = choice
            self._medication_title = medication_label(medication)
            self._default_strength = (
                medication["concentration"]
                if medication["form"] == MedicationForm.LIQUID
                else medication["strength"]
            )
            return await self.async_step_regimen()

        options = [
            SelectOptionDict(value=med_id, label=medication_label(med))
            for med_id, med in by_id.items()
        ]
        options.append(SelectOptionDict(value=NEW_MEDICATION, label="New medication"))
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MEDICATION): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            translation_key="medication_choice",
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
        )

    async def async_step_medication(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Define a new medication inline."""
        errors: dict[str, str] = {}
        if user_input is not None:
            form = MedicationForm(user_input["form"])
            if not user_input["generic_name"].strip():
                errors["generic_name"] = "required"
            if form is MedicationForm.LIQUID:
                if not user_input.get("concentration"):
                    errors["concentration"] = "concentration_required"
            elif not user_input.get("strength"):
                errors["strength"] = "strength_required"
            if not errors:
                medication = {
                    "generic_name": user_input["generic_name"].strip(),
                    "brand_name": (user_input.get("brand_name") or "").strip() or None,
                    "form": form.value,
                    "strength": None
                    if form is MedicationForm.LIQUID
                    else user_input["strength"],
                    "concentration": user_input.get("concentration")
                    if form is MedicationForm.LIQUID
                    else None,
                    "unit": user_input["unit"].strip(),
                    "route": user_input["route"].strip(),
                    "requires_refrigeration": user_input["requires_refrigeration"],
                    "shake_well": user_input["shake_well"],
                    "with_food": _WITH_FOOD_TO_DB[user_input["with_food"]],
                }
                self._new_medication = medication
                self._medication_title = medication_label(medication)
                self._default_strength = (
                    medication["concentration"] or medication["strength"]
                )
                return await self.async_step_regimen()

        return self.async_show_form(
            step_id="medication",
            data_schema=self.add_suggested_values_to_schema(
                MEDICATION_SCHEMA, user_input or {}
            ),
            errors=errors,
        )

    async def async_step_regimen(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Collect the prescription itself."""
        errors: dict[str, str] = {}
        if user_input is not None:
            start_date = date.fromisoformat(user_input[DATA_START_DATE])
            fields, errors = _parse_regimen_input(user_input, start_date)
            engine = self._engine()
            if not errors and self._medication_id and engine is not None:
                overlaps = await self.hass.async_add_executor_job(
                    partial(
                        would_overlap,
                        engine,
                        entry_id=self._entry_id,
                        medication_id=uuid.UUID(self._medication_id),
                        start_date=start_date,
                        end_date=_date_or_none(fields[DATA_END_DATE]),
                    )
                )
                if overlaps:
                    errors["base"] = "overlap"
            if not errors:
                return self.async_create_entry(
                    title=self._medication_title,
                    data={
                        **fields,
                        DATA_MEDICATION_ID: self._medication_id,
                        DATA_NEW_MEDICATION: self._new_medication,
                        DATA_START_DATE: start_date.isoformat(),
                        DATA_EFFECTIVE_DATE: start_date.isoformat(),
                    },
                )

        suggested = user_input or {
            "unit_strength": self._default_strength,
            DATA_START_DATE: dt_util.now().date().isoformat(),
        }
        return self.async_show_form(
            step_id="regimen",
            data_schema=self.add_suggested_values_to_schema(
                _regimen_schema(editing=False), suggested
            ),
            errors=errors,
            description_placeholders={"medication": self._medication_title},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit a regimen. A dosing change becomes a new version from the
        effective date; anything else corrects the current version."""
        subentry = self._get_reconfigure_subentry()
        old = subentry.data
        current_since = date.fromisoformat(
            old.get(DATA_EFFECTIVE_DATE) or old[DATA_START_DATE]
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            effective = date.fromisoformat(user_input[DATA_EFFECTIVE_DATE])
            fields, errors = _parse_regimen_input(
                user_input, date.fromisoformat(old[DATA_START_DATE])
            )
            dosing_changed = any(
                fields[name] != old.get(name) for name in DOSING_FIELDS
            )
            end_date = _date_or_none(fields[DATA_END_DATE])
            if not errors and dosing_changed:
                if effective < current_since:
                    errors[DATA_EFFECTIVE_DATE] = "effective_before_current"
                elif end_date is not None and effective >= end_date:
                    errors[DATA_EFFECTIVE_DATE] = "effective_after_end"
            engine = self._engine()
            if (
                not errors
                and engine is not None
                and fields[DATA_END_DATE] != old.get(DATA_END_DATE)
            ):
                medication_id = await self.hass.async_add_executor_job(
                    medication_id_of_subentry, engine, subentry.subentry_id
                )
                if medication_id is not None and await self.hass.async_add_executor_job(
                    partial(
                        would_overlap,
                        engine,
                        entry_id=self._entry_id,
                        medication_id=medication_id,
                        start_date=date.fromisoformat(old[DATA_START_DATE]),
                        end_date=end_date,
                        ignore_subentry_id=subentry.subentry_id,
                    )
                ):
                    errors["base"] = "overlap"
            if not errors:
                return self.async_update_and_abort(
                    self._get_entry(),
                    subentry,
                    data={
                        **old,
                        **fields,
                        DATA_EFFECTIVE_DATE: (
                            effective if dosing_changed else current_since
                        ).isoformat(),
                    },
                )

        suggested = user_input or {
            **_regimen_form_values(old),
            DATA_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _regimen_schema(editing=True), suggested
            ),
            errors=errors,
            description_placeholders={
                "medication": subentry.title,
                "current_since": current_since.isoformat(),
            },
        )


def _date_or_none(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
