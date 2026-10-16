"""One administration event entity per regimen. Fires once per newly
recorded dose, with the status as the event type."""

from __future__ import annotations

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_DOSE_LOGGED
from .entity import RegimenEntity
from .regimen_config import SUBENTRY_TYPE_REGIMEN
from .storage.administrations import AdministrationStatus, DoseResult


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_REGIMEN:
            async_add_entities(
                [AdministrationEvent(entry, subentry, "administration")],
                config_subentry_id=subentry.subentry_id,
            )


class AdministrationEvent(RegimenEntity, EventEntity):
    _attr_event_types = [status.value for status in AdministrationStatus]

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_DOSE_LOGGED.format(self._subentry.subentry_id),
                self._async_dose_logged,
            )
        )

    @callback
    def _async_dose_logged(self, result: DoseResult) -> None:
        record = result.record
        self._trigger_event(
            record["status"],
            {
                "administration_id": str(result.administration_id),
                "administered_at": record["administered_at"],
                "administered_by": record["administered_by"],
                "dose_units": record["dose_units"],
                "dose_mg": record["dose_mg"],
                "capture_method": record["capture_method"],
                "guardrails_tripped": [outcome.as_dict() for outcome in result.tripped],
            },
        )
        self.async_write_ha_state()
