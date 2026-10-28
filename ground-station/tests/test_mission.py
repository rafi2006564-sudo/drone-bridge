import pytest

from ground_station.config import Config
from ground_station.mission import MissionIn, MissionStore, distance_m, upload_args, validate_mission

HOME = (47.0, 8.0)
M_PER_DEG_LAT = 111_194.9  # at EARTH_R = 6371 km


def north(m: float) -> float:
    return HOME[0] + m / M_PER_DEG_LAT


def mission(**kw) -> MissionIn:
    data = {
        "name": "test",
        "speed": 5.0,
        "finish_action": "hover",
        "waypoints": [
            {"lat": north(100), "lon": HOME[1], "alt": 30},
            {"lat": north(200), "lon": HOME[1], "alt": 30},
        ],
        **kw,
    }
    return MissionIn.model_validate(data)


def wp(meters_north: float, alt: float = 30, **kw) -> dict:
    return {"lat": north(meters_north), "lon": HOME[1], "alt": alt, **kw}


def test_valid_mission_report():
    r = validate_mission(mission(), Config(), HOME)
    assert r.ok, r.errors
    assert r.home_checked
    assert r.length_m == pytest.approx(200, rel=1e-3)  # home -> 100 m -> 200 m
    assert r.duration_s == pytest.approx(40, rel=1e-3)
    assert r.max_distance_from_home_m == pytest.approx(200, rel=1e-3)
    assert r.max_alt_m == 30


def test_go_home_and_first_waypoint_add_return_leg():
    assert validate_mission(mission(finish_action="go_home"), Config(), HOME).length_m == pytest.approx(400, rel=1e-3)
    assert validate_mission(mission(finish_action="first_waypoint"), Config(), HOME).length_m == pytest.approx(300, rel=1e-3)


def test_waypoint_speed_used_for_its_leg():
    m = mission(waypoints=[wp(100), wp(200, speed=10)])
    assert validate_mission(m, Config(), HOME).duration_s == pytest.approx(100 / 5 + 100 / 10, rel=1e-3)


@pytest.mark.parametrize(
    "kw,expect",
    [
        ({"waypoints": [wp(100)]}, "at least 2 waypoints"),
        ({"waypoints": [wp(100), wp(600)]}, "waypoint 2 is 600 m from home"),
        ({"waypoints": [wp(100), wp(200, alt=121)]}, "above the height limit"),
        ({"waypoints": [wp(100, alt=2), wp(200)]}, "below the minimum"),
        ({"speed": 20}, "mission speed 20 m/s exceeds 15"),
        ({"waypoints": [wp(100), wp(200, speed=16)]}, "waypoint 2: speed 16"),
    ],
)
def test_invalid_missions(kw, expect):
    r = validate_mission(mission(**kw), Config(), HOME)
    assert not r.ok
    assert any(expect in e for e in r.errors), r.errors


def test_too_many_waypoints():
    cfg = Config.model_validate({"limits": {"max_waypoints": 3}})
    r = validate_mission(mission(waypoints=[wp(10 * i) for i in range(1, 5)]), cfg, HOME)
    assert any("4 waypoints exceeds the limit of 3" in e for e in r.errors)


def test_without_home_distance_is_not_checked():
    r = validate_mission(mission(waypoints=[wp(100), wp(5000)]), Config(), None)
    assert r.ok and not r.home_checked and r.max_distance_from_home_m is None


def test_upload_args_match_protocol():
    from drone_protocol.messages import make_command

    m = mission(waypoints=[wp(100, actions=[{"action": "photo"}]), wp(200, actions=[{"action": "gimbal_pitch", "pitch": -90}])])
    cmd = make_command("c1", "mission_upload", upload_args(m))
    assert cmd.args.waypoints[1].actions[0].pitch == -90


def test_distance():
    assert distance_m(47, 8, north(1000), 8) == pytest.approx(1000, rel=1e-6)


def test_store_round_trip(tmp_path):
    store = MissionStore(tmp_path)
    m = store.save(mission(name="Field A"))
    assert store.get(m.id) == m
    m2 = store.save(mission(name="Field A v2"), m.id)
    assert m2.id == m.id and m2.created == m.created and m2.name == "Field A v2"
    assert [x.name for x in store.list()] == ["Field A v2"]
    assert store.delete(m.id) and store.get(m.id) is None


@pytest.mark.parametrize("bad_id", ["../../etc", "..\\x", "ABCDEFGH", "", "1234567"])
def test_store_rejects_bad_ids(tmp_path, bad_id):
    store = MissionStore(tmp_path)
    assert store.get(bad_id) is None
    assert not store.delete(bad_id)
    with pytest.raises(KeyError):
        store.save(mission(), bad_id)
