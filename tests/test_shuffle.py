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


# ---------------------------------------------------------------------------
# 6. Mood keyword routing
# ---------------------------------------------------------------------------

_MOOD_KEYWORD_SELECT_RESULT = (
    {"_web_sources": True, "_virtual_tag_id": None, "_mood_keyword": "night snow"},
    1,
    None,       # selectedTag (mood search, no virtual tag)
    0,
    False,
)

_MOOD_KEYWORD_SEND_RESULT = {
    "content_id": "MY_F0002",
    "virtual_tag_id": None,
    "metadata": {"source": "google_arts"},
    "artwork_metadata": {"title": "Snowy Night"},
}


class TestMoodKeywordRouting:
    """Tests for moodKeyword routing through the shuffle pipeline.

    When the manager returns type=web_source with a moodKeyword (from a mood-derived
    composed keyword search winning the pool), the keyword must be forwarded to
    _async_web_source_send so it reaches fetch-and-send for the actual search.
    """

    def test_full_path_passes_mood_keyword_to_web_source_send(self):
        """mood_keyword from the /select response is forwarded to _async_web_source_send."""
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

        mock_send = AsyncMock(return_value=_MOOD_KEYWORD_SEND_RESULT)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    new=AsyncMock(return_value=_MOOD_KEYWORD_SELECT_RESULT),
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

        asyncio.run(_run())

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs.get("mood_keyword") == "night snow", (
            "mood_keyword from the /select response must be forwarded to _async_web_source_send"
        )
        assert kwargs.get("virtual_tag_id") is None, (
            "virtual_tag_id is None for mood keyword searches (no real virtual tag)"
        )

    def test_mood_keyword_select_result_captured_by_select_image(self):
        """_async_select_image correctly captures moodKeyword from the manager response."""
        from custom_components.frame_art_shuffler.shuffle import _async_select_image

        # _async_select_image uses `await session.post(...)` (not context manager),
        # then `await resp.json()`.
        mock_resp = MagicMock()
        mock_resp.json = AsyncMock(return_value={
            "type": "web_source",
            "moodKeyword": "night snow",
            "virtualTagId": None,
            "selectedTag": None,
            "eligibleCount": 1,
            "freshCount": 0,
            "usedFallback": False,
        })

        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=mock_resp)

        hass = MagicMock()

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.shuffle.async_get_clientsession",
                return_value=mock_session,
            ):
                return await _async_select_image(
                    hass, "http://mock:8099", "google-wallpaper",
                    None, "Test TV", [],
                    active_moods=["night", "winter"],
                )

        image_dict, eligible_count, selected_tag, fresh_count, used_fallback = asyncio.run(_run())

        assert image_dict is not None
        assert image_dict.get("_web_sources") is True
        assert image_dict.get("_mood_keyword") == "night snow", (
            "_mood_keyword must be captured from the moodKeyword field in the manager response"
        )
        assert image_dict.get("_virtual_tag_id") is None

    def test_full_path_without_mood_keyword_passes_none(self):
        """When the normal web source path is taken (no moodKeyword), mood_keyword=None."""
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

        asyncio.run(_run())

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        # Normal web source: no mood keyword, virtual_tag_id is the real tag
        assert kwargs.get("mood_keyword") is None
        assert kwargs.get("virtual_tag_id") == "google-wallpaper"


# ---------------------------------------------------------------------------
# Upload guard deadlock regression tests
# ---------------------------------------------------------------------------

class TestUploadGuardDeadlock:
    """Regression tests for the upload guard deadlock (2026-04).

    Bug: _async_web_source_send used to set upload_in_progress *before* calling
    the add-on HTTP endpoint.  The add-on immediately calls back into the HA
    send_image service, which runs via async_guarded_upload.  async_guarded_upload
    saw the flag already set → called on_skip() → HTTP 500 → shuffle broken.

    Fix: _async_web_source_send must NOT touch upload_in_progress.  Only
    async_guarded_upload (invoked inside send_image) sets and clears the flag.
    """

    def test_upload_flag_not_held_during_addon_http_call(self):
        """upload_in_progress must be clear while the add-on HTTP call is in flight.

        Uses an asyncio.Event to pause the mock HTTP response, then probes the
        upload_in_progress set.  If the flag is set during the HTTP call the
        add-on callback into send_image would immediately 500.
        """
        import asyncio

        from custom_components.frame_art_shuffler.shuffle import _async_web_source_send

        entry = _make_entry()
        entry_data: dict = {"upload_in_progress": set()}
        hass = _make_hass(entry_data=entry_data)

        http_started: asyncio.Event
        http_unblock: asyncio.Event
        flag_during_http: list[bool] = []

        async def _run():
            nonlocal http_started, http_unblock
            http_started = asyncio.Event()
            http_unblock = asyncio.Event()

            async def _pausing_post(*args, **kwargs):
                http_started.set()
                await http_unblock.wait()
                mock_resp = AsyncMock()
                mock_resp.json = AsyncMock(return_value={
                    "success": True,
                    "contentId": "MY_F0001",
                    "metadata": {"title": "Test", "source": "google_art_wallpaper"},
                    "artworkMetadata": {"title": "Test"},
                })
                return mock_resp

            mock_session = MagicMock()
            mock_session.post = _pausing_post

            mock_device = MagicMock()
            mock_device.id = "device123"

            async def _probe_then_unblock():
                await http_started.wait()
                flag_during_http.append("tv1" in entry_data.get("upload_in_progress", set()))
                http_unblock.set()

            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.dr.async_get",
                    return_value=MagicMock(
                        async_get_device=MagicMock(return_value=mock_device)
                    ),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_get_clientsession",
                    return_value=mock_session,
                ),
            ):
                await asyncio.gather(
                    _async_web_source_send(
                        hass, entry, "tv1", "Test TV", entry_data, select=False
                    ),
                    _probe_then_unblock(),
                )

        asyncio.run(_run())

        assert flag_during_http == [False], (
            "upload_in_progress must NOT be held during the add-on HTTP call. "
            "If True, the add-on callback into send_image would immediately 500."
        )

    def test_concurrent_guarded_upload_succeeds_during_web_source_send(self):
        """A concurrent async_guarded_upload must complete (not skip) while
        _async_web_source_send is waiting for the add-on HTTP response.

        This is the actual failure mode: the add-on calls back into HA's
        send_image service.  send_image calls async_guarded_upload.  If
        upload_in_progress is already set, on_skip() fires → 500 from service.
        """
        import asyncio

        from custom_components.frame_art_shuffler.shuffle import (
            _async_web_source_send,
            async_guarded_upload,
        )

        entry = _make_entry()
        entry_data: dict = {"upload_in_progress": set()}
        hass = _make_hass(entry_data=entry_data)

        skip_called = False

        async def _run():
            http_started = asyncio.Event()
            http_unblock = asyncio.Event()

            async def _pausing_post(*args, **kwargs):
                http_started.set()
                await http_unblock.wait()
                mock_resp = AsyncMock()
                mock_resp.json = AsyncMock(return_value={
                    "success": True,
                    "contentId": "MY_F0001",
                    "metadata": {"title": "Test", "source": "google_art_wallpaper"},
                    "artworkMetadata": {},
                })
                return mock_resp

            mock_session = MagicMock()
            mock_session.post = _pausing_post
            mock_device = MagicMock()
            mock_device.id = "device123"

            work_ran = []

            async def _addon_callback_work():
                """Simulates the work send_image does inside async_guarded_upload."""
                work_ran.append(True)
                return "ok"

            async def _simulate_addon_callback():
                """Simulates the add-on calling back into send_image while HTTP is in flight."""
                await http_started.wait()

                def _on_skip():
                    nonlocal skip_called
                    skip_called = True

                result = await async_guarded_upload(
                    hass, entry, "tv1", "send_image",
                    _addon_callback_work,
                    on_skip=_on_skip,
                )
                http_unblock.set()
                return result

            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.dr.async_get",
                    return_value=MagicMock(
                        async_get_device=MagicMock(return_value=mock_device)
                    ),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_get_clientsession",
                    return_value=mock_session,
                ),
            ):
                ws_result, callback_result = await asyncio.gather(
                    _async_web_source_send(
                        hass, entry, "tv1", "Test TV", entry_data, select=False
                    ),
                    _simulate_addon_callback(),
                )

            assert not skip_called, (
                "on_skip() must not be called — the add-on callback should run, not be skipped. "
                "If skip_called=True, _async_web_source_send is illegally holding upload_in_progress."
            )
            assert work_ran == [True], (
                "The simulated send_image work must have run (not been skipped)"
            )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Fast-path shuffle concurrency tests
# ---------------------------------------------------------------------------

class TestFastPathConcurrency:
    """Regression tests for concurrent fast-path shuffle calls (2026-04).

    Bug: _async_fast_path_shuffle called select_and_cleanup without holding
    upload_in_progress.  Two rapid button presses launched two concurrent
    select_and_cleanup calls on the same WebSocket, producing interleaved
    responses that confused the art channel.

    Fix: _async_fast_path_shuffle wraps the TV operation in async_guarded_upload.
    The second concurrent call hits the guard, fires on_skip(), and returns False.
    select_and_cleanup is only called once.
    """

    def _make_staged(self) -> dict:
        return {
            "content_id": "MY_F0042",
            "source_type": "web_source",
            "virtual_tag_id": "google-wallpaper",
            "selected_tag": "ws:google-wallpaper",
            "metadata": {"title": "Test Art", "source": "google_arts"},
            "artwork_metadata": {"title": "Test Art"},
            "tagset_fingerprint": "fp_abc123",
        }

    def test_second_concurrent_fast_path_is_skipped(self):
        """A second _async_fast_path_shuffle call while the first is in-flight
        must be skipped (select_and_cleanup called only once).

        Simulates two rapid button presses: the first call pauses inside
        select_and_cleanup; the second call arrives and should hit the
        upload guard and return False without calling select_and_cleanup.
        """
        import asyncio

        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        entry = _make_entry()
        entry_data: dict = {
            "upload_in_progress": set(),
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "shuffle_cache": {},
            "tagset_cache": _LoadedTagsetCache(_TAGSETS),
        }
        hass = _make_hass(entry_data=entry_data)

        select_call_count = []
        skip_called = []

        async def _run():
            first_started = asyncio.Event()
            first_unblock = asyncio.Event()

            async def _pausing_select_and_cleanup(*args, **kwargs):
                select_call_count.append(1)
                first_started.set()
                await first_unblock.wait()
                return True

            async def _first():
                with (
                    patch(
                        "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                        side_effect=_pausing_select_and_cleanup,
                    ),
                    patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                    patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
                    patch("custom_components.frame_art_shuffler.shuffle.dr"),
                    patch("custom_components.frame_art_shuffler.shuffle.async_get_clientsession"),
                ):
                    return await _async_fast_path_shuffle(
                        hass, entry, "tv1", "Test TV",
                        _make_staged_copy(), entry_data,
                        lambda status, msg: None,
                        screen_on=False,
                    )

            async def _second():
                await first_started.wait()

                def _on_skip_recorded():
                    skip_called.append(True)

                with (
                    patch(
                        "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                        side_effect=_pausing_select_and_cleanup,
                    ),
                    patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                    patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
                    patch("custom_components.frame_art_shuffler.shuffle.dr"),
                    patch("custom_components.frame_art_shuffler.shuffle.async_get_clientsession"),
                ):
                    result = await _async_fast_path_shuffle(
                        hass, entry, "tv1", "Test TV",
                        _make_staged_copy(), entry_data,
                        lambda status, msg: None,
                        screen_on=False,
                    )
                first_unblock.set()
                return result

            results = await asyncio.gather(_first(), _second())
            return results

        def _make_staged_copy():
            return dict(self._make_staged())

        results = asyncio.run(_run())

        assert select_call_count == [1], (
            f"select_and_cleanup must be called exactly once, got {len(select_call_count)} calls. "
            "If 2, two concurrent fast-path shuffles are sharing the WebSocket."
        )
        # First call succeeds (or fails — not important here); second must return False
        assert results[1] is False, (
            "The second concurrent fast-path call must return False (skipped by upload guard)"
        )

    def test_sequential_fast_path_calls_both_succeed(self):
        """Sequential (non-concurrent) fast-path calls must both call select_and_cleanup.

        Guards that the fix doesn't accidentally block legitimate sequential calls.
        """
        import asyncio

        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        entry = _make_entry()
        entry_data: dict = {
            "upload_in_progress": set(),
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": MagicMock()},
            "shuffle_cache": {},
            "tagset_cache": _LoadedTagsetCache(_TAGSETS),
        }
        hass = _make_hass(entry_data=entry_data)

        select_call_count = []

        async def _select_ok(*args, **kwargs):
            select_call_count.append(1)
            return True

        def _make_staged_copy():
            return dict({
                "content_id": "MY_F0042",
                "source_type": "web_source",
                "virtual_tag_id": "google-wallpaper",
                "selected_tag": "ws:google-wallpaper",
                "metadata": {"title": "Test Art", "source": "google_arts"},
                "artwork_metadata": {"title": "Test Art"},
                "tagset_fingerprint": "fp_abc123",
            })

        async def _run():
            patches = (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    side_effect=_select_ok,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
                patch("custom_components.frame_art_shuffler.shuffle.dr"),
                patch("custom_components.frame_art_shuffler.shuffle.async_get_clientsession"),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                r1 = await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV",
                    _make_staged_copy(), entry_data,
                    lambda status, msg: None,
                    screen_on=False,
                )
                r2 = await _async_fast_path_shuffle(
                    hass, entry, "tv1", "Test TV",
                    _make_staged_copy(), entry_data,
                    lambda status, msg: None,
                    screen_on=False,
                )
            return r1, r2

        r1, r2 = asyncio.run(_run())

        assert select_call_count == [1, 1], (
            f"Sequential fast-path calls must each invoke select_and_cleanup once, "
            f"got {select_call_count}"
        )
        assert r1 is True
        assert r2 is True
