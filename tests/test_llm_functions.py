"""Tests for llm function setup and cleanup."""

from __future__ import annotations

from collections import deque
from typing import Callable
from unittest.mock import Mock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.llm_intents.const import (
    CONF_BRAVE_ENABLED,
    CONF_GOOGLE_PLACES_ENABLED,
    CONF_HISTORY_ENABLED,
    CONF_WEATHER_ENABLED,
    CONF_WIKIPEDIA_ENABLED,
    DOMAIN,
)
from custom_components.llm_intents.llm_functions import (
    cleanup_llm_functions,
    setup_llm_functions,
)


@pytest.fixture
def hass() -> HomeAssistant:
    """Return a mocked Home Assistant instance."""
    hass = Mock(spec=HomeAssistant)
    hass.data = {}
    hass.config_entries = Mock()
    entry = Mock()
    entry.options = {}
    hass.config_entries.async_entries = Mock(return_value=[entry])
    return hass


@pytest.mark.asyncio
async def test_setup_llm_functions_registers_enabled_apis(hass: HomeAssistant) -> None:
    """Enabled services should register their APIs."""
    config = {
        CONF_BRAVE_ENABLED: True,
        CONF_GOOGLE_PLACES_ENABLED: True,
        CONF_WIKIPEDIA_ENABLED: True,
        CONF_WEATHER_ENABLED: True,
        CONF_HISTORY_ENABLED: True,
    }

    unregister_calls: deque[str] = deque()

    def fake_register(_hass: HomeAssistant, api) -> Callable[[], None]:
        unregister_calls.append(api.name)

        def _undo() -> None:
            unregister_calls.append(f"unregister:{api.name}")

        return _undo

    with patch(
        "custom_components.llm_intents.llm_functions.llm.async_register_api",
        side_effect=fake_register,
    ):
        await setup_llm_functions(hass, config)

    assert DOMAIN in hass.data
    stored = hass.data[DOMAIN]
    assert "api" in stored
    assert "weather_api" in stored
    assert "history_api" in stored
    assert stored["config"] == config
    # Search, weather, and history APIs should have been registered
    assert list(unregister_calls) == ["Search Services", "Weather Forecast", "Entity History"]


@pytest.mark.asyncio
async def test_cleanup_llm_functions_invokes_unregister(hass: HomeAssistant) -> None:
    """Cleanup should execute stored unregister callbacks."""
    hass.data[DOMAIN] = {"unregister_api": []}

    called = []

    def _factory(name):
        def _undo():
            called.append(name)

        return _undo

    hass.data[DOMAIN]["unregister_api"].append(_factory("search"))
    hass.data[DOMAIN]["unregister_api"].append(_factory("history"))

    await cleanup_llm_functions(hass)

    assert called == ["search", "history"]
    assert DOMAIN not in hass.data
