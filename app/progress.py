"""Progress frames: what the agent is doing, as it does it.

SQLite has no LISTEN/NOTIFY, so this is the equivalent in two halves. Every
frame is appended to `run_events`, which is what makes the stream replayable --
a client reconnecting with Last-Event-ID, or opening the page long after the
run started, reads the same rows and misses nothing. On top of that sits an
in-process wakeup so a waiting SSE stream is notified the instant a frame lands
instead of discovering it on the next poll.

The agent's stages run in a threadpool worker, not on the event loop, so the
wakeup has to cross that boundary: `bind_loop()` records the loop at startup and
`emit()` uses `call_soon_threadsafe`. If no loop is bound (a script, a test) the
frame is still written and the stream degrades to its poll timeout.
"""

import asyncio
import json
import threading
from datetime import UTC, datetime

from app.db import connect

_loop: asyncio.AbstractEventLoop | None = None
_waiters: dict[str, set[asyncio.Event]] = {}
_lock = threading.Lock()

# Kinds that mean the run has stopped moving, so a stream can close.
TERMINAL_KINDS = {"finished", "waiting", "failed"}


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def emit(run_id: str, kind: str, message: str, stage: str | None = None, **detail) -> int:
    """Records one frame and wakes anyone streaming this run."""
    with connect() as conn:
        cursor = conn.execute(
            "INSERT INTO run_events (run_id, ts, kind, stage, message, detail) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, datetime.now(UTC).isoformat(), kind, stage, message,
             json.dumps(detail, default=str) if detail else None),
        )
        seq = cursor.lastrowid

    if _loop is not None:
        with _lock:
            events = list(_waiters.get(run_id, ()))
        for event in events:
            try:
                _loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # loop is closing; the frame is still on disk
    return seq


def read_frames(run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT seq, ts, kind, stage, message, detail FROM run_events "
            "WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?",
            (run_id, after_seq, limit),
        ).fetchall()
    return [
        {
            "seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "stage": r["stage"],
            "message": r["message"], "detail": json.loads(r["detail"]) if r["detail"] else {},
        }
        for r in rows
    ]


def subscribe(run_id: str) -> asyncio.Event:
    event = asyncio.Event()
    with _lock:
        _waiters.setdefault(run_id, set()).add(event)
    return event


def unsubscribe(run_id: str, event: asyncio.Event) -> None:
    with _lock:
        waiters = _waiters.get(run_id)
        if waiters:
            waiters.discard(event)
            if not waiters:
                _waiters.pop(run_id, None)


class Ticker:
    """Throttles per-row progress so a million-row file reports steadily rather
    than writing a frame per row."""

    def __init__(self, run_id: str, stage: str, what: str, total: int | None = None,
                 every: int = 500):
        self.run_id, self.stage, self.what = run_id, stage, what
        self.total, self.every = total, every
        self.count = 0

    def tick(self, n: int = 1, conn=None) -> None:
        self.count += n
        if self.count % self.every == 0:
            # A caller batching its writes on one connection holds SQLite's
            # single write lock for the whole batch. The frame below goes out on
            # a different connection, so it would block until that batch ended
            # -- which is precisely when progress has stopped being useful.
            # Committing here releases the lock and publishes the work so far in
            # the same stroke.
            if conn is not None:
                conn.commit()
            self.emit_now()

    def emit_now(self) -> None:
        if self.total:
            message = f"{self.what}: {self.count:,} of {self.total:,}"
        else:
            message = f"{self.what}: {self.count:,}"
        emit(self.run_id, "progress", message, stage=self.stage,
             done=self.count, total=self.total, what=self.what)
