# Test checklists

Tick each item, and note the date and the session log file name.

## Phase 1: mock bridge (no hardware)

Run `pytest` first; all tests must pass. Then, by hand with
`python -m mock_bridge` and `python -m ground_station`:

- [ ] The UI loads with the pills showing backend, RC connected, telemetry,
      limits set.
- [ ] Before GPS lock (first ~6 s, or `gps 4` in the mock console), the
      pre-flight pill shows "takeoff blocked" and its tooltip lists the reasons.
- [ ] Take off asks for confirmation; Cancel sends nothing.
- [ ] Takeoff succeeds once the checks pass; the map shows aircraft and home.
- [ ] Enable virtual stick asks for confirmation.
- [ ] W/S/A/D and the arrow keys move the aircraft. Releasing the keys
      brings it to a stop.
- [ ] The speed scale slider changes the speed (30% ≈ 3 m/s in the mock).
- [ ] Switching to another window releases all keys (no stuck stick).
- [ ] **Closing the tab makes the mock hover** within ~1.5 s
      (`watchdog_hover` in the mock console).
- [ ] Reopening the tab does **not** re-enable virtual stick.
- [ ] `override` in the mock console disables virtual stick and the UI logs
      `virtual_stick_overridden`.
- [ ] A second tab shows "Not in control" and cannot send commands.
- [ ] `warn imu_error critical` blocks takeoff; `clear` releases it.
- [ ] **Stopping the backend (Ctrl+C) in flight:** the mock hovers after 1 s
      and goes home after 10 s.
- [ ] Return home and Land work; the log file in `logs/` has the commands,
      acks, events and telemetry.

## Phase 6 (mock): missions and scheduled sorties

- [ ] Create a mission on the map. The report flags a waypoint beyond
      `max_distance_from_home_m`, one above the height limit, and one below
      `min_mission_alt_m`.
- [ ] Save it and reload the page: it is still listed.
- [ ] Upload is refused for an invalid mission; a valid one uploads
      (`mission_state` ready).
- [ ] Take off, Start (confirm): progress events per waypoint, and the
      finish action happens.
- [ ] Pause, Resume and Stop work; `override` in the mock console pauses
      the mission.
- [ ] Closing the tab during a mission pauses it (`watchdog_hover`), then
      goes home after 10 s.
- [ ] Schedule a sortie 2 minutes ahead: the banner counts down from 60 s,
      and at the time it takes off and flies the mission with no dialog.
- [ ] Cancel from the banner before launch: nothing happens. Cancel during
      the launch: the aircraft lands.
- [ ] Sortie with the tab closed at launch time: aborted, no takeoff.
- [ ] Sortie with `battery 20` in the mock: aborted, no takeoff.
- [ ] Stop the backend over a sortie's launch time and start it again
      later: the sortie shows missed and does not launch.

## Phase 2: RC app, connection and telemetry (propellers removed, no motors)

- [ ] _to be written with Phase 2_

## Phase 4: commands in the MSDK simulator (propellers removed)

- [ ] Every command in PROTOCOL.md works in the simulator.
- [ ] Killing the backend: the aircraft hovers, then returns home after the
      timeout.
- [ ] Each RC override action from `docs/hardware.md` disables virtual stick
      and the event is reported.

## Phase 5: first real flights

- [ ] _to be written before Phase 5: site, weather limits, pilot, abort
      criteria_

## Phase 6 (simulator, then real flight): missions and sorties

- [ ] Every item of the mock checklist above, in the MSDK simulator.
- [ ] A scheduled sortie in the simulator, with the pilot at the RC.
- [ ] In the air: a short mission, then a scheduled sortie, with the pilot
      ready to take over.
