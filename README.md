# drone-bridge

PC ground station for a DJI Matrice 4E. The PC talks over local Wi-Fi to a
custom Android app on the DJI RC Plus 2 Enterprise (MSDK V5), and that app
flies the aircraft. The RC stays in the pilot's hands as the radio link and
the override.

```
Browser UI ⇄ backend (FastAPI, this PC) ⇄ RC Bridge app (RC Plus 2) ⇄ aircraft
```

**Status:** Phase 1 plus missions and scheduled sorties (Phase 6 backend and
UI), all against the mock bridge. The RC app (Phase 2+) is not written yet.

## Quick start (mock aircraft)

Requires Python 3.11 or newer.

```sh
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -e ".[test]"
copy config.example.yaml config.yaml   # cp on macOS/Linux

python -m mock_bridge           # terminal 1: simulated aircraft on :8765
python -m ground_station        # terminal 2: UI on http://127.0.0.1:8000/
```

Open http://127.0.0.1:8000/. Wait about 6 s for the mock's GPS to lock, then:

1. **Take off** (confirm in the dialog).
2. **Enable virtual stick** (confirm).
3. Fly with the keyboard:

| Key | Action |
|---|---|
| `W` / `S` | climb / descend |
| `A` / `D` | yaw left / right |
| arrow keys | pitch and roll |
| `R` / `F` | tilt the gimbal up / down |
| `Space` | release all keys |

Close the tab and the mock hovers. Its console prints `watchdog_hover`.

The mock console takes fault-injection commands; type `help` to list them.
`override` simulates the pilot taking over with the RC, `battery 20` and
`gps 4` degrade the aircraft, and `warn imu_error critical` raises a
critical warning.

## Missions and scheduled sorties

- **Create a mission.** In the Missions panel, press **New** and click the
  map to add waypoints; drag them to move. Set altitude, speed, photo and
  gimbal pitch per waypoint, and what to do at the end. The editor validates
  as you go: distance from home, height limit, minimum altitude, speed and
  waypoint count. It shows the length and estimated time. **Save** keeps
  missions in `data/missions/`.
- **Fly it now.** **Upload** (validated against the actual home point
  again), take off, then **Start** (confirm). Pause, Resume and Stop work in
  flight.
- **Schedule it.** Pick a date and time in the Scheduled sorties panel and
  press **Schedule**. The confirmation is the pre-approval: at that time the
  aircraft takes off by itself and flies the mission, if every pre-flight
  check passes and this page is open. A countdown banner with Cancel shows
  for the last minute. Schedules live in `data/schedules.json`.

This changes safety item 5 of the build plan. See
`docs/build-plan-amendments.md` for exactly what the launch checks are and
when a sortie aborts.

## Tests

```sh
pytest
```

The suite covers schema validation, watchdog timing on both sides, stick
clamping and scaling, the message-age estimator, pre-takeoff checks, command
gating, and integration of the backend with the mock over real WebSockets.
The integration tests cover tab close → hover, backend killed → hover then
RTH, RC override, stale sticks, one client only, and the version mismatch.

After changing `protocol/drone_protocol/messages.py`, regenerate the schemas:

```sh
python -m drone_protocol.gen_schemas
```

## Layout

| Path | What |
|---|---|
| `protocol/` | `PROTOCOL.md`, JSON Schemas, and the shared Python models (`drone_protocol`) |
| `ground-station/backend/` | the backend (`ground_station` package): RC link, safety, logging |
| `ground-station/web/` | the UI: plain HTML/JS. Leaflet is vendored in `vendor/` (works offline) |
| `ground-station/tests/` | pytest suite |
| `mock-bridge/` | simulated aircraft speaking the RC app's protocol (`mock_bridge` package) |
| `docs/` | hardware notes, setup, test checklists, build plan amendments |
| `data/` | saved missions and schedules (gitignored) |
| `logs/` | one JSON Lines file per backend session (gitignored) |

## Safety summary

See section 6 of the build plan and `protocol/PROTOCOL.md`.

- The backend relays heartbeats only while the UI heartbeat is alive (0.5 s
  timeout). The RC watchdog hovers after 1 s without heartbeats, and goes
  home after 10 s if flying.
- Takeoff, enabling virtual stick, and starting a mission need confirmation.
  The backend also refuses them without the confirmation flag. The exception
  is scheduled sorties, approved when created
  (`docs/build-plan-amendments.md`).
- Takeoff is refused unless GPS, the home point, battery, critical warnings,
  fresh telemetry, and the aircraft's ack of the height limit and RTH
  altitude all check out.
- There is no motor-stop command anywhere in the protocol.
- The UI WebSocket only accepts the page served by the backend (Origin
  check), so another website open in the browser cannot send commands.
- Keep the UI tab in front. Browsers throttle hidden tabs, which stops the
  heartbeat; the aircraft then hovers and virtual stick turns off.
