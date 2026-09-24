"""Mind Loop: one wake cycle of the resident's own timeline (ADR-0004).

Lifecycle:

    wake → assemble state → choose a cognitive route (STATE-DRIVEN) →
    retrieve memories across several paths (not one top-k) → reflect →
    nothing durable?  → wake.noop → done
    something durable → persist thought (+ thread bookkeeping) → done

Phase 2 changes from v0.1:
- The route is chosen by :class:`RoutePolicy` from Resident's *state*
  (recent experience, active/dormant threads, route history, topic
  concentration, time since meaningful activity) with small exploration
  noise — NOT by the model and NOT by a die. Randomness is a perturbation,
  never the primary decider.
- Retrieval uses :meth:`MemoryRetrieval.multi_recall` (several paths,
  interleaved) so a recall does not collapse onto one similarity top-k.
- The wake records which memories it recalled (``recalled`` metadata) so
  memory-age-at-activation and retrieval diversity are measurable.
- A periodic ``telemetry.snapshot`` records the behavioural metrics.
- "Nothing happened" (rest / no durable content) stays a first-class outcome.

Every event carries provenance so any claim in the log traces back to the
run that produced it.
"""
from __future__ import annotations

import json
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from .event_store import EventStore
from .memory import MemoryRetrieval
from .mental_state import MentalState, ThreadView
from .models import ROUTES, Event, EventCreate, TELEMETRY_SNAPSHOT
from .providers import DeterministicFakeProvider, ModelProvider, ProviderError
from .origin import classify_origin, thread_origin
from .private_projects import check_genesis
from .route_policy import RoutePolicy, RouteState
from .self_revival import (
    _HISTORY_TYPES, RevivalParams, offering_allowed,
    qualifying_dormant_self_thread, revival_gate,
)
from .semantic import EmbeddingUnavailable
from .telemetry import Telemetry
from .circadian import QUIET_STATES

SYSTEM_PROMPT = (
    "You are the mind of a persistent digital resident. "
    "Input and output are strict JSON. Never invent experiences that "
    "have no supporting event. A wake may legitimately do nothing."
)

# A wake may only draw on memories these routes genuinely reach back for.
_NEEDS_RECALL = ("revisit", "distant", "serendipity")
_RUNTIME_PREFIXES = ("wake.", "sleep.", "reentry.", "telemetry.")
# Route-meta thought prefixes (the deterministic fake prefixes its thoughts with
# these). They are NOT substantive content, so thread payloads skip them.
# Round 7: the single source of truth moved to ``resident.meta_prefixes`` — the
# creation side (sleep.py) needs the same list; duplicated lists drift.
from .meta_prefixes import META_PREFIXES as _META_PREFIXES  # noqa: E402


def _parse_iso(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _most_recent(threads) -> "ThreadView | None":
    """The most-recently-active thread (by last activity timestamp).

    The mind continues what it was most recently on — and, crucially, a fresh
    self-originated thread (just seeded by sleep) is reachable this way instead
    of being shadowed by an older user thread (the previous ``[0]`` selection).
    ISO timestamps compare correctly as strings, so ``max`` is chronological."""
    if not threads:
        return None
    return max(threads, key=lambda t: t.last_event_at or "")


class MindLoop:
    """Runs one wake cycle per call. No state beyond the event store + index."""

    def __init__(
        self,
        store: EventStore,
        memory: MemoryRetrieval | None = None,
        provider: ModelProvider | None = None,
        policy: RoutePolicy | None = None,
        rng: random.Random | None = None,
        *,
        telemetry_every: int = 20,
        force_route: str | None = None,
        self_revival: bool = False,
        revival: RevivalParams | None = None,
        shadow_semantic=None,
        thread_selection: str = "recent",
        thread_yield_s: float | None = None,
        context_exclude_prev_thread: bool = False,
        dormant_selection: str = "recent",
        substantive_context: bool = False,
        thread_query_substantive_only: bool = False,
        recall_pool_hygiene: bool = False,
        zero_history_bootstrap: bool = False,
        private_projects: bool = False,
        agency_propose: bool = False,
    ):
        self.store = store
        self.memory = memory or MemoryRetrieval(store)
        # v1.0 (ADR-0018, Phase B): when on, the reflect output contract offers
        # an OPTIONAL ``intent`` field. The Mind may *propose* an opportunity to
        # act, but it never executes it and never grants itself permission — the
        # proposal is routed to the Agency, whose deterministic policy decides
        # (ADR-0019). Off by default, so flag-off wakes are byte-identical.
        self.agency_propose = bool(agency_propose)
        # E2 (round 8, self_thread_concentration lineage): retrieval-corpus
        # hygiene. When True, P1 route-meta stubs and P3 echo-copies are
        # excluded from the RECALL POOL for every route (the filter lives on
        # the shared MemoryRetrieval, so revisit/question/semantic paths are
        # covered too). Writes stay complete — the event log is untouched.
        if recall_pool_hygiene:
            self.memory.recall_pool_hygiene = True
        self.provider = provider or DeterministicFakeProvider()
        self.policy = policy or RoutePolicy()
        self.rng = rng or random.Random()
        self.telemetry_every = int(telemetry_every)
        self.telemetry = Telemetry(store, self.memory.index)
        # Phase-4: enable the state-gated dormant self-thread revival bias
        # (off by default — the A/B control in the dynamics experiment).
        self.self_revival = bool(self_revival)
        # The *shape* of the bias when it is on (gate thresholds + lever sizes).
        # Defaults are the Phase-4 values; the Phase-4.1 sweep varies a subset.
        self._rev = revival or RevivalParams()
        # Active Living S3 (ADR-0015): how the continuity route picks WHICH
        # active thread to continue. "recent" (sealed) = the most recently
        # active one; "material" = the one whose focus text best matches the
        # present context (recent life), recency as the tiebreak. A selection
        # rule, not a weight: every thread competes with what it actually is.
        self.thread_selection = thread_selection
        # S3R (ADR-0015): temporary yielding — a thread continued very recently
        # yields its turn (when others exist) even if its material matches.
        # Owner-sanctioned refractory; breaks the most-recent monopoly without
        # any quota or rotation schedule.
        self.thread_yield_s = thread_yield_s
        # Round 5 (self_thread_concentration localization, ADR-0015 lineage):
        # VA — "context-exclusion": when reading the present context for thread
        # selection, exclude the thread continued in the immediately previous
        # continuity wake. Tests the soft self-feedback loop: the thread just
        # continued wrote the present context, so a purely material rule can
        # still re-select it. Default False = sealed behavior.
        self.context_exclude_prev_thread = bool(context_exclude_prev_thread)
        self._last_continuity_thread_id: str | None = None
        # VB — "dormant-material": the revisit route picks the dormant thread by
        # the same material-match rule as active selection, instead of
        # ``_most_recent(dormant)``. Default "recent" = sealed behavior.
        self.dormant_selection = dormant_selection
        # VC — "substantive-context": the present context for selection uses
        # only substantive text (world/question/user material); self-written
        # thread-continuation thoughts (thought.* events / route-meta prefixes)
        # are excluded. Extends ``_substantive_text`` filtering to the context
        # read. Default False = sealed behavior.
        self.substantive_context = bool(substantive_context)
        # VD — "thread-query substantive-only" (round 6, self_thread_concentration):
        # the thread side of the same soft feedback loop VC attacked from the
        # context side. ``_thread_query`` already skips route-meta thought
        # EVENTS, but falls back to the thread TITLE when a thread has no
        # substantive event — and self-thread titles can be built from
        # route-meta memory text (sleep.py ``_best_cross_topic_pair`` does not
        # filter meta prefixes), so a thread's matched/recall text can be its
        # own meta output, recursively. VD removes the title fallback: a thread
        # competes (and is recalled) ONLY on what it substantively contains.
        # The present-context computation is untouched (that is VA/VC's axis).
        # Default False = sealed behavior.
        self.thread_query_substantive_only = bool(thread_query_substantive_only)
        # Round 10 (self_thread_concentration, VE4): zero-history cold-start
        # bootstrap. A freshly created self thread has zero events in
        # ``_HISTORY_TYPES`` (its only text is the title), so under VB
        # dormant-material selection it never wins a revisit (it cannot match
        # live material), and under the revival gate it never qualifies
        # (``MIN_SELF_EVENTS = 1``) — round 9's cold-start deadlock: born with
        # no history → never picked → history never accrues. While ANY dormant
        # self thread has zero real history, the revisit pick falls back to the
        # SEALED recency rule (``_most_recent(dormant)``) — the accidental
        # bootstrap that worked in the E2 runs. The newborn is by construction
        # the most recent dormant thread, gets its FIRST revisit, accrues
        # history, and from then on the rule no longer applies to it (it
        # competes on material like every other thread). An opportunity rule,
        # not a quota: zero-history self threads are rare by construction, and
        # with none present the behavior is identical to ``dormant_selection``
        # as configured. Default False = sealed/VE3 behavior.
        self.zero_history_bootstrap = bool(zero_history_bootstrap)
        # v0.2 Step 2 (ADR-0010): the semantic SHADOW layer — records what real
        # vectors would have recalled, audit-only. None = no shadow (default).
        self.shadow_semantic = shadow_semantic
        # ADR-0016: private-project genesis emission, default OFF (a candidate
        # behind a config flag; off = byte-identical sealed behaviour). When on,
        # a persisted thought is followed by the in-mind H1 genesis check,
        # which appends ``project.created`` ONLY if a full genesis chain exists.
        self.private_projects = bool(private_projects)
        # TEST-ONLY override: pin the route to exercise the downstream
        # mechanics (recording, bookkeeping, noop, world guard) in isolation.
        # Off by default; production selection is always RoutePolicy-driven.
        self._force_route = force_route

    # ------------------------------------------------------------------ wake

    async def wake_once(self, trigger: str = "manual", *, circadian_state: str | None = None) -> dict:
        run_id = f"wake_{uuid.uuid4().hex}"
        prov = {"source": "mind_loop", "run_id": run_id, "model": self.provider.model_id}
        # Phase 5: the circadian state this wake runs in (if driven by the
        # circadian scheduler). A recognized quiet (QUIET/DROWSY/SLEEP) raises
        # the dormant self-thread revival *opportunity* (the gate's silence bar
        # drops to the ~6 h working region); it never forces a selection.
        in_quiet_state = circadian_state in QUIET_STATES if circadian_state else False

        # (a) assemble state BEFORE this wake appends anything
        state = MentalState.from_store(self.store)
        new_since, total = self._route_context()
        # Phase-4: dormant self-thread revival candidate (state-gated, gentle).
        # Computed before route selection so the route policy can gently favour
        # "revisit" when a sleeping self-thought is worth resurfacing.
        now_iso = self.store.latest().created_at if self.store.latest() else None
        revival_candidate = None
        if self.self_revival and revival_gate(
            self.store, n_active=len(state.active_threads), now_iso=now_iso,
            silence_s=self._rev.silence_s, low_stimulus_s=self._rev.low_stimulus_s,
            max_active=self._rev.max_active, in_quiet_state=in_quiet_state,
            mode=self._rev.gate_mode,
        ):
            revival_candidate = qualifying_dormant_self_thread(
                state, self.store, now_iso,
                min_dormant_s=self._rev.min_dormant_s,
                min_self_events=self._rev.min_self_events,
            )
            # S2 (ADR-0015): the independent per-thread opportunity clock — an
            # unresolved self-thread is OFFERED at most once per interval.
            # Opportunity only: the route policy still decides, and a noop
            # stays legitimate.
            if (revival_candidate is not None
                    and self._rev.opportunity_clock_s is not None
                    and not offering_allowed(
                        self.store, revival_candidate.thread_id, now_iso,
                        self._rev.opportunity_clock_s)):
                revival_candidate = None

        route_state = RouteState(
            active_threads=len(state.active_threads),
            dormant_threads=len(state.dormant_threads),
            new_since_last_wake=new_since,
            total_events=total,
            has_recent_user_message=self.memory.has_recent("conversation.user_message"),
            topic_concentration=self.memory.topic_concentration(),
            has_exploration_recently=self.memory.has_recent("exploration"),
            route_history=self._recent_routes(),
            recent_topics=self.memory.recent_topic_signals(),
            time_since_meaningful_s=self._time_since_meaningful(),
            no_op_ratio=self._no_op_ratio(),
            self_revival_available=revival_candidate is not None,
            self_revival_route_lift=self._rev.route_lift,
        )

        # (b) state-driven route selection (exploration noise only)
        route, scores, reason = self.policy.select(route_state, self.rng)
        if self._force_route is not None:  # test-only: pin the route
            route = self._force_route
            reason = f"forced route (test): {route}"
        if route not in ROUTES:
            route, reason = "rest", f"invalid route {route!r}; resting"
        decision = "noop" if route == "rest" else "persist"
        if route == "world":
            decision, reason = "noop", "world route unbound in v0.1"

        wake_started = self.store.append(
            EventCreate(
                type="wake.started", visibility="system",
                content={"trigger": trigger}, provenance=dict(prov),
            )
        )
        route_selected = self.store.append(
            EventCreate(
                type="wake.route_selected", visibility="system",
                content={"route": route, "decision": decision, "reason": reason, "scores": scores,
                         "self_revival_candidate": revival_candidate is not None,
                         "self_revival_candidate_thread": (
                             revival_candidate.thread_id if revival_candidate is not None else None),
                         # Phase 5: which circadian state this wake ran in (None =
                         # not circadian-driven). Informational — lets analysis read
                         # "revival in genuine quiet states" vs. forced selection.
                         "circadian_state": circadian_state},
                links={"caused_by": [wake_started.id]}, provenance=dict(prov),
            )
        )

        # (c) no-op is a first-class outcome
        if decision == "noop":
            return self._finish_noop(prov, route_selected, run_id, trigger, route, reason)

        # (d) multi-path recall (never a single top-k) + record the recall
        recalled, sel_thread, recall_query = self._recall_for_route(route, state, revival_candidate)
        recalled_ids = [h.id for h in recalled]
        if recalled_ids:
            self.memory.index.record_retrieval(recalled_ids, wake_started.created_at)

        # (d2) v0.2 Step 2 (ADR-0010): the semantic SHADOW. If attached, compare
        # what legacy hashing recalled vs what real vectors would have recalled
        # — recorded as audit, NEVER influencing the route/recall above.
        if self.shadow_semantic is not None:
            self._run_semantic_shadow(route, recall_query, wake_started, prov)

        # (e) reflection: provider produces grounded thought text for the route
        thought, refl_reason, thread_id, origin, proposed_intent = self._reflect(
            route, state, recalled, sel_thread, wake_started, route_selected, prov,
            revival_candidate
        )
        if thought is None:
            return self._finish_noop(prov, route_selected, run_id, trigger, route, refl_reason)

        # (f) periodic behavioural telemetry
        if self.telemetry_every and self.store.count("wake.started") % self.telemetry_every == 0:
            self._emit_telemetry(prov)

        return {
            "run_id": run_id, "trigger": trigger, "route": route, "decision": "persist",
            "result": "thought", "event_id": thought.id, "text": thought.text,
            "thread_id": thread_id, "reason": reason, "origin": origin,
            "proposed_intent": proposed_intent,  # Phase B (ADR-0018); None unless agency_propose
        }

    # --------------------------------------------------------------- helpers

    def _reflect(
        self, route, state, recalled, sel_thread, wake_started, route_selected, prov,
        revival_candidate=None,
    ) -> tuple[Any | None, str, str | None, str, dict | None]:
        """Ask the provider to turn the recalled memories into a grounded
        thought for this route. Returns (thought_event|None, reason,
        thread_id, origin, proposed_intent|None)."""
        # Provenance guard: the "reach back" routes must have material to draw on.
        if not recalled and route in _NEEDS_RECALL:
            return None, "no recalled material to draw on", None, None, None

        recent = [
            {"id": h.id, "type": h.event.type, "text": h.event.text,
             "thread_id": h.event.links.get("thread_id")}
            for h in recalled
        ]
        active = state.active_threads
        dormant = state.dormant_threads
        last_user = self.store.last_user_interaction()
        # The thread payload shown to the model is the SAME thread the recall was
        # built from (sel_thread) — so a self-thread wake reflects on the
        # self-thread, not on some other active[0].
        ctx = {
            "task": "thought", "route": route,
            "thread": (self._thread_payload(sel_thread) if route == "continuity" and sel_thread
                       else (self._thread_payload(active[0]) if active and route not in ("continuity", "revisit") else None)),
            "dormant_thread": (self._thread_payload(sel_thread) if route == "revisit" and sel_thread
                               else (self._thread_payload(dormant[0]) if dormant and route not in ("continuity", "revisit") else None)),
            "recent": recent,
            "mind_brief": state.mind_brief(),
            "stats": {
                "wakes": self.store.count("wake.started"),
                "noops": self.store.count("wake.noop"),
                "route_dist": self._route_distribution(),
            },
            "user_message": (
                {"id": last_user.id, "text": last_user.content.get("text", "")}
                if last_user is not None else None
            ),
            # the OUTPUT CONTRACT, stated explicitly for real providers (the
            # deterministic fake hardcodes this shape; a real model must be
            # told). The machinery below has always expected exactly this.
            "output": {
                "format": "strict JSON object",
                "schema": {
                    "text": "string — your durable thought for this wake, grounded ONLY in the material above",
                    "decision": '"persist" to leave this thought, or "noop" to leave nothing',
                    "links": {
                        "thread_id": "string or null",
                        "related_to": ["ids of recalled events you actually used"],
                    },
                    "visibility": '"private"',
                    **({"intent": (
                        'optional object {"text": string, "capability": string or null} — ONLY when '
                        "the material above genuinely surfaces an opportunity to DO something with "
                        "your tools; otherwise omit it. You are PROPOSING an opportunity, not acting: "
                        "a separate deterministic policy decides whether it may run, and you cannot "
                        "grant it yourself. Most wakes should omit it."
                    )} if self.agency_propose else {}),
                },
                "rule": "cite only event ids that appear above; both decisions are legitimate — persist when the material above genuinely gives you something durable to say, noop when it doesn't",
            },
        }
        out = self._provider_json(ctx)
        if out is None:
            return None, "model provider unavailable during reflection; resting", None, None, None

        text = str(out.get("text") or "").strip()
        if out.get("decision") == "noop" or not text:
            return None, "model produced no durable content", None, None, None

        model_links = dict(out.get("links") or {})
        # Canonical thread: the one this wake actually continued/revisited. Set it
        # from the selected thread rather than trusting the model to echo it back —
        # this is what makes a self-thread wake persist a thought ON the self-thread
        # (so its origin is measured as self_thread, not a user thread).
        if sel_thread is not None and route in ("continuity", "revisit"):
            thread_id = sel_thread.thread_id
        else:
            thread_id = model_links.get("thread_id")
        # related_to = the provider's specific citations + every memory this
        # wake actually recalled (dangling ids dropped; the store enforces it).
        recalled_ids = [h.id for h in recalled]
        related = list(dict.fromkeys(
            [i for i in (model_links.get("related_to") or [])
             if isinstance(i, str) and self.store.get(i) is not None]
            + recalled_ids
        ))
        links = {k: v for k, v in model_links.items() if k != "related_to"}
        links["related_to"] = related
        links["caused_by"] = [wake_started.id, route_selected.id]
        if thread_id:
            links["thread_id"] = thread_id

        # Where this thought comes from (deterministic, grounded) — stored so the
        # independence profile is measurable (origin.py / ADR-0004).
        origin = classify_origin(
            self.store, route=route, thread_id=thread_id,
            recalled=recalled, now_iso=wake_started.created_at,
        )
        thought = self.store.append(
            EventCreate(
                type="thought.created",
                visibility=out.get("visibility", "private"),
                content={"text": text},
                links=links,
                provenance={**prov, "route": route},
                metadata={"route": route, "recalled": recalled_ids, "origin": origin},
            )
        )
        if recalled_ids:
            # these memories were durably used by this thought
            self.memory.index.record_activation(recalled_ids, wake_started.created_at, wake_started.created_at)

        # route-specific thread bookkeeping (only when the thought references a thread)
        if thread_id and route == "continuity":
            self.store.append(
                EventCreate(type="thread.activated", visibility="private",
                            content={"thread_id": thread_id},
                            links={"thread_id": thread_id, "caused_by": [thought.id]},
                            provenance=dict(prov))
            )
        elif thread_id and route == "revisit":
            # A self-revival = this wake engaged a dormant self-thread WHILE a
            # qualifying candidate was eligible (gate open) — a genuine "a
            # sleeping thought of my own resurfaced", not a natural re-mention.
            self.store.append(
                EventCreate(type="thread.revisited", visibility="private",
                            content={"thread_id": thread_id,
                                     "self_revival": (origin == "self_thread"
                                                      and revival_candidate is not None)},
                            links={"thread_id": thread_id, "caused_by": [thought.id]},
                            provenance=dict(prov))
            )

        # grounded thread resurfacing: if this recall surfaced memories that
        # belong to a DORMANT thread, that thread is re-surfacing (a faithful
        # record of a real recall, not invented content). Capped at one per wake.
        self._resurface_dormant_threads(recalled, prov, thought.id)

        # ADR-0016 (default OFF): the in-mind genesis check. Appends
        # ``project.created`` only if a full H1 genesis chain exists in the
        # log; with the flag off this line never runs (byte-identical).
        if self.private_projects:
            check_genesis(self.store, now_iso=thought.created_at,
                          provenance=dict(prov), enabled=True)

        self.store.append(
            EventCreate(
                type="wake.completed", visibility="system",
                content={"route": route, "result": "persisted", "run_id": prov["run_id"],
                         "event_id": thought.id, "recalled": recalled_ids},
                links={"caused_by": [thought.id]}, provenance=dict(prov),
            )
        )
        # v1.0 (ADR-0018, Phase B): an OPTIONAL proposed intent — a proposal, not
        # an action. The Mind states an opportunity; the Agency's deterministic
        # policy decides. Returned for the orchestrator to route (never acted on
        # here). With agency_propose off this is always None (byte-identical).
        proposed_intent = None
        if self.agency_propose:
            raw_intent = out.get("intent")
            if isinstance(raw_intent, dict):
                it_text = str(raw_intent.get("text") or "").strip()
                if it_text:
                    proposed_intent = {
                        "text": it_text,
                        "capability": raw_intent.get("capability"),
                    }
        return thought, "persisted", thread_id, origin, proposed_intent

    def _recall_for_route(self, route: str, state: MentalState, revival_candidate=None):
        """Choose the retrieval paths + query for a route (multi-path recall).

        Returns ``(hits, selected_thread, query)`` where ``selected_thread`` is
        the ThreadView this wake is continuing/revisiting (None otherwise) — so
        reflection uses the SAME thread the recall was built from (not an
        arbitrary active[0], which would tag the thought to the wrong thread) —
        and ``query`` is the recall query ("" for path-only routes), which the
        semantic shadow reuses for its canonical comparison.

        ``revival_candidate`` (Phase-4): a state-gated dormant self-thread the
        mind *may* gently prefer to revisit; see the ``revisit`` branch.
        """
        q = ""
        thread_id = None
        anchor = None
        sel_thread = None
        active = state.active_threads
        dormant = state.dormant_threads
        if route == "continuity" and active:
            sel_thread = self._select_active_thread(active)
            thread_id = sel_thread.thread_id
            q = self._thread_query(sel_thread)
            paths = ("semantic_near", "revisit")
        elif route == "revisit" and dormant:
            # Sealed: most-recent dormant thread. VB (dormant-material): the
            # same material-match rule as active selection — a dormant thread
            # is revisited when what it IS ABOUT is alive right now.
            if self.dormant_selection == "material" and len(dormant) > 1:
                # VE4 zero-history bootstrap: while a dormant self thread has
                # no real history yet, use the sealed recency pick so the
                # newborn can receive its first revisit (see ctor comment).
                if self.zero_history_bootstrap and any(
                    self._is_zero_history_self_thread(t) for t in dormant
                ):
                    sel_thread = _most_recent(dormant)
                else:
                    sel_thread = self._select_material_thread(dormant)
            else:
                sel_thread = _most_recent(dormant)
            # Gentle, state-gated revival: a qualifying dormant self-thread wins
            # with probability REVIVAL_STRENGTH (< 1); the default most-recent
            # thread still wins otherwise. No forced selection, no quota.
            if (revival_candidate is not None
                    and revival_candidate.thread_id != sel_thread.thread_id
                    and self.rng.random() < self._rev.thread_strength):
                sel_thread = revival_candidate
            thread_id = sel_thread.thread_id
            if self.thread_query_substantive_only:
                # VD: recall by the thread's substantive content, never by a
                # (possibly meta-derived) title; thread_id is a deterministic,
                # content-free fallback that keeps recall functional.
                q = self._thread_query(sel_thread) or sel_thread.thread_id
            else:
                q = sel_thread.title or sel_thread.thread_id
            paths = ("revisit", "forgotten")
        elif route == "personal":
            last = self.store.last_user_interaction()
            q = (last.content.get("text", "") if last else "")
            anchor = last.created_at if last else None
            paths = ("semantic_near", "temporal", "revisit")
        elif route == "distant":
            paths = ("distant", "serendipity", "forgotten")
        elif route == "serendipity":
            paths = ("serendipity", "distant", "forgotten")
        elif route == "self":
            paths = ("forgotten", "semantic_near")
        else:
            paths = ("semantic_near", "forgotten")
        hits = self.memory.multi_recall(
            q, paths=paths, n_each=3, total=6, rng=self.rng, thread_id=thread_id, anchor=anchor
        )
        return hits, sel_thread, q

    def _run_semantic_shadow(self, route: str, query: str, wake_started, prov: dict) -> None:
        """v0.2 Step 2 (ADR-0010): record one canonical shadow comparison — the
        same question, answered by legacy hashing and by the real-vector layer.
        Audit only: it never influences the route, the recall, or the thought,
        and it is written to the SHADOW SIDECAR, not the event log (an audit
        record inside the log would shift the mind's bounded recent-windows and
        perturb the very thing the shadow observes). A provider outage degrades
        explicitly via the layer's own audit; the shadow records nothing."""
        try:
            record = self.shadow_semantic.shadow_compare(
                self.memory, route=route, query=query,
                context={"wake_id": wake_started.id})
        except EmbeddingUnavailable:
            return
        if record is None:
            return
        self.shadow_semantic.record_shadow(record)

    def _resurface_dormant_threads(self, recalled, prov, thought_id) -> None:
        """Record ``thread.revisited`` for a dormant thread whose memories this
        recall just surfaced. At most one per wake. This is a grounded signal —
        the mind really did pull that thread's memories back into play — not a
        content fabrication; it changes bookkeeping only."""
        if not recalled:
            return
        state = MentalState.from_store(self.store)
        dormant_ids = {t.thread_id for t in state.dormant_threads}
        active_ids = {t.thread_id for t in state.active_threads}
        if not dormant_ids:
            return
        for h in recalled:
            tid = h.event.links.get("thread_id") or h.event.content.get("thread_id")
            if tid and tid in dormant_ids and tid not in active_ids:
                self.store.append(
                    EventCreate(
                        type="thread.revisited", visibility="private",
                        content={"thread_id": tid, "reason": "resurfaced via recall"},
                        links={"thread_id": tid, "caused_by": [thought_id]},
                        provenance=dict(prov),
                    )
                )
                return

    def _finish_noop(self, prov, route_selected, run_id, trigger, route, reason) -> dict:
        noop = self.store.append(
            EventCreate(
                type="wake.noop", visibility="system",
                content={"route": route, "reason": reason},
                links={"caused_by": [route_selected.id]}, provenance=dict(prov),
            )
        )
        self.store.append(
            EventCreate(
                type="wake.completed", visibility="system",
                content={"route": route, "result": "noop", "run_id": run_id},
                links={"caused_by": [noop.id]}, provenance=dict(prov),
            )
        )
        return {
            "run_id": run_id, "trigger": trigger, "route": route, "decision": "noop",
            "result": "noop", "event_id": noop.id, "reason": reason,
        }

    def _emit_telemetry(self, prov: dict) -> None:
        snap = self.telemetry.snapshot()
        self.store.append(
            EventCreate(
                type=TELEMETRY_SNAPSHOT, visibility="system",
                content=snap, links={}, provenance=dict(prov),
            )
        )

    def _provider_json(self, ctx: dict[str, Any]) -> dict | None:
        """One provider call + JSON parse; ``None`` on any failure (rest, don't fabricate)."""
        try:
            raw = json.loads(
                self.provider.complete(system=SYSTEM_PROMPT, prompt=json.dumps(ctx, ensure_ascii=False))
            )
        except (ProviderError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    def _route_context(self) -> tuple[int, int]:
        """(new *experience* since the last wake.*, total events).

        World-window **decision telemetry** (``world.window_opened`` /
        ``world.window_skipped``) is excluded: it is "the window considered /
        declined to look" — audit logging, not experience the mind should react
        to. Without this, a run of world *skips* would register as "new activity"
        and shift the route, confounding the A/B/C isolation (ADR-0008). World
        *experience* (``world.observation`` / ``world.experience`` /
        ``impression.*``) still counts — the window is an experience source.
        (``circadian.state_changed`` is left counting, preserving the sealed
        Phase-5 circadian arm.) v0.2 (ADR-0009): world *fetch audit*
        (``world.fetch_*``) is excluded the same way — "the window looked and
        the world did not answer" is not experience. v0.2 Step 2 (ADR-0010):
        the ``semantic.*`` family (shadow comparisons, degraded-mode audit,
        cluster labels) is excluded the same way — all of it is machinery
        audit, none of it experience. All three exclusions are no-ops for any
        fixture-mode run (no such events exist), which the sealed
        regression proves."""
        last_wake = self.store.list(1, type_prefix="wake", order="desc")
        base_seq = (last_wake[0].seq or 0) if last_wake else 0
        new_since = sum(
            1 for e in self.store.since_seq(base_seq, 100_000)
            if not e.type.startswith("wake.")
            and not e.type.startswith("world.window_")
            and not e.type.startswith("world.fetch_")
            and not e.type.startswith("semantic.")
        )
        # total "life" events: exclude world decision-telemetry + fetch audit
        # + semantic audit for the same reason
        total = (self.store.count()
                 - self.store.count("world.window_opened")
                 - self.store.count("world.window_skipped")
                 - self.store.count("world.fetch_")
                 - self.store.count("semantic."))
        return new_since, total

    def _recent_routes(self, n: int = 8) -> list[str]:
        events = self.store.list(n, type_prefix="wake.route_selected", order="desc")
        return [e.content.get("route") for e in events if e.content.get("route")]

    def _no_op_ratio(self) -> float:
        wakes = self.store.count("wake.started")
        if not wakes:
            return 0.0
        return self.store.count("wake.noop") / wakes

    def _time_since_meaningful(self) -> float:
        """Seconds since the last non-runtime, non-system event (large if none)."""
        latest = self.store.latest()
        now = _parse_iso(latest.created_at) if latest else None
        for e in self.store.list(200, order="desc"):
            if e.visibility == "system":
                continue
            if e.type.startswith(_RUNTIME_PREFIXES):
                continue
            et = _parse_iso(e.created_at)
            if now is not None and et is not None:
                return max(0.0, (now - et).total_seconds())
            return 0.0
        return 3 * 86400.0  # nothing meaningful has happened yet

    def _route_distribution(self) -> dict[str, int]:
        dist: dict[str, int] = {}
        for e in self.store.list(10_000, type_prefix="wake.route_selected"):
            r = e.content.get("route")
            if r:
                dist[r] = dist.get(r, 0) + 1
        return dist

    def _present_context_text(self) -> str:
        """S3 (ADR-0015): what is happening RIGHT NOW, as far as the log shows —
        the newest few substantive events (user message / world material /
        thoughts). The material signal thread selection is grounded in; no new
        information, just a deterministic read of the recent life."""
        parts: list[str] = []
        for e in self.store.list(12, order="desc"):
            if e.type.startswith(("wake.", "sleep.", "telemetry.", "circadian")):
                continue
            if self.substantive_context and e.type.startswith("thought."):
                # VC: self-written thread-continuation thoughts are not the
                # "present context" — they are the monopoly's own output.
                continue
            txt = self._substantive_text(e) if self.substantive_context else e.text
            if txt:
                parts.append(txt[:140])
            if len(parts) >= 4:
                break
        return " ".join(parts)

    def _select_active_thread(self, active):
        """Which active thread does this continuity wake continue?

        "recent" (sealed): the most recently active one — at high event
        density this becomes a monopoly (the same thread is always the most
        recent). "material": the thread whose focus text best matches the
        present context, recency as the tiebreak — a thread is continued when
        what it IS ABOUT is alive right now, not merely because it was the
        last thing touched. A selection rule over real material; no quota,
        no weights. VA adds "context-exclusion": the thread continued in the
        immediately previous continuity wake is not eligible (it wrote the
        present context the material rule reads — soft self-feedback)."""
        eligible = active
        if self.context_exclude_prev_thread and self._last_continuity_thread_id:
            excluded = [t for t in active
                        if t.thread_id != self._last_continuity_thread_id]
            if excluded:
                eligible = excluded
        # VE4 zero-history bootstrap (round 10, ACTIVE seam): a newborn self
        # thread carries only thread.created/thread.activated — it never goes
        # dormant before its first continuation, so the dormant-path fallback
        # cannot reach it. While an eligible ACTIVE thread is a self-origin
        # thread with zero real history, use the sealed recency pick so the
        # newborn receives its FIRST continuation (it is by construction the
        # most recent) and starts accruing history; the rule then stops
        # applying to it. Opportunity rule, not a quota (see ctor comment).
        if (self.zero_history_bootstrap
                and any(self._is_zero_history_self_thread(t) for t in eligible)):
            sel = _most_recent(eligible)
        elif self.thread_selection != "material" or len(active) <= 1:
            sel = _most_recent(eligible)
        else:
            sel = self._select_material_thread(eligible)
        if self.context_exclude_prev_thread:
            # Remember for the NEXT continuity wake (this wake's exclusion set
            # must be fixed before this wake's continuation is recorded).
            self._last_continuity_thread_id = sel.thread_id if sel else None
        return sel

    def _is_zero_history_self_thread(self, t: ThreadView) -> bool:
        """True iff the thread is self-origin AND has no durable mental event
        (no ``_HISTORY_TYPES`` event) — round 10's cold-start condition, the
        same definition the revival gate uses (``self_revival._has_real_history``)."""
        if thread_origin(self.store, t.thread_id) != "self":
            return False
        for eid in t.event_ids:
            e = self.store.get(eid)
            if e is not None and e.type in _HISTORY_TYPES:
                return False
        return True

    def _select_material_thread(self, threads):
        """The material-match rule shared by active (S3) and dormant (VB)
        selection: the thread whose focus text best matches the present
        context, recency as the tiebreak. No quota, no weights."""
        eligible = threads
        if self.thread_yield_s:
            newest = max((t.last_event_at or "") for t in threads)
            newest_t = _parse_iso(newest)
            if newest_t is not None:
                # temporary yielding: a thread continued WITHIN the yield
                # window rests its turn — the others get the material match.
                # (The inverted filter — yielding only stale threads — is a
                # no-op: the monopoly thread is always the newest.)
                cutoff_iso = (
                    newest_t - timedelta(seconds=self.thread_yield_s)).isoformat()
                rested = [t for t in threads if (t.last_event_at or "") <= cutoff_iso]
                if rested:
                    eligible = rested
        ctx = self._present_context_text()
        if not ctx.strip():
            return _most_recent(eligible)
        cv = self.memory.embedder.embed([ctx])[0]
        best, best_score = None, None
        for t in eligible:
            txt = self._thread_query(t)
            if not txt:
                continue
            s = self.memory.embedder.cosine(cv, self.memory.embedder.embed([txt])[0])
            if best is None or s > best_score + 1e-12:
                best, best_score = t, s
            elif abs(s - best_score) <= 1e-12 and (t.last_event_at or "") > (best.last_event_at or ""):
                best, best_score = t, s
        return best if best is not None else _most_recent(eligible)

    def _thread_query(self, t: ThreadView) -> str:
        """A query string for a thread: its newest SUBSTANTIVE event, else title.

        Route-meta thoughts (``[continuity] …``, ``[revisit] …``) are skipped so
        a continuity/revisit wake builds on the thread's real content rather than
        recursively quoting its own previous meta-thought."""
        for eid in reversed(t.event_ids):
            e = self.store.get(eid)
            if self.thread_query_substantive_only and (
                    e.type.startswith("thread.")
                    or (t.title and e.text == t.title)):
                # VD: no title-shaped text. ``Event.text`` maps the content
                # "title" key, so the thread.created event itself carries the
                # title — and a self-thread title can be built from route-meta
                # memory text (sleep.py ``_best_cross_topic_pair`` does not
                # filter meta prefixes). Admitting it re-imports the thread's
                # own meta output into the material match — the second arm of
                # the soft self-feedback loop. A thread with no substantive
                # content event simply does not compete on material.
                continue
            text = self._substantive_text(e)
            if text:
                return text
        return "" if self.thread_query_substantive_only else t.title

    def _thread_payload(self, t: ThreadView) -> dict[str, Any]:
        """Provider payload for a thread: id/title + its most recent SUBSTANTIVE
        text events (route-meta thoughts skipped, see _thread_query)."""
        evs: list[dict[str, str]] = []
        for eid in reversed(t.event_ids):
            e = self.store.get(eid)
            text = self._substantive_text(e)
            if text:
                evs.append({"id": e.id, "text": text})
            if len(evs) >= 3:
                break
        evs.reverse()
        return {"thread_id": t.thread_id, "title": t.title, "last_events": evs}

    @staticmethod
    def _substantive_text(e: "Event | None") -> str:
        """An event's text iff it is real content (not a route-meta thought)."""
        if e is None or not e.text:
            return ""
        t = e.text
        if t.startswith(_META_PREFIXES):
            return ""
        return t
