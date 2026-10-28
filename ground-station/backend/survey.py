"""Area survey generator: polygon -> lawnmower waypoints (build plan section 8).

The polygon is projected to a local flat frame in metres, then swept with
parallel lines `spacing_m` apart. Lines are flown back and forth
(boustrophedon). The first and last lines sit half a spacing inside the
area's edges, so the strips cover the whole area.

Concave polygons can cut a line into several segments. The aircraft flies
straight between them, which can cross outside the polygon; the result is
still validated like any other mission (distance from home, altitudes).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .mission import EARTH_R, MissionIn

MAX_LINES = 2000

Lat = Annotated[float, Field(ge=-90.0, le=90.0)]
Lon = Annotated[float, Field(ge=-180.0, le=180.0)]


class SurveyIn(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    name: Annotated[str, Field(min_length=1, max_length=80)] = "Survey"
    polygon: Annotated[list[tuple[Lat, Lon]], Field(min_length=3, max_length=100)]  # (lat, lon)
    alt: Annotated[float, Field(ge=0.0, le=1500.0)]  # m above takeoff
    spacing_m: Annotated[float, Field(ge=1.0, le=500.0)]  # between lines
    # Direction the lines run, degrees clockwise from north (0 = north-south).
    # None = along the polygon's longest edge, which needs the fewest turns.
    angle_deg: Annotated[float, Field(ge=0.0, lt=180.0)] | None = None
    speed: Annotated[float, Field(gt=0.0, le=25.0)]
    # Photo every this many metres along each line; None = no photos.
    photo_spacing_m: Annotated[float, Field(ge=1.0, le=500.0)] | None = None
    # Gimbal pitch set at the first waypoint (-90 = straight down); None = leave it.
    gimbal_pitch: Annotated[float, Field(ge=-90.0, le=35.0)] | None = -90.0
    finish_action: Literal["go_home", "hover", "land", "first_waypoint"] = "go_home"


class SurveyError(ValueError):
    pass


@dataclass
class SurveyResult:
    mission: MissionIn
    area_m2: float
    lines: int
    angle_deg: float
    photos: int


@dataclass(frozen=True)
class _Frame:
    """Local tangent plane around a reference point: x east, y north, metres."""

    lat0: float
    lon0: float

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = math.radians(lon - self.lon0) * EARTH_R * math.cos(math.radians(self.lat0))
        y = math.radians(lat - self.lat0) * EARTH_R
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.lat0 + math.degrees(y / EARTH_R)
        lon = self.lon0 + math.degrees(x / (EARTH_R * math.cos(math.radians(self.lat0))))
        return lat, lon


def polygon_area(pts: list[tuple[float, float]]) -> float:
    s = 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def _segments_cross(a, b, c, d) -> bool:
    """True if segments ab and cd properly intersect (not just touch at ends)."""

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return o1 * o2 < 0 and o3 * o4 < 0


def _self_intersects(pts: list[tuple[float, float]]) -> bool:
    n = len(pts)
    edges = [(pts[i], pts[(i + 1) % n]) for i in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # neighbours share a vertex
            if _segments_cross(*edges[i], *edges[j]):
                return True
    return False


def _longest_edge_angle(pts: list[tuple[float, float]]) -> float:
    best, angle = -1.0, 0.0
    for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
        length = math.hypot(x2 - x1, y2 - y1)
        if length > best:
            best = length
            angle = math.degrees(math.atan2(x2 - x1, y2 - y1)) % 180.0
    return angle


def generate_survey(s: SurveyIn) -> SurveyResult:
    poly = list(s.polygon)
    if len(poly) > 3 and poly[0] == poly[-1]:
        poly = poly[:-1]  # closed ring given
    if len(poly) < 3:
        raise SurveyError("the area needs at least 3 corners")
    frame = _Frame(sum(p[0] for p in poly) / len(poly), sum(p[1] for p in poly) / len(poly))
    pts = [frame.to_xy(lat, lon) for lat, lon in poly]
    # Crossing edges first: a symmetric bowtie also has zero (signed) area.
    if _self_intersects(pts):
        raise SurveyError("the area's edges cross each other; redraw it")
    area = polygon_area(pts)
    if area < 1.0:
        raise SurveyError("the area is too small (or its corners are in a line)")

    angle = _longest_edge_angle(pts) if s.angle_deg is None else s.angle_deg
    th = math.radians(angle)
    d = (math.sin(th), math.cos(th))  # along the lines
    p = (math.cos(th), -math.sin(th))  # across the lines

    uv = [(x * d[0] + y * d[1], x * p[0] + y * p[1]) for x, y in pts]
    vmin = min(v for _, v in uv)
    vmax = max(v for _, v in uv)
    width = vmax - vmin
    n_lines = max(1, math.ceil(width / s.spacing_m - 1e-9))
    if n_lines > MAX_LINES:
        raise SurveyError(f"{n_lines} lines; increase the line spacing")
    step = width / n_lines  # spread lines evenly, never wider than spacing_m

    edges = list(zip(uv, uv[1:] + uv[:1]))
    lines: list[list[tuple[float, float]]] = []  # per line: [(u_start, u_end)], in flight order
    for i in range(n_lines):
        v = vmin + step * (i + 0.5)
        us = []
        for (ua, va), (ub, vb) in edges:
            if (va <= v < vb) or (vb <= v < va):
                us.append(ua + (v - va) * (ub - ua) / (vb - va))
        us.sort()
        segs = [(us[k], us[k + 1]) for k in range(0, len(us) - 1, 2) if us[k + 1] - us[k] > 0.5]
        if not segs:
            continue
        if len(lines) % 2 == 1:  # every other line flies the opposite way
            segs = [(b, a) for a, b in reversed(segs)]
        lines.append([(a, b, v) for a, b in segs])

    if not lines:
        raise SurveyError("no survey lines fit in the area; reduce the line spacing")

    def to_wp(u: float, v: float, photo: bool) -> dict:
        x = u * d[0] + v * p[0]
        y = u * d[1] + v * p[1]
        lat, lon = frame.to_latlon(x, y)
        return {"lat": lat, "lon": lon, "alt": s.alt, "actions": [{"action": "photo"}] if photo else []}

    waypoints: list[dict] = []
    photos = 0
    for line in lines:
        for a, b, v in line:
            length = abs(b - a)
            if s.photo_spacing_m is None:
                waypoints += [to_wp(a, v, False), to_wp(b, v, False)]
                continue
            n = max(1, math.ceil(length / s.photo_spacing_m - 1e-9))
            for k in range(n + 1):
                waypoints.append(to_wp(a + (b - a) * k / n, v, True))
            photos += n + 1

    if s.gimbal_pitch is not None:
        waypoints[0]["actions"].insert(0, {"action": "gimbal_pitch", "pitch": s.gimbal_pitch})

    mission = MissionIn.model_validate(
        {"name": s.name, "speed": s.speed, "finish_action": s.finish_action, "waypoints": waypoints}
    )
    return SurveyResult(mission=mission, area_m2=area, lines=len(lines), angle_deg=angle, photos=photos)
