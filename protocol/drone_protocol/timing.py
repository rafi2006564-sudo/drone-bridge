"""Message age estimation without synchronized clocks.

The `t` field in heartbeat and stick messages is the sender's monotonic clock.
The PC and the RC do not share a clock, so the receiver cannot compute age as
`now - t` directly. Instead it tracks the offset between the two clocks as the
smallest `now - t` seen so far (the fastest delivery), and measures each
message's age relative to that:

    age = (now - t) - offset

`age` is how much later than the fastest observed delivery this message
arrived. On a LAN the fastest delivery takes a few milliseconds, so a 200 ms
limit on `age` rejects messages that were queued or delayed in transit.

Clock drift between the two devices would slowly bias this, so the offset is
allowed to rise by at most DRIFT_ALLOWANCE seconds per second (200 ppm, well
above crystal drift). A sustained real delay can therefore only hide itself
at 0.2 ms per second, which takes over 15 minutes to mask 200 ms.

The RC app must implement the same algorithm (see PROTOCOL.md).
"""

from __future__ import annotations

DRIFT_ALLOWANCE = 0.0002  # s/s


class AgeEstimator:
    def __init__(self, drift_allowance: float = DRIFT_ALLOWANCE) -> None:
        self._drift = drift_allowance
        self._offset: float | None = None
        self._offset_at: float = 0.0  # local time when _offset was last updated

    def observe(self, sender_t: float, now: float) -> float:
        """Record a message and return its estimated age in seconds (>= 0)."""
        sample = now - sender_t
        if self._offset is None:
            self._offset = sample
        else:
            allowed = self._offset + self._drift * max(0.0, now - self._offset_at)
            self._offset = min(sample, allowed)
        self._offset_at = now
        return sample - self._offset

    def reset(self) -> None:
        """Call on every new connection: the peer's clock may have restarted."""
        self._offset = None
