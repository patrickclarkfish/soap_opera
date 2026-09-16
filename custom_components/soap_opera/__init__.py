"""The SOAP Opera integration.

One config entry per animal; one shared SQLite database for the whole
integration, owned in hass.data and refcounted across entries -- set up by
whichever entry loads first, torn down when the last one unloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_SPECIES, DOMAIN
from .storage.database import Database

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = []


@dataclass
class SoapOperaData:
    """Runtime data shared by every soap_opera config entry, on hass.data[DOMAIN]."""

    database: Database
    entry_count: int


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one animal's config entry, sharing the integration-level database."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    shared: SoapOperaData | None = domain_data.get("shared")

    if shared is None:
        database = Database(hass)
        await hass.async_add_executor_job(database.connect_and_migrate)
        shared = SoapOperaData(database=database, entry_count=0)
        domain_data["shared"] = shared

    shared.entry_count += 1

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="SOAP Opera",
        model=entry.data.get(CONF_SPECIES),
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one animal's config entry, tearing down the shared database if last."""
    domain_data = hass.data.get(DOMAIN, {})
    shared: SoapOperaData | None = domain_data.get("shared")

    if shared is not None:
        shared.entry_count -= 1
        if shared.entry_count <= 0:
            await hass.async_add_executor_job(shared.database.close)
            domain_data.pop("shared", None)

    return True
