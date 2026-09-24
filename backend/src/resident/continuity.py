"""Continuity across absence (v0.2 Step 4, ADR-0012).

Step 4 is **not** "user left → record how long → user returns → summarise the
gap". A gap summary is a Daily Brief. The question is: when the *conversational*
timeline breaks, how does Resident notice that real time passed between the two
of us — and which of the things that **actually changed it** during that gap are
still alive when the user speaks again?

The five owner constraints, mechanically:

1. **Absence is a fact, not an emotion.** ``absence.detected`` records the gap
   duration and its two boundary events — nothing else. No sentiment fields
   exist anywhere in this module (enforced by test).
2. **Re-entry ≠ summary.** Nothing compresses the gap's wakes/world events into
   a broadcast. Candidate texts are *quotes* of real evidence events, never
   generated prose.
3. **Only state-changing experiences qualify.** The candidate classes are the
   structural signatures of "this changed me": a thread resurfacing
   (``thread.revisited``), a question re-met (``question.revisited``), an
   impression collision matured (``impression.promoted``), a self-origin thought
   chain extended, world material genuinely entering thinking (a thought whose
   ``recalled`` cites world-family material), and something newly going dormant.
   Plain wakes, skips and fetch failures leave no candidate.
4. **Silence on return is a first-class outcome.** No qualifying change → no
   candidates → ``reentry.noop``. Candidates exist but the return message's own
   retrieval surfaces none → also ``reentry.noop``.
5. **"Happened" and "worth mentioning now" are separate judgments.** Eligibility
   (provenance) is derived structurally from the log. Current relevance is the
   return message's OWN retrieval footprint — ``memory.semantic_near(msg)``: a
   candidate is selected only if the message literally recalls its evidence.
   Deterministic, no LLM, and by construction the same retrieval the mind
   itself uses on a ``personal`` wake.

v1 writes only the fact layer — ``absence.detected`` / ``reentry.candidate`` /
``reentry.selected`` / ``reentry.noop``. It never generates "欢迎回来" language:
how to *say* something is a later, separate question from what is true.

Default OFF (``continuity=False`` / ``RESIDENT_CONTINUITY=on``): the sealed
arms never run this code, so the sealed regression stays byte-identical.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .event_store import EventStore
from .memory import MemoryRetrieval
from .models import Event, EventCreate
from .origin import SELF_ORIGINS
from .semantic import EmbeddingUnavailable
from .world import question_state


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class ContinuityParams:
    """The shape of the continuity layer. The threshold is an *envelope rail*
    ("long enough that the conversational timeline genuinely broke"), not a
    tuned value; the caps are rails like the world-window budgets."""

    absence_threshold_s: float = 4 * 3600.0   # a shorter gap is the same conversation
    max_candidates: int = 8                    # safety rail, not a quota
    retrieval_k: int = 8                       # how deep the message's recall reaches


# --- candidate detection (structural signatures of "this changed me") ---------
#
# Each detector scans the gap window and yields (class, evidence_ids, quote).
# A quote is ALWAYS the underlying event's own text (truncated) — the candidate
# never says anything the evidence does not say.


def _quote(e: Event, limit: int = 80) -> str:
    return (e.text or "")[:limit]


def _detect_thread_revisited(e: Event) -> dict | None:
    tid = e.content.get("thread_id")
    return {"cls": "thread_revisited", "evidence": [e.id],
            "quote": f"线程「{tid}」重新浮现（{e.content.get('reason', '')}）"}


def _detect_question_revisited(e: Event, store: EventStore) -> dict | None:
    qid = e.content.get("question_id")
    q = store.get(qid) if qid else None
    quote = (q.content.get("question") if q is not None else "") or _quote(e)
    ev = [e.id] + ([qid] if qid else [])
    # the candidate is anchored in the question's PRIMARY evidence too (the
    # observations / memories the question grew from): a re-meeting is about
    # the thing itself, and the thing itself is what retrieval actually
    # reaches — the question's own transcription is diluted by design
    # (ADR-0010's "transcription vs the thing itself").
    if q is not None:
        for rid in (q.metadata or {}).get("question", {}).get("evidence") or []:
            if store.get(rid) is not None and rid not in ev:
                ev.append(rid)
    return {"cls": "question_revisited", "evidence": ev, "quote": quote}


def _detect_impression_collision(e: Event, store: EventStore) -> dict | None:
    imp_id = e.content.get("impression_id")
    ev = [e.id] + ([imp_id] if imp_id else [])
    return {"cls": "impression_collision", "evidence": ev,
            "quote": _quote(e) or str(e.content.get("reason", ""))}


def _detect_thought(e: Event, store: EventStore) -> dict | None:
    """A thought during the gap: did world material genuinely enter it
    (world_changed_mind), or is it the self's own chain continuing
    (self_thought_extended)? User-derived thoughts are NOT gap continuity —
    they grow from the user, not from the absence."""
    recalled = (e.metadata or {}).get("recalled") or []
    if isinstance(recalled, str):
        recalled = [recalled]
    for rid in recalled:
        r = store.get(rid)
        if r is not None and r.family == "world":
            return {"cls": "world_changed_mind", "evidence": [e.id, rid],
                    "quote": _quote(e)}
    if (e.metadata or {}).get("origin") in SELF_ORIGINS:
        return {"cls": "self_thought_extended", "evidence": [e.id], "quote": _quote(e)}
    return None


def _expand_evidence(evidence: list[str], store: EventStore, cap: int = 8) -> set[str]:
    """The candidate's full ABOUTNESS chain: the evidence itself plus what it
    grew from (related_to links, recalled ids, question evidence). A candidate
    is contextually alive when the message reaches anything in this chain —
    the thing, or the material it came from. Bounded (cap) and every id
    resolvable."""
    out: list[str] = []
    for eid in evidence:
        if eid not in out:
            out.append(eid)
    for eid in list(evidence):
        e = store.get(eid)
        if e is None:
            continue
        chain: list[str] = []
        rel = e.links.get("related_to") or []
        chain.extend(rel if isinstance(rel, list) else [rel])
        chain.extend((e.metadata or {}).get("recalled") or [])
        qmeta = (e.metadata or {}).get("question")
        if isinstance(qmeta, dict):
            chain.extend(qmeta.get("evidence") or [])
        for rid in chain:
            if isinstance(rid, str) and rid not in out and store.get(rid) is not None:
                out.append(rid)
            if len(out) >= cap:
                return set(out)
    return set(out)


class ContinuityEngine:
    """Records the fact layer of an absence: what changed, and what is alive."""

    def __init__(self, store: EventStore, memory: MemoryRetrieval | None = None,
                 params: ContinuityParams | None = None):
        self.store = store
        self.memory = memory or MemoryRetrieval(store)
        self.params = params or ContinuityParams()

    # ------------------------------------------------------------------ main

    def on_user_return(self, user_event: Event) -> dict:
        """Call once per arriving user message. Silently does nothing when the
        gap is shorter than the threshold (the same conversation continues);
        otherwise writes the fact layer and returns a small summary dict."""
        prev = self._previous_user_event(user_event)
        if prev is None:
            return {"absence": None}
        t_now = _parse_iso(user_event.created_at)
        t_prev = _parse_iso(prev.created_at)
        if t_now is None or t_prev is None:
            return {"absence": None}
        gap_s = (t_now - t_prev).total_seconds()
        if gap_s < self.params.absence_threshold_s:
            return {"absence": None}

        absence = self.store.append(EventCreate(
            type="absence.detected", visibility="system",
            content={"gap_s": round(gap_s, 1),
                     "since_event_id": prev.id, "until_event_id": user_event.id},
            links={"caused_by": [user_event.id], "related_to": [prev.id]},
            provenance={"source": "continuity_engine"},
        ))

        candidates = self._candidates(prev, user_event, gap_s)
        cand_events: list[Event] = []
        for c in candidates[: self.params.max_candidates]:
            cand_events.append(self.store.append(EventCreate(
                type="reentry.candidate", visibility="system",
                content={"cls": c["cls"], "quote": c["quote"], "gap_s": round(gap_s, 1)},
                links={"caused_by": [user_event.id], "related_to": c["evidence"]},
                provenance={"source": "continuity_engine", "class": c["cls"]},
            )))

        selected = self._select(user_event, candidates)
        if selected:
            ev_ids: list[str] = []
            for c in selected:
                for eid in c["evidence"]:
                    if eid not in ev_ids:
                        ev_ids.append(eid)
            decision_event = self.store.append(EventCreate(
                type="reentry.selected", visibility="system",
                content={"classes": sorted({c["cls"] for c in selected}),
                         "gap_s": round(gap_s, 1)},
                links={"caused_by": [user_event.id], "related_to": ev_ids},
                provenance={"source": "continuity_engine"},
            ))
            decision = "selected"
        else:
            decision_event = self.store.append(EventCreate(
                type="reentry.noop", visibility="system",
                content={"reason": "no candidate alive in this context",
                         "candidates": len(candidates), "gap_s": round(gap_s, 1)},
                links={"caused_by": [user_event.id]},
                provenance={"source": "continuity_engine"},
            ))
            decision = "noop"

        return {
            "absence": {"gap_s": round(gap_s, 1), "event_id": absence.id},
            "candidates": [{"cls": c["cls"], "quote": c["quote"]} for c in candidates],
            "selected": [{"cls": c["cls"], "quote": c["quote"]} for c in selected],
            "decision": decision, "decision_event_id": decision_event.id,
        }

    # ------------------------------------------------------------- detection

    def _previous_user_event(self, user_event: Event) -> Event | None:
        """The user message BEFORE this one (the absence's left boundary)."""
        prev: Event | None = None
        for e in self.store.list(200, type_prefix="conversation.user_message", order="asc"):
            if (e.seq or 0) >= (user_event.seq or 0):
                break
            prev = e
        return prev

    def _candidates(self, prev: Event, user_event: Event, gap_s: float) -> list[dict]:
        """Structural scan of the gap window: which events changed state. The
        window is bounded by SEQ (not timestamps): events can share a second
        with the boundary, and the sequence is the honest order of life."""
        window = [e for e in self.store.since_seq(prev.seq or 0, limit=2000)
                  if (e.seq or 0) < (user_event.seq or 0)]
        out: list[dict] = []
        seen_anchor: set[str] = set()
        for e in window:
            c = None
            anchor = None
            if e.type == "question.revisited":
                c = _detect_question_revisited(e, self.store)
                if c is not None:
                    anchor = f"question:{c['evidence'][1] if len(c['evidence']) > 1 else e.id}"
            elif e.type == "thread.revisited":
                c = _detect_thread_revisited(e)
                anchor = f"thread:{e.content.get('thread_id')}"
            elif e.type == "impression.promoted":
                c = _detect_impression_collision(e, self.store)
                anchor = f"imp:{e.id}"
            elif e.type == "thought.created":
                c = _detect_thought(e, self.store)
                anchor = f"thought:{e.id}"
            if c is None:
                continue
            # fact granularity: one continuity fact per entity per absence —
            # a thread re-surfacing twice inside one gap is still one fact
            if anchor in seen_anchor:
                continue
            seen_anchor.add(anchor)
            out.append(c)
        # derived: something newly went dormant during this absence
        now_state = question_state(self.store, now_iso=user_event.created_at)
        then_state = question_state(self.store, now_iso=prev.created_at)
        open_then = {q["question_id"] for q in then_state["questions"] if q["status"] == "open"}
        for q in now_state["questions"]:
            if q["status"] == "dormant" and q["question_id"] in open_then:
                if q["question_id"] in seen_anchor:
                    continue
                seen_anchor.add(q["question_id"])
                out.append({"cls": "went_dormant", "evidence": [q["question_id"]],
                            "quote": q["text"] or ""})
        return out

    # -------------------------------------------------------------- selection

    def _select(self, user_event: Event, candidates: list[dict]) -> list[dict]:
        """Current relevance = the return message's OWN retrieval footprint.
        A candidate is alive iff the message literally recalls its evidence.
        A retrieval outage degrades to nothing selected (an honest noop), and
        a candidate is never selected because it seems important in the
        abstract — only because THIS message reached for it."""
        msg = user_event.content.get("text", "") or ""
        if not msg.strip() or not candidates:
            return []
        try:
            hits = self.memory.semantic_near(msg, n=self.params.retrieval_k)
        except EmbeddingUnavailable:
            return []
        hit_ids = {h.id for h in hits}
        return [c for c in candidates
                if hit_ids & _expand_evidence(c["evidence"], self.store)]
