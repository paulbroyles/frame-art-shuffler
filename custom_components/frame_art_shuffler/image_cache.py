"""In-memory cache for image metadata from the Frame Art Manager add-on.

Used by sensors to look up current image metadata (tags, attributes, etc.)
without reading gallery.json directly. Falls back to cached data if the
add-on is temporarily unreachable.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)


class ImageMetadataCache:
    """Cache for single-image metadata fetched from the manager add-on.

    Fetches on demand from GET /api/images/:filename. Successful responses
    are cached indefinitely (image metadata rarely changes between shuffles).
    On API failure, returns the last-known cached value if available.
    """

    def __init__(self, hass: HomeAssistant, manager_url: str) -> None:
        self._hass = hass
        self._manager_url = manager_url.rstrip("/")
        self._cache: dict[str, dict[str, Any]] = {}

    async def get_image(self, filename: str) -> dict[str, Any] | None:
        """Return metadata for a single image.

        Tries the add-on API first. On success, updates the cache and returns
        the result. On failure, returns the cached version if available, else None.
        """
        try:
            session = async_get_clientsession(self._hass)
            url = f"{self._manager_url}/api/images/{filename}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._cache[filename] = data
                    return data
                if resp.status == 404:
                    return None
                _LOGGER.debug(
                    "image_cache: unexpected status %s for %s", resp.status, filename
                )
        except Exception as err:
            _LOGGER.debug("image_cache: API error for %s: %s", filename, err)

        # Fall back to cached value
        cached = self._cache.get(filename)
        if cached is not None:
            _LOGGER.debug("image_cache: using cached metadata for %s", filename)
        return cached

    def invalidate(self, filename: str | None = None) -> None:
        """Invalidate cache for a specific filename, or the entire cache."""
        if filename is None:
            self._cache.clear()
        else:
            self._cache.pop(filename, None)
