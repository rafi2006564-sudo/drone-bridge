"""GroundStation logic against a fake RC link and a fake clock."""

import asyncio
import json

import pytest

from drone_protocol.messages import Heartbeat, HelloFromBridge, Stick
from ground_station.config import Config
from ground_station.rc_link import CommandResult
from ground_station.station import GroundStation

from .helpers import telemetry


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeLink:
    uri = "ws://fake"

    def __init__(self) -> None:
        self.ready = True
        self.peer = HelloFromBridge(
            protocol_version=1, msdk_version="x", aircraft_model="x", aircraft_firmware="x", rc_firmware="x"
        )
        self.sent: list = []
        self.commands: list = []

    async def send(self, msg, log_it=True):
        if not self.ready:
            return False
        self.sent.append(msg)
        return True

    async def command(self, name, args=None):
        self.commands.append((name, args))
        return CommandResult(True)

    def take(self, kind):
        out = [m for m in self.sent if isinstance(m, kind)]
        self.sent = [m for m in self.sent if not isinstance(m, kind)]
        return out


@pytest.fixture
def gs(tmp_path):
    cfg = Config.model_validate({"logging": {"dir": str(tmp_path)}, "storage": {"dir": str(tmp_path)}})
    clock = FakeClock()
    station = GroundStation(cfg, clock=clock)
    station.link = FakeLink()
    station.clock_ = clock
    station.limits_applied = True
    yield station
    station.log.close()


def ui(gs, **msg):
    gs.handle_ui(json.dumps(msg))


def run(coro):
    return asyncio.run(coro)


def tick(gs, dt=0.05):
    gs.clock_.now += dt
    run(gs.tick())


def test_no_heartbeats_without_ui(gs):
    for _ in range(20):
        tick(gs)
    assert gs.link.take(Heartbeat) == []


def test_heartbeats_relayed_only_while_ui_alive(gs):
    gs.claim_ui()
    ui(gs, type="ui_heartbeat", seq=0)
    for _ in range(10):  # 0.5 s at 20 Hz, UI beat at t=0 only
        tick(gs)
    sent = gs.link.take(Heartbeat)
    assert 4 <= len(sent) <= 6  # ~10 Hz while alive
    tick(gs)  # 0.55 s since the last UI heartbeat
    for _ in range(10):
        tick(gs)
    assert gs.link.take(Heartbeat) == []


def test_ui_loss_sends_zero_stick_immediately(gs):
    gs.claim_ui()
    gs.telemetry = telemetry(virtual_stick_enabled=True, is_flying=True)
    gs._telemetry_at = gs.clock_.now
    ui(gs, type="ui_heartbeat", seq=0)
    ui(gs, type="stick", lx=0, ly=0, rx=0, ry=1.0)
    tick(gs)
    assert gs.link.take(Stick)[-1].ry == pytest.approx(0.3)  # default 30% scale
    gs.clock_.now += 0.6  # UI silent
    run(gs.tick())
    sticks = gs.link.take(Stick)
    assert sticks and all(s.lx == s.ly == s.rx == s.ry == 0 for s in sticks)


def test_closed_tab_stops_heartbeats_at_once(gs):
    q = gs.claim_ui()
    ui(gs, type="ui_heartbeat", seq=0)
    tick(gs)
    gs.link.take(Heartbeat)
    gs.release_ui(q)
    tick(gs, 0.01)
    tick(gs, 0.1)
    assert gs.link.take(Heartbeat) == []


def test_stale_ui_stick_becomes_zero(gs):
    gs.claim_ui()
    gs.telemetry = telemetry(virtual_stick_enabled=True)
    ui(gs, type="stick", lx=1, ly=1, rx=1, ry=1)
    for _ in range(8):  # 0.4 s with heartbeats but no new stick input
        ui(gs, type="ui_heartbeat", seq=0)
        tick(gs)
    assert gs.link.take(Stick)[-1] .ry == 0


def test_no_sticks_sent_unless_virtual_stick_enabled(gs):
    gs.claim_ui()
    gs.telemetry = telemetry(virtual_stick_enabled=False)
    ui(gs, type="ui_heartbeat", seq=0)
    ui(gs, type="stick", lx=0, ly=0, rx=0, ry=1)
    tick(gs)
    assert gs.link.take(Stick) == []


def test_speed_scale_capped_by_config(gs):
    gs.cfg.safety.speed_scale_max = 0.5
    gs.claim_ui()
    ui(gs, type="set_speed_scale", value=0.9)
    assert gs.speed_scale == 0.5


def test_out_of_range_ui_sticks_rejected(gs):
    gs.claim_ui()
    ui(gs, type="stick", lx=0, ly=3, rx=0, ry=0)
    assert gs._ui_sticks_at is None


@pytest.mark.parametrize("name", ["takeoff", "enable_virtual_stick", "mission_start"])
def test_confirmation_required(gs, name):
    gs.telemetry = telemetry()
    gs._telemetry_at = gs.clock_.now
    r = run(gs.execute(name, {}, confirmed=False))
    assert not r.ok and "confirmation" in r.error
    assert gs.link.commands == []


def test_takeoff_runs_preflight(gs):
    gs.telemetry = telemetry(gps_sats=3)
    gs._telemetry_at = gs.clock_.now
    r = run(gs.execute("takeoff", {}, confirmed=True))
    assert not r.ok and "GPS satellites 3" in r.error
    assert gs.link.commands == []
    gs.telemetry = telemetry()
    assert run(gs.execute("takeoff", {}, confirmed=True)).ok
    assert gs.link.commands == [("takeoff", {})]


@pytest.mark.parametrize("name", ["set_height_limit", "set_rth_altitude", "mission_upload", "motors_off"])
def test_ui_cannot_send(gs, name):
    r = run(gs.execute(name, {"m": 500}, confirmed=True))
    assert not r.ok
    assert gs.link.commands == []


def test_video_url_comes_from_config(gs):
    gs.cfg.video.push_url = "rtmp://10.0.0.2:1935/drone"
    run(gs.execute("video_start", {"url": "rtmp://evil/x"}, confirmed=False))
    assert gs.link.commands == [("video_start", {"url": "rtmp://10.0.0.2:1935/drone"})]


def test_limits_applied_on_connect(gs):
    async def scenario():
        gs.limits_applied = False
        gs._on_connection(gs.link.peer)
        await asyncio.gather(*gs._background)

    run(scenario())
    assert gs.link.commands == [("set_height_limit", {"m": 120.0}), ("set_rth_altitude", {"m": 50.0})]
    assert gs.limits_applied


def test_second_ui_rejected(gs):
    q = gs.claim_ui()
    assert q is not None
    assert gs.claim_ui() is None
    gs.release_ui(q)
    assert gs.claim_ui() is not None
