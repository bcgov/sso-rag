"""Optional per-turn report of the pipeline's intermediate values, for the
evaluation harness (evals/collect.py).

The rewriter's output, the search the tool ran, each model call's token use,
and each reply attempt with its grounding verdict are only visible inside
ChatService, the search tool and the rewriter. When a TurnReport is attached
with `capture()`, `record()` appends them to it; otherwise (always, in the
deployed service) `record()` returns at once.

The report lives in a ContextVar, so concurrent turns (asyncio tasks) each see
their own. asyncio tasks and `asyncio.to_thread` (how Strands runs sync tools)
copy the context; the query rewriter's thread pool doesn't, so
`QueryRewriter.submit` runs its job in a copy of the caller's context.
"""

import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class TurnReport:
    # Events in the order they happened, each {"event": <name>, **fields}.
    events: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, event: str, fields: dict) -> None:
        with self._lock:
            self.events.append({"event": event, **fields})

    def of(self, event: str) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["event"] == event]

    def last(self, event: str) -> dict | None:
        found = self.of(event)
        return found[-1] if found else None


_current: ContextVar[TurnReport | None] = ContextVar("turn_report", default=None)


@contextmanager
def capture() -> Iterator[TurnReport]:
    """Attaches a new TurnReport to the current context for the duration of
    the block (one ChatService turn)."""
    report = TurnReport()
    token = _current.set(report)
    try:
        yield report
    finally:
        _current.reset(token)


def record(event: str, **fields) -> None:
    """Adds an event to the attached report; a no-op when none is attached."""
    report = _current.get()
    if report is not None:
        report.add(event, fields)
