"""Sensor platform for Frame Art Shuffler TVs."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription, SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_NAME, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .config_entry import (
    get_active_tagset_name,
    get_effective_tags,
    get_tag_weights,
    get_tv_config,
    get_weighting_type,
    calculate_tag_percentages,
)
from .const import (
    CONF_ENABLE_AUTO_SHUFFLE,
    CONF_LIGHT_SENSOR,
    CONF_OVERRIDE_EXPIRY_TIME,
    CONF_OVERRIDE_TAGSET,
    CONF_SELECTED_TAGSET,
    DOMAIN,
    SIGNAL_SHUFFLE,
    SIGNAL_AUTO_SHUFFLE_NEXT,
    SIGNAL_ORIENTATION,
)
from .coordinator import FrameArtCoordinator
from .activity import FrameArtActivitySensor

# Signal names for event-driven updates
SIGNAL_BRIGHTNESS = f"{DOMAIN}_brightness_adjusted"  # {SIGNAL_BRIGHTNESS}_{entry_id}_{tv_id}

_LOGGER = logging.getLogger(__name__)

# Auto brightness interval (must match __init__.py)
AUTO_BRIGHTNESS_INTERVAL_MINUTES = 10


TV_DESCRIPTION = SensorEntityDescription(
    key="current_artwork",
    icon="mdi:image-frame",
    translation_key="current_artwork",
)

LAST_SHUFFLE_IMAGE_DESCRIPTION = SensorEntityDescription(
    key="last_shuffle_image",
    icon="mdi:image-multiple",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="last_shuffle_image",
)

LAST_SHUFFLE_TIMESTAMP_DESCRIPTION = SensorEntityDescription(
    key="last_shuffle_timestamp",
    icon="mdi:clock-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="last_shuffle_timestamp",
)

AUTO_SHUFFLE_NEXT_DESCRIPTION = SensorEntityDescription(
    key="auto_shuffle_next",
    icon="mdi:clock-fast",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_shuffle_next",
)

IP_DESCRIPTION = SensorEntityDescription(
    key="ip_address",
    icon="mdi:ip-network",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="ip_address",
)

MAC_DESCRIPTION = SensorEntityDescription(
    key="mac_address",
    icon="mdi:ethernet",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="mac_address",
)

MOTION_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="motion_sensor",
    icon="mdi:motion-sensor",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="motion_sensor",
)

LIGHT_SENSOR_DESCRIPTION = SensorEntityDescription(
    key="light_sensor",
    icon="mdi:brightness-auto",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="light_sensor",
)

AUTO_BRIGHT_LAST_ADJUST_DESCRIPTION = SensorEntityDescription(
    key="auto_bright_last_adjust",
    icon="mdi:clock-check-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_bright_last_adjust",
)

AUTO_BRIGHT_NEXT_ADJUST_DESCRIPTION = SensorEntityDescription(
    key="auto_bright_next_adjust",
    icon="mdi:clock-fast",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_bright_next_adjust",
)

AUTO_BRIGHT_TARGET_DESCRIPTION = SensorEntityDescription(
    key="auto_bright_target",
    icon="mdi:brightness-percent",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_bright_target",
)

AUTO_BRIGHT_SENSOR_LUX_DESCRIPTION = SensorEntityDescription(
    key="auto_bright_sensor_lux",
    icon="mdi:brightness-5",
    device_class=SensorDeviceClass.ILLUMINANCE,
    native_unit_of_measurement="lx",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_bright_sensor_lux",
)

AUTO_MOTION_LAST_MOTION_DESCRIPTION = SensorEntityDescription(
    key="auto_motion_last_motion",
    icon="mdi:clock-check-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_motion_last_motion",
)

AUTO_MOTION_OFF_AT_DESCRIPTION = SensorEntityDescription(
    key="auto_motion_off_at",
    icon="mdi:clock-alert-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="auto_motion_off_at",
)

CURRENT_MATTE_DESCRIPTION = SensorEntityDescription(
    key="current_matte",
    icon="mdi:image-filter-frames",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="current_matte",
)

CURRENT_FILTER_DESCRIPTION = SensorEntityDescription(
    key="current_filter",
    icon="mdi:image-filter-vintage",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="current_filter",
)

MATTE_FILTER_DESCRIPTION = SensorEntityDescription(
    key="matte_filter",
    icon="mdi:image-filter-frames",
    entity_category=EntityCategory.CONFIG,
    translation_key="matte_filter",
)

TAGS_COMBINED_DESCRIPTION = SensorEntityDescription(
    key="tags_combined",
    icon="mdi:tag-multiple",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="tags_combined",
)

SELECTED_TAGSET_DESCRIPTION = SensorEntityDescription(
    key="selected_tagset",
    icon="mdi:tag-check",
    translation_key="selected_tagset",
)

SELECTED_TAGSET_WEIGHTING_DESCRIPTION = SensorEntityDescription(
    key="selected_tagset_weighting",
    icon="mdi:scale-balance",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="selected_tagset_weighting",
)

OVERRIDE_TAGSET_DESCRIPTION = SensorEntityDescription(
    key="override_tagset",
    icon="mdi:tag-arrow-right",
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="override_tagset",
)

OVERRIDE_EXPIRY_DESCRIPTION = SensorEntityDescription(
    key="override_expiry",
    icon="mdi:clock-end",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="override_expiry",
)

MATCHING_IMAGE_COUNT_DESCRIPTION = SensorEntityDescription(
    key="matching_image_count",
    icon="mdi:image-multiple-outline",
    translation_key="shuffled_matching_images",
)

ORIENTATION_DESCRIPTION = SensorEntityDescription(
    key="orientation",
    icon="mdi:phone-rotate-landscape",
    translation_key="orientation",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities,
) -> None:
    """Set up Frame Art TV sensors for a config entry."""

    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FrameArtCoordinator = data["coordinator"]

    tracked: dict[str, tuple] = {}

    @callback
    def _process_tvs(tvs: Iterable[dict[str, Any]]) -> None:
        new_entities: list[SensorEntity] = []
        for tv in tvs:
            tv_id = tv.get("id")
            if not tv_id or tv_id in tracked:
                continue
            
            # Always-created sensors
            artwork_info_entity = FrameArtArtworkInfoSensor(hass, entry, tv_id)
            hass.data[DOMAIN][entry.entry_id].setdefault("artwork_sensors", {})[tv_id] = artwork_info_entity

            tv_entities: list[SensorEntity] = [
                FrameArtTVEntity(hass, entry, tv_id),
                FrameArtLastShuffleImageEntity(hass, entry, tv_id),
                FrameArtLastShuffleTimestampEntity(hass, entry, tv_id),
                FrameArtAutoShuffleNextEntity(hass, entry, tv_id),
                FrameArtIPEntity(entry, tv_id),
                FrameArtMACEntity(entry, tv_id),
                FrameArtMotionSensorEntity(entry, tv_id),
                FrameArtLightSensorEntity(entry, tv_id),
                FrameArtCurrentMatteEntity(hass, entry, tv_id),
                FrameArtCurrentFilterEntity(hass, entry, tv_id),
                FrameArtMatteFilterEntity(hass, entry, tv_id),
                FrameArtTagsCombinedEntity(hass, entry, tv_id),
                FrameArtSelectedTagsetEntity(hass, entry, tv_id),
                FrameArtSelectedTagsetWeightingEntity(hass, entry, tv_id),
                FrameArtOverrideTagsetEntity(hass, entry, tv_id),
                FrameArtOverrideExpiryEntity(hass, entry, tv_id),
                FrameArtMatchingImageCountEntity(hass, entry, tv_id),
                FrameArtActivitySensor(hass, entry, tv_id),
                FrameArtOrientationEntity(hass, entry, tv_id),
                artwork_info_entity,
            ]

            # Auto-brightness sensors (only if light sensor configured)
            if tv.get(CONF_LIGHT_SENSOR):
                tv_entities.extend([
                    FrameArtAutoBrightLastAdjustEntity(hass, entry, tv_id),
                    FrameArtAutoBrightNextAdjustEntity(hass, entry, tv_id),
                    FrameArtAutoBrightTargetEntity(hass, entry, tv_id),
                    FrameArtAutoBrightSensorLuxEntity(hass, entry, tv_id),
                ])

            # Auto-motion sensors (only if motion sensors configured)
            if tv.get("motion_sensors"):
                tv_entities.extend([
                    FrameArtAutoMotionLastMotionEntity(hass, entry, tv_id),
                    FrameArtAutoMotionOffAtEntity(hass, entry, tv_id),
                ])

            tracked[tv_id] = tuple(tv_entities)
            new_entities.extend(tv_entities)
            
        if new_entities:
            async_add_entities(new_entities)

    # Process initial TVs from coordinator data
    _process_tvs(coordinator.data or [])

    # Listen for new TVs (coordinator still tracks TV list for entity creation)
    @callback
    def _handle_coordinator_update() -> None:
        _process_tvs(coordinator.data or [])

    unsubscribe = coordinator.async_add_listener(_handle_coordinator_update)
    entry.async_on_unload(unsubscribe)


class FrameArtTVEntity(SensorEntity):
    """Sensor showing current artwork filename with entity_picture for dashboard cards."""

    entity_description = TV_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Current Artwork"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._entry = entry
        self._tv_id = tv_id
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()

        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass, signal, _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from signals."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the current artwork filename."""
        # Check runtime cache first (set by button.py shuffle)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        cached_image = shuffle_cache.get("current_image")
        if cached_image:
            return str(cached_image)

        # Fall back to config entry (for initial value after restart)
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None

        current = tv_config.get("current_image")
        if current:
            return str(current)

        # Fallback to legacy shuffle structure
        shuffle = tv_config.get("shuffle", {})
        if isinstance(shuffle, dict):
            current = shuffle.get("currentImage") or shuffle.get("current")
            if current:
                if isinstance(current, str) and "/" in current:
                    return current.split("/")[-1]
                return str(current)
        return "Unknown"

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None

    @property
    def entity_picture(self) -> str:
        """Return the URL to the current artwork image for picture-entity card."""
        current = self.native_value
        if current and current != "Unknown":
            return f"/local/frame_art/library/{current}"
        return "/local/frame_art/library/_black_placeholder.jpg"


class FrameArtLastShuffleImageEntity(SensorEntity):
    """Sensor entity for last shuffled image filename."""

    entity_description = LAST_SHUFFLE_IMAGE_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Last Shuffle Image"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_last_shuffle_image"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the last shuffled image filename."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        return tv_config.get("last_shuffle_image")

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtLastShuffleTimestampEntity(SensorEntity):
    """Sensor entity for last shuffle timestamp."""

    entity_description = LAST_SHUFFLE_TIMESTAMP_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Last Shuffle Timestamp"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_last_shuffle_timestamp"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return the last shuffle timestamp."""
        # Check runtime cache first (set by button.py shuffle)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        timestamp_str = shuffle_cache.get("last_shuffle_timestamp")
        
        # Fall back to config entry (for initial value after restart)
        if not timestamp_str:
            tv_config = get_tv_config(self._entry, self._tv_id)
            if tv_config:
                timestamp_str = tv_config.get("last_shuffle_timestamp")
        
        if not timestamp_str:
            return None
        
        try:
            dt = datetime.fromisoformat(timestamp_str)
            # Ensure timezone awareness if missing (assume local/system time if naive)
            if dt.tzinfo is None:
                from homeassistant.util import dt as dt_util
                return dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return dt
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtAutoShuffleNextEntity(SensorEntity):
    """Sensor entity showing next scheduled auto shuffle."""

    entity_description = AUTO_SHUFFLE_NEXT_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Shuffle Next"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_shuffle_next"
        self._unsubscribe: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        @callback
        def _auto_shuffle_next_updated() -> None:
            self.async_write_ha_state()

        signal = f"{SIGNAL_AUTO_SHUFFLE_NEXT}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe = async_dispatcher_connect(
            self._hass,
            signal,
            _auto_shuffle_next_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config or not tv_config.get(CONF_ENABLE_AUTO_SHUFFLE, False):
            return None

        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        next_times = data.get("auto_shuffle_next_times", {})
        next_time = next_times.get(self._tv_id)
        if next_time and isinstance(next_time, datetime):
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=timezone.utc)
            return next_time
        return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        return get_tv_config(self._entry, self._tv_id) is not None



class FrameArtIPEntity(SensorEntity):
    """Diagnostic sensor for TV IP address."""

    entity_description = IP_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "IP Address"

    def __init__(self, entry: ConfigEntry, tv_id: str) -> None:
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_ip"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the IP address."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        return tv_config.get("ip")


class FrameArtMACEntity(SensorEntity):
    """Diagnostic sensor for TV MAC address."""

    entity_description = MAC_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "MAC Address"

    def __init__(self, entry: ConfigEntry, tv_id: str) -> None:
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_mac"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the MAC address."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        return tv_config.get("mac")


class FrameArtMotionSensorEntity(SensorEntity):
    """Diagnostic sensor for TV motion sensor entity IDs."""

    entity_description = MOTION_SENSOR_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Motion Sensors"

    def __init__(self, entry: ConfigEntry, tv_id: str) -> None:
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_motion_sensor"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the motion sensor entity IDs as comma-separated string."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        sensors = tv_config.get("motion_sensors", [])
        return ", ".join(sensors) if sensors else None


class FrameArtLightSensorEntity(SensorEntity):
    """Diagnostic sensor for TV light sensor entity ID."""

    entity_description = LIGHT_SENSOR_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Light Source"

    def __init__(self, entry: ConfigEntry, tv_id: str) -> None:
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_light_sensor"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the light sensor entity ID."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        return tv_config.get("light_sensor")


class FrameArtAutoBrightLastAdjustEntity(SensorEntity):
    """Sensor for last auto brightness adjustment timestamp."""

    entity_description = AUTO_BRIGHT_LAST_ADJUST_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Last Adjust"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_bright_last"
        self._unsubscribe_brightness: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to brightness signal for updates."""
        @callback
        def _brightness_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_BRIGHTNESS}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_brightness = async_dispatcher_connect(
            self._hass,
            signal,
            _brightness_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from brightness signal."""
        if self._unsubscribe_brightness:
            self._unsubscribe_brightness()
            self._unsubscribe_brightness = None

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return the last auto brightness adjustment timestamp."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        
        timestamp_str = tv_config.get("last_auto_brightness_timestamp")
        if not timestamp_str:
            return None
        
        try:
            dt = datetime.fromisoformat(timestamp_str)
            if dt.tzinfo is None:
                from homeassistant.util import dt as dt_util
                return dt.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return dt
        except (ValueError, TypeError):
            return None


class FrameArtAutoBrightNextAdjustEntity(SensorEntity):
    """Sensor for next auto brightness adjustment timestamp."""

    entity_description = AUTO_BRIGHT_NEXT_ADJUST_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Next Adjust"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_bright_next"
        self._unsubscribe_brightness: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to brightness signal for updates."""
        @callback
        def _brightness_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_BRIGHTNESS}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_brightness = async_dispatcher_connect(
            self._hass,
            signal,
            _brightness_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from brightness signal."""
        if self._unsubscribe_brightness:
            self._unsubscribe_brightness()
            self._unsubscribe_brightness = None

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return the next auto brightness adjustment timestamp."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        
        # If auto brightness is not enabled, return None
        if not tv_config.get("enable_dynamic_brightness", False):
            return None
        
        # Get the actual scheduled next time from hass.data (set by the timer)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        next_times = data.get("auto_brightness_next_times", {})
        next_time = next_times.get(self._tv_id)
        
        if next_time and isinstance(next_time, datetime):
            if next_time.tzinfo is None:
                next_time = next_time.replace(tzinfo=timezone.utc)
            return next_time
        
        return None


class FrameArtAutoBrightTargetEntity(SensorEntity):
    """Sensor for calculated target brightness based on current lux."""

    entity_description = AUTO_BRIGHT_TARGET_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Target"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_bright_target"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )
        self._unsubscribe_light_sensor: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to light sensor state changes for real-time updates."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        light_sensor = tv_config.get("light_sensor") if tv_config else None
        
        if light_sensor:
            from homeassistant.helpers.event import async_track_state_change_event
            
            @callback
            def _light_sensor_changed(event: Any) -> None:
                """Handle light sensor state change."""
                self.async_write_ha_state()
            
            self._unsubscribe_light_sensor = async_track_state_change_event(
                self._hass,
                [light_sensor],
                _light_sensor_changed,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from light sensor state changes."""
        if self._unsubscribe_light_sensor:
            self._unsubscribe_light_sensor()
            self._unsubscribe_light_sensor = None

    @property
    def native_value(self) -> int | None:  # type: ignore[override]
        """Return the calculated target brightness based on current lux."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        
        # Get the light sensor entity ID
        light_sensor = tv_config.get("light_sensor")
        if not light_sensor:
            return None
        
        # Get current lux value from the sensor
        lux_state = self._hass.states.get(light_sensor)
        if not lux_state or lux_state.state in ("unavailable", "unknown"):
            return None
        
        try:
            current_lux = float(lux_state.state)
        except (ValueError, TypeError):
            return None
        
        # Get calibration values
        min_lux = tv_config.get("min_lux", 0)
        max_lux = tv_config.get("max_lux", 1000)
        min_brightness = tv_config.get("min_brightness", 1)
        max_brightness = tv_config.get("max_brightness", 10)
        
        # Avoid division by zero
        if max_lux <= min_lux:
            return None
        
        # Calculate normalized value (0-1) with clamping
        normalized = (current_lux - min_lux) / (max_lux - min_lux)
        normalized = max(0.0, min(1.0, normalized))
        
        # Calculate target brightness
        target = int(round(min_brightness + normalized * (max_brightness - min_brightness)))
        return max(min_brightness, min(max_brightness, target))


class FrameArtAutoBrightSensorLuxEntity(SensorEntity):
    """Sensor that mirrors the configured light sensor's lux value."""

    entity_description = AUTO_BRIGHT_SENSOR_LUX_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Bright Sensor Lux"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_bright_sensor_lux"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )
        self._unsubscribe_light_sensor: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        """Subscribe to light sensor state changes for real-time updates."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        light_sensor = tv_config.get("light_sensor") if tv_config else None
        
        if light_sensor:
            from homeassistant.helpers.event import async_track_state_change_event
            
            @callback
            def _light_sensor_changed(event: Any) -> None:
                """Handle light sensor state change."""
                self.async_write_ha_state()
            
            self._unsubscribe_light_sensor = async_track_state_change_event(
                self._hass,
                [light_sensor],
                _light_sensor_changed,
            )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from light sensor state changes."""
        if self._unsubscribe_light_sensor:
            self._unsubscribe_light_sensor()
            self._unsubscribe_light_sensor = None

    @property
    def native_value(self) -> float | None:  # type: ignore[override]
        """Return the current lux value from the configured light sensor."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        
        # Get the light sensor entity ID
        light_sensor = tv_config.get("light_sensor")
        if not light_sensor:
            return None
        
        # Get current lux value from the sensor
        lux_state = self._hass.states.get(light_sensor)
        if not lux_state or lux_state.state in ("unavailable", "unknown"):
            return None
        
        try:
            return float(lux_state.state)
        except (ValueError, TypeError):
            return None


class FrameArtAutoMotionLastMotionEntity(SensorEntity):
    """Sensor for last detected motion timestamp."""

    entity_description = AUTO_MOTION_LAST_MOTION_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Motion Last Motion"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_motion_last"
        self._last_motion: datetime | None = None
        self._unsubscribe_motion_sensor: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to motion detected signals for real-time updates."""
        @callback
        def _motion_detected() -> None:
            """Handle motion detected signal."""
            # Clear local cache to force read from config
            self._last_motion = None
            self.async_write_ha_state()
        
        signal = f"{DOMAIN}_motion_detected_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_motion_sensor = async_dispatcher_connect(
            self._hass,
            signal,
            _motion_detected,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from motion detected signals."""
        if self._unsubscribe_motion_sensor:
            self._unsubscribe_motion_sensor()
            self._unsubscribe_motion_sensor = None

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return the last motion timestamp."""
        # Check runtime cache first (set by motion handler)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        motion_cache = data.get("motion_cache", {})
        timestamp_str = motion_cache.get(self._tv_id)
        
        # Fall back to persisted config value (legacy)
        if not timestamp_str:
            tv_config = get_tv_config(self._entry, self._tv_id)
            if tv_config:
                timestamp_str = tv_config.get("last_motion_timestamp")
        
        if not timestamp_str:
            return None
        
        try:
            dt = datetime.fromisoformat(timestamp_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return None


class FrameArtAutoMotionOffAtEntity(SensorEntity):
    """Sensor for when TV will turn off due to no motion."""

    entity_description = AUTO_MOTION_OFF_AT_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Auto-Motion Off At"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_auto_motion_off_at"
        self._unsubscribe_dispatcher: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to off time update signals."""
        # Capture self references for use in callback
        entity = self
        tv_id = self._tv_id
        
        @callback
        def _off_time_updated() -> None:
            """Handle off time update signal."""
            entity.async_write_ha_state()
        
        signal = f"{DOMAIN}_motion_off_time_updated_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_dispatcher = async_dispatcher_connect(
            self._hass,
            signal,
            _off_time_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from off time update signals."""
        if self._unsubscribe_dispatcher:
            self._unsubscribe_dispatcher()
            self._unsubscribe_dispatcher = None

    @property
    def available(self) -> bool:
        """Return if entity is available (only when auto-motion is enabled)."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return False
        return tv_config.get("enable_motion_control", False)

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return when the TV will turn off."""
        # Get the scheduled off time from hass.data
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        motion_off_times = data.get("motion_off_times", {})
        off_time = motion_off_times.get(self._tv_id)
        
        if off_time and isinstance(off_time, datetime):
            if off_time.tzinfo is None:
                off_time = off_time.replace(tzinfo=timezone.utc)
            return off_time
        
        return None


class FrameArtCurrentMatteEntity(SensorEntity):
    """Sensor entity for current image matte."""

    entity_description = CURRENT_MATTE_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Current Matte"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_current_matte"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the current matte."""
        # Check runtime cache first (set by button.py shuffle)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        cached_matte = shuffle_cache.get("current_matte")
        if cached_matte:
            return str(cached_matte)
        return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtCurrentFilterEntity(SensorEntity):
    """Sensor entity for current image filter."""

    entity_description = CURRENT_FILTER_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Current Filter"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_current_filter"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the current filter."""
        # Check runtime cache first (set by button.py shuffle)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        cached_filter = shuffle_cache.get("current_filter")
        if cached_filter:
            return str(cached_filter)
        return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtMatteFilterEntity(SensorEntity):
    """Sensor entity combining matte and filter display."""

    entity_description = MATTE_FILTER_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Matte / Filter"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_matte_filter"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()
        
        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return combined matte / filter value."""
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        
        matte = shuffle_cache.get("current_matte") or "none"
        filter_val = shuffle_cache.get("current_filter") or "none"
        
        return f"{matte} / {filter_val}"

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtTagsCombinedEntity(SensorEntity):
    """Sensor entity combining include and exclude tags display."""

    entity_description = TAGS_COMBINED_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Tags"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_tags_combined"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return combined tags display: [+] include / [-] exclude.
        
        If any tag has a non-default weight, shows percentages:
        [+] zebra(57%), lion(29%), monkey(14%) / [-] blurry
        """
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        
        # Use effective tags (resolved from tagset cache)
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tagset_cache = entry_data.get("tagset_cache")
        tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
        include_tags, exclude_tags = get_effective_tags(self._entry, self._tv_id, tagsets=tagsets)
        tag_weights = get_tag_weights(self._entry, self._tv_id, tagsets=tagsets)
        
        # Check if any weight is non-default (not 1.0)
        has_custom_weights = any(
            tag_weights.get(tag, 1.0) != 1.0 for tag in include_tags
        )
        
        parts = []
        if include_tags:
            if has_custom_weights:
                # Show percentages
                percentages = calculate_tag_percentages(include_tags, tag_weights)
                tag_strs = [f"{tag}({percentages.get(tag, 0)}%)" for tag in include_tags]
                include_str = ", ".join(tag_strs)
            else:
                include_str = ", ".join(include_tags)
            parts.append(f"[+] {include_str}")
        if exclude_tags:
            exclude_str = ", ".join(exclude_tags)
            parts.append(f"[-] {exclude_str}")
        
        if not parts:
            return "none"
        
        return " / ".join(parts)

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtSelectedTagsetEntity(SensorEntity):
    """Sensor entity for the selected (permanent) tagset name."""

    entity_description = SELECTED_TAGSET_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Selected Tagset"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_selected_tagset"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the selected tagset name."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        return tv_config.get(CONF_SELECTED_TAGSET)

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtSelectedTagsetWeightingEntity(SensorEntity):
    """Sensor entity for the selected tagset's weighting type (image or tag)."""

    entity_description = SELECTED_TAGSET_WEIGHTING_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Selected Tagset Weighting"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_selected_tagset_weighting"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the weighting type of the selected tagset (image or tag)."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        tagset_cache = entry_data.get("tagset_cache")
        tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
        return get_weighting_type(self._entry, self._tv_id, tagsets=tagsets)

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtOverrideTagsetEntity(SensorEntity):
    """Sensor entity for the override (temporary) tagset name."""

    entity_description = OVERRIDE_TAGSET_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Override Tagset"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_override_tagset"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return the override tagset name, or 'none' if no override active."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return "none"
        return tv_config.get(CONF_OVERRIDE_TAGSET) or "none"

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtOverrideExpiryEntity(SensorEntity):
    """Sensor entity for when the override tagset expires."""

    entity_description = OVERRIDE_EXPIRY_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Override Expiry"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_override_expiry"

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    @property
    def native_value(self) -> datetime | None:  # type: ignore[override]
        """Return the override expiry time as datetime."""
        tv_config = get_tv_config(self._entry, self._tv_id)
        if not tv_config:
            return None
        expiry_str = tv_config.get(CONF_OVERRIDE_EXPIRY_TIME)
        if not expiry_str:
            return None
        try:
            return datetime.fromisoformat(expiry_str)
        except (ValueError, TypeError):
            return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtMatchingImageCountEntity(SensorEntity):
    """Sensor entity for count of images matching shuffle criteria."""

    entity_description = MATCHING_IMAGE_COUNT_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Shuffled Matching Images"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_matching_image_count"
        self._unsubscribe_shuffle: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to shuffle signal for updates."""
        @callback
        def _shuffle_updated() -> None:
            self.async_write_ha_state()

        signal = f"{SIGNAL_SHUFFLE}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_shuffle = async_dispatcher_connect(
            self._hass,
            signal,
            _shuffle_updated,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from shuffle signal."""
        if self._unsubscribe_shuffle:
            self._unsubscribe_shuffle()
            self._unsubscribe_shuffle = None

    @property
    def native_value(self) -> int | None:  # type: ignore[override]
        """Return the count of images matching shuffle criteria."""
        # Check runtime cache (set by button.py shuffle)
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        shuffle_cache = data.get("shuffle_cache", {}).get(self._tv_id, {})
        count = shuffle_cache.get("matching_image_count")
        if count is not None:
            return int(count)
        return None

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


class FrameArtOrientationEntity(SensorEntity):
    """Sensor for TV physical orientation (portrait or landscape).

    Reads from tv_status_cache populated by binary_sensor.py's polling loop,
    which calls get_tv_orientation() via the art WebSocket API. The last known
    value is retained when the TV is unreachable.
    """

    entity_description = ORIENTATION_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Orientation"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._tv_id = tv_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_orientation"
        self._unsubscribe_orientation: Callable[[], None] | None = None

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to orientation signal for updates."""
        @callback
        def _orientation_updated() -> None:
            self.async_write_ha_state()

        signal = f"{SIGNAL_ORIENTATION}_{self._entry.entry_id}_{self._tv_id}"
        self._unsubscribe_orientation = async_dispatcher_connect(
            self._hass, signal, _orientation_updated
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from orientation signal."""
        if self._unsubscribe_orientation:
            self._unsubscribe_orientation()
            self._unsubscribe_orientation = None

    @property
    def native_value(self) -> str | None:  # type: ignore[override]
        """Return 'portrait', 'landscape', or None if not yet known."""
        data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        status_cache = data.get("tv_status_cache", {})
        return status_cache.get(self._tv_id, {}).get("orientation")

    @property
    def available(self) -> bool:  # type: ignore[override]
        """Return if entity is available."""
        return get_tv_config(self._entry, self._tv_id) is not None


ARTWORK_INFO_DESCRIPTION = SensorEntityDescription(
    key="artwork_info",
    icon="mdi:palette",
    translation_key="artwork_info",
)


class FrameArtArtworkInfoSensor(SensorEntity, RestoreEntity):
    """Tracks what is currently displayed on the TV with its full artwork metadata.

    State is the Samsung content_id of the displayed artwork (stable and unique).
    Attributes carry whatever metadata was provided at display time — title, artist,
    medium, museum, source URL, etc. — with no fixed schema. For locally shuffled
    images the attributes include the filename and tags; for web-source images they
    include the rich metadata passed through the send_image service call.

    Source of truth for automations that drive external displays (e.g. eink).
    """

    entity_description = ARTWORK_INFO_DESCRIPTION
    _attr_has_entity_name = True
    _attr_name = "Displayed Artwork"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, tv_id: str) -> None:
        self._hass = hass
        self._entry = entry
        self._tv_id = tv_id
        self._attr_unique_id = f"{entry.entry_id}_{tv_id}_artwork_info"
        self._content_id: str | None = None
        self._artwork_attrs: dict[str, Any] = {}

        tv_config = get_tv_config(entry, tv_id)
        tv_name = tv_config.get("name", tv_id) if tv_config else tv_id
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, tv_id)},
            name=tv_name,
            manufacturer="Samsung",
            model="Frame TV",
        )

    async def async_added_to_hass(self) -> None:
        """Restore last known state on HA restart."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state not in (None, "unknown", "unavailable"):
            self._content_id = last_state.state
            self._artwork_attrs = dict(last_state.attributes)

    @property
    def native_value(self) -> str | None:
        return self._content_id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._artwork_attrs

    def set_artwork(
        self,
        content_id: str,
        metadata: dict[str, Any],
        source_type: str = "local",
    ) -> None:
        """Update the displayed artwork. Caller must call async_write_ha_state() after."""
        self._content_id = content_id
        self._artwork_attrs = {
            "source_type": source_type,
            **{k: v for k, v in metadata.items() if v is not None},
        }

    def set_external_artwork(self, content_id: str) -> None:
        """Update when an externally-set artwork is detected (no metadata available)."""
        self._content_id = content_id
        self._artwork_attrs = {"source_type": "external"}
