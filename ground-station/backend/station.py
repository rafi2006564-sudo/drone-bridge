"""GroundStation: the one place where UI input becomes RC traffic.

Safety behavior implemented here:
- Heartbeats go to the RC only while the UI heartbeat is alive (item 2).
- When the UI heartbeat is lost, a zero stick is sent at once, and zero
  sticks follow while virtual stick is still on. Without heartbeats the RC
  watchdog then disables virtual stick, so the aircraft hovers.
- Sticks are clamped and scaled by the speed scale (items 3, 7).
- Takeoff passes pre-flight checks (item 6); confirm-required commands need
  `confirmed: true` (item 5).
- Height limit and RTH altitude are set on every connect (item 7).
- Missions are validated against config limits and the actual home point
  before every upload (section 8).
- Everything is logged (item 10).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from drone_protocol.messages import (
    COMMAND_ARGS,
    Event,
    Heartbeat,
    HelloFromBridge,
    Stick,
    Telemetry,
)

from .config import Config
from .mission import MissionStore, mission_json, parse_mission_in, upload_args, validate_mission
from .rc_link import CommandResult, RcLink
from .safety import CONFIRM_REQUIRED, ZERO_STICKS, HeartbeatMonitor, Sticks, preflight_failures
from .scheduler import Scheduler
from .session_log import SessionLog
from .survey import SurveyError, SurveyIn, generate_survey
from .ui_messages import UiCommand, UiHeartbeat, UiOp, UiSetSpeedScale, UiStick, parse_from_ui

log = logging.getLogger(__name__)

# Set from config at connect time only, never from the UI.
BACKEND_ONLY = frozenset({"set_height_limit", "set_rth_altitude"})

UI_QUEUE_SIZE = 500


class GroundStation:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.clock = clock
        self.log = SessionLog(cfg.logging.dir)
        self.link = RcLink(
            cfg,
            self.log,
            on_telemetry=self._on_telemetry,
            on_event=self._on_bridge_event,
            on_connection=self._on_connection,
            on_backend_event=self.backend_event,
        )
        self.ui_monitor = HeartbeatMonitor(cfg.safety.ui_heartbeat_timeout_s)
        self._ui_was_alive = False
        self._ui_queue: asyncio.Queue[dict[str, Any]] | None = None
        self._ui_sticks = ZERO_STICKS
        self._ui_sticks_at: float | None = None
        self.speed_scale = cfg.safety.speed_scale_default
        self.telemetry: Telemetry | None = None
        self._telemetry_at: float | None = None
        self.limits_applied = False
        self._generation = 0  # bumps on every RC (re)connect
        self._hb_seq = 0
        self._next_heartbeat = 0.0
        self._tasks: list[asyncio.Task[Any]] = []
        self._background: set[asyncio.Task[Any]] = set()
        storage = Path(cfg.storage.dir)
        self.missions = MissionStore(storage / "missions")
        self.scheduler = Scheduler(self, storage / "schedules.json")

    # ------------------------------------------------------------------
    # lifecycle

    async def start(self) -> None:
        log.info("session log: %s", self.log.path)
        self.log.write("gs", {"session": "start", "config": self.cfg.model_dump()})
        self._tasks = [
            asyncio.create_task(self.link.run(), name="rc-link"),
            asyncio.create_task(self._control_loop(), name="control"),
            asyncio.create_task(self._status_loop(), name="status"),
            asyncio.create_task(self.scheduler.run(), name="scheduler"),
        ]

    async def stop(self) -> None:
        await self.scheduler.stop()
        for t in self._tasks + list(self._background):
            t.cancel()
        await asyncio.gather(*self._tasks, *self._background, return_exceptions=True)
        self.log.write("gs", {"session": "stop"})
        self.log.close()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ------------------------------------------------------------------
    # UI session (one controlling browser tab at a time)

    def claim_ui(self) -> asyncio.Queue[dict[str, Any]] | None:
        if self._ui_queue is not None:
            return None
        self._ui_queue = asyncio.Queue(UI_QUEUE_SIZE)
        self._ui_sticks = ZERO_STICKS
        self._ui_sticks_at = None
        self.log.write("gs", {"ui": "connected"})
        self._to_ui({"type": "hello", "config": self._ui_config()})
        self._to_ui(self.status())
        self.broadcast_missions()
        self.broadcast_schedules()
        return self._ui_queue

    def release_ui(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if self._ui_queue is queue:
            self._ui_queue = None
            self.ui_monitor.lost()
            self._ui_sticks = ZERO_STICKS
            self._ui_sticks_at = None
            self.log.write("gs", {"ui": "disconnected"})

    def handle_ui(self, raw: str) -> None:
        try:
            msg = parse_from_ui(raw)
        except ValidationError as e:
            self.log.write("ui>gs", {"invalid": raw[:2000], "error": str(e)})
            self.backend_event("warning", "ui_protocol_error", f"invalid message from UI: {e.errors()[0]['msg']}")
            return
        now = self.clock()
        if isinstance(msg, UiHeartbeat):
            self.ui_monitor.beat(now)
        elif isinstance(msg, UiStick):
            self._ui_sticks = Sticks(msg.lx, msg.ly, msg.rx, msg.ry)
            self._ui_sticks_at = now
        elif isinstance(msg, UiSetSpeedScale):
            self.speed_scale = min(msg.value, self.cfg.safety.speed_scale_max)
            self.log.write("ui>gs", msg, applied=self.speed_scale)
            self._to_ui(self.status())
        elif isinstance(msg, UiCommand):
            self.log.write("ui>gs", msg)
            self._spawn(self._ui_command(msg))
        elif isinstance(msg, UiOp):
            self.log.write("ui>gs", msg)
            reply = self._ui_op(msg)
            self.log.write("gs>ui", reply)
            self._to_ui(reply)

    def _to_ui(self, msg: dict[str, Any]) -> None:
        q = self._ui_queue
        if q is None:
            return
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            pass  # a stalled tab; it will be dropped by its heartbeat timeout

    def _ui_config(self) -> dict[str, Any]:
        c = self.cfg
        return {
            "speed_scale_default": c.safety.speed_scale_default,
            "speed_scale_max": c.safety.speed_scale_max,
            "deadband": c.safety.ui_deadband,
            "height_limit_m": c.limits.height_limit_m,
            "rth_altitude_m": c.limits.rth_altitude_m,
            "min_gps_sats": c.preflight.min_gps_sats,
            "min_battery_pct": c.preflight.min_battery_pct,
            "min_mission_alt_m": c.limits.min_mission_alt_m,
            "max_mission_speed_ms": c.limits.max_mission_speed_ms,
            "max_distance_from_home_m": c.limits.max_distance_from_home_m,
            "schedule": c.schedule.model_dump(),
            "confirm_required": sorted(CONFIRM_REQUIRED),
            "map": c.map.model_dump(),
            "simulator": c.simulator.model_dump(),
            "video_player_url": c.video.player_url,
        }

    def backend_event(self, level: str, code: str, message: str) -> None:
        self.log.write("gs", {"event": {"level": level, "code": code, "message": message}})
        self._to_ui({"type": "event", "source": "backend", "level": level, "code": code, "message": message})

    # ------------------------------------------------------------------
    # commands from the UI

    async def _ui_command(self, cmd: UiCommand) -> None:
        result = await self.execute(cmd.name, dict(cmd.args), cmd.confirmed)
        reply = {"type": "ack", "id": cmd.id, "name": cmd.name, "ok": result.ok, "error": result.error}
        self.log.write("gs>ui", reply)
        self._to_ui(reply)

    async def execute(self, name: str, args: dict[str, Any], confirmed: bool) -> CommandResult:
        if name not in COMMAND_ARGS:
            return CommandResult(False, f"unknown command {name!r}")
        if name in BACKEND_ONLY:
            return CommandResult(False, f"{name} is set from config.yaml at connect time")
        if name in CONFIRM_REQUIRED and not confirmed:
            return CommandResult(False, f"{name} requires confirmation")
        if name == "mission_upload":
            return await self._upload_mission(args.get("mission_id"))
        if name == "takeoff":
            reasons = preflight_failures(self.cfg, self.telemetry, self.telemetry_age(), self.limits_applied)
            if reasons:
                return CommandResult(False, "takeoff refused: " + "; ".join(reasons))
        if name == "video_start":
            if not self.cfg.video.push_url:
                return CommandResult(False, "video.push_url is not set in config.yaml")
            args = {"url": self.cfg.video.push_url}
        if name == "simulator_start" and not args:
            args = {"lat": self.cfg.simulator.lat, "lon": self.cfg.simulator.lon}
        return await self.link.command(name, args)

    async def _upload_mission(self, mission_id: Any) -> CommandResult:
        mission = self.missions.get(mission_id) if isinstance(mission_id, str) else None
        if mission is None:
            return CommandResult(False, "mission not found")
        home = self.home()
        if home is None:
            return CommandResult(False, "home point not set; cannot check mission distances")
        report = validate_mission(mission, self.cfg, home)
        if not report.ok:
            return CommandResult(False, "mission is not valid: " + "; ".join(report.errors))
        return await self.link.command("mission_upload", upload_args(mission))

    # ------------------------------------------------------------------
    # missions and schedules (UI operations, answered at once)

    def _ui_op(self, msg: UiOp) -> dict[str, Any]:
        data: Any = None
        try:
            result, data = self._run_op(msg)
        except ValidationError as e:
            err = e.errors()[0]
            loc = ".".join(str(x) for x in err["loc"])
            result = CommandResult(False, f"{loc}: {err['msg']}" if loc else err["msg"])
        return {"type": "ack", "id": msg.id, "name": msg.op, "ok": result.ok, "error": result.error, "data": data}

    def _run_op(self, msg: UiOp) -> tuple[CommandResult, Any]:
        a = msg.args
        if msg.op == "validate_mission":
            report = validate_mission(parse_mission_in(a.get("mission")), self.cfg, self.home())
            return CommandResult(True), report.as_dict()
        if msg.op == "generate_survey":
            try:
                result = generate_survey(SurveyIn.model_validate(a))
            except SurveyError as e:
                return CommandResult(False, str(e)), None
            report = validate_mission(result.mission, self.cfg, self.home())
            return CommandResult(True), {
                "mission": result.mission.model_dump(mode="json"),
                "report": report.as_dict(),
                "area_m2": round(result.area_m2),
                "lines": result.lines,
                "angle_deg": round(result.angle_deg, 1),
                "photos": result.photos,
            }
        if msg.op == "save_mission":
            mission_in = parse_mission_in(a.get("mission"))
            mission_id = a.get("id")
            if mission_id is not None and self.scheduler.references(str(mission_id)):
                return CommandResult(False, "a pending sortie uses this mission; cancel it before editing"), None
            try:
                m = self.missions.save(mission_in, None if mission_id is None else str(mission_id))
            except KeyError:
                return CommandResult(False, "mission not found"), None
            self.broadcast_missions()
            report = validate_mission(m, self.cfg, self.home())
            return CommandResult(True), {"id": m.id, "report": report.as_dict()}
        if msg.op == "delete_mission":
            mission_id = str(a.get("id"))
            if self.scheduler.references(mission_id):
                return CommandResult(False, "a pending sortie uses this mission; cancel it first"), None
            ok = self.missions.delete(mission_id)
            self.broadcast_missions()
            return CommandResult(ok, None if ok else "mission not found"), None
        if msg.op == "create_schedule":
            launch_at = a.get("launch_at")
            if not isinstance(launch_at, (int, float)) or isinstance(launch_at, bool):
                return CommandResult(False, "launch_at must be a Unix time in seconds"), None
            return self.scheduler.create(str(a.get("mission_id")), float(launch_at), msg.confirmed), None
        if msg.op == "cancel_schedule":
            return self.scheduler.cancel(str(a.get("schedule_id"))), None
        if msg.op == "remove_schedule":
            return self.scheduler.remove(str(a.get("schedule_id"))), None
        return CommandResult(False, f"unknown operation {msg.op!r}"), None

    def broadcast_missions(self) -> None:
        home = self.home()
        missions = [
            {**mission_json(m), "report": validate_mission(m, self.cfg, home).as_dict()} for m in self.missions.list()
        ]
        self._to_ui({"type": "missions", "missions": missions})

    def broadcast_schedules(self) -> None:
        self._to_ui({"type": "schedules", "schedules": self.scheduler.as_json(), "server_time": time.time()})

    def home(self) -> tuple[float, float] | None:
        t = self.telemetry
        if t is None or not t.home_set or t.home_lat is None or t.home_lon is None:
            return None
        return (t.home_lat, t.home_lon)

    # ------------------------------------------------------------------
    # RC link callbacks

    def _on_connection(self, peer: HelloFromBridge | None) -> None:
        self._generation += 1
        self.limits_applied = False
        self.telemetry = None
        self._telemetry_at = None
        if peer is None:
            self.backend_event("warning", "rc_disconnected", "link to the RC app lost")
        else:
            self.backend_event(
                "info",
                "rc_connected",
                f"{peer.aircraft_model} (fw {peer.aircraft_firmware}), RC fw {peer.rc_firmware}, MSDK {peer.msdk_version}",
            )
            self._spawn(self._apply_limits(self._generation))
        self._to_ui(self.status())

    async def _apply_limits(self, generation: int) -> None:
        lim = self.cfg.limits
        # Height limit first: the aircraft rejects an RTH altitude above it.
        r1 = await self.link.command("set_height_limit", {"m": lim.height_limit_m})
        r2 = await self.link.command("set_rth_altitude", {"m": lim.rth_altitude_m}) if r1.ok else r1
        if generation != self._generation:
            return  # reconnected meanwhile; that connection applies its own
        self.limits_applied = r1.ok and r2.ok
        if self.limits_applied:
            self.backend_event(
                "info", "limits_applied", f"height limit {lim.height_limit_m:g} m, RTH altitude {lim.rth_altitude_m:g} m"
            )
        else:
            err = r1.error if not r1.ok else r2.error
            self.backend_event("critical", "limits_failed", f"could not set limits ({err}); takeoff is blocked")
        self._to_ui(self.status())

    def _on_telemetry(self, t: Telemetry) -> None:
        old_home = self.home()
        self.telemetry = t
        self._telemetry_at = self.clock()
        self._to_ui(t.model_dump(mode="json"))
        if self.home() != old_home:  # mission reports depend on the home point
            self.broadcast_missions()

    def _on_bridge_event(self, e: Event) -> None:
        self._to_ui({"source": "bridge", **e.model_dump(mode="json")})

    def telemetry_age(self) -> float | None:
        return None if self._telemetry_at is None else self.clock() - self._telemetry_at

    # ------------------------------------------------------------------
    # control loop: heartbeats and sticks

    async def _control_loop(self) -> None:
        period = 1.0 / self.cfg.safety.stick_hz
        next_tick = self.clock()
        while True:
            await self.tick()
            next_tick += period
            delay = next_tick - self.clock()
            if delay < -period:  # fell behind (loop stalled); don't burst to catch up
                next_tick = self.clock()
                delay = 0.0
            await asyncio.sleep(max(0.0, delay))

    async def tick(self) -> None:
        now = self.clock()
        alive = self.ui_monitor.alive(now)
        if alive != self._ui_was_alive:
            self._ui_was_alive = alive
            self.log.write("gs", {"ui_heartbeat": "alive" if alive else "lost", "relaying_heartbeats": alive})
            if not alive:
                log.warning("UI heartbeat lost: heartbeats to RC stopped, sending zero sticks")
                await self._send_sticks(ZERO_STICKS, now)
        if not self.link.ready:
            return
        if alive and now >= self._next_heartbeat:
            self._next_heartbeat = now + 1.0 / self.cfg.safety.heartbeat_hz - 0.002
            await self.link.send(Heartbeat(seq=self._hb_seq, t=now), log_it=False)
            self._hb_seq += 1
        if self.telemetry is not None and self.telemetry.virtual_stick_enabled:
            await self._send_sticks(self.current_sticks(now, alive), now)

    def current_sticks(self, now: float, ui_alive: bool) -> Sticks:
        if not ui_alive or self._ui_sticks_at is None:
            return ZERO_STICKS
        if now - self._ui_sticks_at > self.cfg.safety.ui_stick_timeout_s:
            return ZERO_STICKS
        return self._ui_sticks.scaled(self.speed_scale)

    async def _send_sticks(self, s: Sticks, now: float) -> None:
        await self.link.send(Stick(lx=s.lx, ly=s.ly, rx=s.rx, ry=s.ry, t=now))

    # ------------------------------------------------------------------
    # status for the UI

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            self._to_ui(self.status())

    def status(self) -> dict[str, Any]:
        peer = self.link.peer
        reasons = preflight_failures(self.cfg, self.telemetry, self.telemetry_age(), self.limits_applied)
        age = self.telemetry_age()
        return {
            "type": "status",
            "rc": {
                "uri": self.link.uri,
                "connected": self.link.ready,
                "peer": peer.model_dump(mode="json") if peer else None,
            },
            "telemetry_age": None if age is None else round(age, 2),
            "limits_applied": self.limits_applied,
            "speed_scale": self.speed_scale,
            "preflight": {"ok": not reasons, "reasons": reasons},
        }
