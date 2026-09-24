"""Stateful cognitive-route selection (ADR-0004).

The route a wake takes is decided from **Resident's state**, not from a die:

- base weight per route comes from recent experience, active/dormant threads,
  topic concentration, exploration recency, and time since meaningful
  activity;
- **repetition fatigue**: when the recent routes (or topics) keep looping on
  one thing, the "departure" routes (revisit / distant / serendipity / rest)
  gain weight and the repeated route is damped, so the mind wanders instead
  of spinning;
- randomness is a small **exploration perturbation** only (``exploration``
  noise), never the primary decider;
- ``rest`` ("nothing happened") stays a first-class, often-winning outcome.

The policy is deterministic for a fixed seed, which is what makes the
accelerated life simulation reproducible. It returns the full score vector so
every route decision is *provenance-complete*: the log records why a route
won, not just which one did.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from .models import DEPARTURE_ROUTES, ROUTES


@dataclass
class RouteState:
    """The slice of Resident state that bears on route choice (all derived)."""

    active_threads: int = 0
    dormant_threads: int = 0
    new_since_last_wake: int = 0
    total_events: int = 0
    has_recent_user_message: bool = False
    topic_concentration: float = 0.0
    has_exploration_recently: bool = False
    route_history: list[str] = field(default_factory=list)  # newest first
    recent_topics: list[str] = field(default_factory=list)
    time_since_meaningful_s: float = 0.0
    no_op_ratio: float = 0.0
    # Phase-4: a dormant self-thread is an eligible revival candidate in a lull.
    self_revival_available: bool = False
    # the size of the gentle revisit-route lift this wake (0 when no revival bias
    # applies); read from the revival params so it can be swept (Phase-4.1).
    self_revival_route_lift: float = 0.0


class RoutePolicy:
    def __init__(
        self,
        *,
        exploration: float = 0.08,
        fatigue_window: int = 6,
        departure_gain: float = 0.5,
    ):
        self.exploration = float(exploration)
        self.fatigue_window = int(fatigue_window)
        self.departure_gain = float(departure_gain)

    # ------------------------------------------------------------- scoring

    def base_scores(self, s: RouteState) -> dict[str, float]:
        scores = {r: 0.0 for r in ROUTES}
        # Continuing an active thread is strongly favoured — UNLESS nothing new
        # has happened since the last wake, in which case re-chewing the same
        # thread with no new material should yield to rest (anti-pinning: an
        # active thread must not pin the mind indefinitely).
        active_bonus = 0.45 if s.active_threads > 0 else 0.0
        if s.active_threads > 0 and s.new_since_last_wake == 0:
            active_bonus *= 0.5
        scores["continuity"] = 0.15 + active_bonus
        scores["revisit"] = 0.12 + min(0.40, 0.20 * s.dormant_threads)
        # one topic dominates recent life -> gain rises with concentration
        scores["distant"] = 0.08 + 0.50 * max(0.0, s.topic_concentration - 0.5)
        scores["serendipity"] = 0.08 + (0.35 if s.has_exploration_recently else 0.0)
        scores["personal"] = 0.12 + (0.50 if s.has_recent_user_message else 0.0)
        scores["self"] = 0.08 + (0.25 if s.total_events < 20 else 0.0)
        if s.new_since_last_wake == 0:
            scores["rest"] = 0.50
        else:
            scores["rest"] = 0.12
            # little meaningful activity for a long time ALSO favours resting.
            # (0 = fresh activity just happened -> do NOT rest-bonus.)
            if s.time_since_meaningful_s > 86400.0:
                scores["rest"] = max(scores["rest"], 0.35)
        scores["world"] = 0.0  # unbound in v0.1
        # Phase-4: a sleeping self-thought worth resurfacing gently favours the
        # "revisit" route — state-gated (only when a candidate is eligible) and
        # gentle/bounded (a small additive lift, never a forced pick).
        if s.self_revival_available:
            scores["revisit"] += s.self_revival_route_lift
        return scores

    def fatigue(self, s: RouteState) -> dict[str, float]:
        """Repetition-fatigue adjustments (see module docstring)."""
        adj = {r: 0.0 for r in ROUTES}
        hist = s.route_history[: self.fatigue_window]
        if len(hist) >= 2:
            counts = Counter(hist)
            top_route, top_n = counts.most_common(1)[0]
            dominance = top_n / len(hist)
            if dominance > 0.5:
                adj[top_route] = -0.5 * dominance
                for r in DEPARTURE_ROUTES:
                    if r != top_route:
                        adj[r] += self.departure_gain * dominance
        if s.recent_topics:
            tcounts = Counter(s.recent_topics)
            _top_topic, top_tn = tcounts.most_common(1)[0]
            tdom = top_tn / len(s.recent_topics)
            if tdom > 0.6:
                adj["distant"] += 0.40 * tdom
                adj["serendipity"] += 0.20 * tdom
        return adj

    # -------------------------------------------------------------- select

    def select(
        self, s: RouteState, rng: random.Random
    ) -> tuple[str, dict[str, float], str]:
        """Pick a route. Returns (route, score_vector, human_reason)."""
        scores = self.base_scores(s)
        for r in ROUTES:
            scores[r] += self.fatigue(s)[r]
        if self.exploration > 0:
            for r in ROUTES:
                scores[r] += (rng.random() * 2 - 1) * self.exploration
        # world is never auto-chosen (unbound in v0.1)
        scores["world"] = min(scores["world"], -1.0)
        route = max(ROUTES, key=lambda r: scores[r])
        return route, {r: round(scores[r], 4) for r in ROUTES}, self._reason(s, route)

    @staticmethod
    def _reason(s: RouteState, route: str) -> str:
        if route == "rest":
            if s.new_since_last_wake == 0:
                return "nothing new since last wake; resting is legitimate"
            return "little worth persisting this cycle; resting"
        if route == "continuity":
            return "an active thread pulls"
        if route == "revisit":
            return "old unfinished material resurfaced (fatigue-favoured)"
        if route == "distant":
            return "one topic dominates; sampling away from it"
        if route == "serendipity":
            return "an under-visited corner of memory to meet"
        if route == "personal":
            return "the user's timeline just moved"
        if route == "self":
            return "history is short; observing my own runtime"
        return f"route {route} (state-driven)"
