"""A deliberately simple aircraft model for exercising the ground station.

It is not a flight-dynamics model. Velocities follow stick targets with a
first-order lag; auto modes (takeoff, landing, go home) are scripted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

EARTH_R = 6_371_000.0

MAX_H_SPEED = 10.0  # m/s at full stick
MAX_V_SPEED = 4.0  # m/s at full stick
MAX_YAW_RATE = 90.0  # deg/s at full stick
RESPONSE_TAU = 0.5  # s
TAKEOFF_ALT = 1.2  # m
TAKEOFF_RATE = 1.5  # m/s
LAND_RATE = 1.0  # m/s
RTH_SPEED = 10.0  # m/s
RTH_LAND_RADIUS = 3.0  # m
GPS_ACQUIRE_S = 6.0
GPS_SATS_MAX = 18
HOME_SET_SATS = 10
GIMBAL_RATE_HOLD_S = 0.5  # a gimbal_rotate rate stops this long after the last command
WAYPOINT_REACHED_M = 1.0
WAYPOINT_ALT_TOL_M = 0.5
MISSION_CLIMB_RATE = 3.0  # m/s


class CommandError(Exception):
    pass


def _wrap180(deg: float) -> float:
    return (deg + 180.0) % 360.0 - 180.0


def _wrap360(deg: float) -> float:
    h = deg % 360.0
    return 0.0 if h >= 360.0 else h


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dn = math.radians(lat2 - lat1) * EARTH_R
    de = math.radians(lon2 - lon1) * EARTH_R * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dn, de)


@dataclass
class Warning_:
    code: str
    level: str
    message: str


@dataclass
class AircraftSim:
    lat: float
    lon: float
    alt: float = 0.0
    heading: float = 0.0  # deg, 0 = north
    vn: float = 0.0
    ve: float = 0.0
    vd: float = 0.0
    battery: float = 100.0
    motors_on: bool = False
    is_flying: bool = False
    mode: str = "MANUAL"  # MANUAL, AUTO_TAKE_OFF, GPS_NORMAL, VIRTUAL_STICK, WAYPOINT, GO_HOME, AUTO_LANDING
    virtual_stick: bool = False
    simulator_active: bool = False
    home_lat: float | None = None
    home_lon: float | None = None
    height_limit: float = 120.0
    rth_altitude: float = 50.0
    gimbal_pitch: float = 0.0
    gimbal_rate: float = 0.0
    gimbal_rate_until: float = 0.0
    camera_recording: bool = False
    camera_lens: str = "wide"
    zoom_ratio: float = 1.0
    sticks: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # lx, ly, rx, ry
    elapsed: float = 0.0
    gps_override: int | None = None
    injected_warnings: dict[str, Warning_] = field(default_factory=dict)
    # mission
    mission: dict | None = None  # the mission_upload args, as dicts
    mission_state: str = "idle"  # idle, ready, executing, paused
    mission_index: int = 0  # next waypoint; len(waypoints) = returning to the first one
    photos_taken: int = 0
    # (level, code, message, data) for the server to send as events
    events: list[tuple[str, str, str, dict | None]] = field(default_factory=list)

    # ---------------------------------------------------------------- state

    @property
    def gps_sats(self) -> int:
        if self.gps_override is not None:
            return self.gps_override
        return min(GPS_SATS_MAX, int(GPS_SATS_MAX * self.elapsed / GPS_ACQUIRE_S))

    @property
    def home_set(self) -> bool:
        return self.home_lat is not None

    def warnings(self) -> list[Warning_]:
        out = list(self.injected_warnings.values())
        if self.battery < 15:
            out.append(Warning_("battery_critical", "critical", "battery critically low"))
        elif self.battery < 30:
            out.append(Warning_("battery_low", "warning", "battery low"))
        if self.gps_sats < 6:
            out.append(Warning_("gps_weak", "warning", f"only {self.gps_sats} GPS satellites"))
        return out

    # ------------------------------------------------------------- commands

    def takeoff(self) -> None:
        if self.is_flying or self.motors_on:
            raise CommandError("already flying")
        if not self.home_set:
            raise CommandError("home point not recorded")
        self.motors_on = True
        self.is_flying = True
        self.mode = "AUTO_TAKE_OFF"

    def land(self) -> None:
        if not self.is_flying:
            raise CommandError("not flying")
        self._end_mission("interrupted by land")
        self.mode = "AUTO_LANDING"

    def go_home(self) -> None:
        if not self.is_flying:
            raise CommandError("not flying")
        if not self.home_set:
            raise CommandError("home point not recorded")
        self._end_mission("interrupted by go home")
        self.mode = "GO_HOME"

    # missions

    def mission_upload(self, args: dict) -> None:
        if self.mission_state in ("executing", "paused"):
            raise CommandError("a mission is running; stop it first")
        self.mission = args
        self.mission_state = "ready"

    def mission_start(self) -> None:
        if self.mission_state != "ready" or self.mission is None:
            raise CommandError("no mission uploaded")
        if not self.is_flying or self.mode == "AUTO_TAKE_OFF":
            raise CommandError("take off and hover first")
        if self.virtual_stick:
            raise CommandError("disable virtual stick first")
        if self.mode not in ("GPS_NORMAL",):
            raise CommandError(f"cannot start a mission in {self.mode}")
        self.mission_state = "executing"
        self.mission_index = 0
        self.mode = "WAYPOINT"
        self.events.append(("info", "mission_started", "mission started", {"waypoints": len(self.mission["waypoints"])}))

    def mission_pause(self, reason: str = "paused") -> None:
        if self.mission_state != "executing":
            raise CommandError("no mission executing")
        self.mission_state = "paused"
        self.events.append(("info", "mission_paused", f"mission {reason}", {"waypoint_index": self.mission_index}))

    def mission_resume(self) -> None:
        if self.mission_state != "paused":
            raise CommandError("mission is not paused")
        if self.mode != "WAYPOINT":
            raise CommandError("the aircraft left the mission")
        self.mission_state = "executing"
        self.events.append(("info", "mission_resumed", "mission resumed", {"waypoint_index": self.mission_index}))

    def mission_stop(self) -> None:
        if self.mission_state not in ("executing", "paused"):
            raise CommandError("no mission running")
        self._end_mission("stopped")
        self.mode = self._hover_mode()

    def _end_mission(self, why: str) -> None:
        if self.mission_state in ("executing", "paused"):
            self.events.append(("warning", "mission_interrupted", f"mission {why}", {"waypoint_index": self.mission_index}))
            self.mission_state = "idle"
            self.mission = None

    def cancel_go_home(self) -> None:
        if self.mode != "GO_HOME":
            raise CommandError("not going home")
        self.mode = self._hover_mode()

    def set_virtual_stick(self, enabled: bool) -> None:
        self.virtual_stick = enabled
        self.sticks = (0.0, 0.0, 0.0, 0.0)
        if self.mode in ("GPS_NORMAL", "VIRTUAL_STICK"):
            self.mode = self._hover_mode()

    def gimbal_rotate(self, pitch_rate: float, now: float) -> None:
        self.gimbal_rate = pitch_rate
        self.gimbal_rate_until = now + GIMBAL_RATE_HOLD_S

    def gimbal_set(self, pitch: float) -> None:
        self.gimbal_pitch = max(-90.0, min(35.0, pitch))
        self.gimbal_rate = 0.0

    def simulator_start(self, lat: float, lon: float) -> None:
        if self.is_flying:
            raise CommandError("cannot start the simulator while flying")
        self.lat, self.lon, self.alt = lat, lon, 0.0
        self.home_lat = self.home_lon = None
        self.elapsed = 0.0
        self.simulator_active = True

    def simulator_stop(self) -> None:
        if self.is_flying:
            raise CommandError("cannot stop the simulator while flying")
        self.simulator_active = False

    def _hover_mode(self) -> str:
        if not self.is_flying:
            return "MANUAL"
        return "VIRTUAL_STICK" if self.virtual_stick else "GPS_NORMAL"

    # ---------------------------------------------------------------- physics

    def step(self, dt: float, now: float) -> None:
        self.elapsed += dt
        if not self.home_set and not self.is_flying and self.gps_sats >= HOME_SET_SATS:
            self.home_lat, self.home_lon = self.lat, self.lon

        self.battery = max(0.0, self.battery - dt * (100.0 / 1800.0 if self.motors_on else 0.002))

        if now >= self.gimbal_rate_until:
            self.gimbal_rate = 0.0
        self.gimbal_pitch = max(-90.0, min(35.0, self.gimbal_pitch + self.gimbal_rate * dt))

        if not self.is_flying:
            self.vn = self.ve = self.vd = 0.0
            return

        tn, te, td, yaw_rate = self._targets()
        k = 1.0 - math.exp(-dt / RESPONSE_TAU)
        self.vn += (tn - self.vn) * k
        self.ve += (te - self.ve) * k
        self.vd += (td - self.vd) * k
        self.heading = _wrap360(self.heading + yaw_rate * dt)

        if self.alt >= self.height_limit and self.vd < 0:
            self.vd = 0.0
        self.alt = min(self.height_limit, self.alt - self.vd * dt)
        self.lat += math.degrees(self.vn * dt / EARTH_R)
        self.lon += math.degrees(self.ve * dt / (EARTH_R * math.cos(math.radians(self.lat))))

        if self.mode == "AUTO_TAKE_OFF" and self.alt >= TAKEOFF_ALT:
            self.mode = self._hover_mode()
        if self.mode == "WAYPOINT" and self.mission_state == "executing":
            self._mission_progress()
        if self.alt <= 0.0:
            self.alt = 0.0
            if self.vd > 0:
                self.vd = 0.0
            if self.mode == "AUTO_LANDING":
                self.is_flying = False
                self.motors_on = False
                self.mode = "MANUAL"
                self.vn = self.ve = self.vd = 0.0

    def _targets(self) -> tuple[float, float, float, float]:
        """Target (vel_n, vel_e, vel_d, yaw_rate) for the current mode."""
        if self.mode == "AUTO_TAKE_OFF":
            return 0.0, 0.0, -TAKEOFF_RATE, 0.0
        if self.mode == "AUTO_LANDING":
            return 0.0, 0.0, LAND_RATE, 0.0
        if self.mode == "GO_HOME":
            assert self.home_lat is not None and self.home_lon is not None
            dist = distance_m(self.lat, self.lon, self.home_lat, self.home_lon)
            if dist <= RTH_LAND_RADIUS:
                self.mode = "AUTO_LANDING"
                return 0.0, 0.0, LAND_RATE, 0.0
            if self.alt < self.rth_altitude - 0.5:
                return 0.0, 0.0, -MAX_V_SPEED, 0.0
            dn = math.radians(self.home_lat - self.lat) * EARTH_R
            de = math.radians(self.home_lon - self.lon) * EARTH_R * math.cos(math.radians(self.lat))
            speed = min(RTH_SPEED, dist)  # slow down on approach
            return speed * dn / dist, speed * de / dist, 0.0, 0.0
        if self.mode == "WAYPOINT":
            target = self._mission_target()
            if self.mission_state != "executing" or target is None:
                return 0.0, 0.0, 0.0, 0.0  # paused: hover
            lat, lon, alt, speed = target
            dn = math.radians(lat - self.lat) * EARTH_R
            de = math.radians(lon - self.lon) * EARTH_R * math.cos(math.radians(self.lat))
            dist = math.hypot(dn, de)
            v = min(speed, dist) if dist > 0.01 else 0.0
            vn, ve = (v * dn / dist, v * de / dist) if dist > 0.01 else (0.0, 0.0)
            vd = max(-MISSION_CLIMB_RATE, min(MISSION_CLIMB_RATE, self.alt - alt))
            if dist > 2.0:  # face the direction of travel
                want = math.degrees(math.atan2(de, dn))
                yaw_rate = max(-MAX_YAW_RATE, min(MAX_YAW_RATE, 2.0 * _wrap180(want - self.heading)))
            else:
                yaw_rate = 0.0
            return vn, ve, vd, yaw_rate
        if self.mode == "VIRTUAL_STICK":
            lx, ly, rx, ry = self.sticks
            h = math.radians(self.heading)
            fwd, right = ry * MAX_H_SPEED, rx * MAX_H_SPEED
            vn = fwd * math.cos(h) - right * math.sin(h)
            ve = fwd * math.sin(h) + right * math.cos(h)
            return vn, ve, -ly * MAX_V_SPEED, lx * MAX_YAW_RATE
        return 0.0, 0.0, 0.0, 0.0  # GPS_NORMAL: hover

    def _mission_target(self) -> tuple[float, float, float, float] | None:
        """(lat, lon, alt, speed) the mission is flying to, or None when done."""
        if self.mission is None:
            return None
        wps = self.mission["waypoints"]
        speed = self.mission["speed"]
        if self.mission_index < len(wps):
            wp = wps[self.mission_index]
            return wp["lat"], wp["lon"], wp["alt"], wp.get("speed") or speed
        if self.mission_index == len(wps) and self.mission["finish_action"] == "first_waypoint":
            wp = wps[0]
            return wp["lat"], wp["lon"], wp["alt"], speed
        return None

    def _mission_progress(self) -> None:
        target = self._mission_target()
        assert self.mission is not None
        wps = self.mission["waypoints"]
        if target is not None:
            lat, lon, alt, _ = target
            if distance_m(self.lat, self.lon, lat, lon) > WAYPOINT_REACHED_M or abs(self.alt - alt) > WAYPOINT_ALT_TOL_M:
                return
            if self.mission_index < len(wps):
                for action in wps[self.mission_index].get("actions", []):
                    if action["action"] == "photo":
                        self.photos_taken += 1
                    elif action["action"] == "gimbal_pitch":
                        self.gimbal_set(action["pitch"])
                self.events.append((
                    "info", "mission_progress", f"waypoint {self.mission_index + 1}/{len(wps)} reached",
                    {"waypoint_index": self.mission_index, "total": len(wps)},
                ))
            self.mission_index += 1
            if self._mission_target() is not None:
                return
        finish = self.mission["finish_action"]
        self.mission_state = "idle"
        self.mission = None
        self.events.append(("info", "mission_finished", f"mission finished, then {finish}", {"finish_action": finish}))
        if finish == "go_home":
            self.mode = "GO_HOME"
        elif finish == "land":
            self.mode = "AUTO_LANDING"
        else:
            self.mode = self._hover_mode()

    def attitude(self) -> tuple[float, float]:
        """Approximate (pitch, roll) in degrees from body velocity."""
        h = math.radians(self.heading)
        fwd = self.vn * math.cos(h) + self.ve * math.sin(h)
        right = -self.vn * math.sin(h) + self.ve * math.cos(h)
        return -25.0 * fwd / MAX_H_SPEED, 25.0 * right / MAX_H_SPEED

    def yaw(self) -> float:
        return _wrap180(self.heading)
