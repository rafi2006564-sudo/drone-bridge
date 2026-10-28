"""Run the mock bridge:  python -m mock_bridge [--config config.yaml]

Reads rc.port and the simulator start position from the same config.yaml as
the backend. Type `help` in this console for fault-injection commands.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
from pathlib import Path

from ground_station.config import Config, load_config

from .server import MockBridge
from .sim import AircraftSim


def _console(bridge: MockBridge, loop: asyncio.AbstractEventLoop) -> None:
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            return
        fut = asyncio.run_coroutine_threadsafe(bridge.console_command(line), loop)
        try:
            out = fut.result(timeout=5)
        except Exception as e:  # report and keep the console alive
            out = f"error: {e}"
        if out:
            print(out, flush=True)


async def amain(args: argparse.Namespace) -> None:
    cfg = load_config(args.config) if Path(args.config).exists() else Config()
    port = args.port or cfg.rc.port
    bridge = MockBridge(AircraftSim(lat=cfg.simulator.lat, lon=cfg.simulator.lon))
    server = await bridge.serve(args.host, port)
    print(f"mock bridge listening on ws://{args.host}:{port}  (type 'help' for commands)", flush=True)
    if not args.no_console and sys.stdin and sys.stdin.isatty():
        threading.Thread(target=_console, args=(bridge, asyncio.get_running_loop()), daemon=True).start()
    async with server:
        await bridge.run()


def main() -> None:
    p = argparse.ArgumentParser(prog="mock_bridge")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--no-console", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
