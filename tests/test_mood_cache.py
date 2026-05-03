"""Tests for MoodCache — loading contract, retry behavior, no-op guarantee.

MoodCache follows the same pattern as TagsetCache: async_ensure_loaded() is safe
to call on every shuffle because it is a no-op once loaded. Tests here document
that contract explicitly and verify the same error-recovery paths.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.frame_art_shuffler.mood_cache import MoodCache


_MOODS = {
    "night": {
        "id": "night",
        "label": "Nighttime",
        "boost_tags": ["night", "nocturne"],
        "suppress_tags": ["sunny"],
        "suppress_mode": "penalize",
        "search_terms": ["night"],
        "search_compose": True,
        "reject_terms": [],
        "filters": [],
        "strength": 1.0,
        "exclusive": False,
    },
    "christmas": {
        "id": "christmas",
        "label": "Christmas",
        "boost_tags": ["christmas", "nativity"],
        "suppress_tags": ["halloween"],
        "suppress_mode": "exclude",
        "search_terms": ["christmas"],
        "search_compose": False,
        "reject_terms": ["halloween"],
        "filters": [],
        "strength": 3.0,
        "exclusive": True,
    },
}


def _make_mock_session(status: int = 200, body: dict | None = None):
    """Return a mock aiohttp session whose GET returns the given response."""
    if body is None:
        body = {"moods": _MOODS}

    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=body)

    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_cm)
    return mock_session


class TestMoodCacheInitialState:
    """Verify the cache starts empty and unloaded."""

    def test_get_all_returns_empty_before_load(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")
        assert cache.get_all() == {}

    def test_get_returns_none_before_load(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")
        assert cache.get("night") is None

    def test_ever_loaded_false_before_load(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")
        assert cache._ever_loaded is False


class TestMoodCacheRefresh:
    """Tests for async_refresh — HTTP fetch and cache population."""

    def test_refresh_populates_cache_on_200(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                return await cache.async_refresh()

        result = asyncio.run(_run())

        assert result is True
        assert cache._ever_loaded is True
        assert cache.get_all() == _MOODS
        assert cache.get("night") == _MOODS["night"]
        assert cache.get("christmas") == _MOODS["christmas"]

    def test_refresh_returns_false_on_non_200(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                return await cache.async_refresh()

        result = asyncio.run(_run())

        assert result is False
        assert cache._ever_loaded is False
        assert cache.get_all() == {}

    def test_refresh_returns_false_on_network_error(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")

        mock_session = MagicMock()
        mock_session.get = MagicMock(side_effect=Exception("connection refused"))

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
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
        cache = MoodCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                await cache.async_refresh()

            assert cache._ever_loaded is False

            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                await cache.async_refresh()

        asyncio.run(_run())

        assert cache._ever_loaded is True
        assert cache.get_all() == _MOODS

    def test_get_unknown_mood_returns_none(self):
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                await cache.async_refresh()

        asyncio.run(_run())

        assert cache.get("nonexistent_mood") is None


class TestMoodCacheEnsureLoaded:
    """Tests for the async_ensure_loaded no-op contract.

    This contract is what makes it safe to call async_ensure_loaded() on
    every shuffle: after the first successful load, all subsequent calls
    are synchronous no-ops with no HTTP round-trip.
    """

    def test_ensure_loaded_fetches_when_never_loaded(self):
        """ensure_loaded triggers a fetch when _ever_loaded is False."""
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")
        mock_session = _make_mock_session(200)

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=mock_session,
            ):
                await cache.async_ensure_loaded()

        asyncio.run(_run())

        mock_session.get.assert_called_once()
        assert cache._ever_loaded is True
        assert cache.get_all() == _MOODS

    def test_ensure_loaded_is_noop_after_first_load(self):
        """ensure_loaded does NOT re-fetch after the cache is already loaded.

        Core contract: safe to call on every shuffle without extra HTTP
        round-trips once the initial load succeeds.
        """
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")
        mock_session = _make_mock_session(200)

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=mock_session,
            ):
                await cache.async_ensure_loaded()  # First call: fetches
                await cache.async_ensure_loaded()  # Second call: no-op
                await cache.async_ensure_loaded()  # Third call: no-op

        asyncio.run(_run())

        assert mock_session.get.call_count == 1, (
            "async_ensure_loaded must be a no-op after the first successful load"
        )

    def test_ensure_loaded_retries_after_previous_failure(self):
        """ensure_loaded retries if a prior refresh failed.

        Handles the startup race: manager starts after HA, initial refresh
        fails, but every subsequent shuffle gets another try until it succeeds.
        """
        hass = MagicMock()
        cache = MoodCache(hass, "http://mock:8099")

        async def _run():
            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(500),
            ):
                await cache.async_ensure_loaded()
            assert cache._ever_loaded is False

            with patch(
                "custom_components.frame_art_shuffler.mood_cache.async_get_clientsession",
                return_value=_make_mock_session(200),
            ):
                await cache.async_ensure_loaded()

        asyncio.run(_run())

        assert cache._ever_loaded is True
        assert cache.get_all() == _MOODS
