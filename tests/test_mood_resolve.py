"""Tests for _resolve_active_moods() in shuffle.py.

Covers:
1. Sensor mood parsing — comma-separated state, 'moods' attribute list,
   unavailable/unknown state, empty state, no sensor bound
2. Override moods — no expiry, future expiry (still active), past expiry (ignored),
   bad expiry string (treated as expired)
3. Deduplication and ordering — sensor moods first, overrides appended,
   duplicates removed preserving first occurrence
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from custom_components.frame_art_shuffler.const import (
    CONF_MOOD_OVERRIDES,
    CONF_MOOD_OVERRIDE_EXPIRY,
    CONF_MOOD_SENSOR,
)
from custom_components.frame_art_shuffler.shuffle import _resolve_active_moods


def _make_state(state_str: str, moods_attr=None):
    """Build a minimal HA state mock."""
    s = MagicMock()
    s.state = state_str
    s.attributes = {}
    if moods_attr is not None:
        s.attributes = {"moods": moods_attr}
    return s


def _make_hass(sensor_entity: str | None = None, sensor_state=None):
    """Build a minimal hass mock that returns sensor_state for sensor_entity."""
    hass = MagicMock()
    if sensor_entity and sensor_state is not None:
        hass.states.get = MagicMock(
            side_effect=lambda eid: sensor_state if eid == sensor_entity else None
        )
    else:
        hass.states.get = MagicMock(return_value=None)
    return hass


# ---------------------------------------------------------------------------
# 1. Sensor mood parsing
# ---------------------------------------------------------------------------

class TestSensorMoodParsing:
    """Tests for reading active moods from the bound mood sensor entity."""

    def test_no_sensor_bound_returns_empty(self):
        hass = _make_hass()
        tv_config = {}  # no mood_sensor key
        assert _resolve_active_moods(hass, tv_config) == []

    def test_empty_sensor_entity_returns_empty(self):
        hass = _make_hass()
        tv_config = {CONF_MOOD_SENSOR: ""}
        assert _resolve_active_moods(hass, tv_config) == []

    def test_comma_separated_state(self):
        state = _make_state("night,winter")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == ["night", "winter"]

    def test_comma_separated_state_with_spaces(self):
        state = _make_state("night , winter , snow")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == ["night", "winter", "snow"]

    def test_single_mood_state(self):
        state = _make_state("christmas")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == ["christmas"]

    def test_moods_attribute_list(self):
        """Sensor state is ignored when 'moods' attribute is a list."""
        state = _make_state("2", moods_attr=["night", "winter"])
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == ["night", "winter"]

    def test_moods_attribute_list_with_empty_values_filtered(self):
        state = _make_state("2", moods_attr=["night", "", None, "winter"])
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        result = _resolve_active_moods(hass, tv_config)
        assert "night" in result
        assert "winter" in result
        assert "" not in result

    def test_unavailable_sensor_returns_empty(self):
        state = _make_state("unavailable")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == []

    def test_unknown_sensor_returns_empty(self):
        state = _make_state("unknown")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == []

    def test_empty_state_returns_empty(self):
        state = _make_state("")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {CONF_MOOD_SENSOR: "sensor.art_moods"}
        assert _resolve_active_moods(hass, tv_config) == []

    def test_missing_entity_returns_empty(self):
        """Sensor entity exists in config but HA has no state for it."""
        hass = _make_hass()  # hass.states.get always returns None
        tv_config = {CONF_MOOD_SENSOR: "sensor.nonexistent"}
        assert _resolve_active_moods(hass, tv_config) == []


# ---------------------------------------------------------------------------
# 2. Override moods
# ---------------------------------------------------------------------------

class TestOverrideMoods:
    """Tests for manually activated mood overrides with optional expiry."""

    def test_overrides_with_no_expiry_are_active(self):
        hass = _make_hass()
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas"],
        }
        assert _resolve_active_moods(hass, tv_config) == ["christmas"]

    def test_overrides_with_future_expiry_are_active(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        hass = _make_hass()
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas"],
            CONF_MOOD_OVERRIDE_EXPIRY: future,
        }
        assert _resolve_active_moods(hass, tv_config) == ["christmas"]

    def test_overrides_with_past_expiry_are_ignored(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        hass = _make_hass()
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas"],
            CONF_MOOD_OVERRIDE_EXPIRY: past,
        }
        assert _resolve_active_moods(hass, tv_config) == []

    def test_overrides_with_bad_expiry_string_are_ignored(self):
        """A malformed expiry string is treated as expired (safe default)."""
        hass = _make_hass()
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas"],
            CONF_MOOD_OVERRIDE_EXPIRY: "not-a-date",
        }
        assert _resolve_active_moods(hass, tv_config) == []

    def test_multiple_overrides(self):
        hass = _make_hass()
        tv_config = {
            CONF_MOOD_OVERRIDES: ["christmas", "winter"],
        }
        result = _resolve_active_moods(hass, tv_config)
        assert result == ["christmas", "winter"]

    def test_empty_overrides_list(self):
        hass = _make_hass()
        tv_config = {CONF_MOOD_OVERRIDES: []}
        assert _resolve_active_moods(hass, tv_config) == []


# ---------------------------------------------------------------------------
# 3. Deduplication and merging
# ---------------------------------------------------------------------------

class TestMoodMerging:
    """Tests for union of sensor + override moods with deduplication."""

    def test_sensor_and_overrides_merged(self):
        """Sensor moods and override moods are unioned."""
        state = _make_state("night")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.art_moods",
            CONF_MOOD_OVERRIDES: ["christmas"],
        }
        result = _resolve_active_moods(hass, tv_config)
        assert "night" in result
        assert "christmas" in result

    def test_sensor_moods_appear_before_overrides(self):
        """Sensor moods are listed first (sensor_moods + override_moods order)."""
        state = _make_state("winter")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.art_moods",
            CONF_MOOD_OVERRIDES: ["christmas"],
        }
        result = _resolve_active_moods(hass, tv_config)
        assert result.index("winter") < result.index("christmas")

    def test_duplicate_mood_in_sensor_and_override_deduplicated(self):
        """A mood active in both sensor and override appears only once."""
        state = _make_state("winter")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.art_moods",
            CONF_MOOD_OVERRIDES: ["winter", "christmas"],
        }
        result = _resolve_active_moods(hass, tv_config)
        assert result.count("winter") == 1
        assert "christmas" in result

    def test_expired_overrides_not_merged_with_sensor_moods(self):
        """Expired override moods are dropped; sensor moods are unaffected."""
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        state = _make_state("night")
        hass = _make_hass("sensor.art_moods", state)
        tv_config = {
            CONF_MOOD_SENSOR: "sensor.art_moods",
            CONF_MOOD_OVERRIDES: ["christmas"],
            CONF_MOOD_OVERRIDE_EXPIRY: past,
        }
        result = _resolve_active_moods(hass, tv_config)
        assert result == ["night"]

    def test_no_sensor_no_overrides_returns_empty(self):
        hass = _make_hass()
        tv_config = {}
        assert _resolve_active_moods(hass, tv_config) == []
