"""Associative memory: multi-path retrieval over the event log.

v0.2 (ADR-0004) replaces the single top-k similarity hook with a family of
retrieval *paths*, each answering a different question about the past:

- ``semantic_near`` — what is close in meaning to a query (diversified,
  so it is NOT a degenerate top-k of near-duplicates);
- ``temporal``      — what happened around a moment in time;
- ``revisit``       — old thread material or old material matching a query;
- ``distant``       — what is far from the current focus (novelty);
- ``forgotten``     — what is old and rarely recalled (the long tail);
- ``serendipity``   — a low-probability boundary sample from under-visited
  regions of memory.

:meth:`MemoryRetrieval.multi_recall` runs several of these at once and
interleaves their results so a recall never collapses onto one path — the
explicit guard against "retrieval degenerates to a single top-k".

Everything here is a *derived view* over the append-only log (ADR-0001).
The :class:`MemoryIndex` caches per-event retrieval/activation stats so the
forgotten/serendipity/diversity paths are cheap; it is rebuildable.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime

from .event_store import EventStore
from .memory_index import MemoryIndex
from .meta_prefixes import META_PREFIXES
from .models import Event
from .providers import EmbeddingProvider, HashingEmbeddingProvider
from .semantic import EmbeddingUnavailable


def _cosine(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# E2 retrieval-corpus hygiene (round 8, self_thread_concentration lineage)
#
# Mirrors the E0 counterfactual (scripts/e0_pollution_counterfactual.py)
# exactly: when ``recall_pool_hygiene`` is ON, two mechanism-based classes of
# pollution are excluded from the RECALL POOL (retrieval candidates only —
# nothing is ever dropped from the event log, writes stay complete):
#
# - P1 route-meta stub: a ``thought.created`` whose text starts with a
#   route-meta prefix ([world]-tagged observations are KEPT: primary material).
# - P3 echo-copy: a ``memory.consolidated`` / ``memory.share_candidate`` event
#   whose text transcribes earlier event text (via its own links, or verbatim
#   duplicate of earlier text). Classification is CAUSAL: an event is judged
#   only against text of events that preceded it in the log.
#
# This is a port of E0's fixed ``_is_echo`` (the original probe had a
# non-causal bug: candidate source texts were looked up in a table built over
# the whole log including the future; the fixed rule judges against prior
# text only, which is what a live mind could actually do).
# ---------------------------------------------------------------------------
HYGIENE_STUB_TYPES = frozenset({"thought.created"})
HYGIENE_ECHO_TYPES = frozenset({"memory.consolidated", "memory.share_candidate"})


def _hygiene_norm(t: str) -> str:
    return "".join(t.split()).lower()


def _is_echo(event: Event, prior_norm: dict[str, str], prior_text_set: set[str]) -> bool:
    """P3 rule (mechanism-based, causal): a consolidated/share event whose
    text is a transcription of earlier event text. ``prior_norm`` maps event
    id -> normalized text of every PRIOR text-bearing event."""
    text = event.text
    if not text:
        return False
    nt = _hygiene_norm(text)
    cand_ids: list[str] = []
    rel = event.links.get("related_to") or []
    if isinstance(rel, str):
        rel = [rel]
    cand_ids.extend(lid for lid in rel if isinstance(lid, str))
    src = event.content.get("source_thought_id")
    if isinstance(src, str):
        cand_ids.append(src)
    prov = event.provenance or {}
    p = prov.get("promotion_of") or prov.get("source_thought_id")
    if isinstance(p, str):
        cand_ids.append(p)
    for cid in cand_ids:
        snt = prior_norm.get(cid, "")
        if len(snt) >= 12 and snt in nt:
            return True
    return nt in prior_text_set  # verbatim duplicate of earlier text


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


@dataclass
class RetrievalHit:
    """One recalled event plus the path that surfaced it and a path score."""

    event: Event
    score: float
    path: str
    reason: str = ""

    @property
    def id(self) -> str:
        return self.event.id


class MemoryRetrieval:
    def __init__(self, store: EventStore, embedder: EmbeddingProvider | None = None,
                 *, semantic_layer=None, recall_pool_hygiene: bool = False):
        # v0.2 Step 2 (ADR-0010): ``semantic_layer`` switches the TREATMENT
        # strategies on — distant becomes bridge-scored, serendipity becomes
        # mid-band, topic_concentration becomes cluster-based. None (default)
        # keeps every path byte-identical to the sealed legacy behaviour.
        self.semantic_layer = semantic_layer
        self.store = store
        self.embedder = embedder or HashingEmbeddingProvider()
        self.index = MemoryIndex(store, self.embedder)
        # E2 (round 8): retrieval-corpus hygiene. False (sealed default) = the
        # recall pool is the full non-system log, byte-identical behaviour.
        # True = P1 route-meta stubs and P3 echo-copies are excluded from the
        # recall pool (NOT from the log — writes stay complete), mirroring the
        # E0 counterfactual. Classification is incremental + causal.
        self.recall_pool_hygiene = bool(recall_pool_hygiene)
        self._hygiene_seq = 0
        self._hygiene_excluded: set[str] = set()
        self._hygiene_prior_norm: dict[str, str] = {}
        self._hygiene_prior_text_set: set[str] = set()

    # ------------------------------------------------------ E2 pool hygiene

    def _hygiene_scan(self) -> None:
        """Incrementally classify events appended since the last scan.
        Causal: each event is judged against prior text only."""
        rows = self.store.since_seq(self._hygiene_seq, limit=100000)
        for e in rows:
            text = e.text
            nt = _hygiene_norm(text) if text else ""
            if (e.type in HYGIENE_STUB_TYPES and text.startswith(META_PREFIXES)) or e.type in HYGIENE_ECHO_TYPES and _is_echo(
                    e, self._hygiene_prior_norm, self._hygiene_prior_text_set):
                self._hygiene_excluded.add(e.id)
            if nt:
                self._hygiene_prior_norm[e.id] = nt
                self._hygiene_prior_text_set.add(nt)
            if e.seq is not None:
                self._hygiene_seq = max(self._hygiene_seq, e.seq)

    def _hygiene_fetch_slack(self) -> int:
        """Bounded-window retrievals keep the same window size over the
        cleaned pool (mirrors E0's _FilteredStore over-fetch)."""
        self._hygiene_scan()
        return len(self._hygiene_excluded) + 5000

    def _hygiene_keep(self, e: Event) -> bool:
        return e.id not in self._hygiene_excluded

    # ------------------------------------------------- compatibility surface

    def recent(self, n: int = 20, *, type_prefix: str | None = None) -> list[Event]:
        """The most recent n events, oldest first. Excludes system events.

        NOTE: deliberately NOT hygiene-filtered even when
        ``recall_pool_hygiene`` is on — this window also feeds the SleepEngine
        creation pool (E2 is recall-pool-only; nothing about what gets written
        or created changes). Recall paths that need a cleaned recent window use
        :meth:`_recent_for_recall`."""
        events = self.store.list(n, type_prefix=type_prefix, order="desc")
        return list(reversed([e for e in events if e.visibility != "system"]))

    def _recent_for_recall(self, n: int = 10) -> list[Event]:
        """``recent`` as the recall pool sees it: E2-hygiened when the knob is
        on (mirrors E0's _FilteredStore, which filtered the recent window too)."""
        events = self.recent(n)
        if self.recall_pool_hygiene:
            self._hygiene_scan()
            fetch = int(n) + self._hygiene_fetch_slack()
            raw = list(reversed([e for e in self.store.list(fetch, order="desc")
                                 if e.visibility != "system" and self._hygiene_keep(e)]))
            events = raw[:n]
        return events

    def keyword(self, term: str, n: int = 20) -> list[Event]:
        """Case-insensitive substring search over event text + type."""
        term = term.lower().strip()
        if not term:
            return []
        hits: list[Event] = []
        for e in self.store.list(500, order="desc"):
            blob = f"{e.type} {e.text}".lower()
            if term in blob:
                hits.append(e)
                if len(hits) >= n:
                    break
        return list(reversed(hits))

    def semantic(self, query: str, n: int = 5, *, type_prefix: str | None = None) -> list[Event]:
        """Backward-compatible top list, now diversified (see semantic_near)."""
        return [h.event for h in self.semantic_near(query, n, type_prefix=type_prefix)]

    def topic_concentration(self, n_recent: int = 40) -> float:
        """Share of the dominant event family among recent non-system events.

        0.0..1.0. High = one kind of activity dominates. v0.2 improves the
        signal but keeps this coarse read for the route policy's open-endedness
        guard.

        Treatment (ADR-0010): with a ``semantic_layer`` attached this becomes
        the CLUSTER-based concentration — the share of the recent window in its
        dominant semantic cluster — so the open-endedness guard reads meaning,
        not event-family names. Legacy (default) is untouched.
        """
        if self.semantic_layer is not None:
            try:
                return self.semantic_layer.concentration(n_recent=n_recent)
            except EmbeddingUnavailable:
                return 0.0  # degraded: a neutral read, audited by the wrapper
        events = [e for e in self.store.list(n_recent, order="desc") if e.visibility != "system"]
        if not events:
            return 0.0
        counts: dict[str, int] = {}
        for e in events:
            counts[e.family] = counts.get(e.family, 0) + 1
        return max(counts.values()) / len(events)

    def recent_topic_signals(self, n_recent: int = 20) -> list[str]:
        """Most common recent families (newest-first, capped) for route state."""
        events = [e for e in self.store.list(n_recent, order="desc") if e.visibility != "system"]
        counts: dict[str, int] = {}
        for e in events:
            counts[e.family] = counts.get(e.family, 0) + 1
        return [f for f, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:5]

    def has_recent(self, type_prefix: str, n_recent: int = 10) -> bool:
        recent = [e for e in self.store.list(n_recent, order="desc") if e.visibility != "system"]
        return any(e.type.startswith(type_prefix) for e in recent)

    # -------------------------------------------------------- retrieval paths

    def _non_system_pool(self, limit: int, *, exclude_ids: set[str] | None = None) -> list[Event]:
        if self.recall_pool_hygiene:
            pool = [e for e in self.store.list(
                int(limit) + self._hygiene_fetch_slack(), order="desc")
                if e.visibility != "system" and self._hygiene_keep(e)]
        else:
            pool = [e for e in self.store.list(limit, order="desc") if e.visibility != "system"]
        if exclude_ids:
            pool = [e for e in pool if e.id not in exclude_ids]
        return pool

    def semantic_near(self, query: str, n: int = 5, *, type_prefix: str | None = None, lam: float = 0.7) -> list[RetrievalHit]:
        """Cosine-similar events, diversified by MMR so near-duplicates do not
        all win the top slots (the anti-degenerate-top-k guarantee).

        A provider outage degrades explicitly (``[]`` + an audited
        ``semantic.degraded`` event from the wrapper) — never a crash, never a
        silently different semantic."""
        try:
            return self._semantic_near_inner(query, n, type_prefix=type_prefix, lam=lam)
        except EmbeddingUnavailable:
            return []

    def _semantic_near_inner(self, query: str, n: int = 5, *, type_prefix: str | None = None, lam: float = 0.7) -> list[RetrievalHit]:
        pool = self._non_system_pool(300)
        pool = [e for e in pool if (not type_prefix or e.type.startswith(type_prefix)) and e.text]
        if not pool or not query.strip():
            return []
        qv = self.embedder.embed([query])[0]
        scored = [(e, _cosine(qv, self.index.embed(e))) for e in pool]
        scored = [(e, s) for e, s in scored if s > 0.0]
        if not scored:
            return []
        scored.sort(key=lambda p: -p[1])
        # MMR: greedily pick max( lam*sim(query) - (1-lam)*max_sim(chosen) )
        chosen: list[Event] = []
        chosen_vecs: list[list[float]] = []
        remaining = list(scored)
        while len(chosen) < n and remaining:
            best, best_score = None, float("-inf")
            for idx, (ev, sq) in enumerate(remaining):
                v = self.index.embed(ev)
                redundancy = max((_cosine(v, cv) for cv in chosen_vecs), default=0.0)
                score = lam * sq - (1 - lam) * redundancy
                if score > best_score:
                    best, best_score, best_idx = score, idx, idx
            if best is None:
                break
            ev, sq = remaining.pop(best_idx)
            chosen.append(ev)
            chosen_vecs.append(self.index.embed(ev))
        return [RetrievalHit(e, max(_cosine(qv, self.index.embed(e)), 0.0), "semantic_near", "meaning-close (diversified)") for e in chosen]

    def temporal(self, anchor: str, *, span_hours: float = 24.0, n: int = 5) -> list[RetrievalHit]:
        """Events within ±span_hours of the anchor timestamp, closest first."""
        a = _parse_iso(anchor)
        if a is None:
            return []
        delta = span_hours * 3600.0
        if self.recall_pool_hygiene:
            self._hygiene_scan()
        near: list[tuple[float, Event, float]] = []
        fetch = 500 + self._hygiene_fetch_slack() if self.recall_pool_hygiene else 500
        for e in self.store.list(fetch, order="asc"):
            if e.visibility == "system":
                continue
            if self.recall_pool_hygiene and not self._hygiene_keep(e):
                continue
            if e.visibility == "system":
                continue
            ea = _parse_iso(e.created_at)
            if ea is None:
                continue
            d = abs((ea - a).total_seconds())
            if d <= delta:
                near.append((d, e, 1.0 - d / delta))
        near.sort(key=lambda t: t[0])
        return [RetrievalHit(e, s, "temporal", f"near {anchor}") for _, e, s in near[:n]]

    def revisit(self, *, thread_id: str | None = None, query: str | None = None, n: int = 5) -> list[RetrievalHit]:
        """Old material: a (dormant) thread's events, or old events matching a
        query. 'Old' = outside the recent window, so revisit reaches back."""
        hits: list[RetrievalHit] = []
        if thread_id:
            evs = [
                e
                for e in self._non_system_pool(300)
                if e.links.get("thread_id") == thread_id or e.content.get("thread_id") == thread_id
            ]
            for e in evs:
                hits.append(RetrievalHit(e, 1.0, "revisit", f"thread {thread_id}"))
            hits = hits[:n]
        if query:
            for h in self.semantic_near(query, n):
                if h.event.id in {x.event.id for x in hits}:
                    continue
                hits.append(RetrievalHit(h.event, h.score, "revisit", f"old match to {query!r}"))
                if len(hits) >= n:
                    break
        return hits[:n]

    def distant(self, n: int = 5, *, pool: int = 300) -> list[RetrievalHit]:
        """Events far from the current focus: maximally dissimilar to the
        recent window's embedding centroid, sampled from OUTSIDE it. This is
        the 'step away from the dominant topic' signal.

        Treatment (ADR-0010): with a ``semantic_layer`` attached, distant is
        **bridge-scored** — far, but only surfaced first when some intermediate
        memory connects it back to the focus ("semantically distant with an
        explainable path"), never simply the lowest cosine."""
        if self.semantic_layer is not None:
            try:
                rows = self.semantic_layer.bridge_distant(n)
            except EmbeddingUnavailable:
                return []
            return [
                RetrievalHit(
                    r["event"], r["distance"], "distant",
                    (f"far + bridge {r['bridge']:.2f} via {r['bridge_via']}"
                     if r["bridgeable"] else "far (no bridge found)"),
                )
                for r in rows
            ]
        recent = self._recent_for_recall(10)
        if not recent:
            return []
        vecs = [self.index.embed(e) for e in recent if e.text]
        if not vecs:
            return []
        dim = len(vecs[0])
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(dim)]
        recent_ids = {e.id for e in recent}
        candidates = self._non_system_pool(pool, exclude_ids=recent_ids)
        candidates = [e for e in candidates if e.text]
        if not candidates:
            return []
        scored = sorted(
            ((1.0 - _cosine(centroid, self.index.embed(e)), e) for e in candidates),
            key=lambda p: -p[0],
        )
        return [RetrievalHit(e, s, "distant", "far from current focus") for s, e in scored[:n]]

    def forgotten(self, n: int = 5, *, pool: int = 500) -> list[RetrievalHit]:
        """Old and rarely recalled — the long tail that fades without revisit."""
        try:
            return self._forgotten_inner(n, pool=pool)
        except EmbeddingUnavailable:
            return []

    def _forgotten_inner(self, n: int = 5, *, pool: int = 500) -> list[RetrievalHit]:
        now = self.index._now_iso()
        candidates = [e for e in self._non_system_pool(pool) if e.text]
        if not candidates:
            return []
        scored = sorted(candidates, key=lambda e: -self.index.forgetfulness(e, now))
        out = [
            RetrievalHit(e, self.index.forgetfulness(e, now), "forgotten", "old + rarely recalled")
            for e in scored
            if self.index.forgetfulness(e, now) > 0.0
        ]
        return out[:n]

    def serendipity(self, n: int = 1, *, pool: int = 400, rng: random.Random | None = None) -> list[RetrievalHit]:
        """A low-probability boundary sample: under-retrieved events, drawn
        with rarity-weighted randomness. Bounded, not a goal — pure chance
        meeting an old corner of memory.

        Treatment (ADR-0010): with a ``semantic_layer`` attached, the sample is
        drawn from the MID BAND around the focus — not near, not pure-far — so
        a chance find stays explainably adjacent (never a random farthest)."""
        if self.semantic_layer is not None:
            try:
                rows = self.semantic_layer.bridge_serendipity(n, rng=rng)
            except EmbeddingUnavailable:
                return []
            return [
                RetrievalHit(r["event"], 0.0, "serendipity",
                             f"mid-band chance find (cluster {r.get('cluster')})")
                for r in rows
            ]
        rng = rng or random.Random()
        candidates = [e for e in self._non_system_pool(pool) if e.text]
        if not candidates:
            return []
        counts = [self.index.retrieval_count(e.id) for e in candidates]
        maxc = max(counts) if counts else 0
        weights = [maxc + 1 - c for c in counts]  # under-retrieved weigh more
        chosen: list[Event] = []
        events = list(candidates)
        ws = list(weights)
        for _ in range(min(n, len(events))):
            total = sum(ws)
            if total <= 0:
                break
            r = rng.random() * total
            acc = 0.0
            for i, w in enumerate(ws):
                acc += w
                if acc >= r:
                    chosen.append(events.pop(i))
                    ws.pop(i)
                    break
        return [RetrievalHit(e, 0.0, "serendipity", "under-visited boundary sample") for e in chosen]

    # ------------------------------------------------------------ multi-recall

    def multi_recall(
        self,
        query: str,
        *,
        paths: tuple[str, ...] = ("semantic_near", "revisit", "distant", "forgotten"),
        n_each: int = 3,
        total: int | None = None,
        rng: random.Random | None = None,
        thread_id: str | None = None,
        anchor: str | None = None,
    ) -> list[RetrievalHit]:
        """Run several paths and interleave their results (round-robin).

        The interleaving is the structural guarantee that a recall spans
        multiple routes through memory rather than collapsing onto one
        top-k similarity list. Dedupes by event id, keeps the first (best)
        hit per event.
        """
        per_path: dict[str, list[RetrievalHit]] = {}
        for p in paths:
            if p == "semantic_near":
                per_path[p] = self.semantic_near(query, n_each)
            elif p == "revisit":
                per_path[p] = self.revisit(thread_id=thread_id, query=query, n=n_each)
            elif p == "distant":
                per_path[p] = self.distant(n=n_each)
            elif p == "forgotten":
                per_path[p] = self.forgotten(n=n_each)
            elif p == "serendipity":
                per_path[p] = self.serendipity(n=n_each, rng=rng)
            elif p == "temporal" and anchor:
                per_path[p] = self.temporal(anchor, n=n_each)
        cap = total if total is not None else n_each * max(1, len(per_path))
        lists = [per_path[p] for p in per_path]
        merged: list[RetrievalHit] = []
        seen: set[str] = set()
        i = 0
        while len(merged) < cap and any(i < len(l) for l in lists):
            for l in lists:
                if i < len(l):
                    h = l[i]
                    if h.event.id not in seen:
                        seen.add(h.event.id)
                        merged.append(h)
                    if len(merged) >= cap:
                        return merged
            i += 1
        return merged

    # ------------------------------------------------------------- telemetry

    def retrieval_diversity(self, hits: list[RetrievalHit]) -> float:
        """0..1 distinct-family share of a recall (via the index)."""
        return self.index.retrieval_diversity([h.event for h in hits])
