## Known Issues

### Toggle Orientation button — unverified
Sends the key sequence documented in the samsungtvws examples (KEY_MUTI_VIEW long press
for 2023 and earlier; KEY_HOME long press for 2024+). This may be intended for use with
motorized rotating mounts rather than triggering a TV firmware orientation change. Cannot
be verified without a mount to test against.

## Future Investigations

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