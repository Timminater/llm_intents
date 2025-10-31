"""Tests for the LLM Tools config flow."""

from __future__ import annotations

from unittest.mock import Mock, patch

import voluptuous as vol
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.llm_tools.config_flow import (
    STEP_BRAVE,
    STEP_GOOGLE_PLACES,
    STEP_INIT,
    STEP_USER,
    STEP_WEATHER,
    STEP_WIKIPEDIA,
    ConfigFlow,
    INITIAL_CONFIG_STEP_ORDER,
    OptionsFlow,
    SEARCH_STEP_ORDER,
    get_next_step,
    get_step_user_data_schema,
)
from custom_components.llm_tools.const import (
    ADDON_NAME,
    CONF_BRAVE_ENABLED,
    CONF_GOOGLE_PLACES_ENABLED,
    CONF_HISTORY_ENABLED,
    CONF_WEATHER_ENABLED,
    CONF_WIKIPEDIA_ENABLED,
)


@pytest.fixture
def hass() -> HomeAssistant:
    """Return a mocked Home Assistant instance."""
    return Mock(spec=HomeAssistant)


class TestSchemaHelpers:
    """Test helper functions that build config flow schemas."""

    def test_get_step_user_data_schema_defaults(self, hass: HomeAssistant) -> None:
        """Schema should provide defaults for every service toggle."""
        schema = get_step_user_data_schema(hass)
        assert isinstance(schema, vol.Schema)

        validated = schema({})
        assert validated == {
            CONF_BRAVE_ENABLED: False,
            CONF_GOOGLE_PLACES_ENABLED: False,
            CONF_WIKIPEDIA_ENABLED: False,
            CONF_WEATHER_ENABLED: False,
            CONF_HISTORY_ENABLED: False,
        }

    def test_get_next_step_respects_selection_order(self) -> None:
        """Ensure flow order matches expected progression."""
        selections = {
            CONF_BRAVE_ENABLED: True,
            CONF_GOOGLE_PLACES_ENABLED: False,
            CONF_WIKIPEDIA_ENABLED: True,
            CONF_HISTORY_ENABLED: True,
        }

        step, _ = get_next_step(STEP_USER, selections, SEARCH_STEP_ORDER)
        assert step == STEP_BRAVE

        step, _ = get_next_step(STEP_BRAVE, selections, INITIAL_CONFIG_STEP_ORDER)
        assert step == STEP_WIKIPEDIA

        assert get_next_step(STEP_WIKIPEDIA, selections, INITIAL_CONFIG_STEP_ORDER) is None


class TestConfigFlow:
    """Tests for the initial configuration flow."""

    @pytest.fixture
    def flow(self, hass: HomeAssistant) -> ConfigFlow:
        """Return an initialised config flow."""
        flow = ConfigFlow()
        flow.hass = hass
        return flow

    async def test_async_step_user_initial_form(self, flow: ConfigFlow) -> None:
        """First step should present the selection form."""
        with patch.object(flow, "_async_current_entries", return_value=[]):
            result = await flow.async_step_user()
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == STEP_USER

    async def test_async_step_user_brave_selected(self, flow: ConfigFlow) -> None:
        """Selecting Brave should lead to the Brave configuration step."""
        with (
            patch.object(flow, "_async_current_entries", return_value=[]),
            patch.object(flow, "async_set_unique_id"),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_user({CONF_BRAVE_ENABLED: True})

        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == STEP_BRAVE

    async def test_async_step_user_no_services(self, flow: ConfigFlow) -> None:
        """No selections should immediately create the entry."""
        payload = {
            CONF_BRAVE_ENABLED: False,
            CONF_GOOGLE_PLACES_ENABLED: False,
            CONF_WIKIPEDIA_ENABLED: False,
            CONF_WEATHER_ENABLED: False,
            CONF_HISTORY_ENABLED: False,
        }
        with (
            patch.object(flow, "_async_current_entries", return_value=[]),
            patch.object(flow, "async_set_unique_id"),
            patch.object(flow, "_abort_if_unique_id_configured"),
        ):
            result = await flow.async_step_user(payload)

        assert result == {
            "type": FlowResultType.CREATE_ENTRY,
            "title": ADDON_NAME,
            "data": payload,
            "options": {},
        }


class TestOptionsFlow:
    """Tests for the options flow."""

    @pytest.fixture
    def config_entry(self) -> Mock:
        """Create a mocked config entry used by the options flow."""
        entry = Mock()
        entry.data = {CONF_BRAVE_ENABLED: True, CONF_HISTORY_ENABLED: True}
        entry.options = {CONF_WIKIPEDIA_ENABLED: True}
        entry.entry_id = "test"
        entry.title = ADDON_NAME
        return entry

    @pytest.fixture
    def options_flow(self, config_entry: Mock) -> OptionsFlow:
        """Return an initialised options flow."""
        flow = OptionsFlow(config_entry)
        flow.hass = Mock(spec=HomeAssistant)
        return flow

    async def test_async_step_init_menu(self, options_flow: OptionsFlow) -> None:
        """Initial step should display menu options."""
        result = await options_flow.async_step_init()
        assert result["type"] == FlowResultType.MENU
        assert result["step_id"] == STEP_INIT

    async def test_async_step_configure_history_toggle(self, options_flow: OptionsFlow) -> None:
        """Verify the history toggle appears in the configure form."""
        result = await options_flow.async_step_configure()
        form_schema = result["data_schema"]
        data = form_schema({})
        assert CONF_HISTORY_ENABLED in data

    def test_get_current_services_description(self, options_flow: OptionsFlow) -> None:
        """Ensure the service summary mentions history when enabled."""
        summary = options_flow._get_current_services_description()
        assert "Entity History" in summary


class TestDuplicateRemoval:
    """Regression tests for prior duplicate-step bugs."""

    def test_history_not_in_search_order(self) -> None:
        """History should not inject an extra step without a schema."""
        assert STEP_WEATHER in INITIAL_CONFIG_STEP_ORDER
        assert STEP_WIKIPEDIA in SEARCH_STEP_ORDER
        assert all(
            conf_key != CONF_HISTORY_ENABLED or schema_func is not None
            for conf_key, schema_func in SEARCH_STEP_ORDER.values()
        )
