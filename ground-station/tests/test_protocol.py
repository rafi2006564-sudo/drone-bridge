import json

import pytest

from drone_protocol import gen_schemas
from drone_protocol import messages as m


def stick(**kw):
    frame = {"type": "stick", "lx": 0, "ly": 0, "rx": 0, "ry": 0, "t": 1.0, **kw}
    return json.dumps(frame)


def test_schemas_are_up_to_date():
    for filename, text in gen_schemas.render().items():
        path = gen_schemas.SCHEMA_DIR / filename
        assert path.read_text(encoding="utf-8") == text, f"run python -m drone_protocol.gen_schemas ({filename})"


def test_valid_stick():
    s = m.parse_from_ground_station(stick(lx=-1, ly=1, rx=0.5, ry=-0.25))
    assert isinstance(s, m.Stick) and s.ly == 1.0


@pytest.mark.parametrize(
    "raw",
    [
        stick(lx=1.01),
        stick(ry=-2),
        stick(lx="0.5"),  # no coercion
        stick(extra=1),  # no unknown fields
        '{"type":"stick","lx":NaN,"ly":0,"rx":0,"ry":0,"t":1}',
        '{"type":"stick","lx":Infinity,"ly":0,"rx":0,"ry":0,"t":1}',
        '{"lx":0,"ly":0,"rx":0,"ry":0,"t":1}',  # no type
        '{"type":"nope"}',
        "not json",
        "[]",
    ],
)
def test_invalid_frames_rejected(raw):
    with pytest.raises(m.ProtocolError):
        m.parse_from_ground_station(raw)


def test_every_command_round_trips():
    samples = {
        "gimbal_rotate": {"pitch_rate": 10, "yaw_rate": 0},
        "gimbal_set": {"pitch": -45},
        "camera_zoom": {"ratio": 3},
        "camera_lens": {"lens": "zoom"},
        "mission_upload": {
            "waypoints": [
                {"lat": 1, "lon": 2, "alt": 30, "actions": [{"action": "photo"}]},
                {"lat": 1.001, "lon": 2, "alt": 30, "speed": 5, "actions": [{"action": "gimbal_pitch", "pitch": -90}]},
            ],
            "speed": 8,
            "finish_action": "go_home",
        },
        "set_height_limit": {"m": 120},
        "set_rth_altitude": {"m": 50},
        "simulator_start": {"lat": 47, "lon": 8},
        "video_start": {"url": "rtmp://192.168.1.10:1935/drone"},
    }
    for name in m.COMMAND_ARGS:
        cmd = m.make_command("c1", name, samples.get(name, {}))
        again = m.parse_from_ground_station(m.dump(cmd))
        assert again == cmd


def test_command_requires_args_object():
    with pytest.raises(m.ProtocolError):
        m.parse_from_ground_station('{"type":"command","id":"c1","name":"takeoff"}')


@pytest.mark.parametrize(
    "name,args",
    [
        ("gimbal_set", {}),
        ("gimbal_set", {"pitch": 60}),
        ("camera_lens", {"lens": "ir"}),
        ("takeoff", {"now": True}),
        ("video_start", {"url": "http://example.com"}),
        ("mission_upload", {"waypoints": [{"lat": 1, "lon": 2, "alt": 30}], "speed": 5, "finish_action": "hover"}),
    ],
)
def test_bad_command_args_rejected(name, args):
    with pytest.raises(m.ProtocolError):
        m.make_command("c1", name, args)


@pytest.mark.parametrize("name", ["motors_off", "stop_motors", "emergency_stop", "kill"])
def test_no_motor_stop_command_exists(name):
    assert name not in m.COMMAND_ARGS
    with pytest.raises(m.ProtocolError):
        m.make_command("c1", name)


def test_directions_are_separate():
    ack = '{"type":"ack","id":"c1","ok":true,"error":null}'
    assert isinstance(m.parse_from_bridge(ack), m.Ack)
    with pytest.raises(m.ProtocolError):
        m.parse_from_ground_station(ack)
    with pytest.raises(m.ProtocolError):
        m.parse_from_bridge(stick())


def test_peek_protocol_version():
    assert m.peek_protocol_version('{"type":"hello","protocol_version":7,"future_field":1}') == 7
    assert m.peek_protocol_version('{"type":"hello","protocol_version":true}') is None
    assert m.peek_protocol_version('{"type":"ack"}') is None
    assert m.peek_protocol_version("garbage") is None
