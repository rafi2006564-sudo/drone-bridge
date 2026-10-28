"""Run the ground station:  python -m ground_station [--config config.yaml]"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    p = argparse.ArgumentParser(prog="ground_station")
    p.add_argument("--config", default="config.yaml")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)
    app = create_app(cfg)
    print(f"Ground station UI: http://{cfg.server.host}:{cfg.server.port}/  (RC: {cfg.rc.host}:{cfg.rc.port})", flush=True)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")


if __name__ == "__main__":
    main()
