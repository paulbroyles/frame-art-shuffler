"""Tests for TagsetCache — loading contract, retry behavior, no-op guarantee.

The no-op guarantee is what makes it safe to call async_ensure_loaded() on
every shuffle without extra round-trips: once loaded, subsequent calls are
instant.  Tests here document that contract explicitly.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.frame_art_shuffler.tagset_cache import TagsetCache


_TAGSETS = {
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


def _make_mock_session(status: int = 200, body: dict | None = None):
    """Return a mock aiohttp session whose GET returns the given response."""
    if body is None:
        body = {"tagsets": _TAGSETS}

    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=body)

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)
    return mock_session


class TestTagsetCacheInitialState:
    """Verify the cache starts empty and unloaded."""

    def test_get_all_returns_empty_before_load(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")
        assert cache.get_all() == {}

    def test_get_returns_none_before_load(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")
        assert cache.get("google-wallpaper") is None

    def test_ever_loaded_false_before_load(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")
        assert cache._ever_loaded is False


class TestTagsetCacheRefresh:
    """Tests for async_refresh — HTTP fetch and cache population."""

    def test_refresh_populates_cache_on_200(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                return await cache.async_refresh()

        result = asyncio.run(_run())

        assert result is True
        assert cache._ever_loaded is True
        assert cache.get_all() == _TAGSETS
        assert cache.get("google-wallpaper") == _TAGSETS["google-wallpaper"]

    def test_refresh_returns_false_on_non_200(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                return await cache.async_refresh()

        result = asyncio.run(_run())

        assert result is False
        assert cache._ever_loaded is False
        assert cache.get_all() == {}

    def test_refresh_returns_false_on_network_error(self):
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=Exception("connection refused"))

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=mock_session,
            ):
                return await cache.async_refresh()

        result = asyncio.run(_run())

        assert result is False
        assert cache._ever_loaded is False
        assert cache.get_all() == {}

    def test_refresh_after_failure_succeeds_on_retry(self):
        """Cache recovers: a failed refresh leaves data empty; a subsequent
        successful refresh populates it normally."""
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")

        async def _run():
            # First refresh fails
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                await cache.async_refresh()

            assert cache._ever_loaded is False

            # Second refresh succeeds
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                await cache.async_refresh()

        asyncio.run(_run())

        assert cache._ever_loaded is True
        assert cache.get_all() == _TAGSETS


class TestTagsetCacheEnsureLoaded:
    """Tests for the async_ensure_loaded no-op contract.

    This contract is what makes it safe to call async_ensure_loaded() on
    every shuffle: after the first successful load, all subsequent calls
    are synchronous no-ops with no HTTP round-trip.
    """

    def test_ensure_loaded_fetches_when_never_loaded(self):
        """ensure_loaded triggers a fetch when _ever_loaded is False."""
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")
        mock_session = _make_mock_session(200)

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=mock_session,
            ):
                await cache.async_ensure_loaded()

        asyncio.run(_run())

        # Verify the GET was made and cache is populated
        mock_session.get.assert_called_once()
        assert cache._ever_loaded is True
        assert cache.get_all() == _TAGSETS

    def test_ensure_loaded_is_noop_after_first_load(self):
        """ensure_loaded does NOT re-fetch after the cache is already loaded.

        This is the core contract: safe to call on every shuffle without
        extra HTTP round-trips once the initial load succeeds.
        """
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")
        mock_session = _make_mock_session(200)

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=mock_session,
            ):
                await cache.async_ensure_loaded()  # First call: fetches
                await cache.async_ensure_loaded()  # Second call: no-op
                await cache.async_ensure_loaded()  # Third call: no-op

        asyncio.run(_run())

        # GET must be called exactly once regardless of how many ensure_loaded calls
        assert mock_session.get.call_count == 1, (
            "async_ensure_loaded must be a no-op after the first successful load; "
            "calling it on every shuffle must not cause multiple HTTP requests"
        )

    def test_ensure_loaded_retries_after_previous_failure(self):
        """ensure_loaded retries if a prior refresh failed.

        Handles the startup race: manager starts after HA, initial refresh
        fails, but every subsequent shuffle gets another try until it succeeds.
        """
        hass = MagicMock()
        cache = TagsetCache(hass, "http://mock:8099")

        async def _run():
            # First ensure_loaded: refresh fails (manager not up yet)
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                await cache.async_ensure_loaded()
            assert cache._ever_loaded is False

            # Second ensure_loaded: refresh succeeds (manager now up)
            with patch(
                "custom_components.frame_art_shuffler.tagset_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                await cache.async_ensure_loaded()

        asyncio.run(_run())

        assert cache._ever_loaded is True
        assert cache.get_all() == _TAGSETS
