"""Tests for artwork sensor entity_picture contract and fast-path web source shuffle.

Regression tests for two related bugs:
  1. entity_picture is one image behind: set_artwork() clears _artwork_attrs
     (including cache_file), so web-source callers must call set_cache_file()
     AFTER set_artwork().
  2. Fast-path shuffle never called set_cache_file(), so entity_picture returned
     None and the HA card showed a stale image after every fast-path shuffle.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.frame_art_shuffler.const import DOMAIN
from custom_components.frame_art_shuffler.sensor import FrameArtArtworkInfoSensor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sensor() -> FrameArtArtworkInfoSensor:
    """Create a minimal FrameArtArtworkInfoSensor for testing."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "entry123"
    entry.data = {"tvs": {"tv1": {"id": "tv1", "name": "Living Room"}}}
    with patch(
        "custom_components.frame_art_shuffler.sensor.get_tv_config",
        return_value={"id": "tv1", "name": "Living Room"},
    ):
        return FrameArtArtworkInfoSensor(hass, entry, "tv1")


# ---------------------------------------------------------------------------
# Sensor entity_picture contract tests
# ---------------------------------------------------------------------------

class TestArtworkSensorEntityPicture:
    """Verify entity_picture URL contract for the Current Artwork sensor."""

    def test_entity_picture_none_before_any_update(self):
        sensor = _make_sensor()
        assert sensor.entity_picture is None

    def test_entity_picture_none_for_web_source_without_cache_file(self):
        """set_artwork() alone returns None for web sources.

        Web source images have no local filename, so entity_picture depends on
        cache_file being set via set_cache_file() after set_artwork().  This test
        documents that contract so a future caller omitting set_cache_file()
        will see a test failure rather than a silent regression.
        """
        sensor = _make_sensor()
        sensor.set_artwork("content-abc", {"title": "Test Art"}, source_type="web_source")
        assert sensor.entity_picture is None

    def test_entity_picture_correct_after_set_artwork_then_set_cache_file(self):
        """set_artwork() + set_cache_file() produces the correct entity_picture URL."""
        sensor = _make_sensor()
        sensor.set_artwork("content-abc", {"title": "Test Art"}, source_type="web_source")
        sensor.set_cache_file("device123abc.jpg")
        assert sensor.entity_picture == "/api/frame_art_shuffler/image/device123abc.jpg"

    def test_set_artwork_clears_previous_cache_file(self):
        """set_artwork() wipes cache_file from _artwork_attrs.

        This is why callers must always call set_cache_file() *after*
        set_artwork(), never before.
        """
        sensor = _make_sensor()
        sensor.set_artwork("content-old", {"title": "Old Art"}, source_type="web_source")
        sensor.set_cache_file("old_device.jpg")
        assert sensor.entity_picture == "/api/frame_art_shuffler/image/old_device.jpg"

        # A subsequent set_artwork() should clear the old cache file.
        sensor.set_artwork("content-new", {"title": "New Art"}, source_type="web_source")
        assert sensor.entity_picture is None  # cache_file was cleared

    def test_entity_picture_uses_filename_for_local_images(self):
        """Local library images use filename attribute, no cache_file needed."""
        sensor = _make_sensor()
        sensor.set_artwork(
            "content-local",
            {"filename": "my_painting.jpg", "title": "Local Art"},
            source_type="local",
        )
        assert sensor.entity_picture == "/api/frame_art_shuffler/image/my_painting.jpg"


# ---------------------------------------------------------------------------
# Fast-path shuffle integration test
# ---------------------------------------------------------------------------

class TestFastPathWebSourceEntityPicture:
    """Verify fast-path shuffle updates entity_picture via the promote response.

    Regression test for the bug where _async_fast_path_shuffle called
    set_artwork() (clearing cache_file) but never called set_cache_file(),
    leaving entity_picture as None after every fast-path web source shuffle.
    """

    def test_fast_path_calls_set_cache_file_with_promote_response(self):
        """Fast-path calls set_cache_file() with the cacheFile from /promote."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        hass.config.path = MagicMock(return_value="/tmp/test_artwork_sensor.log")
        hass.async_add_executor_job = AsyncMock(return_value=None)

        entry = MagicMock()
        entry.entry_id = "entry123"
        entry.data = {
            "tvs": {"tv1": {"id": "tv1", "name": "Test TV", "ip": "192.168.1.1"}},
            "frame_art_manager_url": "http://localhost:8099",
        }

        artwork_sensor = MagicMock()

        entry_data = {
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": artwork_sensor},
            "shuffle_cache": {},
        }

        staged = {
            "content_id": "content-xyz",
            "source_type": "web_source",
            "metadata": {"title": "Web Art", "source": "google_arts"},
            "artwork_metadata": {"title": "Web Art", "creator_name": "Artist"},
            "selected_tag": None,
            "tagset_fingerprint": "fp1",
        }

        promote_response = MagicMock()
        promote_response.json = AsyncMock(
            return_value={"success": True, "cacheFile": "device-abc.jpg"}
        )
        mock_session = MagicMock()
        mock_session.post = AsyncMock(return_value=promote_response)

        mock_device = MagicMock()
        mock_device.id = "device-abc"
        mock_registry = MagicMock()
        mock_registry.async_get_device = MagicMock(return_value=mock_device)

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.dr.async_get",
                    return_value=mock_registry,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_get_clientsession",
                    return_value=mock_session,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_fast_path_shuffle(
                    hass,
                    entry,
                    "tv1",
                    "Test TV",
                    staged,
                    entry_data,
                    lambda status, msg: None,
                )

        result = asyncio.run(_run())

        assert result is True
        # The critical contract: set_cache_file must be called with the value
        # returned by the /promote endpoint so entity_picture stays current.
        artwork_sensor.set_cache_file.assert_called_once_with("device-abc.jpg")
        # set_artwork must also have been called (metadata update).
        artwork_sensor.set_artwork.assert_called_once()

    def test_fast_path_handles_promote_failure_gracefully(self):
        """Fast-path does not crash if the promote call fails."""
        from custom_components.frame_art_shuffler.shuffle import _async_fast_path_shuffle

        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        hass.config.path = MagicMock(return_value="/tmp/test_artwork_sensor.log")
        hass.async_add_executor_job = AsyncMock(return_value=None)

        entry = MagicMock()
        entry.entry_id = "entry123"
        entry.data = {
            "tvs": {"tv1": {"id": "tv1", "name": "Test TV", "ip": "192.168.1.1"}},
            "frame_art_manager_url": "http://localhost:8099",
        }

        artwork_sensor = MagicMock()
        entry_data = {
            "art_clients": {"tv1": MagicMock()},
            "artwork_sensors": {"tv1": artwork_sensor},
            "shuffle_cache": {},
        }
        staged = {
            "content_id": "content-xyz",
            "source_type": "web_source",
            "metadata": {"title": "Web Art", "source": "google_arts"},
            "artwork_metadata": {},
            "selected_tag": None,
            "tagset_fingerprint": "fp1",
        }

        mock_device = MagicMock()
        mock_device.id = "device-abc"
        mock_registry = MagicMock()
        mock_registry.async_get_device = MagicMock(return_value=mock_device)

        # Simulate promote call raising an exception
        mock_session = MagicMock()
        mock_session.post = AsyncMock(side_effect=Exception("network error"))

        async def _run():
            with (
                patch(
                    "custom_components.frame_art_shuffler.shuffle.select_and_cleanup",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.dr.async_get",
                    return_value=mock_registry,
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle.async_get_clientsession",
                    return_value=mock_session,
                ),
                patch("custom_components.frame_art_shuffler.shuffle.log_activity"),
                patch("custom_components.frame_art_shuffler.shuffle.async_dispatcher_send"),
            ):
                return await _async_fast_path_shuffle(
                    hass,
                    entry,
                    "tv1",
                    "Test TV",
                    staged,
                    entry_data,
                    lambda status, msg: None,
                )

        # Should still return True (shuffle succeeded, promote failure is non-fatal)
        result = asyncio.run(_run())
        assert result is True
        # set_artwork should still be called
        artwork_sensor.set_artwork.assert_called_once()
        # set_cache_file should NOT be called when promote failed
        artwork_sensor.set_cache_file.assert_not_called()
