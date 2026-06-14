"""Frame Art Shuffler integration base setup.

This integration provides art-focused control for Samsung Frame TVs:
- Upload and select artwork
- Manage art gallery (delete others, select images)
- Control art mode brightness
- Basic power control (screen on/off while staying in art mode)

It can work standalone or alongside Home Assistant's Samsung Smart TV integration.
See README.md for details on standalone vs. hybrid deployment modes.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import voluptuous as vol

from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send

_LOGGER = logging.getLogger(__name__)


def _write_artwork_sensor_log(log_path: Path, tv_name: str, content_id: str, source_type: str, metadata: dict) -> None:
    """Append one line to the artwork sensor update log (blocking; run via executor)."""
    import os
    os.makedirs(log_path.parent, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    title = metadata.get("title", "")
    creator = metadata.get("creator_name", metadata.get("creator", ""))
    keys = [k for k, v in metadata.items() if v not in (None, "", [])]
    line = f"{ts} | {tv_name} | {content_id[:8]} | {source_type} | title={title!r} creator={creator!r} keys={keys}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)


HA_IMPORT_ERROR_MESSAGE = (
    "Home Assistant is required for the Frame Art Shuffler integration. "
    "Install the 'homeassistant' package to enable integration entry points."
)

_HA_AVAILABLE = False

HomeAssistant: Any = Any
ConfigEntry: Any = Any
ServiceCall: Any = Any
Platform: Any = Any
HomeAssistantView: Any = Any
callback: Callable[..., Any] = lambda func, *args, **kwargs: func  # type: ignore[assignment]
dr = None

ha_spec = importlib.util.find_spec("homeassistant")
if ha_spec is not None:  # pragma: no cover - depends on optional dependency
    try:
        _config_entries = importlib.import_module("homeassistant.config_entries")
        _const = importlib.import_module("homeassistant.const")
        _core = importlib.import_module("homeassistant.core")
        _helpers_dr = importlib.import_module("homeassistant.helpers.device_registry")
        _helpers_er = importlib.import_module("homeassistant.helpers.entity_registry")
        _helpers_event = importlib.import_module("homeassistant.helpers.event")
        _exceptions = importlib.import_module("homeassistant.exceptions")
        _http = importlib.import_module("homeassistant.components.http")

        ConfigEntry = getattr(_config_entries, "ConfigEntry")
        Platform = getattr(_const, "Platform")
        HomeAssistant = getattr(_core, "HomeAssistant")
        callback = getattr(_core, "callback")
        ServiceCall = getattr(_core, "ServiceCall")
        SupportsResponse = getattr(_core, "SupportsResponse")
        ServiceValidationError = getattr(_exceptions, "ServiceValidationError")
        HomeAssistantError = getattr(_exceptions, "HomeAssistantError")
        dr = _helpers_dr
        er = _helpers_er
        async_track_time_interval = getattr(_helpers_event, "async_track_time_interval")
        HomeAssistantView = getattr(_http, "HomeAssistantView")
        _HA_AVAILABLE = True
    except ModuleNotFoundError:
        _HA_AVAILABLE = False

if _HA_AVAILABLE:
    from .const import (
        CONF_CALENDAR_ENTITY_ID,
        CONF_CALENDAR_SUPPRESS_MOODS,
        CONF_ENABLE_AUTO_SHUFFLE,
        CONF_LOGGING_ENABLED,
        CONF_LOG_FLUSH_MINUTES,
        CONF_LOG_RETENTION_MONTHS,
        CONF_METADATA_PATH,
        CONF_OVERRIDE_EXPIRY_TIME,
        CONF_OVERRIDE_TAGSET,
        CONF_SELECTED_TAGSET,
        CONF_SHUFFLE_FREQUENCY,
        CONF_TAGSETS,
        CONF_TOKEN_DIR,
        CONF_MOOD_SENSOR,
        CONF_MOOD_OVERRIDES,
        CONF_MOOD_OVERRIDE_EXPIRY,
        DOMAIN,
        SIGNAL_AUTO_SHUFFLE_NEXT,
    )
    from .display_log import DisplayLogManager
    from .coordinator import FrameArtCoordinator
    from .config_entry import get_tv_config, get_global_tagsets, list_tv_configs, remove_tv_config, update_tv_config, update_global_tagsets, get_effective_tags
    from . import frame_tv
    from .frame_tv import TOKEN_DIR as DEFAULT_TOKEN_DIR, TVConnectionManager, set_token_directory, tv_on, tv_off, set_art_mode, is_screen_on, get_tv_model_year
    from .image_cache import ImageMetadataCache
    from .tagset_cache import TagsetCache
    from .mood_cache import MoodCache
    from .dashboard import async_generate_dashboard
    from .activity import log_activity
    from .shuffle import async_guarded_upload, async_shuffle_tv

    PLATFORMS = [Platform.NUMBER, Platform.BUTTON, Platform.SENSOR, Platform.SWITCH, Platform.BINARY_SENSOR]
else:
    DEFAULT_TOKEN_DIR = Path(__file__).resolve().parent / "tokens"
    PLATFORMS: list[Any] = []


if _HA_AVAILABLE:

    async def async_setup(hass: Any, config: dict) -> bool:
        """Set up the Frame Art Shuffler integration."""
        hass.data.setdefault(DOMAIN, {})
        return True


    async def _async_setup_dashboard(hass: Any, entry: Any) -> None:
        """Generate the dashboard YAML file.
        
        Note: The dashboard must be manually registered in configuration.yaml.
        See README.md for setup instructions.
        """
        try:
            success = await async_generate_dashboard(hass, entry)
            if success:
                _LOGGER.info(
                    "Dashboard YAML generated at custom_components/frame_art_shuffler/dashboards/frame_tv_manager.yaml. "
                    "See README.md for manual registration instructions."
                )
            else:
                _LOGGER.debug("Dashboard generation skipped (no TVs configured)")
        except Exception as err:
            _LOGGER.warning(f"Failed to generate dashboard: {err}")


    def _get_structural_config(data: dict[str, Any]) -> dict[str, Any]:
        """Extract structural config data that requires a reload when changed.
        
        Structural changes include:
        - Adding/removing TVs
        - Changing IP/MAC addresses
        - Changing sensor entity IDs (requires re-attaching listeners)
        
        Runtime changes (skipped) include:
        - Toggling features (motion/brightness)
        - Changing thresholds/delays

        MAINTENANCE NOTE:
        If you add new configuration fields that require a component reload to take effect
        (e.g. new connection parameters, new entity IDs that need listeners), you MUST
        add them to the dictionary below. Otherwise, changing them will not trigger a reload.
        """
        tvs = data.get("tvs", {})
        structural = {
            "_calendar_entity_id": data.get(CONF_CALENDAR_ENTITY_ID),
        }
        for tv_id, tv_data in tvs.items():
            structural[tv_id] = {
                "ip": tv_data.get("ip"),
                "mac": tv_data.get("mac"),
                "name": tv_data.get("name"),
                "short_name": tv_data.get("short_name"),
                "motion_sensors": tv_data.get("motion_sensors", []),
                "light_sensor": tv_data.get("light_sensor"),
            }
        return structural


    async def _async_get_pool_filenames(
        hass: Any,
        manager_url: str,
        include_tags: list[str],
        exclude_tags: list[str],
    ) -> set[str]:
        """Fetch the set of eligible pool filenames from the manager add-on.

        Calls GET /api/shuffle/pool-filenames with tag filters.
        Returns an empty set on failure.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        import asyncio

        try:
            session = async_get_clientsession(hass)
            params: dict[str, str] = {}
            if include_tags:
                params["includeTags"] = ",".join(include_tags)
            if exclude_tags:
                params["excludeTags"] = ",".join(exclude_tags)
            url = f"{manager_url.rstrip('/')}/api/shuffle/pool-filenames"
            async with asyncio.timeout(10):
                async with session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return set(data.get("filenames", []))
                    _LOGGER.warning(
                        "pool-filenames: unexpected status %s", resp.status
                    )
        except Exception as err:
            _LOGGER.warning("pool-filenames: API error: %s", err)
        return set()


    # Default recency window settings (in hours)
    DEFAULT_SAME_TV_HOURS = 120
    DEFAULT_CROSS_TV_HOURS = 72
    MIN_RECENCY_HOURS = 6
    MAX_RECENCY_HOURS = 168  # 1 week

    def get_recency_windows(entry: Any) -> tuple[int, int]:
        """Get recency window settings from config entry options.

        Returns:
            Tuple of (same_tv_hours, cross_tv_hours)
        """
        same_tv = entry.options.get("same_tv_hours", DEFAULT_SAME_TV_HOURS)
        cross_tv = entry.options.get("cross_tv_hours", DEFAULT_CROSS_TV_HOURS)
        # Clamp to valid range
        same_tv = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, int(same_tv)))
        cross_tv = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, int(cross_tv)))
        return same_tv, cross_tv


    class PoolHealthView(HomeAssistantView):
        """API endpoint to get pool health data for all TVs."""

        url = "/api/frame_art_shuffler/pool_health"
        name = "api:frame_art_shuffler:pool_health"
        requires_auth = True

        def __init__(self, hass: Any, entry: Any) -> None:
            """Initialize the view."""
            self._hass = hass
            self._entry = entry

        async def get(self, request: Any) -> Any:
            """Handle GET request for pool health data.

            Optional query params for preview:
                same_tv_hours: Override same-TV window (6-168)
                cross_tv_hours: Override cross-TV window (6-168)
            """
            from aiohttp import web

            data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            if not data:
                return web.json_response(
                    {"error": "Integration data not available"},
                    status=500,
                )

            display_log = data.get("display_log")
            manager_url = data.get("manager_url")

            if not display_log or not manager_url:
                return web.json_response(
                    {"error": "Display log or manager URL not available"},
                    status=500,
                )

            # Get configured windows (from config entry options)
            configured_same_tv, configured_cross_tv = get_recency_windows(self._entry)

            # Allow query param overrides for preview (clamped to valid range)
            try:
                same_tv_hours = int(request.query.get("same_tv_hours", configured_same_tv))
                same_tv_hours = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, same_tv_hours))
            except (ValueError, TypeError):
                same_tv_hours = configured_same_tv

            try:
                cross_tv_hours = int(request.query.get("cross_tv_hours", configured_cross_tv))
                cross_tv_hours = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, cross_tv_hours))
            except (ValueError, TypeError):
                cross_tv_hours = configured_cross_tv

            tv_configs = list_tv_configs(self._entry)
            result: dict[str, Any] = {
                "tvs": {},
                "windows": {
                    "same_tv_hours": same_tv_hours,
                    "cross_tv_hours": cross_tv_hours,
                    "configured_same_tv_hours": configured_same_tv,
                    "configured_cross_tv_hours": configured_cross_tv,
                },
            }

            for tv_id, tv_config in tv_configs.items():
                tv_name = tv_config.get("name", tv_id)

                # Get shuffle frequency for this TV (default 60 minutes)
                shuffle_frequency = int(tv_config.get(CONF_SHUFFLE_FREQUENCY, 60) or 60)

                # Get effective tags for this TV (resolves tagset from cache).
                # Ensure cache is loaded; no-op if already loaded, single fetch if startup failed.
                tagset_cache = data.get("tagset_cache")
                if tagset_cache:
                    await tagset_cache.async_ensure_loaded()
                tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
                include_tags, exclude_tags = get_effective_tags(self._entry, tv_id, tagsets=tagsets)

                # Calculate pool filenames
                pool_filenames = await _async_get_pool_filenames(
                    self._hass,
                    manager_url,
                    include_tags,
                    exclude_tags,
                )

                # Get pool health from display log
                health = display_log.get_pool_health(
                    tv_id=tv_id,
                    pool_filenames=pool_filenames,
                    same_tv_hours=same_tv_hours,
                    cross_tv_hours=cross_tv_hours,
                )

                # Get historical pool health for sparkline (last 7 days)
                history = display_log.get_pool_health_history(
                    tv_id=tv_id,
                    hours=168,
                )

                result["tvs"][tv_id] = {
                    "name": tv_name,
                    "shuffle_frequency_minutes": shuffle_frequency,
                    "history": history,
                    **health,
                }

            return web.json_response(result)


    class FrameArtImageView(HomeAssistantView):
        """Proxy endpoint that serves artwork images from the Frame Art Manager add-on.

        Fetches thumbnails (or falls back to full-size) from the manager's HTTP
        server and streams them to the HA frontend.  Used by the Current Artwork
        sensor's entity_picture property so dashboard cards can display artwork.
        """

        url = "/api/frame_art_shuffler/image/{filename}"
        name = "api:frame_art_shuffler:image"
        requires_auth = False  # img src requests don't carry auth headers; images are non-sensitive

        def __init__(self, hass: Any, entry: Any) -> None:
            self._hass = hass
            self._entry = entry

        async def get(self, request: Any, filename: str) -> Any:
            """Proxy an image from the manager's thumbnail or library directory."""
            import aiohttp
            from aiohttp import web
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            # Basic path-traversal guard
            if "/" in filename or "\\" in filename or ".." in filename:
                return web.Response(status=400)

            data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
            manager_url = data.get("manager_url") if data else None
            if not manager_url:
                return web.Response(status=503)

            session = async_get_clientsession(self._hass)

            # Try thumbnail, then full library image, then web source cache
            thumb_url = f"{manager_url}/thumbs/thumb_{filename}"
            full_url = f"{manager_url}/library/{filename}"
            cache_url = f"{manager_url}/cache/{filename}"

            for url in (thumb_url, full_url, cache_url):
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            body = await resp.read()
                            content_type = resp.headers.get("Content-Type", "image/jpeg")
                            return web.Response(body=body, content_type=content_type.split(";")[0].strip())
                except Exception:  # noqa: BLE001
                    continue

            return web.Response(status=404)




    async def _async_invalidate_web_prefetch_for_tv(
        hass: Any,
        entry: Any,
        tv_id: str,
        new_tagset_name: str | None,
    ) -> None:
        """Invalidate the web-source pre-fetch for a TV, then kick off a fresh one.

        Called after a tagset assignment change so the pre-fetched image
        (which was optimised for the old tagset's virtual tag) is replaced
        as quickly as possible.
        """
        from .shuffle import _async_trigger_prefetch  # noqa: E402 - late import
        from .shuffle import _async_select_image       # noqa: E402
        from homeassistant.helpers.aiohttp_client import async_get_clientsession  # noqa: E402

        mgr_url: str = entry.data.get("frame_art_manager_url", "http://localhost:8099")
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, tv_id)})
        if not device:
            return
        device_id = device.id
        session = async_get_clientsession(hass)

        # Invalidate the stored pre-fetch.
        try:
            async with asyncio.timeout(5):
                await session.delete(f"{mgr_url}/api/web-sources/prefetch/{device_id}")
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not delete pre-fetch for %s (non-fatal): %s", tv_id, err)

        # Ask the add-on to pick the next image using the new tagset's virtual tag.
        try:
            tv_name = entry.data.get("tvs", {}).get(tv_id, {}).get("name", tv_id)
            selected, _, _, _, _ = await _async_select_image(
                hass, mgr_url, new_tagset_name, None, tv_name, recent_images=None
            )
            if selected and selected.get("_web_sources") and (vtid := selected.get("_virtual_tag_id")):
                await _async_trigger_prefetch(hass, mgr_url, device_id, vtid)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("Could not start pre-fetch for %s after tagset change (non-fatal): %s", tv_id, err)

    def _parse_calendar_event_description(description: str | None) -> dict:
        """Parse structured flags from a Frame Art calendar event description.

        Supported keys (one per line, case-insensitive):
            uid: <uuid>              — stable manager-assigned identity
            suppress_moods: true|false
            force_shuffle: true|false
            label: <display name>
            linked_calendar: <entity_id>
            linked_uid: <uid>
        """
        result: dict = {
            "uid": None,
            "suppress_moods": False,
            "force_shuffle": False,
            "label": None,
            "linked_calendar": None,
            "linked_uid": None,
        }
        if not description:
            return result
        for line in description.splitlines():
            line = line.strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            value_lower = value.lower()
            if key == "uid":
                result["uid"] = value or None
            elif key == "suppress_moods":
                result["suppress_moods"] = value_lower in ("true", "1", "yes")
            elif key == "force_shuffle":
                result["force_shuffle"] = value_lower in ("true", "1", "yes")
            elif key == "label":
                result["label"] = value or None
            elif key == "linked_calendar":
                result["linked_calendar"] = value or None
            elif key == "linked_uid":
                result["linked_uid"] = value or None
        return result

    async def _async_setup_calendar_monitor(
        hass: Any,
        entry: Any,
        list_tv_configs_fn: Any,
        update_tv_config_fn: Any,
        start_tagset_override_timer_fn: Any,
        cancel_tagset_override_timer_fn: Any,
    ) -> None:
        """Set up the calendar event monitor for tagset overrides.

        Watches the configured HA calendar entity and applies/clears tagset
        overrides on all TVs as calendar events start and end. Event title must
        match a tagset name exactly. Mood suppression is per-event opt-in via
        'suppress_moods: true' in the event description.
        """
        from homeassistant.helpers.event import (
            async_track_point_in_time,
            async_track_state_change_event,
            async_track_time_interval,
        )

        calendar_entity_id: str | None = entry.data.get(CONF_CALENDAR_ENTITY_ID)
        if not calendar_entity_id:
            return

        _LOGGER.debug("Calendar monitor: watching %s", calendar_entity_id)

        # active_calendar_events: uid -> {tagset, suppress_moods, end_time, end_unsub}
        active_calendar_events: dict[str, dict] = {}
        # upcoming_start_unsubs: uid -> unsubscribe callable for start timer
        upcoming_start_unsubs: dict[str, Any] = {}

        async def _async_apply_calendar_event(event_data: dict) -> None:
            """Apply a calendar event as a tagset override on all TVs."""
            uid = event_data.get("uid") or event_data.get("summary", "")
            tagset_name = (event_data.get("summary") or "").strip()
            flags = event_data.get("_flags") or _parse_calendar_event_description(
                event_data.get("description")
            )
            suppress_moods = flags["suppress_moods"]
            force_shuffle = flags["force_shuffle"]

            end_dt: datetime | None = event_data.get("_end_dt")
            if not end_dt:
                return

            if not tagset_name:
                _LOGGER.warning("Calendar monitor: event has no title, skipping")
                return

            # Validate tagset exists
            tagset_cache = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("tagset_cache")
            tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
            if tagsets is None:
                tagsets = get_global_tagsets(entry)
            if tagset_name not in (tagsets or {}):
                if tagset_cache:
                    await tagset_cache.async_refresh()
                    tagsets = tagset_cache.get_all() or tagsets
            if tagset_name not in (tagsets or {}):
                _LOGGER.warning(
                    "Calendar monitor: tagset '%s' not found, skipping event", tagset_name
                )
                return

            _LOGGER.info(
                "Calendar monitor: applying event '%s' (suppress_moods=%s, force_shuffle=%s) until %s",
                tagset_name, suppress_moods, force_shuffle, end_dt,
            )

            # Apply override to all TVs
            for tv_id in list_tv_configs_fn(entry):
                update_tv_config_fn(hass, entry, tv_id, {
                    CONF_OVERRIDE_TAGSET: tagset_name,
                    CONF_OVERRIDE_EXPIRY_TIME: end_dt.isoformat(),
                    CONF_CALENDAR_SUPPRESS_MOODS: suppress_moods,
                })
                start_tagset_override_timer_fn(tv_id, end_dt)
                async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{entry.entry_id}_{tv_id}")
                hass.async_create_background_task(
                    _async_invalidate_web_prefetch_for_tv(hass, entry, tv_id, tagset_name),
                    name=f"cal-prefetch-{tv_id}",
                )
                log_activity(
                    hass, entry.entry_id, tv_id,
                    "calendar_event_started",
                    f"Calendar event: override '{tagset_name}' active until {end_dt.strftime('%H:%M')}",
                )
                if force_shuffle or not _tv_is_displaying(hass, entry, tv_id):
                    hass.data[DOMAIN][entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                    hass.async_create_task(
                        async_shuffle_tv(hass, entry, tv_id, reason="calendar_event", recent_images=None),
                        name=f"cal-shuffle-{tv_id}",
                    )
                else:
                    _LOGGER.debug(
                        "Calendar event '%s': TV %s is displaying, deferring shuffle",
                        tagset_name, tv_id,
                    )

            # Schedule end timer
            async def _on_event_end(_now: Any) -> None:
                await _async_clear_calendar_event(uid)

            end_unsub = async_track_point_in_time(hass, _on_event_end, end_dt)
            active_calendar_events[uid] = {
                "tagset": tagset_name,
                "suppress_moods": suppress_moods,
                "end_dt": end_dt,
                "end_unsub": end_unsub,
            }

        async def _async_clear_calendar_event(uid: str) -> None:
            """Clear the tagset override set by a calendar event."""
            event_info = active_calendar_events.pop(uid, None)
            if not event_info:
                return

            # Cancel end timer (may already have fired)
            unsub = event_info.get("end_unsub")
            if unsub:
                try:
                    unsub()
                except Exception:  # pylint: disable=broad-except
                    pass

            tagset_name = event_info["tagset"]
            _LOGGER.info("Calendar monitor: event '%s' ended, clearing override", tagset_name)

            for tv_id in list_tv_configs_fn(entry):
                tv_config = get_tv_config(entry, tv_id) or {}
                # Only clear if this event's tagset is still the active override
                if tv_config.get(CONF_OVERRIDE_TAGSET) == tagset_name:
                    cancel_tagset_override_timer_fn(tv_id)
                    permanent_tagset = tv_config.get(CONF_SELECTED_TAGSET)
                    update_tv_config_fn(hass, entry, tv_id, {
                        CONF_OVERRIDE_TAGSET: None,
                        CONF_OVERRIDE_EXPIRY_TIME: None,
                        CONF_CALENDAR_SUPPRESS_MOODS: False,
                    })
                    async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{entry.entry_id}_{tv_id}")
                    hass.async_create_background_task(
                        _async_invalidate_web_prefetch_for_tv(hass, entry, tv_id, permanent_tagset),
                        name=f"cal-prefetch-clear-{tv_id}",
                    )
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "calendar_event_ended",
                        f"Calendar event: override '{tagset_name}' ended, reverted to selected tagset",
                    )
                    # Mark TV as needing a reshuffle. Cleared by any successful
                    # shuffle; checked in turn_on_tv to retry if TV was in deep sleep.
                    hass.data[DOMAIN][entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                    if not _tv_is_displaying(hass, entry, tv_id):
                        hass.async_create_task(
                            async_shuffle_tv(hass, entry, tv_id, reason="calendar_event_end", recent_images=None),
                            name=f"cal-shuffle-clear-{tv_id}",
                        )
                    else:
                        _LOGGER.debug(
                            "Calendar event '%s' ended: TV %s is displaying, deferring shuffle",
                            tagset_name, tv_id,
                        )

        async def _async_scan_calendar(now: Any = None) -> None:
            """Fetch calendar events and apply/schedule as needed."""
            try:
                now_utc = datetime.now(timezone.utc)
                result = await hass.services.async_call(
                    "calendar",
                    "get_events",
                    {
                        "entity_id": calendar_entity_id,
                        "start_date_time": now_utc.isoformat(),
                        "end_date_time": (now_utc + timedelta(hours=25)).isoformat(),
                    },
                    blocking=True,
                    return_response=True,
                )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Calendar monitor: failed to fetch events: %s", err)
                return

            if not result:
                return

            events = result.get(calendar_entity_id, {}).get("events", [])
            _LOGGER.debug("Calendar monitor: scan found %d event(s) in 25h window", len(events))
            now_dt = datetime.now(timezone.utc)
            seen_uids: set[str] = set()

            for raw in events:
                _LOGGER.debug(
                    "Calendar monitor: event '%s' start=%s end=%s",
                    raw.get("summary"), raw.get("start"), raw.get("end"),
                )
                try:
                    start_dt = _parse_calendar_dt(raw.get("start"))
                    end_dt = _parse_calendar_dt(raw.get("end"))
                except (ValueError, TypeError) as err:
                    _LOGGER.warning("Calendar monitor: could not parse event times: %s", err)
                    continue

                description = raw.get("description")
                flags = _parse_calendar_event_description(description)
                # Prefer manager-assigned uid from description; fall back to
                # HA-native uid (if ever returned), then event summary.
                uid = flags.get("uid") or raw.get("uid") or raw.get("summary") or ""
                seen_uids.add(uid)

                if end_dt <= now_dt:
                    continue  # already ended

                event_data = {
                    "uid": uid,
                    "summary": raw.get("summary") or "",
                    "description": description,
                    "_end_dt": end_dt,
                    "_flags": flags,
                }

                if start_dt <= now_dt:
                    # Currently active
                    existing = active_calendar_events.get(uid)
                    if existing is None:
                        await _async_apply_calendar_event(event_data)
                    else:
                        # Already tracked — check if end time changed (e.g. user edited event)
                        if existing["end_dt"] != end_dt:
                            _LOGGER.info(
                                "Calendar monitor: end time for '%s' changed %s → %s, updating timer",
                                existing["tagset"], existing["end_dt"], end_dt,
                            )
                            old_unsub = existing.get("end_unsub")
                            if old_unsub:
                                try:
                                    old_unsub()
                                except Exception:  # pylint: disable=broad-except
                                    pass

                            async def _on_updated_end(_now: Any, _uid: str = uid) -> None:
                                await _async_clear_calendar_event(_uid)

                            new_unsub = async_track_point_in_time(
                                hass, _on_updated_end, end_dt
                            )
                            existing["end_dt"] = end_dt
                            existing["end_unsub"] = new_unsub
                            for tv_id in list_tv_configs_fn(entry):
                                tv_cfg = get_tv_config(entry, tv_id) or {}
                                if tv_cfg.get(CONF_OVERRIDE_TAGSET) == existing["tagset"]:
                                    update_tv_config_fn(
                                        hass, entry, tv_id,
                                        {CONF_OVERRIDE_EXPIRY_TIME: end_dt.isoformat()},
                                    )
                                    start_tagset_override_timer_fn(tv_id, end_dt)
                        else:
                            # End time unchanged — heal if override is missing from all TVs
                            override_missing = all(
                                (get_tv_config(entry, tv_id) or {}).get(CONF_OVERRIDE_TAGSET)
                                != existing["tagset"]
                                for tv_id in list_tv_configs_fn(entry)
                            )
                            if override_missing:
                                _LOGGER.warning(
                                    "Calendar monitor: override for '%s' missing from TVs, re-applying",
                                    existing["tagset"],
                                )
                                active_calendar_events.pop(uid, None)
                                await _async_apply_calendar_event(event_data)
                else:
                    # Upcoming — schedule start timer
                    if uid not in upcoming_start_unsubs and uid not in active_calendar_events:
                        async def _make_start_callback(ed: dict) -> Any:
                            async def _on_start(_now: Any) -> None:
                                upcoming_start_unsubs.pop(ed["uid"], None)
                                await _async_apply_calendar_event(ed)
                            return _on_start

                        start_unsub = async_track_point_in_time(
                            hass, await _make_start_callback(event_data), start_dt
                        )
                        upcoming_start_unsubs[uid] = start_unsub
                        _LOGGER.debug(
                            "Calendar monitor: scheduled start of '%s' at %s",
                            event_data["summary"], start_dt,
                        )

            # Clear any active events that are no longer in the calendar
            for uid in list(active_calendar_events.keys()):
                if uid not in seen_uids:
                    _LOGGER.info("Calendar monitor: event %s removed from calendar, clearing", uid)
                    await _async_clear_calendar_event(uid)

            # Cancel upcoming timers for removed events
            for uid in list(upcoming_start_unsubs.keys()):
                if uid not in seen_uids:
                    unsub = upcoming_start_unsubs.pop(uid)
                    try:
                        unsub()
                    except Exception:  # pylint: disable=broad-except
                        pass

        async def _on_calendar_state_change(event: Any) -> None:
            """Re-scan when the calendar entity state changes."""
            await _async_scan_calendar()

        async def _on_interval(now: Any) -> None:
            """Safety-net hourly rescan."""
            await _async_scan_calendar()

        # Subscribe to calendar state changes (fires when current/next event shifts)
        unsub_state = async_track_state_change_event(
            hass, [calendar_entity_id], _on_calendar_state_change
        )
        # Hourly safety-net rescan — must use sync callback for async_track_time_interval
        unsub_interval = async_track_time_interval(
            hass, _on_interval, timedelta(hours=1)
        )
        hass.data[DOMAIN][entry.entry_id]["calendar_unsubs"] = [unsub_state, unsub_interval]

        # Defer initial scan until HA is fully started so all services (including
        # calendar.get_events) are registered. On mid-session reload, HA is already
        # running so scan immediately instead of waiting for a never-fired event.
        from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
        from homeassistant.core import CoreState

        async def _on_ha_started(_event: Any) -> None:
            await _async_scan_calendar()

        if hass.state == CoreState.running:
            hass.async_create_task(
                _async_scan_calendar(), name="cal-monitor-initial"
            )
        else:
            hass.data[DOMAIN][entry.entry_id]["calendar_unsubs"].append(
                hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_ha_started)
            )

    def _parse_calendar_dt(value: Any) -> "datetime":
        """Parse a HA calendar event start/end value to an aware datetime."""
        from datetime import date as date_type
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, date_type):
            # All-day event: treat as UTC midnight
            return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt
        raise TypeError(f"Cannot parse calendar datetime: {value!r}")

    def _tv_is_displaying(hass: Any, entry: Any, tv_id: str) -> bool:
        """Return True if the TV screen is currently on (actively displaying art)."""
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        return bool(data.get("tv_status_cache", {}).get(tv_id, {}).get("screen_on", False))

    async def async_setup_entry(hass: Any, entry: Any) -> bool:
        """Set up a config entry for Frame Art Shuffler."""

        metadata_path = Path(entry.data[CONF_METADATA_PATH])

        # Migrate metadata path from metadata.json → gallery.json (one-time)
        if metadata_path.name == "metadata.json":
            gallery_path = metadata_path.parent / "gallery.json"
            if gallery_path.exists():
                new_data = {**entry.data, CONF_METADATA_PATH: str(gallery_path)}
                hass.config_entries.async_update_entry(entry, data=new_data)
                metadata_path = gallery_path

        token_dir = Path(entry.data[CONF_TOKEN_DIR])

        token_dir.mkdir(parents=True, exist_ok=True)
        set_token_directory(token_dir)

        # Migrate TV data from metadata.json to config entry (one-time)
        if "tvs" not in entry.data or not entry.data["tvs"]:
            await _async_migrate_from_metadata(hass, entry, metadata_path)

        # Migrate motion_sensor (singular) to motion_sensors (list) - v1.1.0
        await _async_migrate_motion_sensors(hass, entry)

        # Migrate per-TV tagsets to global tagsets - v1.2.0
        await _async_migrate_tagsets_to_global(hass, entry)

        # Initialize global tagsets if not present (empty dict)
        if "tagsets" not in entry.data:
            data = {**entry.data, "tagsets": {}}
            hass.config_entries.async_update_entry(entry, data=data)

        coordinator = FrameArtCoordinator(hass, entry)
        await coordinator.async_config_entry_first_refresh()

        manager_url = entry.data.get("frame_art_manager_url", "http://localhost:8099")
        image_cache = ImageMetadataCache(hass, manager_url)
        tagset_cache = TagsetCache(hass, manager_url)
        mood_cache = MoodCache(hass, manager_url)
        # Non-fatal if manager unreachable at startup; sensors fall back to entry.data
        await tagset_cache.async_refresh()
        await mood_cache.async_refresh()

        art_clients = {
            tv_id: TVConnectionManager(tv_cfg["ip"])
            for tv_id, tv_cfg in entry.data.get("tvs", {}).items()
            if tv_cfg.get("ip")
        }

        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "coordinator": coordinator,
            "metadata_path": metadata_path,
            "manager_url": manager_url,
            "image_cache": image_cache,
            "tagset_cache": tagset_cache,
            "mood_cache": mood_cache,
            "token_dir": token_dir,
            "config_snapshot": _get_structural_config(entry.data),
            "art_clients": art_clients,
            # Initialize dicts that sensors need to read from
            # These will be populated by the timer code after platforms are set up
            "auto_brightness_next_times": {},
            "motion_off_times": {},
            "shuffle_cache": {},
            "staged_images": {},
            "upload_in_progress": set(),
            "auto_shuffle_next_times": {},
            "tv_model_years": {},
            "recovery_pending": {},    # tv_id → {"pending_since": monotonic, "reason": str}
            "art_channel_recovery": {},  # tv_id → {"cooldown_until": monotonic}
        }

        # Migrate tagset definitions from config entry to manager (one-time).
        # If entry.data["tagsets"] is non-empty, push each to POST /api/tagsets,
        # then clear from config entry. Non-fatal if manager is unreachable.
        existing_tagsets = entry.data.get("tagsets", {})
        if existing_tagsets:
            hass.async_create_task(
                _async_migrate_tagsets_to_manager(hass, entry, manager_url, existing_tagsets)
            )

        async def _fetch_model_years() -> None:
            """Fetch and cache model year for each TV in the background."""
            tv_model_years = hass.data[DOMAIN][entry.entry_id]["tv_model_years"]
            for tv_id, tv_cfg in entry.data.get("tvs", {}).items():
                ip = tv_cfg.get("ip")
                if ip:
                    year = await get_tv_model_year(ip)
                    if year is not None:
                        tv_model_years[tv_id] = year
                        _LOGGER.debug("TV %s model year: %s", tv_id, year)

        hass.async_create_task(_fetch_model_years())

        display_log = DisplayLogManager(hass, entry)
        await display_log.async_setup()
        hass.data[DOMAIN][entry.entry_id]["display_log"] = display_log

        # Remove stale entities that were merged/removed in prior versions
        _async_remove_stale_entities(hass, entry)

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

        # Register device removal handler
        entry.async_on_unload(
            _register_device_removal_listener(hass, entry, metadata_path)
        )

        # --- External artwork change detection ---
        # Polls the TV for the current content_id and updates the Artwork Info sensor
        # when something else (Samsung app, Art Store gallery rotation, etc.) changed
        # what's displayed without going through this integration.
        async def _check_external_artwork_change(tv_id: str, client) -> None:
            """Query TV for current artwork; update sensor only if it changed externally."""
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            artwork_sensor = data.get("artwork_sensors", {}).get(tv_id)
            if not artwork_sensor:
                return

            # Suppress during active uploads — the art_mode_changed event fired by the TV
            # after a successful upload can race with the sensor update in _perform_upload.
            if tv_id in data.get("upload_in_progress", set()):
                _LOGGER.debug(
                    "External artwork check suppressed for %s: upload in progress", tv_id
                )
                return

            # Suppress for 30s after an upload guard clears — handles the window where
            # aiohttp may have cancelled _perform_upload (client disconnect on timeout)
            # but the TV still received and selected the image.
            last_cleared = data.get("last_upload_cleared_at", 0)
            if asyncio.get_event_loop().time() - last_cleared < 30:
                _LOGGER.debug(
                    "External artwork check suppressed for %s: recent upload (%.0fs ago)",
                    tv_id, asyncio.get_event_loop().time() - last_cleared,
                )
                return

            try:
                current = await client.art.get_current()
                content_id = current.get("content_id") if current else None
                if content_id and content_id != artwork_sensor.native_value:
                    _LOGGER.debug(
                        "External artwork change detected on %s: %s → %s",
                        tv_id, artwork_sensor.native_value, content_id,
                    )
                    artwork_sensor.set_external_artwork(content_id)
                    artwork_sensor.async_write_ha_state()
                    log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
                    await hass.async_add_executor_job(
                        _write_artwork_sensor_log, log_path, tv_id, content_id, "external", {}
                    )
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Could not query current artwork for %s: %s", tv_id, err)

        # Register WebSocket event callbacks (art_mode_changed, wakeup) on each client.
        # These fire at natural transition moments; the callback schedules a check as a task.
        for tv_id, client in art_clients.items():
            def _make_callback(tid: str, c) -> Any:
                async def _on_art_event(event: Any, response: Any) -> None:
                    await _check_external_artwork_change(tid, c)
                return _on_art_event
            client.set_artwork_change_callback(_make_callback(tv_id, client))

        # Periodic poll: catch mid-session changes (e.g. user browses gallery on TV).
        _ARTWORK_POLL_INTERVAL = timedelta(minutes=2)

        async def _poll_artwork_changes(_now: Any = None) -> None:
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            for tv_id, client in data.get("art_clients", {}).items():
                await _check_external_artwork_change(tv_id, client)

        entry.async_on_unload(
            async_track_time_interval(hass, _poll_artwork_changes, _ARTWORK_POLL_INTERVAL)
        )

        async def _async_get_image_tags(data: dict, filename: str | None) -> list[str]:
            """Look up image tags from the manager add-on."""
            if not filename:
                return []
            try:
                cache: ImageMetadataCache = data.get("image_cache")
                if cache:
                    image_meta = await cache.get_image(filename)
                    if image_meta:
                        return list(image_meta.get("tags", []))
            except Exception:
                pass
            return []

        async def async_handle_send_image(call: ServiceCall) -> dict[str, Any] | None:
            """Handle the send_image service.

            When select=True (default): upload, select as current artwork,
            clean up old images, update sensors/cache/activity/display log.
            Uses upload guard to prevent concurrent uploads.

            When select=False: upload only (pre-upload pipeline).
            No guard, no state updates. Returns {content_id}.

            Always returns {content_id} when called with return_response.
            """
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            data = hass.data[DOMAIN][target_entry.entry_id]

            select = call.data.get("select", True)
            image_path = call.data.get("image_path")
            matte = call.data.get("matte")

            ip = tv_data["ip"]
            mac = tv_data.get("mac")
            tv_name = tv_data.get("name", tv_id)

            client = data.get("art_clients", {}).get(tv_id)
            if client is None:
                raise ValueError(f"No art client found for TV {tv_id}")

            if select:
                # --- select=True: full upload + select + cleanup ---
                image_url = call.data.get("image_url")
                filename = call.data.get("filename")
                filter_id = call.data.get("filter")
                screen_on = call.data.get("screen_on", True)
                artwork_metadata = call.data.get("artwork_metadata")

                # Resolve image path
                final_path = None
                if image_path:
                    final_path = image_path
                elif image_url:
                    if image_url.startswith("/local/"):
                        final_path = hass.config.path("www", image_url[7:])
                    else:
                        raise ValueError("image_url must start with /local/")
                elif filename:
                    metadata_path = data["metadata_path"]
                    final_path = str(metadata_path.parent / "library" / filename)

                if not final_path:
                    raise ValueError("Must provide image_path, image_url, or filename")

                display_filename = filename if filename else Path(final_path).name

                result_content_id: str | None = None

                async def _perform_upload() -> bool:
                    nonlocal result_content_id
                    from datetime import datetime, timezone as dt_timezone

                    # Invalidate staged image — gallery state is changing externally
                    staged = data.get("staged_images", {})
                    if tv_id in staged:
                        _LOGGER.debug("send_image: invalidating staged image for %s", tv_id)
                        del staged[tv_id]

                    log_activity(
                        hass,
                        target_entry.entry_id,
                        tv_id,
                        "select_image",
                        f"Selecting custom image ({display_filename}) via service call",
                    )

                    content_id = await frame_tv.set_art_on_tv_deleteothers(
                        client,
                        final_path,
                        mac_address=mac,
                        matte=matte,
                        photo_filter=filter_id,
                        delete_others=True,
                        screen_on=screen_on,
                    )
                    result_content_id = content_id

                    # Update shuffle_cache
                    shuffle_cache = data.setdefault("shuffle_cache", {}).setdefault(tv_id, {})
                    shuffle_cache["current_image"] = display_filename
                    shuffle_cache["current_matte"] = matte
                    shuffle_cache["current_filter"] = filter_id
                    shuffle_cache["matching_image_count"] = 0  # Not a shuffle

                    # Update artwork info sensor
                    artwork_sensor = data.get("artwork_sensors", {}).get(tv_id)
                    if artwork_sensor and content_id:
                        if artwork_metadata is not None:
                            _LOGGER.debug(
                                "Artwork sensor update [%s] content_id=%s source=web_source title=%r creator=%r keys=%s",
                                tv_name, content_id[:8],
                                artwork_metadata.get("title", ""),
                                artwork_metadata.get("creator_name", artwork_metadata.get("creator", "")),
                                [k for k, v in artwork_metadata.items() if v not in (None, "", [])],
                            )
                            artwork_sensor.set_artwork(content_id, artwork_metadata, source_type="web_source")
                            log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
                            await hass.async_add_executor_job(
                                _write_artwork_sensor_log, log_path, tv_name, content_id, "web_source", artwork_metadata
                            )
                        else:
                            from .shuffle import _build_local_sensor_meta
                            if filename:
                                tags = await _async_get_image_tags(data, filename)
                                meta = await _build_local_sensor_meta(data, filename, tags)
                            else:
                                meta = {"filename": display_filename}
                            _LOGGER.debug(
                                "Artwork sensor update [%s] content_id=%s source=local filename=%r",
                                tv_name, content_id[:8], display_filename,
                            )
                            artwork_sensor.set_artwork(content_id, meta, source_type="local")
                            log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
                            await hass.async_add_executor_job(
                                _write_artwork_sensor_log, log_path, tv_name, content_id, "local", meta
                            )
                        artwork_sensor.async_write_ha_state()

                    # Send signal to update sensors
                    signal = f"{DOMAIN}_shuffle_{target_entry.entry_id}_{tv_id}"
                    async_dispatcher_send(hass, signal)

                    if filename:
                        coordinator = data["coordinator"]
                        await coordinator.async_set_active_image(
                            tv_id, filename, is_shuffle=False
                        )

                    # Update display log
                    display_log = data.get("display_log")
                    if display_log and filename:
                        image_tags = await _async_get_image_tags(data, filename)
                        display_log.note_display_start(
                            tv_id=tv_id,
                            tv_name=tv_name,
                            filename=filename,
                            tags=image_tags,
                            source="manual",
                            shuffle_mode=None,
                            started_at=datetime.now(dt_timezone.utc),
                            matte=matte,
                        )

                    return True

                def _on_skip() -> None:
                    _LOGGER.info(
                        "send_image skipped for %s (%s): upload already running",
                        tv_name,
                        tv_id,
                    )
                    raise ServiceValidationError(
                        f"Upload skipped for {tv_name}: another upload is already in progress. Please wait a moment and try again."
                    )

                await async_guarded_upload(
                    hass,
                    target_entry,
                    tv_id,
                    "send_image",
                    _perform_upload,
                    _on_skip,
                )

                return {"content_id": result_content_id}

            else:
                # --- select=False: upload only (pre-upload pipeline) ---
                if not image_path:
                    raise ValueError("Must provide image_path")

                # No upload guard — pre-upload runs in background and must not
                # block user-initiated select=True calls which DO use the guard.
                # However, concurrent TV WebSocket usage (select=False alongside
                # select=True) causes interleaved responses and failures, so
                # abort fast if another upload is already running.
                data = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {})
                if tv_id in data.get("upload_in_progress", set()):
                    raise HomeAssistantError(
                        f"Pre-upload skipped for {tv_name}: another upload is in progress"
                    )
                try:
                    content_id = await frame_tv.upload_to_tv_only(
                        client,
                        image_path,
                        mac_address=mac,
                        matte=matte,
                    )
                except frame_tv.FrameArtError as err:
                    raise HomeAssistantError(str(err)) from err

                return {"content_id": content_id}

        hass.services.async_register(
            DOMAIN,
            "send_image",
            async_handle_send_image,
            supports_response=SupportsResponse.OPTIONAL,
        )

        log_options_schema = vol.Schema(
            {
                vol.Optional(CONF_LOGGING_ENABLED): bool,
                vol.Optional(CONF_LOG_RETENTION_MONTHS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=12)
                ),
                vol.Optional(CONF_LOG_FLUSH_MINUTES): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
            }
        )

        async def async_handle_set_log_options(call: ServiceCall) -> None:
            """Update logging runtime settings without requiring a reload."""

            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                raise ValueError("Integration data not available for logging update")

            updated_options = dict(entry.options or {})
            changed = False

            if CONF_LOGGING_ENABLED in call.data:
                value = bool(call.data[CONF_LOGGING_ENABLED])
                if updated_options.get(CONF_LOGGING_ENABLED, DEFAULT_LOGGING_ENABLED) != value:
                    updated_options[CONF_LOGGING_ENABLED] = value
                    changed = True

            if CONF_LOG_RETENTION_MONTHS in call.data:
                value = int(call.data[CONF_LOG_RETENTION_MONTHS])
                if updated_options.get(
                    CONF_LOG_RETENTION_MONTHS,
                    DEFAULT_LOG_RETENTION_MONTHS,
                ) != value:
                    updated_options[CONF_LOG_RETENTION_MONTHS] = value
                    changed = True

            if CONF_LOG_FLUSH_MINUTES in call.data:
                value = int(call.data[CONF_LOG_FLUSH_MINUTES])
                if updated_options.get(
                    CONF_LOG_FLUSH_MINUTES,
                    DEFAULT_LOG_FLUSH_MINUTES,
                ) != value:
                    updated_options[CONF_LOG_FLUSH_MINUTES] = value
                    changed = True

            if not changed:
                return

            hass.config_entries.async_update_entry(
                entry,
                options=updated_options,
            )

        hass.services.async_register(
            DOMAIN,
            "set_log_options",
            async_handle_set_log_options,
            schema=log_options_schema,
        )

        async def async_handle_flush_display_log(call: ServiceCall) -> None:
            """Manually flush pending display log sessions to disk."""
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                _LOGGER.warning("Integration data not available for flush")
                return

            display_log = data.get("display_log")
            if not display_log:
                _LOGGER.warning("Display log manager not initialized")
                return

            await display_log.async_flush(force=True)
            _LOGGER.info("Display log flushed successfully")

        hass.services.async_register(
            DOMAIN,
            "flush_display_log",
            async_handle_flush_display_log,
        )

        async def async_handle_clear_display_log(call: ServiceCall) -> None:
            """Clear all display log data."""
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                _LOGGER.warning("Integration data not available for clear")
                return

            display_log = data.get("display_log")
            if not display_log:
                _LOGGER.warning("Display log manager not initialized")
                return

            await display_log.async_clear_logs()
            _LOGGER.info("Display logs cleared successfully")

        hass.services.async_register(
            DOMAIN,
            "clear_display_log",
            async_handle_clear_display_log,
        )

        # --- TV Power Control Services ---
        async def _resolve_tv_from_call(call: ServiceCall) -> tuple[Any, str, dict[str, Any]]:
            """Resolve device_id/entity_id to config entry, tv_id, and tv_data."""
            device_id = call.data.get("device_id")
            entity_id = call.data.get("entity_id")

            if entity_id and not device_id:
                ent_reg = er.async_get(hass)
                entity_entry = ent_reg.async_get(entity_id)
                if entity_entry:
                    device_id = entity_entry.device_id

            if not device_id:
                raise ServiceValidationError("Must provide device_id or entity_id")

            device_registry = dr.async_get(hass)
            device = device_registry.async_get(device_id)
            if not device:
                raise ServiceValidationError(f"Device {device_id} not found")

            target_entry: Any | None = None
            for eid in device.config_entries:
                config_entry = hass.config_entries.async_get_entry(eid)
                if config_entry and config_entry.domain == DOMAIN:
                    target_entry = config_entry
                    break

            if not target_entry:
                raise ServiceValidationError(f"No Frame Art Shuffler config entry found for device {device_id}")

            data = hass.data.get(DOMAIN, {}).get(target_entry.entry_id)
            if not data:
                raise ServiceValidationError(
                    "Integration is still initializing — please retry in a moment"
                )

            tv_id = None
            for identifier in device.identifiers:
                if identifier[0] == DOMAIN:
                    tv_id = identifier[1]
                    break

            if not tv_id:
                raise ServiceValidationError(f"Could not determine TV ID from device {device_id}")

            coordinator = data["coordinator"]
            tv_data = next((tv for tv in coordinator.data if tv["id"] == tv_id), None)
            if not tv_data:
                raise ServiceValidationError(f"TV {tv_id} not found in coordinator data")

            return target_entry, tv_id, tv_data

        async def async_handle_turn_on_tv(call: ServiceCall) -> None:
            """Handle the turn_on_tv service."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            reason = call.data.get("reason")

            ip = tv_data["ip"]
            mac = tv_data.get("mac")
            tv_name = tv_data.get("name", tv_id)

            if not mac:
                raise ValueError(f"Cannot turn on {tv_name}: missing MAC address")

            data = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {})
            client = data.get("art_clients", {}).get(tv_id)

            try:
                await tv_on(ip, mac, client=client)
                _LOGGER.info(f"Sent Wake-on-LAN to {tv_name}")

                await set_art_mode(client)
                _LOGGER.info(f"Switched {tv_name} to art mode")

                # Update status cache
                data = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {})
                status_cache = data.get("tv_status_cache", {})
                if tv_id in status_cache:
                    status_cache[tv_id]["screen_on"] = True

                # Log activity with optional reason
                if reason:
                    message = f"Screen turned on ({reason})"
                else:
                    message = "Screen turned on (turn_on_tv service)"
                log_activity(hass, target_entry.entry_id, tv_id, "screen_on", message)

                # Start display log session for the current image
                display_log = data.get("display_log")
                if display_log:
                    # Get current image from shuffle_cache or config
                    shuffle_cache = data.get("shuffle_cache", {}).get(tv_id, {})
                    current_image = shuffle_cache.get("current_image")
                    if not current_image:
                        current_image = tv_config.get("current_image") if tv_config else None
                    
                    if current_image:
                        image_tags = await _async_get_image_tags(data, current_image)
                        
                        # Get TV's configured tags for matched_tags computation
                        tv_tags = tv_config.get("include_tags") if tv_config else None
                        
                        display_log.note_screen_on(
                            tv_id=tv_id,
                            tv_name=tv_name,
                            filename=current_image,
                            tags=image_tags,
                            tv_tags=tv_tags,
                        )

                # If a tagset change happened while TV was in deep sleep (e.g., calendar
                # event ended at 2am), shuffle now that the TV is accessible.
                pending = data.get("pending_reshuffles", set())
                if tv_id in pending:
                    pending.discard(tv_id)
                    _LOGGER.info("turn_on_tv: triggering pending reshuffle for %s", tv_name)
                    hass.async_create_task(
                        async_shuffle_tv(hass, target_entry, tv_id, reason="turn_on_reshuffle", recent_images=None),
                        name=f"turn-on-reshuffle-{tv_id}",
                    )

                # Start motion off timer if enabled
                tv_config = get_tv_config(target_entry, tv_id)
                if tv_config and tv_config.get("enable_motion_control", False):
                    start_motion_off_timer = data.get("start_motion_off_timer")
                    if start_motion_off_timer:
                        start_motion_off_timer(tv_id)

            except Exception as err:
                _LOGGER.error(f"Failed to turn on {tv_name}: {err}")
                raise

        async def async_handle_turn_off_tv(call: ServiceCall) -> None:
            """Handle the turn_off_tv service."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            reason = call.data.get("reason")

            ip = tv_data["ip"]
            tv_name = tv_data.get("name", tv_id)

            try:
                await tv_off(ip)
                _LOGGER.info(f"Turned off {tv_name} screen")

                # Update status cache
                data = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {})
                status_cache = data.get("tv_status_cache", {})
                if tv_id in status_cache:
                    status_cache[tv_id]["screen_on"] = False

                # Log activity with optional reason
                if reason:
                    message = f"Screen turned off ({reason})"
                else:
                    message = "Screen turned off (turn_off_tv service)"
                log_activity(hass, target_entry.entry_id, tv_id, "screen_off", message)

                # Close display log session since screen is turning off
                display_log = data.get("display_log")
                if display_log:
                    display_log.note_screen_off(tv_id=tv_id, tv_name=tv_name)

                # Cancel motion off timer if enabled
                tv_config = get_tv_config(target_entry, tv_id)
                if tv_config and tv_config.get("enable_motion_control", False):
                    cancel_motion_off_timer = data.get("cancel_motion_off_timer")
                    if cancel_motion_off_timer:
                        cancel_motion_off_timer(tv_id)

            except Exception as err:
                _LOGGER.error(f"Failed to turn off {tv_name}: {err}")
                raise

        hass.services.async_register(
            DOMAIN,
            "turn_on_tv",
            async_handle_turn_on_tv,
        )

        hass.services.async_register(
            DOMAIN,
            "turn_off_tv",
            async_handle_turn_off_tv,
        )

        # ===== TAGSET SERVICES =====
        # Timer management for tagset override expiry
        tagset_override_timers: dict[str, Callable[[], None]] = {}

        def cancel_tagset_override_timer(tv_id: str) -> None:
            """Cancel the tagset override expiry timer for a TV."""
            if tv_id in tagset_override_timers:
                tagset_override_timers[tv_id]()
                del tagset_override_timers[tv_id]

        def start_tagset_override_timer(tv_id: str, expiry_time: datetime) -> None:
            """Start the tagset override expiry timer for a TV."""
            from homeassistant.helpers.event import async_track_point_in_time
            
            cancel_tagset_override_timer(tv_id)

            async def async_override_expiry_callback(_now: Any) -> None:
                """Timer callback to clear tagset override."""
                tv_config = get_tv_config(entry, tv_id)
                if not tv_config:
                    return

                tv_name = tv_config.get("name", tv_id)
                override_name = tv_config.get(CONF_OVERRIDE_TAGSET)
                
                # Clear the override
                update_tv_config(hass, entry, tv_id, {
                    CONF_OVERRIDE_TAGSET: None,
                    CONF_OVERRIDE_EXPIRY_TIME: None,
                })
                
                _LOGGER.info(f"Tagset override '{override_name}' expired for {tv_name}")
                log_activity(
                    hass, entry.entry_id, tv_id,
                    "tagset_override_expired",
                    f"Override '{override_name}' expired, reverted to selected tagset",
                )
                
                # Signal sensors to update
                async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{entry.entry_id}_{tv_id}")

                # Clean up timer reference
                if tv_id in tagset_override_timers:
                    del tagset_override_timers[tv_id]

                # Shuffle to reverted tagset if TV is not actively displaying
                if not _tv_is_displaying(hass, entry, tv_id):
                    permanent_tagset = tv_config.get(CONF_SELECTED_TAGSET)
                    hass.async_create_background_task(
                        _async_invalidate_web_prefetch_for_tv(hass, entry, tv_id, permanent_tagset),
                        name=f"prefetch-after-expiry-{tv_id}",
                    )
                    hass.data[DOMAIN][entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                    hass.async_create_task(
                        async_shuffle_tv(hass, entry, tv_id, reason="expiry", recent_images=None),
                        name=f"shuffle-after-expiry-{tv_id}",
                    )

            unsubscribe = async_track_point_in_time(
                hass,
                async_override_expiry_callback,
                expiry_time,
            )
            tagset_override_timers[tv_id] = unsubscribe
            _LOGGER.debug(f"Tagset override timer set for {tv_id} at {expiry_time}")

        # Store timer functions for access elsewhere
        hass.data[DOMAIN][entry.entry_id]["cancel_tagset_override_timer"] = cancel_tagset_override_timer
        hass.data[DOMAIN][entry.entry_id]["start_tagset_override_timer"] = start_tagset_override_timer

        # Restore any pending override timers on startup
        for tv_id, tv_config in list_tv_configs(entry).items():
            expiry_str = tv_config.get(CONF_OVERRIDE_EXPIRY_TIME)
            if expiry_str:
                try:
                    expiry_time = datetime.fromisoformat(expiry_str)
                    if expiry_time > datetime.now(timezone.utc):
                        start_tagset_override_timer(tv_id, expiry_time)
                        _LOGGER.info(f"Restored tagset override timer for {tv_config.get('name', tv_id)}")
                    else:
                        # Expired while HA was down - clear it
                        update_tv_config(hass, entry, tv_id, {
                            CONF_OVERRIDE_TAGSET: None,
                            CONF_OVERRIDE_EXPIRY_TIME: None,
                            CONF_CALENDAR_SUPPRESS_MOODS: False,
                        })
                        _LOGGER.info(f"Cleared expired tagset override for {tv_config.get('name', tv_id)}")
                except (ValueError, TypeError) as e:
                    _LOGGER.warning(f"Invalid override expiry time for {tv_id}: {e}")

        # --- Calendar event monitor ---
        # Watches a configured HA calendar entity and applies tagset overrides
        # for the duration of each event. Event title must match a tagset name.
        # Optional description flags (one per line): suppress_moods: true
        await _async_setup_calendar_monitor(hass, entry, list_tv_configs, update_tv_config,
                                            start_tagset_override_timer, cancel_tagset_override_timer)

        # --- Staged image invalidation on tagset changes ---
        # When a tagset definition or TV assignment changes, clear staged images
        # whose fingerprint no longer matches and kick off a new pre-upload.
        from .shuffle import _async_pre_upload_next  # noqa: E402 - late import avoids circular

        @callback
        def _invalidate_staged_for_all_tvs(*_args: Any) -> None:
            """Global tagset changed — check all TVs."""
            staged = hass.data[DOMAIN][entry.entry_id].get("staged_images", {})
            entry_data = hass.data[DOMAIN][entry.entry_id]
            for tid in list(staged.keys()):
                from .config_entry import get_tagset_fingerprint  # noqa: E402
                current_fp = get_tagset_fingerprint(entry, tid)
                if staged[tid].get("tagset_fingerprint") != current_fp:
                    _LOGGER.debug("Tagset changed: invalidating staged image for %s", tid)
                    del staged[tid]
                    hass.async_create_task(
                        _async_pre_upload_next(hass, entry, tid, entry_data),
                        f"pre-upload-restage-{tid}",
                    )

        def _make_tv_invalidator(tid: str) -> Callable[..., None]:
            """Per-TV tagset changed — check specific TV."""
            @callback
            def _invalidate(*_args: Any) -> None:
                staged = hass.data[DOMAIN][entry.entry_id].get("staged_images", {})
                entry_data = hass.data[DOMAIN][entry.entry_id]
                if tid in staged:
                    from .config_entry import get_tagset_fingerprint  # noqa: E402
                    current_fp = get_tagset_fingerprint(entry, tid)
                    if staged[tid].get("tagset_fingerprint") != current_fp:
                        _LOGGER.debug("Tagset changed: invalidating staged image for %s", tid)
                        del staged[tid]
                        hass.async_create_task(
                            _async_pre_upload_next(hass, entry, tid, entry_data),
                            f"pre-upload-restage-{tid}",
                        )
            return _invalidate

        # Listen for global tagset definition changes (upsert/delete)
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_tagset_updated_{entry.entry_id}",
            _invalidate_staged_for_all_tvs,
        )

        # Listen for per-TV tagset assignment changes (select/override/clear)
        for tv_id in list_tv_configs(entry):
            async_dispatcher_connect(
                hass,
                f"{DOMAIN}_tagset_updated_{entry.entry_id}_{tv_id}",
                _make_tv_invalidator(tv_id),
            )

        # Bind the module-level helper to this entry's context.
        async def _async_invalidate_and_prefetch_for_tv(tv_id: str, new_tagset_name: str | None) -> None:
            await _async_invalidate_web_prefetch_for_tv(hass, entry, tv_id, new_tagset_name)

        async def async_handle_select_tagset(call: ServiceCall) -> None:
            """Permanently switch which tagset a TV uses.

            Requires device_id - this is a per-TV assignment.
            """
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            
            name = call.data.get("name", "").strip()
            if not name:
                raise ServiceValidationError("Tagset name is required")
            
            tv_name = tv_data.get("name", tv_id)

            # Validate tagset exists in cache (manager owns definitions).
            # If the cache is empty (manager was unreachable at startup), try a fresh fetch.
            tagset_cache = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {}).get("tagset_cache")
            tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
            if tagsets is None:
                tagsets = get_global_tagsets(target_entry)
            if name not in (tagsets or {}):
                # Not in cache — refresh once in case it was added since last load.
                if tagset_cache:
                    await tagset_cache.async_refresh()
                    tagsets = tagset_cache.get_all() or tagsets
            if name not in (tagsets or {}):
                raise ServiceValidationError(f"Tagset '{name}' not found")

            update_tv_config(hass, target_entry, tv_id, {CONF_SELECTED_TAGSET: name})
            
            _LOGGER.info(f"Selected tagset '{name}' for {tv_name}")
            log_activity(
                hass, target_entry.entry_id, tv_id,
                "tagset_selected",
                f"Selected tagset '{name}'",
            )
            if tagset_cache:
                await tagset_cache.async_refresh()
            async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{target_entry.entry_id}_{tv_id}")
            hass.async_create_background_task(
                _async_invalidate_and_prefetch_for_tv(tv_id, name),
                name=f"prefetch-after-tagset-{tv_id}",
            )
            if not _tv_is_displaying(hass, target_entry, tv_id):
                hass.data[DOMAIN][target_entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                hass.async_create_task(
                    async_shuffle_tv(hass, target_entry, tv_id, reason="tagset_select", recent_images=None),
                    name=f"shuffle-after-select-{tv_id}",
                )
            else:
                _LOGGER.debug("Select tagset '%s' for %s: TV is displaying, deferring shuffle", name, tv_name)

        async def async_handle_override_tagset(call: ServiceCall) -> None:
            """Apply a temporary tagset override with required expiry.
            
            Requires device_id - this is a per-TV operation.
            """
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            
            name = call.data.get("name", "").strip()
            duration_minutes = call.data.get("duration_minutes")
            
            if not name:
                raise ServiceValidationError("Tagset name is required")
            if not duration_minutes or duration_minutes <= 0:
                raise ServiceValidationError("duration_minutes is required and must be > 0")
            
            tv_name = tv_data.get("name", tv_id)

            # Validate tagset exists in cache (manager owns definitions).
            # If the name isn't found, refresh once in case it was added since last load.
            tagset_cache = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {}).get("tagset_cache")
            tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
            if tagsets is None:
                tagsets = get_global_tagsets(target_entry)
            if name not in (tagsets or {}):
                if tagset_cache:
                    await tagset_cache.async_refresh()
                    tagsets = tagset_cache.get_all() or tagsets
            if name not in (tagsets or {}):
                raise ServiceValidationError(f"Tagset '{name}' not found")

            expiry_time = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
            
            update_tv_config(hass, target_entry, tv_id, {
                CONF_OVERRIDE_TAGSET: name,
                CONF_OVERRIDE_EXPIRY_TIME: expiry_time.isoformat(),
            })
            
            start_tagset_override_timer(tv_id, expiry_time)
            
            _LOGGER.info(f"Tagset override '{name}' applied for {tv_name} ({duration_minutes}m)")
            log_activity(
                hass, target_entry.entry_id, tv_id,
                "tagset_override_applied",
                f"Override '{name}' applied for {duration_minutes}m",
            )
            async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{target_entry.entry_id}_{tv_id}")
            hass.async_create_background_task(
                _async_invalidate_and_prefetch_for_tv(tv_id, name),
                name=f"prefetch-after-override-{tv_id}",
            )

            force_shuffle = call.data.get("force_shuffle", False)
            if force_shuffle or not _tv_is_displaying(hass, target_entry, tv_id):
                hass.data[DOMAIN][target_entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                await async_shuffle_tv(hass, target_entry, tv_id, reason="override", recent_images=None)
            else:
                _LOGGER.debug("Override tagset '%s' for %s: TV is displaying, deferring shuffle", name, tv_name)

        async def async_handle_clear_tagset_override(call: ServiceCall) -> None:
            """Clear an active tagset override early."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            
            tv_config = get_tv_config(target_entry, tv_id)
            tv_name = tv_data.get("name", tv_id)
            override_name = tv_config.get(CONF_OVERRIDE_TAGSET) if tv_config else None
            
            cancel_tagset_override_timer(tv_id)
            
            update_tv_config(hass, target_entry, tv_id, {
                CONF_OVERRIDE_TAGSET: None,
                CONF_OVERRIDE_EXPIRY_TIME: None,
            })
            
            if override_name:
                _LOGGER.info(f"Tagset override '{override_name}' cleared for {tv_name}")
                log_activity(
                    hass, target_entry.entry_id, tv_id,
                    "tagset_override_cleared",
                    f"Override '{override_name}' cleared",
                )
            async_dispatcher_send(hass, f"{DOMAIN}_tagset_updated_{target_entry.entry_id}_{tv_id}")
            # After clearing the override, fall back to the permanent tagset.
            permanent_tagset = get_tv_config(target_entry, tv_id).get(CONF_SELECTED_TAGSET) if get_tv_config(target_entry, tv_id) else None
            hass.async_create_background_task(
                _async_invalidate_and_prefetch_for_tv(tv_id, permanent_tagset),
                name=f"prefetch-after-override-clear-{tv_id}",
            )
            if not _tv_is_displaying(hass, target_entry, tv_id):
                hass.data[DOMAIN][target_entry.entry_id].setdefault("pending_reshuffles", set()).add(tv_id)
                hass.async_create_task(
                    async_shuffle_tv(hass, target_entry, tv_id, reason="override_clear", recent_images=None),
                    name=f"shuffle-after-override-clear-{tv_id}",
                )

        # Register tagset assignment services
        hass.services.async_register(
            DOMAIN,
            "select_tagset",
            async_handle_select_tagset,
        )

        hass.services.async_register(
            DOMAIN,
            "override_tagset",
            async_handle_override_tagset,
        )

        hass.services.async_register(
            DOMAIN,
            "clear_tagset_override",
            async_handle_clear_tagset_override,
        )

        # ── Mood services ──────────────────────────────────────────────────────

        def _get_active_moods_from_sensor(hass: HomeAssistant, sensor_entity_id: str) -> list[str]:
            """Read active mood IDs from a HA sensor entity.

            Supports two formats:
              1. Comma-separated string state: "night,snow"
              2. State attribute 'moods' containing a list: ["night", "snow"]
            """
            if not sensor_entity_id:
                return []
            state = hass.states.get(sensor_entity_id)
            if state is None or state.state in ("unavailable", "unknown", ""):
                return []
            # Format 2: attribute list
            mood_attr = state.attributes.get("moods")
            if isinstance(mood_attr, list):
                return [str(m).strip() for m in mood_attr if m]
            # Format 1: comma-separated string
            return [m.strip() for m in state.state.split(",") if m.strip()]

        def _resolve_active_moods(tv_config: dict, hass: HomeAssistant) -> list[str]:
            """Merge sensor-derived moods and service-call overrides into the active mood list."""
            mood_sensor = tv_config.get(CONF_MOOD_SENSOR, "")
            sensor_moods = _get_active_moods_from_sensor(hass, mood_sensor)

            override_moods: list[str] = tv_config.get(CONF_MOOD_OVERRIDES) or []
            expiry_str = tv_config.get(CONF_MOOD_OVERRIDE_EXPIRY)
            if expiry_str:
                try:
                    expiry = datetime.fromisoformat(expiry_str)
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > expiry:
                        override_moods = []
                except Exception:
                    override_moods = []

            # Union: sensor moods + service overrides (deduplicated, order preserved)
            seen: set[str] = set()
            result: list[str] = []
            for m in sensor_moods + override_moods:
                if m not in seen:
                    seen.add(m)
                    result.append(m)
            return result

        async def async_handle_set_mood_sensor(call: ServiceCall) -> None:
            """Bind a TV to a HA sensor entity that provides active mood IDs."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            sensor = call.data.get("sensor", "").strip()
            tv_name = tv_data.get("name", tv_id)

            update_tv_config(hass, target_entry, tv_id, {CONF_MOOD_SENSOR: sensor or None})
            _LOGGER.info("Set mood sensor to '%s' for %s", sensor or "(none)", tv_name)

        async def async_handle_activate_mood(call: ServiceCall) -> None:
            """Manually activate one or more moods on a TV with optional expiry."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            mood_ids = call.data.get("moods", [])
            expiry_str = call.data.get("expiry", "")
            tv_name = tv_data.get("name", tv_id)

            if not mood_ids:
                raise ServiceValidationError("At least one mood ID is required")

            mood_cache = hass.data.get(DOMAIN, {}).get(target_entry.entry_id, {}).get("mood_cache")
            moods = (mood_cache.get_all() or {}) if mood_cache else {}
            if mood_cache and not moods:
                await mood_cache.async_refresh()
                moods = mood_cache.get_all() or {}
            for mid in mood_ids:
                if moods and mid not in moods:
                    raise ServiceValidationError(f"Mood '{mid}' not found")

            tv_config = get_tv_config(target_entry, tv_id) or {}
            current_overrides: list[str] = list(tv_config.get(CONF_MOOD_OVERRIDES) or [])
            for mid in mood_ids:
                if mid not in current_overrides:
                    current_overrides.append(mid)

            update_data: dict = {CONF_MOOD_OVERRIDES: current_overrides}
            if expiry_str:
                update_data[CONF_MOOD_OVERRIDE_EXPIRY] = expiry_str
            else:
                update_data[CONF_MOOD_OVERRIDE_EXPIRY] = None

            update_tv_config(hass, target_entry, tv_id, update_data)
            _LOGGER.info("Activated moods %s for %s", mood_ids, tv_name)
            log_activity(
                hass, target_entry.entry_id, tv_id,
                "mood_activated",
                f"Activated moods: {', '.join(mood_ids)}",
            )

        async def async_handle_deactivate_mood(call: ServiceCall) -> None:
            """Remove service-call mood overrides from a TV."""
            target_entry, tv_id, tv_data = await _resolve_tv_from_call(call)
            mood_ids: list[str] = call.data.get("moods", [])
            tv_name = tv_data.get("name", tv_id)

            tv_config = get_tv_config(target_entry, tv_id) or {}
            current_overrides: list[str] = list(tv_config.get(CONF_MOOD_OVERRIDES) or [])

            if mood_ids:
                new_overrides = [m for m in current_overrides if m not in mood_ids]
            else:
                new_overrides = []

            update_tv_config(hass, target_entry, tv_id, {
                CONF_MOOD_OVERRIDES: new_overrides or None,
                CONF_MOOD_OVERRIDE_EXPIRY: None,
            })
            _LOGGER.info("Deactivated mood overrides for %s", tv_name)

        # Register mood services
        hass.services.async_register(
            DOMAIN,
            "set_mood_sensor",
            async_handle_set_mood_sensor,
        )
        hass.services.async_register(
            DOMAIN,
            "activate_mood",
            async_handle_activate_mood,
        )
        hass.services.async_register(
            DOMAIN,
            "deactivate_mood",
            async_handle_deactivate_mood,
        )

        # Service to set recency windows
        async def async_handle_set_recency_windows(call: Any) -> None:
            """Handle set_recency_windows service call."""
            same_tv_hours = call.data.get("same_tv_hours")
            cross_tv_hours = call.data.get("cross_tv_hours")

            # Build new options dict
            new_options = dict(entry.options)

            if same_tv_hours is not None:
                same_tv_hours = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, int(same_tv_hours)))
                new_options["same_tv_hours"] = same_tv_hours

            if cross_tv_hours is not None:
                cross_tv_hours = max(MIN_RECENCY_HOURS, min(MAX_RECENCY_HOURS, int(cross_tv_hours)))
                new_options["cross_tv_hours"] = cross_tv_hours

            # Update config entry options
            hass.config_entries.async_update_entry(entry, options=new_options)
            _LOGGER.info(
                "Recency windows updated: same_tv=%sh, cross_tv=%sh",
                new_options.get("same_tv_hours", DEFAULT_SAME_TV_HOURS),
                new_options.get("cross_tv_hours", DEFAULT_CROSS_TV_HOURS),
            )

        hass.services.async_register(
            DOMAIN,
            "set_recency_windows",
            async_handle_set_recency_windows,
        )

        # Per-TV auto brightness timer management
        auto_brightness_timers: dict[str, Callable[[], None]] = {}
        # Use the dict already initialized in hass.data so sensors can access it
        auto_brightness_next_times = hass.data[DOMAIN][entry.entry_id]["auto_brightness_next_times"]

        def cancel_tv_timer(tv_id: str) -> None:
            """Cancel the auto brightness timer for a specific TV."""
            if tv_id in auto_brightness_timers:
                auto_brightness_timers[tv_id]()
                del auto_brightness_timers[tv_id]
            if tv_id in auto_brightness_next_times:
                del auto_brightness_next_times[tv_id]

        def start_tv_timer(tv_id: str) -> None:
            """Start or restart the auto brightness timer for a specific TV."""
            # Cancel existing timer if any
            cancel_tv_timer(tv_id)

            async def async_tv_brightness_tick(_now: Any) -> None:
                """Timer callback for a single TV's auto brightness."""
                tv_configs = list_tv_configs(entry)
                tv_config = tv_configs.get(tv_id)
                
                # If TV no longer exists or auto brightness disabled, cancel timer
                if not tv_config or not tv_config.get("enable_dynamic_brightness", False):
                    cancel_tv_timer(tv_id)
                    return
                
                # Update next scheduled time before running
                auto_brightness_next_times[tv_id] = datetime.now(timezone.utc) + timedelta(minutes=10)
                
                await async_adjust_tv_brightness(tv_id)

            # Schedule timer for this TV
            unsubscribe = async_track_time_interval(
                hass,
                async_tv_brightness_tick,
                timedelta(minutes=10),
            )
            auto_brightness_timers[tv_id] = unsubscribe
            entry.async_on_unload(unsubscribe)

            # Store the next scheduled time so the sensor can show it accurately
            # async_track_time_interval fires 10 minutes from now
            next_time = datetime.now(timezone.utc) + timedelta(minutes=10)
            auto_brightness_next_times[tv_id] = next_time
            _LOGGER.debug(f"Auto brightness: Next adjust for {tv_id} scheduled at {next_time}")

            # Trigger immediate adjustment so we don't wait 10 minutes
            hass.async_create_task(async_adjust_tv_brightness(tv_id))

        # ===== BRIGHTNESS SETTING WITH RETRY =====
        async def async_set_brightness_with_retry(
            tv_id: str,
            brightness: int,
            *,
            reason: str = "manual",
            max_attempts: int = 2,
            retry_delay_seconds: int = 5,
            log_success: bool = True,
        ) -> bool:
            """Set TV brightness with retry logic and activity logging on failure.
            
            Args:
                tv_id: The TV identifier
                brightness: Target brightness (1-10)
                reason: Description for activity log (e.g., "auto, lux: 150" or "post-shuffle sync")
                max_attempts: Number of attempts before giving up
                retry_delay_seconds: Delay between retries
                log_success: Whether to log activity entry on success (default True)
                
            Returns:
                True if brightness was set successfully, False otherwise
            """
            import asyncio
            
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config:
                _LOGGER.warning(f"Brightness: TV config not found for {tv_id}")
                return False
            
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                return False
            
            coordinator = data["coordinator"]
            tv_data = next((tv for tv in coordinator.data if tv["id"] == tv_id), None)
            if not tv_data:
                return False
            
            ip = tv_data["ip"]
            tv_name = tv_config.get("name", tv_id)
            client = data.get("art_clients", {}).get(tv_id)
            if client is None:
                _LOGGER.warning(f"Brightness: No art client found for {tv_id}")
                return False

            last_error = None
            for attempt in range(1, max_attempts + 1):
                try:
                    _LOGGER.info(
                        f"Brightness: Attempting to set {tv_name} ({ip}) to {brightness} "
                        f"(attempt {attempt}/{max_attempts})"
                    )
                    await frame_tv.set_tv_brightness(client, brightness)
                    
                    # Success! Store timestamp and brightness
                    from .config_entry import update_tv_config as update_config
                    update_config(
                        hass,
                        entry,
                        tv_id,
                        {
                            "last_auto_brightness_timestamp": datetime.now(timezone.utc).isoformat(),
                            "current_brightness": brightness,
                        },
                    )
                    
                    # Also store in hass.data for lightweight entity sync
                    brightness_cache = hass.data[DOMAIN][entry.entry_id].setdefault("brightness_cache", {})
                    brightness_cache[tv_id] = brightness
                    
                    # Send brightness signal so sensors update
                    signal = f"{DOMAIN}_brightness_adjusted_{entry.entry_id}_{tv_id}"
                    async_dispatcher_send(hass, signal)
                    
                    _LOGGER.info(f"Brightness: {tv_name} successfully set to {brightness}")
                    
                    # Log activity on success (can be suppressed for background operations)
                    if log_success:
                        log_activity(
                            hass, entry.entry_id, tv_id,
                            "brightness_adjusted",
                            f"Brightness → {brightness} ({reason})",
                        )
                    
                    return True
                    
                except Exception as err:
                    last_error = err
                    if attempt < max_attempts:
                        _LOGGER.warning(
                            f"Brightness attempt {attempt}/{max_attempts} failed for {tv_name}: {err}. "
                            f"Retrying in {retry_delay_seconds}s..."
                        )
                        await asyncio.sleep(retry_delay_seconds)
                    else:
                        _LOGGER.error(
                            f"Brightness failed for {tv_name} after {max_attempts} attempts: {err}"
                        )
            
            # All attempts failed - only log to activity if screen was on (unexpected failure)
            # When screen is off, brightness commands are expected to fail
            status_cache = data.get("tv_status_cache", {})
            screen_on = status_cache.get(tv_id, {}).get("screen_on", True)  # Default True to be safe
            
            if screen_on:
                log_activity(
                    hass, entry.entry_id, tv_id,
                    "brightness_failed",
                    f"Brightness → {brightness} failed: {last_error}",
                )
            else:
                _LOGGER.debug(
                    f"Brightness: Skipped failure log for {tv_name} - screen is off (expected)"
                )
            return False

        async def async_sync_brightness_after_shuffle(tv_id: str) -> bool:
            """Sync brightness to the TV after a shuffle completes.
            
            This ensures the TV has the correct brightness even if the previous
            set command was lost or the TV reset to a different value.
            Uses the cached brightness value (from auto-brightness or manual set),
            or calculates from lux sensor if auto-brightness is enabled.
            """
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config:
                return False
            
            tv_name = tv_config.get("name", tv_id)
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            
            # Determine target brightness
            target_brightness = None
            reason = "post-shuffle sync"
            
            # If auto-brightness is enabled, calculate from lux
            if tv_config.get("enable_dynamic_brightness", False):
                lux_entity_id = tv_config.get("light_sensor")
                if lux_entity_id:
                    lux_state = hass.states.get(lux_entity_id)
                    if lux_state and lux_state.state not in ("unavailable", "unknown"):
                        try:
                            current_lux = float(lux_state.state)
                            min_lux = tv_config.get("min_lux", 0)
                            max_lux = tv_config.get("max_lux", 1000)
                            min_brightness = tv_config.get("min_brightness", 1)
                            max_brightness = tv_config.get("max_brightness", 10)
                            
                            if max_lux > min_lux:
                                normalized = (current_lux - min_lux) / (max_lux - min_lux)
                                normalized = max(0.0, min(1.0, normalized))
                                target_brightness = int(round(
                                    min_brightness + normalized * (max_brightness - min_brightness)
                                ))
                                target_brightness = max(min_brightness, min(max_brightness, target_brightness))
                                reason = f"post-shuffle sync, lux: {current_lux:.0f}"
                        except (ValueError, TypeError):
                            pass
            
            # Fallback to cached brightness if auto-brightness didn't provide a value
            if target_brightness is None:
                brightness_cache = data.get("brightness_cache", {})
                target_brightness = brightness_cache.get(tv_id)
            
            # Final fallback to stored config brightness
            if target_brightness is None:
                target_brightness = tv_config.get("current_brightness")
            
            if target_brightness is None:
                _LOGGER.debug(f"Post-shuffle brightness sync: No brightness value for {tv_name}, skipping")
                return True  # Not an error, just nothing to sync
            
            _LOGGER.info(f"Post-shuffle brightness sync: Setting {tv_name} to {target_brightness}")
            
            brightness_client = data.get("art_clients", {}).get(tv_id)

            success = await async_set_brightness_with_retry(
                tv_id,
                int(target_brightness),
                reason=reason,
                log_success=False,  # Don't create noisy activity entries for background sync
            )

            if success and brightness_client:
                # Schedule delayed verification and reinforcement to catch brightness drift
                # See docs/BRIGHTNESS_DRIFT.md for details on this issue
                async def _delayed_brightness_check() -> None:
                    _LOGGER.debug(f"Starting delayed brightness verification for {tv_name}")
                    await asyncio.sleep(5)  # Wait for TV to settle after image render
                    try:
                        actual = await frame_tv.get_tv_brightness(brightness_client)
                        if actual is not None and actual != target_brightness:
                            _LOGGER.warning(
                                f"Brightness drift detected for {tv_name}: expected {target_brightness}, "
                                f"TV reports {actual}. Re-setting brightness."
                            )
                            # Re-set brightness to correct the drift
                            await async_set_brightness_with_retry(
                                tv_id,
                                int(target_brightness),
                                reason="drift correction",
                                log_success=False,
                            )
                        else:
                            _LOGGER.debug(
                                f"Post-shuffle brightness verified for {tv_name}: {actual}"
                            )
                    except Exception as err:
                        _LOGGER.debug(f"Post-shuffle brightness verification failed for {tv_name}: {err}")
                
                hass.async_create_task(_delayed_brightness_check())
            
            return success

        # Auto brightness helper for a single TV
        async def async_adjust_tv_brightness(tv_id: str, restart_timer: bool = False) -> bool:
            """Adjust brightness for a single TV. Returns True on success."""
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config:
                _LOGGER.warning(f"Auto brightness: TV config not found for {tv_id}")
                return False

            lux_entity_id = tv_config.get("light_sensor")
            if not lux_entity_id:
                _LOGGER.debug(f"Auto brightness: No light sensor configured for {tv_id}")
                return False

            # Get current lux value from the sensor
            lux_state = hass.states.get(lux_entity_id)
            if not lux_state or lux_state.state in ("unavailable", "unknown"):
                _LOGGER.debug(f"Lux sensor {lux_entity_id} unavailable for {tv_id}")
                return False

            try:
                current_lux = float(lux_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(f"Invalid lux value from {lux_entity_id}: {lux_state.state}")
                return False

            # Get calibration values
            min_lux = tv_config.get("min_lux", 0)
            max_lux = tv_config.get("max_lux", 1000)
            min_brightness = tv_config.get("min_brightness", 1)
            max_brightness = tv_config.get("max_brightness", 10)

            # Avoid division by zero
            if max_lux <= min_lux:
                _LOGGER.warning(f"Invalid lux range for {tv_id}: max_lux must be > min_lux")
                return False

            # Calculate normalized value (0-1) with clamping
            normalized = (current_lux - min_lux) / (max_lux - min_lux)
            normalized = max(0.0, min(1.0, normalized))

            # Calculate target brightness
            target_brightness = int(round(
                min_brightness + normalized * (max_brightness - min_brightness)
            ))
            target_brightness = max(min_brightness, min(max_brightness, target_brightness))

            # Get TV data from coordinator
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                return False

            coordinator = data["coordinator"]
            tv_data = next((tv for tv in coordinator.data if tv["id"] == tv_id), None)
            if not tv_data:
                return False

            ip = tv_data["ip"]
            tv_name = tv_config.get("name", tv_id)

            # Check if brightness already matches target - skip unnecessary command
            brightness_cache = data.get("brightness_cache", {})
            current_brightness = brightness_cache.get(tv_id)
            if current_brightness is None:
                current_brightness = tv_config.get("current_brightness")
            
            if current_brightness is not None and int(current_brightness) == target_brightness:
                _LOGGER.debug(
                    f"Auto brightness: {tv_name} already at brightness {target_brightness}, skipping"
                )
                # Log that we checked but didn't need to adjust
                # log_activity(
                #     hass, entry.entry_id, tv_id,
                #     "brightness_skipped",
                #     f"Already at brightness {target_brightness}",
                # )
                # Still restart timer if requested
                if restart_timer and tv_config.get("enable_dynamic_brightness", False):
                    start_tv_timer(tv_id)
                return True

            # Set brightness on TV with retry logic
            success = await async_set_brightness_with_retry(
                tv_id,
                target_brightness,
                reason=f"auto, lux: {current_lux:.0f}",
            )
            
            # Restart timer if requested (e.g., from Trigger Now button)
            if restart_timer and tv_config.get("enable_dynamic_brightness", False):
                start_tv_timer(tv_id)
            
            return success

        # Store helper functions so buttons/switches/shuffle can use them
        hass.data[DOMAIN][entry.entry_id]["async_adjust_tv_brightness"] = async_adjust_tv_brightness
        hass.data[DOMAIN][entry.entry_id]["async_set_brightness_with_retry"] = async_set_brightness_with_retry
        hass.data[DOMAIN][entry.entry_id]["async_sync_brightness_after_shuffle"] = async_sync_brightness_after_shuffle
        hass.data[DOMAIN][entry.entry_id]["start_tv_timer"] = start_tv_timer
        hass.data[DOMAIN][entry.entry_id]["cancel_tv_timer"] = cancel_tv_timer

        # Start timers for all TVs that have auto brightness enabled
        tv_configs = list_tv_configs(entry)
        for tv_id, tv_config in tv_configs.items():
            if tv_config.get("enable_dynamic_brightness", False):
                _LOGGER.info(f"Auto brightness: Starting timer for {tv_config.get('name', tv_id)}")
                start_tv_timer(tv_id)

        # Trigger a coordinator refresh so sensors pick up the new next_times
        await coordinator.async_request_refresh()

        # ===== AUTO SHUFFLE MANAGEMENT =====
        auto_shuffle_timers: dict[str, Callable[[], None]] = {}
        auto_shuffle_next_times = hass.data[DOMAIN][entry.entry_id]["auto_shuffle_next_times"]

        def _set_auto_shuffle_next_time(tv_id: str, next_time: datetime | None) -> None:
            if next_time is None:
                auto_shuffle_next_times.pop(tv_id, None)
            else:
                auto_shuffle_next_times[tv_id] = next_time
            signal = f"{SIGNAL_AUTO_SHUFFLE_NEXT}_{entry.entry_id}_{tv_id}"
            async_dispatcher_send(hass, signal)

        def cancel_auto_shuffle_timer(tv_id: str) -> None:
            """Cancel the auto shuffle timer for a specific TV."""
            if tv_id in auto_shuffle_timers:
                auto_shuffle_timers[tv_id]()
                del auto_shuffle_timers[tv_id]
            _set_auto_shuffle_next_time(tv_id, None)

        async def async_run_auto_shuffle(tv_id: str) -> None:
            """Execute an auto shuffle cycle for a TV."""
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config or not tv_config.get(CONF_ENABLE_AUTO_SHUFFLE, False):
                cancel_auto_shuffle_timer(tv_id)
                return

            tv_name = tv_config.get("name", tv_id)
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            status_cache = data.get("tv_status_cache", {})
            screen_state = status_cache.get(tv_id, {}).get("screen_on")

            if screen_state is False:
                return

            if screen_state is None:
                log_activity(
                    hass,
                    entry.entry_id,
                    tv_id,
                    "shuffle_skipped",
                    "Auto shuffle skipped: Screen state unknown",
                )
                return

            # Check if override is active - skip recency during overrides
            # (user deliberately chose a different tagset, don't constrain their pool)
            has_override = tv_config.get("override_tagset")
            if has_override:
                recent_images = None
            else:
                # Get recent images for recency preference using dual time windows:
                # - Same-TV: 120 hours (5 days) — "don't show what I've seen on this TV recently"
                # - Cross-TV: 72 hours (3 days) — "don't show what was recently on another TV"
                #
                # The all-TVs query includes the current TV, so there's some overlap
                # with the same-TV query. This is harmless — unioning sets just means
                # some images appear in both, but the final "recent" set is the same.
                recent_images: set[str] | None = set()
                display_log = data.get("display_log")
                if display_log:
                    # Get recency windows from config (with defaults)
                    same_tv_hours, cross_tv_hours = get_recency_windows(entry)
                    same_tv_recent = display_log.get_recent_auto_shuffle_images(tv_id=tv_id, hours=same_tv_hours)
                    cross_tv_recent = display_log.get_recent_auto_shuffle_images(tv_id=None, hours=cross_tv_hours)
                    recent_images = same_tv_recent | cross_tv_recent

            await async_shuffle_tv(
                hass,
                entry,
                tv_id,
                reason="auto",
                skip_if_screen_off=True,
                recent_images=recent_images,
            )

        def start_auto_shuffle_timer(tv_id: str) -> None:
            """Start or restart the auto shuffle timer for a TV."""
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config or not tv_config.get(CONF_ENABLE_AUTO_SHUFFLE, False):
                cancel_auto_shuffle_timer(tv_id)
                return

            cancel_auto_shuffle_timer(tv_id)

            frequency_minutes = int(tv_config.get(CONF_SHUFFLE_FREQUENCY, 60) or 60)
            if frequency_minutes <= 0:
                frequency_minutes = 1
            interval = timedelta(minutes=frequency_minutes)
            tv_name = tv_config.get("name", tv_id)

            async def async_auto_shuffle_tick(_now: Any) -> None:
                tv_configs_inner = list_tv_configs(entry)
                tv_config_inner = tv_configs_inner.get(tv_id)
                if not tv_config_inner or not tv_config_inner.get(CONF_ENABLE_AUTO_SHUFFLE, False):
                    cancel_auto_shuffle_timer(tv_id)
                    return

                _set_auto_shuffle_next_time(tv_id, datetime.now(timezone.utc) + interval)
                await async_run_auto_shuffle(tv_id)

            unsubscribe = async_track_time_interval(
                hass,
                async_auto_shuffle_tick,
                interval,
            )
            auto_shuffle_timers[tv_id] = unsubscribe
            entry.async_on_unload(unsubscribe)

            next_time = datetime.now(timezone.utc) + interval
            _set_auto_shuffle_next_time(tv_id, next_time)
            _LOGGER.debug(
                "Auto shuffle (%s): Next shuffle scheduled at %s",
                tv_name,
                next_time.isoformat(),
            )


        hass.data[DOMAIN][entry.entry_id]["start_auto_shuffle_timer"] = start_auto_shuffle_timer
        hass.data[DOMAIN][entry.entry_id]["cancel_auto_shuffle_timer"] = cancel_auto_shuffle_timer
        hass.data[DOMAIN][entry.entry_id]["async_run_auto_shuffle"] = async_run_auto_shuffle

        tv_configs = list_tv_configs(entry)
        for tv_id, tv_config in tv_configs.items():
            if tv_config.get(CONF_ENABLE_AUTO_SHUFFLE, False):
                _LOGGER.debug("Auto shuffle: Starting timer for %s", tv_config.get("name", tv_id))
                start_auto_shuffle_timer(tv_id)

        # ===== ART CHANNEL RECOVERY WATCHDOG =====
        # Periodically checks for TVs with a pending stale-channel recovery and
        # power-cycles them when the timing is appropriate (screen off, or overnight
        # fallback when deferred too long).

        # Reasons that should recover at the next watchdog tick (screen state irrelevant).
        _IMMEDIATE_RECOVERY_REASONS = frozenset({
            "manual", "calendar_event", "calendar_event_end", "expiry",
            "override", "override_clear", "tagset_select", "turn_on_reshuffle",
        })
        _OVERNIGHT_HOURS_START = 3   # 03:00 local
        _OVERNIGHT_HOURS_END = 5     # 05:00 local
        _OVERNIGHT_DEFER_THRESHOLD = 3 * 3600  # defer threshold before overnight fallback (s)
        _RECOVERY_COOLDOWN = 2 * 3600  # minimum time between recovery attempts per TV (s)

        async def _async_art_channel_watchdog(_now: datetime) -> None:
            """Check for pending art channel recoveries and execute when appropriate."""
            data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not data:
                return

            recovery_pending: dict = data.get("recovery_pending", {})
            art_channel_recovery: dict = data.get("art_channel_recovery", {})
            now_mono = asyncio.get_event_loop().time()
            local_hour = datetime.now().hour

            for tv_id in list(recovery_pending):
                tv_cfg = entry.data.get("tvs", {}).get(tv_id)
                if not tv_cfg or not tv_cfg.get("auto_recover_art_channel", False):
                    recovery_pending.pop(tv_id, None)
                    continue

                client = data.get("art_clients", {}).get(tv_id)
                if not client or client.stale_duration() == 0.0:
                    # Channel healthy again (e.g. manual power-cycle) — clear flag.
                    recovery_pending.pop(tv_id, None)
                    continue

                # Cooldown guard — don't hammer the TV repeatedly.
                cooldown_until = art_channel_recovery.get(tv_id, {}).get("cooldown_until", 0)
                if now_mono < cooldown_until:
                    continue

                pending = recovery_pending[tv_id]
                reason = pending.get("reason", "auto")
                pending_age = now_mono - pending.get("pending_since", now_mono)

                # Decide whether to recover now.
                tv_name = tv_cfg.get("name", tv_id)
                tv_ip = tv_cfg.get("ip", "")
                should_recover = False

                if reason in _IMMEDIATE_RECOVERY_REASONS:
                    should_recover = True
                else:
                    # Scheduled origin: prefer screen-off window; fall back to overnight.
                    try:
                        screen_on = await is_screen_on(tv_ip)
                    except Exception:
                        screen_on = True  # assume screen on if we can't check
                    if not screen_on:
                        should_recover = True
                    elif pending_age > _OVERNIGHT_DEFER_THRESHOLD:
                        if _OVERNIGHT_HOURS_START <= local_hour < _OVERNIGHT_HOURS_END:
                            should_recover = True
                        else:
                            log_activity(
                                hass, entry.entry_id, tv_id,
                                "art_channel_stale_deferred",
                                f"Art channel stale for {tv_name} — deferring recovery "
                                f"({pending_age / 3600:.1f}h pending; waiting for overnight window)",
                            )

                if not should_recover:
                    continue

                _LOGGER.info(
                    "Art channel watchdog: recovering %s (reason=%s, pending=%.0fs)",
                    tv_name, reason, pending_age,
                )
                mac = tv_cfg.get("mac")
                try:
                    ok = await client.recover_art_channel(mac_address=mac)
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.warning("Art channel recovery exception for %s: %s", tv_name, err)
                    ok = False

                # Stamp cooldown regardless of outcome.
                art_channel_recovery[tv_id] = {"cooldown_until": now_mono + _RECOVERY_COOLDOWN}
                recovery_pending.pop(tv_id, None)

                if ok:
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "art_channel_recovered",
                        f"Art channel recovered for {tv_name} (was stale for {pending_age / 60:.0f}min)",
                    )
                else:
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "art_channel_recovery_failed",
                        f"Art channel recovery failed for {tv_name} — will retry after cooldown",
                    )

        entry.async_on_unload(
            async_track_time_interval(
                hass,
                _async_art_channel_watchdog,
                timedelta(seconds=60),
            )
        )

        # ===== AUTO MOTION CONTROL =====
        # Per-TV motion listener and off-timer management
        motion_listeners: dict[str, Callable[[], None]] = {}
        motion_off_timers: dict[str, Callable[[], None]] = {}
        # Use the dict already initialized in hass.data so sensors can access it
        motion_off_times = hass.data[DOMAIN][entry.entry_id]["motion_off_times"]

        def _get_sensor_short_name(sensor_id: str) -> str:
            """Extract a friendly short name from a sensor entity ID."""
            # binary_sensor.kitchen_motion -> kitchen_motion
            if "." in sensor_id:
                return sensor_id.split(".", 1)[1]
            return sensor_id

        def cancel_motion_off_timer(tv_id: str) -> None:
            """Cancel the motion off timer for a specific TV."""
            _LOGGER.debug(f"Auto motion: Cancelling off timer for {tv_id}")
            if tv_id in motion_off_timers:
                motion_off_timers[tv_id]()
                del motion_off_timers[tv_id]
            if tv_id in motion_off_times:
                del motion_off_times[tv_id]
                # Signal sensors to update
                async_dispatcher_send(hass, f"{DOMAIN}_motion_off_time_updated_{entry.entry_id}_{tv_id}")

        # Constants for upload-in-progress reschedule behavior
        UPLOAD_WAIT_RESCHEDULE_SECONDS = 30
        UPLOAD_WAIT_MAX_RESCHEDULES = 10  # 10 * 30s = 5 minutes max wait

        def start_motion_off_timer(tv_id: str, reschedule_count: int = 0) -> None:
            """Start or restart the motion off timer for a specific TV.
            
            Always starts a fresh timer from now. If auto-motion is enabled
            and the TV is on, we manage its power state.
            
            Args:
                tv_id: The TV identifier
                reschedule_count: Number of times we've rescheduled due to upload-in-progress.
                    Used internally for the safety net; callers should not pass this.
            """
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config:
                return

            # If this is a reschedule due to upload-in-progress, use short delay
            # Otherwise use the configured off_delay_minutes
            if reschedule_count > 0:
                off_time = datetime.now(timezone.utc) + timedelta(seconds=UPLOAD_WAIT_RESCHEDULE_SECONDS)
            else:
                off_delay_minutes = tv_config.get("motion_off_delay", 15)
                off_time = datetime.now(timezone.utc) + timedelta(minutes=off_delay_minutes)
            
            tv_name = tv_config.get("name", tv_id)
            
            cancel_motion_off_timer(tv_id)
            motion_off_times[tv_id] = off_time
            
            # Signal sensors to update
            async_dispatcher_send(hass, f"{DOMAIN}_motion_off_time_updated_{entry.entry_id}_{tv_id}")

            async def async_motion_off_callback(_now: Any) -> None:
                """Timer callback to turn off TV."""
                tv_configs = list_tv_configs(entry)
                tv_config = tv_configs.get(tv_id)
                if not tv_config or not tv_config.get("enable_motion_control", False):
                    cancel_motion_off_timer(tv_id)
                    return

                tv_name = tv_config.get("name", tv_id)
                ip = tv_config.get("ip")
                if not ip:
                    _LOGGER.warning(f"Auto motion: No IP for {tv_name}")
                    return

                # Check if an upload is in progress for this TV - if so, delay turn-off
                # to avoid killing the connection mid-upload and causing spurious errors
                upload_flags = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("upload_in_progress", set())
                if tv_id in upload_flags:
                    # Safety net: limit max reschedules to prevent infinite loop if something
                    # goes very wrong. In practice this shouldn't be needed because:
                    # 1. async_guarded_upload uses try/finally to always clear the flag
                    # 2. Uploads have their own timeouts (websocket, HTTP)
                    # 3. HA restart would clear in-memory state anyway
                    # But we add this as defense-in-depth against unforeseen edge cases.
                    if reschedule_count >= UPLOAD_WAIT_MAX_RESCHEDULES:
                        _LOGGER.warning(
                            "Auto motion: Max reschedules (%d) reached for %s while waiting for upload. "
                            "Proceeding with turn-off anyway.",
                            UPLOAD_WAIT_MAX_RESCHEDULES, tv_name
                        )
                    else:
                        _LOGGER.debug(
                            "Auto motion: Delaying turn-off for %s - upload in progress (reschedule %d/%d)",
                            tv_name, reschedule_count + 1, UPLOAD_WAIT_MAX_RESCHEDULES
                        )
                        start_motion_off_timer(tv_id, reschedule_count + 1)
                        return

                try:
                    _LOGGER.info(f"Auto motion: Turning off {tv_name} ({ip}) due to no motion")
                    await frame_tv.tv_off(ip)
                    _LOGGER.info(f"Auto motion: {tv_name} turned off successfully")
                    
                    # Build message with last sensor info if available
                    motion_cache = hass.data[DOMAIN][entry.entry_id].get("motion_cache", {})
                    last_sensor = motion_cache.get(f"{tv_id}_last_sensor")
                    last_sensor_time_str = motion_cache.get(f"{tv_id}_last_sensor_time")
                    motion_off_msg = "Turned off (no motion)"
                    if last_sensor and last_sensor_time_str:
                        try:
                            last_sensor_time = datetime.fromisoformat(last_sensor_time_str)
                            minutes_ago = int((datetime.now(timezone.utc) - last_sensor_time).total_seconds() / 60)
                            sensor_short = _get_sensor_short_name(last_sensor)
                            motion_off_msg = f"Turned off - last: {sensor_short} {minutes_ago}m ago"
                        except (ValueError, TypeError):
                            pass
                    
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "motion_off",
                        motion_off_msg,
                    )
                    # Close the display log session since screen is turning off
                    display_log = hass.data[DOMAIN][entry.entry_id].get("display_log")
                    if display_log:
                        display_log.note_screen_off(tv_id=tv_id, tv_name=tv_name)
                except Exception as err:
                    _LOGGER.warning(f"Auto motion: Failed to turn off {tv_name}: {err}")
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "error",
                        f"Turn off failed: {err}",
                    )
                finally:
                    # Clear timer state
                    if tv_id in motion_off_times:
                        del motion_off_times[tv_id]
                    # Signal sensors to update
                    async_dispatcher_send(hass, f"{DOMAIN}_motion_off_time_updated_{entry.entry_id}_{tv_id}")
                    # Refresh coordinator to update all entities
                    await coordinator.async_request_refresh()

            # Schedule one-shot timer using async_track_point_in_time
            from homeassistant.helpers.event import async_track_point_in_time
            unsubscribe = async_track_point_in_time(
                hass,
                async_motion_off_callback,
                off_time,
            )
            motion_off_timers[tv_id] = unsubscribe
            # Note: Don't use entry.async_on_unload here - cancel_motion_off_timer 
            # handles cleanup, and we'd accumulate callbacks on repeated calls
            _LOGGER.debug(f"Auto motion: Off timer set for {tv_name} at {off_time}")

        async def async_handle_motion(tv_id: str, tv_config: dict, sensor_id: str | None = None) -> None:
            """Handle motion detection for a TV."""
            # Re-fetch tv_config to get current settings (e.g., verbose_motion_logging)
            # The passed tv_config may be stale if settings changed after listener started
            current_tv_configs = list_tv_configs(entry)
            current_tv_config = current_tv_configs.get(tv_id, tv_config)
            
            tv_name = current_tv_config.get("name", tv_id)
            ip = current_tv_config.get("ip")
            mac = current_tv_config.get("mac")

            if not ip:
                _LOGGER.warning(f"Auto motion: No IP for {tv_name}")
                return

            # Update last motion timestamp and sensor in runtime cache (NOT entry.data to avoid reload)
            motion_cache = hass.data[DOMAIN][entry.entry_id].setdefault("motion_cache", {})
            motion_cache[tv_id] = datetime.now(timezone.utc).isoformat()
            if sensor_id:
                motion_cache[f"{tv_id}_last_sensor"] = sensor_id
                motion_cache[f"{tv_id}_last_sensor_time"] = datetime.now(timezone.utc).isoformat()

            # Signal sensors to update
            async_dispatcher_send(hass, f"{DOMAIN}_motion_detected_{entry.entry_id}_{tv_id}")

            # Check if screen is on - if so, just reset timer
            try:
                screen_on = await frame_tv.is_screen_on(ip)
                if screen_on:
                    _LOGGER.debug(f"Auto motion: {tv_name} screen already on, resetting timer")
                    start_motion_off_timer(tv_id)
                    # Log if verbose motion logging is enabled
                    if current_tv_config.get("verbose_motion_logging", False) and sensor_id:
                        sensor_short = _get_sensor_short_name(sensor_id)
                        log_activity(
                            hass, entry.entry_id, tv_id,
                            "motion_detected",
                            f"Motion ({sensor_short}) - timer reset",
                        )
                    return
            except Exception as err:
                _LOGGER.debug(f"Auto motion: Could not check screen state for {tv_name}: {err}")
                # Continue to wake anyway - WOL is harmless if TV is already on

            power_on_in_progress = hass.data[DOMAIN][entry.entry_id].setdefault("power_on_in_progress", {})

            if power_on_in_progress.get(tv_id):
                _LOGGER.debug(f"Auto motion: {tv_name} power-on already in progress, skipping")
                return

            # Optimistically start the off timer so UI updates immediately
            # (The 15s wake sequence would otherwise leave the sensor 'unknown' for too long)
            start_motion_off_timer(tv_id)

            # Turn on TV via Wake-on-LAN
            if mac:
                try:
                    power_on_in_progress[tv_id] = True
                    _LOGGER.info(f"Auto motion: Waking {tv_name} ({ip}) via WOL")
                    motion_client = hass.data[DOMAIN][entry.entry_id].get("art_clients", {}).get(tv_id)
                    await frame_tv.tv_on(ip, mac, client=motion_client)
                    _LOGGER.info(f"Auto motion: {tv_name} wake sequence complete")
                    sensor_short = _get_sensor_short_name(sensor_id) if sensor_id else "motion"
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "motion_wake",
                        f"Screen on (woken by {sensor_short})",
                    )
                except Exception as err:
                    _LOGGER.warning(f"Auto motion: Failed to wake {tv_name}: {err}")
                    log_activity(
                        hass, entry.entry_id, tv_id,
                        "error",
                        f"Wake failed: {err}",
                    )
                    # If wake failed, cancel the timer we just started
                    cancel_motion_off_timer(tv_id)
                finally:
                    power_on_in_progress.pop(tv_id, None)
            else:
                _LOGGER.warning(f"Auto motion: No MAC address for {tv_name}, cannot wake")
                cancel_motion_off_timer(tv_id)

        def stop_motion_listener(tv_id: str) -> None:
            """Stop listening for motion for a specific TV."""
            if tv_id in motion_listeners:
                motion_listeners[tv_id]()
                del motion_listeners[tv_id]
            cancel_motion_off_timer(tv_id)
            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            tv_name = tv_config.get("name", tv_id) if tv_config else tv_id
            _LOGGER.info(f"Auto motion: Stopped listener for {tv_name}")

        async def async_start_motion_listener(tv_id: str) -> None:
            """Start listening for motion for a specific TV."""
            # Stop existing listener if any
            stop_motion_listener(tv_id)

            tv_configs = list_tv_configs(entry)
            tv_config = tv_configs.get(tv_id)
            if not tv_config:
                return

            motion_sensors = tv_config.get("motion_sensors", [])
            if not motion_sensors:
                _LOGGER.warning(f"Auto motion: No motion sensors configured for {tv_id}")
                return

            tv_name = tv_config.get("name", tv_id)
            ip = tv_config.get("ip")

            # We don't check hass.states.get() here because sensors
            # might not be initialized yet (e.g. Z-Wave/Zigbee at startup).
            # async_track_state_change_event handles missing entities gracefully
            # and will trigger once the entity becomes available.

            @callback
            def motion_state_changed(event: Any) -> None:
                """Handle motion sensor state change."""
                new_state = event.data.get("new_state")
                if not new_state:
                    return

                # Only trigger on motion detected (state = "on")
                # Any sensor in the list reporting "on" will wake the TV (OR logic)
                if new_state.state == "on":
                    sensor_id = new_state.entity_id
                    _LOGGER.debug(f"Auto motion: Motion detected for {tv_name} (sensor: {sensor_id})")
                    hass.async_create_task(async_handle_motion(tv_id, tv_config, sensor_id))

            # Subscribe to state changes for all configured motion sensors
            from homeassistant.helpers.event import async_track_state_change_event
            unsubscribe = async_track_state_change_event(
                hass,
                motion_sensors,
                motion_state_changed,
            )
            motion_listeners[tv_id] = unsubscribe
            entry.async_on_unload(unsubscribe)

            # Only start the off timer if the TV is currently on AND motion was recent
            # This handles HA restart - if motion is stale, don't set timer (TV was probably turned on manually)
            if ip:
                try:
                    screen_on = await frame_tv.is_screen_on(ip)
                    if screen_on:
                        _LOGGER.info(f"Auto motion: {tv_name} is on at startup, starting off timer")
                        start_motion_off_timer(tv_id)
                    else:
                        _LOGGER.info(f"Auto motion: {tv_name} is off, waiting for motion")
                except Exception as err:
                    _LOGGER.debug(f"Auto motion: Could not check {tv_name} screen state: {err}")
                    # If we can't check, don't start timer - wait for motion
            
            sensors_str = ", ".join(motion_sensors)
            _LOGGER.info(f"Auto motion: Started listener for {tv_name} (sensors: {sensors_str})")

        def start_motion_listener(tv_id: str) -> None:
            """Sync wrapper to start motion listener."""
            hass.async_create_task(async_start_motion_listener(tv_id))

        # Store helper functions for switches and other platforms
        hass.data[DOMAIN][entry.entry_id]["start_motion_listener"] = start_motion_listener
        hass.data[DOMAIN][entry.entry_id]["stop_motion_listener"] = stop_motion_listener
        hass.data[DOMAIN][entry.entry_id]["start_motion_off_timer"] = start_motion_off_timer
        hass.data[DOMAIN][entry.entry_id]["cancel_motion_off_timer"] = cancel_motion_off_timer

        # Start motion listeners for all TVs that have motion control enabled
        tv_configs = list_tv_configs(entry)
        for tv_id, tv_config in tv_configs.items():
            if tv_config.get("enable_motion_control", False):
                start_motion_listener(tv_id)

        # ── generate_oel_payload service ────────────────────────────────────
        # Calls the Frame Art Manager's /api/oel endpoint to produce a
        # pixel-accurate OEL drawcustom payload using real font metrics.
        # Returns {"payload": [...], "debug": {...}} for use with response_variable.

        async def async_handle_generate_oel_payload(call: ServiceCall) -> dict[str, Any]:
            """Call the manager's layout engine and return an OEL payload."""
            import html as _html
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            from homeassistant.helpers import entity_registry as er
            import aiohttp as _aiohttp

            manager_url: str = call.data.get("manager_url", "").rstrip("/")
            if not manager_url:
                # Fall back to integration-stored manager URL
                manager_url = (
                    hass.data[DOMAIN][entry.entry_id].get("manager_url", "")
                    .rstrip("/")
                )
            if not manager_url:
                raise ServiceValidationError(
                    "No manager URL available. Set manager_url in the service call "
                    "or configure Frame Art Manager URL in the integration settings."
                )

            sensor_entity_id: str = call.data.get("sensor_entity_id", "")
            state = hass.states.get(sensor_entity_id) if sensor_entity_id else None
            attrs: dict[str, Any] = dict(state.attributes) if state else {}

            # Build the artwork URL using the manager's direct port (no HA auth
            # required), so QR codes can be scanned without being logged into HA.
            artwork_url = call.data.get("artwork_url", "")
            ent_reg = er.async_get(hass)
            ent_entry = ent_reg.async_get(sensor_entity_id) if sensor_entity_id else None
            if ent_entry and ent_entry.device_id and manager_url:
                artwork_url = f"{manager_url}/artwork/{ent_entry.device_id}"

            def _clean(val: Any) -> str:
                """Strip HTML entities from a metadata string value."""
                return _html.unescape(str(val)) if val else ""

            metadata = {
                "creator_name":        _clean(attrs.get("creator_name", "")),
                "creator_nationality": _clean(attrs.get("creator_nationality", "")),
                "creator_lifespan":    _clean(attrs.get("creator_lifespan", "")),
                "title":               _clean(attrs.get("title", "")),
                "date":                _clean(attrs.get("date", "")),
                "medium":              _clean(attrs.get("medium", "")),
                "dimensions":          _clean(attrs.get("dimensions", "")),
                "museum":              _clean(attrs.get("museum", "")),
                "description":         _clean(attrs.get("description", "")),
                "artwork_url":         artwork_url,
            }

            body = {
                "metadata":     metadata,
                "display":      {
                    "width":  call.data.get("display_width",  400),
                    "height": call.data.get("display_height", 300),
                },
                "refresh_type": call.data.get("refresh_type", "Full"),
                "template":     call.data.get("template", "museum_placard"),
            }

            session = async_get_clientsession(hass)
            try:
                async with session.post(
                    f"{manager_url}/api/oel",
                    json=body,
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise ServiceValidationError(
                            f"Manager returned HTTP {resp.status}: {text[:200]}"
                        )
                    return await resp.json()
            except _aiohttp.ClientError as err:
                raise ServiceValidationError(
                    f"Could not reach Frame Art Manager at {manager_url}: {err}"
                ) from err

        hass.services.async_register(
            DOMAIN,
            "generate_oel_payload",
            async_handle_generate_oel_payload,
            supports_response=SupportsResponse.ONLY,
        )

        # Generate and register the Lovelace dashboard
        await _async_setup_dashboard(hass, entry)

        # Log integration start for all TVs
        for tv_id in tv_configs.keys():
            log_activity(
                hass, entry.entry_id, tv_id,
                "integration_start",
                "Integration loaded",
            )

        # Register API endpoints
        hass.http.register_view(PoolHealthView(hass, entry))
        hass.http.register_view(FrameArtImageView(hass, entry))

        return True


    async def async_unload_entry(hass: Any, entry: Any) -> bool:
        """Unload a Frame Art Shuffler config entry."""

        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        if not unload_ok:
            return False

        data = hass.data.get(DOMAIN)
        if data and entry.entry_id in data:
            display_log: DisplayLogManager | None = data[entry.entry_id].get("display_log")
            if display_log:
                await display_log.async_shutdown()
            for client in data[entry.entry_id].get("art_clients", {}).values():
                await client.async_close()
            data.pop(entry.entry_id)

        if not hass.config_entries.async_entries(DOMAIN):
            set_token_directory(DEFAULT_TOKEN_DIR)

        return True


    async def _async_reload_entry(hass: Any, entry: Any) -> None:
        """Reload config entry."""
        # Check if reload is necessary
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if data and "config_snapshot" in data:
            old_structural = data["config_snapshot"]
            new_structural = _get_structural_config(entry.data)
            
            if old_structural == new_structural:
                _LOGGER.debug("Skipping reload for runtime config change")
                # Update snapshot just in case (though it should be identical)
                data["config_snapshot"] = new_structural

                display_log: DisplayLogManager | None = data.get("display_log")
                if display_log:
                    display_log.update_settings(
                        enabled=entry.options.get(CONF_LOGGING_ENABLED),
                        retention_months=entry.options.get(CONF_LOG_RETENTION_MONTHS),
                        flush_minutes=entry.options.get(CONF_LOG_FLUSH_MINUTES),
                    )
                return

        _LOGGER.info("Reloading entry due to structural config change")
        await hass.config_entries.async_reload(entry.entry_id)


    async def _async_migrate_from_metadata(
        hass: Any,
        entry: Any,
        metadata_path: Path,
    ) -> None:
        """One-time migration: import TV data from metadata.json into config entry."""
        import json

        def _read_tvs() -> list:
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    return json.load(f).get("tvs", [])
            except Exception:
                return []

        try:
            metadata_tvs = await hass.async_add_executor_job(_read_tvs)
        except Exception:
            # metadata.json might not exist yet or be empty
            return

        if not metadata_tvs:
            return

        tvs = {}
        for tv in metadata_tvs:
            tv_id = tv.get("id")
            if not tv_id:
                continue

            shuffle_data = tv.get("shuffle", {})
            tvs[tv_id] = {
                "id": tv_id,
                "name": tv.get("name"),
                "ip": tv.get("ip"),
                "mac": tv.get("mac"),
                "shuffle_frequency_minutes": shuffle_data.get("frequencyMinutes", 60) if isinstance(shuffle_data, dict) else 60,
                "tags": tv.get("tags", []),
                "exclude_tags": tv.get("notTags", []),
            }

        if tvs:
            data = {**entry.data, "tvs": tvs}
            hass.config_entries.async_update_entry(entry, data=data)


    def _async_remove_stale_entities(hass: Any, entry: Any) -> None:
        """Remove entity registry entries for entities removed in prior versions.

        Called once at setup; idempotent — silently skips entries that don't exist.
        """
        entity_registry = er.async_get(hass)
        tv_ids = list(entry.data.get("tvs", {}).keys())
        entry_id = entry.entry_id
        stale_unique_ids = (
            # Merged into single smart Art Mode button (v1.x entity audit)
            [(tv_id, "button", f"{tv_id}_on_art_mode") for tv_id in tv_ids]
            # Merged into single Shuffle button (v1.x entity audit)
            + [(tv_id, "button", f"{tv_id}_shuffle_silent") for tv_id in tv_ids]
            # Replaced by FrameArtArtworkInfoSensor (renamed Current Artwork)
            + [(tv_id, "sensor", f"{entry_id}_{tv_id}") for tv_id in tv_ids]
        )
        for _tv_id, platform, unique_id in stale_unique_ids:
            entity_id = entity_registry.async_get_entity_id(platform, DOMAIN, unique_id)
            if entity_id:
                _LOGGER.debug("Removing stale entity %s (unique_id: %s)", entity_id, unique_id)
                entity_registry.async_remove(entity_id)

    async def _async_migrate_motion_sensors(hass: Any, entry: Any) -> None:
        """Migrate motion_sensor (singular string) to motion_sensors (list).
        
        This migration converts the old single motion sensor config to the new
        multi-sensor list format. Added in v1.1.0.
        """
        tvs = entry.data.get("tvs", {})
        if not tvs:
            return

        # Check if any TV needs migration
        needs_migration = any(
            "motion_sensor" in tv_config and "motion_sensors" not in tv_config
            for tv_config in tvs.values()
        )

        if not needs_migration:
            return

        # Perform migration
        data = {**entry.data}
        tvs_copy = {tv_id: tv_data.copy() for tv_id, tv_data in tvs.items()}

        for tv_id, tv_config in tvs_copy.items():
            if "motion_sensor" in tv_config:
                old_value = tv_config.pop("motion_sensor")
                # Convert single value to list (or empty list if None/empty)
                tv_config["motion_sensors"] = [old_value] if old_value else []

        data["tvs"] = tvs_copy
        hass.config_entries.async_update_entry(entry, data=data)
        _LOGGER.info("Migrated motion_sensor config to motion_sensors list format")


    async def _async_migrate_tagsets_to_global(hass: Any, entry: Any) -> None:
        """Migrate per-TV tagsets to global tagsets.
        
        Old structure (v1.1.x): Each TV had its own tagsets dict
            tv_config["tagsets"] = {"everyday": {...}, "holiday": {...}}
        
        New structure (v1.2.0): Global tagsets at integration level
            entry.data["tagsets"] = {"everyday": {...}, "holiday": {...}}
            TVs reference global tagsets by name via selected_tagset/override_tagset
        
        Migration merges all per-TV tagsets into global, handling name conflicts
        by appending _tvname suffix.
        """
        # Skip if already migrated (global tagsets exist)
        if entry.data.get("tagsets"):
            return

        tvs = entry.data.get("tvs", {})
        if not tvs:
            return

        # Check if any TV has the old per-TV tagsets
        has_per_tv_tagsets = any(
            tv_config.get("tagsets")
            for tv_config in tvs.values()
        )

        if not has_per_tv_tagsets:
            return

        # Collect all tagsets from all TVs
        global_tagsets: dict[str, Any] = {}
        data = {**entry.data}
        tvs_copy = {tv_id: tv_data.copy() for tv_id, tv_data in tvs.items()}

        for tv_id, tv_config in tvs_copy.items():
            per_tv_tagsets = tv_config.get("tagsets", {})
            if not per_tv_tagsets:
                continue

            tv_name = tv_config.get("name", tv_id)
            
            for tagset_name, tagset_data in per_tv_tagsets.items():
                # If name already exists in global, append TV name suffix
                final_name = tagset_name
                if tagset_name in global_tagsets:
                    # Check if it's identical (same tags)
                    existing = global_tagsets[tagset_name]
                    if (existing.get("tags") == tagset_data.get("tags") and
                        existing.get("exclude_tags") == tagset_data.get("exclude_tags")):
                        # Identical, no need to create duplicate
                        pass
                    else:
                        # Different content, add with suffix
                        suffix = tv_name.lower().replace(" ", "_")
                        final_name = f"{tagset_name}_{suffix}"
                        while final_name in global_tagsets:
                            final_name = f"{final_name}_2"
                        global_tagsets[final_name] = tagset_data.copy()
                        # Update TV's selected/override if it was using this tagset
                        if tv_config.get("selected_tagset") == tagset_name:
                            tv_config["selected_tagset"] = final_name
                        if tv_config.get("override_tagset") == tagset_name:
                            tv_config["override_tagset"] = final_name
                else:
                    global_tagsets[final_name] = tagset_data.copy()

            # Remove per-TV tagsets after migration
            if "tagsets" in tv_config:
                del tv_config["tagsets"]

        data["tvs"] = tvs_copy
        data["tagsets"] = global_tagsets
        hass.config_entries.async_update_entry(entry, data=data)
        _LOGGER.info(
            "Migrated per-TV tagsets to global tagsets: %d tagsets created",
            len(global_tagsets)
        )


    async def _async_migrate_tagsets_to_manager(
        hass: Any,
        entry: Any,
        manager_url: str,
        tagsets: dict[str, Any],
    ) -> None:
        """Push existing tagset definitions to the manager add-on, then clear from config entry.

        Runs once at startup when entry.data["tagsets"] is non-empty.
        Non-fatal: if the manager is unreachable, tagsets stay in config entry
        and the migration retries on next HA restart.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        import aiohttp

        session = async_get_clientsession(hass)
        url = f"{manager_url.rstrip('/')}/api/tagsets"
        pushed = []

        for name, defn in tagsets.items():
            payload = {
                "name": name,
                "tags": defn.get("tags", []),
                "exclude_tags": defn.get("exclude_tags", []),
                "weighting_type": defn.get("weighting_type", "image"),
                "tag_weights": defn.get("tag_weights", {}),
            }
            try:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status in (200, 201, 409):
                        # 409 = already exists, that's fine
                        pushed.append(name)
                    else:
                        _LOGGER.warning(
                            "tagset migration: failed to push '%s' (status %s)", name, resp.status
                        )
            except Exception as err:
                _LOGGER.warning("tagset migration: error pushing '%s': %s", name, err)

        if len(pushed) == len(tagsets):
            _LOGGER.info(
                "tagset migration: pushed %d tagset(s) to manager, clearing from config entry",
                len(pushed),
            )
            # Refresh cache so subsequent shuffles see the newly pushed tagsets
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            tagset_cache = entry_data.get("tagset_cache")
            if tagset_cache:
                await tagset_cache.async_refresh()
            update_global_tagsets(hass, entry, {})
        else:
            _LOGGER.warning(
                "tagset migration: only pushed %d/%d tagsets — will retry on next restart",
                len(pushed),
                len(tagsets),
            )

    @callback
    def _register_device_removal_listener(
        hass: Any,
        entry: Any,
        metadata_path: Path,
    ) -> Callable[[], None]:
        """Register a listener for device removal to clean up metadata."""

        @callback
        def device_removed(event):
            """Handle device removal."""
            device_id = event.data["device_id"]
            device_registry = dr.async_get(hass)
            device = device_registry.async_get(device_id)

            if not device:
                return

            # Check if this device belongs to our integration
            if not any(entry.entry_id in entry_id for entry_id in device.config_entries):
                return

            # Extract TV ID from device identifiers
            # Format is now just tv_id (no home prefix)
            tv_id = None
            for identifier in device.identifiers:
                if identifier[0] == DOMAIN:
                    tv_id = identifier[1]
                    break

            if not tv_id:
                return

            # Remove from metadata
            async def _remove_tv():
                token_dir = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("token_dir")
                try:
                    # Get TV IP from config entry for token file deletion
                    tv_cfg = get_tv_config(entry, tv_id)
                    tv_ip = tv_cfg.get("ip") if tv_cfg else None

                    # Remove from config entry
                    remove_tv_config(hass, entry, tv_id)

                    # Delete token file if it exists
                    if token_dir and tv_ip:
                        from .flow_utils import safe_token_filename
                        token_path = Path(token_dir) / f"{safe_token_filename(tv_ip)}.token"
                        if token_path.exists():
                            await hass.async_add_executor_job(token_path.unlink)

                    # Refresh coordinator
                    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
                    if data:
                        coordinator = data.get("coordinator")
                        if coordinator:
                            await coordinator.async_request_refresh()
                except Exception:
                    # TV might already be deleted, ignore
                    pass

            hass.async_create_task(_remove_tv())

        return hass.bus.async_listen("device_registry_updated", device_removed)

else:

    async def async_setup(*args: Any, **kwargs: Any) -> bool:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)


    async def async_setup_entry(*args: Any, **kwargs: Any) -> bool:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)


    async def async_unload_entry(*args: Any, **kwargs: Any) -> bool:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)


    async def _async_reload_entry(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)


    async def _async_migrate_from_metadata(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)


    def _register_device_removal_listener(*args: Any, **kwargs: Any) -> Callable[[], None]:  # pragma: no cover - fallback path
        raise ImportError(HA_IMPORT_ERROR_MESSAGE)
