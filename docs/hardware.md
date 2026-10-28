# Hardware notes

Fill this in during Phases 0–5. Leave nothing as "probably": record what was
observed, when, and on which firmware.

## Versions

| Item | Version | Date checked |
|---|---|---|
| Aircraft (Matrice 4E) firmware | | |
| RC Plus 2 Enterprise firmware | | |
| DJI Pilot 2 version on the RC | | |
| MSDK V5 version pinned in Gradle | | |
| MSDK release note that lists the firmware above | (link) | |

## Stopping DJI Pilot 2 from taking the connection

Steps verified on the RC Plus 2 (Phase 0):

1. _TODO_

How to confirm Pilot 2 is not running or holding the aircraft:

- _TODO_

## RC override (safety item 4)

Which RC actions exit virtual stick on the Matrice 4E. Test each in the MSDK
simulator first (Phase 4), then in the air (Phase 5).

| RC action | Exits virtual stick? | Event seen in the UI | Tested (sim / air, date) |
|---|---|---|---|
| Flight mode switch (N/S/F) | | | |
| Pause / brake button | | | |
| RTH button | | | |
| Moving the sticks | | | |

## Video latency (Phase 3)

Glass-to-glass, measured by filming a stopwatch shown next to the browser.

| Path | Lens | Resolution | Latency | Date |
|---|---|---|---|---|
| Option A: MSDK live stream → MediaMTX → WebRTC | | | | |

If latency is above about 500 ms, keep the manual speed scale low and use the
RC screen as the primary view during manual flight.
