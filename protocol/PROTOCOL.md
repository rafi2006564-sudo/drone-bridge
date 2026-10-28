# Backend ⇄ RC Bridge protocol, version 1

The ground station backend (client) and the RC Bridge app (server) talk over
one WebSocket on the RC, port `8765` by default. Frames are JSON text.

The source of truth is `drone_protocol/messages.py`. The JSON Schemas in
`schemas/` are generated from it (`python -m drone_protocol.gen_schemas`), and
a test fails if they drift. **Both sides validate every frame against these
schemas.** Validation is strict:

- no unknown fields (`additionalProperties: false`)
- no type coercion (`"1"` is not a number, `0` is not a boolean)
- no `NaN` or `Infinity`
- `type` is required on every frame, and `name` on every command

| Schema file | Direction | Message |
|---|---|---|
| `hello.ground_station.json` | backend → RC | handshake |
| `hello.bridge.json` | RC → backend | handshake reply |
| `heartbeat.json` | backend → RC | liveness, 10 Hz |
| `stick.json` | backend → RC | virtual stick, 20 Hz |
| `command.json` | backend → RC | commands (one sub-schema per `name`) |
| `ack.json` | RC → backend | one per command |
| `telemetry.json` | RC → backend | aircraft state, 10 Hz |
| `event.json` | RC → backend | warnings, mode changes, watchdog actions |

## Connection

- The RC app accepts **one** client. While one is connected, a new connection
  is closed with code `4001`.
- The backend sends `hello` first. The RC app replies with its own `hello`.
  Before the handshake, nothing else is accepted.
- **Version check.** Each side reads `protocol_version` *before* full
  validation, because a peer on another version may send unknown fields. On
  a mismatch the RC app still sends its `hello` (so the backend can log what
  it found), then closes with `4002`. The backend closes with `4002` too.
- A frame that fails validation is closed with `4003` during the handshake,
  and ignored afterwards, with an `event` of code `protocol_error`. A
  command that fails validation but has a readable `id` gets
  `ack {ok: false, error}`.
- WebSocket ping is on (2 s interval, 4 s timeout) on both sides. Per-message
  compression is off (latency).

### Close codes

| Code | Meaning |
|---|---|
| 4001 | another ground station is connected |
| 4002 | protocol version mismatch |
| 4003 | protocol error during the handshake |

## Messages

### `hello` (backend → RC)

```json
{"type": "hello", "protocol_version": 1, "go_home_timeout_s": 10.0}
```

`go_home_timeout_s` (2–60) is the watchdog's go-home timeout. It is sent here
so that every setting lives in the backend's `config.yaml`.

### `hello` (RC → backend)

```json
{"type": "hello", "protocol_version": 1, "msdk_version": "5.x.y",
 "aircraft_model": "...", "aircraft_firmware": "...", "rc_firmware": "..."}
```

### `heartbeat` (backend → RC, 10 Hz)

```json
{"type": "heartbeat", "seq": 42, "t": 1234.567}
```

The backend sends heartbeats **only while the browser UI is alive**. See the
watchdog section.

### `stick` (backend → RC, 20 Hz while virtual stick is enabled)

```json
{"type": "stick", "lx": 0.0, "ly": 0.3, "rx": 0.0, "ry": -0.15, "t": 1234.567}
```

Mode 2, each value from -1.0 to 1.0:

| Field | Stick | Positive means |
|---|---|---|
| `lx` | left x | yaw right (clockwise) |
| `ly` | left y | climb |
| `rx` | right x | roll right |
| `ry` | right y | pitch forward |

The values already include the speed scale. The RC app maps them linearly to
the SDK's stick range.

### `command` (backend → RC)

```json
{"type": "command", "id": "c17", "name": "gimbal_set", "args": {"pitch": -45.0}}
```

`args` is always present. It is `{}` for commands without arguments.

| `name` | `args` |
|---|---|
| `enable_virtual_stick`, `disable_virtual_stick` | `{}` |
| `takeoff`, `land`, `go_home`, `cancel_go_home` | `{}` |
| `gimbal_rotate` | `pitch_rate`, `yaw_rate`: deg/s, -90 to 90. Rates hold for at most 0.5 s, so the backend repeats them while a key is held |
| `gimbal_set` | `pitch`: deg, -90 (down) to 35 |
| `gimbal_reset` | `{}` |
| `camera_photo`, `camera_record_start`, `camera_record_stop` | `{}` |
| `camera_zoom` | `ratio`: 1–200. The RC app clamps or rejects ratios outside the lens's range |
| `camera_lens` | `lens`: `"wide"` or `"zoom"` |
| `mission_upload` | `waypoints` (2 or more: `lat`, `lon`, `alt` m above takeoff, optional `speed`, `actions`), `speed` m/s, `finish_action`: `go_home` \| `hover` \| `land` \| `first_waypoint` |
| `mission_start`, `mission_pause`, `mission_resume`, `mission_stop` | `{}` |
| `set_height_limit`, `set_rth_altitude` | `m`: metres |
| `simulator_start` | `lat`, `lon` |
| `simulator_stop` | `{}` |
| `video_start` | `url`: `rtmp://`, `rtsp://` or `srt://` |
| `video_stop` | `{}` |

Waypoint actions are `{"action": "photo"}` and
`{"action": "gimbal_pitch", "pitch": -90}`.

**There is no command that stops the motors.** The RC app must not implement
one, whatever the protocol later grows (safety item 8).

### `ack` (RC → backend)

```json
{"type": "ack", "id": "c17", "ok": false, "error": "not flying"}
```

Exactly one per command, sent when the SDK call completes or fails. The
backend times out after 5 s (30 s for `mission_upload`).

### `telemetry` (RC → backend, 10 Hz)

Every field is always present. `null` means unknown. Units: metres, m/s,
degrees.

| Field | Notes |
|---|---|
| `seq`, `t` | counter, RC monotonic clock |
| `lat`, `lon`, `alt_rel` | `alt_rel` is relative to the takeoff point |
| `heading` | 0 ≤ h < 360, compass |
| `vel_n`, `vel_e`, `vel_d` | NED. `vel_d` positive is descending |
| `pitch`, `roll`, `yaw` | attitude, `yaw` from -180 to 180 |
| `battery_pct` | 0–100 |
| `gps_sats`, `gps_level` | level as the SDK reports it (0–10) |
| `home_lat`, `home_lon`, `home_set` | |
| `flight_mode` | the SDK flight mode name, as a string |
| `motors_on`, `is_flying`, `virtual_stick_enabled`, `simulator_active` | |
| `gimbal_pitch`, `gimbal_yaw` | |
| `rc_link_quality`, `video_link_quality` | 0–100 |
| `mission_state` | `idle` \| `uploading` \| `ready` \| `executing` \| `paused` \| `unknown` |
| `active_warnings` | *addition:* `[{code, level: warning\|critical, message}]`, from the SDK's health/diagnostics information. The backend blocks takeoff while any is `critical` |
| `camera_recording`, `camera_lens`, `zoom_ratio` | *addition:* camera state for the UI |

The fields marked *addition* are not in the build plan's list. They are
needed for the "no critical warning" pre-takeoff check and for the camera
buttons.

### `event` (RC → backend)

```json
{"type": "event", "level": "warning", "code": "watchdog_hover",
 "message": "no heartbeat for 1 s ...", "t": 1234.5, "data": null}
```

`level` is `info`, `warning` or `critical`. `code` is lower_snake_case, and
`data` is an optional object. Codes so far:

| Code | Level | When |
|---|---|---|
| `watchdog_hover` | warning | 1 s without a heartbeat |
| `watchdog_go_home` | critical | go-home timeout reached while flying |
| `watchdog_go_home_failed` | critical | the go-home call failed |
| `heartbeat_restored` | info | heartbeats resumed after a watchdog action |
| `virtual_stick_overridden` | warning | virtual stick turned off by the aircraft or RC (the pilot took over) |
| `flight_mode_changed` | info | `data: {from, to}` |
| `stale_sticks_dropped` | warning | at most once per second |
| `protocol_error` | warning | an invalid frame was ignored |
| `mission_started` | info | `data: {waypoints}` |
| `mission_progress` | info | a waypoint was reached and its actions run; `data: {waypoint_index, total}` |
| `mission_paused`, `mission_resumed` | info | `data: {waypoint_index}` |
| `mission_paused_by_rc` | warning | the pilot paused the mission with the RC |
| `mission_interrupted` | warning | the mission ended early (stop, land, go home); `data: {waypoint_index}` |
| `mission_finished` | info | the last waypoint was done; `data: {finish_action}` |

## Message age (`t` fields)

`t` in `heartbeat` and `stick` is the backend's monotonic clock in seconds.
The PC and the RC do not share a clock, so the RC app estimates each
message's age like this (`drone_protocol/timing.py` is the reference):

```
sample = now_rc - t
offset = sample                                   on the first message of a connection
offset = min(sample, offset + 0.0002 * elapsed)   afterwards (elapsed = RC time since the last update)
age    = sample - offset
```

`offset` is the fastest delivery seen so far, so `age` is how much later than
that a message arrived. The `0.0002` s/s term (200 ppm) lets the estimate
follow clock drift. It also means a sustained real delay can only hide at
0.2 ms per second. Heartbeats and sticks both update the estimate. Reset it
on every new connection.

## RC app behavior requirements

These mirror `mock-bridge/mock_bridge/watchdog.py`, which is the reference
for the Kotlin `safety/Watchdog`.

1. **Stale sticks.** Drop a `stick` whose age is over **200 ms**. Clamp
   accepted values to [-1, 1], and treat NaN as 0 (the schema already rejects
   NaN; clamp anyway).
2. **Stick hold.** With virtual stick on, if no stick has been accepted for
   **300 ms**, send zero sticks. *Addition:* this covers a backend that keeps
   heartbeating but stops sending sticks.
3. **Hover watchdog.** Arms on the first heartbeat. After **1.0 s** with no
   heartbeat: send zero sticks, disable virtual stick, **pause an executing
   mission**, and send `watchdog_hover`. Pausing the mission keeps "the PC
   is gone, so the aircraft hovers" true during missions too.
4. **Go-home watchdog.** After `go_home_timeout_s` with no heartbeat, if the
   aircraft is flying: go home and send `watchdog_go_home`. It fires only if
   the aircraft was airborne (flying or motors on) when the heartbeat was
   lost, so a pilot who takes off by hand after the PC is gone does not get
   an RTH.
5. Each watchdog action fires **once per outage**. When heartbeats resume,
   send `heartbeat_restored`.
6. **Never re-enable virtual stick on your own** — not after the watchdog,
   and not after the aircraft or RC turned it off. Only an
   `enable_virtual_stick` command turns it on.
7. When virtual stick is turned off by anything other than a
   `disable_virtual_stick` command or the watchdog, send
   `virtual_stick_overridden`.
8. **Missions.** `mission_upload` builds the KMZ with the SDK's WPMZ library
   and uploads it; `mission_state` becomes `ready`. `mission_start` is
   accepted only while hovering in the air with virtual stick off. The
   backend always takes off first and waits for a stable hover, so it does
   not rely on the SDK starting a mission from the ground. Report progress
   with the `mission_*` events.
