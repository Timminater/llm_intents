"""Tests for the History tool."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import State
from homeassistant.util import dt as dt_util

from custom_components.llm_intents.History import EntityHistoryTool


class TestEntityHistoryTool:
    """Test the EntityHistoryTool."""

    def test_tool_initialization(self):
        """Test that the tool can be initialized."""
        tool = EntityHistoryTool()
        assert tool.name == "get_entity_history"
        assert "historical data" in tool.description.lower()

    def test_parse_datetime_valid(self):
        """Test parsing valid datetime strings."""
        tool = EntityHistoryTool()
        
        # Test ISO format with timezone
        dt = tool._parse_datetime("2025-10-30T14:30:00+00:00")
        assert dt is not None
        assert dt.year == 2025
        assert dt.month == 10
        assert dt.day == 30
        
        # Test ISO format with Z
        dt = tool._parse_datetime("2025-10-30T14:30:00Z")
        assert dt is not None
        assert dt.year == 2025

    def test_parse_datetime_invalid(self):
        """Test parsing invalid datetime strings."""
        tool = EntityHistoryTool()
        
        # Test invalid format
        dt = tool._parse_datetime("invalid-date")
        assert dt is None
        
        # Test empty string
        dt = tool._parse_datetime("")
        assert dt is None

    def test_get_default_time_range(self):
        """Test getting default time range."""
        tool = EntityHistoryTool()
        start_time, end_time = tool._get_default_time_range()
        
        assert isinstance(start_time, datetime)
        assert isinstance(end_time, datetime)
        assert end_time > start_time
        # Should be approximately 12 hours difference
        diff = end_time - start_time
        assert abs(diff.total_seconds() - 43200) < 60  # 12 hours = 43200 seconds

    def test_format_state_entry(self):
        """Test formatting of state entries."""
        tool = EntityHistoryTool()

        # Test using State object
        entry_state = State(
            "light.test",
            "on",
            {"brightness": 255},
            last_changed=datetime(2025, 10, 30, 14, 30, tzinfo=dt_util.UTC),
        )
        formatted_state = tool._format_state_entry(entry_state)
        assert "State: on" in formatted_state
        assert "brightness: 255" in formatted_state

        # Test entry without attributes (dict input)
        entry = {
            "last_changed": "2025-10-30T14:30:00+00:00",
            "state": "off",
        }
        formatted = tool._format_state_entry(entry)
        assert "State: off" in formatted
        assert "brightness" not in formatted

    def test_summarize_history(self):
        """Test history summarization."""
        tool = EntityHistoryTool()

        # Test with multiple state changes using State objects
        base_time = datetime(2025, 10, 30, 10, 0, tzinfo=dt_util.UTC)
        history = [
            State("light.test", "on", last_changed=base_time + timedelta(hours=0)),
            State("light.test", "off", last_changed=base_time + timedelta(hours=1)),
            State("light.test", "on", last_changed=base_time + timedelta(hours=2)),
            State("light.test", "off", last_changed=base_time + timedelta(hours=3)),
            State("light.test", "on", last_changed=base_time + timedelta(hours=4)),
        ]

        summary = tool._summarize_history(history, "light.test")
        assert "Summary for light.test" in summary
        assert "Total state changes: 5" in summary
        assert "on (3x)" in summary
        assert "off (2x)" in summary
        assert "Most recent state: on" in summary

    def test_summarize_numeric_history(self):
        """Test summarization with numeric values."""
        tool = EntityHistoryTool()

        # Test with numeric sensor data
        base_time = datetime(2025, 10, 30, 10, 0, tzinfo=dt_util.UTC)
        history = [
            State("sensor.temperature", "20.5", last_changed=base_time + timedelta(hours=0)),
            State("sensor.temperature", "22.1", last_changed=base_time + timedelta(hours=1)),
            State("sensor.temperature", "21.3", last_changed=base_time + timedelta(hours=2)),
            State("sensor.temperature", "23.7", last_changed=base_time + timedelta(hours=3)),
        ]

        summary = tool._summarize_history(history, "sensor.temperature")
        assert "Numeric range: 20.50 - 23.70" in summary
        assert "Average: 21.90" in summary

    def test_summarize_empty_history(self):
        """Test summarization with empty history."""
        tool = EntityHistoryTool()
        
        summary = tool._summarize_history([], "sensor.test")
        assert "No history data available" in summary

    @pytest.mark.asyncio
    async def test_find_last_state_change_success(self):
        """Test finding last state change successfully."""
        tool = EntityHistoryTool()
        hass = MagicMock()

        # Mock successful history response
        mock_result = {
            "person.tim": [
                State(
                    "person.tim",
                    "away",
                    last_changed=datetime(2025, 10, 30, 8, 0, tzinfo=dt_util.UTC),
                ),
                State(
                    "person.tim",
                    "home",
                    last_changed=datetime(2025, 10, 30, 9, 0, tzinfo=dt_util.UTC),
                ),
                State(
                    "person.tim",
                    "away",
                    last_changed=datetime(2025, 10, 30, 10, 0, tzinfo=dt_util.UTC),
                ),
                State(
                    "person.tim",
                    "home",
                    last_changed=datetime(2025, 10, 30, 14, 0, tzinfo=dt_util.UTC),
                ),
            ]
        }

        with patch(
            "custom_components.llm_intents.History.async_get_history",
            AsyncMock(return_value=mock_result),
        ):
            last_change = await tool._find_last_state_change(hass, "person.tim", "home")

        assert last_change is not None
        assert last_change.year == 2025
        assert last_change.month == 10
        assert last_change.day == 30
        assert last_change.hour == 14

    @pytest.mark.asyncio
    async def test_find_last_state_change_not_found(self):
        """Test finding last state change when state not found."""
        tool = EntityHistoryTool()
        hass = MagicMock()

        # Mock history response without target state
        mock_result = {
            "person.tim": [
                State(
                    "person.tim",
                    "away",
                    last_changed=datetime(2025, 10, 30, 8, 0, tzinfo=dt_util.UTC),
                ),
                State(
                    "person.tim",
                    "not_home",
                    last_changed=datetime(2025, 10, 30, 9, 0, tzinfo=dt_util.UTC),
                ),
            ]
        }

        with patch(
            "custom_components.llm_intents.History.async_get_history",
            AsyncMock(return_value=mock_result),
        ):
            last_change = await tool._find_last_state_change(hass, "person.tim", "home")

        assert last_change is None

    @pytest.mark.asyncio
    async def test_find_last_state_change_error(self):
        """Test finding last state change with error."""
        tool = EntityHistoryTool()
        hass = MagicMock()

        # Mock history call error
        with patch(
            "custom_components.llm_intents.History.async_get_history",
            AsyncMock(side_effect=Exception("Service error")),
        ):
            last_change = await tool._find_last_state_change(hass, "person.tim", "home")

        assert last_change is None
