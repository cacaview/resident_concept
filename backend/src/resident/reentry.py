"""Re-entry: what Resident surfaces when the user returns.

When the user reappears after an absence, Resident examines the events that
happened since the last interaction and decides among ``share_now``,
``mention_later``, or ``nothing_to_share`` (EVENT_MODEL.md, "Re-entry";
ARCHITECTURE.md, "Re-entry").

Anti-fabrication guarantee: every candidate is derived *only* from real,
already-appended events, and each candidate carries the ``event_ids`` that
support its claim. :meth:`ReentryEngine.validate_candidate` rejects any
candidate citing an event that does not exist in the store — a sentence about
Resident's life must never outrun its provenance. The guarantee is enforced
on the production path: ``run()`` validates every candidate with
``validate_candidate`` before persisting it (ADR-0022); a candidate whose
provenance does not resolve is recorded as a ``reentry.skipped`` event with
the failure reason and is never proposed.
"""
from __future__ import annotations

from .event_store import EventStore
from .memory import MemoryRetrieval
from .models import MEMORY_SHARE_CANDIDATE, Event, EventCreate

# Event types eligible to be re-entry material. Wake/sleep/re-entry runtime
# noise and conversation itself are never "news about my day". A promoted
# share candidate IS deliberate surfacing and therefore eligible.
_CANDIDATE_PREFIXES: tuple[str, ...] = (
    "thought.created",
    "experience.created",
    "exploration.",
    "artifact.",
    "project.",
    "memory.share_candidate",
    "action.",
)

# High-signal types earn strength 2 (share_now); the rest are 1 (mention_later).
# A completed action is a real deed (strength 2); a failed one is mentionable
# (strength 1). The `intent.*` family is deliberately NOT a candidate: the intent
# is the impulse, the action is the deed — only the deed is "news about my day".
_STRENGTH_2: frozenset[str] = frozenset(
    {
        "thought.created",
        "experience.created",
        "artifact.created",
        "exploration.completed",
        MEMORY_SHARE_CANDIDATE,
        "action.completed",
    }
)

_EXCLUDED_PREFIXES: tuple[str, ...] = ("wake.", "sleep.", "reentry.", "conversation.")

# A thought/wake on one of these routes is a deliberate "step back" re-engagement
# with older material. A plain continuity/personal/self thought that merely
# recalls a memory incidentally is NOT enough to justify surfacing it.
_DELIBERATE_ROUTES: frozenset[str] = frozenset({"revisit", "distant", "serendipity"})
# Event types that are themselves a durable re-engagement (no route check needed).
_REFLECTION_TYPES: frozenset[str] = frozenset({"memory.consolidated", "thread.revisited"})


def _claim_for(e: Event) -> str:
    """An honest Chinese sentence derived ONLY from this event's type+text.

    It never describes activity the event does not contain.
    """
    text = e.text[:60]
    if e.type == "thought.created":
        return f"我想了一件事：「{text}」"
    if e.type == "experience.created":
        return f"我记下一段经历：「{text}」"
    if e.type == "exploration.completed":
        return f"我完成了一次探索：「{text}」"
    if e.type.startswith("exploration."):
        return f"我探索时留意到：「{text}」"
    if e.type.startswith("artifact."):
        return f"我做出了一个东西：「{text}」"
    if e.type.startswith("project."):
        return f"关于「{text}」，我留下了项目记录"
    if e.type == "action.completed":
        return f"我完成了一个行动：「{text}」"
    if e.type == "action.failed":
        return f"一个行动没有成功：「{text}」"
    return f"我留下了记录：「{text}」"


def _cites(event: Event, event_id: str) -> bool:
    """True iff the event's related_to link cites event_id."""
    related = event.links.get("related_to") or []
    if isinstance(related, str):
        related = [related]
    return event_id in related


def _is_later_reflection(e: Event) -> bool:
    """A later event that GENUINELY re-engages earlier material (anti-fabrication
    bar for promotion): a consolidation, a thread resurfacing, or a thought /
    wake on a deliberate step-back route. Resting on a memory, or recalling it
    incidentally while continuing another thread, does not qualify."""
    if e.type in _REFLECTION_TYPES:
        return True
    if e.type == "thought.created":
        return e.metadata.get("route") in _DELIBERATE_ROUTES
    if e.type == "wake.route_selected":
        return e.content.get("route") in _DELIBERATE_ROUTES
    return False


class ReentryEngine:
    """Selects what to share on re-entry, from real events only."""

    def __init__(self, store: EventStore, memory: MemoryRetrieval | None = None):
        self.store = store
        self.memory = memory or MemoryRetrieval(store)

    # ---------------------------------------------------------------- pure

    def evaluate(self, since: str | None = None) -> dict:
        """Pure evaluation (no writes): pick candidates since the baseline.

        Baseline: the ``since`` param if given, else the last user
        interaction's timestamp, else None (entire history).
        """
        baseline = self._baseline(since)

        events = self.store.since(baseline) if baseline else self.store.list(200, order="asc")

        # Newest first; the candidate pool is capped at 5.
        candidates: list[dict] = []
        for e in reversed(events):
            if e.visibility not in ("shareable", "user_visible"):
                continue
            if e.type.startswith(_EXCLUDED_PREFIXES):
                continue
            if not e.type.startswith(_CANDIDATE_PREFIXES):
                continue
            if not e.text:
                continue
            # A share candidate's claim is the ORIGINAL thought's claim, stored
            # verbatim at promotion time — it is never re-invented here.
            # (Event.text returns content["claim"] for these events.)
            claim = e.text if e.type == MEMORY_SHARE_CANDIDATE else _claim_for(e)
            candidates.append(
                {
                    "claim": claim,
                    "event_ids": [e.id],
                    "type": e.type,
                    "created_at": e.created_at,
                    "strength": 2 if e.type in _STRENGTH_2 else 1,
                }
            )
            if len(candidates) >= 5:
                break

        if not candidates:
            decision = "nothing_to_share"
        elif any(c["strength"] >= 2 for c in candidates):
            decision = "share_now"
        else:
            decision = "mention_later"

        return {
            "decision": decision,
            "candidates": candidates,
            "baseline": baseline,
            "evaluated_count": len(events),
        }

    # ------------------------------------------------------------ validation

    def validate_candidate(self, candidate: dict) -> bool:
        """True iff every cited event exists in the store.

        A candidate with no events, or citing a nonexistent event, is
        invalid — this is the anti-fabrication guarantee.
        """
        ids = candidate.get("event_ids") or []
        if not ids:
            return False
        return all(self.store.get(i) is not None for i in ids)

    # ---------------------------------------------------------------- write

    def _baseline(self, since: str | None) -> str | None:
        """The ``since`` param if given, else the last user interaction."""
        if since:
            return since
        last = self.store.last_user_interaction()
        return last.created_at if last is not None else None

    # ------------------------------------------------------- promotions

    def _already_promoted(self, thought_id: str) -> bool:
        """True iff a memory.share_candidate already points at this thought."""
        for e in self.store.list(500, type_prefix=MEMORY_SHARE_CANDIDATE):
            if e.content.get("source_thought_id") == thought_id:
                return True
        return False

    def consider_promotions(self, since: str | None = None) -> list[dict]:
        """Pure: private thoughts worth surfacing NOW as share candidates.

        A thought qualifies only if a LATER durable event genuinely
        revisited it: a ``memory.consolidated``, a ``thread.revisited``, or a
        later ``thought.created`` / ``wake.route_selected`` on a *deliberate*
        step-back route (revisit / distant / serendipity) — citing it via
        ``related_to``. Merely recalling a thought incidentally while
        continuing another thread does NOT justify surfacing it (anti-
        fabrication). A thought with no such reflection is never proposed, and
        a thought that already has a share candidate is not proposed twice.

        Returns dicts ``{"source_event_id", "justified_by", "claim"}``,
        most recently revisited first, capped at 5.
        """
        baseline = self._baseline(since)
        pool = self.store.since(baseline, limit=500) if baseline else self.store.list(500, order="asc")

        # Promotable private material: thoughts AND artifacts (ADR-0016 — a
        # creation is private like a thought, and surfaces through the same
        # justified-promotion path; with no artifact.* in the store this is
        # identical to the previous behaviour).
        material = [
            e
            for tp in ("thought.created", "artifact.created")
            for e in self.store.list(500, type_prefix=tp, order="asc")
            if e.visibility in ("private", "shareable")
            and e.text
            and not self._already_promoted(e.id)
        ]
        if not material:
            return []

        seq_of = {e.id: e.seq or 0 for e in pool}
        tseq = {t.id: t.seq or 0 for t in material}

        out: list[dict] = []
        for t in material:
            justified_by = [
                e.id
                for e in pool
                if (e.seq or 0) > (t.seq or 0)
                and _is_later_reflection(e)
                and _cites(e, t.id)
            ]
            if justified_by:
                out.append(
                    {
                        "source_event_id": t.id,
                        "justified_by": justified_by,  # ascending by seq
                        "claim": _claim_for(t),
                    }
                )

        # Most recently revisited first, then newest thought; cap 5.
        out.sort(
            key=lambda p: (seq_of.get(p["justified_by"][-1], 0), tseq[p["source_event_id"]]),
            reverse=True,
        )
        return out[:5]

    def promote(self, since: str | None = None) -> list[dict]:
        """Append a ``memory.share_candidate`` per promotion candidate.

        The original private thought is immutable and unchanged; the new
        shareable event carries provenance pointing at the original thought
        and at the later reflections that justified surfacing it. Every id in
        the links resolves (the store enforces this), so a promotion with no
        real later reflection can never be created. Returns one dict per
        created candidate.
        """
        created: list[dict] = []
        for promo in self.consider_promotions(since):
            if self._already_promoted(promo["source_event_id"]):
                continue  # already surfaced: never promote twice
            justified_by = promo["justified_by"]
            event = self.store.append(
                EventCreate(
                    type=MEMORY_SHARE_CANDIDATE,
                    visibility="shareable",
                    content={
                        "claim": promo["claim"],
                        "source_thought_id": promo["source_event_id"],
                    },
                    links={
                        "related_to": [promo["source_event_id"], *justified_by],
                        "caused_by": [justified_by[-1]] if justified_by else [],
                    },
                    provenance={
                        "source": "reentry_engine",
                        "promotion_of": promo["source_event_id"],
                        "justified_by": justified_by,
                    },
                )
            )
            created.append(
                {
                    "event_id": event.id,
                    "source_event_id": promo["source_event_id"],
                    "claim": promo["claim"],
                    "justified_by": justified_by,
                }
            )
        return created

    # ---------------------------------------------------------------- run

    def run(self, since: str | None = None) -> dict:
        """Evaluate and persist the decision as re-entry events.

        Provenance is enforced at the persistence boundary (ADR-0022): every
        candidate must pass :meth:`validate_candidate` before anything is
        written. A candidate citing a nonexistent event is recorded as a
        ``reentry.skipped`` event carrying the failure reason — it never
        enters ``reentry.proposed``. Valid candidates persist exactly as
        before (byte-identical events and result keys).
        """
        res = self.evaluate(since)

        # Anti-fabrication guarantee at the persistence boundary: a candidate
        # whose provenance does not resolve is a REJECTION, recorded as an
        # event — not an exception, and never proposed.
        valid: list[dict] = []
        rejected: list[dict] = []
        for c in res["candidates"]:
            (valid if self.validate_candidate(c) else rejected).append(c)
        res["candidates"] = valid
        if rejected:
            # The persisted decision/count reflect candidates with resolvable
            # provenance only (same rule evaluate() uses).
            res["decision"] = (
                "share_now" if any(c["strength"] >= 2 for c in valid)
                else "mention_later" if valid
                else "nothing_to_share"
            )
            res["rejected_candidates"] = rejected

        evaluated = self.store.append(
            EventCreate(
                type="reentry.evaluated",
                visibility="system",
                content={
                    "decision": res["decision"],
                    "candidate_count": len(valid),
                    "baseline": res["baseline"],
                },
                provenance={"source": "reentry_engine"},
            )
        )
        if valid:
            related: list[str] = []
            for c in valid:
                related.extend(c["event_ids"])
            proposed = self.store.append(
                EventCreate(
                    type="reentry.proposed",
                    visibility="private",
                    content={
                        "decision": res["decision"],
                        "candidates": valid[:3],
                    },
                    links={
                        "caused_by": [evaluated.id],
                        "related_to": related,
                    },
                    provenance={"source": "reentry_engine"},
                )
            )
            res["proposed_event_id"] = proposed.id
        elif not rejected:
            skipped = self.store.append(
                EventCreate(
                    type="reentry.skipped",
                    visibility="system",
                    content={"reason": "nothing_to_share"},
                    links={"caused_by": [evaluated.id]},
                    provenance={"source": "reentry_engine"},
                )
            )
            res["skipped_event_id"] = skipped.id
        if rejected:
            # One replayable rejection event per invalid candidate, in the
            # existing reentry.skipped shape with the provenance failure as
            # the reason (missing ids resolved against the store).
            rejected_event_ids: list[str] = []
            for r in rejected:
                missing = [
                    i for i in (r.get("event_ids") or []) if self.store.get(i) is None
                ]
                skipped = self.store.append(
                    EventCreate(
                        type="reentry.skipped",
                        visibility="system",
                        content={
                            "reason": "provenance_failed",
                            "candidate_event_ids": r.get("event_ids") or [],
                            "missing_event_ids": missing,
                            "candidate_type": r.get("type"),
                        },
                        links={"caused_by": [evaluated.id]},
                        provenance={"source": "reentry_engine"},
                    )
                )
                rejected_event_ids.append(skipped.id)
            res["rejected_event_ids"] = rejected_event_ids
            if "skipped_event_id" not in res:
                res["skipped_event_id"] = rejected_event_ids[-1]
        res["evaluated_event_id"] = evaluated.id
        return res
