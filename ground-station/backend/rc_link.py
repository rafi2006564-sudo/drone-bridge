"""WebSocket client to the RC Bridge app (or the mock bridge).

Owns the connection, the handshake, reconnects, and command/ack matching.
It does not decide what to send; GroundStation does.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from drone_protocol.messages import (
    PROTOCOL_VERSION,
    Ack,
    Event,
    HelloFromBridge,
    HelloFromGroundStation,
    ProtocolError,
    Telemetry,
    dump,
    make_command,
    parse_from_bridge,
    peek_protocol_version,
)

from .config import Config
from .session_log import SessionLog

log = logging.getLogger(__name__)

CLOSE_VERSION_MISMATCH = 4002


class HandshakeError(Exception):
    pass


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    error: str | None = None


class RcLink:
    def __init__(
        self,
        cfg: Config,
        session_log: SessionLog,
        on_telemetry: Callable[[Telemetry], None],
        on_event: Callable[[Event], None],
        on_connection: Callable[[HelloFromBridge | None], None],
        on_backend_event: Callable[[str, str, str], None],
    ) -> None:
        self._cfg = cfg
        self._log = session_log
        self._on_telemetry = on_telemetry
        self._on_event = on_event
        self._on_connection = on_connection
        self._on_backend_event = on_backend_event  # (level, code, message)
        self._ws: ClientConnection | None = None
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Ack]] = {}
        self._ids = itertools.count(1)
        self.peer: HelloFromBridge | None = None
        self._last_error: str | None = None

    @property
    def uri(self) -> str:
        return f"ws://{self._cfg.rc.host}:{self._cfg.rc.port}"

    @property
    def ready(self) -> bool:
        """Connected and handshake complete."""
        return self._ws is not None and self.peer is not None

    async def run(self) -> None:
        """Connect, run the session, and reconnect forever."""
        while True:
            try:
                async with connect(
                    self.uri,
                    open_timeout=self._cfg.rc.connect_timeout_s,
                    ping_interval=2,
                    ping_timeout=4,
                    close_timeout=1,
                    compression=None,
                    proxy=None,
                ) as ws:
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except (OSError, TimeoutError, WebSocketException, HandshakeError) as e:
                msg = f"{type(e).__name__}: {e}"
                if msg != self._last_error:  # don't spam while the RC is off
                    self._last_error = msg
                    log.info("RC link: %s", msg)
                    self._log.write("gs", {"link": "error", "uri": self.uri, "error": msg})
            finally:
                self._teardown()
            await asyncio.sleep(self._cfg.rc.reconnect_delay_s)

    async def _session(self, ws: ClientConnection) -> None:
        hello = HelloFromGroundStation(
            protocol_version=PROTOCOL_VERSION,
            go_home_timeout_s=self._cfg.safety.rc_go_home_timeout_s,
        )
        await ws.send(dump(hello))
        self._log.write("gs>rc", hello)
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=self._cfg.rc.connect_timeout_s)
        except TimeoutError:
            raise HandshakeError("no hello from the RC app") from None
        version = peek_protocol_version(raw)
        if version != PROTOCOL_VERSION:
            await ws.close(CLOSE_VERSION_MISMATCH, "protocol version mismatch")
            text = f"RC app speaks protocol {version}, backend speaks {PROTOCOL_VERSION}"
            self._on_backend_event("critical", "protocol_mismatch", text)
            raise HandshakeError(text)
        try:
            peer = parse_from_bridge(raw)
        except ProtocolError as e:
            raise HandshakeError(f"bad hello: {e}") from None
        if not isinstance(peer, HelloFromBridge):
            raise HandshakeError(f"expected hello, got {peer.type}")
        self._log.write("rc>gs", peer)

        self._ws = ws
        self.peer = peer
        self._last_error = None
        log.info("RC link up: %s", peer)
        self._on_connection(peer)

        async for raw in ws:
            self._dispatch(raw)

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = parse_from_bridge(raw)
        except ProtocolError as e:
            self._log.write("rc>gs", {"invalid": str(raw)[:2000], "error": str(e)})
            self._on_backend_event("warning", "protocol_error", f"invalid frame from RC: {e}")
            return
        self._log.write("rc>gs", msg)
        if isinstance(msg, Telemetry):
            self._on_telemetry(msg)
        elif isinstance(msg, Ack):
            fut = self._pending.get(msg.id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
        elif isinstance(msg, Event):
            self._on_event(msg)
        else:
            self._on_backend_event("warning", "protocol_error", f"unexpected {msg.type} after handshake")

    def _teardown(self) -> None:
        was_up = self.peer is not None
        self._ws = None
        self.peer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result(Ack(id="-", ok=False, error="link to RC lost"))
        self._pending.clear()
        if was_up:
            self._log.write("gs", {"link": "down"})
            self._on_connection(None)

    async def send(self, msg: Any, log_it: bool = True) -> bool:
        """Send a validated protocol message. Returns False if not connected."""
        ws = self._ws
        if ws is None or self.peer is None:
            return False
        try:
            async with self._send_lock:
                await ws.send(dump(msg))
        except WebSocketException:
            return False
        if log_it:
            self._log.write("gs>rc", msg)
        return True

    async def command(self, name: str, args: dict[str, Any] | None = None) -> CommandResult:
        cmd_id = f"c{next(self._ids)}"
        try:
            msg = make_command(cmd_id, name, args)
        except ProtocolError as e:
            return CommandResult(False, f"invalid command: {e}")
        if not self.ready:
            return CommandResult(False, "not connected to the RC")
        fut: asyncio.Future[Ack] = asyncio.get_running_loop().create_future()
        self._pending[cmd_id] = fut
        try:
            if not await self.send(msg):
                return CommandResult(False, "not connected to the RC")
            timeout = (
                self._cfg.rc.upload_timeout_s if name == "mission_upload" else self._cfg.rc.command_timeout_s
            )
            try:
                ack = await asyncio.wait_for(asyncio.shield(fut), timeout)
            except TimeoutError:
                self._log.write("gs", {"ack_timeout": cmd_id, "name": name})
                return CommandResult(False, f"no ack within {timeout:.0f} s")
            return CommandResult(ack.ok, ack.error)
        finally:
            self._pending.pop(cmd_id, None)
