"""Structured observability: a per-mission run manifest + event log + counters.

A system that drives a physical robot and writes money/reputation on-chain must be
*auditable* — after a misbehaving mission or a disputed on-chain write you need to
reconstruct exactly what happened. This module gives every mission a :class:`Run`:
an append-only, structured event timeline (objective → verb calls → artifacts →
on-chain tx → safety events) plus counters and timers, serializable to a JSON
manifest.

The manifest (``run.events``) is the source of truth and is always recorded; the
Python ``logging`` stream is an optional, level-gated mirror (quiet by default —
set ``MPAK_LOG_LEVEL=INFO`` to watch live, safety/errors always surface). Pure
stdlib — no new dependencies, fully unit-testable via an injected clock.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

_LEVELS = {"debug", "info", "warning", "error", "critical"}


def get_logger(name: str = "mpak") -> logging.Logger:
    """A process-wide structured logger (configured once; quiet by default)."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        h = logging.StreamHandler()
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(h)
        logger.setLevel(os.environ.get("MPAK_LOG_LEVEL", "WARNING").upper())
        logger.propagate = False
    return logger


@dataclass
class Event:
    t: float
    type: str
    level: str
    fields: dict


class Run:
    """An auditable mission run — structured event timeline + counters + manifest."""

    def __init__(self, objective: str = "", run_id: str | None = None, *,
                 logger: logging.Logger | None = None, clock=time.time):
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.objective = objective
        self._clock = clock
        self.started = clock()
        self.events: list[Event] = []
        self.counters: dict[str, int] = {}
        self._log = logger or get_logger()
        self.event("run_start", objective=objective, run_id=self.run_id)

    def event(self, type: str, level: str = "info", **fields) -> Event:
        """Record a structured event (always stored; logged if the level passes)."""
        lvl = level if level in _LEVELS else "info"
        e = Event(self._clock(), type, lvl, fields)
        self.events.append(e)
        rec = {"run": self.run_id, "type": type, **fields}
        self._log.log(getattr(logging, lvl.upper()), json.dumps(rec, default=str, sort_keys=True))
        return e

    def counter(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    @contextmanager
    def timer(self, name: str):
        """Time a block; records a ``timing`` event and accumulates total ms."""
        t0 = self._clock()
        try:
            yield
        finally:
            ms = (self._clock() - t0) * 1000.0
            self.counter(f"{name}.count")
            self.counters[f"{name}.ms_total"] = self.counters.get(f"{name}.ms_total", 0) + int(ms)
            self.event("timing", name=name, ms=round(ms, 1))

    def manifest(self) -> dict:
        """The full run manifest (objective → counters → ordered event timeline)."""
        return {
            "run_id": self.run_id,
            "objective": self.objective,
            "started": self.started,
            "duration_s": round(self._clock() - self.started, 2),
            "counters": dict(self.counters),
            "events": [{"t": round(e.t, 3), "type": e.type, "level": e.level, **e.fields}
                       for e in self.events],
        }

    def save(self, path: str) -> str:
        """Write the manifest as pretty JSON to ``path``; returns the path."""
        with open(path, "w") as f:
            json.dump(self.manifest(), f, indent=2, default=str)
        return path
