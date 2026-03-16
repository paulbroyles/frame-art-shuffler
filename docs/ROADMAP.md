## Known Issues

### Toggle Orientation button — unverified
Sends the key sequence documented in the samsungtvws examples (KEY_MUTI_VIEW long press
for 2023 and earlier; KEY_HOME long press for 2024+). This may be intended for use with
motorized rotating mounts rather than triggering a TV firmware orientation change. Cannot
be verified without a mount to test against.

## Future Investigations

### REST API intermittent failure (Python aiohttp vs. curl discrepancy)

**Observed (2026-03-16)**: The TV's REST API (`https://<ip>:8002/api/v2/`) responded correctly to `curl -sk` from the HA box but `_rest_device_info()` (Python aiohttp) returned None for ~20 minutes, logging "Screen status check failed (no REST response)" every 10 seconds. The failure resolved spontaneously when the TV transitioned state.

**Impact**: With REST returning None, `_check_rest_state` previously returned `(False, False)`, causing `select_and_cleanup` to conclude the network was down and send a WoL packet. Since the network was actually up, the WoL acted as a second packet and woke the TV screen unexpectedly during a "silent" shuffle.

**Mitigations already in place**:
- TCP fallback in `_check_rest_state`: if REST fails but a raw TCP connect to port 8002 succeeds, returns `(True, False)` (network-awake, screen-unknown) → WoL skipped
- Fixed variable bug in `select_and_cleanup`: was passing `pre_screen_on` instead of `pre_network_awake` to `_ensure_tv_reachable`
- `_rest_device_info` now logs the actual exception instead of silently swallowing it

**Root cause unknown**: Possible candidates — aiohttp `verify_ssl=False` deprecation (TypeError swallowed), TV's HTTP server throttling/refusing Python connections while accepting curl, transient socket exhaustion from rapid ClientSession creation. The debug logging added to `_rest_device_info` will help identify the exception on next occurrence.

**Potential improvements**:
- Investigate whether `verify_ssl=False` should be replaced with `ssl=False` in `async_rest.py` for newer aiohttp compatibility
- Consider reusing a single aiohttp `ClientSession` per integration lifetime rather than creating/destroying one per REST call (reduces socket churn, may prevent TV-side connection limits)
- Add a retry (2–3 attempts, 500ms apart) before concluding REST is unavailable



### Preemptive art channel reconnection
Currently `ensure_connected()` detects a stale art channel connection reactively — on
the first button press after the TV's art app has reset, the probe times out (up to 2 s)
before the reconnect happens. This is correct and safe, but the first button press of
the day is slower than subsequent ones.

Investigate whether there is a low-cost way to detect and re-establish a stale connection
proactively, before any user request arrives. Possible approaches:

- **Periodic heartbeat task**: A background asyncio task that calls `ensure_connected()`
  (or a lightweight art probe) on a schedule, e.g. every 30–60 minutes. Would keep the
  connection warm and surface staleness before any button press.
- **TV event-driven reconnect**: Listen for TV power/wakeup events (already partially
  handled via `process_event`) and trigger a reconnect when the TV wakes, since art app
  resets often coincide with wake events.
- **Connection lifecycle callback**: Hook into the recv loop exit (when the art app
  closes the WebSocket cleanly) to schedule an immediate reconnect attempt rather than
  waiting for the next user request.

See `TVConnectionManager.ensure_connected()` in `frame_tv.py` and
`docs/STALE_ART_CHANNEL.md` for context.