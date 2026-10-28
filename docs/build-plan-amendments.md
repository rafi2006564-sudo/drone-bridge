# Build plan amendments

Changes to the build plan's safety requirements (section 6), with the reason
and what replaces the original protection. Each was decided by the operator.

## A1: Scheduled sorties launch without a confirmation at launch time

**Date:** 2026-10-29. **Changes:** safety item 5 ("Takeoff, enabling virtual
stick, and starting a mission each need a confirmation step in the UI").

**What changed.** A scheduled sortie takes off and starts its mission at a set
time with no confirmation at that moment. The operator confirms once, when
creating the schedule. The dialog states that the aircraft will take off by
itself. Manual takeoff, enabling virtual stick, and manual mission start
still need a confirmation every time.

**What replaces it.** At launch time the backend requires all of these, or
the sortie is aborted with no takeoff:

- every pre-takeoff check of safety item 6 (GPS, home point, battery, no
  critical warning, fresh telemetry, limits acked by the aircraft, not
  already flying)
- the browser UI open with a live heartbeat, so someone is at the ground
  station
- virtual stick off, and no mission already running
- the mission valid against `config.yaml` limits and the aircraft's actual
  home point (re-checked at launch, not only when it was scheduled)
- the launch happening within `schedule.launch_window_s` (default 60 s) of
  the set time. A sortie is never launched late. If the PC was asleep or the
  backend was down, it is marked missed.

Also:

- **Warning.** A countdown banner with a Cancel button shows for
  `schedule.warn_before_s` (default 60 s) before launch, and during the
  launch.
- **Launch sequence.** The sequence is upload, takeoff, stable hover, then
  mission start. A cancel, a failed step, UI heartbeat loss, or RC link loss
  aborts it. If the aircraft has left the ground by then, the backend
  commands land.
- **After the mission starts**, the normal rules apply. UI loss makes the RC
  watchdog pause the mission and hover, then go home after the timeout; the
  pilot can pause or take over with the RC.
- **Scheduling limits.** At most 7 days ahead (`schedule.max_horizon_days`),
  at least `schedule.min_lead_s` ahead, and sorties at least 10 minutes
  apart.
- **Editing.** A mission used by a pending sortie cannot be edited or
  deleted.
- **Logging.** Every schedule creation (approval), cancellation, and outcome
  is in the session log.

**Unchanged:** the pilot holds the RC and must be present at launch time
(build plan section 2 and the operator's regulatory obligations). This is
not an unattended or docked operation.
