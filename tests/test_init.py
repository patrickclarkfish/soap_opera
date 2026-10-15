"""Tests for async_setup_entry / async_unload_entry: the shared-database
single-flight guard and the refcount bookkeeping around it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import sqlalchemy as sa
from custom_components.soap_opera import (
    SoapOperaData,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.soap_opera.const import CONF_SPECIES, DOMAIN
from custom_components.soap_opera.storage import database
from custom_components.soap_opera.storage.database import DB_FILENAME, Database
from custom_components.soap_opera.storage.migrations import SCHEMA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
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


async def test_first_entry_failure_cleans_up_the_database_it_just_opened(
    hass: HomeAssistant,
) -> None:
    """If the entry that creates the shared database then fails, nothing else
    holds a reference to it and HA won't call async_unload_entry for a setup
    that raised -- so the open engine must be closed and removed here, not
    leaked for the life of the process.
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

    assert "shared" not in hass.data[DOMAIN]


async def test_second_entry_failure_does_not_touch_the_shared_database(
    hass: HomeAssistant,
) -> None:
    """A failure in an entry that did *not* create the shared database must
    not close or remove it -- other entries may still be using it -- and
    must not have incremented the refcount either.
    """
    entry1 = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN, title="Waffles", data={"name": "Waffles", CONF_SPECIES: "Cat"}
    )
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    assert await async_setup_entry(hass, entry1)
    shared_before: SoapOperaData = hass.data[DOMAIN]["shared"]
    assert shared_before.entry_count == 1

    with (
        patch(
            "custom_components.soap_opera.dr.async_get",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await async_setup_entry(hass, entry2)

    shared_after: SoapOperaData = hass.data[DOMAIN]["shared"]
    assert shared_after is shared_before
    assert shared_after.entry_count == 1


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


async def test_schema_too_new_raises_config_entry_error(hass: HomeAssistant) -> None:
    """A downgrade guard trip should surface as ConfigEntryError, so HA shows
    the purpose-built message instead of an unhandled-exception traceback.
    """
    db_path = hass.config.path(DB_FILENAME)
    engine = sa.create_engine(f"sqlite:///{db_path}", future=True)
    database.run_migrations(engine)
    with engine.begin() as conn:
        conn.execute(
            database.schema_version_table.insert().values(
                version=SCHEMA_VERSION + 1, applied_at="future"
            )
        )
    engine.dispose()

    entry = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError):
        await async_setup_entry(hass, entry)


async def test_concurrent_unload_and_setup_of_different_entries_do_not_race(
    hass: HomeAssistant,
) -> None:
    """Unloading one entry while a second entry is concurrently being set up
    must not race: the second entry's setup must not be left holding a
    reference to a database the first entry's unload just closed.
    """
    entry1 = MockConfigEntry(
        domain=DOMAIN, title="Biscuit", data={"name": "Biscuit", CONF_SPECIES: "Dog"}
    )
    entry2 = MockConfigEntry(
        domain=DOMAIN, title="Waffles", data={"name": "Waffles", CONF_SPECIES: "Cat"}
    )
    entry1.add_to_hass(hass)
    entry2.add_to_hass(hass)

    assert await async_setup_entry(hass, entry1)

    results = await asyncio.gather(
        async_unload_entry(hass, entry1),
        async_setup_entry(hass, entry2),
    )

    assert results == [True, True]
    shared: SoapOperaData = hass.data[DOMAIN]["shared"]
    assert shared.entry_count == 1
    # Doesn't raise: proves this is a live engine, not one entry1's unload
    # closed out from under entry2's setup.
    assert shared.database.engine is not None
