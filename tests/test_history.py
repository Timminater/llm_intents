"""Tests for the History tool."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import State
from homeassistant.helpers import llm
from homeassistant.util import dt as dt_util

from custom_components.llm_tools.History import (
    EntityHistoryTool,
    HistoryRecorderError,
    HistoryRequest,
)


def _tool_input(**tool_args):
    return llm.ToolInput(tool_name="get_entity_history", tool_args=tool_args)


def _llm_context() -> llm.LLMContext:
    return llm.LLMContext(
        platform="test",
        context=None,
        language="en",
        assistant="assist",
        device_id=None,
    )


def _entry(
    state: str,
    timestamp: datetime,
    *,
    last_changed: datetime | None = None,
    attributes: dict | None = None,
) -> dict:
    return {
        "state": state,
        "last_changed": (last_changed or timestamp).isoformat(),
        "last_updated": timestamp.isoformat(),
        "attributes": attributes or {},
    }


class TestEntityHistoryTool:
    """Test the EntityHistoryTool."""

    def test_tool_initialization(self) -> None:
        """Test that the tool can be initialized."""
        tool = EntityHistoryTool()
        assert tool.name == "get_entity_history"
        assert "historical data" in tool.description.lower()

    def test_parse_datetime_valid(self) -> None:
        """Test parsing valid datetime strings."""
        tool = EntityHistoryTool()

        parsed = tool._parse_datetime("2025-10-30T14:30:00+00:00")
        assert parsed is not None
        assert parsed.year == 2025
        assert parsed.month == 10
        assert parsed.day == 30

        parsed = tool._parse_datetime("2025-10-30T14:30:00Z")
        assert parsed is not None
        assert parsed.tzinfo == dt_util.UTC

    def test_parse_datetime_invalid(self) -> None:
        """Test parsing invalid datetime strings."""
        tool = EntityHistoryTool()
        assert tool._parse_datetime("invalid-date") is None
        assert tool._parse_datetime("") is None

    def test_get_default_time_range_uses_end_time(self) -> None:
        """Default start should be derived from the chosen end time."""
        tool = EntityHistoryTool()
        end_time = datetime(2026, 3, 16, 12, 0, tzinfo=dt_util.UTC)

        start_time, resolved_end_time = tool._get_default_time_range(end_time)

        assert resolved_end_time == end_time
        assert start_time == end_time - timedelta(hours=12)

    def test_normalize_bool_accepts_string_false(self) -> None:
        """LLM-style boolean strings should normalize cleanly."""
        tool = EntityHistoryTool()

        assert tool._normalize_bool("false") is False
        assert tool._normalize_bool("0") is False
        assert tool._normalize_bool(False) is False
        assert tool._normalize_bool("true") is True

    @pytest.mark.asyncio
    async def test_resolve_since_state_expands_window_until_match(self) -> None:
        """since_entity_state should search beyond the initial window."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        end_time = datetime(2026, 3, 16, 18, 0, tzinfo=dt_util.UTC)
        match_time = datetime(2026, 3, 6, 8, 0, tzinfo=dt_util.UTC)

        history_side_effect = [
            {"person.tim": []},
            {"person.tim": [_entry("home", match_time)]},
        ]

        with (
            patch.object(
                tool,
                "_get_oldest_recorded_time",
                return_value=datetime(2026, 2, 1, tzinfo=dt_util.UTC),
            ),
            patch.object(
                tool,
                "_async_get_state_history",
                AsyncMock(side_effect=history_side_effect),
            ) as history_mock,
        ):
            result = await tool._resolve_since_state(
                hass,
                tool._normalize_since_state(
                    {"entity_id": "person.tim", "state": "home"}
                ),
                end_time,
            )

        assert result == match_time
        assert history_mock.await_count == 2

    @pytest.mark.asyncio
    async def test_resolve_since_state_finds_existing_start_state(self) -> None:
        """A state already active before the window should still resolve."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        end_time = datetime(2026, 3, 16, 18, 0, tzinfo=dt_util.UTC)
        match_time = datetime(2026, 3, 10, 7, 0, tzinfo=dt_util.UTC)

        with (
            patch.object(
                tool,
                "_get_oldest_recorded_time",
                return_value=datetime(2026, 3, 1, tzinfo=dt_util.UTC),
            ),
            patch.object(
                tool,
                "_async_get_state_history",
                AsyncMock(return_value={"person.tim": [_entry("home", end_time, last_changed=match_time)]}),
            ),
        ):
            result = await tool._resolve_since_state(
                hass,
                tool._normalize_since_state(
                    {"entity_id": "person.tim", "state": "home"}
                ),
                end_time,
            )

        assert result == match_time

    @pytest.mark.asyncio
    async def test_async_call_explicit_range_timeline_response(self) -> None:
        """Timeline calls should return the normalized success contract."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = State("light.test", "on")
        base_time = datetime(2026, 3, 16, 8, 0, tzinfo=dt_util.UTC)
        history = [
            _entry("on", base_time, attributes={"brightness": 10}),
            _entry(
                "on",
                base_time + timedelta(hours=1),
                last_changed=base_time,
                attributes={"brightness": 60},
            ),
            _entry("off", base_time + timedelta(hours=2), attributes={"brightness": 0}),
        ]

        with patch.object(
            tool,
            "_async_get_significant_history",
            AsyncMock(return_value=history),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="light.test",
                    start_time=base_time.isoformat(),
                    end_time=(base_time + timedelta(hours=3)).isoformat(),
                    summarize=False,
                ),
                _llm_context(),
            )

        assert result["success"] is True
        assert result["data"]["mode"] == "timeline"
        assert result["data"]["resolved_from"] == "explicit_range"
        assert result["data"]["start_time"] == base_time.isoformat()
        assert result["data"]["end_time"] == (
            base_time + timedelta(hours=3)
        ).isoformat()
        assert result["data"]["entries"][0]["is_start_state"] is True
        assert result["data"]["entries"][1]["attributes"]["brightness"] == 60
        assert "instruction" in result
        assert "Do not expose internal reasoning" in result["instruction"]
        assert "State at range start" in result["result"]

    @pytest.mark.asyncio
    async def test_async_call_since_state_summary_uses_statistics(self) -> None:
        """Summary calls should use recorder statistics when available."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = State("sensor.temperature", "23")
        start_time = datetime(2026, 3, 16, 9, 0, tzinfo=dt_util.UTC)
        end_time = datetime(2026, 3, 16, 12, 0, tzinfo=dt_util.UTC)
        history = [
            _entry("20", start_time, attributes={"unit_of_measurement": "C"}),
            _entry("22", start_time + timedelta(hours=1), attributes={"unit_of_measurement": "C"}),
            _entry("23", start_time + timedelta(hours=2), attributes={"unit_of_measurement": "C"}),
        ]

        with (
            patch.object(tool, "_resolve_since_state", AsyncMock(return_value=start_time)),
            patch.object(
                tool,
                "_async_get_significant_history",
                AsyncMock(return_value=history),
            ),
            patch.object(
                tool,
                "_async_get_statistics_summary",
                AsyncMock(
                    return_value={
                        "min": 20.0,
                        "max": 23.0,
                        "average": 21.5,
                        "change": 3.0,
                    }
                ),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.temperature",
                    end_time=end_time.isoformat(),
                    since_entity_state={"entity_id": "person.tim", "state": "home"},
                    summarize=True,
                ),
                _llm_context(),
            )

        assert result["success"] is True
        assert result["data"]["mode"] == "summary"
        assert result["data"]["resolved_from"] == "since_entity_state"
        assert result["data"]["used_statistics"] is True
        assert result["data"]["start_time"] == start_time.isoformat()
        assert result["data"]["end_time"] == end_time.isoformat()
        assert result["data"]["metrics"]["start"] == 20.0
        assert result["data"]["metrics"]["end"] == 23.0
        assert result["data"]["metrics"]["average"] == 21.5
        assert "recorder statistics" in result["result"]

    @pytest.mark.asyncio
    async def test_async_call_numeric_summary_falls_back_without_statistics(self) -> None:
        """Numeric summaries should still work without recorder statistics."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = State("sensor.power", "14")
        start_time = datetime(2026, 3, 16, 9, 0, tzinfo=dt_util.UTC)
        end_time = start_time + timedelta(hours=3)
        history = [
            _entry("10", start_time),
            _entry("12", start_time + timedelta(hours=1)),
            _entry("14", start_time + timedelta(hours=2)),
        ]

        with (
            patch.object(
                tool,
                "_async_get_significant_history",
                AsyncMock(return_value=history),
            ),
            patch.object(
                tool,
                "_async_get_statistics_summary",
                AsyncMock(return_value=None),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.power",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                    summarize=True,
                ),
                _llm_context(),
            )

        assert result["success"] is True
        assert result["data"]["used_statistics"] is False
        assert result["data"]["metrics"]["start"] == 10.0
        assert result["data"]["metrics"]["end"] == 14.0
        assert result["data"]["metrics"]["change"] == 4.0
        assert pytest.approx(result["data"]["metrics"]["average"], rel=1e-4) == 12.0
        assert "raw history" in result["result"]

    @pytest.mark.asyncio
    async def test_async_call_single_numeric_entry_still_uses_numeric_summary(self) -> None:
        """A single numeric datapoint should still be summarized numerically."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = State("sensor.power", "10")
        start_time = datetime(2026, 3, 16, 9, 0, tzinfo=dt_util.UTC)
        end_time = start_time + timedelta(hours=1)

        with (
            patch.object(
                tool,
                "_async_get_significant_history",
                AsyncMock(return_value=[_entry("10", start_time)]),
            ),
            patch.object(
                tool,
                "_async_get_statistics_summary",
                AsyncMock(return_value=None),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.power",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                    summarize=True,
                ),
                _llm_context(),
            )

        assert result["success"] is True
        assert result["data"]["mode"] == "summary"
        assert result["data"]["used_statistics"] is False
        assert result["data"]["metrics"]["start"] == 10.0
        assert result["data"]["metrics"]["end"] == 10.0
        assert result["data"]["metrics"]["min"] == 10.0
        assert result["data"]["metrics"]["max"] == 10.0
        assert result["data"]["metrics"]["average"] == 10.0
        assert result["data"]["metrics"]["change"] == 0.0
        assert "Start value: 10.00" in result["result"]

    @pytest.mark.asyncio
    async def test_async_call_invalid_datetime(self) -> None:
        """Invalid datetimes should return the normalized error contract."""
        tool = EntityHistoryTool()
        hass = MagicMock()

        result = await tool.async_call(
            hass,
            _tool_input(entity_id="sensor.test", start_time="invalid"),
            _llm_context(),
        )

        assert result["success"] is False
        assert "Invalid start_time format" in result["error"]
        assert result["data"]["entity_id"] == "sensor.test"
        assert result["data"]["start_time"] is None
        assert result["data"]["end_time"] is None
        assert "instruction" in result
        assert "plain text only" in result["instruction"]

    @pytest.mark.asyncio
    async def test_async_call_rejects_conflicting_since_state_and_start_time(self) -> None:
        """start_time and since_entity_state should not be accepted together."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        end_time = datetime(2026, 3, 16, 12, 0, tzinfo=dt_util.UTC)

        with patch("custom_components.llm_tools.History.dt_util.utcnow", return_value=end_time):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.test",
                    start_time=end_time.isoformat(),
                    since_entity_state={"entity_id": "person.tim", "state": "home"},
                ),
                _llm_context(),
            )

        assert result["success"] is False
        assert "cannot be combined" in result["error"]
        assert result["data"]["entity_id"] == "sensor.test"
        assert result["data"]["end_time"] == end_time.isoformat()

    @pytest.mark.asyncio
    async def test_async_call_since_state_lookup_error_keeps_requested_entity(self) -> None:
        """since_entity_state failures should still report the requested entity_id."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        end_time = datetime(2026, 3, 16, 12, 0, tzinfo=dt_util.UTC)

        with (
            patch("custom_components.llm_tools.History.dt_util.utcnow", return_value=end_time),
            patch.object(
                tool,
                "_resolve_since_state",
                AsyncMock(
                    side_effect=HistoryRecorderError(
                        "History recorder is not enabled",
                        entity_id="person.tim",
                        end_time=end_time,
                    )
                ),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.temperature",
                    since_entity_state={"entity_id": "person.tim", "state": "home"},
                ),
                _llm_context(),
            )

        assert result["success"] is False
        assert result["error"] == "History recorder is not enabled"
        assert result["data"]["entity_id"] == "sensor.temperature"
        assert result["data"]["end_time"] == end_time.isoformat()

    @pytest.mark.asyncio
    async def test_async_call_unknown_entity_error(self) -> None:
        """Unknown entities should return a distinct error."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = None
        start_time = datetime(2026, 3, 16, 8, 0, tzinfo=dt_util.UTC)
        end_time = start_time + timedelta(hours=1)

        with (
            patch.object(
                tool,
                "_async_get_significant_history",
                AsyncMock(return_value=[]),
            ),
            patch.object(
                tool,
                "_async_has_statistics_metadata",
                AsyncMock(return_value=False),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="sensor.missing",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                ),
                _llm_context(),
            )

        assert result["success"] is False
        assert result["error"] == "Unknown entity_id: sensor.missing"
        assert result["data"]["start_time"] == start_time.isoformat()
        assert result["data"]["end_time"] == end_time.isoformat()

    @pytest.mark.asyncio
    async def test_async_call_current_entity_without_recorded_history(self) -> None:
        """Known entities without recorder data should be reported clearly."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        hass.states.get.return_value = State("light.test", "on")
        start_time = datetime(2026, 3, 16, 8, 0, tzinfo=dt_util.UTC)
        end_time = start_time + timedelta(hours=1)

        with patch.object(
            tool,
            "_async_get_significant_history",
            AsyncMock(return_value=[]),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(
                    entity_id="light.test",
                    start_time=start_time.isoformat(),
                    end_time=end_time.isoformat(),
                ),
                _llm_context(),
            )

        assert result["success"] is False
        assert result["error"] == "No recorded history found for entity light.test"

    @pytest.mark.asyncio
    async def test_async_call_recorder_error_path(self) -> None:
        """Recorder failures should return a user-facing error payload."""
        tool = EntityHistoryTool()
        hass = MagicMock()
        start_time = datetime(2026, 3, 16, 8, 0, tzinfo=dt_util.UTC)
        end_time = start_time + timedelta(hours=1)

        with (
            patch.object(
                tool,
                "_normalize_request",
                AsyncMock(
                    return_value=HistoryRequest(
                        entity_id="sensor.test",
                        start_time=start_time,
                        end_time=end_time,
                        summarize=None,
                        resolved_from="explicit_range",
                    )
                ),
            ),
            patch.object(
                tool,
                "_async_get_significant_history",
                AsyncMock(
                    side_effect=HistoryRecorderError(
                        "History recorder is not enabled",
                        entity_id="sensor.test",
                        start_time=start_time,
                        end_time=end_time,
                    )
                ),
            ),
        ):
            result = await tool.async_call(
                hass,
                _tool_input(entity_id="sensor.test"),
                _llm_context(),
            )

        assert result["success"] is False
        assert result["error"] == "History recorder is not enabled"
        assert result["data"]["start_time"] == start_time.isoformat()
        assert result["data"]["end_time"] == end_time.isoformat()
