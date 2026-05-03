"""Constants for the Frame Art Shuffler integration."""

DOMAIN = "frame_art_shuffler"
DEFAULT_PORT = 8002
DEFAULT_TIMEOUT = 30

TOKEN_DIR_NAME = "frame_art_shuffler/tokens"
LOG_STORAGE_RELATIVE_PATH = "frame_art/logs"
LOG_EVENTS_FILENAME = "events.json"
LOG_SUMMARY_FILENAME = "summary.json"
LOG_TAG_NONE = "<none>"

CONF_METADATA_PATH = "metadata_path"
CONF_TOKEN_DIR = "token_dir"
CONF_SHORT_NAME = "short_name"
CONF_TAGS = "tags"
CONF_EXCLUDE_TAGS = "exclude_tags"
CONF_SHUFFLE_FREQUENCY = "shuffle_frequency_minutes"
CONF_NEXT_SHUFFLE_TIME = "next_shuffle_time"
CONF_ENABLE_AUTO_SHUFFLE = "enable_auto_shuffle"
CONF_TV_ID = "tv_id"
CONF_MOTION_SENSOR = "motion_sensor"  # Deprecated: use CONF_MOTION_SENSORS
CONF_MOTION_SENSORS = "motion_sensors"
CONF_LIGHT_SENSOR = "light_sensor"
CONF_MIN_LUX = "min_lux"
CONF_MAX_LUX = "max_lux"
CONF_MIN_BRIGHTNESS = "min_brightness"
CONF_MAX_BRIGHTNESS = "max_brightness"
CONF_ENABLE_DYNAMIC_BRIGHTNESS = "enable_dynamic_brightness"
CONF_ENABLE_MOTION_CONTROL = "enable_motion_control"
CONF_MOTION_OFF_DELAY = "motion_off_delay"
CONF_LOGGING_ENABLED = "logging_enabled"
CONF_LOG_RETENTION_MONTHS = "log_retention_months"
CONF_LOG_FLUSH_MINUTES = "log_flush_interval_minutes"

# Tagsets
CONF_TAGSETS = "tagsets"
CONF_SELECTED_TAGSET = "selected_tagset"
CONF_OVERRIDE_TAGSET = "override_tagset"
CONF_OVERRIDE_EXPIRY_TIME = "override_expiry_time"

# Moods
CONF_MOOD_SENSOR = "mood_sensor"          # entity_id of HA sensor providing active mood IDs
CONF_MOOD_OVERRIDES = "mood_overrides"    # list of mood IDs activated via service call
CONF_MOOD_OVERRIDE_EXPIRY = "mood_override_expiry"  # ISO timestamp for override expiry
CONF_MOOD_BASELINE_FLOOR = "mood_baseline_floor"  # float 0.0-1.0, reserved fraction for base rotation

DEFAULT_LOGGING_ENABLED = True
DEFAULT_LOG_RETENTION_MONTHS = 6
DEFAULT_LOG_FLUSH_MINUTES = 5

SIGNAL_SHUFFLE = f"{DOMAIN}_shuffle"
SIGNAL_AUTO_SHUFFLE_NEXT = f"{DOMAIN}_auto_shuffle_next"
SIGNAL_ORIENTATION = f"{DOMAIN}_orientation"  # {SIGNAL_ORIENTATION}_{entry_id}_{tv_id}
