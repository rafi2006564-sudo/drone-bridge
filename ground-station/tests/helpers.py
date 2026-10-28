from drone_protocol.messages import Telemetry

_DEFAULTS = dict(
    seq=1, t=0.0, lat=47.0, lon=8.0, alt_rel=0.0, heading=0.0, vel_n=0.0, vel_e=0.0, vel_d=0.0,
    pitch=0.0, roll=0.0, yaw=0.0, battery_pct=90, gps_sats=18, gps_level=5,
    home_lat=47.0, home_lon=8.0, home_set=True, flight_mode="MANUAL", motors_on=False,
    is_flying=False, virtual_stick_enabled=False, simulator_active=False, gimbal_pitch=0.0,
    gimbal_yaw=0.0, rc_link_quality=100, video_link_quality=None, mission_state="idle",
    active_warnings=[], camera_recording=False, camera_lens="wide", zoom_ratio=1.0,
)


def telemetry(**overrides) -> Telemetry:
    return Telemetry(**{**_DEFAULTS, **overrides})
