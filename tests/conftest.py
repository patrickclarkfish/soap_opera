"""Shared fixtures for soap_opera tests."""

from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/soap_opera discoverable to Home Assistant in tests."""
    yield


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Give every test its own throwaway hass.config.path(), via tmp_path.

    pytest-homeassistant-custom-component's own default hass_config_dir
    fixture points at a fixed directory inside the installed package,
    shared across every test and never cleaned up. Our tests write a real
    soap_opera.db there (Database resolves its path from
    hass.config.path()), so without this override, a test asserting
    against a corrupted/future-schema database (see
    test_schema_too_new_raises_config_entry_error) leaves that file behind
    and silently breaks every later test that opens the same database.
    """
    return hass_tmp_config_dir
