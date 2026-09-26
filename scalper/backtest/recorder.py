"""Record raw WebSocket messages to hourly gzip JSONL files for later replay."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path


class Recorder:
    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh = None
        self._hour: str | None = None
        self.count = 0

    def _file_for(self, ts: float):
        hour = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y%m%dT%H")
        if hour != self._hour:
            if self._fh:
                self._fh.close()
            self._hour = hour
            self._fh = gzip.open(self.dir / f"ws-{hour}.jsonl.gz", "at", encoding="utf-8")  # noqa: SIM115 - long-lived, rotated hourly
        return self._fh

    async def write(self, msg: dict, ts: float) -> None:
        fh = self._file_for(ts)
        fh.write(json.dumps({"ts": ts, "msg": msg}, separators=(",", ":")) + "\n")
        self.count += 1

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
