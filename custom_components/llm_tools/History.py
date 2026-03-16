"""History tool for Home Assistant entity state timelines and summaries."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

import voluptuous as vol
from homeassistant.components.recorder.history import (
    get_significant_states,
    state_changes_during_period,
)
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistic_during_period,
)
from homeassistant.core import HomeAssistant, State, valid_entity_id
from homeassistant.helpers import llm
from homeassistant.helpers.recorder import get_instance
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonObjectType, JsonValueType

_LOGGER = logging.getLogger(__name__)

DEFAULT_LOOKBACK_HOURS = 12
INITIAL_SINCE_STATE_WINDOW = timedelta(days=1)
SUMMARY_ENTRY_THRESHOLD = 20
EXTRA_ATTRIBUTE_LIMIT = 4
RELEVANT_ATTRIBUTES = (
    "friendly_name",
    "unit_of_measurement",
    "temperature",
    "humidity",
    "brightness",
    "color_temp",
    "hvac_action",
    "percentage",
)
TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off"}


@dataclass(slots=True)
class SinceStateCriteria:
    """Normalized since-state lookup criteria."""

    entity_id: str
    state: str


@dataclass(slots=True)
class HistoryRequest:
    """Normalized history request."""

    entity_id: str
    start_time: datetime
    end_time: datetime
    summarize: bool | None
    resolved_from: str
    since_entity_state: SinceStateCriteria | None = None


@dataclass(slots=True)
class NormalizedHistoryEntry:
    """History entry used for formatting and analysis."""

    state: str
    last_changed: datetime | None
    last_updated: datetime
    effective_time: datetime
    is_start_state: bool
    attributes: dict[str, JsonValueType]


class HistoryToolError(Exception):
    """Base exception for history tool failures."""

    def __init__(
        self,
        message: str,
        *,
        entity_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.entity_id = entity_id
        self.start_time = start_time
        self.end_time = end_time


class HistoryValidationError(HistoryToolError):
    """Raised when tool input is invalid."""


class HistoryRecorderError(HistoryToolError):
    """Raised when recorder history/statistics cannot be read."""


class HistoryNotFoundError(HistoryToolError):
    """Raised when history could not be found for a request."""


class EntityHistoryTool(llm.Tool):
    """Tool for retrieving entity history data."""

    name = "get_entity_history"
    description = (
        "Retrieve historical data and state changes for Home Assistant entities. "
        "Use this to get past values, status changes, or trends for any entity."
    )

    parameters = vol.Schema(
        {
            vol.Required(
                "entity_id",
                description=(
                    "The entity ID to get history for "
                    "(e.g., 'sensor.temperature', 'light.living_room')"
                ),
            ): str,
            vol.Optional(
                "start_time",
                description=(
                    "Start time in ISO format "
                    "(e.g., '2025-10-30T10:00:00+00:00'). "
                    "If not provided, defaults to 12 hours before the end time"
                ),
            ): str,
            vol.Optional(
                "end_time",
                description=(
                    "End time in ISO format "
                    "(e.g., '2025-10-30T18:00:00+00:00'). "
                    "If not provided, defaults to now"
                ),
            ): str,
            vol.Optional(
                "since_entity_state",
                description=(
                    "Get history since a specific entity state. "
                    "Format: {'entity_id': 'person.tim', 'state': 'home'}"
                ),
            ): dict,
            vol.Optional(
                "summarize",
                description=(
                    "Whether to summarize the data. "
                    "Defaults to true for more than 20 entries"
                ),
            ): bool,
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

        return dt_util.as_utc(parsed)

    def _get_default_time_range(
        self, end_time: datetime | None = None
    ) -> tuple[datetime, datetime]:
        """Get default time range (last 12 hours)."""
        resolved_end_time = end_time or dt_util.utcnow()
        start_time = resolved_end_time - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
        return start_time, resolved_end_time

    def _get_recorder_instance(self, hass: HomeAssistant):
        """Return recorder instance or raise a user-facing error."""
        try:
            return get_instance(hass)
        except Exception as err:
            raise HistoryRecorderError("History recorder is not enabled") from err

    async def _async_get_state_history(
        self,
        hass: HomeAssistant,
        start_time: datetime,
        end_time: datetime,
        entity_id: str,
        *,
        include_start_time_state: bool = False,
        descending: bool = False,
        limit: int | None = None,
    ) -> dict[str, list[State]]:
        """Retrieve raw state-change history from the recorder."""
        instance = self._get_recorder_instance(hass)
        job = partial(
            state_changes_during_period,
            hass=hass,
            start_time=start_time,
            end_time=end_time,
            entity_id=entity_id,
            no_attributes=False,
            descending=descending,
            limit=limit,
            include_start_time_state=include_start_time_state,
        )
        try:
            return await instance.async_add_executor_job(job)
        except Exception as err:
            raise HistoryRecorderError(
                "History recorder is not enabled or unavailable",
                entity_id=entity_id,
                start_time=start_time,
                end_time=end_time,
            ) from err

    async def _async_get_significant_history(
        self,
        hass: HomeAssistant,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> list[State | dict[str, Any]]:
        """Retrieve full history including attribute-only updates."""
        instance = self._get_recorder_instance(hass)
        job = partial(
            get_significant_states,
            hass=hass,
            start_time=start_time,
            end_time=end_time,
            entity_ids=[entity_id],
            filters=None,
            include_start_time_state=True,
            significant_changes_only=False,
            minimal_response=False,
            no_attributes=False,
            compressed_state_format=False,
        )
        try:
            history_data = await instance.async_add_executor_job(job)
        except Exception as err:
            raise HistoryRecorderError(
                "History recorder is not enabled or unavailable",
                entity_id=entity_id,
                start_time=start_time,
                end_time=end_time,
            ) from err

        return self._extract_history_for_entity(history_data, entity_id)

    def _extract_history_for_entity(
        self,
        history_data: dict[str, list[State | dict[str, Any]]],
        entity_id: str,
    ) -> list[State | dict[str, Any]]:
        """Extract a single entity timeline from recorder results."""
        entity_key = entity_id.lower()
        history = history_data.get(entity_key) or history_data.get(entity_id)
        return list(history) if history else []

    def _normalize_bool(self, value: Any) -> bool | None:
        """Normalize flexible boolean-like values from LLM input."""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in TRUE_VALUES:
                return True
            if normalized in FALSE_VALUES:
                return False
        raise HistoryValidationError("summarize must be a boolean value")

    def _normalize_since_state(self, value: Any) -> SinceStateCriteria:
        """Validate and normalize since-state criteria."""
        if not isinstance(value, dict):
            raise HistoryValidationError(
                "since_entity_state must be an object with 'entity_id' and 'state'"
            )

        raw_entity_id = value.get("entity_id")
        raw_state = value.get("state")

        if not isinstance(raw_entity_id, str) or not raw_entity_id.strip():
            raise HistoryValidationError(
                "since_entity_state must include a valid 'entity_id'"
            )
        if not isinstance(raw_state, str) or not raw_state.strip():
            raise HistoryValidationError(
                "since_entity_state must include a valid 'state'"
            )

        entity_id = raw_entity_id.strip().lower()
        if not valid_entity_id(entity_id):
            raise HistoryValidationError(
                f"since_entity_state entity_id '{raw_entity_id}' is not valid"
            )

        return SinceStateCriteria(entity_id=entity_id, state=raw_state.strip())

    def _get_oldest_recorded_time(self, hass: HomeAssistant) -> datetime | None:
        """Return the oldest recorder timestamp if available."""
        instance = self._get_recorder_instance(hass)
        try:
            oldest_ts = getattr(instance.states_manager, "oldest_ts", None)
        except Exception as err:
            raise HistoryRecorderError("History recorder is not enabled") from err

        if oldest_ts is None:
            return None
        return dt_util.utc_from_timestamp(oldest_ts)

    def _get_state_value(self, state_info: State | dict[str, Any]) -> str:
        """Return normalized state value from a recorder entry."""
        if isinstance(state_info, State):
            return state_info.state
        return str(state_info.get("state", "unknown"))

    def _get_last_changed(self, state_info: State | dict[str, Any]) -> datetime | None:
        """Return last_changed from a recorder entry."""
        if isinstance(state_info, State):
            value = state_info.last_changed or state_info.last_updated
        else:
            value = state_info.get("last_changed") or state_info.get("last_updated")

        if value is None:
            return None
        if isinstance(value, datetime):
            return dt_util.as_utc(value)
        return self._parse_datetime(str(value))

    def _get_last_updated(self, state_info: State | dict[str, Any]) -> datetime | None:
        """Return last_updated from a recorder entry."""
        if isinstance(state_info, State):
            value = state_info.last_updated or state_info.last_changed
        else:
            value = state_info.get("last_updated") or state_info.get("last_changed")

        if value is None:
            return None
        if isinstance(value, datetime):
            return dt_util.as_utc(value)
        return self._parse_datetime(str(value))

    def _get_attributes(
        self, state_info: State | dict[str, Any]
    ) -> dict[str, JsonValueType]:
        """Extract relevant attributes in a JSON-safe structure."""
        raw_attributes: dict[str, Any]
        if isinstance(state_info, State):
            raw_attributes = dict(state_info.attributes)
        else:
            raw_attributes = dict(state_info.get("attributes", {}))

        attributes: dict[str, JsonValueType] = {}

        for key in RELEVANT_ATTRIBUTES:
            if key in raw_attributes:
                attributes[key] = self._to_json_value(raw_attributes[key])

        attribute_limit = len(RELEVANT_ATTRIBUTES) + EXTRA_ATTRIBUTE_LIMIT
        for key, value in raw_attributes.items():
            if key in attributes or len(attributes) >= attribute_limit:
                continue
            normalized = self._to_json_value(value)
            if isinstance(normalized, (int, float, bool)):
                attributes[key] = normalized
            elif isinstance(normalized, str) and len(normalized) <= 80:
                attributes[key] = normalized

        return attributes

    def _to_json_value(self, value: Any) -> JsonValueType:
        """Convert recorder data into JSON-safe values."""
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, datetime):
            return self._isoformat(value)
        if isinstance(value, dict):
            return {
                str(key): self._to_json_value(child)
                for key, child in value.items()
            }
        if isinstance(value, (list, tuple, set)):
            return [self._to_json_value(item) for item in value]
        return str(value)

    def _isoformat(self, value: datetime | None) -> str | None:
        """Return UTC ISO string."""
        if value is None:
            return None
        return dt_util.as_utc(value).isoformat()

    def _effective_time(
        self,
        state_info: State | dict[str, Any],
        start_time: datetime,
    ) -> datetime:
        """Return the effective timestamp for ordering and duration math."""
        last_updated = self._get_last_updated(state_info)
        last_changed = self._get_last_changed(state_info)
        candidate = last_updated or last_changed or start_time
        return max(candidate, start_time)

    def _normalize_history_entries(
        self,
        history: list[State | dict[str, Any]],
        start_time: datetime,
    ) -> list[NormalizedHistoryEntry]:
        """Normalize recorder history into a stable internal structure."""
        entries: list[NormalizedHistoryEntry] = []
        for index, state_info in enumerate(history):
            effective_time = self._effective_time(state_info, start_time)
            entries.append(
                NormalizedHistoryEntry(
                    state=self._get_state_value(state_info),
                    last_changed=self._get_last_changed(state_info),
                    last_updated=self._get_last_updated(state_info) or effective_time,
                    effective_time=effective_time,
                    is_start_state=index == 0 and effective_time == start_time,
                    attributes=self._get_attributes(state_info),
                )
            )
        return entries

    def _serialize_entries(
        self, entries: list[NormalizedHistoryEntry]
    ) -> list[dict[str, JsonValueType]]:
        """Serialize normalized entries into API response data."""
        serialized: list[dict[str, JsonValueType]] = []
        for entry in entries:
            serialized.append(
                {
                    "state": entry.state,
                    "last_changed": self._isoformat(entry.last_changed),
                    "last_updated": self._isoformat(entry.last_updated),
                    "is_start_state": entry.is_start_state,
                    "attributes": entry.attributes,
                }
            )
        return serialized

    def _format_local_time(self, value: datetime | None) -> str:
        """Format a datetime in local time for LLM-readable text."""
        if value is None:
            return "unknown"
        return dt_util.as_local(dt_util.as_utc(value)).strftime("%Y-%m-%d %H:%M:%S")

    def _format_attributes(self, attributes: dict[str, JsonValueType]) -> str:
        """Format a compact attribute suffix."""
        if not attributes:
            return ""
        parts = [f"{key}: {value}" for key, value in attributes.items()]
        return ", " + ", ".join(parts)

    def _format_entry_text(self, entry: NormalizedHistoryEntry) -> str:
        """Format an entry for the timeline output."""
        attr_text = self._format_attributes(entry.attributes)
        if entry.is_start_state:
            return (
                f"- State at range start: {entry.state}"
                f" (last changed {self._format_local_time(entry.last_changed)})"
                f"{attr_text}"
            )
        return (
            f"- [{self._format_local_time(entry.last_updated)}] "
            f"State: {entry.state}{attr_text}"
        )

    def _format_duration(self, seconds: float) -> str:
        """Format a duration in a compact human-readable form."""
        rounded = max(int(round(seconds)), 0)
        hours, remainder = divmod(rounded, 3600)
        minutes, secs = divmod(remainder, 60)
        parts: list[str] = []
        if hours:
            parts.append(f"{hours}h")
        if minutes:
            parts.append(f"{minutes}m")
        if secs or not parts:
            parts.append(f"{secs}s")
        return " ".join(parts)

    def _entry_times(
        self,
        entries: list[NormalizedHistoryEntry],
        end_time: datetime,
    ) -> list[tuple[NormalizedHistoryEntry, datetime, datetime]]:
        """Return entries paired with duration windows."""
        spans: list[tuple[NormalizedHistoryEntry, datetime, datetime]] = []
        for index, entry in enumerate(entries):
            span_start = entry.effective_time
            if index + 1 < len(entries):
                span_end = entries[index + 1].effective_time
            else:
                span_end = end_time
            if span_end < span_start:
                span_end = span_start
            spans.append((entry, span_start, span_end))
        return spans

    def _parse_float(self, value: str) -> float | None:
        """Parse numeric state values."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _build_categorical_metrics(
        self,
        entries: list[NormalizedHistoryEntry],
        end_time: datetime,
    ) -> dict[str, JsonValueType]:
        """Build metrics for non-numeric entities."""
        time_in_state: dict[str, float] = {}
        state_counts: dict[str, int] = {}

        for entry, _span_start, span_end in self._entry_times(entries, end_time):
            state_counts[entry.state] = state_counts.get(entry.state, 0) + 1
            duration = max((span_end - entry.effective_time).total_seconds(), 0.0)
            time_in_state[entry.state] = time_in_state.get(entry.state, 0.0) + duration

        return {
            "entry_count": len(entries),
            "most_recent_state": entries[-1].state,
            "time_in_state_seconds": {
                state: int(round(duration))
                for state, duration in time_in_state.items()
            },
            "state_counts": state_counts,
        }

    def _build_raw_numeric_metrics(
        self,
        entries: list[NormalizedHistoryEntry],
        end_time: datetime,
    ) -> dict[str, float] | None:
        """Build numeric metrics directly from raw history."""
        parsed_values = [
            value
            for value in (self._parse_float(entry.state) for entry in entries)
            if value is not None
        ]
        if not parsed_values:
            return None

        weighted_sum = 0.0
        weighted_seconds = 0.0
        first_value: float | None = None
        last_value: float | None = None

        for entry, _span_start, span_end in self._entry_times(entries, end_time):
            value = self._parse_float(entry.state)
            if value is None:
                continue
            if first_value is None:
                first_value = value
            last_value = value
            duration = max((span_end - entry.effective_time).total_seconds(), 0.0)
            weighted_sum += value * duration
            weighted_seconds += duration

        if first_value is None or last_value is None:
            return None

        average = (
            weighted_sum / weighted_seconds
            if weighted_seconds > 0
            else sum(parsed_values) / len(parsed_values)
        )
        return {
            "start": first_value,
            "end": last_value,
            "min": min(parsed_values),
            "max": max(parsed_values),
            "average": average,
            "change": last_value - first_value,
        }

    async def _async_get_statistics_summary(
        self,
        hass: HomeAssistant,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> dict[str, float] | None:
        """Return statistics summary for numeric entities when available."""
        instance = self._get_recorder_instance(hass)
        metadata_job = partial(get_metadata, hass, statistic_ids={entity_id})
        try:
            metadata = await instance.async_add_executor_job(metadata_job)
        except Exception as err:
            raise HistoryRecorderError(
                "History statistics are unavailable",
                entity_id=entity_id,
                start_time=start_time,
                end_time=end_time,
            ) from err

        if entity_id not in metadata:
            return None

        stats_job = partial(
            statistic_during_period,
            hass=hass,
            start_time=start_time,
            end_time=end_time,
            statistic_id=entity_id,
            types={"min", "mean", "max", "change"},
            units=None,
        )
        try:
            stats = await instance.async_add_executor_job(stats_job)
        except Exception as err:
            raise HistoryRecorderError(
                "History statistics are unavailable",
                entity_id=entity_id,
                start_time=start_time,
                end_time=end_time,
            ) from err

        metrics: dict[str, float] = {}
        for source_key, target_key in (
            ("min", "min"),
            ("max", "max"),
            ("mean", "average"),
            ("change", "change"),
        ):
            value = stats.get(source_key)
            if isinstance(value, (int, float)):
                metrics[target_key] = float(value)
        return metrics or None

    async def _async_has_statistics_metadata(
        self, hass: HomeAssistant, entity_id: str
    ) -> bool:
        """Return whether recorder statistics metadata exists for an entity."""
        instance = self._get_recorder_instance(hass)
        metadata_job = partial(get_metadata, hass, statistic_ids={entity_id})
        try:
            metadata = await instance.async_add_executor_job(metadata_job)
        except Exception:
            return False
        return entity_id in metadata

    def _build_numeric_summary_text(
        self,
        entity_id: str,
        entries: list[NormalizedHistoryEntry],
        metrics: dict[str, float],
        used_statistics: bool,
    ) -> str:
        """Build a readable summary for numeric entities."""
        summary_lines = [
            f"Summary for {entity_id}:",
            f"- Start value: {metrics['start']:.2f}",
            f"- End value: {metrics['end']:.2f}",
            f"- Range: {metrics['min']:.2f} to {metrics['max']:.2f}",
            f"- Average: {metrics['average']:.2f}",
            f"- Change: {metrics['change']:+.2f}",
            f"- Most recent update: {self._format_local_time(entries[-1].last_updated)}",
        ]
        if used_statistics:
            summary_lines.append("- Numeric summary source: recorder statistics")
        else:
            summary_lines.append("- Numeric summary source: raw history")
        if len(entries) > 3:
            summary_lines.append("")
            summary_lines.append("Recent updates:")
            for entry in entries[-3:]:
                summary_lines.append(self._format_entry_text(entry))
        return "\n".join(summary_lines)

    def _build_categorical_summary_text(
        self,
        entity_id: str,
        entries: list[NormalizedHistoryEntry],
        metrics: dict[str, JsonValueType],
    ) -> str:
        """Build a readable summary for non-numeric entities."""
        time_in_state = metrics["time_in_state_seconds"]
        assert isinstance(time_in_state, dict)
        state_lines = [
            f"{state} ({self._format_duration(float(duration))})"
            for state, duration in time_in_state.items()
        ]
        summary_lines = [
            f"Summary for {entity_id}:",
            f"- Most recent state: {entries[-1].state}",
            f"- Total recorded entries: {metrics['entry_count']}",
            f"- Time in state: {', '.join(state_lines)}",
            f"- Most recent update: {self._format_local_time(entries[-1].last_updated)}",
        ]
        if len(entries) > 3:
            summary_lines.append("")
            summary_lines.append("Recent updates:")
            for entry in entries[-3:]:
                summary_lines.append(self._format_entry_text(entry))
        return "\n".join(summary_lines)

    def _build_timeline_text(
        self,
        entity_id: str,
        entries: list[NormalizedHistoryEntry],
        request: HistoryRequest,
    ) -> str:
        """Build human-readable timeline output."""
        header = (
            f"History for {entity_id} from "
            f"{self._format_local_time(request.start_time)} to "
            f"{self._format_local_time(request.end_time)}:"
        )
        body = [self._format_entry_text(entry) for entry in entries]
        return "\n".join([header, *body])

    def _success_response(
        self,
        *,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
        mode: str,
        resolved_from: str,
        used_statistics: bool,
        entries: list[dict[str, JsonValueType]],
        metrics: dict[str, JsonValueType],
        result_text: str,
    ) -> JsonObjectType:
        """Build a success response that matches the tool contract."""
        return {
            "success": True,
            "result": result_text,
            "data": {
                "entity_id": entity_id,
                "start_time": self._isoformat(start_time),
                "end_time": self._isoformat(end_time),
                "mode": mode,
                "resolved_from": resolved_from,
                "used_statistics": used_statistics,
                "entries": entries,
                "metrics": metrics,
            },
        }

    def _error_response(
        self,
        message: str,
        *,
        entity_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> JsonObjectType:
        """Build an error response that matches the tool contract."""
        return {
            "success": False,
            "error": message,
            "data": {
                "entity_id": entity_id,
                "start_time": self._isoformat(start_time),
                "end_time": self._isoformat(end_time),
            },
        }

    def _entity_is_currently_known(self, hass: HomeAssistant, entity_id: str) -> bool:
        """Return whether an entity currently exists in Home Assistant state."""
        states = getattr(hass, "states", None)
        if states is None or not hasattr(states, "get"):
            return False
        try:
            return states.get(entity_id) is not None
        except Exception:
            return False

    async def _resolve_since_state(
        self,
        hass: HomeAssistant,
        criteria: SinceStateCriteria,
        end_time: datetime,
    ) -> datetime:
        """Resolve the last time a reference entity had a target state."""
        oldest_recorded_time = self._get_oldest_recorded_time(hass)
        lookback = INITIAL_SINCE_STATE_WINDOW

        while True:
            start_time = end_time - lookback
            reached_oldest = False

            if oldest_recorded_time is not None and start_time <= oldest_recorded_time:
                start_time = oldest_recorded_time
                reached_oldest = True

            history_data = await self._async_get_state_history(
                hass,
                start_time,
                end_time,
                criteria.entity_id,
                include_start_time_state=True,
                descending=True,
            )
            history = self._extract_history_for_entity(history_data, criteria.entity_id)

            for state_info in history:
                if self._get_state_value(state_info) != criteria.state:
                    continue
                match_time = (
                    self._get_last_changed(state_info)
                    or self._get_last_updated(state_info)
                )
                if match_time is not None:
                    return dt_util.as_utc(match_time)

            if reached_oldest:
                raise HistoryNotFoundError(
                    (
                        f"Could not find a recorded '{criteria.state}' state for "
                        f"{criteria.entity_id} within available history"
                    ),
                    entity_id=criteria.entity_id,
                    start_time=start_time,
                    end_time=end_time,
                )

            lookback *= 2

    async def _normalize_request(
        self, hass: HomeAssistant, tool_args: dict[str, Any]
    ) -> HistoryRequest:
        """Validate tool input and resolve request boundaries."""
        raw_entity_id = tool_args.get("entity_id")
        if not isinstance(raw_entity_id, str) or not raw_entity_id.strip():
            raise HistoryValidationError("entity_id is required")

        entity_id = raw_entity_id.strip().lower()
        if not valid_entity_id(entity_id):
            raise HistoryValidationError(
                f"entity_id '{raw_entity_id}' is not a valid Home Assistant entity ID"
            )

        summarize = self._normalize_bool(tool_args.get("summarize"))
        start_time_str = tool_args.get("start_time")
        end_time_str = tool_args.get("end_time")
        since_state_value = tool_args.get("since_entity_state")

        if end_time_str is not None:
            if not isinstance(end_time_str, str):
                raise HistoryValidationError(
                    "end_time must be an ISO datetime string",
                    entity_id=entity_id,
                )
            end_time = self._parse_datetime(end_time_str)
            if end_time is None:
                raise HistoryValidationError(
                    f"Invalid end_time format: {end_time_str}",
                    entity_id=entity_id,
                )
        else:
            end_time = dt_util.utcnow()

        since_entity_state = None
        if since_state_value is not None:
            since_entity_state = self._normalize_since_state(since_state_value)
            if start_time_str is not None:
                raise HistoryValidationError(
                    "start_time cannot be combined with since_entity_state",
                    entity_id=entity_id,
                    end_time=end_time,
                )
            try:
                start_time = await self._resolve_since_state(
                    hass, since_entity_state, end_time
                )
            except HistoryToolError as err:
                raise type(err)(
                    err.message,
                    entity_id=entity_id,
                    start_time=err.start_time,
                    end_time=err.end_time,
                ) from err
            resolved_from = "since_entity_state"
        else:
            default_start_time, _default_end_time = self._get_default_time_range(
                end_time
            )
            if start_time_str is not None:
                if not isinstance(start_time_str, str):
                    raise HistoryValidationError(
                        "start_time must be an ISO datetime string",
                        entity_id=entity_id,
                        end_time=end_time,
                    )
                start_time = self._parse_datetime(start_time_str)
                if start_time is None:
                    raise HistoryValidationError(
                        f"Invalid start_time format: {start_time_str}",
                        entity_id=entity_id,
                        end_time=end_time,
                    )
            else:
                start_time = default_start_time

            resolved_from = (
                "explicit_range"
                if start_time_str is not None or end_time_str is not None
                else "default"
            )

        if start_time >= end_time:
            raise HistoryValidationError(
                "start_time must be before end_time",
                entity_id=entity_id,
                start_time=start_time,
                end_time=end_time,
            )

        return HistoryRequest(
            entity_id=entity_id,
            start_time=start_time,
            end_time=end_time,
            summarize=summarize,
            resolved_from=resolved_from,
            since_entity_state=since_entity_state,
        )

    async def _build_summary(
        self,
        hass: HomeAssistant,
        request: HistoryRequest,
        entries: list[NormalizedHistoryEntry],
    ) -> tuple[str, dict[str, JsonValueType], bool]:
        """Build summary output and metrics."""
        raw_numeric_metrics = self._build_raw_numeric_metrics(entries, request.end_time)
        used_statistics = False

        if raw_numeric_metrics is not None:
            metrics: dict[str, JsonValueType] = {
                "entry_count": len(entries),
                **raw_numeric_metrics,
            }
            statistics_metrics = await self._async_get_statistics_summary(
                hass,
                request.entity_id,
                request.start_time,
                request.end_time,
            )
            if statistics_metrics is not None:
                metrics.update(statistics_metrics)
                used_statistics = True

            result_text = self._build_numeric_summary_text(
                request.entity_id,
                entries,
                {
                    "start": float(metrics["start"]),
                    "end": float(metrics["end"]),
                    "min": float(metrics["min"]),
                    "max": float(metrics["max"]),
                    "average": float(metrics["average"]),
                    "change": float(metrics["change"]),
                },
                used_statistics,
            )
            return result_text, metrics, used_statistics

        metrics = self._build_categorical_metrics(entries, request.end_time)
        result_text = self._build_categorical_summary_text(
            request.entity_id,
            entries,
            metrics,
        )
        return result_text, metrics, False

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        del llm_context

        entity_id = tool_input.tool_args.get("entity_id")
        _LOGGER.info("History requested for entity: %s", entity_id)

        try:
            request = await self._normalize_request(hass, tool_input.tool_args)
            history = await self._async_get_significant_history(
                hass,
                request.entity_id,
                request.start_time,
                request.end_time,
            )

            if not history:
                if self._entity_is_currently_known(hass, request.entity_id):
                    raise HistoryNotFoundError(
                        f"No recorded history found for entity {request.entity_id}",
                        entity_id=request.entity_id,
                        start_time=request.start_time,
                        end_time=request.end_time,
                    )
                if await self._async_has_statistics_metadata(hass, request.entity_id):
                    raise HistoryNotFoundError(
                        (
                            f"No recorded state history found for entity "
                            f"{request.entity_id} in the requested time range"
                        ),
                        entity_id=request.entity_id,
                        start_time=request.start_time,
                        end_time=request.end_time,
                    )
                raise HistoryNotFoundError(
                    f"Unknown entity_id: {request.entity_id}",
                    entity_id=request.entity_id,
                    start_time=request.start_time,
                    end_time=request.end_time,
                )

            entries = self._normalize_history_entries(history, request.start_time)
            serialized_entries = self._serialize_entries(entries)
            should_summarize = (
                request.summarize
                if request.summarize is not None
                else len(entries) > SUMMARY_ENTRY_THRESHOLD
            )

            if should_summarize:
                result_text, metrics, used_statistics = await self._build_summary(
                    hass,
                    request,
                    entries,
                )
                return self._success_response(
                    entity_id=request.entity_id,
                    start_time=request.start_time,
                    end_time=request.end_time,
                    mode="summary",
                    resolved_from=request.resolved_from,
                    used_statistics=used_statistics,
                    entries=serialized_entries,
                    metrics=metrics,
                    result_text=result_text,
                )

            result_text = self._build_timeline_text(request.entity_id, entries, request)
            return self._success_response(
                entity_id=request.entity_id,
                start_time=request.start_time,
                end_time=request.end_time,
                mode="timeline",
                resolved_from=request.resolved_from,
                used_statistics=False,
                entries=serialized_entries,
                metrics={"entry_count": len(entries)},
                result_text=result_text,
            )
        except HistoryToolError as err:
            return self._error_response(
                err.message,
                entity_id=err.entity_id
                or (entity_id.strip().lower() if isinstance(entity_id, str) else None),
                start_time=err.start_time,
                end_time=err.end_time,
            )
        except Exception as err:
            _LOGGER.exception("History retrieval error")
            return self._error_response(
                f"Error retrieving history: {err!s}",
                entity_id=entity_id.strip().lower() if isinstance(entity_id, str) else None,
            )
