"""Dormant self-thread revival — a state-gated, gentle recall bias (ADR-0006).

The Phase-3 14-day run showed **self-origin plateaus at ~0.23**: self-threads
form early, then go dormant and are never re-engaged, so a thought of the mind's
own rarely *survives the day and re-influences itself*. This module is the
mechanism under study in Phase 4.

During a quiet lull, a dormant self-thread that has **real history** and has
**slept long enough** gets a *gently raised* chance of being the thread the mind
revisits. It is, by design:

- **STATE-GATED** — only when there has been no recent user input, few threads
  are active, and no strong external stimulus has happened recently;
- **GENTLE + PROBABILISTIC** — a bounded, seeded-probability preference
  (``REVIVAL_STRENGTH`` < 1). The default (most-recent) thread still wins with
  probability ``1 - REVIVAL_STRENGTH``; nothing is ever *forced*;
- **NOT A QUOTA** — no fixed share of wakes is reserved for self material;
- **NOT provider-based** — which *thread* is recalled is decided here from real
  store state; the model (fake or real) only grounds the resulting *text* from
  that thread's real memories. Independence emerges from recall, not from the
  model being told to be independent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .mental_state import ThreadView
from .origin import thread_origin

# --- gate thresholds (virtual time; all gentle) ---------------------------
SILENCE_S = 12 * 3600          # (1) no user input for >= this long
LOW_STIMULUS_S = 6 * 3600      # (3) no strong external stimulus (user msg / experience) for >= this long
MAX_ACTIVE = 2                 # (2) at most this many active threads
MIN_DORMANT_S = 12 * 3600      # candidate self-thread has been dormant >= this long
MIN_SELF_EVENTS = 1            # candidate must carry >= this many durable mental events

# the gentle recall preference (a probability, never a guarantee)
REVIVAL_STRENGTH = 0.35

# Phase 5 (ADR-0007): when the circadian state machine has *recognized* a
# genuine quiet lull (QUIET / DROWSY / SLEEP), the mind "knows" it is a quiet
# period, so the user-silence bar for the gate drops to this working-region
# floor (~6 h, from the Phase-4.1 sensitivity study) instead of the full
# default. This is how night / long-silence raises the *opportunity* that a
# dormant self-thread candidate enters recall — it never forces which thread is
# picked (that stays the probabilistic ``REVIVAL_STRENGTH`` below) and never
# enlarges the route lift. Outside a recognized quiet (or with circadian off)
# the full ``silence_s`` applies, so the default (A/B control) is unchanged.
QUIET_GATE_SILENCE_S = 6 * 3600

# the gentle, state-gated route lift: when a dormant self-thread is an eligible
# candidate in a lull, "revisit" (the route that engages dormant threads) is
# made a little more attractive — enough to sometimes beat `rest`, never enough
# to dominate. This is the primary lever: it raises the probability the mind
# *engages* the sleeping self-thought at all (the thread-level REVIVAL_STRENGTH
# only decides which dormant thread a revisit lands on).
SELF_REVIVAL_ROUTE_LIFT = 0.15

# event types that count as the thread's "real history" (it was actually thought on)
_HISTORY_TYPES = ("thought.created", "memory.consolidated", "experience.created")
# event types that count as a "strong external stimulus"
_EXTERNAL_TYPES = ("conversation.user_message", "experience.created")


@dataclass(frozen=True)
class RevivalParams:
    """The tunable knobs of the dormant self-thread revival bias.

    Defaults are the values chosen in Phase 4 (see ADR-0006). The Phase-4.1
    sensitivity study varies a *subset* of these (``min_dormant_s``,
    ``silence_s``, ``max_active`` — and later ``route_lift``) to map the
    mechanism's working region; it never chases a target ratio. Every field is
    a gentle, bounded bias — see the module docstring for the non-quota /
    non-forced / non-provider guarantees that hold for *any* combination.
    (The on/off switch itself stays ``MindLoop.self_revival``; these are only
    the *shape* of the bias when it is on.)

    Active Living Experiment (ADR-0014/0015): two experiment-only fields,
    both defaulting to the sealed behaviour —

    - ``gate_mode="dormancy"`` (S1): the gate's *time bars* (user silence +
      external-stimulus) are replaced by the candidate's own dormancy — the
      self-thread is due when IT has slept long enough, not when the
      environment has been quiet long enough. The active-threads cap stays.
    - ``opportunity_clock_s`` (S2): an independent low-frequency offering
      clock per unresolved self-thread — a candidate may be *offered* at most
      once per interval (12/18/24 h). Opportunity only: the route policy and
      ``thread_strength`` still decide, and a noop stays legitimate.
    """

    # gate: a quiet lull must hold before any candidate is even considered
    silence_s: float = SILENCE_S          # (1) no user input for >= this long
    low_stimulus_s: float = LOW_STIMULUS_S  # (3) no strong external stimulus for >= this long
    max_active: int = MAX_ACTIVE          # (2) at most this many active threads
    # candidate: the dormant self-thread must have slept long enough + be real
    min_dormant_s: float = MIN_DORMANT_S
    min_self_events: int = MIN_SELF_EVENTS
    # the two gentle levers
    route_lift: float = SELF_REVIVAL_ROUTE_LIFT  # bounded nudge to the revisit route
    thread_strength: float = REVIVAL_STRENGTH    # probabilistic thread preference
    # experiment-only (S1/S2 of the Active Living Experiment)
    gate_mode: str = "sealed"                     # "sealed" | "dormancy" (S1)
    opportunity_clock_s: float | None = None      # S2: per-thread offering clock


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _seconds_since(store, type_prefixes: tuple[str, ...], now_iso: str | None) -> float | None:
    """Seconds since the newest event whose type is in ``type_prefixes``;
    ``None`` if no such event exists (treated as "forever ago" by the caller)."""
    now = _parse_iso(now_iso)
    newest: datetime | None = None
    for prefix in type_prefixes:
        for e in store.list(500, type_prefix=prefix, order="desc"):
            t = _parse_iso(e.created_at)
            if t is not None and (newest is None or t > newest):
                newest = t
    if newest is None:
        return None
    if now is None:
        return 0.0
    return max(0.0, (now - newest).total_seconds())


def revival_gate(
    store,
    *,
    n_active: int,
    now_iso: str | None,
    silence_s: float = SILENCE_S,
    low_stimulus_s: float = LOW_STIMULUS_S,
    max_active: int = MAX_ACTIVE,
    in_quiet_state: bool = False,
    mode: str = "sealed",
) -> bool:
    """True iff the mind is in a quiet lull suitable for a self-thread revival:
    no recent user input, no strong external stimulus, and few active threads.
    (A strong external stimulus is exactly a recent user message / experience.)

    ``in_quiet_state`` (Phase 5): when the circadian state machine has
    *recognized* a genuine quiet (QUIET / DROWSY / SLEEP), the user-silence bar
    drops to the working-region floor (``QUIET_GATE_SILENCE_S``, ~6 h) — the
    circadian confirms the lull is real, so the gate need not re-require the
    full default silence. This raises the *opportunity* that a dormant
    self-thread candidate enters recall; it is not a forced selection and does
    not change the route lift. Default ``False`` keeps the A/B control
    byte-identical (the full ``silence_s`` applies).

    ``mode="dormancy"`` (Active Living Experiment, S1): the environment time
    bars are *replaced* by the candidate's own dormancy (checked in
    :func:`qualifying_dormant_self_thread`) — the self-thread is due when IT
    has slept long enough, not when the environment has been quiet long
    enough. The active-threads cap still applies."""
    if n_active > max_active:
        return False
    if mode == "dormancy":
        return True
    eff_silence = min(silence_s, QUIET_GATE_SILENCE_S) if in_quiet_state else silence_s
    since_user = _seconds_since(store, ("conversation.user_message",), now_iso)
    if since_user is not None and since_user < eff_silence:
        return False
    since_ext = _seconds_since(store, _EXTERNAL_TYPES, now_iso)
    if since_ext is not None and since_ext < low_stimulus_s:
        return False
    return True


def _last_offering(store, thread_id: str) -> datetime | None:
    """The last time this self-thread was OFFERED as a revival candidate
    (recorded on wake.route_selected)."""
    newest: datetime | None = None
    for e in store.list(500, type_prefix="wake.route_selected", order="desc"):
        if e.content.get("self_revival_candidate_thread") != thread_id:
            continue
        t = _parse_iso(e.created_at)
        if t is not None and (newest is None or t > newest):
            newest = t
            break
    return newest


def offering_allowed(store, thread_id: str, now_iso: str | None, clock_s: float) -> bool:
    """S2's independent low-frequency opportunity clock: an unresolved
    self-thread may be OFFERED at most once per ``clock_s``. Opportunity
    only — the route policy, ``thread_strength`` and honest noops still
    decide everything downstream."""
    last = _last_offering(store, thread_id)
    if last is None:
        return True
    now = _parse_iso(now_iso)
    if now is None:
        return False
    return (now - last).total_seconds() >= clock_s


def _has_real_history(store, thread: ThreadView, min_events: int) -> bool:
    """The thread was actually thought on (>= ``min_events`` durable mental events)."""
    n = 0
    for eid in thread.event_ids:
        e = store.get(eid)
        if e is not None and e.type in _HISTORY_TYPES:
            n += 1
        if n >= min_events:
            return True
    return False


def qualifying_dormant_self_thread(
    state,
    store,
    now_iso: str | None,
    *,
    min_dormant_s: float = MIN_DORMANT_S,
    min_self_events: int = MIN_SELF_EVENTS,
) -> ThreadView | None:
    """The best dormant self-thread to revive: origin ``self``, dormant long
    enough, with real history. Ties go to the most-forgotten (oldest last
    activity) — deterministic."""
    now = _parse_iso(now_iso)
    best: ThreadView | None = None
    for t in state.dormant_threads:
        if thread_origin(store, t.thread_id) != "self":
            continue
        if not _has_real_history(store, t, min_self_events):
            continue
        last = _parse_iso(t.last_event_at)
        if now is None or last is None:
            continue
        if (now - last).total_seconds() < min_dormant_s:
            continue
        if best is None or (t.last_event_at < best.last_event_at):
            best = t
    return best
