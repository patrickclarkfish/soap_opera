"""Tests for async_setup_entry / async_unload_entry: the shared-database
single-flight guard and the refcount bookkeeping around it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from custom_components.soap_opera import (
    SoapOperaData,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.soap_opera.const import CONF_SPECIES, DOMAIN
from custom_components.soap_opera.storage.database import Database
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_concurrent_setup_creates_exactly_one_database(
    hass: HomeAssistant,
) -> None:
    """Two entries loading at once must not race into creating two databases."""
    entry1 = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN, title="Waffles", data={"name": "Waffles", CONF_SPECIES: "Cat"}
    )
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    created: list[Database] = []
    real_init = Database.__init__

    def counting_init(self: Database, *args: object, **kwargs: object) -> None:
        created.append(self)
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Database, "__init__", counting_init):
        results = await asyncio.gather(
            async_setup_entry(hass, entry1),
            async_setup_entry(hass, entry2),
        )

    assert results == [True, True]
    assert len(created) == 1

    shared: SoapOperaData = hass.data[DOMAIN]["shared"]
    assert shared.entry_count == 2


async def test_failed_setup_does_not_increment_refcount(hass: HomeAssistant) -> None:
    """If setup raises after the database exists, the refcount must not have moved --
    otherwise it never comes back down, since HA won't call async_unload_entry for an
    entry whose async_setup_entry raised.
    """
    entry = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.soap_opera.dr.async_get",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await async_setup_entry(hass, entry)

    shared: SoapOperaData = hass.data[DOMAIN]["shared"]
    assert shared.entry_count == 0


async def test_unload_closes_database_when_last_entry_leaves(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry.add_to_hass(hass)
    await async_setup_entry(hass, entry)

    assert await async_unload_entry(hass, entry)

    assert "shared" not in hass.data[DOMAIN]
