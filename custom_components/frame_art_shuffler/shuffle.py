"""Shuffle and upload helpers for Frame Art Shuffler.

This module centralizes all artwork uploads so we can enforce a per-TV
"only one upload at a time" guarantee. Any future feature that uploads an
image to a Frame TV **must** call :func:`async_guarded_upload` (either
directly or indirectly via :func:`async_shuffle_tv`) to ensure we never
run overlapping transfers for the same device.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .activity import log_activity
from .config_entry import (
    get_active_tagset_name,
    get_effective_tags,
    get_tagset_fingerprint,
    get_tv_config,
)
from .const import (
    CONF_CALENDAR_SUPPRESS_MOODS,
    CONF_MOOD_OVERRIDES,
    CONF_MOOD_OVERRIDE_EXPIRY,
    CONF_MOOD_SENSOR,
    DOMAIN,
)
from .frame_tv import (
    FrameArtError,
    select_and_cleanup,
    set_art_on_tv_deleteothers,
    upload_to_tv_only,
)

_LOGGER = logging.getLogger(__name__)

UploadWork = Callable[[], Awaitable[Any]]


def _resolve_active_moods(hass: HomeAssistant, tv_config: dict[str, Any]) -> list[str]:
    """Return the list of active mood IDs for a TV at the current moment.

    Merges two sources:
    - Sensor moods: read from the HA entity bound as mood_sensor (if any).
      Supports comma-separated string state or attribute 'moods' list.
    - Override moods: manually activated via the activate_mood service,
      subject to optional expiry.
    """
    if tv_config.get(CONF_CALENDAR_SUPPRESS_MOODS):
        return []

    mood_sensor = tv_config.get(CONF_MOOD_SENSOR, "")
    sensor_moods: list[str] = []
    if mood_sensor:
        state = hass.states.get(mood_sensor)
        if state and state.state not in ("unavailable", "unknown", ""):
            mood_attr = state.attributes.get("moods")
            if isinstance(mood_attr, list):
                sensor_moods = [str(m).strip() for m in mood_attr if m]
            else:
                sensor_moods = [m.strip() for m in state.state.split(",") if m.strip()]

    override_moods: list[str] = list(tv_config.get(CONF_MOOD_OVERRIDES) or [])
    expiry_str = tv_config.get(CONF_MOOD_OVERRIDE_EXPIRY)
    if expiry_str and override_moods:
        try:
            expiry = datetime.fromisoformat(expiry_str)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expiry:
                override_moods = []
        except Exception:
            override_moods = []

    seen: set[str] = set()
    result: list[str] = []
    for m in sensor_moods + override_moods:
        if m not in seen:
            seen.add(m)
            result.append(m)
    return result


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


async def _build_local_sensor_meta(
    entry_data: dict[str, Any],
    filename: str,
    tags: list[str],
) -> dict[str, Any]:
    """Build sensor metadata for a local image.

    Starts with filename + tags, then augments with any custom attributes
    (title, artist, year, medium, etc.) from the image cache.
    """
    meta: dict[str, Any] = {"filename": filename, "tags": tags}
    cache = entry_data.get("image_cache")
    if cache:
        try:
            image_meta = await cache.get_image(filename)
            if image_meta:
                for k, v in (image_meta.get("attributes") or {}).items():
                    if v is not None and v != "":
                        meta[k] = v
                for k, v in (image_meta.get("resolvedEntityAttributes") or {}).items():
                    if v is not None and v != "":
                        meta[k] = v
        except Exception:
            pass
    return meta
SkipCallback = Callable[[], None]
StatusCallback = Callable[[str, str], None]


# WoL ~45s + 3 retries × ~30s = ~135s worst case for a healthy upload; cap at 150s.
_GUARDED_UPLOAD_TIMEOUT = 150


async def async_guarded_upload(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    action: str,
    work: UploadWork,
    on_skip: SkipCallback | None = None,
) -> Any:
    """Run an upload while preventing concurrent uploads for the same TV."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if not data:
        return await work()

    upload_flags: set[str] = data.setdefault("upload_in_progress", set())

    if tv_id in upload_flags:
        tv_config = get_tv_config(entry, tv_id) or {}
        tv_name = tv_config.get("name", tv_id)
        _LOGGER.info(
            "Skipping %s for %s: another upload is still running", action, tv_name
        )
        if on_skip:
            on_skip()
        return None

    upload_flags.add(tv_id)
    try:
        try:
            async with asyncio.timeout(_GUARDED_UPLOAD_TIMEOUT):
                return await work()
        except TimeoutError as err:
            tv_config = get_tv_config(entry, tv_id) or {}
            tv_name = tv_config.get("name", tv_id)
            _LOGGER.warning(
                "Upload for %s (%s) timed out after %ss — art channel may be stale",
                tv_name, tv_id, _GUARDED_UPLOAD_TIMEOUT,
            )
            raise FrameArtError(
                f"Upload for {tv_name} timed out after {_GUARDED_UPLOAD_TIMEOUT}s "
                "— art channel may be stale; try again"
            ) from err
    finally:
        upload_flags.discard(tv_id)
        data["last_upload_cleared_at"] = asyncio.get_event_loop().time()


async def _async_select_image(
    hass: HomeAssistant,
    manager_url: str,
    tagset_name: str | None,
    current_image: str | None,
    tv_name: str,
    recent_images: set[str] | None = None,
    active_moods: list[str] | None = None,
) -> tuple[dict[str, Any] | None, int, str | None, int, bool]:
    """Call the manager add-on to select a random image.

    Returns the same 5-tuple as the old _select_random_image:
        (image_dict, eligible_count, selected_tag, fresh_count, used_fallback)
    image_dict is None when no eligible images exist.
    image_dict has {"_web_sources": True, "_virtual_tag_id": ...} for web sources.
    active_moods is the list of mood IDs currently active for this TV.
    """
    session = async_get_clientsession(hass)
    payload: dict[str, Any] = {
        "tagsetName": tagset_name,
        "currentImage": current_image,
        "recentImages": list(recent_images) if recent_images else [],
        "activeMoods": active_moods or [],
    }
    try:
        async with asyncio.timeout(10):
            resp = await session.post(
                f"{manager_url}/api/shuffle/select", json=payload
            )
            data = await resp.json()
    except Exception as err:
        raise FrameArtError(f"Shuffle select API call failed for {tv_name}: {err}") from err

    result_type = data.get("type")
    eligible_count = data.get("eligibleCount", 0)

    if result_type == "none":
        _LOGGER.warning("No eligible images for %s (eligibleCount=0)", tv_name)
        return None, eligible_count, None, 0, False

    if result_type == "web_source":
        return (
            {
                "_web_sources": True,
                "_virtual_tag_id": data.get("virtualTagId"),
                "_mood_keyword": data.get("moodKeyword"),
            },
            eligible_count,
            data.get("selectedTag"),
            0,
            False,
        )

    # Library image
    return (
        data,
        eligible_count,
        data.get("selectedTag"),
        data.get("freshCount", 0),
        data.get("usedFallback", False),
    )




async def _async_trigger_prefetch(
    hass: HomeAssistant,
    manager_url: str,
    device_id: str,
    virtual_tag_id: str | None = None,
    tv_orientation: str | None = None,
    active_moods: list[str] | None = None,
) -> None:
    """Fire-and-forget: ask the add-on to pre-fetch the next image for a device."""
    session = async_get_clientsession(hass)
    payload: dict[str, Any] = {}
    if virtual_tag_id:
        payload["virtualTagId"] = virtual_tag_id
    if tv_orientation:
        payload["tvOrientation"] = tv_orientation
    if active_moods:
        payload["activeMoods"] = active_moods
    try:
        async with asyncio.timeout(10):
            await session.post(
                f"{manager_url}/api/web-sources/prefetch/{device_id}",
                json=payload,
            )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Pre-fetch trigger failed for device %s (non-fatal): %s", device_id, err)


async def _async_web_source_send(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    tv_name: str,
    entry_data: dict[str, Any],
    *,
    select: bool = True,
    screen_on: bool = True,
    virtual_tag_id: str | None = None,
    matte: str | None = None,
    matching_count: int = 0,
    selected_tag: str | None = None,
    active_moods: list[str] | None = None,
    mood_keyword: str | None = None,
    _notify: Callable[[str, str], None] | None = None,
) -> dict[str, Any] | None:
    """Call the Frame Art Manager add-on to fetch and send a web source image.

    When select=True: fetches, uploads, selects on TV, updates activity/cache/signals.
    When select=False: fetches, uploads only. Returns metadata for staging.

    Returns a dict with response data, or None on failure (select=False only).
    Raises FrameArtError on failure when select=True.
    """
    registry = dr.async_get(hass)
    device = registry.async_get_device(identifiers={(DOMAIN, tv_id)})
    if not device:
        if select:
            raise FrameArtError(f"Could not find HA device for TV '{tv_name}' (id: {tv_id})")
        _LOGGER.warning("pre-upload: no HA device for TV '%s'", tv_name)
        return None

    frame_art_manager_url = entry.data.get("frame_art_manager_url", "http://localhost:8099")
    session = async_get_clientsession(hass)

    payload: dict[str, Any] = {"deviceId": device.id, "select": select}
    if select:
        payload["screenOn"] = screen_on
    if virtual_tag_id:
        payload["virtualTagId"] = virtual_tag_id
    if active_moods:
        payload["activeMoods"] = active_moods
    if mood_keyword:
        payload["moodKeyword"] = mood_keyword
    if matte:
        payload["matte"] = matte

    # NOTE: Do NOT set upload_in_progress here.  The add-on calls back into
    # the send_image service, which runs under async_guarded_upload and sets
    # the flag for the actual TV WebSocket operation.  Setting it here would
    # cause send_image to see the flag already set and immediately 500.
    try:
        async with asyncio.timeout(65 if select else 120):
            resp = await session.post(
                f"{frame_art_manager_url}/api/web-sources/fetch-and-send",
                json=payload,
            )
            data = await resp.json()
    except Exception as err:
        if select:
            raise FrameArtError(f"Web source API call failed for {tv_name}: {err}") from err
        _LOGGER.warning("pre-upload: web source fetch-and-send failed for %s: %s", tv_name, err)
        return None

    if not data.get("success"):
        error_msg = data.get("error", "Unknown error")
        if select:
            raise FrameArtError(f"Web source fetch failed for {tv_name}: {error_msg}")
        _LOGGER.warning("pre-upload: web source fetch-and-send returned error for %s: %s", tv_name, error_msg)
        return None

    if select:
        # Update activity, cache, signals
        art_metadata = data.get("metadata", {})
        title = art_metadata.get("title") or "Unknown"
        source = art_metadata.get("source") or "web source"
        log_activity(hass, entry.entry_id, tv_id, "shuffle", f"Web source selected: \"{title}\" from {source}")

        display_log = entry_data.get("display_log")
        if display_log:
            now_log = datetime.now(timezone.utc)
            display_log.note_display_start(
                tv_id=tv_id,
                tv_name=tv_name,
                filename=f"web:{source}/{title}",
                tags=[selected_tag] if selected_tag else [],
                source="shuffle",
                shuffle_mode=None,
                started_at=now_log,
                tagset_name=get_active_tagset_name(entry, tv_id),
                source_url=art_metadata.get("artworkUrl"),
            )

        now = datetime.now(timezone.utc)
        shuffle_cache = entry_data.setdefault("shuffle_cache", {})
        shuffle_cache[tv_id] = {
            "current_image": None,
            "current_matte": None,
            "current_filter": None,
            "matching_image_count": matching_count,
            "last_shuffle_timestamp": now.isoformat(),
            "selected_tag": selected_tag,
            "web_source": True,
        }

        signal = f"{DOMAIN}_shuffle_{entry.entry_id}_{tv_id}"
        async_dispatcher_send(hass, signal)

        if coordinator := entry_data.get("coordinator"):
            await coordinator.async_set_active_image(tv_id, None, is_shuffle=True)

        # Augment the artwork sensor with cache_file so entity_picture works
        artwork_sensor = entry_data.get("artwork_sensors", {}).get(tv_id)
        cache_file = data.get("cacheFile")
        if artwork_sensor and cache_file:
            artwork_sensor.set_cache_file(cache_file)
            artwork_sensor.async_write_ha_state()

        if _notify:
            _notify("success", f"Web source selected: {title}")

        # Kick off background pre-fetch so the NEXT shuffle can skip the
        # fetch+process overhead.  Fire-and-forget.
        # The add-on stores the pre-fetch keyed by (config fingerprint + activeMoods);
        # at use-time only the moods are checked, not the specific keyword.
        used_virtual_tag_id = data.get("virtualTagId") or virtual_tag_id
        if used_virtual_tag_id or active_moods:
            hass.async_create_background_task(
                _async_trigger_prefetch(
                    hass, frame_art_manager_url, device.id,
                    used_virtual_tag_id,
                    active_moods=active_moods,
                ),
                name=f"prefetch_{tv_id}",
            )

    return {
        "content_id": data.get("contentId"),
        "metadata": data.get("metadata", {}),
        "artwork_metadata": data.get("artworkMetadata", {}),
        "source_id": data.get("sourceId"),
        "virtual_tag_id": data.get("virtualTagId"),
        "cache_file": data.get("cacheFile"),
    }


async def _async_fast_path_shuffle(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    tv_name: str,
    staged: dict[str, Any],
    entry_data: dict[str, Any],
    _notify: Callable[[str, str], None],
    *,
    screen_on: bool = True,
    reason: str = "manual",
    recent_images: set[str] | None = None,
) -> bool:
    """Execute a fast-path shuffle using a pre-uploaded staged image.

    Calls select_and_cleanup to display the staged image without re-uploading.
    Updates sensors, activity, display log — same as a full shuffle.
    Returns True on success, False if the staged image could not be selected.
    """
    content_id = staged["content_id"]
    tv_config = get_tv_config(entry, tv_id)
    tv_mac = tv_config.get("mac") if tv_config else None

    client = entry_data.get("art_clients", {}).get(tv_id)
    if client is None:
        _LOGGER.warning("fast-path: no art client for %s", tv_name)
        return False

    photo_filter = staged.get("photo_filter")

    # Guard against concurrent TV WebSocket operations (e.g. two rapid button
    # presses).  Two concurrent select_and_cleanup calls share the same WebSocket
    # connection and produce interleaved responses that confuse the art channel.
    async def _do_select() -> bool:
        try:
            return await select_and_cleanup(
                client,
                content_id,
                screen_on=screen_on,
                mac_address=tv_mac,
                photo_filter=photo_filter,
                debug=False,
            )
        except Exception as err:
            _LOGGER.warning("fast-path: select_and_cleanup failed for %s: %s", tv_name, err)
            return False

    def _on_skip() -> None:
        _LOGGER.info(
            "fast-path: skipping %s for %s — another upload already in progress",
            content_id, tv_name,
        )

    result = await async_guarded_upload(
        hass, entry, tv_id, "fast-path shuffle", _do_select, on_skip=_on_skip,
    )
    if not result:
        return False

    # --- Update sensors, cache, activity (mirrors full-path _perform_upload) ---
    now = datetime.now(timezone.utc)
    shuffle_cache = entry_data.setdefault("shuffle_cache", {})
    tagset_cache = entry_data.get("tagset_cache")
    tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
    include_tags, _ = get_effective_tags(entry, tv_id, tagsets=tagsets)

    if staged.get("source_type") == "web_source":
        # Promote staged cache → display cache on the add-on so the artwork
        # page serves the correct image for what's now on the TV.
        promote_cache_file = None
        registry = dr.async_get(hass)
        device = registry.async_get_device(identifiers={(DOMAIN, tv_id)})
        if device:
            frame_art_manager_url = entry.data.get(
                "frame_art_manager_url", "http://localhost:8099",
            )
            session = async_get_clientsession(hass)
            try:
                async with asyncio.timeout(10):
                    promote_resp = await session.post(
                        f"{frame_art_manager_url}/api/web-sources/cache/{device.id}/promote",
                    )
                    promote_data = await promote_resp.json()
                    promote_cache_file = promote_data.get("cacheFile")
            except Exception as err:
                _LOGGER.warning("fast-path: cache promote failed for %s: %s", tv_name, err)

        art_metadata = staged.get("metadata", {})
        title = art_metadata.get("title") or "Unknown"
        source = art_metadata.get("source") or "web source"
        activity_msg = f"Web source displayed (fast): \"{title}\" from {source}"
        log_activity(hass, entry.entry_id, tv_id, "shuffle", activity_msg)

        # Update artwork info sensor with mapped HA metadata (rich fields)
        artwork_metadata = staged.get("artwork_metadata", {})
        artwork_sensor = entry_data.get("artwork_sensors", {}).get(tv_id)
        if artwork_sensor and content_id:
            _meta = artwork_metadata or art_metadata
            _LOGGER.debug(
                "Artwork sensor update [%s] content_id=%s source=web_source(fast) title=%r creator=%r keys=%s",
                tv_id, content_id[:8],
                _meta.get("title", ""),
                _meta.get("creator_name", _meta.get("creator", "")),
                [k for k, v in _meta.items() if v not in (None, "", [])],
            )
            artwork_sensor.set_artwork(content_id, _meta, source_type="web_source")
            if promote_cache_file:
                artwork_sensor.set_cache_file(promote_cache_file)
            artwork_sensor.async_write_ha_state()
            log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
            await hass.async_add_executor_job(
                _write_artwork_sensor_log, log_path, tv_id, content_id, "web_source(fast)", _meta
            )

        shuffle_cache[tv_id] = {
            "current_image": None,
            "current_matte": None,
            "current_filter": None,
            "matching_image_count": 0,
            "last_shuffle_timestamp": now.isoformat(),
            "selected_tag": staged.get("selected_tag"),
            "web_source": True,
        }

        signal = f"{DOMAIN}_shuffle_{entry.entry_id}_{tv_id}"
        async_dispatcher_send(hass, signal)

        if coordinator := entry_data.get("coordinator"):
            await coordinator.async_set_active_image(tv_id, None, is_shuffle=True)

        display_log = entry_data.get("display_log")
        if display_log:
            art_meta = staged.get("artwork_metadata") or staged.get("metadata", {})
            display_log.note_display_start(
                tv_id=tv_id,
                tv_name=tv_name,
                filename=f"web:{source}/{title}",
                tags=[staged.get("selected_tag")] if staged.get("selected_tag") else [],
                source="shuffle",
                shuffle_mode=reason,
                started_at=now,
                tagset_name=get_active_tagset_name(entry, tv_id),
                source_url=art_meta.get("artworkUrl"),
            )

        _notify("success", f"Web source displayed (fast): {title}")
    else:
        image_filename = staged.get("filename", "unknown")
        image_data = staged.get("image_data", {})
        image_matte = staged.get("matte")
        image_filter = staged.get("photo_filter")
        selected_tag = staged.get("selected_tag")

        # Update artwork info sensor
        artwork_sensor = entry_data.get("artwork_sensors", {}).get(tv_id)
        if artwork_sensor and content_id:
            sensor_meta = await _build_local_sensor_meta(
                entry_data, image_filename, list(image_data.get("tags", []))
            )
            _LOGGER.debug(
                "Artwork sensor update [%s] content_id=%s source=local(staged) filename=%r",
                tv_id, content_id[:8], image_filename,
            )
            artwork_sensor.set_artwork(content_id, sensor_meta, source_type="local")
            artwork_sensor.async_write_ha_state()
            log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
            await hass.async_add_executor_job(
                _write_artwork_sensor_log, log_path, tv_id, content_id, "local(staged)", sensor_meta
            )

        shuffle_cache[tv_id] = {
            "current_image": image_filename,
            "current_matte": image_matte,
            "current_filter": image_filter,
            "matching_image_count": 0,
            "last_shuffle_timestamp": now.isoformat(),
            "selected_tag": selected_tag,
        }

        activity_msg = f"Shuffled to {image_filename} (fast path)"
        log_activity(hass, entry.entry_id, tv_id, "shuffle", activity_msg)

        signal = f"{DOMAIN}_shuffle_{entry.entry_id}_{tv_id}"
        async_dispatcher_send(hass, signal)

        if coordinator := entry_data.get("coordinator"):
            await coordinator.async_set_active_image(tv_id, image_filename, is_shuffle=True)

        display_log = entry_data.get("display_log")
        if display_log:
            tagset_name = get_active_tagset_name(entry, tv_id, tagsets=tagsets)
            display_log.note_display_start(
                tv_id=tv_id,
                tv_name=tv_name,
                filename=image_filename,
                tags=list(image_data.get("tags", [])),
                source="shuffle",
                shuffle_mode=reason,
                started_at=now,
                tv_tags=include_tags if include_tags else None,
                matte=image_matte,
                photo_filter=image_filter,
                tagset_name=tagset_name,
            )

        _notify("success", f"Shuffled to {image_filename} (fast)")

    # Sync brightness (same as full path)
    if screen_on:
        async_sync_brightness = entry_data.get("async_sync_brightness_after_shuffle")
        if async_sync_brightness:
            try:
                await async_sync_brightness(tv_id)
            except Exception as err:
                _LOGGER.warning("Post-shuffle brightness sync failed for %s: %s", tv_name, err)

    # Clear any pending-reshuffle flag — this shuffle counts as the retry.
    entry_data.get("pending_reshuffles", set()).discard(tv_id)
    return True


async def _async_pre_upload_next(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    entry_data: dict[str, Any],
) -> None:
    """Background task: select and pre-upload the next image for fast-path shuffle.

    Runs after each shuffle completes.  Stores the result in
    entry_data["staged_images"][tv_id] for use by the next shuffle.
    """
    tv_config = get_tv_config(entry, tv_id)
    if not tv_config:
        return
    tv_name = tv_config.get("name", tv_id)
    tv_mac = tv_config.get("mac")

    # Abort if a full upload is already running for this TV.  Pre-upload uses the
    # same TVConnectionManager as the full-path shuffle — concurrent WebSocket
    # operations on the same connection cause interleaved responses and failures.
    if tv_id in entry_data.get("upload_in_progress", set()):
        _LOGGER.debug("pre-upload: skipping for %s — upload already in progress", tv_name)
        return

    _LOGGER.debug("pre-upload: starting for %s", tv_name)

    # Read tagsets from cache for fingerprint/tagset resolution.
    # Ensure loaded first: handles startup race where the manager add-on starts
    # after HA and the initial TagsetCache fetch failed silently.
    tagset_cache = entry_data.get("tagset_cache")
    if tagset_cache:
        await tagset_cache.async_ensure_loaded()
    tagsets = (tagset_cache.get_all() or None) if tagset_cache else None

    # Compute current tagset fingerprint
    fingerprint = get_tagset_fingerprint(entry, tv_id, tagsets=tagsets)
    tagset_name = get_active_tagset_name(entry, tv_id, tagsets=tagsets)

    library_path = Path(entry.data.get("metadata_path", "")).parent / "library"

    shuffle_cache = entry_data.setdefault("shuffle_cache", {})
    runtime_state = shuffle_cache.get(tv_id, {})
    current_image = runtime_state.get("current_image")

    manager_url = entry.data.get("frame_art_manager_url", "http://localhost:8099")
    # Resolve active moods for the pre-upload (mirrors _async_shuffle_tv_inner).
    pre_upload_moods = _resolve_active_moods(hass, tv_config)

    try:
        selected_image, _count, selected_tag, _fresh, _fallback = await _async_select_image(
            hass,
            manager_url,
            tagset_name,
            current_image,
            tv_name,
            None,  # No recency filtering for pre-upload
            active_moods=pre_upload_moods,
        )
    except Exception as err:
        _LOGGER.debug("pre-upload: image selection failed for %s: %s", tv_name, err)
        return

    if not selected_image:
        _LOGGER.debug("pre-upload: no eligible image for %s", tv_name)
        return

    staged_images = entry_data.setdefault("staged_images", {})

    try:
        if selected_image.get("_web_sources"):
            # Web source pre-upload
            result = await _async_web_source_send(
                hass, entry, tv_id, tv_name, entry_data,
                select=False,
                virtual_tag_id=selected_image.get("_virtual_tag_id"),
                active_moods=pre_upload_moods,
                mood_keyword=selected_image.get("_mood_keyword"),
            )
            if not result or not result.get("content_id"):
                _LOGGER.debug("pre-upload: web source upload failed for %s", tv_name)
                return

            staged_images[tv_id] = {
                "content_id": result["content_id"],
                "tagset_fingerprint": fingerprint,
                "source_type": "web_source",
                "metadata": result.get("metadata", {}),
                "artwork_metadata": result.get("artwork_metadata", {}),
                "virtual_tag_id": result.get("virtual_tag_id"),
                "selected_tag": selected_tag,
                "matte": None,
                "photo_filter": None,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }
        else:
            # Local library image pre-upload
            image_filename = selected_image["filename"]
            image_path = library_path / image_filename
            if not image_path.exists():
                _LOGGER.debug("pre-upload: image file missing: %s", image_filename)
                return

            image_matte = selected_image.get("matte")
            image_filter = selected_image.get("filter")
            if image_filter and isinstance(image_filter, str) and image_filter.lower() == "none":
                image_filter = None

            client = entry_data.get("art_clients", {}).get(tv_id)
            if client is None:
                _LOGGER.debug("pre-upload: no art client for %s", tv_name)
                return

            content_id = await upload_to_tv_only(
                client,
                str(image_path),
                mac_address=tv_mac,
                matte=image_matte,
            )

            staged_images[tv_id] = {
                "content_id": content_id,
                "tagset_fingerprint": fingerprint,
                "source_type": "local",
                "image_data": selected_image,
                "filename": image_filename,
                "selected_tag": selected_tag,
                "matte": image_matte,
                "photo_filter": image_filter,
                "staged_at": datetime.now(timezone.utc).isoformat(),
            }

        _LOGGER.info(
            "pre-upload: staged next image for %s (content_id=%s)",
            tv_name, staged_images[tv_id]["content_id"],
        )
    except Exception as err:
        _LOGGER.warning("pre-upload: failed for %s: %s", tv_name, err)
        # Non-fatal — next shuffle will use full path


async def async_shuffle_tv(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    *,
    reason: str = "manual",
    skip_if_screen_off: bool = False,
    screen_on: bool = True,
    status_callback: StatusCallback | None = None,
    recent_images: set[str] | None = None,
) -> bool:
    """Shuffle a TV's artwork selection, enforcing the upload guard."""
    def _notify(status: str, message: str) -> None:
        if status_callback:
            status_callback(status, message)

    # Get TV name early for error logging (fallback to tv_id if not available)
    tv_config = get_tv_config(entry, tv_id)
    tv_name = tv_config.get("name", tv_id) if tv_config else tv_id

    try:
        return await _async_shuffle_tv_inner(
            hass, entry, tv_id, tv_config, tv_name, reason, skip_if_screen_off, _notify,
            recent_images, screen_on=screen_on,
        )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error("Shuffle failed for %s: %s", tv_name, err)
        log_activity(
            hass,
            entry.entry_id,
            tv_id,
            "shuffle_failed",
            f"Shuffle failed: {err}",
        )
        _notify("error", f"Shuffle failed: {err}")
        # If the art channel is stale and auto-recovery is opt-in, flag for watchdog.
        if tv_config and tv_config.get("auto_recover_art_channel", False):
            entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            client = entry_data.get("art_clients", {}).get(tv_id)
            if client and client.stale_duration() > 0:
                entry_data.setdefault("recovery_pending", {})[tv_id] = {
                    "pending_since": asyncio.get_event_loop().time(),
                    "reason": reason,
                }
                _LOGGER.info(
                    "Stale art channel flagged for %s (reason=%s) — watchdog will recover",
                    tv_name, reason,
                )
        return False


async def _async_shuffle_tv_inner(
    hass: HomeAssistant,
    entry: Any,
    tv_id: str,
    tv_config: dict[str, Any] | None,
    tv_name: str,
    reason: str,
    skip_if_screen_off: bool,
    _notify: Callable[[str, str], None],
    recent_images: set[str] | None = None,
    *,
    screen_on: bool = True,
) -> bool:
    """Inner implementation of shuffle - exceptions bubble up to caller."""
    if not tv_config:
        raise FrameArtError(f"TV config not found for {tv_id}")

    tv_ip = tv_config.get("ip")
    if not tv_ip:
        raise FrameArtError(f"Missing IP address in config for {tv_name}")
    tv_mac = tv_config.get("mac")

    library_path = Path(entry.data.get("metadata_path", "")).parent / "library"
    manager_url = entry.data.get("frame_art_manager_url", "http://localhost:8099")

    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})

    # Read tagsets from cache for tagset resolution / fingerprinting.
    # Ensure loaded first: handles startup race where the manager add-on starts
    # after HA and the initial TagsetCache fetch failed silently.
    tagset_cache = entry_data.get("tagset_cache")
    if tagset_cache:
        await tagset_cache.async_ensure_loaded()
    tagsets = (tagset_cache.get_all() or None) if tagset_cache else None
    tagset_name = get_active_tagset_name(entry, tv_id, tagsets=tagsets)
    include_tags, _ = get_effective_tags(entry, tv_id, tagsets=tagsets)

    # Resolve active moods for this TV (sensor + service overrides)
    active_moods = _resolve_active_moods(hass, tv_config)
    if active_moods:
        _LOGGER.debug("Active moods for %s: %s", tv_name, active_moods)

    shuffle_cache = entry_data.setdefault("shuffle_cache", {})
    runtime_state = shuffle_cache.get(tv_id, {})
    current_image = runtime_state.get("current_image") or tv_config.get("current_image")
    tv_name = tv_config.get("name", tv_id)

    # --- Fast path: use pre-uploaded staged image ---
    staged_images = entry_data.get("staged_images", {})
    staged = staged_images.get(tv_id)
    if staged:
        current_fingerprint = get_tagset_fingerprint(entry, tv_id, tagsets=tagsets)
        if staged.get("tagset_fingerprint") == current_fingerprint:
            _LOGGER.info(
                "Fast-path shuffle for %s: using staged content_id=%s",
                tv_name, staged["content_id"],
            )
            fast_ok = await _async_fast_path_shuffle(
                hass, entry, tv_id, tv_name, staged, entry_data,
                _notify, screen_on=screen_on, reason=reason,
                recent_images=recent_images,
            )
            if fast_ok:
                # Clear staged image and kick off background pre-upload for N+2
                staged_images.pop(tv_id, None)
                hass.async_create_task(
                    _async_pre_upload_next(hass, entry, tv_id, entry_data),
                    f"pre-upload-{tv_id}",
                )
                return True
            else:
                # Fast path failed (e.g. content_id gone) — fall through to full path
                _LOGGER.info("Fast-path failed for %s, falling through to full path", tv_name)
                staged_images.pop(tv_id, None)
        else:
            _LOGGER.debug(
                "Staged image for %s has stale fingerprint (staged=%s, current=%s)",
                tv_name, staged.get("tagset_fingerprint"), current_fingerprint,
            )
            staged_images.pop(tv_id, None)

    if skip_if_screen_off:
        status_cache = entry_data.get("tv_status_cache", {})
        screen_state = status_cache.get(tv_id, {}).get("screen_on")
        if screen_state is not True:
            if screen_state is False:
                message = "Shuffle skipped: screen is off"
            else:
                message = "Shuffle skipped: screen state unknown"
            log_activity(
                hass,
                entry.entry_id,
                tv_id,
                "shuffle_skipped",
                message,
            )
            _notify("skipped", message)
            return False

    selected_image, matching_count, selected_tag, fresh_count, used_fallback = await _async_select_image(
        hass,
        manager_url,
        tagset_name,
        current_image,
        tv_name,
        recent_images,
        active_moods=active_moods,
    )

    if not selected_image:
        # No eligible images - this is not an error, just nothing to do
        return False

    # Web sources sentinel — call add-on API instead of uploading a library image
    if selected_image.get("_web_sources"):
        ws_result = await _async_web_source_send(
            hass, entry, tv_id, tv_name, entry_data,
            select=True,
            screen_on=screen_on,
            matching_count=matching_count,
            selected_tag=selected_tag,
            _notify=_notify,
            virtual_tag_id=selected_image.get("_virtual_tag_id"),
            active_moods=active_moods,
            mood_keyword=selected_image.get("_mood_keyword"),
        )
        if ws_result:
            hass.async_create_task(
                _async_pre_upload_next(hass, entry, tv_id, entry_data),
                f"pre-upload-{tv_id}",
            )
            entry_data.get("pending_reshuffles", set()).discard(tv_id)
        return ws_result

    image_filename = selected_image["filename"]
    image_path = library_path / image_filename
    if not image_path.exists():
        raise FrameArtError(f"Image file missing: {image_filename}")

    image_matte = selected_image.get("matte")
    image_filter = selected_image.get("filter")
    if image_filter and isinstance(image_filter, str) and image_filter.lower() == "none":
        image_filter = None

    async def _perform_upload() -> bool:
        client = entry_data.get("art_clients", {}).get(tv_id)
        if client is None:
            raise FrameArtError(f"No art client found for TV {tv_id}")

        content_id = await set_art_on_tv_deleteothers(
            client,
            str(image_path),
            delete_others=True,
            matte=image_matte,
            photo_filter=image_filter,
            mac_address=tv_mac,
            screen_on=screen_on,
        )

        # Update artwork info sensor
        artwork_sensor = entry_data.get("artwork_sensors", {}).get(tv_id)
        if artwork_sensor and content_id:
            sensor_meta = await _build_local_sensor_meta(
                entry_data, image_filename, list(selected_image.get("tags", []))
            )
            _LOGGER.debug(
                "Artwork sensor update [%s] content_id=%s source=local filename=%r",
                tv_id, content_id[:8], image_filename,
            )
            artwork_sensor.set_artwork(content_id, sensor_meta, source_type="local")
            artwork_sensor.async_write_ha_state()
            log_path = Path(hass.config.path("frame_art/logs/artwork_sensor.log"))
            await hass.async_add_executor_job(
                _write_artwork_sensor_log, log_path, tv_id, content_id, "local", sensor_meta
            )

        now = datetime.now(timezone.utc)
        timestamp = now.isoformat()
        shuffle_cache[tv_id] = {
            "current_image": image_filename,
            "current_matte": image_matte,
            "current_filter": image_filter,
            "matching_image_count": matching_count,
            "last_shuffle_timestamp": timestamp,
            "selected_tag": selected_tag,
        }

        # Build activity message with recency and tag info
        if selected_tag:
            if used_fallback:
                activity_msg = f"Shuffled to {image_filename} (tag: {selected_tag}, all in tag were recent)"
            elif fresh_count > 0:
                activity_msg = f"Shuffled to {image_filename} (tag: {selected_tag}, from {fresh_count} fresh in tag)"
            else:
                activity_msg = f"Shuffled to {image_filename} (tag: {selected_tag})"
        else:
            if used_fallback:
                activity_msg = f"Shuffled to {image_filename} (all {matching_count} eligible were recent, picked randomly)"
            elif fresh_count > 0:
                activity_msg = f"Shuffled to {image_filename} (from {fresh_count} fresh of {matching_count} eligible)"
            else:
                activity_msg = f"Shuffled to {image_filename}"

        log_activity(
            hass,
            entry.entry_id,
            tv_id,
            "shuffle",
            activity_msg,
        )

        signal = f"{DOMAIN}_shuffle_{entry.entry_id}_{tv_id}"
        async_dispatcher_send(hass, signal)

        if coordinator := entry_data.get("coordinator"):
            await coordinator.async_set_active_image(tv_id, image_filename, is_shuffle=True)

        display_log = entry_data.get("display_log")
        if display_log:
            # Pass pool stats for sparkline history (only for auto-shuffle with recency enabled)
            # Skip recording when recent_images is None (e.g., during tagset overrides)
            pool_size_arg = matching_count if reason == "auto" and recent_images is not None else None
            pool_available_arg = fresh_count if reason == "auto" and recent_images is not None else None
            display_log.note_display_start(
                tv_id=tv_id,
                tv_name=tv_name,
                filename=image_filename,
                tags=list(selected_image.get("tags", [])),
                source="shuffle",
                shuffle_mode=reason,
                started_at=now,
                tv_tags=include_tags if include_tags else None,
                matte=image_matte,
                photo_filter=image_filter,
                tagset_name=tagset_name,
                pool_size=pool_size_arg,
                pool_available=pool_available_arg,
            )

        _notify("success", f"Shuffled to {image_filename}")
        
        # Sync brightness after shuffle to ensure TV has correct brightness
        # This helps recover from cases where brightness was set but TV didn't apply it.
        # Skip when screen_on=False: the TV screen is off and a new WebSocket connection
        # for brightness could wake it.
        if screen_on:
            async_sync_brightness = entry_data.get("async_sync_brightness_after_shuffle")
            if async_sync_brightness:
                try:
                    await async_sync_brightness(tv_id)
                except Exception as err:
                    # Don't fail the shuffle if brightness sync fails - it's logged separately
                    _LOGGER.warning(f"Post-shuffle brightness sync failed for {tv_name}: {err}")

        # Clear any pending-reshuffle flag — this shuffle counts as the retry.
        entry_data.get("pending_reshuffles", set()).discard(tv_id)
        return True

    def _on_skip() -> None:
        log_activity(
            hass,
            entry.entry_id,
            tv_id,
            "shuffle_skipped",
            "Shuffle skipped: upload already running",
        )
        _notify("skipped", "Another upload already running")

    max_attempts = 2
    retry_delay_seconds = 60

    for attempt in range(1, max_attempts + 1):
        try:
            result = await async_guarded_upload(
                hass,
                entry,
                tv_id,
                "shuffle",
                _perform_upload,
                _on_skip,
            )
            if result:
                # Full-path succeeded — kick off background pre-upload for N+1
                hass.async_create_task(
                    _async_pre_upload_next(hass, entry, tv_id, entry_data),
                    f"pre-upload-{tv_id}",
                )
            return bool(result)
        except (FrameArtError, Exception) as err:  # pylint: disable=broad-except
            if attempt < max_attempts:
                _LOGGER.warning(
                    "Shuffle attempt %d/%d failed for %s to %s: %s. Retrying in %ds...",
                    attempt,
                    max_attempts,
                    image_filename,
                    tv_name,
                    err,
                    retry_delay_seconds,
                )
                await asyncio.sleep(retry_delay_seconds)
            else:
                # Re-raise so outer handler logs it
                raise FrameArtError(
                    f"Upload failed for {image_filename} after {max_attempts} attempts: {err}"
                ) from err

    return False
