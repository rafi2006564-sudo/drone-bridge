"""Missions and scheduled sorties end to end, backend against the mock bridge."""

import asyncio
import time

from .test_integration import Rig, arun
from .test_mission import mission, wp

NO_LEAD = {"schedule": {"min_lead_s": 0}}


async def ready(rig):
    rig.bridge.sim.gps_override = 18
    await rig.until(lambda: rig.gs.home() is not None, 3)
    # The mock records home where it starts, which is the test HOME point.
    return rig.gs.missions.save(mission(waypoints=[wp(15, alt=6), wp(30, alt=6)], speed=8))


def schedule_now(rig, m, delay=0.3):
    r = rig.gs.scheduler.create(m.id, time.time() + delay, confirmed=True)
    assert r.ok, r.error
    return rig.gs.scheduler.schedules[-1]


def test_scheduled_sortie_flies_the_mission(tmp_path):
    async def scenario():
        async with Rig(tmp_path, **NO_LEAD) as rig:
            rig.open_tab()
            m = await ready(rig)
            s = schedule_now(rig, m)
            await rig.until(lambda: s.state == "launched", 15)
            assert rig.bridge.sim.mission_state == "executing"
            await rig.until(lambda: "mission_finished" in rig.bridge_events, 20)
            assert rig.bridge_events.count("mission_progress") == 2
            assert rig.bridge.sim.mode == "GPS_NORMAL"  # finish_action hover
            assert not rig.bridge.sim.virtual_stick

    arun(scenario())


def test_sortie_aborts_without_an_open_ui(tmp_path):
    async def scenario():
        async with Rig(tmp_path, **NO_LEAD) as rig:
            m = await ready(rig)
            s = schedule_now(rig, m)
            await rig.until(lambda: s.state == "aborted", 5)
            assert "no browser UI" in s.detail
            assert not rig.bridge.sim.motors_on and rig.bridge.sim.mission is None

    arun(scenario())


def test_sortie_aborts_when_preflight_fails(tmp_path):
    async def scenario():
        async with Rig(tmp_path, **NO_LEAD) as rig:
            rig.open_tab()
            m = await ready(rig)
            rig.bridge.sim.battery = 20
            s = schedule_now(rig, m)
            await rig.until(lambda: s.state == "aborted", 5)
            assert "battery" in s.detail and "< 30%" in s.detail
            assert not rig.bridge.sim.motors_on

    arun(scenario())


def test_cancel_after_takeoff_lands(tmp_path):
    async def scenario():
        async with Rig(tmp_path, **NO_LEAD) as rig:
            rig.open_tab()
            m = await ready(rig)
            s = schedule_now(rig, m)
            await rig.until(lambda: rig.bridge.sim.is_flying, 5)
            assert rig.gs.scheduler.cancel(s.id).ok
            await rig.until(lambda: s.state == "aborted", 5)
            assert "cancelled" in s.detail and "landing" in s.detail
            assert rig.bridge.sim.mode == "AUTO_LANDING"
            assert rig.bridge.sim.mission_state != "executing"

    arun(scenario())


def test_upload_is_validated_against_home(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.open_tab()
            await ready(rig)
            far = rig.gs.missions.save(mission(waypoints=[wp(100), wp(5000)]))
            r = await rig.gs.execute("mission_upload", {"mission_id": far.id}, False)
            assert not r.ok and "5000 m from home" in r.error
            assert rig.bridge.sim.mission is None

    arun(scenario())


def test_ui_loss_pauses_a_running_mission(tmp_path):
    async def scenario():
        async with Rig(tmp_path) as rig:
            rig.open_tab()
            m = await ready(rig)
            assert (await rig.gs.execute("mission_upload", {"mission_id": m.id}, False)).ok
            assert (await rig.gs.execute("takeoff", {}, True)).ok
            await rig.until(lambda: rig.bridge.sim.mode == "GPS_NORMAL", 5)
            assert not (await rig.gs.execute("mission_start", {}, False)).ok  # needs confirmation
            assert (await rig.gs.execute("mission_start", {}, True)).ok
            await asyncio.sleep(0.5)
            rig.close_tab()
            await rig.until(lambda: rig.bridge.sim.mission_state == "paused", 2.5)
            assert "watchdog_hover" in rig.bridge_events

    arun(scenario())
