"""Bridge-side watchdog (safety item 1).

This is the reference implementation for the RC app's safety/Watchdog class:
plain logic, no I/O, driven by an explicit clock so it can be unit tested.

- No heartbeat for HOVER_TIMEOUT_S: zero sticks, disable virtual stick, event.
- No heartbeat for go_home_timeout_s, aircraft flying: go home, event.
  Fires only if the aircraft was airborne (flying or motors on) when the
  heartbeat was lost, so a pilot who takes off by hand after the PC is gone
  does not get an unexpected RTH.
- Each action fires once per outage. When heartbeats resume, a RESTORED
  action is reported; virtual stick is never re-enabled automatically.
- The watchdog arms on the first heartbeat, so an RC app that never had a
  ground station connected does nothing.
"""

from __future__ import annotations

from enum import Enum

HOVER_TIMEOUT_S = 1.0
STICK_MAX_AGE_S = 0.2  # older stick messages are dropped (safety item 3)
STICK_HOLD_S = 0.3  # no accepted stick for this long -> sticks go to zero


class Action(Enum):
    HOVER = "hover"
    GO_HOME = "go_home"
    RESTORED = "restored"


class BridgeWatchdog:
    def __init__(self, go_home_timeout_s: float = 10.0) -> None:
        self.go_home_timeout_s = go_home_timeout_s
        self._last_heartbeat: float | None = None
        self._hover_fired = False
        self._go_home_fired = False
        self._airborne_at_loss = False

    @property
    def armed(self) -> bool:
        return self._last_heartbeat is not None

    def heartbeat(self, now: float) -> list[Action]:
        restored = self._hover_fired
        self._last_heartbeat = now
        self._hover_fired = False
        self._go_home_fired = False
        return [Action.RESTORED] if restored else []

    def check(self, now: float, is_flying: bool, motors_on: bool) -> list[Action]:
        if self._last_heartbeat is None:
            return []
        gap = now - self._last_heartbeat
        actions = []
        if gap >= HOVER_TIMEOUT_S and not self._hover_fired:
            self._hover_fired = True
            self._airborne_at_loss = is_flying or motors_on
            actions.append(Action.HOVER)
        if (
            gap >= self.go_home_timeout_s
            and is_flying
            and self._airborne_at_loss
            and not self._go_home_fired
        ):
            self._go_home_fired = True
            actions.append(Action.GO_HOME)
        return actions
