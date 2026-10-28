"""Generate protocol/schemas/*.json from the Pydantic models.

Run after changing messages.py:  python -m drone_protocol.gen_schemas
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import TypeAdapter

from . import messages as m

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"

# file name -> type; one schema per message type and direction
SCHEMAS = {
    "hello.ground_station.json": m.HelloFromGroundStation,
    "hello.bridge.json": m.HelloFromBridge,
    "heartbeat.json": m.Heartbeat,
    "stick.json": m.Stick,
    "command.json": m.Command,
    "ack.json": m.Ack,
    "telemetry.json": m.Telemetry,
    "event.json": m.Event,
}


# Discriminator fields have Python defaults for convenience but are required
# on the wire.
_DISCRIMINATORS = ("type", "name", "action")


def _require_discriminators(node: object) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            req = node.setdefault("required", [])
            for key in _DISCRIMINATORS:
                if key in props and "const" in props[key] and key not in req:
                    req.insert(0, key)
                    props[key].pop("default", None)
        for v in node.values():
            _require_discriminators(v)
    elif isinstance(node, list):
        for v in node:
            _require_discriminators(v)


def render() -> dict[str, str]:
    out = {}
    for filename, tp in SCHEMAS.items():
        schema = TypeAdapter(tp).json_schema()
        _require_discriminators(schema)
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"drone-bridge/protocol/v{m.PROTOCOL_VERSION}/{filename}",
            **schema,
        }
        out[filename] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out


def main() -> None:
    SCHEMA_DIR.mkdir(exist_ok=True)
    for filename, text in render().items():
        (SCHEMA_DIR / filename).write_text(text, encoding="utf-8", newline="\n")
        print("wrote", SCHEMA_DIR / filename)


if __name__ == "__main__":
    main()
