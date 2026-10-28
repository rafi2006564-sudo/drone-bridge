"""Safety logic with no I/O, so it can be unit tested with a fake clock."""

from __future__ import annotations

import math
from dataclasses import dataclass

from drone_protocol.messages import Telemetry

from .config import Config

# Commands the UI must confirm (safety item 5). The backend refuses them
# without `confirmed: true`, so a UI bug cannot skip the dialog silently.
CONFIRM_REQUIRED = frozenset({"takeoff", "enable_virtual_stick", "mission_start"})


class HeartbeatMonitor:
    """Tracks whether a peer has sent a heartbeat within `timeout` seconds."""

    def __init__(self, timeout: float) -> None:
        self.timeout = timeout
        self._last: float | None = None

    def beat(self, now: float) -> None:
        self._last = now

    def lost(self) -> None:
        """The peer disconnected: treat it as dead right away."""
        self._last = None

    def alive(self, now: float) -> bool:
        return self._last is not None and now - self._last <= self.timeout


def clamp_unit(v: float) -> float:
    """Clamp to [-1, 1]. NaN and infinities become 0."""
    if not math.isfinite(v):
        return 0.0
    return max(-1.0, min(1.0, v))


@dataclass(frozen=True)
class Sticks:
    lx: float = 0.0  # yaw
    ly: float = 0.0  # throttle
    rx: float = 0.0  # roll
    ry: float = 0.0  # pitch

    def scaled(self, scale: float) -> "Sticks":
        s = max(0.0, min(1.0, scale)) if math.isfinite(scale) else 0.0
        return Sticks(*(clamp_unit(clamp_unit(v) * s) for v in (self.lx, self.ly, self.rx, self.ry)))


ZERO_STICKS = Sticks()


def preflight_failures(
    cfg: Config,
    telemetry: Telemetry | None,
    telemetry_age: float | None,
    limits_applied: bool,
) -> list[str]:
    """Reasons takeoff must be refused (safety item 6). Empty list = clear."""
    if telemetry is None or telemetry_age is None:
        return ["no telemetry from the aircraft"]
    reasons = []
    if telemetry_age > cfg.rc.telemetry_stale_s:
        reasons.append(f"telemetry is stale ({telemetry_age:.1f} s old)")
    if telemetry.gps_sats < cfg.preflight.min_gps_sats:
        reasons.append(f"GPS satellites {telemetry.gps_sats} < {cfg.preflight.min_gps_sats}")
    if not telemetry.home_set:
        reasons.append("home point not set")
    if telemetry.battery_pct is None:
        reasons.append("battery level unknown")
    elif telemetry.battery_pct < cfg.preflight.min_battery_pct:
        reasons.append(f"battery {telemetry.battery_pct}% < {cfg.preflight.min_battery_pct}%")
    critical = [w.code for w in telemetry.active_warnings if w.level == "critical"]
    if critical:
        reasons.append("critical warning active: " + ", ".join(critical))
    if not limits_applied:
        reasons.append("height limit / RTH altitude not confirmed by the aircraft")
    if telemetry.is_flying:
        reasons.append("aircraft is already flying")
    return reasons
