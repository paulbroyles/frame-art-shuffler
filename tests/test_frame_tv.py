"""Tests for frame_tv.py — _upload_detecting_deep_sleep."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.frame_art_shuffler.frame_tv import (
    FrameArtDeepSleepError,
    _upload_detecting_deep_sleep,
)

_IP = "192.168.1.100"
_PATH = "/tmp/test.jpg"
_MATTE = "none"

# Patch target for the two module-level constants so tests run in milliseconds.
_POLL = "custom_components.frame_art_shuffler.frame_tv._DEEP_SLEEP_POLL_SECS"
_THRESHOLD = "custom_components.frame_art_shuffler.frame_tv._DEEP_SLEEP_FAIL_THRESHOLD"
_REST = "custom_components.frame_art_shuffler.frame_tv._check_rest_state"


class TestUploadDetectingDeepSleep:
    def test_fast_upload_returns_content_id(self):
        """Upload finishes before the first REST poll — content_id returned, no REST call made."""
        async def _run():
            art = MagicMock()
            art.upload = AsyncMock(return_value="MY_F001")

            with patch(_REST) as mock_rest, patch(_POLL, 0.05):
                mock_rest.return_value = (True, True)
                result = await _upload_detecting_deep_sleep(art, _PATH, _MATTE, _IP)

            assert result == "MY_F001"
            mock_rest.assert_not_called()

        asyncio.run(_run())

    def test_deep_sleep_raises_after_threshold_dark_polls(self):
        """Upload hangs + REST always dark → FrameArtDeepSleepError after FAIL_THRESHOLD polls."""
        async def _run():
            art = MagicMock()

            async def hanging_upload(*args, **kwargs):
                await asyncio.sleep(9999)

            art.upload = hanging_upload

            with patch(_REST) as mock_rest, patch(_POLL, 0.01), patch(_THRESHOLD, 2):
                mock_rest.return_value = (False, False)
                with pytest.raises(FrameArtDeepSleepError):
                    await _upload_detecting_deep_sleep(art, _PATH, _MATTE, _IP)

            assert mock_rest.call_count >= 2

        asyncio.run(_run())

    def test_single_dark_poll_does_not_abort(self):
        """One dark REST poll (below threshold) is tolerated; upload still completes."""
        async def _run():
            upload_done = asyncio.Event()

            art = MagicMock()

            async def slow_upload(*args, **kwargs):
                # Complete after the event is set externally.
                await upload_done.wait()
                return "MY_F002"

            art.upload = slow_upload

            poll_count = 0

            async def mock_rest(*args, **kwargs):
                nonlocal poll_count
                poll_count += 1
                if poll_count == 1:
                    # Dark on first poll — set event so upload completes before second poll.
                    upload_done.set()
                    return (False, False)
                return (True, True)

            with patch(_REST, side_effect=mock_rest), patch(_POLL, 0.01), patch(_THRESHOLD, 2):
                result = await _upload_detecting_deep_sleep(art, _PATH, _MATTE, _IP)

            assert result == "MY_F002"

        asyncio.run(_run())

    def test_upload_exception_propagates(self):
        """art.upload() raises → exception propagates unchanged (no deep-sleep wrapping)."""
        async def _run():
            art = MagicMock()
            art.upload = AsyncMock(side_effect=ConnectionError("TV disconnected"))

            with patch(_REST) as mock_rest, patch(_POLL, 0.05):
                mock_rest.return_value = (True, True)
                with pytest.raises(ConnectionError, match="TV disconnected"):
                    await _upload_detecting_deep_sleep(art, _PATH, _MATTE, _IP)

        asyncio.run(_run())

    def test_upload_task_cancelled_on_deep_sleep(self):
        """After FrameArtDeepSleepError, the underlying upload task is cancelled (not leaked)."""
        async def _run():
            upload_started = asyncio.Event()
            upload_cancelled = asyncio.Event()

            art = MagicMock()

            async def hanging_upload(*args, **kwargs):
                upload_started.set()
                try:
                    await asyncio.sleep(9999)
                except asyncio.CancelledError:
                    upload_cancelled.set()
                    raise

            art.upload = hanging_upload

            with patch(_REST) as mock_rest, patch(_POLL, 0.01), patch(_THRESHOLD, 2):
                mock_rest.return_value = (False, False)
                with pytest.raises(FrameArtDeepSleepError):
                    await _upload_detecting_deep_sleep(art, _PATH, _MATTE, _IP)

            # Give the event loop a moment to settle task cleanup.
            await asyncio.sleep(0.01)
            assert upload_cancelled.is_set(), "Upload task was not cancelled after deep-sleep abort"

        asyncio.run(_run())
