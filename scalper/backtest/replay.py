"""Replay recorded WebSocket messages through the engine with a simulated clock."""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterable, Iterator
from pathlib import Path


class SimClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def iter_recording(paths: Iterable[Path]) -> Iterator[tuple[dict, float]]:
    for p in sorted(paths):
        opener = gzip.open if str(p).endswith(".gz") else open
        with opener(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                yield rec["msg"], float(rec["ts"])


async def replay(engine, clock: SimClock, paths: Iterable[Path]) -> int:
    n = 0
    for msg, ts in iter_recording(paths):
        clock.now = ts
        await engine.on_message(msg, ts)
        n += 1
    return n
