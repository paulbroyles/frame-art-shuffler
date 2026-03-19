"""In-memory cache for tagset definitions from the Frame Art Manager add-on.

Fetches all tagset definitions from GET /api/tagsets on demand.
Provides sync-accessible cached data between refreshes, so sensors and
fingerprint helpers can read tagsets without awaiting.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class TagsetCache:
    """Cache of all tagset definitions fetched from the manager add-on.

    Fetches from GET /api/tagsets. Cached snapshot is accessible synchronously
    via get_all() / get() between refreshes. Falls back to the last-known data
    if the add-on is temporarily unreachable.
    """

    def __init__(self, hass: HomeAssistant, manager_url: str) -> None:
        self._hass = hass
        self._manager_url = manager_url.rstrip("/")
        self._tagsets: dict[str, Any] = {}
        self._ever_loaded: bool = False

    async def async_refresh(self) -> bool:
        """Fetch all tagset definitions from the manager. Returns True on success."""
        try:
            session = async_get_clientsession(self._hass)
            url = f"{self._manager_url}/api/tagsets"
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._tagsets = data.get("tagsets", {})
                    self._ever_loaded = True
                    _LOGGER.debug(
                        "tagset_cache: loaded %d tagsets", len(self._tagsets)
                    )
                    return True
                _LOGGER.warning(
                    "tagset_cache: unexpected status %s from GET /api/tagsets",
                    resp.status,
                )
        except Exception as err:
            _LOGGER.warning("tagset_cache: failed to refresh tagsets: %s", err)
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
        """Return cached tagsets dict (sync)."""
        return self._tagsets

    def get(self, name: str) -> dict[str, Any] | None:
        """Return a single cached tagset definition by name, or None."""
        return self._tagsets.get(name)
