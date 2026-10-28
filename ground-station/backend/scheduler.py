"""Scheduled mission sorties: take off at a set time and fly a saved mission.

The operator approves a sortie when creating it (build plan amendment A1,
docs/build-plan-amendments.md). At launch time nothing is confirmed again,
so every other safety check runs then:

- the pre-takeoff checks (safety item 6), plus: virtual stick off and no
  mission already running
- the browser UI is open with a live heartbeat (someone is at the station)
- the mission is validated again against the aircraft's actual home point
- the sortie launches only within `launch_window_s` of its time, never later

Launch sequence: mission_upload -> takeoff -> stable hover -> mission_start.
Any failure, a cancel, or loss of the UI aborts the sequence. If the aircraft
is already in the air at that point, the backend commands land. Once the
mission has started, the normal safety behavior applies (UI loss -> RC
watchdog hover and RTH).
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, TypeAdapter

from .mission import upload_args, validate_mission
from .rc_link import CommandResult
from .safety import preflight_failures

if TYPE_CHECKING:
    from .station import GroundStation

ScheduleState = Literal["pending", "launching", "launched", "aborted", "missed", "cancelled"]
MIN_SEPARATION_S = 600.0  # two sorties at least 10 min apart
HOVER_STABLE_S = 1.0  # takeoff counts as done after this long in a steady hover


class Schedule(BaseModel):
    id: str
    mission_id: str
    mission_name: str
    launch_at: float  # Unix time, seconds
    created_at: float
    state: ScheduleState = "pending"
    detail: str | None = None
    warned: bool = False
    finished_at: float | None = None


_schedules = TypeAdapter(list[Schedule])


class SortieAborted(Exception):
    pass


class Scheduler:
    def __init__(self, station: "GroundStation", path: str | Path, wall: Callable[[], float] = time.time) -> None:
        self.gs = station
        self.cfg = station.cfg.schedule
        self.path = Path(path)
        self.wall = wall
        self._active: asyncio.Task[None] | None = None
        self._active_id: str | None = None
        self._cancel_requested: set[str] = set()
        self.schedules: list[Schedule] = self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> list[Schedule]:
        if not self.path.exists():
            return []
        try:
            items = _schedules.validate_json(self.path.read_text(encoding="utf-8"))
        except ValueError:
            self.gs.log.write("gs", {"schedules": "unreadable file ignored", "path": str(self.path)})
            return []
        now = self.wall()
        for s in items:
            if s.state == "launching":
                self._finish(s, "aborted", "the backend restarted during the launch", now, save=False)
            elif s.state == "pending" and now > s.launch_at + self.cfg.launch_window_s:
                self._finish(s, "missed", "the backend was not running at launch time", now, save=False)
        return items

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(_schedules.dump_json(self.schedules, indent=2))
        tmp.replace(self.path)

    def _finish(self, s: Schedule, state: ScheduleState, detail: str, now: float, save: bool = True) -> None:
        s.state = state
        s.detail = detail
        s.finished_at = now
        self.gs.log.write("gs", {"sortie": s.model_dump()})
        if save:
            self._save()
            self.gs.broadcast_schedules()

    def as_json(self) -> list[dict[str, Any]]:
        return [s.model_dump(mode="json") for s in sorted(self.schedules, key=lambda s: s.launch_at)]

    # ------------------------------------------------------------ operator actions

    def create(self, mission_id: str, launch_at: float, confirmed: bool) -> CommandResult:
        if not confirmed:
            return CommandResult(False, "scheduling a sortie requires confirmation")
        mission = self.gs.missions.get(mission_id)
        if mission is None:
            return CommandResult(False, "mission not found")
        now = self.wall()
        if not math.isfinite(launch_at):
            return CommandResult(False, "invalid launch time")
        if launch_at < now + self.cfg.min_lead_s:
            return CommandResult(False, f"launch time must be at least {self.cfg.min_lead_s:g} s from now")
        if launch_at > now + self.cfg.max_horizon_days * 86400:
            return CommandResult(False, f"launch time must be within {self.cfg.max_horizon_days:g} days")
        for other in self.schedules:
            if other.state in ("pending", "launching") and abs(other.launch_at - launch_at) < MIN_SEPARATION_S:
                return CommandResult(False, f"too close to sortie '{other.mission_name}' (keep 10 min apart)")
        report = validate_mission(mission, self.gs.cfg, self.gs.home())
        if not report.ok:
            return CommandResult(False, "mission is not valid: " + "; ".join(report.errors))
        s = Schedule(
            id=uuid.uuid4().hex[:8],
            mission_id=mission.id,
            mission_name=mission.name,
            launch_at=launch_at,
            created_at=now,
            detail=None if report.home_checked else "distance from home will be checked at launch",
        )
        self.schedules.append(s)
        self.gs.log.write("gs", {"sortie": s.model_dump(), "approved": True})
        self._save()
        self.gs.broadcast_schedules()
        return CommandResult(True)

    def cancel(self, schedule_id: str) -> CommandResult:
        s = self._find(schedule_id)
        if s is None:
            return CommandResult(False, "schedule not found")
        if s.state == "pending":
            self._finish(s, "cancelled", "cancelled by the operator", self.wall())
            return CommandResult(True)
        if s.state == "launching":
            self._cancel_requested.add(s.id)
            return CommandResult(True)
        return CommandResult(False, f"sortie is already {s.state}")

    def remove(self, schedule_id: str) -> CommandResult:
        """Delete a finished entry from the list."""
        s = self._find(schedule_id)
        if s is None:
            return CommandResult(False, "schedule not found")
        if s.state in ("pending", "launching"):
            return CommandResult(False, "cancel the sortie first")
        self.schedules.remove(s)
        self._save()
        self.gs.broadcast_schedules()
        return CommandResult(True)

    def references(self, mission_id: str) -> bool:
        return any(s.mission_id == mission_id and s.state in ("pending", "launching") for s in self.schedules)

    def _find(self, schedule_id: str) -> Schedule | None:
        return next((s for s in self.schedules if s.id == schedule_id), None)

    # ------------------------------------------------------------ timing

    async def run(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(0.5)

    def tick(self) -> None:
        now = self.wall()
        for s in self.schedules:
            if s.state != "pending":
                continue
            if now > s.launch_at + self.cfg.launch_window_s:
                self._finish(s, "missed", "launch time passed while the backend could not launch", now)
                self.gs.backend_event("warning", "sortie_missed", f"sortie '{s.mission_name}' missed")
            elif now >= s.launch_at:
                if self._active is not None:
                    self._finish(s, "aborted", "another sortie was launching", now)
                    self.gs.backend_event("critical", "sortie_aborted", f"sortie '{s.mission_name}': another sortie was launching")
                else:
                    s.state = "launching"
                    self._save()
                    self.gs.broadcast_schedules()
                    self._active_id = s.id
                    self._active = asyncio.create_task(self._launch(s))
            elif not s.warned and s.launch_at - now <= self.cfg.warn_before_s:
                s.warned = True
                self.gs.backend_event(
                    "warning",
                    "sortie_imminent",
                    f"sortie '{s.mission_name}' takes off by itself in {s.launch_at - now:.0f} s; pilot to the RC",
                )
                self.gs.broadcast_schedules()

    async def stop(self) -> None:
        if self._active is not None:
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)

    # ------------------------------------------------------------ launch

    async def _launch(self, s: Schedule) -> None:
        takeoff_sent = False
        try:
            self.gs.backend_event("warning", "sortie_launching", f"sortie '{s.mission_name}': launching")
            mission = self.gs.missions.get(s.mission_id)
            if mission is None:
                raise SortieAborted("mission was deleted")
            self._preconditions(s)
            report = validate_mission(mission, self.gs.cfg, self.gs.home())
            if not report.ok or not report.home_checked:
                raise SortieAborted("mission is not valid: " + "; ".join(report.errors or ["home point unknown"]))

            await self._step(s, "mission_upload", upload_args(mission))
            self._preconditions(s)
            takeoff_sent = True  # from here on, an abort lands the aircraft if it left the ground
            await self._step(s, "takeoff")
            await self._wait_stable_hover(s)
            self._guard(s)
            await self._step(s, "mission_start")
            takeoff_sent = False
            self._finish(s, "launched", f"mission started ({report.length_m:.0f} m, ~{report.duration_s / 60:.0f} min)", self.wall())
            self.gs.backend_event("info", "sortie_launched", f"sortie '{s.mission_name}': mission started")
        except SortieAborted as e:
            reason = str(e)
            t = self.gs.telemetry
            if takeoff_sent and (t is None or t.motors_on or t.is_flying):
                r = await self.gs.link.command("land")
                reason += "; landing" if r.ok else f"; LAND FAILED ({r.error}): take over with the RC"
            self._finish(s, "aborted", reason, self.wall())
            self.gs.backend_event("critical", "sortie_aborted", f"sortie '{s.mission_name}' aborted: {reason}")
        finally:
            self._cancel_requested.discard(s.id)
            self._active = None
            self._active_id = None

    def _guard(self, s: Schedule) -> None:
        """Conditions that must hold throughout the launch sequence."""
        if s.id in self._cancel_requested:
            raise SortieAborted("cancelled by the operator")
        if not self.gs.link.ready:
            raise SortieAborted("link to the RC lost")
        if not self.gs.ui_monitor.alive(self.gs.clock()):
            raise SortieAborted("no browser UI open (heartbeat missing)")

    def _preconditions(self, s: Schedule) -> None:
        self._guard(s)
        gs = self.gs
        reasons = preflight_failures(gs.cfg, gs.telemetry, gs.telemetry_age(), gs.limits_applied)
        t = gs.telemetry
        if t is not None:
            if t.virtual_stick_enabled:
                reasons.append("virtual stick is enabled")
            if t.mission_state in ("executing", "paused"):
                reasons.append("a mission is already running")
        if reasons:
            raise SortieAborted("; ".join(reasons))

    async def _step(self, s: Schedule, name: str, args: dict[str, Any] | None = None) -> None:
        self._guard(s)
        r = await self.gs.link.command(name, args)
        if not r.ok:
            raise SortieAborted(f"{name} failed: {r.error}")

    async def _wait_stable_hover(self, s: Schedule) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.cfg.takeoff_timeout_s
        stable_since: float | None = None
        while True:
            self._guard(s)
            t = self.gs.telemetry
            now = loop.time()
            steady = (
                t is not None
                and t.is_flying
                and t.alt_rel is not None
                and t.alt_rel >= 1.0
                and t.vel_d is not None
                and abs(t.vel_d) < 0.3
            )
            if steady:
                stable_since = stable_since if stable_since is not None else now
                if now - stable_since >= HOVER_STABLE_S:
                    return
            else:
                stable_since = None
            if now > deadline:
                raise SortieAborted(f"no stable hover within {self.cfg.takeoff_timeout_s:g} s of takeoff")
            await asyncio.sleep(0.1)
