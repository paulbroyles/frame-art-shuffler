"""Tests for the calendar event override system.

Covers:
1. _parse_calendar_event_description — flag parsing from event description text
2. _parse_calendar_dt — HA calendar start/end value coercion to aware datetime
3. CONF_CALENDAR_SUPPRESS_MOODS flag in _resolve_active_moods — short-circuits all moods
"""

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.frame_art_shuffler import (
    _parse_calendar_dt,
    _parse_calendar_event_description,
)
from custom_components.frame_art_shuffler.const import (
    CONF_CALENDAR_SUPPRESS_MOODS,
    CONF_MOOD_OVERRIDES,
    CONF_MOOD_SENSOR,
)
from custom_components.frame_art_shuffler.shuffle import _resolve_active_moods


# ---------------------------------------------------------------------------
# 1. _parse_calendar_event_description
# ---------------------------------------------------------------------------

class TestParseCalendarEventDescription:
    """Tests for structured flag parsing from calendar event description text."""

    DEFAULTS = {
        "uid": None,
        "suppress_moods": False,
        "force_shuffle": False,
        "label": None,
        "linked_calendar": None,
        "linked_uid": None,
    }

    def test_none_description_returns_defaults(self):
        assert _parse_calendar_event_description(None) == self.DEFAULTS

    def test_empty_string_returns_defaults(self):
        assert _parse_calendar_event_description("") == self.DEFAULTS

    def test_no_flags_returns_defaults(self):
        assert _parse_calendar_event_description("Just a note.") == self.DEFAULTS

    def test_uid_parsed(self):
        result = _parse_calendar_event_description("uid: 550e8400-e29b-41d4-a716-446655440000")
        assert result["uid"] == "550e8400-e29b-41d4-a716-446655440000"

    def test_uid_absent_is_none(self):
        result = _parse_calendar_event_description("suppress_moods: true")
        assert result["uid"] is None

    def test_label_parsed(self):
        result = _parse_calendar_event_description("label: Star Wars Day")
        assert result["label"] == "Star Wars Day"

    def test_linked_calendar_parsed(self):
        result = _parse_calendar_event_description("linked_calendar: calendar.family")
        assert result["linked_calendar"] == "calendar.family"

    def test_linked_uid_parsed(self):
        result = _parse_calendar_event_description("linked_uid: google-uid-xyz")
        assert result["linked_uid"] == "google-uid-xyz"

    def test_all_fields_together(self):
        desc = (
            "uid: my-uuid\n"
            "label: Star Wars Day\n"
            "suppress_moods: true\n"
            "force_shuffle: false\n"
            "linked_calendar: calendar.family\n"
            "linked_uid: goog-123"
        )
        result = _parse_calendar_event_description(desc)
        assert result["uid"] == "my-uuid"
        assert result["label"] == "Star Wars Day"
        assert result["suppress_moods"] is True
        assert result["force_shuffle"] is False
        assert result["linked_calendar"] == "calendar.family"
        assert result["linked_uid"] == "goog-123"

    def test_suppress_moods_true(self):
        result = _parse_calendar_event_description("suppress_moods: true")
        assert result["suppress_moods"] is True

    def test_suppress_moods_false_explicit(self):
        result = _parse_calendar_event_description("suppress_moods: false")
        assert result["suppress_moods"] is False

    def test_suppress_moods_yes(self):
        result = _parse_calendar_event_description("suppress_moods: yes")
        assert result["suppress_moods"] is True

    def test_suppress_moods_1(self):
        result = _parse_calendar_event_description("suppress_moods: 1")
        assert result["suppress_moods"] is True

    def test_suppress_moods_case_insensitive(self):
        result = _parse_calendar_event_description("Suppress_Moods: TRUE")
        assert result["suppress_moods"] is True

    def test_suppress_moods_among_other_lines(self):
        desc = "Some notes here.\nsuppress_moods: true\nlinked_calendar: calendar.family"
        result = _parse_calendar_event_description(desc)
        assert result["suppress_moods"] is True

    def test_unknown_flags_ignored(self):
        assert _parse_calendar_event_description("unknown_flag: whatever\nfoo: bar") == self.DEFAULTS

    def test_lines_without_colon_ignored(self):
        assert _parse_calendar_event_description("suppress_moods true\nno colon here") == self.DEFAULTS

    def test_whitespace_around_key_and_value(self):
        result = _parse_calendar_event_description("  suppress_moods  :  true  ")
        assert result["suppress_moods"] is True

    def test_force_shuffle_true(self):
        result = _parse_calendar_event_description("force_shuffle: true")
        assert result["force_shuffle"] is True

    def test_force_shuffle_false_explicit(self):
        result = _parse_calendar_event_description("force_shuffle: false")
        assert result["force_shuffle"] is False

    def test_force_shuffle_absent_defaults_false(self):
        result = _parse_calendar_event_description("suppress_moods: true")
        assert result["force_shuffle"] is False

    def test_both_flags_together(self):
        desc = "suppress_moods: true\nforce_shuffle: true"
        result = _parse_calendar_event_description(desc)
        assert result["suppress_moods"] is True
        assert result["force_shuffle"] is True


# ---------------------------------------------------------------------------
# 2. _parse_calendar_dt
# ---------------------------------------------------------------------------

class TestParseCalendarDt:
    """Tests for datetime coercion from HA calendar event start/end values."""

    def test_aware_datetime_passthrough(self):
        dt = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        result = _parse_calendar_dt(dt)
        assert result == dt
        assert result.tzinfo is not None

    def test_naive_datetime_becomes_utc(self):
        dt = datetime(2026, 5, 4, 12, 0)
        result = _parse_calendar_dt(dt)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026 and result.hour == 12

    def test_date_object_becomes_utc_midnight(self):
        d = date(2026, 12, 25)
        result = _parse_calendar_dt(d)
        assert result.tzinfo == timezone.utc
        assert result.year == 2026
        assert result.month == 12
        assert result.day == 25
        assert result.hour == 0 and result.minute == 0

    def test_iso_string_with_tz(self):
        s = "2026-05-04T12:00:00+00:00"
        result = _parse_calendar_dt(s)
        assert result.tzinfo is not None
        assert result.year == 2026 and result.month == 5 and result.day == 4

    def test_iso_string_without_tz_becomes_utc(self):
        s = "2026-05-04T12:00:00"
        result = _parse_calendar_dt(s)
        assert result.tzinfo == timezone.utc

    def test_invalid_string_raises(self):
        import pytest
        with pytest.raises((ValueError, TypeError)):
            _parse_calendar_dt("not-a-date")

    def test_none_raises(self):
        import pytest
        with pytest.raises((ValueError, TypeError, AttributeError)):
            _parse_calendar_dt(None)


# ---------------------------------------------------------------------------
# 3. CONF_CALENDAR_SUPPRESS_MOODS in _resolve_active_moods
# ---------------------------------------------------------------------------

class TestCalendarSuppressMoods:
    """Tests for mood suppression via calendar event flag."""

    def _make_hass_with_sensor(self, entity_id: str, state_str: str):
        hass = MagicMock()
        state = MagicMock()
        state.state = state_str
        state.attributes = {}
        hass.states.get = MagicMock(
            side_effect=lambda eid: state if eid == entity_id else None
        )
        return hass

    def test_suppress_moods_true_returns_empty_regardless_of_sensor(self):
        hass = self._make_hass_with_sensor("sensor.moods", "christmas,winter")
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.moods",
            CONF_CALENDAR_SUPPRESS_MOODS: True,
        }
        assert _resolve_active_moods(hass, tv_config) == []

    def test_suppress_moods_true_returns_empty_regardless_of_overrides(self):
        hass = MagicMock()
        hass.states.get = MagicMock(return_value=None)
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas", "winter"],
            CONF_CALENDAR_SUPPRESS_MOODS: True,
        }
        assert _resolve_active_moods(hass, tv_config) == []

    def test_suppress_moods_true_overrides_both_sensor_and_overrides(self):
        hass = self._make_hass_with_sensor("sensor.moods", "night")
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.moods",
            CONF_MOOD_OVERRIDES: ["christmas"],
            CONF_CALENDAR_SUPPRESS_MOODS: True,
        }
        assert _resolve_active_moods(hass, tv_config) == []

    def test_suppress_moods_false_does_not_suppress(self):
        hass = self._make_hass_with_sensor("sensor.moods", "christmas")
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.moods",
            CONF_CALENDAR_SUPPRESS_MOODS: False,
        }
        result = _resolve_active_moods(hass, tv_config)
        assert result == ["christmas"]

    def test_suppress_moods_absent_does_not_suppress(self):
        """Default behavior when flag is not set — moods are active."""
        hass = self._make_hass_with_sensor("sensor.moods", "christmas")
        tv_config = {CONF_MOOD_SENSOR: "sensor.moods"}
        result = _resolve_active_moods(hass, tv_config)
        assert result == ["christmas"]
