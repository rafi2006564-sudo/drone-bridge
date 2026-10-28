"""Backend against the mock bridge over real WebSockets, in real time."""

import asyncio
import json
import socket

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from drone_protocol.messages import Event, HelloFromGroundStation, Stick, dump
from ground_station.config import Config
from ground_station.station import GroundStation
from mock_bridge.server import MockBridge
from mock_bridge.sim import AircraftSim


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Rig:
    """Mock bridge + ground station + a fake browser tab."""

    def __init__(self, tmp_path, go_home_timeout=10.0, **sections):
        self.port = free_port()
        self.cfg = Config.model_validate(
            {
                "rc": {"port": self.port, "reconnect_delay_s": 0.2},
                "safety": {"rc_go_home_timeout_s": go_home_timeout},
                "logging": {"dir": str(tmp_path)},
                "storage": {"dir": str(tmp_path)},
                **sections,
            }
        )
        self.bridge = MockBridge(AircraftSim(lat=47.0, lon=8.0))
        self.gs = GroundStation(self.cfg)
        self.ui_queue = None
        self.sticks = {"lx": 0.0, "ly": 0.0, "rx": 0.0, "ry": 0.0}
        self._tasks = []
        self.bridge_events: list[str] = []
        orig = self.bridge.event

        async def spy(level, code, message, data=None):
            self.bridge_events.append(code)
            await orig(level, code, message, data)

        self.bridge.event = spy

    async def __aenter__(self):
        self.server = await self.bridge.serve("127.0.0.1", self.port)
        self._tasks.append(asyncio.create_task(self.bridge.run()))
        await self.gs.start()
        await self.until(lambda: self.gs.limits_applied, 3)
        return self

    async def __aexit__(self, *exc):
        self.close_tab()
        for t in self._tasks:
            t.cancel()
        await self.gs.stop()
        self.server.close()
        await self.server.wait_closed()

    def open_tab(self):
        self.ui_queue = self.gs.claim_ui()

        async def browser():
            seq = 0
            while True:
                self.gs.handle_ui(json.dumps({"type": "ui_heartbeat", "seq": seq}))
                self.gs.handle_ui(json.dumps({"type": "stick", **self.sticks}))
                seq += 1
                await asyncio.sleep(0.05)

        self._browser = asyncio.create_task(browser())

    def close_tab(self):
        if self.ui_queue is not None:
            self._browser.cancel()
            self.gs.release_ui(self.ui_queue)
            self.ui_queue = None

    async def until(self, cond, timeout):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError("condition not reached in time")
            await asyncio.sleep(0.02)

    async def airborne_with_virtual_stick(self):
        self.bridge.sim.gps_override = 18
        await self.until(lambda: self.gs.telemetry is not None and self.gs.telemetry.home_set, 3)
        assert (await self.gs.execute("takeoff", {}, True)).ok
        assert (await self.gs.execute("enable_virtual_stick", {}, True)).ok
        await self.until(lambda: self.bridge.sim.mode == "VIRTUAL_STICK", 3)


def arun(coro):
    asyncio.run(asyncio.wait_for(coro, 30))


def test_handshake_and_limits(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            assert rig.gs.link.peer.aircraft_model == "MOCK_MATRICE_4E"
            assert rig.bridge.sim.height_limit == 120 and rig.bridge.sim.rth_altitude == 50

    arun(scenario())


def test_takeoff_refused_until_gps_and_home(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.bridge.sim.gps_override = 4
            await rig.until(lambda: rig.gs.telemetry is not None, 2)
            r = await rig.gs.execute("takeoff", {}, True)
            assert not r.ok and "GPS" in r.error and "home point" in r.error
            assert not rig.bridge.sim.motors_on

    arun(scenario())


def test_fly_with_sticks(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.open_tab()
            await rig.airborne_with_virtual_stick()
            lat0 = rig.bridge.sim.lat
            rig.sticks["ry"] = 1.0  # forward, scaled to 30%
            await asyncio.sleep(2.0)
            assert rig.bridge.sim.lat > lat0
            assert 2.0 < rig.bridge.sim.vn <= 3.05  # 30% of 10 m/s
            rig.sticks["ry"] = 0.0
            await rig.until(lambda: abs(rig.bridge.sim.vn) < 0.2, 3)

    arun(scenario())


def test_closing_tab_makes_aircraft_hover(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.open_tab()
            await rig.airborne_with_virtual_stick()
            rig.sticks["ry"] = 1.0
            await asyncio.sleep(1.0)
            rig.close_tab()
            await rig.until(lambda: not rig.bridge.sim.virtual_stick, 2.0)
            assert "watchdog_hover" in rig.bridge_events
            assert rig.bridge.sim.mode == "GPS_NORMAL"
            assert rig.bridge.sim.sticks == (0.0, 0.0, 0.0, 0.0)
            await rig.until(lambda: abs(rig.bridge.sim.vn) < 0.2, 3)
            # Opening the tab again does not re-enable virtual stick.
            rig.open_tab()
            await asyncio.sleep(0.5)
            assert not rig.bridge.sim.virtual_stick
            assert "heartbeat_restored" in rig.bridge_events

    arun(scenario())


def test_killing_backend_hovers_then_goes_home(tmp_path):
    async def scenario():
        async with Rig(tmp_path, go_home_timeout=2.0) as rig:
            rig.open_tab()
            await rig.airborne_with_virtual_stick()
            await rig.gs.stop()  # backend gone
            rig.gs.start = rig.gs.stop = _noop
            await rig.until(lambda: "watchdog_hover" in rig.bridge_events, 2)
            assert not rig.bridge.sim.virtual_stick
            await rig.until(lambda: "watchdog_go_home" in rig.bridge_events, 3)
            # Already above home, so go home turns straight into landing.
            assert rig.bridge.sim.mode in ("GO_HOME", "AUTO_LANDING")

    arun(scenario())


def test_rc_override_reported_and_not_reenabled(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.open_tab()
            await rig.airborne_with_virtual_stick()
            await rig.bridge.rc_override()
            await rig.until(lambda: not rig.gs.telemetry.virtual_stick_enabled, 2)
            events = []
            while not rig.ui_queue.empty():
                events.append(rig.ui_queue.get_nowait())
            assert any(e.get("code") == "virtual_stick_overridden" for e in events)
            await asyncio.sleep(0.5)
            assert not rig.bridge.sim.virtual_stick

    arun(scenario())


def test_bridge_drops_stale_and_clamps_sticks(tmp_path):
    async def scenario():
        bridge = MockBridge(AircraftSim(lat=47.0, lon=8.0))
        bridge.sim.virtual_stick = True
        bridge.sim.is_flying = True
        sent_t = 50.0
        await bridge.handle_frame(dump(Stick(lx=0, ly=0, rx=0, ry=0.5, t=sent_t)))
        bridge.clock = lambda: bridge.ages._offset + sent_t + 0.05 + 0.3  # 300 ms later than the fastest
        await bridge.handle_frame(dump(Stick(lx=0, ly=0, rx=0, ry=1.0, t=sent_t + 0.05)))
        assert bridge.stale_sticks_dropped == 1
        assert bridge.sim.sticks[3] == 0.5

    arun(scenario())


def test_bridge_accepts_one_client_and_checks_version(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            uri = f"ws://127.0.0.1:{rig.port}"
            async with connect(uri, proxy=None) as second:  # backend already holds the slot
                with pytest.raises(ConnectionClosed) as e:
                    await second.recv()
                assert e.value.rcvd.code == 4001
        bridge = MockBridge(AircraftSim(lat=47.0, lon=8.0))
        port = free_port()
        async with await bridge.serve("127.0.0.1", port):
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
                await ws.send(json.dumps({"type": "hello", "protocol_version": 99, "go_home_timeout_s": 10}))
                reply = json.loads(await ws.recv())
                assert reply["protocol_version"] == 1
                with pytest.raises(ConnectionClosed) as e:
                    await ws.recv()
                assert e.value.rcvd.code == 4002

    arun(scenario())


def test_invalid_command_gets_negative_ack(tmp_path):
    async def scenario():
        bridge = MockBridge(AircraftSim(lat=47.0, lon=8.0))
        port = free_port()
        async with await bridge.serve("127.0.0.1", port):
            async with connect(f"ws://127.0.0.1:{port}", proxy=None) as ws:
                await ws.send(dump(HelloFromGroundStation(protocol_version=1, go_home_timeout_s=10)))
                await ws.recv()
                await ws.send(json.dumps({"type": "command", "id": "x1", "name": "gimbal_set", "args": {"pitch": 99}}))
                ack = json.loads(await ws.recv())
                assert ack == {"type": "ack", "id": "x1", "ok": False, "error": ack["error"]}
                assert "pitch" in ack["error"]

    arun(scenario())


async def _noop():
    pass
