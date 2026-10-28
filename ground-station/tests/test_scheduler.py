"""Scheduler rules with a fake wall clock (no launch sequence; see test_sortie.py)."""

import json

import pytest

from ground_station.config import Config
from ground_station.mission import MissionIn
from ground_station.scheduler import Schedule, Scheduler
from ground_station.station import GroundStation

from .helpers import telemetry
from .test_mission import mission, wp
from .test_station import FakeLink


class Wall:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def env(tmp_path):
    cfg = Config.model_validate({"logging": {"dir": str(tmp_path)}, "storage": {"dir": str(tmp_path)}})
    gs = GroundStation(cfg)
    gs.link = FakeLink()
    gs.telemetry = telemetry()
    wall = Wall()
    gs.scheduler = Scheduler(gs, tmp_path / "schedules.json", wall=wall)
    m = gs.missions.save(mission())
    yield gs, wall, m
    gs.log.close()


def test_create_requires_confirmation(env):
    gs, wall, m = env
    r = gs.scheduler.create(m.id, wall.now + 3600, confirmed=False)
    assert not r.ok and "confirmation" in r.error
    assert gs.scheduler.schedules == []


@pytest.mark.parametrize(
    "offset,expect",
    [(30, "at least 60 s from now"), (-10, "at least 60 s"), (8 * 86400, "within 7 days"), (float("nan"), "invalid")],
)
def test_create_time_limits(env, offset, expect):
    gs, wall, m = env
    r = gs.scheduler.create(m.id, wall.now + offset, confirmed=True)
    assert not r.ok and expect in r.error


def test_create_rejects_invalid_mission(env):
    gs, wall, _ = env
    bad = gs.missions.save(mission(waypoints=[wp(100), wp(5000)]))  # too far from home
    r = gs.scheduler.create(bad.id, wall.now + 3600, confirmed=True)
    assert not r.ok and "from home" in r.error
    assert not gs.scheduler.create("deadbeef", wall.now + 3600, confirmed=True).ok


def test_create_keeps_sorties_apart(env):
    gs, wall, m = env
    assert gs.scheduler.create(m.id, wall.now + 3600, confirmed=True).ok
    r = gs.scheduler.create(m.id, wall.now + 3600 + 300, confirmed=True)
    assert not r.ok and "10 min apart" in r.error
    assert gs.scheduler.create(m.id, wall.now + 3600 + 700, confirmed=True).ok


def test_persisted_and_reloaded(env, tmp_path):
    gs, wall, m = env
    gs.scheduler.create(m.id, wall.now + 3600, confirmed=True)
    again = Scheduler(gs, tmp_path / "schedules.json", wall=wall)
    assert [s.state for s in again.schedules] == ["pending"]


def test_reload_marks_missed_and_interrupted(env, tmp_path):
    gs, wall, m = env
    path = tmp_path / "schedules.json"
    items = [
        Schedule(id="a1", mission_id=m.id, mission_name="x", launch_at=wall.now - 120, created_at=0),
        Schedule(id="a2", mission_id=m.id, mission_name="x", launch_at=wall.now - 30, created_at=0),
        Schedule(id="a3", mission_id=m.id, mission_name="x", launch_at=wall.now - 5, created_at=0, state="launching"),
    ]
    path.write_text(json.dumps([s.model_dump() for s in items]))
    states = {s.id: s.state for s in Scheduler(gs, path, wall=wall).schedules}
    assert states == {"a1": "missed", "a2": "pending", "a3": "aborted"}


def test_never_launches_late(env):
    gs, wall, m = env
    gs.scheduler.create(m.id, wall.now + 3600, confirmed=True)
    wall.now += 3600 + 61  # backend was asleep through the launch window
    gs.scheduler.tick()
    s = gs.scheduler.schedules[0]
    assert s.state == "missed"
    assert gs.link.commands == []


def test_warning_before_launch(env):
    gs, wall, m = env
    q = gs.claim_ui()
    gs.scheduler.create(m.id, wall.now + 3600, confirmed=True)
    wall.now += 3600 - 61
    gs.scheduler.tick()
    wall.now += 2
    gs.scheduler.tick()
    gs.scheduler.tick()
    codes = []
    while not q.empty():
        msg = q.get_nowait()
        if msg.get("type") == "event":
            codes.append(msg["code"])
    assert codes.count("sortie_imminent") == 1


def test_cancel_and_remove(env):
    gs, wall, m = env
    gs.scheduler.create(m.id, wall.now + 3600, confirmed=True)
    s = gs.scheduler.schedules[0]
    assert not gs.scheduler.remove(s.id).ok  # must cancel first
    assert gs.scheduler.cancel(s.id).ok
    assert s.state == "cancelled"
    wall.now += 3600
    gs.scheduler.tick()
    assert s.state == "cancelled" and gs.link.commands == []
    assert gs.scheduler.remove(s.id).ok and gs.scheduler.schedules == []


def test_mission_in_use_cannot_be_deleted_or_edited(env):
    gs, wall, m = env
    gs.claim_ui()
    gs.scheduler.create(m.id, wall.now + 3600, confirmed=True)
    from ground_station.ui_messages import UiOp

    r = gs._ui_op(UiOp(type="op", id="1", op="delete_mission", args={"id": m.id}))
    assert not r["ok"] and "pending sortie" in r["error"]
    r = gs._ui_op(UiOp(type="op", id="2", op="save_mission", args={"id": m.id, "mission": mission().model_dump(mode="json")}))
    assert not r["ok"]
    assert isinstance(gs.missions.get(m.id), MissionIn)
