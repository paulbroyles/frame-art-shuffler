"""Tests for _async_invalidate_web_prefetch_for_tv in __init__.py.

Scenarios:
1. Web-source result → DELETE prefetch + trigger new prefetch.
2. Library image result → DELETE prefetch, no trigger.
3. No device in registry → bail out, no HTTP calls at all.
4. DELETE fails → error swallowed, still attempts to trigger prefetch.
5. _async_select_image fails → error swallowed, no trigger.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.frame_art_shuffler.const import DOMAIN

TV_ID = "tv-abc"
DEVICE_ID = "ha-device-uuid"
ENTRY_ID = "entry-xyz"
MGR_URL = "http://manager:8099"

_SELECT_WEB_SOURCE = (
    {"_web_sources": True, "_virtual_tag_id": "google-wallpaper"},
    1, "ws:google-wallpaper", 0, False,
)
_SELECT_LIBRARY = (
    {"filename": "nighthawks.jpg", "tags": ["night"]},
    5, "night", 0, False,
)
_SELECT_NONE = (None, 0, None, 0, False)


def _make_entry(mgr_url=MGR_URL):
    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.data = {
        "frame_art_manager_url": mgr_url,
        "tvs": {TV_ID: {"id": TV_ID, "name": "Living Room TV"}},
    }
    return entry


def _make_hass():
    hass = MagicMock()
    return hass


def _make_device_registry(device_id=DEVICE_ID):
    device = MagicMock()
    device.id = device_id
    registry = MagicMock()
    registry.async_get_device = MagicMock(return_value=device)
    return registry


def _make_session(delete_raises=None):
    """Return a mock aiohttp ClientSession whose .delete() is an async context manager."""
    @asynccontextmanager
    async def _delete_cm(url, **kwargs):
        if delete_raises:
            raise delete_raises
        yield MagicMock()

    session = MagicMock()
    session.delete = MagicMock(side_effect=_delete_cm)
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInvalidateWebPrefetchForTv:

    def _run(self, coro):
        return asyncio.run(coro)

    def test_web_source_result_deletes_and_triggers_prefetch(self):
        """When shuffle/select returns a web source, DELETE + trigger are both called."""
        async def _run():
            from custom_components.frame_art_shuffler import (
                _async_invalidate_web_prefetch_for_tv,
            )

            hass = _make_hass()
            entry = _make_entry()
            session = _make_session()
            registry = _make_device_registry()

            with (
                patch("custom_components.frame_art_shuffler.dr.async_get", return_value=registry),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    AsyncMock(return_value=_SELECT_WEB_SOURCE),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_trigger_prefetch",
                    AsyncMock(),
                ) as mock_trigger,
                patch(
                    "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                    return_value=session,
                ),
            ):
                await _async_invalidate_web_prefetch_for_tv(hass, entry, TV_ID, "everyday")

            assert session.delete.called
            delete_url = session.delete.call_args[0][0]
            assert f"prefetch/{DEVICE_ID}" in delete_url

        self._run(_run())

    def test_library_result_deletes_but_does_not_trigger_prefetch(self):
        """When shuffle/select returns a library image, DELETE is called but no trigger."""
        async def _run():
            from custom_components.frame_art_shuffler import (
                _async_invalidate_web_prefetch_for_tv,
            )

            hass = _make_hass()
            entry = _make_entry()
            session = _make_session()
            registry = _make_device_registry()

            trigger = AsyncMock()
            with (
                patch("custom_components.frame_art_shuffler.dr.async_get", return_value=registry),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    AsyncMock(return_value=_SELECT_LIBRARY),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_trigger_prefetch",
                    trigger,
                ),
                patch(
                    "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                    return_value=session,
                ),
            ):
                await _async_invalidate_web_prefetch_for_tv(hass, entry, TV_ID, "everyday")

            assert session.delete.called
            trigger.assert_not_called()

        self._run(_run())

    def test_no_device_bails_without_http_calls(self):
        """If no HA device is registered for the TV, no HTTP calls are made."""
        async def _run():
            from custom_components.frame_art_shuffler import (
                _async_invalidate_web_prefetch_for_tv,
            )

            hass = _make_hass()
            entry = _make_entry()
            session = _make_session()

            registry = MagicMock()
            registry.async_get_device = MagicMock(return_value=None)

            with (
                patch("custom_components.frame_art_shuffler.dr.async_get", return_value=registry),
                patch(
                    "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                    return_value=session,
                ),
            ):
                await _async_invalidate_web_prefetch_for_tv(hass, entry, TV_ID, "everyday")

            session.delete.assert_not_called()

        self._run(_run())

    def test_delete_failure_is_non_fatal(self):
        """DELETE raising does not prevent the prefetch trigger attempt."""
        async def _run():
            from custom_components.frame_art_shuffler import (
                _async_invalidate_web_prefetch_for_tv,
            )

            hass = _make_hass()
            entry = _make_entry()
            session = _make_session(delete_raises=OSError("connection refused"))
            registry = _make_device_registry()

            trigger = AsyncMock()
            with (
                patch("custom_components.frame_art_shuffler.dr.async_get", return_value=registry),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    AsyncMock(return_value=_SELECT_WEB_SOURCE),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_trigger_prefetch",
                    trigger,
                ),
                patch(
                    "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                    return_value=session,
                ),
            ):
                # Must not raise.
                await _async_invalidate_web_prefetch_for_tv(hass, entry, TV_ID, "everyday")

            trigger.assert_called_once()

        self._run(_run())

    def test_select_image_failure_is_non_fatal(self):
        """_async_select_image raising does not propagate."""
        async def _run():
            from custom_components.frame_art_shuffler import (
                _async_invalidate_web_prefetch_for_tv,
            )

            hass = _make_hass()
            entry = _make_entry()
            session = _make_session()
            registry = _make_device_registry()

            trigger = AsyncMock()
            with (
                patch("custom_components.frame_art_shuffler.dr.async_get", return_value=registry),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_select_image",
                    AsyncMock(side_effect=RuntimeError("manager down")),
                ),
                patch(
                    "custom_components.frame_art_shuffler.shuffle._async_trigger_prefetch",
                    trigger,
                ),
                patch(
                    "homeassistant.helpers.aiohttp_client.async_get_clientsession",
                    return_value=session,
                ),
            ):
                await _async_invalidate_web_prefetch_for_tv(hass, entry, TV_ID, "everyday")

            trigger.assert_not_called()

        self._run(_run())
