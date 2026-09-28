"""In-process pub/sub for job events (SSE) and node-wide admin events.

Job events get a per-job sequence number and are persisted (except previews) so a client can reconnect
with Last-Event-ID and replay what it missed.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from collections import defaultdict

from .db import DB

PERSIST_LIMIT = 200  # events kept per job


class EventBus:
    def __init__(self, db: DB):
        self.db = db
        self._job_subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._admin_subs: set[asyncio.Queue] = set()
        self._seq: dict[str, int] = {}
        self.last_preview: dict[str, dict] = {}

    def _next_seq(self, job_id: str) -> int:
        if job_id not in self._seq:
            self._seq[job_id] = int(self.db.scalar("SELECT COALESCE(MAX(seq),0) FROM job_events WHERE job_id=?", (job_id,)) or 0)
        self._seq[job_id] += 1
        return self._seq[job_id]

    def emit(self, job_id: str, type_: str, data: dict | None = None) -> dict:
        ev = {"job_id": job_id, "seq": self._next_seq(job_id), "ts": time.time(), "type": type_, "data": data or {}}
        if type_ == "preview":
            self.last_preview[job_id] = ev
        else:
            if type_ != "progress" or ev["seq"] % 5 == 0:  # progress is chatty: keep a sample for replay
                with contextlib.suppress(Exception):
                    self.db.insert("job_events", {"job_id": job_id, "seq": ev["seq"], "ts": ev["ts"], "type": type_, "data": ev["data"]})
                    if ev["seq"] > PERSIST_LIMIT:
                        self.db.execute("DELETE FROM job_events WHERE job_id=? AND seq<=?", (job_id, ev["seq"] - PERSIST_LIMIT))
        for q in list(self._job_subs.get(job_id, ())):
            _put(q, ev)
        if type_ != "preview" or self._admin_subs:
            self.admin({"type": f"job.{type_}", "job_id": job_id, "data": ev["data"]})
        if type_ in ("succeeded", "failed", "canceled", "expired"):
            self._seq.pop(job_id, None)
            self.last_preview.pop(job_id, None)
        return ev

    def history(self, job_id: str, after_seq: int = 0) -> list[dict]:
        return [{**r, "job_id": job_id} for r in
                self.db.all("SELECT seq, ts, type, data FROM job_events WHERE job_id=? AND seq>? ORDER BY seq", (job_id, after_seq))]

    def subscribe(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._job_subs[job_id].add(q)
        return q

    def unsubscribe(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._job_subs.get(job_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._job_subs.pop(job_id, None)

    # ---------- admin (dashboard) ----------
    def admin(self, ev: dict) -> None:
        for q in list(self._admin_subs):
            _put(q, ev)

    def subscribe_admin(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._admin_subs.add(q)
        return q

    def unsubscribe_admin(self, q: asyncio.Queue) -> None:
        self._admin_subs.discard(q)

    @property
    def admin_watchers(self) -> int:
        return len(self._admin_subs)


def _put(q: asyncio.Queue, ev: dict) -> None:
    try:
        q.put_nowait(ev)
    except asyncio.QueueFull:  # slow consumer: drop the oldest event rather than block the node
        with contextlib.suppress(asyncio.QueueEmpty):
            q.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(ev)
