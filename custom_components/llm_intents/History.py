import logging
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType
from homeassistant.util import dt as dt_util
from homeassistant.components.recorder.history import async_get_history

_LOGGER = logging.getLogger(__name__)


class EntityHistoryTool(llm.Tool):
    """Tool for retrieving entity history data."""

    name = "get_entity_history"
    description = "Retrieve historical data and state changes for Home Assistant entities. Use this to get past values, status changes, or trends for any entity."
    
    parameters = vol.Schema(
        {
            vol.Required("entity_id", description="The entity ID to get history for (e.g., 'sensor.temperature', 'light.living_room')"): str,
            vol.Optional("start_time", description="Start time in ISO format (e.g., '2025-10-30T10:00:00+00:00'). If not provided, defaults to 12 hours ago"): str,
            vol.Optional("end_time", description="End time in ISO format (e.g., '2025-10-30T18:00:00+00:00'). If not provided, defaults to now"): str,
            vol.Optional("since_entity_state", description="Get history since a specific entity state. Format: {'entity_id': 'person.tim', 'state': 'home'}"): dict,
            vol.Optional("summarize", description="Whether to summarize the data if there are many values. Defaults to true for more than 20 entries"): bool,
        }
    )

    def _parse_datetime(self, datetime_str: str) -> datetime | None:
        """Parse ISO datetime string into an aware datetime."""
        if not datetime_str:
            return None

        parsed = dt_util.parse_datetime(datetime_str)
        if parsed is None:
            _LOGGER.error("Failed to parse datetime '%s'", datetime_str)
            return None

        if parsed.tzinfo is None:
            parsed = dt_util.as_utc(parsed)

        return parsed

    def _get_default_time_range(self) -> tuple[datetime, datetime]:
        """Get default time range (last 12 hours)."""
        end_time = dt_util.utcnow()
        start_time = end_time - timedelta(hours=12)
        return start_time, end_time

    async def _find_last_state_change(self, hass: HomeAssistant, entity_id: str, target_state: str) -> datetime | None:
        """Find the last time an entity had a specific state."""
        try:
            # Get history for the last 7 days to find the state change
            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(days=7)
            history_data = await async_get_history(
                hass,
                start_time,
                end_time=end_time,
                entity_id=[entity_id],
                include_start_time_state=False,
                significant_changes_only=False,
            )

            if not history_data or entity_id not in history_data:
                return None

            history = history_data[entity_id]
            last_change_time: datetime | None = None

            # Iterate through history in reverse (newest first)
            for state_info in reversed(history):
                state_value = (
                    state_info.state if isinstance(state_info, State) else state_info.get("state")
                )
                if state_value == target_state:
                    last_changed = (
                        state_info.last_changed if isinstance(state_info, State) else self._parse_datetime(state_info.get("last_changed"))
                    )
                    if last_changed:
                        last_change_time = dt_util.as_utc(last_changed)
                        break

            return last_change_time

        except Exception as e:
            _LOGGER.error(f"Error finding last state change for {entity_id}: {e}")
            return None

    def _format_state_entry(self, state_info: State | dict) -> str:
        """Format a single state entry for LLM consumption."""
        if isinstance(state_info, State):
            timestamp = state_info.last_changed or state_info.last_updated
            state = state_info.state
            attributes = state_info.attributes
        else:
            timestamp = state_info.get("last_changed", state_info.get("last_updated"))
            state = state_info.get("state", "unknown")
            attributes = state_info.get("attributes", {})

        # Parse timestamp for better formatting
        try:
            if isinstance(timestamp, datetime):
                dt_obj = timestamp
                if dt_obj.tzinfo is None:
                    dt_obj = dt_util.as_utc(dt_obj)
                dt_obj = dt_util.as_local(dt_obj)
            else:
                dt_obj = self._parse_datetime(timestamp)
                if dt_obj:
                    dt_obj = dt_util.as_local(dt_obj)
            formatted_time = (
                dt_obj.strftime("%Y-%m-%d %H:%M:%S") if dt_obj else str(timestamp)
            )
        except Exception:
            formatted_time = timestamp

        # Format key attributes
        attr_parts = []
        if attributes:
            # Include most relevant attributes based on entity type
            relevant_attrs = ["temperature", "humidity", "brightness", "color_temp", "unit_of_measurement", "friendly_name"]
            for attr in relevant_attrs:
                if attr in attributes:
                    attr_parts.append(f"{attr}: {attributes[attr]}")

            # Include numeric attributes if they seem important
            for key, value in attributes.items():
                if isinstance(value, (int, float)) and key not in relevant_attrs and len(attr_parts) < 5:
                    attr_parts.append(f"{key}: {value}")

        attr_str = f", {', '.join(attr_parts)}" if attr_parts else ""
        return f"- [{formatted_time}] State: {state}{attr_str}"

    def _summarize_history(self, history: list[State | dict], entity_id: str) -> str:
        """Create a summary of history data."""
        if not history:
            return f"No history data available for {entity_id}"

        # Analyze states
        states = [
            entry.state if isinstance(entry, State) else entry.get("state")
            for entry in history
        ]
        unique_states = list(set(states))
        state_counts = {state: states.count(state) for state in unique_states}

        # Get numeric data if available
        numeric_values = []
        for entry in history:
            state_value = entry.state if isinstance(entry, State) else entry.get("state")
            try:
                if state_value is not None:
                    numeric_values.append(float(state_value))
            except (ValueError, AttributeError):
                pass

        # Get most recent state
        most_recent = history[-1] if history else None
        recent_time: datetime | str | None = None
        if most_recent:
            if isinstance(most_recent, State):
                recent_time = most_recent.last_changed or most_recent.last_updated
            else:
                recent_time = most_recent.get("last_changed", most_recent.get("last_updated"))

        recent_formatted = ""
        if recent_time:
            try:
                if isinstance(recent_time, datetime):
                    dt_obj = recent_time
                    if dt_obj.tzinfo is None:
                        dt_obj = dt_util.as_utc(dt_obj)
                    dt_obj = dt_util.as_local(dt_obj)
                else:
                    dt_obj = self._parse_datetime(recent_time)
                    if dt_obj:
                        dt_obj = dt_util.as_local(dt_obj)
                recent_formatted = dt_obj.strftime("%H:%M") if dt_obj else str(recent_time)
            except Exception:
                recent_formatted = str(recent_time)

        # Build summary
        summary_lines = [
            f"Summary for {entity_id}:",
            f"- Total state changes: {len(history)}",
            f"- States: {', '.join([f'{state} ({count}x)' for state, count in state_counts.items()])}",
        ]

        if numeric_values:
            summary_lines.extend([
                f"- Numeric range: {min(numeric_values):.2f} - {max(numeric_values):.2f}",
                f"- Average: {sum(numeric_values)/len(numeric_values):.2f}",
            ])

        if most_recent:
            recent_state = (
                most_recent.state if isinstance(most_recent, State) else most_recent.get("state")
            )
            summary_lines.append(f"- Most recent state: {recent_state} (since {recent_formatted})")

        # Show first few and last few entries for context
        if len(history) > 6:
            summary_lines.append("\nRecent changes:")
            for entry in history[-3:]:
                summary_lines.append(self._format_state_entry(entry))
        
        return "\n".join(summary_lines)

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        entity_id = tool_input.tool_args.get("entity_id")
        start_time_str = tool_input.tool_args.get("start_time")
        end_time_str = tool_input.tool_args.get("end_time")
        since_entity_state = tool_input.tool_args.get("since_entity_state")
        summarize = tool_input.tool_args.get("summarize")

        _LOGGER.info(f"History requested for entity: {entity_id}")

        try:
            if not entity_id or not isinstance(entity_id, str):
                return {"error": "entity_id is required"}

            # Determine time range
            if since_entity_state:
                # Use since_entity_state to determine start time
                since_entity = since_entity_state.get("entity_id")
                since_state = since_entity_state.get("state")
                
                if not since_entity or not since_state:
                    return {"error": "since_entity_state must include both 'entity_id' and 'state'"}
                
                start_time = await self._find_last_state_change(hass, since_entity, since_state)
                if not start_time:
                    return {"error": f"Could not find last time {since_entity} had state '{since_state}'"}
                
                # Add a small buffer to exclude the state change itself
                start_time = start_time + timedelta(seconds=1)
                end_time = datetime.now()
                
            else:
                # Use provided times or defaults
                default_start, default_end = self._get_default_time_range()
                if start_time_str:
                    start_time = self._parse_datetime(start_time_str)
                    if not start_time:
                        return {"error": f"Invalid start_time format: {start_time_str}"}
                else:
                    start_time = default_start

                if end_time_str:
                    end_time = self._parse_datetime(end_time_str)
                    if not end_time:
                        return {"error": f"Invalid end_time format: {end_time_str}"}
                else:
                    end_time = default_end

            # Validate time range
            if start_time >= end_time:
                return {"error": "start_time must be before end_time"}

            # Get history data
            history_data = await async_get_history(
                hass,
                start_time,
                end_time=end_time,
                entity_id=[entity_id],
                include_start_time_state=False,
                significant_changes_only=False,
            )

            if not history_data or entity_id not in history_data:
                return {"error": f"No history data found for entity {entity_id}"}

            history = history_data[entity_id]
            if not history:
                return {"error": f"No history data available for {entity_id} in the specified time range"}

            # Determine if we should summarize
            should_summarize = summarize or (summarize is None and len(history) > 20)

            if should_summarize:
                return self._summarize_history(history, entity_id)
            else:
                # Format all entries
                formatted_entries = [self._format_state_entry(entry) for entry in history]
                return f"History for {entity_id}:\n" + "\n".join(formatted_entries)
                
        except Exception as e:
            _LOGGER.error("History retrieval error: %s", e)
            return {"error": f"Error retrieving history: {e!s}"}
