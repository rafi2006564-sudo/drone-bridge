import math

import pytest

from drone_protocol.messages import ActiveWarning
from drone_protocol.timing import AgeEstimator
from ground_station.config import Config
from ground_station.safety import HeartbeatMonitor, Sticks, clamp_unit, preflight_failures
from mock_bridge.watchdog import Action, BridgeWatchdog

from .helpers import telemetry

# ---------------------------------------------------------------- UI heartbeat


def test_heartbeat_monitor_timing():
    hb = HeartbeatMonitor(0.5)
    assert not hb.alive(0.0)  # never beaten
    hb.beat(10.0)
    assert hb.alive(10.0)
    assert hb.alive(10.5)
    assert not hb.alive(10.51)
    hb.beat(11.0)
    assert hb.alive(11.2)
    hb.lost()
    assert not hb.alive(11.2)


# ---------------------------------------------------------------- sticks


@pytest.mark.parametrize(
    "v,out", [(0.3, 0.3), (1.0, 1.0), (1.5, 1.0), (-7, -1.0), (math.nan, 0.0), (math.inf, 0.0), (-math.inf, 0.0)]
)
def test_clamp_unit(v, out):
    assert clamp_unit(v) == out


def test_sticks_scaled_and_clamped():
    s = Sticks(1.0, -1.0, 0.5, 2.0).scaled(0.3)
    assert s == pytest.approx(Sticks(0.3, -0.3, 0.15, 0.3))
    assert Sticks(1, 1, 1, 1).scaled(5.0) == Sticks(1, 1, 1, 1)  # scale capped at 1
    assert Sticks(1, 1, 1, 1).scaled(math.nan) == Sticks(0, 0, 0, 0)
    assert Sticks(math.nan, 0, 0, 0).scaled(1.0).lx == 0.0


# ---------------------------------------------------------------- pre-takeoff checks


def test_preflight_ok():
    assert preflight_failures(Config(), telemetry(), 0.1, True) == []


@pytest.mark.parametrize(
    "overrides,age,limits,expect",
    [
        ({"gps_sats": 9}, 0.1, True, "GPS satellites 9 < 10"),
        ({"home_set": False}, 0.1, True, "home point not set"),
        ({"battery_pct": 29}, 0.1, True, "battery 29% < 30%"),
        ({"battery_pct": None}, 0.1, True, "battery level unknown"),
        (
            {"active_warnings": [ActiveWarning(code="imu_error", level="critical", message="x")]},
            0.1,
            True,
            "critical warning active: imu_error",
        ),
        ({}, 2.0, True, "telemetry is stale"),
        ({}, 0.1, False, "height limit / RTH altitude not confirmed"),
        ({"is_flying": True}, 0.1, True, "already flying"),
    ],
)
def test_preflight_refusals(overrides, age, limits, expect):
    reasons = preflight_failures(Config(), telemetry(**overrides), age, limits)
    assert any(expect in r for r in reasons), reasons


def test_preflight_ignores_non_critical_warnings():
    t = telemetry(active_warnings=[ActiveWarning(code="battery_low", level="warning", message="x")])
    assert preflight_failures(Config(), t, 0.1, True) == []


def test_preflight_without_telemetry():
    assert preflight_failures(Config(), None, None, True) == ["no telemetry from the aircraft"]


def test_preflight_thresholds_at_boundary_pass():
    cfg = Config()
    t = telemetry(gps_sats=cfg.preflight.min_gps_sats, battery_pct=cfg.preflight.min_battery_pct)
    assert preflight_failures(cfg, t, 0.1, True) == []


# ---------------------------------------------------------------- stick age


def test_age_estimator_with_clock_offset():
    est = AgeEstimator()
    offset = 5000.0  # the two clocks are unrelated
    assert est.observe(1.000, 1.003 + offset) == 0.0
    assert est.observe(1.050, 1.052 + offset) == pytest.approx(0.0)  # faster delivery lowers the baseline
    assert est.observe(1.100, 1.102 + offset) == pytest.approx(0.0)
    assert est.observe(1.150, 1.402 + offset) == pytest.approx(0.25, abs=1e-3)  # delayed 250 ms
    assert est.observe(1.200, 1.203 + offset) == pytest.approx(0.001, abs=1e-3)


def test_age_estimator_cannot_hide_sustained_delay_quickly():
    est = AgeEstimator()
    est.observe(0.0, 0.0)
    # Every message from now on is 300 ms late, for 60 s at 20 Hz.
    ages = [est.observe(k * 0.05, k * 0.05 + 0.3) for k in range(1, 1201)]
    assert min(ages) > 0.28


def test_age_estimator_tracks_clock_drift():
    est = AgeEstimator()
    drift = 50e-6  # sender clock slow by 50 ppm
    ages = [est.observe(k * 0.05 * (1 - drift), k * 0.05) for k in range(0, 72000)]  # 1 h
    assert max(ages) < 0.01


# ---------------------------------------------------------------- bridge watchdog


def test_watchdog_not_armed_before_first_heartbeat():
    wd = BridgeWatchdog(10.0)
    assert wd.check(100.0, is_flying=True, motors_on=True) == []


def test_watchdog_hover_then_go_home_once():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    assert wd.check(0.99, True, True) == []
    assert wd.check(1.0, True, True) == [Action.HOVER]
    assert wd.check(5.0, True, True) == []
    assert wd.check(9.99, True, True) == []
    assert wd.check(10.0, True, True) == [Action.GO_HOME]
    assert wd.check(30.0, True, True) == []


def test_watchdog_no_go_home_on_ground():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    assert wd.check(1.0, False, False) == [Action.HOVER]
    assert wd.check(11.0, False, False) == []


def test_watchdog_no_go_home_if_pilot_takes_off_after_loss():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    wd.check(1.0, False, False)  # lost on the ground
    assert wd.check(20.0, True, True) == []  # pilot flying by hand later


def test_watchdog_go_home_if_lost_during_takeoff():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    assert wd.check(1.0, False, True) == [Action.HOVER]  # motors on, climbing
    assert wd.check(10.0, True, True) == [Action.GO_HOME]


def test_watchdog_restored_and_rearmed():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    wd.check(1.0, True, True)
    assert wd.heartbeat(1.5) == [Action.RESTORED]
    assert wd.heartbeat(1.6) == []
    assert wd.check(2.59, True, True) == []
    assert wd.check(2.6, True, True) == [Action.HOVER]


def test_watchdog_single_late_check_fires_both():
    wd = BridgeWatchdog(10.0)
    wd.heartbeat(0.0)
    assert wd.check(12.0, True, True) == [Action.HOVER, Action.GO_HOME]
