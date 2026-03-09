## Stale Art Channel Connection

### Symptom

All art-channel buttons (Shuffle, Shuffle Silently, Art Mode) stop working after the
integration has been running for some hours. A manual integration reload via the HA UI
fixes the problem immediately. The TV is on, art is displaying, ping and the REST API
respond normally — only the WebSocket art channel is unresponsive.

### Root cause

The Samsung Frame TV exposes its art channel over a persistent WebSocket connection to
the endpoint `wss://{ip}:8002/api/v2/channels/com.samsung.art-app`.

The WebSocket transport layer (ping/pong, connection state) is managed by the TV's
general WebSocket server. The art application that actually processes `art_app_request`
commands runs on top of that transport. These are independent components.

When the TV's art application resets or enters a sleep state (firmware-managed, not
user-visible), it can stop processing art commands while the underlying WebSocket
connection remains alive. The TV's WebSocket server continues responding to
protocol-level pings, so the client-side `connection.state` never becomes CLOSED and
`is_alive()` keeps returning True.

The websockets library 16.0 default is `ping_interval=20, ping_timeout=20` — the
integration sends a WebSocket ping every 20 seconds and would close the connection if
no pong was received within 20 seconds. But since the TV's WebSocket SERVER responds
to pings (not the art app), the pings succeed and the connection appears healthy
indefinitely, even after the art app has reset.

The consequence: `TVConnectionManager.ensure_connected()` sees `is_alive() = True` and
returns without reconnecting. Subsequent art requests are sent on the stale connection
but no responses arrive. `wait_for_response` times out, `get_artmode()` raises
`AssertionError`, and the button press fails.

This scenario most commonly occurs overnight or after extended periods of inactivity,
when firmware-managed power transitions silently reset the art application.

### Evidence from related projects

Several community projects have documented related behaviours on Samsung Frame TVs,
confirming that art channel WebSocket connections can become non-functional without
the transport layer indicating a problem:

- Art mode commands hang while the connection appears open:
  https://github.com/xchwarze/samsung-tv-ws-api/issues/108

- Art upload fails despite an established `com.samsung.art-app` connection:
  https://github.com/xchwarze/samsung-tv-ws-api/issues/130

- Art channel socket enters a rapid open/close loop on Tizen 6.5:
  https://github.com/tavicu/homebridge-samsung-tizen/issues/519

- Frame 2021: art channel WebSocket closes after a few seconds and stops recognizing
  events, making persistent connections unreliable:
  https://github.com/Toxblh/node-red-contrib-samsung-tv-control/issues/47

- `WebSocketConnectionClosedException: Connection is already closed` thrown on an
  art channel that reports as connected:
  https://github.com/ollo69/ha-samsungtv-smart/issues/36

Research in the `ollo69/ha-samsungtv-smart` codebase also shows that some
implementations set a ping interval of 3600 seconds (1 hour) because the TV itself
sends pings at that interval. This confirms that the TV-side WebSocket server is the
ping/pong counterpart, not the art application, and that transport-layer keep-alive
gives no signal about art-application health.

### Fix

`TVConnectionManager.ensure_connected()` was modified to probe the art channel with a
lightweight `get_artmode()` request whenever the connection appears alive. If the probe
times out or fails for any reason, the connection is closed and a fresh one is opened
before returning.

This adds at most ~100 ms overhead per `ensure_connected()` call when the connection
is healthy (normal round-trip to TV). When the connection is stale it adds ~2 s
(probe timeout) before triggering the reconnect, which typically succeeds in under 1 s.

All callers of `ensure_connected()` benefit automatically without any changes.

See `TVConnectionManager.ensure_connected()` in `frame_tv.py`.