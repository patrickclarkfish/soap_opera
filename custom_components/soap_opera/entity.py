"""Base for entities that belong to one regimen subentry."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class RegimenEntity(Entity):
    """Attached to the animal's device, named after the medication."""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, subentry: ConfigSubentry, key: str) -> None:
        self._entry = entry
        self._subentry = subentry
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"medication": subentry.title}
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
