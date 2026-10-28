"""Mock RC bridge: speaks the RC app's protocol over a simulated aircraft.

It behaves like the RC app is specified to: one client at a time, handshake
first, stale sticks dropped, the same watchdog, acks for every command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import WebSocketException

from drone_protocol.messages import (
    PROTOCOL_VERSION,
    Ack,
    ActiveWarning,
    Event,
    Heartbeat,
    HelloFromBridge,
    HelloFromGroundStation,
    ProtocolError,
    Stick,
    Telemetry,
    dump,
    parse_from_ground_station,
    peek_protocol_version,
)
from drone_protocol.timing import AgeEstimator

from .sim import AircraftSim, CommandError, Warning_
from .watchdog import STICK_HOLD_S, STICK_MAX_AGE_S, Action, BridgeWatchdog

log = logging.getLogger("mock_bridge")

CLOSE_BUSY = 4001
CLOSE_VERSION_MISMATCH = 4002
CLOSE_PROTOCOL = 4003

PHYSICS_HZ = 50.0
TELEMETRY_HZ = 10.0


def _clamp(v: float) -> float:
    return max(-1.0, min(1.0, v)) if v == v else 0.0  # NaN -> 0


def _peek_command_id(raw: str | bytes) -> str | None:
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if isinstance(obj, dict) and obj.get("type") == "command":
        cid = obj.get("id")
        if isinstance(cid, str) and 0 < len(cid) <= 64:
            return cid
    return None


class MockBridge:
    def __init__(self, sim: AircraftSim, clock: Callable[[], float] = time.monotonic) -> None:
        self.sim = sim
        self.clock = clock
        self.watchdog = BridgeWatchdog()
        self.ages = AgeEstimator()
        self.hello = HelloFromBridge(
            protocol_version=PROTOCOL_VERSION,
            msdk_version="mock",
            aircraft_model="MOCK_MATRICE_4E",
            aircraft_firmware="mock",
            rc_firmware="mock",
        )
        self._client: ServerConnection | None = None
        self._session: ServerConnection | None = None  # client after handshake
        self._send_lock = asyncio.Lock()
        self._last_stick_at: float | None = None
        self._telemetry_seq = 0
        self._last_mode = sim.mode
        self._last_stale_event = 0.0
        self.stale_sticks_dropped = 0

    # ------------------------------------------------------------ server

    async def serve(self, host: str, port: int) -> Server:
        return await serve(self._handler, host, port, compression=None, ping_interval=2, ping_timeout=4)

    async def run(self) -> None:
        await asyncio.gather(self._physics_loop(), self._telemetry_loop())

    async def _handler(self, ws: ServerConnection) -> None:
        if self._client is not None:
            await ws.close(CLOSE_BUSY, "another ground station is connected")
            return
        self._client = ws
        log.info("client connected from %s", ws.remote_address)
        try:
            await self._handshake_and_serve(ws)
        except WebSocketException as e:
            log.info("connection ended: %s", e)
        finally:
            self._client = None
            self._session = None
            log.info("client disconnected")

    async def _handshake_and_serve(self, ws: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except TimeoutError:
            await ws.close(CLOSE_PROTOCOL, "no hello")
            return
        if peek_protocol_version(raw) != PROTOCOL_VERSION:
            await ws.send(dump(self.hello))
            await ws.close(CLOSE_VERSION_MISMATCH, "protocol version mismatch")
            return
        try:
            hello = parse_from_ground_station(raw)
        except ProtocolError as e:
            await ws.close(CLOSE_PROTOCOL, f"bad hello: {e}"[:120])
            return
        if not isinstance(hello, HelloFromGroundStation):
            await ws.close(CLOSE_PROTOCOL, "expected hello")
            return
        self.watchdog.go_home_timeout_s = hello.go_home_timeout_s
        self.ages.reset()
        await ws.send(dump(self.hello))
        self._session = ws
        log.info("handshake ok, go-home timeout %.1f s", hello.go_home_timeout_s)
        async for raw in ws:
            await self.handle_frame(raw)

    async def _send(self, msg: Any) -> None:
        ws = self._session
        if ws is None:
            return
        try:
            async with self._send_lock:
                await ws.send(dump(msg))
        except WebSocketException:
            pass

    async def event(self, level: str, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        log.log(logging.WARNING if level != "info" else logging.INFO, "event %s: %s", code, message)
        await self._send(Event(level=level, code=code, message=message, t=self.clock(), data=data))

    # ------------------------------------------------------------ inbound

    async def handle_frame(self, raw: str | bytes) -> None:
        now = self.clock()
        try:
            msg = parse_from_ground_station(raw)
        except ProtocolError as e:
            cmd_id = _peek_command_id(raw)
            if cmd_id is not None:
                await self._send(Ack(id=cmd_id, ok=False, error=f"invalid command: {e}"))
            else:
                await self.event("warning", "protocol_error", f"invalid frame ignored: {e}")
            return

        if isinstance(msg, Heartbeat):
            self.ages.observe(msg.t, now)
            for action in self.watchdog.heartbeat(now):
                if action is Action.RESTORED:
                    await self.event("info", "heartbeat_restored", "heartbeat restored; virtual stick stays off")
        elif isinstance(msg, Stick):
            await self._handle_stick(msg, now)
        elif isinstance(msg, HelloFromGroundStation):
            await self.event("warning", "protocol_error", "unexpected second hello ignored")
        else:  # command
            ok, error = self._run_command(msg, now)
            log.info("command %s %s -> %s%s", msg.name, msg.args.model_dump(), "ok" if ok else "FAIL ", error or "")
            await self._send(Ack(id=msg.id, ok=ok, error=error))

    async def _handle_stick(self, msg: Stick, now: float) -> None:
        age = self.ages.observe(msg.t, now)
        if age > STICK_MAX_AGE_S:
            self.stale_sticks_dropped += 1
            if now - self._last_stale_event > 1.0:
                self._last_stale_event = now
                await self.event("warning", "stale_sticks_dropped", f"stick message {age * 1000:.0f} ms late, dropped")
            return
        if not self.sim.virtual_stick:
            return
        self.sim.sticks = (_clamp(msg.lx), _clamp(msg.ly), _clamp(msg.rx), _clamp(msg.ry))
        self._last_stick_at = now

    def _run_command(self, msg: Any, now: float) -> tuple[bool, str | None]:
        sim, a = self.sim, msg.args
        try:
            match msg.name:
                case "enable_virtual_stick":
                    sim.set_virtual_stick(True)
                    self._last_stick_at = None
                case "disable_virtual_stick":
                    sim.set_virtual_stick(False)
                case "takeoff":
                    sim.takeoff()
                case "land":
                    sim.land()
                case "go_home":
                    sim.go_home()
                case "cancel_go_home":
                    sim.cancel_go_home()
                case "gimbal_rotate":
                    sim.gimbal_rotate(a.pitch_rate, now)
                case "gimbal_set":
                    sim.gimbal_set(a.pitch)
                case "gimbal_reset":
                    sim.gimbal_set(0.0)
                case "camera_photo":
                    pass
                case "camera_record_start":
                    if sim.camera_recording:
                        raise CommandError("already recording")
                    sim.camera_recording = True
                case "camera_record_stop":
                    if not sim.camera_recording:
                        raise CommandError("not recording")
                    sim.camera_recording = False
                case "camera_zoom":
                    if sim.camera_lens != "zoom":
                        raise CommandError("switch to the zoom lens first")
                    sim.zoom_ratio = a.ratio
                case "camera_lens":
                    sim.camera_lens = a.lens
                    if a.lens == "wide":
                        sim.zoom_ratio = 1.0
                case "set_height_limit":
                    if a.m < 20:
                        raise CommandError("height limit must be at least 20 m")
                    sim.height_limit = a.m
                case "set_rth_altitude":
                    if a.m > sim.height_limit:
                        raise CommandError("RTH altitude above height limit")
                    sim.rth_altitude = a.m
                case "simulator_start":
                    sim.simulator_start(a.lat, a.lon)
                case "simulator_stop":
                    sim.simulator_stop()
                case "video_start" | "video_stop":
                    raise CommandError("the mock bridge has no video")
                case "mission_upload":
                    sim.mission_upload(a.model_dump(mode="json"))
                case "mission_start":
                    sim.mission_start()
                case "mission_pause":
                    sim.mission_pause()
                case "mission_resume":
                    sim.mission_resume()
                case "mission_stop":
                    sim.mission_stop()
                case other:
                    raise CommandError(f"unhandled command {other}")
        except CommandError as e:
            return False, str(e)
        return True, None

    # ------------------------------------------------------------ loops

    async def _physics_loop(self) -> None:
        period = 1.0 / PHYSICS_HZ
        last = self.clock()
        while True:
            await asyncio.sleep(period)
            now = self.clock()
            await self.step(now - last, now)
            last = now

    async def step(self, dt: float, now: float) -> None:
        sim = self.sim
        if sim.virtual_stick and self._last_stick_at is not None and now - self._last_stick_at > STICK_HOLD_S:
            sim.sticks = (0.0, 0.0, 0.0, 0.0)
            self._last_stick_at = None

        for action in self.watchdog.check(now, sim.is_flying, sim.motors_on):
            if action is Action.HOVER:
                sim.sticks = (0.0, 0.0, 0.0, 0.0)
                was_on = sim.virtual_stick
                sim.set_virtual_stick(False)
                paused = sim.mission_state == "executing"
                if paused:
                    sim.mission_pause("paused by the watchdog")
                await self.event(
                    "warning",
                    "watchdog_hover",
                    "no heartbeat for 1 s: sticks zeroed"
                    + (", virtual stick disabled" if was_on else "")
                    + (", mission paused" if paused else ""),
                )
            elif action is Action.GO_HOME:
                try:
                    sim.go_home()
                    await self.event(
                        "critical", "watchdog_go_home", f"no heartbeat for {self.watchdog.go_home_timeout_s:g} s: going home"
                    )
                except CommandError as e:
                    await self.event("critical", "watchdog_go_home_failed", f"go home failed: {e}")

        sim.step(dt, now)
        while sim.events:
            await self.event(*sim.events.pop(0))
        if sim.mode != self._last_mode:
            await self.event("info", "flight_mode_changed", f"{self._last_mode} -> {sim.mode}",
                             {"from": self._last_mode, "to": sim.mode})
            self._last_mode = sim.mode

    async def _telemetry_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0 / TELEMETRY_HZ)
            if self._session is not None:
                await self._send(self.telemetry())

    def telemetry(self) -> Telemetry:
        s = self.sim
        pitch, roll = s.attitude()
        sats = s.gps_sats
        self._telemetry_seq += 1
        return Telemetry(
            seq=self._telemetry_seq,
            t=self.clock(),
            lat=s.lat,
            lon=s.lon,
            alt_rel=round(s.alt, 2),
            heading=round(s.heading, 1) % 360.0,
            vel_n=round(s.vn, 2),
            vel_e=round(s.ve, 2),
            vel_d=round(s.vd, 2),
            pitch=round(pitch, 1),
            roll=round(roll, 1),
            yaw=round(s.yaw(), 1),
            battery_pct=int(s.battery),
            gps_sats=sats,
            gps_level=min(5, sats // 3),
            home_lat=s.home_lat,
            home_lon=s.home_lon,
            home_set=s.home_set,
            flight_mode=s.mode,
            motors_on=s.motors_on,
            is_flying=s.is_flying,
            virtual_stick_enabled=s.virtual_stick,
            simulator_active=s.simulator_active,
            gimbal_pitch=round(s.gimbal_pitch, 1),
            gimbal_yaw=0.0,
            rc_link_quality=100,
            video_link_quality=None,
            mission_state=s.mission_state,
            active_warnings=[ActiveWarning(code=w.code, level=w.level, message=w.message) for w in s.warnings()],
            camera_recording=s.camera_recording,
            camera_lens=s.camera_lens,
            zoom_ratio=s.zoom_ratio,
        )

    # ------------------------------------------------------------ fault injection

    async def rc_override(self) -> None:
        """Simulate the pilot taking over with the RC (flight mode switch / pause)."""
        if self.sim.mission_state == "executing":
            self.sim.mission_pause("paused by the pilot on the RC")
            await self.event("warning", "mission_paused_by_rc", "pilot paused the mission with the RC")
        if not self.sim.virtual_stick:
            return
        self.sim.set_virtual_stick(False)
        await self.event("warning", "virtual_stick_overridden", "pilot took control with the RC; virtual stick disabled")

    async def console_command(self, line: str) -> str:
        parts = line.split()
        if not parts:
            return ""
        cmd, rest = parts[0].lower(), parts[1:]
        if cmd == "override":
            await self.rc_override()
            return "RC override applied"
        if cmd == "battery" and rest:
            self.sim.battery = float(rest[0])
            return f"battery set to {self.sim.battery:.0f}%"
        if cmd == "gps" and rest:
            self.sim.gps_override = None if rest[0] == "auto" else int(rest[0])
            return f"GPS satellites: {rest[0]}"
        if cmd == "warn" and rest:
            level = "critical" if len(rest) > 1 and rest[1] == "critical" else "warning"
            self.sim.injected_warnings[rest[0]] = Warning_(rest[0], level, f"injected {level}")
            return f"warning {rest[0]} ({level}) active"
        if cmd == "clear":
            self.sim.injected_warnings.clear()
            return "injected warnings cleared"
        if cmd == "drop" and self._client is not None:
            await self._client.close(1001, "dropped by mock console")
            return "connection dropped"
        if cmd == "status":
            s = self.sim
            return (f"mode={s.mode} flying={s.is_flying} vs={s.virtual_stick} alt={s.alt:.1f} "
                    f"batt={s.battery:.0f}% sats={s.gps_sats} home={s.home_set} "
                    f"mission={s.mission_state}#{s.mission_index} photos={s.photos_taken} "
                    f"stale_dropped={self.stale_sticks_dropped}")
        return CONSOLE_HELP


CONSOLE_HELP = """commands:
  override              pilot takes over with the RC (disables virtual stick, pauses a mission)
  battery <pct>         set battery level
  gps <n>|auto          force GPS satellite count
  warn <code> [critical] inject an active warning
  clear                 clear injected warnings
  drop                  close the ground station connection
  status                print aircraft state"""
