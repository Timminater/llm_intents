"""Tests for the LLM Tools integration init module."""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.llm_tools import (
    DOMAIN,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.llm_tools.const import CONF_BRAVE_ENABLED


@pytest.fixture
def hass() -> HomeAssistant:
    """Return a mocked Home Assistant instance."""
    hass = Mock(spec=HomeAssistant)
    hass.data = {}
    return hass


@pytest.fixture
def config_entry() -> ConfigEntry:
    """Return a mocked config entry."""
    entry = Mock(spec=ConfigEntry)
    entry.entry_id = "abc123"
    entry.data = {CONF_BRAVE_ENABLED: True}
    return entry


@pytest.mark.asyncio
async def test_async_setup_initialises_domain(hass: HomeAssistant) -> None:
    """async_setup should prepare hass.data for the integration."""
    assert await async_setup(hass, {})
    assert DOMAIN in hass.data


@pytest.mark.asyncio
async def test_async_setup_entry_triggers_llm_setup(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Setting up an entry should delegate to setup_llm_functions."""
    with patch(
        "custom_components.llm_tools.llm_functions.setup_llm_functions"
    ) as mock_setup:
        result = await async_setup_entry(hass, config_entry)

    assert result is True
    mock_setup.assert_called_once_with(hass, config_entry.data)


@pytest.mark.asyncio
async def test_async_unload_entry_calls_cleanup(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Unloading an entry should clean up registered APIs."""
    with patch(
        "custom_components.llm_tools.llm_functions.cleanup_llm_functions"
    ) as mock_cleanup:
        assert await async_unload_entry(hass, config_entry)

    mock_cleanup.assert_called_once_with(hass)
