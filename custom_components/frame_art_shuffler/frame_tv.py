"""Helper utilities for interacting with Samsung Frame TVs.

This module provides art-focused functions for Frame TV control:
- set_art_on_tv_deleteothers: Upload and display artwork, manage gallery
- set_tv_brightness: Adjust art mode brightness (1-10, or 50 for max)
- is_art_mode_enabled: Check if TV is in art mode (screen may be on/off)
- is_screen_on: Check if screen is actually displaying content
- tv_on/tv_off: Screen power control (stays in art mode)
- set_art_mode: Switch TV to art mode (from TV mode or other state)

Power commands use the same KEY_POWER hold behavior as the Samsung Smart TV
integration to turn the screen off while maintaining art mode.

All TV operations use the async samsungtvws library and are native async.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

# VENDORING NOTE:
# We import a local vendored copy of samsungtvws (v3.0.3) to avoid conflicts
# with Home Assistant's built-in outdated version.
# If functionality breaks due to TV firmware updates, check the upstream repo:
# https://github.com/xchwarze/samsung-tv-ws-api
try:
    from . import samsungtvws
    from .samsungtvws.async_art import SamsungTVAsyncArt
    from .samsungtvws.async_remote import SamsungTVWSAsyncRemote
    from .samsungtvws.async_rest import SamsungTVAsyncRest
    from .samsungtvws.remote import SendRemoteKey
    from .samsungtvws.exceptions import UnauthorizedError
except ImportError:
    import samsungtvws
    from samsungtvws.async_art import SamsungTVAsyncArt
    from samsungtvws.async_remote import SamsungTVWSAsyncRemote
    from samsungtvws.async_rest import SamsungTVAsyncRest
    from samsungtvws.remote import SendRemoteKey
    from samsungtvws.exceptions import UnauthorizedError

from .const import DEFAULT_PORT, DEFAULT_TIMEOUT

_LOGGER = logging.getLogger(__name__)

TOKEN_DIR = Path(__file__).resolve().parent / "tokens"

_ART_MODE_ON = "on"
_UPLOAD_RETRIES = 3
_UPLOAD_RETRY_DELAY = 2
_INITIAL_UPLOAD_SETTLE = 6

# Placeholder matte used during upload to enable matte support.
# The actual desired matte is applied via change_matte() after upload.
# This workaround avoids Samsung firmware bug causing Error 40000.
# See docs/MATTE_BEHAVIOR.md for details.
_MATTE_PLACEHOLDER = "flexible_warm"
_DISPLAY_RETRY_DELAYS = (0, 10, 15)
_POST_DISPLAY_VERIFY_DELAY = 8
_DELETE_SETTLE = 4
_BRIGHTNESS_VERIFY_DELAY = 1
_VALID_BRIGHTNESS = set(range(1, 11)) | {50}
_WARN_FILE_MB = 10
_LARGE_FILE_MB = 15
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_POWER_COMMAND_RETRIES = 4
_POWER_RETRY_DELAY = 2
_POWER_COMMAND_TIMEOUT = 8
_SCREEN_CHECK_TIMEOUT = 6
_WOL_BROADCAST_IP = "255.255.255.255"
_WOL_BROADCAST_PORT = 9

# Use a dedicated directory for integration data to keep /config clean
# This matches the structure we want for tokens as well
DATA_DIR = Path("/config/frame_art_shuffler")
PROGRESS_LOG_FILE = DATA_DIR / "upload.log"


def _log_progress(msg: str) -> None:
    """Log message to the shared progress file."""
    _LOGGER.info(msg)
    try:
        # Ensure directory exists
        if not PROGRESS_LOG_FILE.parent.exists():
            # Only try to create if we are in a writable environment like /config
            if str(PROGRESS_LOG_FILE).startswith("/config"):
                PROGRESS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # Only write if parent dir exists
        if PROGRESS_LOG_FILE.parent.exists():
            timestamp = datetime.now().strftime("%H:%M:%S")
            with open(PROGRESS_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {msg}\n")
    except Exception:
        pass


def _clear_progress() -> None:
    """Clear the progress log file."""
    try:
        if PROGRESS_LOG_FILE.parent.exists():
            with open(PROGRESS_LOG_FILE, "w", encoding="utf-8") as f:
                f.write("")
    except Exception:
        pass


class FrameArtError(Exception):
    """Base error raised by the Frame TV helper."""


class FrameArtConnectionError(FrameArtError):
    """Raised when the TV can't be reached."""


class FrameArtUploadError(FrameArtError):
    """Raised when an upload or art operation fails."""


def set_token_directory(path: Path) -> None:
    """Override the token storage directory used by SamsungTVWS."""

    global TOKEN_DIR  # noqa: PLW0603 - module-level configuration mutation is intentional
    TOKEN_DIR = path
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    _LOGGER.info("Token directory set to: %s", TOKEN_DIR)


class _SamsungTVAsyncArtNoInit(SamsungTVAsyncArt):
    """SamsungTVAsyncArt subclass that skips the blocking sync REST call in get_token().

    SamsungTVAsyncArt.__init__ calls get_token() which instantiates SamsungTVWS (sync).
    SamsungTVWS.__init__ immediately makes a blocking REST call (get_model_year()) that
    blocks the event loop. Token handling for our use case is done via the token_file
    parameter, which SamsungTVWSBaseConnection reads lazily, so get_token() is a no-op.
    """

    def get_token(self) -> None:  # type: ignore[override]
        pass


class _AsyncFrameTVArtSession:
    """Async context manager for Samsung TV art channel WebSocket operations."""

    def __init__(self, ip: str, timeout: Optional[float] = None) -> None:
        self.ip = ip
        self.token_path = _token_path(ip)
        self._timeout = timeout if timeout is not None else DEFAULT_TIMEOUT
        _LOGGER.debug("Using token path: %s (exists: %s)", self.token_path, self.token_path.exists())
        self._art = _SamsungTVAsyncArtNoInit(
            host=ip,
            port=DEFAULT_PORT,
            timeout=self._timeout,
            token_file=str(self.token_path),
            name="FrameArtShuffler",
        )

    @property
    def art(self) -> SamsungTVAsyncArt:
        return self._art

    async def __aenter__(self) -> "_AsyncFrameTVArtSession":
        # Ensure we have a valid token by performing a handshake on the remote channel
        # if the token file is missing. The art channel does not support initial handshake.
        if not self.token_path.exists():
            _LOGGER.info(
                "No token found for %s at %s, attempting handshake via remote control channel...",
                self.ip, self.token_path,
            )
            try:
                remote = SamsungTVWSAsyncRemote(
                    host=self.ip,
                    port=DEFAULT_PORT,
                    timeout=self._timeout,
                    token_file=str(self.token_path),
                    name="FrameArtShuffler",
                )
                await remote.open()
                await remote.close()
                _LOGGER.info("Handshake successful, token saved to %s.", self.token_path)
            except Exception as err:
                _LOGGER.warning("Handshake attempt failed: %s", err)
                # We continue anyway, as art() might handle it or we want to bubble the error later

        await self._art.open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._art.close()


async def _rest_device_info(ip: str, timeout: float) -> Optional[dict]:
    """Fetch TV device info via async REST API. Returns None on any failure."""
    try:
        async with aiohttp.ClientSession() as session:
            rest_api = SamsungTVAsyncRest(
                host=ip, port=DEFAULT_PORT, session=session, timeout=timeout
            )
            return await rest_api.rest_device_info()
    except Exception:
        return None


async def set_art_on_tv_deleteothers(
    ip: str,
    artpath: str,
    *,
    mac_address: Optional[str] = None,
    delete_others: bool = False,
    ensure_art_mode: bool = True,
    screen_on: bool = True,
    matte: Optional[str] = None,
    photo_filter: Optional[str] = None,
    wait_after_upload: float = _INITIAL_UPLOAD_SETTLE,
    brightness: Optional[int] = None,
    debug: bool = False,
) -> str:
    """Upload art to the Frame TV, mirror test script behaviour, and return content_id.

    When screen_on=False the TV screen is left off throughout the operation:
    - Wake-on-LAN (if needed) sends only a single packet to bring the TV to
      "network awake, screen off" standby rather than the two-packet sequence
      that lights up the screen.
    - Art mode is not forced on (skips KEY_POWER toggle to avoid waking screen).
    - The uploaded image is selected with show=False so it becomes the active art
      for the next time the screen turns on, without activating the display now.
    """

    _clear_progress()
    _log_progress(f"Starting process for {ip}...")

    file_path = Path(artpath).expanduser().resolve()
    if not file_path.exists():
        raise FrameArtUploadError(f"Art file not found: {file_path}")

    payload = file_path.read_bytes()
    _log_file_details(file_path, payload)

    file_type = _detect_file_type(file_path)
    file_size = len(payload)

    if file_size > _MAX_UPLOAD_BYTES:
        size_mb = file_size / (1024 * 1024)
        raise FrameArtUploadError(
            f"Art file {file_path.name} is {size_mb:.2f} MB; maximum supported size is 5.00 MB"
        )

    # For screen_on=False (Shuffle Silently): check REST state BEFORE any WoL or WebSocket activity.
    # This gives us two pieces of information:
    # 1. pre_network_awake: is the TV's network interface already up?
    #    If yes, sending WoL would act as the "second packet" of the two-packet sequence
    #    and wake the screen — so we must skip WoL when network is already awake.
    # 2. pre_screen_on: was the screen physically on before the upload started?
    #    Used at display time to decide whether to show the art (screen was on) or
    #    stage it silently for the next screen wake (screen was off).
    pre_network_awake = False
    pre_screen_on = False
    if not screen_on:
        pre_network_awake, pre_screen_on = await _check_rest_state(ip)
        if pre_screen_on:
            _log_progress(f"Pre-upload: screen is on, network awake")
        elif pre_network_awake:
            _log_progress(f"Pre-upload: network awake, screen is off")
        else:
            _log_progress(f"Pre-upload: TV is in deep sleep")

    # Fail fast: Check if TV is reachable with a short timeout before starting the heavy upload process.
    # This prevents the UI from hanging for minutes if the TV is off.
    # Skip this check when no token exists yet: the 4-second timeout is far too short for the user
    # to see and accept the TV's "Allow connection?" permission prompt.  The upload session below
    # uses a 120-second timeout which gives enough time for first-time pairing to complete.
    token_exists = _token_path(ip).exists()
    if token_exists:
        try:
            _log_progress(f"Checking connectivity to {ip}...")
            async with _AsyncFrameTVArtSession(ip, timeout=4) as session:
                # Perform a lightweight operation to verify connection.
                # We don't care about the actual return value (True/False); we just want to confirm
                # that the TV received the request and responded, proving it is network-reachable.
                await session.art.get_artmode()
        except UnauthorizedError as err:
            # TV is reachable but rejecting the stored token. WoL is not relevant here.
            # Clear the stale token so the next attempt can trigger a fresh pairing prompt.
            token_path = _token_path(ip)
            if token_path.exists():
                token_path.unlink()
                _log_progress(f"Token rejected by TV — stale token deleted. Please retry to re-pair.")
            else:
                _log_progress(f"Token rejected by TV — please retry to trigger a new pairing prompt.")
            _log_progress(f"***** CONNECTION FAILED *****")
            raise FrameArtConnectionError(
                f"TV {ip} rejected the saved token (ms.channel.unauthorized). "
                f"The stale token has been deleted — please try again to re-pair."
            ) from err
        except Exception as err:
            # If we have a MAC address, try to wake the TV
            if mac_address:
                _log_progress(f"Art WebSocket unreachable. Checking whether WoL is needed...")
                try:
                    if screen_on:
                        # Full two-packet WoL: wakes network interface then turns on screen
                        await tv_on(ip, mac_address)
                        _log_progress(f"Wake sequence complete. Retrying connection...")
                        # Retry connectivity check after full wake
                        async with _AsyncFrameTVArtSession(ip, timeout=4) as session:
                            await session.art.get_artmode()
                    else:
                        # Single-packet WoL: brings TV to network-awake state, screen stays off.
                        # BUT only send WoL if the network interface is not already awake.
                        # If it is awake, a WoL packet would act as the "second packet"
                        # in the two-packet sequence and wake the screen.
                        if pre_network_awake:
                            _log_progress(f"Network interface already awake — skipping WoL to avoid waking screen.")
                        else:
                            _log_progress(f"Waking network interface (screen will stay off)...")
                            await tv_network_wake(mac_address, ip)
                            _log_progress(f"TV network-awake. Proceeding with upload (screen off).")

                except Exception as wake_err:
                    # If wake or retry failed, fall through to error
                    _log_progress(f"Wake attempt failed or TV still unreachable: {wake_err}")
                    _log_progress(f"***** CONNECTION FAILED *****")
                    raise FrameArtConnectionError(f"TV {ip} is unreachable after wake attempt: {wake_err}") from wake_err
            else:
                _log_progress(f"TV appears to be off or unreachable.")
                _log_progress(f"***** CONNECTION FAILED *****")
                raise FrameArtConnectionError(f"TV {ip} is unreachable (timeout): {err}") from err
    else:
        _log_progress(f"No token found for {ip} — skipping fast connectivity check to allow pairing.")

    # Upload with retries - recreate session on each attempt since connection may be broken
    response = None
    last_error: Optional[Exception] = None
    content_id: Optional[str] = None

    for attempt in range(_UPLOAD_RETRIES):
        if attempt:
            _LOGGER.info("Retrying upload attempt %s/%s", attempt + 1, _UPLOAD_RETRIES)
            await asyncio.sleep(_UPLOAD_RETRY_DELAY)

        try:
            # Use 120s timeout for upload to handle large files/slow networks
            async with _AsyncFrameTVArtSession(ip, timeout=120) as session:
                art = session.art

                if ensure_art_mode and screen_on and attempt == 0:  # Only check on first attempt
                    # Skip when screen_on=False — KEY_POWER toggle could wake the screen
                    await _ensure_art_mode(art, debug=debug)

                if brightness is not None and attempt == 0:  # Only set on first attempt
                    await _set_brightness(art, brightness, debug=debug)

                # Get current image count before upload
                images_before = None
                try:
                    _log_progress("Checking Art Mode connection and listing current images...")
                    images_before = await art.available()
                    ids = [img.get('content_id') for img in images_before] if images_before else []
                    count = len(images_before) if images_before else 0

                    # TV firmware often reports the active image twice (once as active, once as available).
                    # If we see exactly 2 identical IDs, report it as 1 image to avoid user confusion.
                    if count == 2 and len(ids) == 2 and ids[0] == ids[1]:
                        count = 1

                    _log_progress(f"Art Mode connection OK. Images on TV: {count} {ids}")
                except Exception as err:  # pylint: disable=broad-except
                    _LOGGER.warning("Could not list images (Art Mode connection issue?): %s", err)

                # Matte workaround: Samsung firmware has a bug where uploading with a matte
                # causes Error 40000 when selecting the image. Workaround is to upload with
                # a placeholder matte, then use change_matte() to apply the desired matte.
                # See docs/MATTE_BEHAVIOR.md for details.
                desired_matte = matte
                upload_matte = _MATTE_PLACEHOLDER if desired_matte and desired_matte != "none" else "none"

                try:
                    _log_progress(f"Uploading image to {ip} (attempt {attempt + 1}/{_UPLOAD_RETRIES})...")
                    if desired_matte and desired_matte != "none":
                        _log_progress(f"Uploading with placeholder matte (will apply '{desired_matte}' after)")

                    upload_result = await art.upload(
                        payload,
                        matte=upload_matte,
                        portrait_matte=upload_matte,
                        file_type=file_type,
                        timeout=120,
                    )

                    if upload_result:
                        content_id = upload_result
                        if desired_matte and desired_matte != "none":
                            _log_progress(f"Applying matte: {desired_matte}")
                            await art.change_matte(content_id, desired_matte)
                        _log_progress(f"Upload successful, content_id={content_id}")
                        if debug:
                            _LOGGER.debug("Upload returned content_id=%s", content_id)
                        break  # Success, exit retry loop

                    else:
                        # art.upload() returned None — timed out waiting for image_added confirmation.
                        # Check if the image actually appeared on the TV despite the timeout.
                        _LOGGER.info(
                            "Upload attempt %s timed out - checking if image appeared on TV...",
                            attempt + 1,
                        )

                        timeout_recovered = False
                        try:
                            await asyncio.sleep(6)  # Give TV more time to finish processing
                            images_after = await art.available()

                            if images_after and images_before is not None:
                                # Check if a new image appeared by comparing counts
                                count_before = len(images_before)
                                count_after = len(images_after)

                                if count_after > count_before:
                                    # New image(s) appeared! Find the new one by comparing lists
                                    before_ids = {img.get('content_id') for img in images_before}
                                    new_images = [img for img in images_after if img.get('content_id') not in before_ids]

                                    if new_images:
                                        # Found new image(s), take the first one
                                        content_id = new_images[0].get('content_id')
                                        _LOGGER.info("Upload timed out but new image appeared on TV (content_id=%s)", content_id)
                                        if content_id and desired_matte and desired_matte != "none":
                                            await art.change_matte(content_id, desired_matte)
                                        timeout_recovered = bool(content_id)
                                    else:
                                        # Fallback: if count increased but can't identify which, use newest
                                        _LOGGER.warning("Count increased but couldn't identify new image, using newest")
                                        content_id = images_after[-1].get('content_id')
                                        if content_id:
                                            if desired_matte and desired_matte != "none":
                                                await art.change_matte(content_id, desired_matte)
                                            timeout_recovered = True
                                else:
                                    _LOGGER.warning("Upload timed out and no new image appeared - upload actually failed")

                            # If we don't have before count, try to find by comparing with after
                            elif images_after:
                                _LOGGER.info("Upload timed out, no before count available, using newest image...")
                                content_id = images_after[-1].get('content_id')
                                _LOGGER.warning("Upload timed out, using newest image as best guess (content_id=%s)", content_id)
                                if content_id:
                                    timeout_recovered = True
                            else:
                                _LOGGER.warning("Upload timed out and TV has no images")

                        except Exception as check_err:  # pylint: disable=broad-except
                            _LOGGER.warning("Could not check TV gallery after timeout: %s", check_err)

                        if timeout_recovered:
                            break

                        # If timeout recovery failed, this was a real failure - will retry
                        last_error = FrameArtUploadError(
                            f"Upload attempt {attempt + 1} timed out and no new image confirmed on TV"
                        )
                        if attempt < _UPLOAD_RETRIES - 1:
                            _LOGGER.info("Upload actually failed (not just timeout), will retry...")
                        else:
                            raise last_error

                except Exception as upload_err:  # pylint: disable=broad-except
                    last_error = upload_err
                    if attempt < _UPLOAD_RETRIES - 1:
                        _LOGGER.warning("Upload attempt %s failed: %s", attempt + 1, upload_err)
                    else:
                        raise

        except Exception as err:  # pylint: disable=broad-except
            last_error = err
            if attempt == _UPLOAD_RETRIES - 1:
                # Last attempt failed
                break

    # Check if we got a content_id (either from successful upload or timeout recovery)
    if not content_id:
        raise FrameArtUploadError(f"Upload failed after {_UPLOAD_RETRIES} attempts: {last_error}")

    # We have a content_id, continue with display
    try:
        async with _AsyncFrameTVArtSession(ip) as session:
            art = session.art

            await _wait_with_countdown(wait_after_upload, "Waiting for TV to process upload")

            if screen_on:
                displayed = await _display_uploaded_art(
                    art,
                    content_id,
                    debug=debug,
                )
                if not displayed:
                    _LOGGER.warning("Uploaded art %s but could not verify display; check TV manually", content_id)
                else:
                    _log_progress(f"Art {content_id} successfully displayed on {ip}")
            else:
                # screen_on=False (Shuffle Silently): don't wake the screen if it's off.
                # Use pre_screen_on (checked via REST before any WoL) to decide:
                # - Screen was on  → display the art visibly (it's already showing, no waking needed)
                # - Screen was off → stage silently so it shows on the next screen wake
                if pre_screen_on:
                    displayed = await _display_uploaded_art(
                        art,
                        content_id,
                        debug=debug,
                    )
                    if not displayed:
                        _LOGGER.warning("Uploaded art %s but could not verify display; check TV manually", content_id)
                    else:
                        _log_progress(f"Art {content_id} successfully displayed on {ip} (screen was already on)")
                else:
                    await _select_uploaded_art_silent(art, content_id)
                    _log_progress(f"Art {content_id} staged on {ip} (screen off — will show on wake)")

            # Apply photo filter if specified
            if photo_filter is not None and photo_filter.lower() not in ("none", ""):
                try:
                    _log_progress(f"Applying photo filter '{photo_filter}' to {ip}")
                    if debug:
                        _LOGGER.debug("Applying photo filter '%s' to content_id=%s", photo_filter, content_id)
                    await art.set_photo_filter(content_id, photo_filter)
                    _log_progress(f"Photo filter '{photo_filter}' applied successfully")
                    if debug:
                        _LOGGER.debug("Successfully applied photo filter '%s'", photo_filter)
                except Exception as filter_err:  # pylint: disable=broad-except
                    _LOGGER.warning("Failed to apply photo filter '%s': %s", photo_filter, filter_err)

            if delete_others:
                _log_progress("Cleaning up old images from TV memory...")
                await _delete_other_images(art, content_id, debug=debug)

            _log_progress(f"Upload complete for {ip} (content_id={content_id})")

            return content_id
    except Exception as err:  # pylint: disable=broad-except
        # Upload worked but post-processing failed
        _LOGGER.error("Upload succeeded (content_id=%s) but post-processing failed: %s", content_id, err)
        raise FrameArtUploadError(f"Upload succeeded but failed to display/cleanup: {err}") from err


async def set_tv_brightness(ip: str, brightness: int) -> None:
    """Set the art-mode brightness following the reference script behaviour."""

    if brightness not in _VALID_BRIGHTNESS:
        raise ValueError("Brightness must be 1-10 for normal or 50 for max")

    async with _AsyncFrameTVArtSession(ip) as session:
        art = session.art

        # Set brightness directly without pre-checking current value
        # (TV can be slow/unresponsive to brightness queries)
        await _set_brightness_value(art, brightness)
        await asyncio.sleep(_BRIGHTNESS_VERIFY_DELAY)

        # Try to verify, but don't fail if verification times out
        try:
            confirmed = await _get_brightness_value(art)
            if confirmed != brightness:
                _LOGGER.warning(
                    "TV reported brightness %s after setting %s (may be stale)",
                    confirmed, brightness
                )
            else:
                _LOGGER.info("Brightness set to %s on %s", confirmed, ip)
        except Exception as err:  # pylint: disable=broad-except
            # Verification failed but set command was sent
            _LOGGER.info("Brightness command sent to %s (verification timed out)", ip)


async def get_tv_brightness(ip: str) -> Optional[int]:
    """Query the TV's current brightness value.

    Returns the brightness (1-10) or None if the query fails.
    This opens a new connection to the TV to get the current value.
    """
    try:
        async with _AsyncFrameTVArtSession(ip) as session:
            return await _get_brightness_value(session.art)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Failed to get brightness from %s: %s", ip, err)
        return None


async def is_art_mode_enabled(ip: str) -> Optional[bool]:
    """Return True when the TV reports art mode is active, False if not, None if unknown.

    Returns None if the Art WebSocket connection times out or fails, indicating
    the state could not be determined. This allows callers to distinguish between
    "art mode is definitely off" (False) and "we couldn't check" (None).
    """
    try:
        async with _AsyncFrameTVArtSession(ip) as session:
            status = await session.art.get_artmode()
            _LOGGER.debug("Art mode status for %s: %s", ip, status)
            return status == _ART_MODE_ON
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Art mode check failed for %s: %s", ip, err)
        return None  # Unknown state - couldn't connect to check


async def is_screen_on(ip: str, timeout: Optional[float] = None) -> bool:
    """Return True when the TV screen is actually on and displaying content.

    This checks the TV's power state via the async REST API, not WebSocket.
    REST API is faster and more reliable for status checks since it doesn't
    require opening a WebSocket connection.

    Note: May return False if the TV is in a deep sleep state where REST
    API is also unresponsive.
    """
    data = await _rest_device_info(ip, timeout or _SCREEN_CHECK_TIMEOUT)
    if data is None:
        _LOGGER.debug("Screen status check failed for %s (no REST response)", ip)
        return False
    return data.get("device", {}).get("PowerState", "off") == "on"


async def _check_rest_state(ip: str, timeout: float = 3.0) -> tuple:
    """Check TV network and screen state via async REST API.

    Returns (network_awake, screen_on) where:
    - network_awake: True if the REST API responded at all (network interface is up,
      regardless of whether the screen is on or off)
    - screen_on: True if the TV reports PowerState='on' (screen is physically on)

    Unlike is_screen_on(), this distinguishes between:
    - "REST responded but screen off" → (True, False): network awake, screen dark
    - "REST did not respond"          → (False, False): TV in deep sleep

    This is used before sending Wake-on-LAN to determine whether the network
    interface needs waking. Sending WoL to a TV whose network is already awake
    can act as the "second packet" of the two-packet sequence, waking the screen.
    """
    data = await _rest_device_info(ip, timeout)
    if data is None:
        return False, False
    power_on = data.get("device", {}).get("PowerState", "off") == "on"
    return True, power_on


async def tv_network_wake(mac_address: str, ip: str, *, timeout: int = 45) -> bool:
    """Bring Frame TV to network-awake state via a single Wake-on-LAN packet.

    This sends ONLY the first WoL packet, which wakes the network interface and
    moves the TV into "network awake, screen off" standby. The screen does NOT
    turn on.

    Compare to tv_on(), which sends two WoL packets — the second one turns on the
    screen. Use this function when you want to upload or change art while keeping
    the screen off (e.g., during auto-shuffle while the TV is sleeping).

    Polls the art WebSocket until the TV responds or the timeout expires.
    Returns True when the TV is reachable.
    Raises FrameArtConnectionError if the TV does not respond within the timeout.
    """
    _send_wake_on_lan(mac_address)
    _LOGGER.info(
        "Wake-on-LAN packet sent to %s (single packet — network wake only, screen stays off)",
        mac_address,
    )

    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    poll_interval = 3
    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        try:
            async with _AsyncFrameTVArtSession(ip, timeout=4) as session:
                await session.art.get_artmode()
            _LOGGER.info("TV %s is now network-reachable (screen off)", ip)
            return True
        except UnauthorizedError:
            # Token rejected — TV is reachable but we have auth issues.
            # Treat as reachable so the upload loop can try (or fail with a clear error).
            _LOGGER.info("TV %s is network-reachable (token rejected, screen off)", ip)
            return True
        except Exception:
            pass  # Not yet reachable; keep polling

    raise FrameArtConnectionError(
        f"TV {ip} did not become network-reachable within {timeout}s after Wake-on-LAN"
    )


async def tv_on(ip: str, mac_address: str) -> bool:
    """Wake Frame TV via Wake-on-LAN.

    Samsung Frame TVs require a two-stage Wake-on-LAN approach with significant delay:

    1. First WOL wakes the network interface, but the TV enters a "network awake,
       screen off" standby state where the screen remains black.

    2. The TV needs 12+ seconds to fully transition into this network-awake state
       before it will respond to commands.

    3. Second WOL (sent after the delay) actually turns on the screen and displays
       art mode.

    CRITICAL: The 12-second delay between WOL packets is required. Testing showed that
    shorter delays (2s, 5s) do not work - the TV must fully enter the network-awake
    state before the second WOL will turn on the screen. This mimics the reliable
    behavior of manually running the WOL command twice from the CLI with natural
    human delay between commands.

    This function intentionally does NOT send KEY_POWER to avoid toggle issues where
    the TV might switch from art mode to TV content mode unexpectedly.

    Returns True when Wake-on-LAN was sent successfully. For diagnostic purposes,
    logs the TV's screen and art mode state after waking.

    If you need to ensure the TV is in art mode after waking, call set_art_mode()
    separately.
    """

    # First WOL: Wake network interface
    _send_wake_on_lan(mac_address)
    _LOGGER.info("Wake-on-LAN packet sent to %s (first - waking network)", mac_address)

    # CRITICAL: Wait for TV to fully enter network-awake state
    # This delay was determined through testing - shorter delays (2s, 5s) do not work.
    # The TV needs this time to transition from "fully off" to "network awake, screen off"
    # before the second WOL packet will successfully turn on the screen.
    await asyncio.sleep(12)

    # Second WOL: Turn on screen
    _send_wake_on_lan(mac_address)
    _LOGGER.info("Wake-on-LAN packet sent to %s (second - turning on screen)", mac_address)

    # Give TV time to fully wake up and display art
    await asyncio.sleep(3)

    # Check state for diagnostic purposes (don't take action based on it)
    try:
        screen = await is_screen_on(ip, timeout=_SCREEN_CHECK_TIMEOUT)
        art_enabled = await is_art_mode_enabled(ip)
        _LOGGER.info(
            "TV %s state after Wake-on-LAN: screen_on=%s, art_mode=%s",
            ip, screen, art_enabled
        )
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Could not check TV state after Wake-on-LAN: %s", err)

    return True


async def set_art_mode(ip: str) -> None:
    """Switch TV to art mode by sending KEY_POWER.

    When the TV is powered on and showing content (TV channels, apps, etc.), sending
    KEY_POWER will switch it to art mode. This is the reliable programmatic method
    discovered from the Nick Waterton examples.

    If the TV is already in art mode, this is a no-op.
    If the TV is off, this will turn it on (behavior depends on TV settings).

    Note: KEY_POWER is a toggle, so we must verify current state before sending it.
    If we cannot determine the current state, we do not send the command to avoid
    accidentally toggling out of art mode.
    """

    # First check if already in art mode
    # KEY_POWER is a toggle, so we MUST know the current state
    async with _AsyncFrameTVArtSession(ip) as session:
        try:
            status = await session.art.get_artmode()
            _LOGGER.debug("Current art mode status for %s: %s", ip, status)

            if status == _ART_MODE_ON:
                _LOGGER.info("TV %s already in art mode", ip)
                return
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("Could not verify art mode status for %s: %s. Not sending KEY_POWER to avoid toggling out of art mode.", ip, err)
            # Do NOT continue - KEY_POWER is a toggle so we need to know current state
            raise FrameArtUploadError(f"Cannot determine art mode status for {ip}, refusing to send KEY_POWER") from err

    # Send KEY_POWER to switch to art mode
    token_path = _token_path(ip)
    try:
        remote = SamsungTVWSAsyncRemote(
            host=ip,
            port=DEFAULT_PORT,
            token_file=str(token_path),
            name="FrameArtShuffler",
        )
        await remote.send_command(SendRemoteKey.click("KEY_POWER"))
        await remote.close()
        _LOGGER.info("Sent KEY_POWER to switch %s to art mode", ip)

        # Give TV time to switch
        await asyncio.sleep(3)

        # Verify it worked
        async with _AsyncFrameTVArtSession(ip) as session:
            new_status = await session.art.get_artmode()
            if new_status == _ART_MODE_ON:
                _LOGGER.info("TV %s successfully switched to art mode", ip)
            else:
                _LOGGER.warning("TV %s may not have switched to art mode (status: %s)", ip, new_status)

    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Failed to switch {ip} to art mode: {err}") from err


async def tv_off(ip: str) -> None:
    """Power off Frame TV screen while staying in art mode (hold KEY_POWER for 3 seconds).

    This operation is idempotent - if the TV screen is already off (or unreachable),
    the function returns successfully without attempting to send a power command.
    """
    # Check if screen is already off before attempting power command
    # is_screen_on() returns False if screen is off OR if TV is unreachable
    if not await is_screen_on(ip, timeout=_SCREEN_CHECK_TIMEOUT):
        _LOGGER.info("TV %s screen is already off or unreachable, skipping power command", ip)
        return

    token_path = _token_path(ip)
    last_error: Optional[Exception] = None

    for attempt in range(_POWER_COMMAND_RETRIES):
        if attempt > 0:
            _LOGGER.debug("Retrying tv_off attempt %s/%s", attempt + 1, _POWER_COMMAND_RETRIES)
            await asyncio.sleep(_POWER_RETRY_DELAY)

        try:
            remote = SamsungTVWSAsyncRemote(
                host=ip,
                port=DEFAULT_PORT,
                timeout=_POWER_COMMAND_TIMEOUT,
                token_file=str(token_path),
                name="FrameArtShuffler",
            )
            # For Frame TVs, hold KEY_POWER for 3 seconds to turn screen off while staying in art mode
            # This mimics the Samsung Smart TV integration's Frame-specific behavior
            await remote.send_commands(SendRemoteKey.hold("KEY_POWER", 3))
            await remote.close()
            return
        except Exception as err:  # pylint: disable=broad-except
            last_error = err
            _LOGGER.debug("tv_off attempt %s failed: %s", attempt + 1, err)

    # All retries exhausted - but don't raise an exception
    # If we can't reach the TV to turn it off, it's effectively already off or will
    # turn itself off. Raising here would break automation scripts unnecessarily.
    _LOGGER.warning(
        "Could not send power-off command to TV %s after %s attempts (%s), "
        "treating as already off",
        ip, _POWER_COMMAND_RETRIES, last_error
    )


def _send_wake_on_lan(mac_address: str) -> None:
    """Broadcast a Wake-on-LAN packet to wake the Frame TV network interface."""

    cleaned = re.sub(r"[^0-9A-Fa-f]", "", mac_address)
    if len(cleaned) != 12:
        raise FrameArtConnectionError(f"Invalid MAC address for Wake-on-LAN: {mac_address}")

    wol_payload = bytes.fromhex("FF" * 6 + cleaned * 16)

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(wol_payload, (_WOL_BROADCAST_IP, _WOL_BROADCAST_PORT))
    except OSError as err:
        raise FrameArtConnectionError(f"Failed to send Wake-on-LAN packet to {mac_address}: {err}") from err


async def _display_uploaded_art(art: SamsungTVAsyncArt, content_id: str, *, debug: bool) -> bool:
    # Method 1: try direct selection with retries mirroring the reference script
    for attempt, delay in enumerate(_DISPLAY_RETRY_DELAYS):
        if attempt and delay:
            _LOGGER.debug("Waiting %ss before retrying display", delay)
            await asyncio.sleep(delay)
        try:
            await art.select_image(content_id, show=True)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.debug("select_image failed on attempt %s: %s", attempt + 1, err)
            continue

        await _wait_with_countdown(_POST_DISPLAY_VERIFY_DELAY, "Image selected. Verifying display")
        if await _verify_current_art(art, content_id, debug=debug):
            return True

    # Method 2: fallback to selecting the newest image from the gallery
    try:
        gallery = await art.available() or []
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Fetching available art failed: %s", err)
        gallery = []

    if gallery:
        newest = gallery[-1].get("content_id")
        if newest:
            try:
                await art.select_image(newest, show=True)
                await asyncio.sleep(_POST_DISPLAY_VERIFY_DELAY)
                return await _verify_current_art(art, newest, debug=debug)
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("Fallback select_image failed: %s", err)

    return False


async def _select_uploaded_art_silent(art: SamsungTVAsyncArt, content_id: str) -> None:
    """Select uploaded art as the current image without activating the display.

    Uses select_image(show=False) so the image becomes the active art the TV
    will show next time the screen turns on, without lighting up the screen now.
    """
    try:
        await art.select_image(content_id, show=False)
        _LOGGER.info("Art %s selected silently (screen off — will display on wake)", content_id)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.warning("Failed to silently select art %s: %s", content_id, err)


async def _verify_current_art(art: SamsungTVAsyncArt, expected_content_id: str, *, debug: bool) -> bool:
    try:
        current = await art.get_current()
        current_id = current.get("content_id", "unknown")
        if debug:
            _LOGGER.debug("Current art: %s (expected %s)", current_id, expected_content_id)
        return current_id == expected_content_id
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Could not verify current art: %s", err)
        return False


async def _delete_other_images(art: SamsungTVAsyncArt, keep_content_id: str, *, debug: bool) -> None:
    try:
        available = await art.available() or []
    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Could not enumerate TV gallery: {err}") from err

    deletions = [item.get("content_id") for item in available if item.get("content_id") and item.get("content_id") != keep_content_id]

    # Log what we are keeping to help debug duplicate issues
    kept = [item.get("content_id") for item in available if item.get("content_id") == keep_content_id]
    if len(kept) > 1:
        _log_progress(f"Warning: Found {len(kept)} copies of active image {keep_content_id}. Keeping all to avoid accidental deletion.")

    if not deletions:
        _LOGGER.debug("No other images to delete")
        return

    _log_progress(f"Deleting {len(deletions)} old images: {deletions}")
    await art.delete_list(deletions)
    if debug:
        _LOGGER.debug("Deleted %s old images", len(deletions))
    await asyncio.sleep(_DELETE_SETTLE)


async def _set_brightness(art: SamsungTVAsyncArt, brightness: int, *, debug: bool) -> None:
    try:
        current = await _get_brightness_value(art)
        if debug:
            _LOGGER.debug("Current brightness before set: %s", current)
    except Exception:
        current = None

    await _set_brightness_value(art, brightness)
    await asyncio.sleep(_BRIGHTNESS_VERIFY_DELAY)

    try:
        confirmed = await _get_brightness_value(art)
    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Unable to verify brightness after setting {brightness}: {err}") from err

    if confirmed != brightness:
        raise FrameArtUploadError(
            f"Expected brightness {brightness} but TV reported {confirmed}"
        )

    _LOGGER.info("Brightness set to %s", confirmed)


async def _ensure_art_mode(art: SamsungTVAsyncArt, *, debug: bool) -> None:
    try:
        status = await art.get_artmode()
        if debug:
            _LOGGER.debug("Art mode status: %s", status)
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.debug("Unable to read art mode status: %s", err)
        return

    if status == _ART_MODE_ON:
        return

    try:
        await art.set_artmode(_ART_MODE_ON)
        await asyncio.sleep(_INITIAL_UPLOAD_SETTLE)
        status = await art.get_artmode()
    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Unable to enable art mode: {err}") from err

    if status != _ART_MODE_ON:
        raise FrameArtUploadError(f"TV art mode still {status}, expected {_ART_MODE_ON}")


def _log_file_details(file_path: Path, payload: bytes) -> None:
    size_mb = len(payload) / (1024 * 1024)
    _LOGGER.info("Preparing upload of %s (%.2f MB)", file_path.name, size_mb)

    if size_mb > _LARGE_FILE_MB:
        _LOGGER.warning(
            "File %.2f MB is large; Samsung Frame TVs may timeout. Consider resizing to < %.1f MB",
            size_mb,
            _WARN_FILE_MB,
        )
    elif size_mb > _WARN_FILE_MB:
        _LOGGER.info("File %.2f MB; expect longer upload times", size_mb)


def _detect_file_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "jpeg"
    if suffix == ".png":
        return "png"
    return "jpeg"


def _token_path(ip: str) -> Path:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    # Match flow_utils.safe_token_filename behavior: replace all non-alphanumeric with _
    safe_ip = re.sub(r"[^A-Za-z0-9]+", "_", ip)
    return TOKEN_DIR / f"{safe_ip}.token"


async def _get_brightness_value(art: SamsungTVAsyncArt) -> int:
    try:
        data = await art.get_brightness()
    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Unable to read brightness: {err}") from err

    if not data:
        raise FrameArtUploadError("TV did not return a brightness response")

    # get_brightness() returns the parsed response data dict.
    # The brightness value is in data["value"].
    value = data.get("value")
    if value is None:
        _LOGGER.debug(f"Brightness response missing 'value' key. Response: {data}")
        raise FrameArtUploadError(f"Brightness response missing 'value' field")

    return int(value)


async def _set_brightness_value(art: SamsungTVAsyncArt, brightness: int) -> None:
    try:
        await art.set_brightness(brightness)
    except Exception as err:  # pylint: disable=broad-except
        raise FrameArtUploadError(f"Unable to set brightness {brightness}: {err}") from err


def delete_token(ip: str) -> None:
    """Delete the token file for the given IP address."""
    token_path = _token_path(ip)
    if token_path.exists():
        try:
            token_path.unlink()
            _LOGGER.info("Deleted token file for %s", ip)
        except OSError as err:
            raise FrameArtError(f"Failed to delete token file for {ip}: {err}") from err
    else:
        _LOGGER.info("No token file found for %s", ip)


async def _wait_with_countdown(seconds: float, msg: str) -> None:
    """Wait for specified seconds while logging a countdown inline."""
    _LOGGER.info(f"{msg} ({seconds}s)")

    # Ensure directory exists
    try:
        if not PROGRESS_LOG_FILE.parent.exists():
            if str(PROGRESS_LOG_FILE).startswith("/config"):
                PROGRESS_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # If we can't write to file, just sleep
    if not PROGRESS_LOG_FILE.parent.exists():
        await asyncio.sleep(seconds)
        return

    try:
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(PROGRESS_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {msg} ")
            f.flush()

            remaining = int(seconds)
            while remaining > 0:
                f.write(f"{remaining}... ")
                f.flush()
                await asyncio.sleep(1)
                remaining -= 1

            f.write("Done.\n")
    except Exception:
        # Fallback if file operations fail
        await asyncio.sleep(seconds)
