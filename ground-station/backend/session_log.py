"""One JSON Lines file per backend session in logs/ (safety item 10)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class SessionLog:
    def __init__(self, directory: str | Path) -> None:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"session-{stamp}.jsonl"
        n = 1
        while self.path.exists():
            n += 1
            self.path = directory / f"session-{stamp}-{n}.jsonl"
        self._f = self.path.open("a", encoding="utf-8", newline="\n")

    def write(self, direction: str, msg: BaseModel | dict[str, Any], **extra: Any) -> None:
        """direction: 'rc>gs', 'gs>rc', 'ui>gs', 'gs>ui' or 'gs' (backend-internal)."""
        if self._f.closed:  # late callbacks during shutdown
            return
        body = msg.model_dump(mode="json") if isinstance(msg, BaseModel) else msg
        record = {"ts": time.time(), "mono": time.monotonic(), "dir": direction, "msg": body}
        if extra:
            record.update(extra)
        self._f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()
