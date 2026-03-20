"""Tests for shuffle.py — TagsetCache startup race, web source routing, fast path.

Key scenarios covered:

1. TagsetCache startup race (regression for 2026-03-20 bug):
   After a power loss + HA restart, the manager add-on may start after HA,
   leaving TagsetCache._ever_loaded=False.  If a shuffle or pre-upload fires
   before the coordinator runs async_ensure_loaded(), get_active_tagset_name()
   receives an empty tagsets dict and returns None.  The manager then receives
   tagsetName=null and returns a random local library image instead of the
   configured web source virtual tag.

   Fix: both _async_pre_upload_next and _async_shuffle_tv_inner call
   tagset_cache.async_ensure_loaded() before reading tagsets.

2. Web source routing:
   When the manager returns type=web_source, the shuffle must call
   _async_web_source_send (not the local library path), both for full-path
   shuffles and for the pre-upload pipeline.

3. Fast-path fingerprint validation:
   A staged image whose tagset_fingerprint matches the current fingerprint is
   used immediately (fast path).  A stale fingerprint causes the staged image
   to be discarded and the full path to run.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from custom_components.frame_art_shuffler.const import DOMAIN


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_TAGSETS = {
    "google-wallpaper": {
        "tags": ["ws:google-wallpaper"],
        "exclude_tags": [],
        "weighting_type": "image",
        "tag_weights": {},
    }
}

_WEB_SOURCE_SELECT_RESULT = (
    {"_web_sources": True, "_virtual_tag_id": "google-wallpaper"},
    1,               # eligibleCount
    "ws:google-wallpaper",  # selectedTag
    0,               # freshCount
    False,           # usedFallback
)

_WEB_SOURCE_SEND_RESULT = {
    "content_id": "MY_F0001",
    "virtual_tag_id": "google-wallpaper",
    "metadata": {"source": "google_art_wallpaper"},
    "artwork_metadata": {"title": "Test Wallpaper"},
}

_LOCAL_SELECT_RESULT = (
    {
        "filename": "nighthawks-1942-f5ff1784.jpg",
        "tags": ["night"],
        "matte": "none",
        "filter": "None",
    },
    14,     # eligibleCount
    "night",
    0,
    False,
)


class _LazyTagsetCache:
    """Simulates TagsetCache that hasn't been loaded yet.

    get_all() returns {} until async_ensure_loaded() is called, mimicking the
    startup race where the manager add-on starts after HA.
    """

    def __init__(self, tagsets_to_load: dict):
        self._tagsets: dict = {}
        self._tagsets_to_load = tagsets_to_load
        self._ever_loaded = False
        self.ensure_loaded_call_count = 0

    async def async_ensure_loaded(self) -> None:
        self.ensure_loaded_call_count += 1
        self._tagsets = self._tagsets_to_load
        self._ever_loaded = True

    def get_all(self) -> dict:
        return self._tagsets


class _LoadedTagsetCache:
    """Simulates TagsetCache that is already populated."""

    def __init__(self, tagsets: dict):
        self._tagsets = tagsets
        self._ever_loaded = True
        self.ensure_loaded_call_count = 0

    async def async_ensure_loaded(self) -> None:
        self.ensure_loaded_call_count += 1
        # Already loaded — no-op (mirrors real behavior)

    def get_all(self) -> dict:
        return self._tagsets


def _make_entry(selected_tagset: str = "google-wallpaper") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.data = {
        "tvs": {
            "tv1": {
                "id": "tv1",
                "name": "Test TV",
                "ip": "192.168.1.1",
                "mac": "aa:bb:cc:dd:ee:ff",
                "selected_tagset": selected_tagset,
            }
        },
        "tagsets": {},   # empty — tagsets live in manager after migration
        "frame_art_manager_url": "http://mock-manager:8099",
        "metadata_path": "/fake/gallery.json",
    }
    return entry


def _make_hass(entry_id: str = "entry123", entry_data: dict | None = None) -> MagicMock:
    hass = MagicMock()
    hass.config.path = MagicMock(return_value="/tmp/test_shuffle.log")
    hass.async_add_executor_job = AsyncMock(return_value=None)
    hass.async_create_task = MagicMock()
    hass.data = {
        DOMAIN: {entry_id: entry_data or {}}
    }
    return hass


# ---------------------------------------------------------------------------
# 1. TagsetCache startup race regression tests
# ---------------------------------------------------------------------------

class TestTagsetCacheStartupRace:
    """Regression tests for the bug where TagsetCache was not loaded before
    tagset resolution, causing tagset_name=None and local art being shuffled
    instead of the configured web source virtual tag."""

    def test_pre_upload_calls_ensure_loaded_when_cache_empty(self):
        """_async_pre_upload_next calls async_ensure_loaded() before selecting.

        When the cache starts empty (_ever_loaded=False), ensure_loaded must be
        called so that get_active_tagset_name() receives the populated tagsets
        dict and returns the correct tagset name instead of None.
        """
        from custom_components.frame_art_shuffler.shuffle import _async_pre_upload_next

        tagset_cache = _LazyTagsetCache(_TAGSETS)
        entry = _make_entry()
        hass = _make_hass()

        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
        }

        captured_tagset_name = []

        async def _capture_select(hass, manager_url, tagset_name, *args, **kwargs):
            captured_tagset_name.append(tagset_name)
            return (None, 0, None, 0, False)  # no eligible image — clean exit

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.shuffle._async_select_image",
                side_effect=_capture_select,
            ):
                await _async_pre_upload_next(hass, entry, "tv1", entry_data)

        asyncio.run(_run())

        assert tagset_cache.ensure_loaded_call_count == 1, (
            "async_ensure_loaded() must be called exactly once before image selection"
        )
        assert captured_tagset_name == ["google-wallpaper"], (
            f"Expected tagset_name='google-wallpaper', got {captured_tagset_name!r}. "
            "If None, TagsetCache was not loaded before get_active_tagset_name() ran."
        )

    def test_pre_upload_without_fix_would_send_null_tagset(self):
        """Documents the pre-fix behavior: empty cache → tagset_name=None → local art.

        This test shows what WOULD happen without async_ensure_loaded() — the
        cache stays empty, get_active_tagset_name() returns None, and the manager
        receives tagsetName=null.
        """
        # Simulate: cache exists but never loaded (no async_ensure_loaded call)
        class _NeverLoadedCache:
            def get_all(self):
                return {}  # always empty
            async def async_ensure_loaded(self):
                pass  # no-op (does not populate)
            _ever_loaded = False

        from custom_components.frame_art_shuffler.config_entry import get_active_tagset_name

        entry = _make_entry(selected_tagset="google-wallpaper")
        # entry.data["tagsets"] is {} — mirrors post-migration state

        # With empty tagsets from an unloaded cache, get_active_tagset_name returns None
        tagsets = _NeverLoadedCache().get_all()   # {}
        tagsets_or_none = tagsets or None         # mirrors shuffle.py: `get_all() or None`
        name = get_active_tagset_name(entry, "tv1", tagsets=tagsets_or_none)
        assert name is None, (
            "Pre-fix: empty tagsets + no async_ensure_loaded → tagset_name=None → "
            "manager receives tagsetName=null → local library art selected"
        )

    def test_shuffle_calls_ensure_loaded_when_cache_empty(self):
        """_async_shuffle_tv_inner calls async_ensure_loaded() before selecting.

        Same startup-race guard as pre-upload, applied to the full shuffle path.
        """
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LazyTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        captured_tagset_name = []

        async def _capture_select(hass, manager_url, tagset_name, *args, **kwargs):
            captured_tagset_name.append(tagset_name)
            return (None, 0, None, 0, False)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    side_effect=_capture_select,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert tagset_cache.ensure_loaded_call_count == 1, (
            "async_ensure_loaded() must be called once in _async_shuffle_tv_inner"
        )
        assert captured_tagset_name == ["google-wallpaper"], (
            f"Expected tagset_name='google-wallpaper', got {captured_tagset_name!r}"
        )


# ---------------------------------------------------------------------------
# 2. Pre-upload pipeline tests
# ---------------------------------------------------------------------------

class TestPreUploadPipeline:
    """Tests for _async_pre_upload_next behavior."""

    def test_pre_upload_stages_web_source_image(self):
        """When tagset resolves to a web source virtual tag, staged image is web_source."""
        from custom_components.frame_art_shuffler.shuffle import _async_pre_upload_next

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        hass = _make_hass()

        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
        }

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_WEB_SOURCE_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=AsyncMock(return_value=_WEB_SOURCE_SEND_RESULT),
                ),
            ):
                await _async_pre_upload_next(hass, entry, "tv1", entry_data)

        asyncio.run(_run())

        staged = entry_data["staged_images"].get("tv1")
        assert staged is not None, "A staged image should have been stored"
        assert staged["source_type"] == "web_source"
        assert staged["content_id"] == "MY_F0001"
        assert staged["virtual_tag_id"] == "google-wallpaper"
        assert "tagset_fingerprint" in staged

    def test_pre_upload_web_source_send_called_with_select_false(self):
        """Pre-upload calls _async_web_source_send with select=False (upload only)."""
        from custom_components.frame_art_shuffler.shuffle import _async_pre_upload_next

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        hass = _make_hass()
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
        }

        mock_send = AsyncMock(return_value=_WEB_SOURCE_SEND_RESULT)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_WEB_SOURCE_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=mock_send,
                ),
            ):
                await _async_pre_upload_next(hass, entry, "tv1", entry_data)

        asyncio.run(_run())

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs.get("select") is False, (
            "Pre-upload must use select=False (upload-only, not select-as-current)"
        )
        assert kwargs.get("virtual_tag_id") == "google-wallpaper"

    def test_pre_upload_skips_if_no_eligible_image(self):
        """Pre-upload does nothing when the manager returns no eligible image."""
        from custom_components.frame_art_shuffler.shuffle import _async_pre_upload_next

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        hass = _make_hass()
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
        }

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.shuffle._async_select_image",
                new=AsyncMock(return_value=(None, 0, None, 0, False)),
            ):
                await _async_pre_upload_next(hass, entry, "tv1", entry_data)

        asyncio.run(_run())

        assert "tv1" not in entry_data["staged_images"], (
            "No staged image should be stored when the manager returns no eligible image"
        )


# ---------------------------------------------------------------------------
# 3. Full shuffle path — web source routing
# ---------------------------------------------------------------------------

class TestShuffleWebSourceRouting:
    """Tests for _async_shuffle_tv_inner web source routing."""

    def test_full_path_routes_to_web_source_send(self):
        """Full shuffle path calls _async_web_source_send when manager returns web_source."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_send = AsyncMock(return_value=_WEB_SOURCE_SEND_RESULT)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_WEB_SOURCE_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=mock_send,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_pre_upload_next",
                    new=AsyncMock(),
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert result, "Shuffle should succeed (ws_result is truthy)"
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs.get("select") is True, (
            "Full-path shuffle must use select=True (upload + select as current)"
        )
        assert kwargs.get("virtual_tag_id") == "google-wallpaper"

    def test_full_path_does_not_call_select_and_cleanup_for_web_source(self):
        """Web source shuffle must NOT call select_and_cleanup (local path)."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_select_and_cleanup = AsyncMock(return_value=True)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_WEB_SOURCE_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=AsyncMock(return_value=_WEB_SOURCE_SEND_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=mock_select_and_cleanup,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_pre_upload_next",
                    new=AsyncMock(),
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        asyncio.run(_run())

        mock_select_and_cleanup.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Fast-path fingerprint tests
# ---------------------------------------------------------------------------

class TestFastPathFingerprint:
    """Tests for staged-image fingerprint validation in the fast path."""

    def _compute_fingerprint(self, entry, tv_id, tagsets):
        from custom_components.frame_art_shuffler.config_entry import get_tagset_fingerprint
        return get_tagset_fingerprint(entry, tv_id, tagsets=tagsets)

    def test_fast_path_used_when_fingerprint_matches(self):
        """A staged image with a matching fingerprint triggers the fast path."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]

        # Compute the fingerprint that the shuffle will also compute
        fp = self._compute_fingerprint(entry, "tv1", tagsets=_TAGSETS)

        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {
                "tv1": {
                    "content_id": "MY_F0001",
                    "source_type": "web_source",
                    "metadata": {},
                    "artwork_metadata": {},
                    "tagset_fingerprint": fp,
                    "virtual_tag_id": "google-wallpaper",
                    "selected_tag": "ws:google-wallpaper",
                }
            },
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_select_image = AsyncMock(return_value=_WEB_SOURCE_SELECT_RESULT)
        mock_fast_path = AsyncMock(return_value=True)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=mock_select_image,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_fast_path_shuffle",
                    new=mock_fast_path,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_pre_upload_next",
                    new=AsyncMock(),
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert result is True
        mock_fast_path.assert_called_once(), "Fast path should be taken for matching fingerprint"
        mock_select_image.assert_not_called(), "_async_select_image should not run on fast path"

    def test_fast_path_discards_stale_fingerprint(self):
        """A staged image whose fingerprint no longer matches is discarded."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]

        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {
                "tv1": {
                    "content_id": "MY_F0001",
                    "source_type": "web_source",
                    "metadata": {},
                    "artwork_metadata": {},
                    "tagset_fingerprint": "stale_fp_000000",  # wrong fingerprint
                    "virtual_tag_id": "google-wallpaper",
                    "selected_tag": "ws:google-wallpaper",
                }
            },
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_select_image = AsyncMock(return_value=(None, 0, None, 0, False))
        mock_fast_path = AsyncMock(return_value=True)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=mock_select_image,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_fast_path_shuffle",
                    new=mock_fast_path,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        asyncio.run(_run())

        mock_fast_path.assert_not_called(), "Stale fingerprint must discard staged image"
        mock_select_image.assert_called_once(), "Full path must run after stale staged image discarded"
        assert "tv1" not in entry_data["staged_images"], (
            "Stale staged image must be removed from staged_images"
        )

    def test_fast_path_fingerprint_changes_when_tagset_changes(self):
        """Changing the active tagset produces a different fingerprint.

        This is what causes a pre-uploaded image staged for tagset A to be
        discarded when the TV is switched to tagset B.
        """
        from custom_components.frame_art_shuffler.config_entry import get_tagset_fingerprint

        tagsets = {
            "google-wallpaper": {
                "tags": ["ws:google-wallpaper"],
                "exclude_tags": [],
                "weighting_type": "image",
                "tag_weights": {},
            },
            "night": {
                "tags": ["night"],
                "exclude_tags": [],
                "weighting_type": "image",
                "tag_weights": {},
            },
        }

        entry_a = _make_entry(selected_tagset="google-wallpaper")
        entry_b = _make_entry(selected_tagset="night")

        fp_a = get_tagset_fingerprint(entry_a, "tv1", tagsets=tagsets)
        fp_b = get_tagset_fingerprint(entry_b, "tv1", tagsets=tagsets)

        assert fp_a != fp_b, (
            "Different active tagsets must produce different fingerprints so "
            "staged images are invalidated on tagset switch"
        )


# ---------------------------------------------------------------------------
# 5. Local library full shuffle path
# ---------------------------------------------------------------------------

class TestLocalLibraryShuffle:
    """Tests for the full shuffle path when the manager returns a local library image."""

    def test_full_path_local_calls_guarded_upload(self):
        """Local library shuffle calls async_guarded_upload (not web source path)."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_guarded = AsyncMock(return_value=True)
        mock_ws_send = AsyncMock(return_value=_WEB_SOURCE_SEND_RESULT)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_LOCAL_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=mock_ws_send,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_guarded_upload",
                    new=mock_guarded,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_pre_upload_next",
                    new=AsyncMock(),
                ),
                patch("pathlib.Path.exists", return_value=True),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert result is True
        mock_guarded.assert_called_once()
        mock_ws_send.assert_not_called()

    def test_full_path_local_does_not_call_web_source_send(self):
        """Local library shuffle must never call _async_web_source_send."""
        from custom_components.frame_art_shuffler.shuffle import _async_shuffle_tv_inner

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry()
        tv_config = entry.data["tvs"]["tv1"]
        entry_data = {
            "tagset_cache": tagset_cache,
            "staged_images": {},
            "shuffle_cache": {},
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "tv_status_cache": {},
        }
        hass = _make_hass(entry_data=entry_data)

        mock_ws_send = AsyncMock()

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_LOCAL_SELECT_RESULT),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_web_source_send",
                    new=mock_ws_send,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_guarded_upload",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_pre_upload_next",
                    new=AsyncMock(),
                ),
                patch("pathlib.Path.exists", return_value=True),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_shuffle_tv_inner(
                    hass, entry, "tv1", tv_config, "Test TV",
                    "manual", False, lambda s, m: None,
                )

        asyncio.run(_run())
        mock_ws_send.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Local art fast path
# ---------------------------------------------------------------------------

class TestLocalFastPath:
    """Tests for _async_fast_path_shuffle with source_type='local'."""

    def _make_local_staged(self, fingerprint: str) -> dict:
        return {
            "content_id": "MY_F0042",
            "source_type": "local",
            "filename": "nighthawks-1942-f5ff1784.jpg",
            "image_data": {"tags": ["night"]},
            "matte": "none",
            "photo_filter": None,
            "selected_tag": "night",
            "tagset_fingerprint": fingerprint,
        }

    def test_local_fast_path_calls_select_and_cleanup(self):
        """Fast path for local art calls select_and_cleanup with the staged content_id."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry(selected_tagset="night")
        hass = _make_hass()
        hass.config.path = MagicMock(return_value="/tmp/test_local_fast.log")

        entry_data = {
            "tagset_cache": tagset_cache,
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "shuffle_cache": {},
        }

        staged = self._make_local_staged("fp_local")
        mock_select = AsyncMock(return_value=True)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=mock_select,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV", staged, entry_data,
                    lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert result is True
        mock_select.assert_called_once()
        args, kwargs = mock_select.call_args
        assert args[0] is entry_data["art_clients"]["tv1"]
        assert args[1] == "MY_F0042"

    def test_local_fast_path_updates_shuffle_cache(self):
        """Fast path for local art sets current_image in shuffle_cache."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry(selected_tagset="night")
        hass = _make_hass()
        hass.config.path = MagicMock(return_value="/tmp/test_local_fast.log")

        entry_data = {
            "tagset_cache": tagset_cache,
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "shuffle_cache": {},
        }
        staged = self._make_local_staged("fp_local")

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=AsyncMock(return_value=True),
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV", staged, entry_data,
                    lambda s, m: None,
                )

        asyncio.run(_run())

        cache = entry_data["shuffle_cache"].get("tv1", {})
        assert cache.get("current_image") == "nighthawks-1942-f5ff1784.jpg"
        assert cache.get("selected_tag") == "night"

    def test_local_fast_path_updates_artwork_sensor(self):
        """Fast path for local art calls set_artwork with source_type='local'."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry(selected_tagset="night")
        hass = _make_hass()
        hass.config.path = MagicMock(return_value="/tmp/test_local_fast.log")

        artwork_sensor = MagicMock()
        entry_data = {
            "tagset_cache": tagset_cache,
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": artwork_sensor},
            "shuffle_cache": {},
        }
        staged = self._make_local_staged("fp_local")

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=AsyncMock(return_value=True),
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV", staged, entry_data,
                    lambda s, m: None,
                )

        asyncio.run(_run())

        artwork_sensor.set_artwork.assert_called_once()
        _, kwargs = artwork_sensor.set_artwork.call_args
        assert kwargs.get("source_type") == "local"

    def test_local_fast_path_returns_false_when_select_fails(self):
        """Fast path returns False (and doesn't update sensors) when select_and_cleanup fails."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        tagset_cache = _LoadedTagsetCache(_TAGSETS)
        entry = _make_entry(selected_tagset="night")
        hass = _make_hass()

        artwork_sensor = MagicMock()
        entry_data = {
            "tagset_cache": tagset_cache,
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": artwork_sensor},
            "shuffle_cache": {},
        }
        staged = self._make_local_staged("fp_local")

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=AsyncMock(return_value=False),
                ),
            ):
                return await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV", staged, entry_data,
                    lambda s, m: None,
                )

        result = asyncio.run(_run())

        assert result is False
        artwork_sensor.set_artwork.assert_not_called()
