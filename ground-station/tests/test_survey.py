import math

import pytest

from ground_station.config import Config
from ground_station.mission import distance_m, validate_mission
from ground_station.survey import SurveyError, SurveyIn, _Frame, generate_survey

LAT0, LON0 = 47.0, 8.0
FRAME = _Frame(LAT0, LON0)


def ll(x: float, y: float) -> tuple[float, float]:
    """Metres east/north of the reference point -> (lat, lon)."""
    return FRAME.to_latlon(x, y)


def xy(wp) -> tuple[float, float]:
    return FRAME.to_xy(wp.lat, wp.lon)


def survey(polygon_m, **kw):
    data = {"polygon": [ll(x, y) for x, y in polygon_m], "alt": 40, "spacing_m": 10, "speed": 8, **kw}
    return generate_survey(SurveyIn.model_validate(data))


RECT = [(0, 0), (100, 0), (100, 50), (0, 50)]  # 100 m east-west, 50 m north-south


def test_rectangle_auto_angle_runs_along_long_side():
    r = survey(RECT)
    assert r.angle_deg == pytest.approx(90)  # lines run east-west
    assert r.lines == 5  # 50 m / 10 m
    assert r.area_m2 == pytest.approx(5000, rel=1e-3)
    wps = r.mission.waypoints
    assert len(wps) == 10  # two ends per line
    ys = sorted({round(xy(w)[1], 3) for w in wps})
    assert ys == pytest.approx([5, 15, 25, 35, 45], abs=0.01)  # half a spacing inside the edges
    for w in wps:
        x, y = xy(w)
        assert -0.01 <= x <= 100.01 and 0 <= y <= 50
        assert w.alt == 40


def test_lines_alternate_direction():
    wps = survey(RECT, gimbal_pitch=None).mission.waypoints
    first_dx = xy(wps[1])[0] - xy(wps[0])[0]
    second_dx = xy(wps[3])[0] - xy(wps[2])[0]
    assert first_dx * second_dx < 0
    # the turn between lines is short (same side), not a diagonal back across
    assert distance_m(wps[1].lat, wps[1].lon, wps[2].lat, wps[2].lon) == pytest.approx(10, abs=0.01)


def test_explicit_angle():
    r = survey(RECT, angle_deg=0)  # north-south lines across the 100 m width
    assert r.lines == 10
    xs = sorted({round(xy(w)[0], 2) for w in r.mission.waypoints})
    assert xs[0] == pytest.approx(5, abs=0.01) and xs[-1] == pytest.approx(95, abs=0.01)


def test_spacing_never_exceeds_request():
    r = survey([(0, 0), (100, 0), (100, 47), (0, 47)], spacing_m=10)
    ys = sorted({round(xy(w)[1], 4) for w in r.mission.waypoints})
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    assert max(gaps) <= 10 and r.lines == 5


def test_photos_along_lines():
    r = survey(RECT, photo_spacing_m=20)
    # 100 m lines -> 5 intervals -> 6 photo points per line
    assert r.photos == 30 and len(r.mission.waypoints) == 30
    assert all(any(a.action == "photo" for a in w.actions) for w in r.mission.waypoints)


def test_gimbal_set_at_first_waypoint_before_photo():
    first = survey(RECT, photo_spacing_m=50).mission.waypoints[0]
    assert [a.action for a in first.actions] == ["gimbal_pitch", "photo"]
    assert first.actions[0].pitch == -90


def test_concave_polygon_splits_lines():
    # U shape: two 20 m wide arms joined at the bottom
    u_shape = [(0, 0), (60, 0), (60, 60), (40, 60), (40, 20), (20, 20), (20, 60), (0, 60)]
    r = survey(u_shape, angle_deg=90)  # east-west lines cross both arms above y=20
    upper = [w for w in r.mission.waypoints if xy(w)[1] > 20]
    assert len(upper) == 4 * 4  # 4 lines above y=20, two segments each
    for w in upper:
        x, _ = xy(w)
        assert x <= 20.01 or x >= 39.99  # never ends inside the gap


def test_self_intersecting_rejected():
    bowtie = [(0, 0), (50, 50), (50, 0), (0, 50)]
    with pytest.raises(SurveyError, match="cross"):
        survey(bowtie)


def test_degenerate_rejected():
    with pytest.raises(SurveyError, match="too small"):
        survey([(0, 0), (10, 0), (20, 0)])


def test_closed_ring_accepted():
    r = survey(RECT + [RECT[0]])
    assert r.lines == 5


def test_result_is_a_valid_mission():
    r = survey(RECT, photo_spacing_m=25)
    report = validate_mission(r.mission, Config(), (LAT0, LON0))
    assert report.ok, report.errors
    assert report.length_m > 5 * 100


def test_too_many_waypoints_reported_by_validation():
    r = survey([(0, 0), (400, 0), (400, 400), (0, 400)], spacing_m=5, photo_spacing_m=5)
    report = validate_mission(r.mission, Config(), (LAT0, LON0))
    assert any("exceeds the limit" in e for e in report.errors)


def test_frame_round_trip():
    lat, lon = ll(123.4, -56.7)
    x, y = FRAME.to_xy(lat, lon)
    assert (x, y) == pytest.approx((123.4, -56.7), abs=1e-6)
    assert math.isclose(distance_m(LAT0, LON0, *ll(0, 1000)), 1000, rel_tol=1e-6)
