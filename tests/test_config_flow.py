"""Tests for the SOAP Opera config flow."""

from __future__ import annotations

from custom_components.soap_opera.const import DOMAIN
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr


async def test_happy_path_creates_entry_and_device(hass: HomeAssistant) -> None:
    """A completed flow creates an entry titled with the animal's name, and a device."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "Biscuit", "species": "Dog", "breed": "Corgi"},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Biscuit"
    assert result["data"]["species"] == "Dog"
    assert result["data"]["breed"] == "Corgi"

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1

    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, entries[0].entry_id), entries[0].entry_id
    )
    assert device is not None
    assert device.name == "Biscuit"


async def test_duplicate_name_is_rejected(hass: HomeAssistant) -> None:
    """A second entry with the same name (case-insensitively) is not created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "Biscuit", "species": "Dog"}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "biscuit", "species": "Dog"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_exists"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_whitespace_only_name_is_rejected(hass: HomeAssistant) -> None:
    """A name that strips down to empty must not create a nameless entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "   ", "species": "Dog"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "name_required"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 0
