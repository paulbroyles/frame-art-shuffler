"""In-memory cache for mood definitions from the Frame Art Manager add-on.

Fetches all mood definitions from GET /api/moods on demand.
Provides sync-accessible cached data between refreshes, so the shuffle
engine can read moods without awaiting.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class MoodCache:
    """Cache of all mood definitions fetched from the manager add-on.

    Fetches from GET /api/moods. Cached snapshot is accessible synchronously
    via get_all() / get() between refreshes. Falls back to the last-known data
    if the add-on is temporarily unreachable.
    """

    def __init__(self, hass: HomeAssistant, manager_url: str) -> None:
        self._hass = hass
        self._manager_url = manager_url.rstrip("/")
        self._moods: dict[str, Any] = {}
        self._ever_loaded: bool = False

    async def async_refresh(self) -> bool:
        """Fetch all mood definitions from the manager. Returns True on success."""
        try:
            session = async_get_clientsession(self._hass)
            url = f"{self._manager_url}/api/moods"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._moods = data.get("moods", {})
                    self._ever_loaded = True
                    _LOGGER.debug(
                        "mood_cache: loaded %d moods", len(self._moods)
                    )
                    return True
                _LOGGER.warning(
                    "mood_cache: unexpected status %s from GET /api/moods",
                    resp.status,
                )
        except Exception as err:
            _LOGGER.warning("mood_cache: failed to refresh moods: %s", err)
        return False

    async def async_ensure_loaded(self) -> None:
        """Refresh if the cache has never been successfully loaded.

        Safe to call on every shuffle: no-op once loaded, single fetch if not.
        Handles the startup race condition where the manager add-on starts
        after HA and the initial async_refresh() failed silently.
        """
        if not self._ever_loaded:
            await self.async_refresh()

    def get_all(self) -> dict[str, Any]:
        """Return cached moods dict (sync)."""
        return self._moods

    def get(self, mood_id: str) -> dict[str, Any] | None:
        """Return a single cached mood definition by ID, or None."""
        return self._moods.get(mood_id)
