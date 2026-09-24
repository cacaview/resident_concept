"""Sleep: state-gated memory consolidation, not a content cron (ADR-0004).

A sleep cycle observes the *real* store state — recent thoughts, active /
dormant threads, the forgotten tail — and only when actual material
qualifies appends small durable consolidation events:

- ``compress``        — near-duplicate recent thoughts folded into ONE
  ``memory.consolidated`` note (originals kept: events are immutable, ADR-0001);
- ``thread_dormancy`` — an active thread with no recent touch becomes dormant;
- ``reactivate``      — a forgotten memory strongly related to a recent one
  is re-linked;
- ``cross_link``      — at most one pair of close memories from DIFFERENT
  families is linked;
- ``dream``           — a low-probability free association: a GROUNDED
  ``thought.created`` structurally re-linking two real recalled memories
  (no anthropomorphic dream narrative, ever).

If nothing qualifies, the cycle legitimately does nothing: ``result ==
"noop"`` is a first-class outcome (ADR-0002). Sleep never invents
experiences and never links an event that does not exist (the store rejects
dangling links), and every event it appends carries provenance
``{"source": "sleep", "run_id": ...}`` (EVENT_MODEL.md).

Determinism: given a seeded ``rng`` and fixed store contents a sleep is fully
deterministic — timestamps come from the store's clock (``created_at``),
never from wall-clock reads in this module.
"""
from __future__ import annotations

import math
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from .event_store import EventStore
from .memory import MemoryRetrieval
from .mental_state import MentalState
from .meta_prefixes import META_PREFIXES
from .models import MEMORY_CONSOLIDATED, SLEEP_COMPLETED, SLEEP_STARTED, Event, EventCreate
from .origin import thread_origin

RECENT_WINDOW = 15          # how far back "recent" reaches
DUPLICATE_THRESHOLD = 0.8   # cosine above this => near-duplicate
RELATED_THRESHOLD = 0.5     # cosine above this => "strongly related"
DREAM_MIN_THOUGHTS = 3      # material required before a dream may happen
DEFAULT_DREAM_PROB = 0.2    # dreams are a low-probability side-association
DEFAULT_SELF_PROB = 0.35    # self-originated threads are a low-prob consolidation side-effect
SELF_MAX_THREADS = 4        # bound on total self threads (prevents runaway, not a quota)
MAX_REACTIVATIONS = 3


def _cosine(a: list[float], b: list[float]) -> float:
    return math.fsum(x * y for x, y in zip(a, b))


def _norm_title(s: Any) -> str:
    """Normalized title text: casefold, drop whitespace/punctuation. Used only
    by the round-13 near-identical-thread check (exact/normalized equality)."""
    if not isinstance(s, str):
        return ""
    return "".join(ch for ch in s.casefold() if ch.isalnum())


class SleepEngine:
    """One state-gated consolidation cycle per call.

    ``provider`` is optional and not required: the deterministic rules run
    without any model. Any future use of it must be guarded on
    ``self.provider is not None``.
    """

    def __init__(
        self,
        store: EventStore,
        memory: MemoryRetrieval | None = None,
        provider: Any = None,
        rng: random.Random | None = None,
        dream_prob: float = DEFAULT_DREAM_PROB,
        self_prob: float = DEFAULT_SELF_PROB,
        self_max: int = SELF_MAX_THREADS,
        consolidation_pool_days: float | None = None,
        thread_title_meta_filter: bool = False,
        recall_pool_hygiene: bool = False,
    ):
        self.store = store
        self.memory = memory or MemoryRetrieval(store)
        self.provider = provider
        self.rng = rng if rng is not None else random.Random()
        self.dream_prob = dream_prob
        self.self_prob = self_prob
        self.self_max = self_max
        # Active Living (ADR-0015): the self-thread creation pool was calibrated
        # as recent(60) at the sealed cadence (~2-3 days of life). At higher
        # event density the same COUNT window covers only hours, crowding the
        # cross-topic user material out — no self-thread is ever born. A
        # time-anchored window (days) restores the sealed-era SPAN without
        # touching any criterion. None = the sealed count window.
        self.consolidation_pool_days = consolidation_pool_days
        # Round 7 (self_thread_concentration): VE creation-side hygiene. The
        # self-thread title is built from the cross-topic pair's memory text;
        # when the pool contains route-meta thoughts (``[continuity] …``) or
        # thread-lifecycle events (whose ``Event.text`` IS the thread title —
        # and a title can itself be meta-derived), the new title is meta-text
        # like 「[continuity] 继」与「[continuity] 继」之间. With the filter on,
        # titles are built ONLY from substantive text. Default False = sealed
        # behavior (byte-identical).
        self.thread_title_meta_filter = bool(thread_title_meta_filter)
        # E2 (round 8): retrieval-corpus hygiene — P1 route-meta stubs and P3
        # echo-copies leave the RECALL/RETRIEVAL pool (the shared
        # MemoryRetrieval paths, including the forgotten() leg of the
        # consolidation candidate sourcing). Retrieval-side only: the event
        # log stays complete and nothing about WRITES changes.
        if recall_pool_hygiene:
            self.memory.recall_pool_hygiene = True

    # ------------------------------------------------------------------ run

    async def sleep_once(self, trigger: str = "night") -> dict:
        """Run one consolidation cycle. Returns a summary dict."""
        run_id = f"sleep_{uuid.uuid4().hex}"
        prov = {"source": "sleep", "run_id": run_id}
        sleep_started = self.store.append(
            EventCreate(
                type=SLEEP_STARTED,
                visibility="system",
                content={"trigger": trigger, "run_id": run_id},
                provenance=dict(prov),
            )
        )
        at = sleep_started.created_at

        # ------------------------------------------------- observe (real)
        recent = self.memory.recent(RECENT_WINDOW)  # non-system, oldest first
        recent_thoughts = [e for e in recent if e.type == "thought.created" and e.text]
        state = MentalState.from_store(self.store)  # active/dormant threads
        forgotten = self.memory.forgotten(n=RECENT_WINDOW)
        recent_ids = {e.id for e in recent}

        actions: list[str] = []
        durable: list[Event] = []
        used: set[str] = set()
        activated: list[str] = []

        # --------------------------------------------------- compress
        dups = self._duplicate_cluster(recent_thoughts)
        if dups:
            ids = [e.id for e in dups]
            ev = self.store.append(
                EventCreate(
                    type=MEMORY_CONSOLIDATED,
                    visibility="private",
                    content={
                        "action": "compress",
                        "summary": f"合并了 {len(dups)} 条相近的想法：「{dups[0].text[:40]}」",
                        "duplicates": ids,
                    },
                    links={"related_to": ids, "caused_by": [sleep_started.id]},
                    provenance=dict(prov),
                )
            )
            self._note(ev, actions, durable, used, activated, ids)

        # --------------------------------------------- thread_dormancy
        for t in state.active_threads:
            if t.last_event_id and t.last_event_id not in recent_ids:
                ev = self.store.append(
                    EventCreate(
                        type="thread.dormant",
                        visibility="private",
                        content={"thread_id": t.thread_id},
                        links={"thread_id": t.thread_id, "caused_by": [sleep_started.id]},
                        provenance=dict(prov),
                    )
                )
                actions.append("thread_dormancy")
                durable.append(ev)

        # ------------------------------------------------ reactivate
        n_act = 0
        for h in forgotten:
            if n_act >= MAX_REACTIVATIONS:
                break
            old = h.event
            if not old.text or old.id in used or old.id in recent_ids:
                continue
            partner, score = self._closest(old, recent, used)
            if partner is not None and score >= RELATED_THRESHOLD:
                ev = self._consolidate(
                    "reactivate",
                    f"旧记忆「{old.text[:30]}」与近来的「{partner.text[:30]}」重新关联。",
                    [old.id, partner.id],
                    sleep_started.id,
                    prov,
                )
                self._note(ev, actions, durable, used, activated, [old.id, partner.id])
                n_act += 1

        # ------------------------------------------------- cross_link
        pair = self._cross_family_pair(recent, used)
        if pair is not None:
            a, b = pair
            ev = self._consolidate(
                "cross_link",
                f"跨类别关联：「{a.text[:30]}」与「{b.text[:30]}」放在一起看。",
                [a.id, b.id],
                sleep_started.id,
                prov,
            )
            self._note(ev, actions, durable, used, activated, [a.id, b.id])

        # ------------------------------------------------------------ dream
        dreamed = False
        if self.rng.random() < self.dream_prob and len(recent_thoughts) >= DREAM_MIN_THOUGHTS:
            pair = self._best_pair(recent_thoughts, used)
            if pair is not None:
                a, b = pair
                text = f"[dream] 把「{a.text[:30]}」和「{b.text[:30]}」放在一起看。"
                thought = self.store.append(
                    EventCreate(
                        type="thought.created",
                        visibility="private",
                        content={"text": text},
                        links={"related_to": [a.id, b.id], "caused_by": [sleep_started.id]},
                        provenance=dict(prov),
                        metadata={"route": "dream", "recalled": [a.id, b.id]},
                    )
                )
                actions.append("dream")
                durable.append(thought)
                dreamed = True
                used.update({a.id, b.id})
                activated.extend((a.id, b.id))

        # --------------------------------------------- self-originated thread
        # With low probability — and only when this mind has recently been across
        # >=2 DISTINCT user topics (it has real cross-topic material to work with)
        # — Resident crystallizes a NEW SELF-ORIGINATED thread: its own, not tied
        # to any user topic. This is the lightest non-user material source
        # available before world browsing, and it is the mechanism through which
        # independence is *allowed to emerge* (origin.py / ADR-0004). It is
        # GROUNDED — two REAL memories are linked as its basis, never an invented
        # interest or experience — and it is a probabilistic side-effect of
        # consolidation, NOT a quota forcing independence. Capped to avoid
        # runaway growth (the cap bounds thread count; it does not steer any
        # individual thought).
        if self.rng.random() < self.self_prob and self._count_self_threads() < self.self_max:
            pool = self.memory.recent(60)
            if self.consolidation_pool_days is not None and at:
                # time-anchored pool: the same SPAN of life the sealed count
                # window covered, regardless of event density
                try:
                    t0 = datetime.fromisoformat(at)
                    cutoff = (t0 - timedelta(days=self.consolidation_pool_days)).isoformat()
                    pool = [e for e in self.store.list(2000, order="desc")
                            if e.created_at >= cutoff]
                    if self.thread_title_meta_filter:
                        # Round 9 (self_thread_concentration): apply the SAME
                        # predicate as _best_cross_topic_pair BEFORE the bounded
                        # slice, not after. At active density the 2.5d window
                        # holds far more than 400 events and route-meta stubs
                        # ([continuity]/[revisit] regenerated at write time) are
                        # the dense class — capping first evicts the sparse
                        # substantive text (experience summaries) and leaves the
                        # title filter nothing to pair, which is exactly the
                        # VE/VE2 creation collapse (see out/conc_round9/
                        # diagnosis.md). Filtering first is idempotent with the
                        # in-pair predicate; with the filter OFF this branch is
                        # byte-identical to sealed behavior.
                        pool = [e for e in pool if not (
                            (e.text and e.text.startswith(META_PREFIXES))
                            or e.type.startswith("thread."))]
                    pool = pool[:400]
                except ValueError:
                    pool = self.memory.recent(60)
            pair = self._best_cross_topic_pair(pool)
            if pair is not None:
                a, b = pair
                title = f"「{a.text[:14]}」与「{b.text[:14]}」之间"
                # Round 13 (DATA-QUALITY FIX, not a knob): the 28d real-model
                # battery showed every seed creating 4 near-identical self
                # threads on days 2-3 — the same near-duplicate pair (or its
                # same truncated 14-char title) winning successive sleep
                # cycles inside one consolidation window. Duplicate threads
                # corrupt the concentration metric (they split or fake-spread
                # one topic across 4 thread ids) and feed the revisit loop 4
                # copies of the same material. A near-identical pair (same
                # memory pair, or title equal after normalization) within the
                # consolidation window must not spawn another thread — the
                # cycle legitimately does nothing instead.
                if self._near_identical_self_thread_exists(title, {a.id, b.id}, at):
                    # Deduplicated: no new thread is spawned. Recorded as a
                    # marker in the sleep summary (actions list) so the fix is
                    # auditable; the cycle itself remains a legitimate noop
                    # with respect to thread creation.
                    actions.append("self_thread_deduplicated")
                else:
                    self_tid = f"self_{uuid.uuid4().hex[:12]}"
                    c = self.store.append(
                        EventCreate(
                            type="thread.created", visibility="private",
                            content={"thread_id": self_tid, "title": title, "origin": "self"},
                            links={"caused_by": [sleep_started.id], "related_to": [a.id, b.id]},
                            provenance={**prov, "origin": "self", "derived_from": [a.id, b.id]},
                        )
                    )
                    self.store.append(
                        EventCreate(
                            type="thread.activated", visibility="private",
                            content={"thread_id": self_tid},
                            links={"thread_id": self_tid, "caused_by": [c.id]},
                            provenance=dict(prov),
                        )
                    )
                    actions.append("self_thread")
                    durable.append(c)
                    used.update({a.id, b.id})
                    activated.extend((a.id, b.id))

        # -------------------------------------------------- record usage
        self.memory.index.record_retrieval(
            [e.id for e in recent] + [h.event.id for h in forgotten], at
        )
        if activated:
            self.memory.index.record_activation(activated, at, at)

        # ---------------------------------------------------- complete
        result = "dreamed" if dreamed else ("consolidated" if actions else "noop")
        self.store.append(
            EventCreate(
                type=SLEEP_COMPLETED,
                visibility="system",
                content={
                    "result": result,
                    "run_id": run_id,
                    "actions": actions,
                    "event_count": len(durable),
                },
                links={"caused_by": [d.id for d in durable] or [sleep_started.id]},
                provenance=dict(prov),
            )
        )
        return {
            "run_id": run_id,
            "trigger": trigger,
            "result": result,
            "actions": actions,
            "event_ids": [d.id for d in durable],
        }

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _note(
        ev: Event,
        actions: list[str],
        durable: list[Event],
        used: set[str],
        activated: list[str],
        ids: list[str],
    ) -> None:
        if ev.content.get("action"):
            actions.append(str(ev.content["action"]))
        durable.append(ev)
        used.update(ids)
        activated.extend(ids)

    def _consolidate(
        self, action: str, summary: str, related: list[str], caused_by: str, prov: dict
    ) -> Event:
        return self.store.append(
            EventCreate(
                type=MEMORY_CONSOLIDATED,
                visibility="private",
                content={"action": action, "summary": summary},
                links={"related_to": list(related), "caused_by": [caused_by]},
                provenance=dict(prov),
            )
        )

    def _duplicate_cluster(self, thoughts: list[Event]) -> list[Event]:
        """First near-duplicate cluster among recent thoughts (deterministic)."""
        if len(thoughts) < 2:
            return []
        vecs = {e.id: self.memory.index.embed(e) for e in thoughts}
        start = None
        for i in range(len(thoughts)):
            if any(
                _cosine(vecs[thoughts[i].id], vecs[thoughts[j].id]) > DUPLICATE_THRESHOLD
                for j in range(i + 1, len(thoughts))
            ):
                start = i
                break
        if start is None:
            return []
        cluster = [thoughts[start]]
        for k, e in enumerate(thoughts):
            if k != start and any(
                _cosine(vecs[e.id], vecs[c.id]) > DUPLICATE_THRESHOLD for c in cluster
            ):
                cluster.append(e)
        return cluster

    def _closest(self, anchor: Event, pool: list[Event], used: set[str]) -> tuple[Event | None, float]:
        """The pool event most similar to ``anchor`` (excluding used ids)."""
        vo = self.memory.index.embed(anchor)
        best: Event | None = None
        best_score = -1.0
        for e in pool:
            if not e.text or e.id == anchor.id or e.id in used:
                continue
            s = _cosine(vo, self.memory.index.embed(e))
            if s > best_score:
                best, best_score = e, s
        return best, best_score

    def _cross_family_pair(self, pool: list[Event], used: set[str]) -> tuple[Event, Event] | None:
        """Closest pair from DIFFERENT families (at most one per sleep)."""
        cand = [e for e in pool if e.text and e.id not in used]
        best: tuple[float, Event, Event] | None = None
        for i in range(len(cand)):
            for j in range(i + 1, len(cand)):
                a, b = cand[i], cand[j]
                if a.family == b.family:
                    continue
                s = _cosine(self.memory.index.embed(a), self.memory.index.embed(b))
                if s >= RELATED_THRESHOLD and (best is None or s > best[0]):
                    best = (s, a, b)
        return (best[1], best[2]) if best else None

    def _best_pair(self, pool: list[Event], used: set[str]) -> tuple[Event, Event] | None:
        """Most-similar pair (deterministic: first pair wins on ties)."""
        cand = [e for e in pool if e.text and e.id not in used]
        best: tuple[float, Event, Event] | None = None
        for i in range(len(cand)):
            for j in range(i + 1, len(cand)):
                s = _cosine(self.memory.index.embed(cand[i]), self.memory.index.embed(cand[j]))
                if best is None or s > best[0]:
                    best = (s, cand[i], cand[j])
        return (best[1], best[2]) if best else None

    # ------------------------------------------------- self-originated thread

    def _count_self_threads(self) -> int:
        return sum(
            1 for e in self.store.list(1000, type_prefix="thread.created")
            if e.content.get("origin") == "self"
        )

    def _near_identical_self_thread_exists(
        self, title: str, pair_ids: set[str], now_iso: str | None
    ) -> bool:
        """Round 13 consolidation data-quality fix: True iff a self-origin
        thread already exists within the consolidation window that is
        NEAR-IDENTICAL to the candidate pair — either built from the SAME
        memories (``derived_from``/``related_to`` pair match) or carrying the
        same title after normalization (casefold + whitespace/punctuation
        strip; the 14-char truncation makes distinct-but-same-topic pairs
        collide into byte-identical titles). The window is
        ``consolidation_pool_days`` when time-anchored (the same span of life
        the creation pool covers), else all existing self threads.
        Deterministic; no thresholds beyond exact/normalized equality."""
        cutoff: str | None = None
        if self.consolidation_pool_days is not None and now_iso:
            try:
                t0 = datetime.fromisoformat(now_iso)
                cutoff = (t0 - timedelta(days=self.consolidation_pool_days)).isoformat()
            except ValueError:
                cutoff = None
        for e in self.store.list(1000, type_prefix="thread.created"):
            if e.content.get("origin") != "self":
                continue
            if cutoff is not None and e.created_at < cutoff:
                continue
            derived = e.provenance.get("derived_from") or e.links.get("related_to") or []
            if pair_ids and set(str(i) for i in derived) == pair_ids:
                return True
            if _norm_title(e.content.get("title")) and \
                    _norm_title(e.content.get("title")) == _norm_title(title):
                return True
        return False

    def _best_cross_topic_pair(self, pool: list[Event]) -> tuple[Event, Event] | None:
        """The most-connected pair of memories from two DIFFERENT user topics —
        the grounded basis for a self synthesis. No similarity threshold: the mind
        links its closest cross-topic pair (a faithful "these two real memories are
        in my head together", never an invented insight). None until the mind has
        material from >=2 distinct user topics."""
        by_tid: dict[str, list[Event]] = {}
        for e in pool:
            tid = e.links.get("thread_id")
            if tid and e.text and thread_origin(self.store, tid) == "user":
                if self.thread_title_meta_filter and (
                        e.text.startswith(META_PREFIXES)
                        or e.type.startswith("thread.")):
                    # VE creation-side hygiene: a meta-prefixed memory (or a
                    # thread-lifecycle event whose text IS its title) must not
                    # name a new self thread — that is how titles like
                    # 「[continuity] 继」与「[continuity] 继」之间 are born and
                    # how the soft self-feedback loop recurses through
                    # creation. Substantive text only.
                    continue
                by_tid.setdefault(tid, []).append(e)
        groups = [by_tid[t] for t in by_tid]
        if len(groups) < 2:
            return None
        best: tuple[float, Event, Event] | None = None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                for a in groups[i]:
                    for b in groups[j]:
                        s = _cosine(self.memory.index.embed(a), self.memory.index.embed(b))
                        if best is None or s > best[0]:
                            best = (s, a, b)
        return (best[1], best[2]) if best else None
