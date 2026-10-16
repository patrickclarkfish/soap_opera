"""One "log dose" button per regimen: logs the standard dose, now."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .dosing import async_log_dose
from .entity import RegimenEntity
from .regimen_config import SUBENTRY_TYPE_REGIMEN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_REGIMEN:
            async_add_entities(
                [LogDoseButton(entry, subentry, "log_dose")],
                config_subentry_id=subentry.subentry_id,
            )


class LogDoseButton(RegimenEntity, ButtonEntity):
    async def async_press(self) -> None:
        # The pressing user's context, so the dose is attributed to them.
        await async_log_dose(
            self.hass,
            entry_id=self._entry.entry_id,
            subentry_id=self._subentry.subentry_id,
            capture_method="button",
            context=self._context or Context(),
        )
