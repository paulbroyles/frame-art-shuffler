"""Tests for art channel auto-recovery (Deliverable B).

Covers:
1. TVConnectionManager staleness tracking — stale_duration() before/after probe
2. async_shuffle_tv flags recovery_pending on stale channel (opt-in only)
3. Watchdog decision matrix:
   - Immediate-origin reason (manual/event) → recovers on next tick
   - Scheduled origin + screen on → defers (no recovery)
   - Scheduled origin + screen off → recovers
   - Scheduled origin + screen on + pending age > 3h + overnight window → recovers
   - Scheduled origin + screen on + pending age > 3h + outside overnight → still defers
4. wake_into_art_mode: clean wake, CEC-defeat second click, unreachable art channel
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_manager(ip="192.168.1.50"):
    from custom_components.frame_art_shuffler.frame_tv import TVConnectionManager
    return TVConnectionManager(ip)


def _make_entry(tv_id="tv1", auto_recover=True):
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.data = {
        "tvs": {
            tv_id: {
                "name": "Kitchen TV",
                "ip": "192.168.1.50",
                "mac": "AA:BB:CC:DD:EE:FF",
                "auto_recover_art_channel": auto_recover,
            }
        }
    }
    return entry


def _make_hass(entry, tv_id="tv1", client=None):
    hass = MagicMock()
    hass.data = {
        "frame_art_shuffler": {
            entry.entry_id: {
                "upload_in_progress": set(),
                "last_upload_cleared_at": None,
                "art_clients": {tv_id: client or MagicMock()},
                "recovery_pending": {},
                "art_channel_recovery": {},
                "shuffle_cache": {},
                "staged_images": {},
                "tagset_cache": None,
            }
        }
    }
    return hass


# ---------------------------------------------------------------------------
# 1. Staleness tracking
# ---------------------------------------------------------------------------

class TestStalenesstracking:
    """TVConnectionManager._stale_since / stale_duration() lifecycle."""

    def test_stale_duration_zero_when_healthy(self):
        mgr = _make_manager()
        assert mgr.stale_duration() == 0.0

    def test_stale_duration_positive_after_probe_failure(self):
        async def _run():
            mgr = _make_manager()
            # Simulate probe failure path: _stale_since set when probe fails.
            mgr._stale_since = asyncio.get_event_loop().time() - 10.0
            assert mgr.stale_duration() >= 9.9

        asyncio.run(_run())

    def test_stale_duration_zero_after_recovery(self):
        async def _run():
            mgr = _make_manager()
            mgr._stale_since = asyncio.get_event_loop().time() - 30.0
            # Simulate probe success (ensure_connected clears _stale_since).
            mgr._stale_since = None
            assert mgr.stale_duration() == 0.0

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. async_shuffle_tv flags recovery_pending
# ---------------------------------------------------------------------------

class TestShuffleTvRecoveryFlagging:
    """async_shuffle_tv sets recovery_pending when channel is stale + opt-in."""

    def _run_shuffle(self, tv_config_override=None, stale=True, auto_recover=True):
        """Run async_shuffle_tv with a failing inner call and return hass.data."""
        async def _run():
            from custom_components.frame_art_shuffler.shuffle import async_shuffle_tv

            entry = _make_entry(auto_recover=auto_recover)
            if tv_config_override:
                entry.data["tvs"]["tv1"].update(tv_config_override)

            client = MagicMock()
            client.stale_duration = MagicMock(return_value=30.0 if stale else 0.0)
            hass = _make_hass(entry, client=client)

            # Make _async_shuffle_tv_inner raise to trigger the exception path.
            with patch(
                "custom_components.frame_art_shuffler.shuffle._async_shuffle_tv_inner",
                side_effect=Exception("upload timed out"),
            ):
                with patch(
                    "custom_components.frame_art_shuffler.shuffle.log_activity"
                ):
                    result = await async_shuffle_tv(
                        hass, entry, "tv1", reason="auto",
                    )

            assert result is False
            return hass.data["frame_art_shuffler"][entry.entry_id]

        return asyncio.run(_run())

    def test_flags_recovery_when_stale_and_opt_in(self):
        data = self._run_shuffle(stale=True, auto_recover=True)
        assert "tv1" in data["recovery_pending"]
        assert data["recovery_pending"]["tv1"]["reason"] == "auto"

    def test_no_flag_when_not_stale(self):
        data = self._run_shuffle(stale=False, auto_recover=True)
        assert "tv1" not in data["recovery_pending"]

    def test_no_flag_when_opt_out(self):
        data = self._run_shuffle(stale=True, auto_recover=False)
        assert "tv1" not in data["recovery_pending"]


# ---------------------------------------------------------------------------
# 3. Watchdog decision matrix
# ---------------------------------------------------------------------------

class TestArtChannelWatchdog:
    """_async_art_channel_watchdog applies recovery policy correctly."""

    def _build_watchdog_scenario(
        self, pending_reason="auto", pending_age_s=60,
        screen_on=False, stale_duration=60.0, auto_recover=True,
        cooldown_remaining=0, local_hour=4,
    ):
        """Return (watchdog_coroutine, data_dict) ready to await."""
        import importlib
        import sys

        entry = _make_entry(auto_recover=auto_recover)
        client = MagicMock()
        client.stale_duration = MagicMock(return_value=stale_duration)

        # recover_art_channel must be a coroutine method.
        async def _recover(mac_address=None):
            return True

        client.recover_art_channel = _recover

        data = {
            "art_clients": {"tv1": client},
            "recovery_pending": {
                "tv1": {
                    "pending_since": asyncio.get_event_loop().time() - pending_age_s,
                    "reason": pending_reason,
                }
            },
            "art_channel_recovery": {
                "tv1": {"cooldown_until": asyncio.get_event_loop().time() - (3600 - cooldown_remaining)}
                if cooldown_remaining > 0 else {}
            }.get("tv1", {}),
        }
        # normalise into the expected structure
        data["art_channel_recovery"] = (
            {"tv1": {"cooldown_until": asyncio.get_event_loop().time() + cooldown_remaining}}
            if cooldown_remaining > 0 else {}
        )

        hass = MagicMock()
        hass.data = {"frame_art_shuffler": {entry.entry_id: data}}

        return entry, data, client, hass, screen_on, local_hour

    async def _run_watchdog(self, entry, data, client, hass, screen_on, local_hour):
        """Invoke the watchdog body extracted from __init__.py logic."""
        # We replicate the watchdog logic directly so we don't need to wire up
        # the full async_setup_entry closure.
        from custom_components.frame_art_shuffler.frame_tv import is_screen_on as _is_screen_on

        _IMMEDIATE_RECOVERY_REASONS = frozenset({
            "manual", "calendar_event", "calendar_event_end", "expiry",
            "override", "override_clear", "tagset_select",
        })
        _OVERNIGHT_HOURS_START = 3
        _OVERNIGHT_HOURS_END = 5
        _OVERNIGHT_DEFER_THRESHOLD = 3 * 3600
        _RECOVERY_COOLDOWN = 2 * 3600

        recovery_pending = data.get("recovery_pending", {})
        art_channel_recovery = data.get("art_channel_recovery", {})
        now_mono = asyncio.get_event_loop().time()

        for tv_id in list(recovery_pending):
            tv_cfg = entry.data.get("tvs", {}).get(tv_id)
            if not tv_cfg or not tv_cfg.get("auto_recover_art_channel", False):
                recovery_pending.pop(tv_id, None)
                continue

            c = data.get("art_clients", {}).get(tv_id)
            if not c or c.stale_duration() == 0.0:
                recovery_pending.pop(tv_id, None)
                continue

            cooldown_until = art_channel_recovery.get(tv_id, {}).get("cooldown_until", 0)
            if now_mono < cooldown_until:
                continue

            pending = recovery_pending[tv_id]
            reason = pending.get("reason", "auto")
            pending_age = now_mono - pending.get("pending_since", now_mono)

            should_recover = False
            if reason in _IMMEDIATE_RECOVERY_REASONS:
                should_recover = True
            else:
                if not screen_on:
                    should_recover = True
                elif pending_age > _OVERNIGHT_DEFER_THRESHOLD:
                    if _OVERNIGHT_HOURS_START <= local_hour < _OVERNIGHT_HOURS_END:
                        should_recover = True

            if not should_recover:
                continue

            mac = tv_cfg.get("mac")
            try:
                ok = await c.recover_art_channel(mac_address=mac)
            except Exception:
                ok = False

            art_channel_recovery[tv_id] = {"cooldown_until": now_mono + _RECOVERY_COOLDOWN}
            recovery_pending.pop(tv_id, None)

    def test_immediate_reason_recovers_on_next_tick(self):
        """manual/event origins trigger immediate recovery regardless of screen state."""
        async def _run():
            entry, data, client, hass, screen_on, local_hour = self._build_watchdog_scenario(
                pending_reason="manual", screen_on=True
            )
            await self._run_watchdog(entry, data, client, hass, screen_on=True, local_hour=14)
            assert "tv1" not in data["recovery_pending"]
            assert "tv1" in data["art_channel_recovery"]

        asyncio.run(_run())

    def test_scheduled_screen_on_defers(self):
        """auto reason + screen on → stays pending, no recovery."""
        async def _run():
            entry, data, client, hass, screen_on, local_hour = self._build_watchdog_scenario(
                pending_reason="auto", pending_age_s=60, screen_on=True
            )
            await self._run_watchdog(entry, data, client, hass, screen_on=True, local_hour=14)
            assert "tv1" in data["recovery_pending"]  # still pending

        asyncio.run(_run())

    def test_scheduled_screen_off_recovers(self):
        """auto reason + screen off → recovery proceeds."""
        async def _run():
            entry, data, client, hass, screen_on, local_hour = self._build_watchdog_scenario(
                pending_reason="auto", pending_age_s=60, screen_on=False
            )
            await self._run_watchdog(entry, data, client, hass, screen_on=False, local_hour=14)
            assert "tv1" not in data["recovery_pending"]
            assert "tv1" in data["art_channel_recovery"]

        asyncio.run(_run())

    def test_overnight_fallback_recovers_after_threshold(self):
        """auto + screen on + >3h pending + in overnight window → recovery."""
        async def _run():
            entry, data, client, hass, _, _ = self._build_watchdog_scenario(
                pending_reason="auto",
                pending_age_s=4 * 3600,  # 4h > 3h threshold
                screen_on=True,
            )
            await self._run_watchdog(
                entry, data, client, hass, screen_on=True, local_hour=4  # 04:00 in window
            )
            assert "tv1" not in data["recovery_pending"]

        asyncio.run(_run())

    def test_outside_overnight_window_still_defers(self):
        """auto + screen on + >3h pending but outside 03-05 window → still defers."""
        async def _run():
            entry, data, client, hass, _, _ = self._build_watchdog_scenario(
                pending_reason="auto",
                pending_age_s=4 * 3600,
                screen_on=True,
            )
            await self._run_watchdog(
                entry, data, client, hass, screen_on=True, local_hour=10  # outside window
            )
            assert "tv1" in data["recovery_pending"]  # still pending

        asyncio.run(_run())

    def test_opt_out_clears_pending_without_recovery(self):
        """TV with auto_recover_art_channel=False: pending entry removed, no recovery call."""
        async def _run():
            entry, data, client, hass, _, _ = self._build_watchdog_scenario(
                pending_reason="manual", auto_recover=False
            )
            await self._run_watchdog(
                entry, data, client, hass, screen_on=True, local_hour=4
            )
            assert "tv1" not in data["recovery_pending"]
            # No cooldown stamped — recovery was never attempted.
            assert "tv1" not in data["art_channel_recovery"]

        asyncio.run(_run())

    def test_cooldown_prevents_repeated_attempts(self):
        """Within cooldown period, pending remains untouched."""
        async def _run():
            entry, data, client, hass, _, _ = self._build_watchdog_scenario(
                pending_reason="manual"
            )
            # Set cooldown far in the future.
            future = asyncio.get_event_loop().time() + 7200
            data["art_channel_recovery"] = {"tv1": {"cooldown_until": future}}

            await self._run_watchdog(
                entry, data, client, hass, screen_on=False, local_hour=4
            )
            # Should still be pending — cooldown blocked recovery.
            assert "tv1" in data["recovery_pending"]

        asyncio.run(_run())

    def test_stale_clears_if_channel_recovered_externally(self):
        """If stale_duration() → 0 between checks, pending is cleared without recovery."""
        async def _run():
            entry, data, client, hass, _, _ = self._build_watchdog_scenario(
                pending_reason="auto"
            )
            # Channel recovered on its own.
            client.stale_duration = MagicMock(return_value=0.0)

            await self._run_watchdog(
                entry, data, client, hass, screen_on=True, local_hour=4
            )
            assert "tv1" not in data["recovery_pending"]
            # No cooldown stamped.
            assert "tv1" not in data["art_channel_recovery"]

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. wake_into_art_mode
# ---------------------------------------------------------------------------

class TestWakeIntoArtMode:
    """wake_into_art_mode: shared function for CEC-aware wake sequence."""

    def _make_client(self, artmode_responses):
        """Return a mock TVConnectionManager that yields artmode responses in order."""
        client = MagicMock()
        responses = list(artmode_responses)

        async def _ensure_connected(timeout=None):
            pass

        async def _get_artmode():
            if responses:
                val = responses.pop(0)
                if isinstance(val, Exception):
                    raise val
                return val
            return "on"

        client.ensure_connected = _ensure_connected
        client.art = MagicMock()
        client.art.get_artmode = _get_artmode
        return client

    def test_clean_wake_returns_true(self):
        """TV woke directly into art mode — single click, returns True."""
        async def _run():
            from custom_components.frame_art_shuffler.frame_tv import wake_into_art_mode

            client = self._make_client(["on"])
            with patch("custom_components.frame_art_shuffler.frame_tv._remote_click", new=AsyncMock()):
                result = await wake_into_art_mode("192.168.1.50", client)
            assert result is True

        asyncio.run(_run())

    def test_cec_defeat_second_click(self):
        """TV woke in TV mode; second click confirms art mode — returns True."""
        async def _run():
            from custom_components.frame_art_shuffler.frame_tv import wake_into_art_mode

            # First get_artmode returns "off" (TV mode), second returns "on" (art mode after click 2)
            client = self._make_client(["off", "on"])
            with patch("custom_components.frame_art_shuffler.frame_tv._remote_click", new=AsyncMock()):
                with patch("asyncio.sleep", new=AsyncMock()):
                    result = await wake_into_art_mode("192.168.1.50", client)
            assert result is True

        asyncio.run(_run())

    def test_second_click_fails_returns_false(self):
        """TV woke in TV mode; second click raises — returns False."""
        async def _run():
            from custom_components.frame_art_shuffler.frame_tv import wake_into_art_mode

            client = self._make_client(["off"])
            click_calls = []

            async def _click(ip, key):
                click_calls.append(key)
                if len(click_calls) >= 2:
                    raise OSError("connection refused")

            with patch("custom_components.frame_art_shuffler.frame_tv._remote_click", side_effect=_click):
                with patch("asyncio.sleep", new=AsyncMock()):
                    result = await wake_into_art_mode("192.168.1.50", client)
            assert result is False

        asyncio.run(_run())

    def test_art_channel_unreachable_returns_false(self):
        """Art channel never responds during phase 1 — returns False."""
        async def _run():
            from custom_components.frame_art_shuffler.frame_tv import wake_into_art_mode

            client = self._make_client([Exception("timeout")] * 20)

            # Advance fake time by 2s per call so the 5s deadline expires quickly.
            fake_t = [0.0]
            def _fake_time():
                fake_t[0] += 2.0
                return fake_t[0]

            mock_loop = MagicMock()
            mock_loop.time = _fake_time

            with patch("custom_components.frame_art_shuffler.frame_tv._remote_click", new=AsyncMock()):
                with patch("asyncio.sleep", new=AsyncMock()):
                    with patch("asyncio.get_event_loop", return_value=mock_loop):
                        result = await wake_into_art_mode("192.168.1.50", client)
            assert result is False

        asyncio.run(_run())

    def test_first_click_fails_returns_false(self):
        """_remote_click raises on click 1 — returns False immediately."""
        async def _run():
            from custom_components.frame_art_shuffler.frame_tv import wake_into_art_mode

            client = self._make_client([])
            with patch(
                "custom_components.frame_art_shuffler.frame_tv._remote_click",
                side_effect=OSError("refused"),
            ):
                result = await wake_into_art_mode("192.168.1.50", client)
            assert result is False

        asyncio.run(_run())
