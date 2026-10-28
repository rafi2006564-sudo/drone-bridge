"""FastAPI app: serves the web UI and the UI WebSocket."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from .config import Config
from .station import GroundStation

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

CLOSE_UI_BUSY = 4001
CLOSE_BAD_ORIGIN = 4003


def same_origin(ws: WebSocket) -> bool:
    """Only the page served by this backend may connect.

    Browsers let any website open a WebSocket to localhost, so without this
    check a page on the internet could send flight commands.
    """
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    if not origin or not host:
        return False
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.lower()


def create_app(cfg: Config, station: GroundStation | None = None) -> FastAPI:
    station = station or GroundStation(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await station.start()
        try:
            yield
        finally:
            await station.stop()

    app = FastAPI(title="Drone ground station", lifespan=lifespan)
    app.state.station = station

    @app.websocket("/ws")
    async def ui_socket(ws: WebSocket) -> None:
        if not same_origin(ws):
            await ws.close(code=CLOSE_BAD_ORIGIN)
            return
        await ws.accept()
        queue = station.claim_ui()
        if queue is None:
            await ws.send_text(json.dumps({"type": "rejected", "reason": "another browser tab is in control"}))
            await ws.close(code=CLOSE_UI_BUSY)
            return

        async def pump() -> None:
            while True:
                msg = await queue.get()
                await ws.send_text(json.dumps(msg))

        sender = asyncio.create_task(pump())
        try:
            while True:
                station.handle_ui(await ws.receive_text())
        except WebSocketDisconnect:
            pass
        finally:
            station.release_ui(queue)
            sender.cancel()

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
