"""Append-oriented event store.

The event log is the historical source of truth (ADR-0001). Events are
immutable; derived views are rebuildable from the log.

Provenance invariant: any event link that references another event
(``evt_*`` id) must point at an event that already exists. The store
rejects dangling references, so no claim in the log can cite activity
that never happened.
"""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .models import Event, EventCreate, VISIBILITIES

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  visibility TEXT NOT NULL,
  content_json TEXT NOT NULL,
  links_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
"""

_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_EVENT_ID_PREFIX = "evt_"


def _type_like(type_prefix: str) -> str:
    """Build a LIKE pattern for a type prefix.

    Event types are always exactly ``word.word`` (see _EVENT_TYPE_RE), so a
    prefix that already contains a dot denotes a full type (or a longer
    dotted prefix) and can be matched directly; a bare family prefix must be
    followed by a dot so that ``"wake"`` matches ``wake.*`` but not
    ``waken.*``.
    """
    return f"{type_prefix}%" if "." in type_prefix else f"{type_prefix}.%"


class EventStoreError(ValueError):
    """Raised when an append would violate store invariants."""


def _iter_link_ids(links: dict) -> Iterable[str]:
    """Yield every id-shaped value inside a links dict."""
    for v in links.values():
        if isinstance(v, str):
            yield v
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    yield item


class EventStore:
    def __init__(self, path: Path | str, *, now_fn=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Injectable clock: the accelerated life simulation passes a virtual
        # clock so the whole 7-day timeline fits in one run; the real backend
        # uses the wall clock (default). All time-derived views (memory age,
        # "since meaningful activity") read created_at, so they inherit this.
        self._now_fn = now_fn
        with self._connect() as c:
            c.executescript(SCHEMA)

    def _now_iso(self) -> str:
        if self._now_fn is not None:
            return self._now_fn().isoformat()
        return datetime.now(timezone.utc).isoformat()

    @contextmanager
    def _connect(self):
        """Yield a connection, commit (or roll back) on exit, and ALWAYS close
        it. Closing here instead of leaving the handle to GC is what keeps
        read-only snapshot tooling portable: on Windows a file cannot be
        deleted while a handle is still open, so a lingering connection would
        break ``tempfile.TemporaryDirectory`` cleanup."""
        c = sqlite3.connect(self.path)
        try:
            c.row_factory = sqlite3.Row
            with c:  # commit on success / roll back on exception (prior behavior)
                yield c
        finally:
            c.close()

    # ---------------------------------------------------------------- write

    def append(self, data: EventCreate) -> Event:
        """Validate and append one event. Returns the stored Event (with seq)."""
        self._validate(data)
        e = Event.new(data, created_at=self._now_iso())
        with self._connect() as c:
            try:
                cur = c.execute(
                    "INSERT INTO events(id,created_at,type,actor,visibility,"
                    "content_json,links_json,provenance_json,metadata_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        e.id, e.created_at, e.type, e.actor, e.visibility,
                        json.dumps(e.content, ensure_ascii=False),
                        json.dumps(e.links, ensure_ascii=False),
                        json.dumps(e.provenance, ensure_ascii=False),
                        json.dumps(e.metadata, ensure_ascii=False),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EventStoreError(f"append rejected: {exc}") from exc
            e.seq = int(cur.lastrowid)
        return e

    def _validate(self, data: EventCreate) -> None:
        if not data.type or not _EVENT_TYPE_RE.match(data.type):
            raise EventStoreError(f"invalid event type: {data.type!r}")
        if not data.actor:
            raise EventStoreError("event actor must be non-empty")
        if data.visibility not in VISIBILITIES:
            raise EventStoreError(f"invalid visibility: {data.visibility!r}")
        # Provenance invariant: event links must not dangle.
        with self._connect() as c:
            for link_id in _iter_link_ids(data.links):
                if not link_id.startswith(_EVENT_ID_PREFIX):
                    continue  # thread/entity ids are not events
                row = c.execute("SELECT 1 FROM events WHERE id=?", (link_id,)).fetchone()
                if row is None:
                    raise EventStoreError(
                        f"dangling event link: {link_id!r} does not exist"
                    )

    # ------------------------------------------------------------------ read

    def get(self, event_id: str) -> Event | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return self._row(r) if r else None

    def list(
        self,
        limit: int = 100,
        *,
        type_prefix: str | None = None,
        since: str | None = None,
        order: str = "desc",
    ) -> list[Event]:
        """List events. order: 'desc' (newest first) or 'asc'."""
        sql = "SELECT * FROM events"
        args: list = []
        where = []
        if type_prefix:
            where.append("type LIKE ?")
            args.append(_type_like(type_prefix))
        if since:
            where.append("created_at > ?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq " + ("DESC" if order == "desc" else "ASC")
        sql += " LIMIT ?"
        args.append(int(limit))
        with self._connect() as c:
            rows = c.execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def since(self, created_at: str, limit: int = 500) -> list[Event]:
        """Events strictly after created_at, oldest first."""
        return self.list(limit, type_prefix=None, since=created_at, order="asc")

    def since_seq(self, seq: int, limit: int = 500) -> list[Event]:
        """Events strictly after a seq number, oldest first."""
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                (int(seq), int(limit)),
            ).fetchall()
        return [self._row(r) for r in rows]

    def latest(self) -> Event | None:
        with self._connect() as c:
            r = c.execute("SELECT * FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def count(self, type_prefix: str | None = None) -> int:
        with self._connect() as c:
            if type_prefix:
                r = c.execute("SELECT COUNT(*) FROM events WHERE type LIKE ?", (_type_like(type_prefix),)).fetchone()
            else:
                r = c.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(r[0])

    def last_user_interaction(self) -> Event | None:
        """Most recent conversation.user_message event, if any."""
        with self._connect() as c:
            r = c.execute(
                "SELECT * FROM events WHERE type='conversation.user_message' "
                "ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r: sqlite3.Row) -> Event:
        return Event(
            id=r["id"], created_at=r["created_at"], seq=r["seq"],
            type=r["type"], actor=r["actor"], visibility=r["visibility"],
            content=json.loads(r["content_json"]),
            links=json.loads(r["links_json"]),
            provenance=json.loads(r["provenance_json"]),
            metadata=json.loads(r["metadata_json"]),
        )
