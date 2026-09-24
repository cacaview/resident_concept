"""Behavioral telemetry (ADR-0004): what shape is Resident's mind taking?

These are *derived* readings over the append-only log (plus the optional
:class:`MemoryIndex` for age-at-activation). They are structural and
provable-from-history — never fabricated — so the accelerated life
simulation can honestly answer "why has it been thinking like this lately?"

Metrics:
- ``route_entropy``        — diversity of cognitive routes taken (0=monotone, 1=uniform)
- ``topic_entropy``        — diversity of recent activity families
- ``memory_age_at_activation`` — how old memories were when re-used
- ``revisit_rate``         — share of wakes that revisited old material
- ``thread_resurrection_rate`` — share of dormant threads that came back
- ``no_op_ratio``          — share of wakes that legitimately did nothing
- ``user_topic_dependency`` — how much recent thought is drawn from the user
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime

from .event_store import EventStore
from .memory_index import MemoryIndex
from .models import ROUTES
from .origin import SELF_ORIGINS, THOUGHT_ORIGINS, USER_ORIGINS
from .self_dynamics import (
    self_dynamics_profile,
    self_loop_concentration,
    self_thread_generation_depth,
    self_topic_diversity,
)
from .self_revival import SELF_REVIVAL_ROUTE_LIFT
from .world import world_profile

#: event families that are bookkeeping, not "the mind's activity" (for the
#: topic-entropy read — mirrors the runtime prefixes used elsewhere).
_RUNTIME_PREFIXES = ("wake.", "sleep.", "reentry.", "telemetry.", "circadian.")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _hours_between(earlier_iso: str | None, later_iso: str | None) -> float | None:
    """Hours from ``earlier_iso`` to ``later_iso`` (None if either unparseable)."""
    a, b = _parse_iso(earlier_iso), _parse_iso(later_iso)
    if a is None or b is None:
        return None
    return round(max(0.0, (b - a).total_seconds()) / 3600.0, 3)


def _normalized_entropy(counts: list[int]) -> float:
    """Shannon entropy of a count list, normalised to 0..1 (1 = uniform)."""
    total = sum(counts)
    if total <= 1:
        return 0.0
    n = len([c for c in counts if c > 0])
    if n <= 1:
        return 0.0
    h = -sum((c / total) * math.log2(c / total) for c in counts if c > 0)
    return h / math.log2(n)


class Telemetry:
    def __init__(self, store: EventStore, index: MemoryIndex | None = None):
        self.store = store
        self.index = index

    def snapshot(self, *, route_window: int = 300, thought_window: int = 40) -> dict:
        # --- route distribution / entropy --------------------------------
        route_events = self.store.list(route_window, type_prefix="wake.route_selected", order="desc")
        routes = [e.content.get("route") for e in route_events if e.content.get("route")]
        route_counts = Counter(routes)
        route_entropy = _normalized_entropy(list(route_counts.values()))
        revisit_rate = (route_counts.get("revisit", 0) / len(routes)) if routes else 0.0

        # --- no-op ratio --------------------------------------------------
        n_wakes = self.store.count("wake.started")
        n_noops = self.store.count("wake.noop")
        no_op_ratio = (n_noops / n_wakes) if n_wakes else 0.0

        # --- topic entropy (recent, non-runtime) --------------------------
        recent = [
            e for e in self.store.list(120, order="desc")
            if e.visibility != "system" and not e.type.startswith(("wake.", "sleep.", "reentry."))
        ]
        topic_counts = Counter(e.family for e in recent)
        topic_entropy = _normalized_entropy(list(topic_counts.values()))

        # --- memory age at activation ------------------------------------
        age_stats = self.index.age_stats() if self.index else {"count": 0, "mean": 0.0, "p50": 0.0, "max": 0.0}

        # --- thread resurrection -----------------------------------------
        n_dormant = self.store.count("thread.dormant")
        n_revisited = self.store.count("thread.revisited")
        resurrection_rate = (n_revisited / n_dormant) if n_dormant else 0.0

        # --- user topic dependency ---------------------------------------
        user_dep, dep_base = self._user_topic_dependency(thought_window)

        # --- thought-origin independence profile -------------------------
        origin_dist, origin_base = self._origin_profile(thought_window)
        user_recent_share = origin_dist.get("user_recent", 0) / origin_base if origin_base else 0.0
        user_derived_share = sum(origin_dist.get(o, 0) for o in USER_ORIGINS) / origin_base if origin_base else 0.0
        self_origin_share = sum(origin_dist.get(o, 0) for o in SELF_ORIGINS) / origin_base if origin_base else 0.0
        # independence = the share NOT growing from the user's *recent* input
        # (its own material + old-user + re-encounter + chance, all "not fresh
        # user pull"). This is the Phase-3 headline metric (vs user_recent_share).
        independence_share = 1.0 - user_recent_share

        return {
            "route_distribution": dict(route_counts),
            "route_entropy": round(route_entropy, 4),
            "topic_entropy": round(topic_entropy, 4),
            "revisit_rate": round(revisit_rate, 4),
            "thread_resurrection_rate": round(resurrection_rate, 4),
            "no_op_ratio": round(no_op_ratio, 4),
            "user_topic_dependency": round(user_dep, 4),
            "user_topic_dependency_base": dep_base,
            "origin_distribution": {o: origin_dist.get(o, 0) for o in THOUGHT_ORIGINS},
            "origin_base": origin_base,
            "user_recent_share": round(user_recent_share, 4),
            "user_derived_share": round(user_derived_share, 4),
            "self_origin_share": round(self_origin_share, 4),
            "independence_share": round(independence_share, 4),
            "self_dynamics": self_dynamics_profile(self.store),
            "memory_age_at_activation": age_stats,
            "n_wakes": n_wakes,
            "n_route_samples": len(routes),
            "world": world_profile(self.store),
        }

    def _origin_profile(self, thought_window: int) -> tuple[dict[str, int], int]:
        """Counts of each thought-origin label among recent persistent thoughts.

        Thoughts predating origin-tagging (or a thought with no origin) fall
        under ``unknown`` and are excluded from the base so the shares are
        meaningful. Returns (label -> count, base_count)."""
        dist: dict[str, int] = {}
        base = 0
        for t in self.store.list(thought_window, type_prefix="thought.created", order="desc"):
            origin = (t.metadata or {}).get("origin")
            if not origin:
                continue
            base += 1
            dist[origin] = dist.get(origin, 0) + 1
        return dist, base

    def _user_topic_dependency(self, thought_window: int) -> tuple[float, int]:
        """Fraction of recent thoughts that draw on a user message (via
        related_to). High = Resident's mind is mostly echoing the user's
        recent topics; low = it is thinking about its own material."""
        thoughts = self.store.list(thought_window, type_prefix="thought.created", order="desc")
        dependent = 0
        base = 0
        for t in thoughts:
            related = t.links.get("related_to") or []
            if not related:
                continue
            base += 1
            cites_user = any(
                (e := self.store.get(eid)) is not None and e.type == "conversation.user_message"
                for eid in related
                if isinstance(eid, str) and eid.startswith("evt_")
            )
            if cites_user:
                dependent += 1
        return (dependent / base) if base else 0.0, base

    # ----------------------------------------------------------- windowed read

    def window_report(
        self,
        since_iso: str,
        *,
        now_iso: str | None = None,
        circadian_state: str | None = None,
        route_lift: float = SELF_REVIVAL_ROUTE_LIFT,
        window: str | None = None,
    ) -> dict:
        """Windowed behavioural + circadian + revival report over
        ``[since_iso, now_iso]``. This is the read behind
        ``/api/telemetry?window=24h|7d|30d``.

        Every value is derived from events created within the window (ISO
        ``created_at`` is a uniform format, so string comparison is safe — the
        store's own ``created_at > ?`` filter relies on it), so the report is
        provenance-complete and reproducible from the log. ``circadian_state``
        is the state machine's *current* state (a live read, not an aggregate);
        the silences are measured at ``now_iso``.

        The revival metrics are the windowed port of
        ``scripts/revival_sweep.intervention_metrics``: ``eligible_wakes`` (the
        circadian-recognised quiet raised the *opportunity*), ``revival_engaged``
        (a dormant self-thread actually resurfaced), ``route_changed_by_bias``
        (counterfactual: ``revisit`` would NOT have won without the recorded
        lift). ``revival_delta`` here is the *within-window* self-origin thought
        count in revived self-threads (a lower bound on the mechanism's total
        effect — a resurfaced thread may continue naturally); the
        treatment-vs-control delta (circadian minus fixed) is a simulation
        quantity (``resident.circadian_sim``), not measurable in a single run.
        """
        if now_iso is None:
            latest = self.store.latest()
            now_iso = latest.created_at if latest else since_iso

        # --- circadian state + silences (measured at now_iso) ----------------
        user_silence_h = _hours_between(
            self._newest_created(("conversation.user_message",)), now_iso
        )
        meaningful_silence_h = _hours_between(self._newest_meaningful(), now_iso)

        # --- window-scoped event lists ---------------------------------------
        def _in(pfx: str) -> list:
            return [e for e in self.store.list(20000, type_prefix=pfx)
                    if since_iso <= e.created_at <= now_iso]

        wake_started = _in("wake.started")
        wake_noop = _in("wake.noop")
        route_events = _in("wake.route_selected")
        sleep_completed = _in("sleep.completed")
        consolidations = _in("memory.consolidated")

        # --- route distribution / entropy ------------------------------------
        routes = [e.content.get("route") for e in route_events if e.content.get("route")]
        route_counts = Counter(routes)
        route_entropy = _normalized_entropy(list(route_counts.values()))

        # --- no-op ratio -----------------------------------------------------
        n_wakes = len(wake_started)
        no_op_ratio = (len(wake_noop) / n_wakes) if n_wakes else 0.0

        # --- topic entropy (the mind's non-runtime activity in the window) ---
        recent = [
            e for e in self.store.list(20000, order="asc")
            if since_iso <= e.created_at <= now_iso
            and e.visibility != "system"
            and not e.type.startswith(_RUNTIME_PREFIXES)
        ]
        topic_counts = Counter(e.family for e in recent)
        topic_entropy = _normalized_entropy(list(topic_counts.values()))

        # --- thought-origin shares (thoughts in the window) ------------------
        origin_dist, origin_base = self._origin_profile_in_window(since_iso, now_iso)
        user_recent_share = origin_dist.get("user_recent", 0) / origin_base if origin_base else 0.0
        user_derived_share = (sum(origin_dist.get(o, 0) for o in USER_ORIGINS) / origin_base) if origin_base else 0.0
        self_origin_share = (sum(origin_dist.get(o, 0) for o in SELF_ORIGINS) / origin_base) if origin_base else 0.0

        # --- revival opportunity / intervention (windowed) -------------------
        iv = self._revival_intervention(route_events, since_iso, now_iso, route_lift)

        # --- revival delta / leverage (within-window) ------------------------
        revived_threads = {
            e.content.get("thread_id")
            for e in _in("thread.revisited")
            if e.content.get("self_revival") and e.content.get("thread_id")
        }
        revival_delta = sum(
            1 for t in self.store.list(20000, type_prefix="thought.created", order="asc")
            if since_iso <= t.created_at <= now_iso
            and (t.metadata or {}).get("origin") in SELF_ORIGINS
            and (t.links.get("thread_id") or t.content.get("thread_id")) in revived_threads
        )
        eligible = iv["eligible_wakes"]
        revival_leverage = round(revival_delta / eligible, 3) if eligible else 0.0

        # --- self-dynamics (windowed) ----------------------------------------
        self_loop_concentration_ = self_loop_concentration(self.store, since_iso)
        self_topic_diversity_ = self_topic_diversity(self.store, since_iso)
        generation_depth = self_thread_generation_depth(self.store, since_iso)

        return {
            "window": window,
            "since_iso": since_iso,
            "now_iso": now_iso,
            "circadian_state": circadian_state,
            "user_silence_h": user_silence_h,
            "meaningful_silence_h": meaningful_silence_h,
            "n_wakes": n_wakes,
            "noops": len(wake_noop),
            "no_op_ratio": round(no_op_ratio, 4),
            "n_sleeps": len(sleep_completed),
            "n_consolidations": len(consolidations),
            "route_distribution": dict(route_counts),
            "route_entropy": round(route_entropy, 4),
            "topic_entropy": round(topic_entropy, 4),
            "origin_base": origin_base,
            "user_recent_share": round(user_recent_share, 4),
            "user_derived_share": round(user_derived_share, 4),
            "self_origin_share": round(self_origin_share, 4),
            **iv,
            "revival_delta": revival_delta,
            "negative_delta_count": 0,  # a single live run has no control; see the sim
            "revival_leverage": revival_leverage,
            "self_loop_concentration": self_loop_concentration_,
            "self_topic_diversity": self_topic_diversity_,
            "generation_depth": generation_depth,
            "n_route_samples": len(routes),
            # Phase 6 (ADR-0008): the world-window read, scoped to the same window.
            "world": world_profile(self.store, since_iso=since_iso, now_iso=now_iso),
        }

    # ---------------------------------------------------- windowed sub-reads

    def _newest_created(self, prefixes: tuple[str, ...]) -> str | None:
        """ISO ``created_at`` of the newest event among ``prefixes`` (None if none)."""
        newest = None
        for p in prefixes:
            for e in self.store.list(500, type_prefix=p, order="desc"):
                if newest is None or e.created_at > newest:
                    newest = e.created_at
        return newest

    def _newest_meaningful(self) -> str | None:
        """ISO ``created_at`` of the newest non-runtime, non-system event (None if none)."""
        for e in self.store.list(1000, order="desc"):
            if e.visibility == "system":
                continue
            if e.type.startswith(_RUNTIME_PREFIXES):
                continue
            return e.created_at
        return None

    def _origin_profile_in_window(self, since_iso: str, now_iso: str) -> tuple[dict[str, int], int]:
        """Thought-origin counts among thoughts created in ``[since, now]``."""
        dist: dict[str, int] = {}
        base = 0
        for t in self.store.list(20000, type_prefix="thought.created", order="asc"):
            if not (since_iso <= t.created_at <= now_iso):
                continue
            origin = (t.metadata or {}).get("origin")
            if not origin:
                continue
            base += 1
            dist[origin] = dist.get(origin, 0) + 1
        return dist, base

    def _revival_intervention(
        self, route_events: list, since_iso: str, now_iso: str, route_lift: float
    ) -> dict:
        """Windowed port of ``scripts/revival_sweep.intervention_metrics``.

        ``route_events`` is already window-scoped. The counterfactual holds the
        exploration draw constant (it is not re-rolled) and only removes the
        recorded ``route_lift`` from ``revisit``'s score, re-argmaxing over
        ``ROUTES`` with first-maximal tie-breaking — so it measures exactly the
        wakes where the bias changed the outcome.
        """
        eligible = [e for e in route_events if e.content.get("self_revival_candidate")]
        revisit_in_lull = 0
        changed = 0
        for e in eligible:
            c = e.content
            if c.get("route") != "revisit":
                continue
            revisit_in_lull += 1
            scores = dict(c.get("scores") or {})
            if not scores:
                continue
            scores["revisit"] = scores.get("revisit", 0.0) - route_lift
            if max(ROUTES, key=lambda r: scores.get(r, float("-inf"))) != "revisit":
                changed += 1

        # engaged / self_candidate_selected (windowed ``thread.revisited``),
        # tracing each revival back to its wake's candidate via the thought.
        thoughts = {e.id: e for e in self.store.list(20000, type_prefix="thought.created")}
        rs_by_id = {e.id: e for e in route_events}
        engaged = 0
        selected = 0
        for e in self.store.list(20000, type_prefix="thread.revisited"):
            if not (since_iso <= e.created_at <= now_iso):
                continue
            c = e.content
            if not c.get("self_revival"):
                continue
            engaged += 1
            tid = c.get("thread_id")
            rid = None
            for cid in (e.links.get("caused_by") or []):
                t = thoughts.get(cid)
                if t:
                    for c2 in (t.links.get("caused_by") or []):
                        if c2 in rs_by_id:
                            rid = c2
                    if rid:
                        break
            if rid:
                cand = rs_by_id[rid].content.get("self_revival_candidate_thread")
                if cand and cand == tid:
                    selected += 1

        return {
            "eligible_wakes": len(eligible),
            "revisit_in_lull": revisit_in_lull,
            "route_changed_by_bias": changed,
            "self_candidate_selected": selected,
            "revival_engaged": engaged,
        }
