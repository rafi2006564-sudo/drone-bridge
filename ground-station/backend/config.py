"""Configuration loaded from config.yaml. Every setting lives in that one file."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RcConfig(_Section):
    host: str = "127.0.0.1"  # RC Plus 2 IP (DHCP reservation), or 127.0.0.1 for the mock
    port: Annotated[int, Field(ge=1, le=65535)] = 8765
    connect_timeout_s: Annotated[float, Field(gt=0)] = 3.0
    reconnect_delay_s: Annotated[float, Field(gt=0)] = 1.0
    command_timeout_s: Annotated[float, Field(gt=0)] = 5.0
    upload_timeout_s: Annotated[float, Field(gt=0)] = 30.0
    telemetry_stale_s: Annotated[float, Field(gt=0)] = 1.0


class ServerConfig(_Section):
    host: str = "127.0.0.1"
    port: Annotated[int, Field(ge=1, le=65535)] = 8000


class SafetyConfig(_Section):
    ui_heartbeat_timeout_s: Annotated[float, Field(gt=0, le=0.5)] = 0.5
    heartbeat_hz: Annotated[float, Field(ge=10)] = 10.0
    stick_hz: Annotated[float, Field(ge=20)] = 20.0
    ui_stick_timeout_s: Annotated[float, Field(gt=0, le=0.5)] = 0.3
    # Sent to the RC app in hello: no heartbeat for this long while flying -> go home.
    rc_go_home_timeout_s: Annotated[float, Field(ge=2, le=60)] = 10.0
    speed_scale_default: Annotated[float, Field(gt=0, le=1)] = 0.3
    speed_scale_max: Annotated[float, Field(gt=0, le=1)] = 1.0
    ui_deadband: Annotated[float, Field(ge=0, lt=0.5)] = 0.05

    @model_validator(mode="after")
    def _check(self) -> "SafetyConfig":
        if self.speed_scale_default > self.speed_scale_max:
            raise ValueError("speed_scale_default must not exceed speed_scale_max")
        return self


class LimitsConfig(_Section):
    height_limit_m: Annotated[float, Field(ge=20, le=1500)] = 120.0
    rth_altitude_m: Annotated[float, Field(ge=2, le=1500)] = 50.0
    max_distance_from_home_m: Annotated[float, Field(gt=0)] = 500.0  # missions
    max_waypoints: Annotated[int, Field(ge=2)] = 200  # missions; check the SDK limit
    min_mission_alt_m: Annotated[float, Field(ge=0)] = 5.0
    max_mission_speed_ms: Annotated[float, Field(gt=0, le=25)] = 15.0

    @model_validator(mode="after")
    def _check(self) -> "LimitsConfig":
        if self.rth_altitude_m > self.height_limit_m:
            raise ValueError("rth_altitude_m must not exceed height_limit_m")
        return self


class PreflightConfig(_Section):
    min_gps_sats: Annotated[int, Field(ge=0)] = 10
    min_battery_pct: Annotated[int, Field(ge=0, le=100)] = 30


class VideoConfig(_Section):
    # URL the RC app pushes the live stream to (MediaMTX on the PC). Phase 3.
    push_url: str | None = None
    # Page the browser embeds to play the stream (MediaMTX WebRTC). Phase 3.
    player_url: str | None = None


class MapConfig(_Section):
    # The default needs internet on the PC. Point this at a local tile server
    # for fully offline use; markers and the track still work without tiles.
    tile_url: str = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    tile_attribution: str = "&copy; OpenStreetMap contributors"


class SimulatorConfig(_Section):
    lat: Annotated[float, Field(ge=-90, le=90)] = 47.397742
    lon: Annotated[float, Field(ge=-180, le=180)] = 8.545594


class LoggingConfig(_Section):
    dir: str = "logs"


class StorageConfig(_Section):
    dir: str = "data"  # saved missions and schedules


class ScheduleConfig(_Section):
    # A sortie launches at its time, or up to this much later (e.g. the
    # backend was busy). Beyond that it is marked missed; it never launches late.
    launch_window_s: Annotated[float, Field(ge=5, le=300)] = 60.0
    warn_before_s: Annotated[float, Field(ge=0)] = 60.0  # countdown in the UI
    min_lead_s: Annotated[float, Field(ge=0)] = 60.0  # earliest schedule: now + this
    max_horizon_days: Annotated[float, Field(gt=0, le=30)] = 7.0
    takeoff_timeout_s: Annotated[float, Field(gt=0)] = 30.0  # takeoff -> stable hover


class Config(_Section):
    rc: RcConfig = Field(default_factory=RcConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    map: MapConfig = Field(default_factory=MapConfig)
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; copy config.example.yaml to config.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)
