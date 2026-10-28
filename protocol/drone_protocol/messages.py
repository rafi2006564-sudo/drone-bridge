"""Protocol messages between the ground station backend and the RC bridge.

These Pydantic models are the source of truth for the wire format. The JSON
Schemas in protocol/schemas/ are generated from them (see gen_schemas.py) and
a test fails if the checked-in schemas drift from the models.

Validation is strict: no type coercion, no unknown fields, no NaN/Infinity.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    """A frame that is not valid JSON or does not match the schema."""


class _Msg(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, frozen=True)


Unit = Annotated[float, Field(ge=-1.0, le=1.0)]
Lat = Annotated[float, Field(ge=-90.0, le=90.0)]
Lon = Annotated[float, Field(ge=-180.0, le=180.0)]
CommandId = Annotated[str, Field(min_length=1, max_length=64)]


# --------------------------------------------------------------------------
# Handshake
# --------------------------------------------------------------------------

class HelloFromGroundStation(_Msg):
    type: Literal["hello"] = "hello"
    protocol_version: int
    # How long the RC app waits without a heartbeat, while flying, before it
    # triggers go home. Sent here so all settings live in the backend config.
    go_home_timeout_s: Annotated[float, Field(ge=2.0, le=60.0)]


class HelloFromBridge(_Msg):
    type: Literal["hello"] = "hello"
    protocol_version: int
    msdk_version: str
    aircraft_model: str
    aircraft_firmware: str
    rc_firmware: str


# --------------------------------------------------------------------------
# Ground station -> bridge
# --------------------------------------------------------------------------

class Heartbeat(_Msg):
    type: Literal["heartbeat"] = "heartbeat"
    seq: Annotated[int, Field(ge=0)]
    t: float  # sender monotonic clock, seconds


class Stick(_Msg):
    """Mode 2 sticks. Positive = throttle up, yaw right, roll right, pitch forward."""

    type: Literal["stick"] = "stick"
    lx: Unit  # yaw
    ly: Unit  # throttle
    rx: Unit  # roll
    ry: Unit  # pitch
    t: float  # sender monotonic clock, seconds


class NoArgs(_Msg):
    pass


class GimbalRotateArgs(_Msg):
    pitch_rate: Annotated[float, Field(ge=-90.0, le=90.0)]  # deg/s, + = up
    yaw_rate: Annotated[float, Field(ge=-90.0, le=90.0)]  # deg/s, + = right


class GimbalSetArgs(_Msg):
    pitch: Annotated[float, Field(ge=-90.0, le=35.0)]  # deg, 0 = horizon, -90 = down


class CameraZoomArgs(_Msg):
    ratio: Annotated[float, Field(ge=1.0, le=200.0)]


class CameraLensArgs(_Msg):
    lens: Literal["wide", "zoom"]


class PhotoAction(_Msg):
    action: Literal["photo"] = "photo"


class GimbalPitchAction(_Msg):
    action: Literal["gimbal_pitch"] = "gimbal_pitch"
    pitch: Annotated[float, Field(ge=-90.0, le=35.0)]


WaypointAction = Annotated[Union[PhotoAction, GimbalPitchAction], Field(discriminator="action")]


class Waypoint(_Msg):
    lat: Lat
    lon: Lon
    alt: Annotated[float, Field(ge=0.0, le=1500.0)]  # m above takeoff point
    speed: Annotated[float, Field(gt=0.0, le=25.0)] | None = None  # m/s, None = mission speed
    actions: list[WaypointAction] = Field(default_factory=list)


class MissionUploadArgs(_Msg):
    waypoints: Annotated[list[Waypoint], Field(min_length=2)]
    speed: Annotated[float, Field(gt=0.0, le=25.0)]  # m/s
    finish_action: Literal["go_home", "hover", "land", "first_waypoint"]


class HeightArgs(_Msg):
    m: Annotated[float, Field(gt=0.0, le=1500.0)]


class SimulatorStartArgs(_Msg):
    lat: Lat
    lon: Lon


class VideoStartArgs(_Msg):
    url: Annotated[str, Field(min_length=1, max_length=512, pattern=r"^(rtmp|rtsp|srt)://")]


# Command name -> argument model. Commands without arguments send "args": {}.
COMMAND_ARGS: dict[str, type[_Msg]] = {
    "enable_virtual_stick": NoArgs,
    "disable_virtual_stick": NoArgs,
    "takeoff": NoArgs,
    "land": NoArgs,
    "go_home": NoArgs,
    "cancel_go_home": NoArgs,
    "gimbal_rotate": GimbalRotateArgs,
    "gimbal_set": GimbalSetArgs,
    "gimbal_reset": NoArgs,
    "camera_photo": NoArgs,
    "camera_record_start": NoArgs,
    "camera_record_stop": NoArgs,
    "camera_zoom": CameraZoomArgs,
    "camera_lens": CameraLensArgs,
    "mission_upload": MissionUploadArgs,
    "mission_start": NoArgs,
    "mission_pause": NoArgs,
    "mission_resume": NoArgs,
    "mission_stop": NoArgs,
    "set_height_limit": HeightArgs,
    "set_rth_altitude": HeightArgs,
    "simulator_start": SimulatorStartArgs,
    "simulator_stop": NoArgs,
    "video_start": VideoStartArgs,
    "video_stop": NoArgs,
}
# There is deliberately no command that stops the motors (safety item 8).


def _command_model(name: str, args_model: type[_Msg]) -> type[_Msg]:
    title = "Command_" + name
    namespace: dict[str, Any] = {
        "__annotations__": {
            "type": Literal["command"],
            "id": CommandId,
            "name": Literal[name],  # type: ignore[valid-type]
            "args": args_model,
        },
        "type": "command",
        "name": name,
        "__module__": __name__,
    }
    return type(title, (_Msg,), namespace)


COMMAND_MODELS: dict[str, type[_Msg]] = {n: _command_model(n, a) for n, a in COMMAND_ARGS.items()}

Command = Annotated[Union[tuple(COMMAND_MODELS.values())], Field(discriminator="name")]  # type: ignore[misc]


# --------------------------------------------------------------------------
# Bridge -> ground station
# --------------------------------------------------------------------------

class Ack(_Msg):
    type: Literal["ack"] = "ack"
    id: CommandId
    ok: bool
    error: str | None = None


class ActiveWarning(_Msg):
    code: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    level: Literal["warning", "critical"]
    message: str


MissionState = Literal["idle", "uploading", "ready", "executing", "paused", "unknown"]


class Telemetry(_Msg):
    type: Literal["telemetry"] = "telemetry"
    seq: Annotated[int, Field(ge=0)]
    t: float  # bridge monotonic clock, seconds
    # position (None = unknown)
    lat: Lat | None
    lon: Lon | None
    alt_rel: float | None  # m above takeoff point
    # motion: velocities in m/s (NED), angles in degrees
    heading: Annotated[float, Field(ge=0.0, lt=360.0)] | None
    vel_n: float | None
    vel_e: float | None
    vel_d: float | None
    pitch: float | None
    roll: float | None
    yaw: float | None
    # power
    battery_pct: Annotated[int, Field(ge=0, le=100)] | None
    # GPS
    gps_sats: Annotated[int, Field(ge=0)]
    gps_level: Annotated[int, Field(ge=0, le=10)]
    # home
    home_lat: Lat | None
    home_lon: Lon | None
    home_set: bool
    # state
    flight_mode: str
    motors_on: bool
    is_flying: bool
    virtual_stick_enabled: bool
    simulator_active: bool
    # gimbal
    gimbal_pitch: float | None
    gimbal_yaw: float | None
    # links, 0-100
    rc_link_quality: Annotated[int, Field(ge=0, le=100)] | None
    video_link_quality: Annotated[int, Field(ge=0, le=100)] | None
    # missions
    mission_state: MissionState
    # additions beyond the build plan's field list (see PROTOCOL.md)
    active_warnings: list[ActiveWarning]
    camera_recording: bool
    camera_lens: Literal["wide", "zoom"] | None
    zoom_ratio: float | None


class Event(_Msg):
    type: Literal["event"] = "event"
    level: Literal["info", "warning", "critical"]
    code: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    message: str
    t: float  # sender monotonic clock, seconds
    data: dict[str, Any] | None = None


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

FromGroundStation = Annotated[
    Union[HelloFromGroundStation, Heartbeat, Stick, Command], Field(discriminator="type")
]
FromBridge = Annotated[
    Union[HelloFromBridge, Ack, Telemetry, Event], Field(discriminator="type")
]

_from_gs = TypeAdapter(FromGroundStation)
_from_bridge = TypeAdapter(FromBridge)
_command = TypeAdapter(Command)


def _parse(adapter: TypeAdapter, raw: str | bytes) -> Any:
    try:
        return adapter.validate_json(raw)
    except ValidationError as e:
        raise ProtocolError(_summarize(e)) from None


def _summarize(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:3]:
        loc = ".".join(str(x) for x in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


def parse_from_ground_station(raw: str | bytes) -> Any:
    """Validate a frame sent by the backend (what the RC app receives)."""
    return _parse(_from_gs, raw)


def parse_from_bridge(raw: str | bytes) -> Any:
    """Validate a frame sent by the RC app (what the backend receives)."""
    return _parse(_from_bridge, raw)


def make_command(cmd_id: str, name: str, args: dict[str, Any] | None = None) -> Any:
    """Build a validated command; raises ProtocolError on bad name or args."""
    frame = {"type": "command", "id": cmd_id, "name": name, "args": args or {}}
    return _parse(_command, json.dumps(frame, allow_nan=False))


def peek_protocol_version(raw: str | bytes) -> int | None:
    """Read protocol_version from a hello frame before full validation.

    A peer on a different protocol version may send fields this version does
    not know, so the version is checked first to give a clear error.
    """
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "hello":
        return None
    v = obj.get("protocol_version")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def dump(msg: _Msg) -> str:
    return msg.model_dump_json()
