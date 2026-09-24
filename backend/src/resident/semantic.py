"""The semantic layer (v0.2 Step 2, ADR-0010): real-vector memory machinery.

Three responsibilities, strictly separated:

1. **Shadow mode (safe by construction).** A :class:`SemanticLayer` attached to
   the MindLoop as a *shadow* runs the SAME retrieval questions the legacy
   hashing embedder answers, but with real (provider) vectors, and records the
   divergence as ``semantic.shadow_compared`` events. The mind's decisions
   still come from legacy — shadow only answers "what would it have thought of
   with a different associative fabric?" Shadow events are ``system``
   visibility and excluded from the mind's route context (audit ≠ experience;
   fixture runs contain none, so the sealed regression is untouched).

2. **Treatment mode.** Wiring a cached real provider into
   :class:`~resident.memory.MemoryRetrieval` switches semantic_near / distant /
   serendipity / concentration to real vectors + bridge strategies. This is
   the experiment arm that IS allowed to change dynamics — never the default.

3. **The topic view.** Deterministic leader clustering (seeded by store order,
   threshold τ) over the resident's own texts; cluster identity comes ONLY
   from vectors + events (a stable ``cluster_<seed-hash>`` id, centroid,
   members, threads, first/last seen). An LLM may attach a display label —
   recorded as ``semantic.cluster_labeled`` — but labels are never identity
   and can never change clustering.

Determinism: all vectors flow through the :class:`~resident.vector_cache.VectorCache`
(identity-checked: provider/model/version/dim/normalization/content-hash), so
an experiment re-run replays cached vectors without the network. Provider
failures never crash the mind: the resilient wrapper raises
:class:`EmbeddingUnavailable`, retrieval paths return empty, and a
``semantic.degraded`` audit event records the degraded mode explicitly —
semantics never silently change.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from .event_store import EventStore
from .providers import ProviderError
from .vector_cache import VectorCache, content_hash

SEMANTIC_CLUSTER_LABELED = "semantic.cluster_labeled"

#: runtime families excluded from clustering (machinery, not experience)
_RUNTIME_PREFIXES = ("wake.", "sleep.", "reentry.", "telemetry.", "circadian.", "semantic.")
#: leader-clustering similarity threshold (structural constant, not tuned to a
#: target metric; documented in ADR-0010)
CLUSTER_TAU = 0.55
#: distant-bridge: minimum "connective tissue" for a far candidate to count as
#: bridgeable (there must exist an intermediate memory linking it to the focus)
MIN_BRIDGE = 0.10
#: serendipity band: not near (>= hi), not pure-far (< lo) — the middle ground
SERENDIPITY_LO = 0.15
SERENDIPITY_HI = 0.45


class EmbeddingUnavailable(RuntimeError):
    """The semantic provider failed and no cached vector exists. Retrieval
    paths catch this and degrade explicitly (empty result + audit event) —
    never a crash into the MindLoop, never a silent different semantic."""


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class ResilientEmbedding:
    """Cache-first wrapper: cache → provider → :class:`EmbeddingUnavailable`.
    ``on_degraded`` is called (at most once per wake; see :func:`emit_degraded`)
    with the error reason so the caller can leave an explicit audit trail."""

    def __init__(self, provider, cache: VectorCache | None = None, on_degraded=None):
        self.provider = provider
        self.cache = cache
        self.on_degraded = on_degraded
        self._identity: dict | None = None

    @property
    def model_id(self) -> str:
        return getattr(self.provider, "model_id", "unknown")

    @property
    def identity(self) -> dict:
        if self._identity is None:
            try:
                dim = len(self.provider.embed([""])[0])
            except ProviderError as exc:
                if self.on_degraded is not None:
                    self.on_degraded(str(exc))
                raise EmbeddingUnavailable(str(exc)) from exc
            ident = dict(getattr(self.provider, "identity", {}) or {})
            ident.setdefault("provider", self.provider.__class__.__name__)
            ident.setdefault("model", self.model_id)
            ident.setdefault("version", str(getattr(self.provider, "version", "0")))
            ident.setdefault("dim", dim)
            ident.setdefault("normalization", getattr(self.provider, "normalization", "none"))
            self._identity = ident
        return self._identity

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            ident = self.identity
            if self.cache is not None:
                hit = self.cache.get(dim=ident["dim"], text=text, **{
                    k: ident[k] for k in ("provider", "model", "version", "normalization")})
                if hit is not None:
                    out.append(hit)
                    continue
            try:
                vec = self.provider.embed([text])[0]
            except ProviderError as exc:
                if self.on_degraded is not None:
                    self.on_degraded(str(exc))
                raise EmbeddingUnavailable(str(exc)) from exc
            except EmbeddingUnavailable:
                raise
            if self.cache is not None:
                self.cache.put(dim=ident["dim"], text=text, vector=vec, **{
                    k: ident[k] for k in ("provider", "model", "version", "normalization")})
            out.append(vec)
        return out

    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Duck-type compatibility: callers may use the embedder's cosine
        (the legacy HashingEmbeddingProvider exposes one)."""
        return sum(x * y for x, y in zip(a, b))


class ShadowLog:
    """Append-only JSONL sidecar for semantic audit records.

    Shadow comparisons and degraded-mode marks deliberately do NOT enter the
    event log: the log is Resident's *life*, and an audit record squeezed
    between events would shift every bounded "recent" window (the mind's
    ``has_recent`` slices a raw window before filtering system events — an
    audit event in that window changes route scores, i.e. the shadow would
    perturb the very thing it observes). A sidecar is still a durable,
    provenance-linked record (each entry carries the ``wake_id`` it shadows),
    and it keeps the sealed event log byte-identical by construction.
    """

    def __init__(self, path):
        self.path = str(path)

    def append(self, record: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def read(self) -> list[dict]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                return [json.loads(line) for line in fh if line.strip()]
        except FileNotFoundError:
            return []


def emit_degraded(record: dict, log: ShadowLog | None, store: EventStore | None = None) -> bool:
    """Record an explicit degraded-mode mark — sidecar first (mind-invisible by
    construction). Kept at most one per wake. Returns whether it was written."""
    if log is not None:
        last_wake = store.list(1, type_prefix="wake.started", order="desc") if store else []
        records = log.read()
        last = next((r for r in reversed(records) if r.get("kind") == "degraded"), None)
        if last and last.get("wake_seq") is not None and last_wake:
            if last["wake_seq"] >= last_wake[0].seq:
                return False  # already degraded inside this wake
        log.append({"kind": "degraded",
                    "wake_seq": last_wake[0].seq if last_wake else None,
                    **record})
        return True
    return False


def make_semantic_stack(store: EventStore, cache_dir, *, mode: str = "legacy",
                        shadow: bool = False, provider=None, on_degraded=None,
                        shadow_log_path=None, record_extra: dict | None = None):
    """The one builder for the semantic stack (env-independent; main.py parses
    env and delegates; sims call it directly with explicit modes).

    Returns ``(shadow_layer | None, memory_kwargs | None)``:
    - ``mode="legacy"`` (default): both None unless ``shadow=True``.
    - ``mode="semantic"``: the treatment — MemoryRetrieval kwargs carrying the
      cached real-vector embedder + the layer (bridge distant / mid-band
      serendipity / cluster concentration).
    - ``shadow=True`` (legacy main only): an audit-only layer whose comparisons
      go to the sidecar JSONL at ``shadow_log_path`` (never the event log).

    ``record_extra`` (default None): extra keys merged into the DEFAULT
    on_degraded audit record (alongside ``reason`` / ``model``) — for a caller
    that cannot see the internal ShadowLog but still wants its degraded marks
    tagged (e.g. main.py's ``{"mode": "treatment"}`). An explicit
    ``on_degraded`` always wins; with both None the behaviour is unchanged.
    """
    from .providers import DeterministicSemanticEmbeddingProvider
    from .vector_cache import VectorCache

    if mode != "semantic" and not shadow:
        return None, None
    cache = VectorCache(cache_dir)
    if provider is None:
        provider = DeterministicSemanticEmbeddingProvider()
    log = ShadowLog(shadow_log_path) if shadow_log_path else None
    if on_degraded is None and log is not None:
        extra = dict(record_extra or {})
        on_degraded = lambda reason: emit_degraded(  # noqa: E731 (explicit, audited)
            {"reason": reason[:300], "model": getattr(provider, "model_id", "unknown"),
             **extra},
            log, store)
    layer = SemanticLayer(store, provider, cache=cache, on_degraded=on_degraded,
                          shadow_log=log)
    memory_kwargs = None
    if mode == "semantic":
        memory_kwargs = {
            "embedder": ResilientEmbedding(provider, cache=cache, on_degraded=on_degraded),
            "semantic_layer": layer,
        }
    return (layer if (shadow and mode != "semantic") else None), memory_kwargs


@dataclass
class Cluster:
    """One deterministic topic cluster. Identity = the seed event (the first
    text that opened it), hashed to a stable id; the ordinal is display-order."""

    id: str
    ordinal: int
    seed_event_id: str
    centroid: list[float]
    members: list[str] = field(default_factory=list)       # event ids
    threads: set[str] = field(default_factory=set)
    first_seen: str = ""
    last_seen: str = ""
    label: str | None = None  # display only; never identity


class SemanticLayer:
    """Real-vector memory over the store: cached embeddings, recall,
    deterministic topic clustering, bridge-scoring, and the shadow comparison."""

    def __init__(self, store: EventStore, provider, *, cache: VectorCache | None = None,
                 cluster_tau: float | None = None, on_degraded=None,
                 shadow_log: ShadowLog | None = None):
        self.store = store
        self.embedding = ResilientEmbedding(provider, cache=cache, on_degraded=on_degraded)
        # cluster threshold: the provider may declare a hint calibrated to its
        # own vector geometry (families differ wildly); explicit arg overrides.
        self.cluster_tau = (cluster_tau if cluster_tau is not None
                            else float(getattr(provider, "cluster_tau_hint", CLUSTER_TAU)))
        self._vec_cache: dict[str, list[float]] = {}   # event id -> vector
        self._text_hash_seen: set[str] = set()
        self.shadow_log = shadow_log

    def record_shadow(self, record: dict) -> None:
        """Persist one shadow comparison to the sidecar (never the event log —
        see :class:`ShadowLog` for why)."""
        if self.shadow_log is not None:
            self.shadow_log.append({"kind": "shadow", **record})

    # ------------------------------------------------------------- embedding

    def embed_event(self, event) -> list[float]:
        vec = self._vec_cache.get(event.id)
        if vec is None:
            vec = self.embedding.embed([event.text])[0]
            self._vec_cache[event.id] = vec
        return vec

    def embed_text(self, text: str) -> list[float]:
        return self.embedding.embed([text])[0]

    # ---------------------------------------------------------------- pool

    def _pool(self, limit: int = 400, *, exclude_ids: set[str] | None = None) -> list:
        pool = [e for e in self.store.list(limit, order="desc") if e.visibility != "system"]
        pool = [e for e in pool if not e.type.startswith(_RUNTIME_PREFIXES) and e.text]
        if exclude_ids:
            pool = [e for e in pool if e.id not in exclude_ids]
        return list(reversed(pool))  # store order (oldest first) for determinism

    # -------------------------------------------------------------- recall

    def recall(self, query: str, n: int = 5, *, pool_limit: int = 400) -> list[dict]:
        """Semantic top-k with the same MMR diversification shape as the legacy
        path — the only difference vs legacy is the vectors. Ranked hits as
        dicts (rank starts at 1) so shadow comparison is positional. A provider
        outage degrades explicitly: empty result (audited by the wrapper)."""
        if not query or not query.strip():
            return []
        try:
            return self._recall_inner(query, n, pool_limit=pool_limit)
        except EmbeddingUnavailable:
            return []

    def _recall_inner(self, query: str, n: int = 5, *, pool_limit: int = 400) -> list[dict]:
        pool = self._pool(pool_limit)
        if not pool:
            return []
        qv = self.embed_text(query)
        scored = [(e, max(0.0, cosine(qv, self.embed_event(e)))) for e in pool]
        scored = [(e, s) for e, s in scored if s > 0.0]
        if not scored:
            return []
        scored.sort(key=lambda p: -p[1])
        lam = 0.7
        chosen: list[tuple[object, float]] = []
        chosen_vecs: list[list[float]] = []
        remaining = list(scored)
        while len(chosen) < n and remaining:
            best_idx, best_score = None, float("-inf")
            for idx, (ev, sq) in enumerate(remaining):
                v = self.embed_event(ev)
                redundancy = max((cosine(v, cv) for cv in chosen_vecs), default=0.0)
                s = lam * sq - (1 - lam) * redundancy
                if s > best_score:
                    best_idx, best_score = idx, s
            ev, sq = remaining.pop(best_idx)
            chosen.append((ev, sq))
            chosen_vecs.append(self.embed_event(ev))
        return [{"rank": i + 1, "id": e.id, "score": round(s, 4), "type": e.type,
                 "thread": e.links.get("thread_id") or e.content.get("thread_id"),
                 "age_days": round(self._age_days(e), 3)}
                for i, (e, s) in enumerate(chosen)]

    # ------------------------------------------------------------ clusters

    def clusters(self, *, pool_limit: int = 400) -> list[Cluster]:
        """Deterministic leader clustering in store order: each event joins the
        first cluster whose centroid is within τ, else opens a new one. Order-
        dependent but fully reproducible; identity is the seed event, so ids
        stay stable as the store grows (ordinals shift, identities do not)."""
        pool = self._pool(pool_limit)
        out: list[Cluster] = []
        for e in pool:
            v = self.embed_event(e)
            best: Cluster | None = None
            best_sim = 0.0
            for c in out:
                s = cosine(v, c.centroid)
                if s > best_sim:
                    best_sim, best = s, c
            if best is not None and best_sim >= self.cluster_tau:
                c = best
                n = len(c.members)
                c.centroid = [(x * n + y) / (n + 1) for x, y in zip(c.centroid, v)]
                c.members.append(e.id)
            else:
                c = Cluster(id="cluster_" + hashlib.sha256(e.id.encode()).hexdigest()[:10],
                            ordinal=len(out), seed_event_id=e.id, centroid=list(v))
                c.members.append(e.id)
                out.append(c)
            tid = e.links.get("thread_id") or e.content.get("thread_id")
            if tid:
                c.threads.add(tid)
            if not c.first_seen:
                c.first_seen = e.created_at
            c.last_seen = e.created_at
        return out

    def topic_view(self, *, pool_limit: int = 400) -> dict:
        """The cluster fact base: vector-derived identity + stats (+ optional
        labels from a separate, non-authoritative sidecar)."""
        cls = self.clusters(pool_limit=pool_limit)
        sizes = [len(c.members) for c in cls] or [0]
        total = sum(sizes)
        return {
            "n_clusters": len(cls),
            "tau": self.cluster_tau,
            "cluster_entropy": _normalized_entropy(sizes),
            "clusters": [{
                "id": c.id, "ordinal": c.ordinal, "n_members": len(c.members),
                "n_threads": len(c.threads), "threads": sorted(c.threads),
                "first_seen": c.first_seen, "last_seen": c.last_seen,
                "label": c.label,
                "seed_event_id": c.seed_event_id,
            } for c in cls],
            "largest_share": round(max(sizes) / total, 3) if total else 0.0,
        }

    def concentration(self, *, n_recent: int = 40) -> float:
        """Cluster-based topic concentration: the share of the recent window in
        its dominant cluster (the treatment-mode ``topic_concentration``)."""
        recent = [e for e in self.store.list(n_recent, order="desc")
                  if e.visibility != "system" and not e.type.startswith(_RUNTIME_PREFIXES) and e.text]
        if len(recent) < 2:
            return 0.0
        recent_ids = {e.id for e in recent}
        cls = self.clusters(pool_limit=max(200, n_recent * 4))
        counts: dict[str, int] = {}
        for c in cls:
            hit = sum(1 for m in c.members if m in recent_ids)
            if hit:
                counts[c.id] = hit
        total = sum(counts.values())
        if not total:
            return 0.0
        return round(max(counts.values()) / total, 3)

    # ------------------------------------------------------ bridge strategies

    def bridge_distant(self, n: int = 3, *, pool_limit: int = 300,
                       bridge_pool: int = 30, min_bridge: float = MIN_BRIDGE,
                       exclude_ids: set[str] | None = None) -> list[dict]:
        """distant ≠ lowest cosine. Score = far from the focus centroid, but
        only counted as *bridgeable* when some intermediate memory m (mid-band
        to the focus) also reaches the candidate: sim(e, m) ≥ min_bridge for an
        m with sim(m, focus) in the mid band. That is "semantically distant but
        with an explainable path back" — a graph far-neighbour, not a random
        far. Falls back to pure-farthest (flagged) when nothing bridges."""
        recent = [e for e in self.store.list(10, order="desc") if e.visibility != "system" and e.text]
        if not recent:
            return []
        recent_ids = {e.id for e in recent}
        exclude = recent_ids | (exclude_ids or set())
        pool = [e for e in self._pool(pool_limit, exclude_ids=exclude)]
        if not pool:
            return []
        vecs = [self.embed_event(e) for e in recent]
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
        scored = []
        for e in pool:
            v = self.embed_event(e)
            d = 1.0 - cosine(v, centroid)          # distance from focus
            scored.append((d, e, v))
        scored.sort(key=lambda p: -p[0])
        # intermediates: mid-band to the focus (the "near-but-not-top" world),
        # each carrying its own focus-similarity and id for evidence recording
        mids = [(1.0 - d, e) for d, e, _ in scored if 0.25 <= (1.0 - d) <= 0.75][:bridge_pool]
        mid_vecs = [(self.embed_event(e), focus_sim, e.id) for focus_sim, e in mids]
        out = []
        for d, e, v in scored:
            best_bridge = 0.0
            bridge_via = None
            for mv, mid_focus_sim, mid_id in mid_vecs:
                if mid_id == e.id:
                    continue
                chain = min(cosine(v, mv), mid_focus_sim)
                if chain > best_bridge:
                    best_bridge, bridge_via = chain, mid_id
            out.append({"id": e.id, "event": e, "distance": round(d, 4),
                        "bridge": round(best_bridge, 4), "bridge_via": bridge_via,
                        "bridgeable": best_bridge >= min_bridge, "type": e.type,
                        "age_days": round(self._age_days(e), 3)})
            if len(out) >= 400:
                break
        # rank: bridgeable first (by distance), then the rest (pure-far, flagged)
        out.sort(key=lambda r: (not r["bridgeable"], -r["distance"]))
        return out[:n]

    def bridge_serendipity(self, n: int = 1, *, pool_limit: int = 400, rng=None) -> list[dict]:
        """serendipity ≠ random farthest: rarity-weighted sampling restricted to
        the MID BAND around the focus (Serendipity lives between "same old" and
        "unrelated"), so a chance find is still explainably adjacent."""
        import random as _random

        rng = rng or _random.Random()
        recent = [e for e in self.store.list(10, order="desc") if e.visibility != "system" and e.text]
        if not recent:
            return []
        vecs = [self.embed_event(e) for e in recent]
        centroid = [sum(v[i] for v in vecs) / len(vecs) for i in range(len(vecs[0]))]
        pool = self._pool(pool_limit)
        band = []
        for e in pool:
            sim = cosine(self.embed_event(e), centroid)
            if SERENDIPITY_LO <= sim <= SERENDIPITY_HI:
                band.append(e)
        if not band:
            return []
        counts = [0] * len(band)  # rarity within the band (retrieval counts live
        # in the legacy index; the semantic band is by construction rarely hit)
        weights = [max(counts) + 1 - c for c in counts]
        chosen = []
        events = list(band)
        ws = list(weights)
        for _ in range(min(n, len(events))):
            total = sum(ws)
            r = rng.random() * total
            acc = 0.0
            for i, w in enumerate(ws):
                acc += w
                if acc >= r:
                    chosen.append(events.pop(i))
                    ws.pop(i)
                    break
        return [{"id": e.id, "event": e, "type": e.type, "age_days": round(self._age_days(e), 3),
                 "cluster": self._cluster_of(e.id, pool)}
                for e in chosen]

    def _cluster_of(self, event_id: str, pool) -> str | None:
        cls = self.clusters(pool_limit=max(len(pool), 200))
        for c in cls:
            if event_id in c.members:
                return c.id
        return None

    # ------------------------------------------------------- shadow compare

    def _age_days(self, event) -> float:
        latest = self.store.latest()
        if latest is None:
            return 0.0
        from .memory import _parse_iso

        a, b = _parse_iso(event.created_at), _parse_iso(latest.created_at)
        if a is None or b is None:
            return 0.0
        return max(0.0, (b - a).total_seconds()) / 86400.0

    def shadow_compare(self, legacy_memory, *, route: str, query: str,
                       context: dict | None = None, k: int = 5) -> dict | None:
        """One canonical comparison: what legacy hashing recalled vs what real
        vectors would have recalled, for the same wake and query. A path-only
        route (empty query) still compares the DISTANT selections. ``None``
        when there is nothing at all to compare (empty store / degraded)."""
        try:
            if query and query.strip():
                legacy_top = legacy_memory.semantic_near(query, k)
                semantic_top = self.recall(query, k)
            else:
                legacy_top, semantic_top = [], []
            legacy_distant = legacy_memory.distant(3)
            bridge_distant = self.bridge_distant(3)
        except EmbeddingUnavailable:
            return None
        if not legacy_top and not semantic_top and not legacy_distant and not bridge_distant:
            return None
        legacy_ids = [h.event.id for h in legacy_top]
        semantic_ids = [h["id"] for h in semantic_top]
        overlap = len(set(legacy_ids) & set(semantic_ids))
        common = set(legacy_ids) & set(semantic_ids)
        disp = [abs((legacy_ids.index(i) + 1) - (semantic_ids.index(i) + 1))
                for i in common]
        l_thread = None
        s_thread = None
        if legacy_top:
            l_thread = (legacy_top[0].event.links.get("thread_id")
                        or legacy_top[0].event.content.get("thread_id"))
        if semantic_top:
            s_thread = semantic_top[0]["thread"]
        record = {
            "route": route,
            "query_hash": content_hash(query)[:16],
            "k": k,
            "legacy_top": legacy_ids,
            "semantic_top": semantic_ids,
            "overlap": overlap,
            "rank_displacement_mean": round(sum(disp) / len(disp), 3) if disp else None,
            "top1_differs": bool(legacy_ids and semantic_ids and legacy_ids[0] != semantic_ids[0]),
            "legacy_top1_age_days": round(self._age_days(legacy_top[0].event), 3) if legacy_top else None,
            "semantic_top1_age_days": semantic_top[0]["age_days"] if semantic_top else None,
            "legacy_top1_thread": l_thread,
            "semantic_top1_thread": s_thread,
            "thread_selection_diverges": bool(l_thread != s_thread),
            "distant_legacy": [h.event.id for h in legacy_distant],
            "distant_bridge": [r["id"] for r in bridge_distant],
            "distant_bridge_rate": round(
                sum(1 for r in bridge_distant if r["bridgeable"]) / len(bridge_distant), 3)
            if bridge_distant else 0.0,
            **(context or {}),
        }
        if legacy_top:
            record["legacy_top1_cluster"] = self._cluster_of(legacy_top[0].event.id, self._pool())
        if semantic_top:
            record["semantic_top1_cluster"] = self._cluster_of(semantic_top[0]["id"], self._pool())
        return record


# --------------------------------------------------------------------- utils


def _normalized_entropy(counts: list[int]) -> float:
    total = sum(counts)
    if total <= 0 or not counts:
        return 0.0
    import math as _m

    h = 0.0
    for c in counts:
        if c > 0:
            p = c / total
            h -= p * _m.log(p, 2)
    return round(h / _m.log(max(len([c for c in counts if c > 0]), 2), 2), 3)


# ------------------------------------------------------- derived telemetry


def semantic_profile(store: EventStore, *, layer: SemanticLayer | None = None,
                     shadow_log: ShadowLog | None = None) -> dict:
    """Derived semantic telemetry (the brief's metric list). Shadow records come
    from the SIDECAR (they are not event-log data, by design); serendipity
    survival is derived from the thought events' recall metadata. Cluster-
    structure metrics use the layer's cluster view when provided; otherwise
    ``None`` — honestly missing, not zero."""
    records = shadow_log.read() if shadow_log is not None else []
    shadows = [r for r in records if r.get("kind") == "shadow"]
    degraded = sum(1 for r in records if r.get("kind") == "degraded")
    out: dict = {"n_shadow_comparisons": len(shadows), "n_degraded": degraded}
    if not shadows:
        out.update({
            "legacy_semantic_overlap": None, "rank_displacement": None,
            "thread_selection_divergence": None, "semantic_memory_age_days": None,
            "distant_bridge_rate": None, "top1_divergence_rate": None,
            "cross_cluster_recall_rate": None, "cluster_entropy": None,
            "cluster_lifetime_days": None, "serendipity_survival_rate": None,
        })
        return out

    overlaps, displacements, diverges, ages, bridge_rates, top1_diffs = [], [], [], [], [], []
    prev_cluster = None
    cross_cluster = 0
    cluster_seq = 0
    for c in shadows:
        if c.get("overlap") is not None and c.get("k"):
            overlaps.append(c["overlap"] / c["k"])  # overlap fraction @k
        if c.get("rank_displacement_mean") is not None:
            displacements.append(c["rank_displacement_mean"])
        if c.get("thread_selection_diverges") is not None:
            diverges.append(1.0 if c["thread_selection_diverges"] else 0.0)
        if c.get("semantic_top1_age_days") is not None:
            ages.append(c["semantic_top1_age_days"])
        if c.get("distant_bridge_rate") is not None:
            bridge_rates.append(c["distant_bridge_rate"])
        if c.get("top1_differs") is not None:
            top1_diffs.append(1.0 if c["top1_differs"] else 0.0)
        cur = c.get("semantic_top1_cluster")
        if cur is not None:
            cluster_seq += 1
            if prev_cluster is not None and cur != prev_cluster:
                cross_cluster += 1
            prev_cluster = cur

    def _mean(xs):
        return round(sum(xs) / len(xs), 3) if xs else None

    out.update({
        "legacy_semantic_overlap": _mean(overlaps),
        "rank_displacement": _mean(displacements),
        "thread_selection_divergence": _mean(diverges),
        "semantic_memory_age_days": _mean(ages),
        "distant_bridge_rate": _mean(bridge_rates),
        "top1_divergence_rate": _mean(top1_diffs),
        "cross_cluster_recall_rate": round(cross_cluster / cluster_seq, 3) if cluster_seq else None,
    })
    if layer is not None:
        view = layer.topic_view()
        out["cluster_entropy"] = view["cluster_entropy"]
        out["n_clusters"] = view["n_clusters"]
        lifetimes = []
        for c in view["clusters"]:
            from .memory import _parse_iso

            a, b = _parse_iso(c["first_seen"]), _parse_iso(c["last_seen"])
            if a and b:
                lifetimes.append((b - a).total_seconds() / 86400.0)
        out["cluster_lifetime_days"] = _mean(lifetimes)
    else:
        out["cluster_entropy"] = None
        out["cluster_lifetime_days"] = None
    # serendipity survival: a serendipity wake's recalled material (thought
    # metadata.recalled on route=="serendipity") that is recalled AGAIN by a
    # later thought — the chance find stayed alive in memory.
    thoughts = [t for t in store.list(5000, type_prefix="thought.created", order="asc")]
    ser_recall_ids: set[str] = set()
    for t in thoughts:
        if (t.metadata or {}).get("route") == "serendipity":
            rec = t.metadata.get("recalled") or []
            ser_recall_ids.update(rec)
    survived = 0
    for t in thoughts:  # later thoughts that re-recall any serendipity pick
        if (t.metadata or {}).get("route") == "serendipity":
            continue
        rec = set((t.metadata or {}).get("recalled") or [])
        if rec & ser_recall_ids:
            survived += 1
    if ser_recall_ids:
        # normalize by the number of serendipity wakes that actually recalled
        n_ser_wakes = sum(1 for t in thoughts
                          if (t.metadata or {}).get("route") == "serendipity"
                          and (t.metadata or {}).get("recalled"))
        out["serendipity_survival_rate"] = round(survived / max(1, n_ser_wakes), 3)
    else:
        out["serendipity_survival_rate"] = None
    return out
