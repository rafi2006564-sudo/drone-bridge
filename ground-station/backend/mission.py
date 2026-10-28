"""Mission definitions, validation (build plan section 8) and storage."""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from drone_protocol.messages import Waypoint

from .config import Config

EARTH_R = 6_371_000.0
MISSION_ID = re.compile(r"^[a-f0-9]{8}$")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular approximation; accurate to well under 1% at mission scale."""
    dn = math.radians(lat2 - lat1) * EARTH_R
    de = math.radians(lon2 - lon1) * EARTH_R * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dn, de)


class MissionIn(BaseModel):
    """A mission as the UI edits it."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: Annotated[str, Field(min_length=1, max_length=80)]
    speed: Annotated[float, Field(gt=0.0, le=25.0)]
    finish_action: Literal["go_home", "hover", "land", "first_waypoint"]
    waypoints: list[Waypoint]


class Mission(MissionIn):
    id: str
    created: float
    updated: float


@dataclass
class MissionReport:
    errors: list[str] = field(default_factory=list)
    length_m: float = 0.0
    duration_s: float = 0.0
    max_alt_m: float = 0.0
    max_distance_from_home_m: float | None = None
    home_checked: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "length_m": round(self.length_m, 1),
            "duration_s": round(self.duration_s),
            "max_alt_m": self.max_alt_m,
            "max_distance_from_home_m": (
                None if self.max_distance_from_home_m is None else round(self.max_distance_from_home_m, 1)
            ),
            "home_checked": self.home_checked,
        }


def validate_mission(m: MissionIn, cfg: Config, home: tuple[float, float] | None) -> MissionReport:
    """Check a mission against the limits in config.yaml.

    `home` is the aircraft's recorded home point. Without it, the distance
    check cannot run and the report says so (`home_checked` False); an upload
    or a scheduled launch always passes a home point.
    """
    lim = cfg.limits
    r = MissionReport()
    wps = m.waypoints
    n = len(wps)
    if n < 2:
        r.errors.append(f"a mission needs at least 2 waypoints (has {n})")
    if n > lim.max_waypoints:
        r.errors.append(f"{n} waypoints exceeds the limit of {lim.max_waypoints}")
    if m.speed > lim.max_mission_speed_ms:
        r.errors.append(f"mission speed {m.speed:g} m/s exceeds {lim.max_mission_speed_ms:g} m/s")
    for i, wp in enumerate(wps, 1):
        if wp.alt > lim.height_limit_m:
            r.errors.append(f"waypoint {i}: altitude {wp.alt:g} m is above the height limit {lim.height_limit_m:g} m")
        if wp.alt < lim.min_mission_alt_m:
            r.errors.append(f"waypoint {i}: altitude {wp.alt:g} m is below the minimum {lim.min_mission_alt_m:g} m")
        if wp.speed is not None and wp.speed > lim.max_mission_speed_ms:
            r.errors.append(f"waypoint {i}: speed {wp.speed:g} m/s exceeds {lim.max_mission_speed_ms:g} m/s")
    if not wps:
        return r
    r.max_alt_m = max(wp.alt for wp in wps)

    if home is not None:
        r.home_checked = True
        dists = [distance_m(home[0], home[1], wp.lat, wp.lon) for wp in wps]
        r.max_distance_from_home_m = max(dists)
        for i, d in enumerate(dists, 1):
            if d > lim.max_distance_from_home_m:
                r.errors.append(
                    f"waypoint {i} is {d:.0f} m from home, over the {lim.max_distance_from_home_m:g} m limit"
                )

    # Estimated path: (home ->) waypoints (-> home / first waypoint).
    pts: list[tuple[float, float, float]] = [(wp.lat, wp.lon, wp.speed or m.speed) for wp in wps]
    if home is not None:
        pts.insert(0, (home[0], home[1], m.speed))
        if m.finish_action == "go_home":
            pts.append((home[0], home[1], m.speed))
    if m.finish_action == "first_waypoint":
        pts.append((wps[0].lat, wps[0].lon, m.speed))
    for (la1, lo1, _), (la2, lo2, v) in zip(pts, pts[1:]):
        d = distance_m(la1, lo1, la2, lo2)
        r.length_m += d
        r.duration_s += d / v
    return r


def upload_args(m: MissionIn) -> dict[str, Any]:
    """The `mission_upload` command arguments for this mission."""
    return {
        "waypoints": [wp.model_dump(mode="json") for wp in m.waypoints],
        "speed": m.speed,
        "finish_action": m.finish_action,
    }


class MissionStore:
    """Missions saved as one JSON file each, so they survive restarts."""

    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, mission_id: str) -> Path:
        if not MISSION_ID.match(mission_id):
            raise KeyError(mission_id)
        return self.dir / f"{mission_id}.json"

    def get(self, mission_id: str) -> Mission | None:
        try:
            path = self._path(mission_id)
        except KeyError:
            return None
        if not path.exists():
            return None
        try:
            return Mission.model_validate_json(path.read_text(encoding="utf-8"))
        except ValidationError:
            return None

    def list(self) -> list[Mission]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            m = self.get(p.stem)
            if m is not None:
                out.append(m)
        return sorted(out, key=lambda m: m.name.lower())

    def save(self, data: MissionIn, mission_id: str | None = None) -> Mission:
        now = time.time()
        existing = self.get(mission_id) if mission_id is not None else None
        if mission_id is not None and existing is None:
            raise KeyError(mission_id)
        m = Mission(
            **data.model_dump(),
            id=existing.id if existing else uuid.uuid4().hex[:8],
            created=existing.created if existing else now,
            updated=now,
        )
        path = self._path(m.id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(m.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return m

    def delete(self, mission_id: str) -> bool:
        try:
            path = self._path(mission_id)
        except KeyError:
            return False
        if not path.exists():
            return False
        path.unlink()
        return True


def parse_mission_in(data: Any) -> MissionIn:
    return MissionIn.model_validate(data)


def mission_json(m: Mission) -> dict[str, Any]:
    return json.loads(m.model_dump_json())
