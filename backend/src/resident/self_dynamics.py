"""Self-dynamics telemetry (ADR-0006): does a thought of Resident's own
*survive the day and re-influence itself*?

Phase 4 studies the mind's own material as a little dynamical system. These
metrics are **derived reads** over the append-only log (never new mutable
state), so they are reproducible and provenance-complete. They measure:

- ``self_thread_revival_rate``      — of the lull wakes where a dormant
  self-thread was an eligible candidate, the share where it was actually
  revived (revisited). How often the revival mechanism fires.
- ``self_thread_avg_lifetime_days``  — mean age of self-threads (creation →
  last engagement): do self thoughts *live*, or die the same day?
- ``self_loop_concentration``        — top-1 self-thread's share of self-origin
  thoughts (+ distinct count). High ⇒ the mind is *sucked into one* self idea.
- ``self_thread_generation_depth``   — depth of "thought begetting thought":
  the longest chain of self-origin thoughts where each cites an earlier
  self-origin thought in the SAME thread.
- ``longest_self_chain``             — longest run of consecutive self-origin
  thoughts (a user turn or a non-self thought breaks it; rests do not).
- ``self_topic_diversity``           — how many distinct self-threads the mind
  engages + the entropy of self thoughts over them (broad vs. narrow).
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from .origin import SELF_ORIGINS

_EXTERNAL = ("conversation.user_message", "experience.created")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _self_threads(store) -> dict[str, dict]:
    """self-originated threads → {created_at, last_at, title}."""
    out: dict[str, dict] = {}
    for e in store.list(10000, type_prefix="thread.created"):
        if e.content.get("origin") == "self":
            tid = e.content.get("thread_id")
            out[tid] = {"created_at": e.created_at, "last_at": e.created_at,
                        "title": e.content.get("title", "")}
    for e in store.list(10000):
        tid = e.links.get("thread_id") or e.content.get("thread_id")
        if tid in out and e.created_at > out[tid]["last_at"]:
            out[tid]["last_at"] = e.created_at
    return out


def _self_thoughts(store, since_iso: str | None = None) -> list[dict]:
    """All self-origin durable thoughts, oldest first. When ``since_iso`` is
    given, only thoughts at/after that instant are included (windowed read;
    ISO timestamps are same-format, so string comparison is safe — the store's
    own `created_at > ?` filter relies on the same)."""
    out: list[dict] = []
    for e in store.list(10000, type_prefix="thought.created", order="asc"):
        if since_iso is not None and e.created_at < since_iso:
            continue
        origin = (e.metadata or {}).get("origin")
        if origin in SELF_ORIGINS:
            rel = e.links.get("related_to") or []
            out.append({
                "id": e.id, "seq": e.seq or 0, "created_at": e.created_at,
                "thread_id": e.links.get("thread_id") or e.content.get("thread_id"),
                "related_to": rel if isinstance(rel, list) else [rel],
            })
    return out


def _revival_counts(store) -> tuple[int, int]:
    """(eligible_wakes, engaged). ``eligible`` = lull wakes where a dormant
    self-thread was an eligible candidate (``wake.route_selected`` records the
    candidate as available); ``engaged`` = those wakes where the mind actually
    engaged a self-thread (``thread.revisited`` flagged ``self_revival``)."""
    eligible = sum(1 for e in store.list(10000, type_prefix="wake.route_selected")
                   if e.content.get("self_revival_candidate"))
    engaged = sum(1 for e in store.list(10000, type_prefix="thread.revisited")
                  if e.content.get("self_revival"))
    return eligible, engaged


def self_thread_revival_rate(store) -> float:
    """Of the lull wakes where a dormant self-thread was an eligible candidate,
    the share where the mind actually engaged a self-thread. ``0.0`` when the
    mechanism never had an eligible opportunity — see
    ``self_thread_revival_detail`` for the raw counts, so a ``0.0`` reads as
    "0 of N" (N may be 0 or 1) rather than a silent 'measured zero'."""
    eligible, engaged = _revival_counts(store)
    return round(engaged / eligible, 4) if eligible else 0.0


def self_thread_revival_detail(store) -> dict:
    """Raw counts behind :func:`self_thread_revival_rate` (transparency for the
    degenerate small-N case)."""
    eligible, engaged = _revival_counts(store)
    return {"eligible_wakes": eligible, "engaged": engaged}


def self_thread_avg_lifetime_days(store) -> float:
    st = _self_threads(store)
    if not st:
        return 0.0
    total = 0.0
    for info in st.values():
        c, l = _parse_iso(info["created_at"]), _parse_iso(info["last_at"])
        if c is not None and l is not None:
            total += max(0.0, (l - c).total_seconds())
    return round(total / len(st) / 86400.0, 3)


def self_loop_concentration(store, since_iso: str | None = None) -> dict:
    tids = [t["thread_id"] for t in _self_thoughts(store, since_iso) if t["thread_id"]]
    if not tids:
        return {"top1_share": 0.0, "n_distinct": 0, "total": 0}
    counts = Counter(tids)
    top1 = max(counts.values())
    return {"top1_share": round(top1 / len(tids), 4), "n_distinct": len(counts),
            "total": len(tids)}


def self_thread_generation_depth(store, since_iso: str | None = None) -> int:
    """Longest chain of self-origin thoughts where each cites an EARLIER
    self-origin thought in the SAME thread (thought begetting thought)."""
    thoughts = _self_thoughts(store, since_iso)
    by_id = {t["id"]: t for t in thoughts}
    memo: dict[str, int] = {}

    def depth(tid: str, thread: str, seq: int, chain: frozenset[str]) -> int:
        if tid in memo:
            return memo[tid]
        if tid in chain:  # cycle guard (shouldn't happen: earlier-seq edges only)
            return 1
        best = 1
        for rid in by_id[tid]["related_to"]:
            r = by_id.get(rid)
            if r and r["thread_id"] == thread and r["seq"] < seq:
                best = max(best, 1 + depth(rid, thread, seq, chain | {tid}))
        memo[tid] = best
        return best

    return max((depth(t["id"], t["thread_id"] or "", t["seq"], frozenset()) for t in thoughts),
               default=0)


def longest_self_chain(store) -> int:
    """Longest run of consecutive self-origin thoughts; a user turn or a
    non-self thought breaks it, a rest/noop does not."""
    best = cur = 0
    for e in store.list(100000, order="asc"):
        if e.type in _EXTERNAL:
            cur = 0
        elif e.type == "thought.created":
            if (e.metadata or {}).get("origin") in SELF_ORIGINS:
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
    return best


def self_topic_diversity(store, since_iso: str | None = None) -> dict:
    """Distinct self-threads engaged + normalized entropy of self thoughts over them."""
    tids = [t["thread_id"] for t in _self_thoughts(store, since_iso) if t["thread_id"]]
    if not tids:
        return {"n_distinct": 0, "entropy": 0.0}
    counts = Counter(tids)
    total = len(tids)
    n = len(counts)
    h = -sum((c / total) * math.log2(c / total) for c in counts.values())
    return {"n_distinct": n, "entropy": round(h / math.log2(n), 4) if n > 1 else 0.0}


def self_dynamics_profile(store) -> dict:
    """All six self-dynamics metrics in one derived read."""
    return {
        "self_thread_revival_rate": self_thread_revival_rate(store),
        "self_thread_revival_detail": self_thread_revival_detail(store),
        "self_thread_avg_lifetime_days": self_thread_avg_lifetime_days(store),
        "self_loop_concentration": self_loop_concentration(store),
        "self_thread_generation_depth": self_thread_generation_depth(store),
        "longest_self_chain": longest_self_chain(store),
        "self_topic_diversity": self_topic_diversity(store),
    }
