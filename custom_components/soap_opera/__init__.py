"""The SOAP Opera integration.

One config entry per animal; one shared SQLite database for the whole
integration, owned in hass.data and refcounted across entries -- set up by
whichever entry loads first, torn down when the last one unloads.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_BREED, CONF_DATE_OF_BIRTH, CONF_SPECIES, DOMAIN
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
    # setdefault on a plain dict never awaits, so this is race-free even
    # though HA sets up config entries for one integration concurrently.
    lock: asyncio.Lock = domain_data.setdefault("lock", asyncio.Lock())

    async with lock:
        shared: SoapOperaData | None = domain_data.get("shared")
        if shared is None:
            database = Database(hass)
            await hass.async_add_executor_job(database.connect_and_migrate)
            shared = SoapOperaData(database=database, entry_count=0)
            domain_data["shared"] = shared

    # The database is the source of truth for an animal's data; the config
    # entry is derived from it. Keyed on entry_id, so a rename (handled by
    # the reload triggered below) updates this row rather than duplicating
    # it.
    # species is a required config-flow field, always present; breed and
    # date of birth are optional.
    species: str = entry.data[CONF_SPECIES]

    await hass.async_add_executor_job(
        shared.database.upsert_subject,
        entry.entry_id,
        entry.title,
        species,
        entry.data.get(CONF_BREED),
        entry.data.get(CONF_DATE_OF_BIRTH),
    )

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="SOAP Opera",
        model=species,
    )

    # Only counted once setup has fully succeeded: HA does not call
    # async_unload_entry for an entry whose async_setup_entry raised, so
    # incrementing any earlier would leak this count on a partial failure.
    shared.entry_count += 1

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload on entry update so an edit (e.g. a rename) re-syncs to the database."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one animal's config entry, tearing down the shared database if last."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    domain_data = hass.data.get(DOMAIN, {})
    shared: SoapOperaData | None = domain_data.get("shared")

    if shared is not None:
        shared.entry_count -= 1
        if shared.entry_count <= 0:
            await hass.async_add_executor_job(shared.database.close)
            domain_data.pop("shared", None)

    return True
