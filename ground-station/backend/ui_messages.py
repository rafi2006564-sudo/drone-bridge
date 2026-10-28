"""Messages between the browser UI and the backend (not the RC protocol)."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

Unit = Annotated[float, Field(ge=-1.0, le=1.0)]


class _Msg(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class UiHeartbeat(_Msg):
    type: Literal["ui_heartbeat"]
    seq: Annotated[int, Field(ge=0)]


class UiStick(_Msg):
    """Raw stick state after the UI deadband, before the speed scale."""

    type: Literal["stick"]
    lx: Unit
    ly: Unit
    rx: Unit
    ry: Unit


class UiCommand(_Msg):
    type: Literal["command"]
    id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str, Field(min_length=1, max_length=64)]
    args: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class UiOp(_Msg):
    """Mission and schedule management; answered with an ack carrying `data`."""

    type: Literal["op"]
    id: Annotated[str, Field(min_length=1, max_length=64)]
    op: Literal[
        "validate_mission", "save_mission", "delete_mission", "generate_survey",
        "create_schedule", "cancel_schedule", "remove_schedule",
    ]
    args: dict[str, Any] = Field(default_factory=dict)
    confirmed: bool = False


class UiSetSpeedScale(_Msg):
    type: Literal["set_speed_scale"]
    value: Annotated[float, Field(gt=0.0, le=1.0)]


FromUi = Annotated[Union[UiHeartbeat, UiStick, UiCommand, UiOp, UiSetSpeedScale], Field(discriminator="type")]
parse_from_ui = TypeAdapter(FromUi).validate_json
