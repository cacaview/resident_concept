"""Derived, rebuildable index that powers associative memory (ADR-0004).

The event log is the single source of truth (ADR-0001). This index is a
*cache* over it — it holds per-event retrieval/activation stats and an
embedding cache so that retrieval paths (forgotten / serendipity /
diversity) and telemetry (memory-age-at-activation) are cheap. Nothing in
it is authoritative: :meth:`MemoryIndex.rebuild` recomputes the counters
from the log, so a process that crashes loses at most the cache, never
the data.

Two distinct notions, both derivable from the log:

- **retrieval** — an event was *recalled*: it appears in a wake/sleep's
  ``recalled`` metadata list.
- **activation** — an event was *used as provenance* for a durable thought,
  question, or consolidation (linked via ``related_to``).

"Memory age at activation" (telemetry) is ``now - created_at`` measured at
the moment an event is activated; the index accumulates those samples.
"""
from __future__ import annotations

import math
from datetime import datetime

from .event_store import EventStore
from .models import Event
from .providers import EmbeddingProvider


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


class MemoryIndex:
    def __init__(self, store: EventStore, embedder: EmbeddingProvider | None = None):
        self.store = store
        self.embedder = embedder
        self._retrieval_count: dict[str, int] = {}
        self._last_retrieved: dict[str, str] = {}
        self._activation_count: dict[str, int] = {}
        # age-at-activation samples (seconds) accumulated for telemetry
        self._age_at_activation: list[float] = []
        self._embed_cache: dict[str, list[float]] = {}
        self._built_seq: int = 0
        self.rebuild()

    # ------------------------------------------------------------ rebuilding

    def rebuild(self) -> None:
        """Recompute all counters from the log (full scan, oldest→newest)."""
        self._retrieval_count.clear()
        self._last_retrieved.clear()
        self._activation_count.clear()
        self._age_at_activation.clear()
        self._embed_cache.clear()
        now_iso = self._now_iso()
        for e in self.store.list(100_000, order="asc"):
            # retrieval: events recalled by this event
            for eid in e.metadata.get("recalled") or []:
                if not isinstance(eid, str):
                    continue
                self._retrieval_count[eid] = self._retrieval_count.get(eid, 0) + 1
                if not self._last_retrieved.get(eid, "") or e.created_at > self._last_retrieved[eid]:
                    self._last_retrieved[eid] = e.created_at
            # activation: events this event cites as provenance
            for eid in e.links.get("related_to") or []:
                if not isinstance(eid, str):
                    continue
                self._activation_count[eid] = self._activation_count.get(eid, 0) + 1
            # age-at-activation sample for the durable events this one cites
            if e.type in ("thought.created", "question.created", "memory.consolidated"):
                age = self._age_seconds(e.created_at, now_iso)
                for eid in e.links.get("related_to") or []:
                    src = self.store.get(eid)
                    if src is not None and age is not None:
                        src_age = self._age_seconds(src.created_at, e.created_at)
                        if src_age is not None:
                            self._age_at_activation.append(src_age)
        last = self.store.latest()
        self._built_seq = last.seq if last else 0

    # -------------------------------------------------------- hot-path cache

    def record_retrieval(self, event_ids: list[str], at: str) -> None:
        """Increment retrieval stats as a recall happens (within-run cache)."""
        for eid in event_ids:
            if not isinstance(eid, str):
                continue
            self._retrieval_count[eid] = self._retrieval_count.get(eid, 0) + 1
            if not self._last_retrieved.get(eid, "") or at > self._last_retrieved[eid]:
                self._last_retrieved[eid] = at

    def record_activation(self, event_ids: list[str], at: str, now: str) -> None:
        """Increment activation stats + accumulate age-at-activation samples."""
        for eid in event_ids:
            if not isinstance(eid, str):
                continue
            self._activation_count[eid] = self._activation_count.get(eid, 0) + 1
            src = self.store.get(eid)
            if src is not None:
                age = self._age_seconds(src.created_at, at)
                if age is not None:
                    self._age_at_activation.append(age)

    # ------------------------------------------------------------------ reads

    def retrieval_count(self, event_id: str) -> int:
        return self._retrieval_count.get(event_id, 0)

    def last_retrieved(self, event_id: str) -> str | None:
        return self._last_retrieved.get(event_id)

    def activation_count(self, event_id: str) -> int:
        return self._activation_count.get(event_id, 0)

    def age_seconds(self, event: Event, now: str | None = None) -> float:
        """Age of an event in seconds (from created_at to now or latest event)."""
        return self._age_seconds(event.created_at, now or self._now_iso()) or 0.0

    def forgetfulness(self, event: Event, now: str | None = None) -> float:
        """Higher = older AND less retrieved. The signal for `forgotten`."""
        age = self.age_seconds(event, now)
        count = self.retrieval_count(event.id)
        # Normalize age to a 0..~1 range over ~14 days so a young store does
        # not treat "a few hours old" as forgotten.
        age_norm = min(1.0, age / (14 * 86400.0))
        return age_norm / (1.0 + count)

    def embed(self, event: Event) -> list[float]:
        """Cached embedding of an event (text, falling back to type)."""
        v = self._embed_cache.get(event.id)
        if v is None:
            if self.embedder is None:
                raise RuntimeError("MemoryIndex has no embedder")
            v = self.embedder.embed([event.text or event.type])[0]
            self._embed_cache[event.id] = v
        return v

    def retrieval_diversity(self, events: list[Event]) -> float:
        """0..1: share of distinct *families* among the recalled set.

        Low = the recall collapsed onto one kind of event (a degenerate
        top-k); high = it spanned several. This is the guard the user asked
        for: retrieval must not reduce to a single top-k similarity.
        """
        if not events:
            return 0.0
        fams = {e.family for e in events}
        return len(fams) / len(events) if len(events) > 1 else 0.0

    def age_stats(self) -> dict:
        """Distribution of age-at-activation samples (seconds)."""
        samples = self._age_at_activation
        if not samples:
            return {"count": 0, "mean": 0.0, "p50": 0.0, "max": 0.0}
        s = sorted(samples)
        n = len(s)
        return {
            "count": n,
            "mean": sum(s) / n,
            "p50": s[n // 2],
            "max": s[-1],
        }

    # ------------------------------------------------------------------ utils

    def _now_iso(self) -> str:
        last = self.store.latest()
        return last.created_at if last else ""

    @staticmethod
    def _age_seconds(older: str, newer: str) -> float | None:
        a, b = _parse_iso(older), _parse_iso(newer)
        if a is None or b is None:
            return None
        return max(0.0, (b - a).total_seconds())
