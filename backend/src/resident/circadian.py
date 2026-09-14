"""Circadian rhythm: a state machine for activity and quiet (ADR-0007).

Phase 5. The direct follow-through to the Phase-4.1 sensitivity study, which
established the *working region* of the dormant self-thread revival bias
(dormancy 6–12 h, user silence ≈6 h, `MAX_ACTIVE` loose) and showed that the
**structure of the lulls** — how many real, quiet, no-user wake slots actually
exist — is the binding constraint on whether a sleeping self-thought resurfaces
"naturally" versus being pushed in. A fixed-interval scheduler produces uniform
wake tiling (no real night, no genuine quiet), so the revival gate (which needs
a long quiet lull) barely opens. A *circadian* scheduler gives the mind a rhythm
of activity and silence: it engages when the user is present, quiets down as
silence grows, drowses, sleeps (with real consolidation), and re-opens. Those
quiet states are exactly where the dormant self-thread revival gets a genuine
opportunity to enter recall — and, per the Phase-4/4.1 constraint, the circadian
only *raises that opportunity* (it never forces a thread and never enlarges the
route lift as the primary lever; see :mod:`resident.self_revival` and ADR-0006).

The state machine is **evidence-driven, not a fixed-clock cron**: the local
clock is *one* of several inputs (a bounded bias), and the decision also reads
time-since-user-activity, time-since-meaningful-activity, recent wake density,
and the current active-thread state. Every state change is recorded as a
``circadian.state_changed`` event carrying its ``from``/``to`` states and the
evidence that justified it, so the rhythm is provenance-complete and reproducible.

States
------
- ``ACTIVE``        — engaged: the user (or a recent meaningful self-activity)
  happened lately; active threads are being worked.
- ``QUIET``         — user silence has grown past a short threshold, but the mind
  is still awake and can think (a wake here is often a legitimate no-op).
- ``DROWSY``        — longer silence, drifting toward the night window, few active
  threads; the mind is winding down.
- ``SLEEP``         — deep sleep; a real ``SleepEngine`` consolidation cycle runs
  (and a sleep may legitimately do nothing — no forced thought, ADR-0004).
- ``CONSOLIDATING`` — the in-progress sub-state while the ``SleepEngine`` actually
  runs its cycle within a sleep episode.
- ``WAKE``          — the re-opening from sleep: the mind comes back, its threads
  and mental state are **not** cleared, and it re-enters ACTIVE/QUIET.

Determinism: given a fixed store + a reference ``now`` the decision is a pure
function (no wall-clock reads, no randomness here), so a seeded virtual timeline
is reproducible. Randomness belongs to the ``MindLoop``/``SleepEngine`` it
drives, never to the state decision itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .event_store import EventStore
from .mental_state import MentalState
from .models import EventCreate

# ---------------------------------------------------------------- states

ACTIVE = "active"
QUIET = "quiet"
DROWSY = "drowsy"
SLEEP = "sleep"
CONSOLIDATING = "consolidating"
WAKE = "wake"

#: the six circadian states
CIRCADIAN_STATES: frozenset[str] = frozenset(
    {ACTIVE, QUIET, DROWSY, SLEEP, CONSOLIDATING, WAKE}
)
#: states that represent a genuine quiet lull (the mind is not actively engaged
#: with the user). In these, the circadian may raise the dormant self-thread
#: revival *opportunity* (see MindLoop / self_revival) — never force a selection.
QUIET_STATES: frozenset[str] = frozenset({QUIET, DROWSY, SLEEP, CONSOLIDATING})
#: the "asleep" states (a consolidation is a sub-state of sleep, not a re-open)
ASLEEP_STATES: frozenset[str] = frozenset({SLEEP, CONSOLIDATING})

#: the provenance record of a state transition (from → to, with the evidence)
CIRCADIAN_STATE_CHANGED = "circadian.state_changed"

#: a "no such event exists" sentinel for the silence reads — treated as very
#: long silence (a mind with no user activity at all is, by definition, silent).
_NO_ACTIVITY_S = 30 * 86400.0


# ---------------------------------------------------------------- helpers


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _hours(s: float) -> float:
    """Seconds → hours, rounded for human-readable evidence."""
    if s is None or s >= _NO_ACTIVITY_S:
        return -1.0  # marker: no such activity recorded
    return round(s / 3600.0, 2)


def _in_window(hour: float, window: tuple[float, float]) -> bool:
    """True if ``hour`` is inside a possibly-wrapping [start, end) hour window."""
    start, end = window
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


# ---------------------------------------------------------------- evidence


@dataclass(frozen=True)
class CircadianEvidence:
    """The slice of the store + reference clock that bears on the circadian
    decision. Every field is a *derived* read (virtual or wall time); none is a
    stored mutable state. ``current`` is the state the machine is holding — it
    feeds the transition (in particular sleep stickiness).

    All ``since_*`` fields are in seconds; a value ``>= _NO_ACTIVITY_S`` means
    "no such activity has ever been recorded" (treated as maximal silence).
    """

    now: str                      # the reference "now" (iso) — sim virtual or wall
    local_hour: float             # 0..24 hour-of-day in the reference zone
    since_user_s: float           # since the last conversation.user_message
    since_meaningful_s: float     # since the last non-runtime, non-system event
    recent_wake_count: int        # wake.started in the recent (density) window
    n_active_threads: int         # current active-thread count
    current: str                  # the state the machine is holding
    since_external_s: float = _NO_ACTIVITY_S     # since last external stimulus (user/experience)
    since_consolidation_s: float = _NO_ACTIVITY_S  # since the last sleep.completed


@dataclass
class Decision:
    """The outcome of :meth:`CircadianStateMachine.assess`."""

    state: str
    changed: bool
    from_state: str | None
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- params


@dataclass(frozen=True)
class CircadianParams:
    """Thresholds of the circadian state machine (virtual time).

    These are **evidence-based defaults**, not dials tuned toward a ratio
    (the owner constraint). Two of them are set directly from the Phase-4.1
    sensitivity study's working region: the *sleep* threshold (≈6 h of user
    silence) and — in :mod:`resident.self_revival` — the *revival opportunity*
    floor (≈6 h). The local clock enters only as a **bounded bias**
    (``night_bias``); it is one of five signals, never the sole decider, so the
    machine is not a fixed-clock cron. See ADR-0007 for the full rationale.
    """

    # how "recent" activity holds the mind ACTIVE
    active_user_s: float = 30 * 60.0        # user activity within this -> ACTIVE
    active_meaningful_s: float = 60 * 60.0  # meaningful self-activity within this -> ACTIVE
    # user-silence thresholds that drive the descent (before clock bias)
    quiet_after_s: float = 2 * 3600.0
    drowsy_after_s: float = 4 * 3600.0
    sleep_after_s: float = 6 * 3600.0       # ~ the Phase-4.1 "silence ≈6 h" working region
    # the local-clock bias (bounded; NOT the decider)
    night_window: tuple[float, float] = (22.0, 6.0)  # [start, end) hours biasing toward sleep
    night_bias: float = 0.20
    wake_window: tuple[float, float] = (6.0, 9.0)    # morning hours that enable a re-open
    # recent-wake-density ("tiredness")
    wake_density_window_s: float = 6 * 3600.0
    wake_density_full: float = 6.0         # wakes in the window that read as "fully tired"
    # active-thread state (engagement vs. readiness to sleep)
    max_active_for_sleep: int = 2          # > this many active threads -> too engaged to fully sleep
    # sleep-episode mechanics
    consolidation_interval_s: float = 2 * 3600.0  # how often a sleep episode runs a SleepEngine cycle
    # wake cadence within each awake state (structural: how often the mind wakes there;
    # a *new user message* always wakes promptly regardless of these)
    active_wake_interval_s: float = 1 * 3600.0
    quiet_wake_interval_s: float = 3 * 3600.0
    drowsy_wake_interval_s: float = 4 * 3600.0


# ---------------------------------------------------------------- machine


class CircadianStateMachine:
    """Owns the current circadian state and records every transition.

    The machine is the single writer of ``circadian.state_changed``. It keeps
    the current state in memory (a fast path) but also **reconstructs** it from
    the log on construction, so a live process that restarts picks up where it
    left off (the event log remains the source of truth — ADR-0001).
    """

    def __init__(
        self,
        store: EventStore,
        *,
        params: CircadianParams | None = None,
        initial_state: str | None = None,
    ):
        self.store = store
        self.params = params or CircadianParams()
        # current state: explicit > reconstructed-from-log > ACTIVE (fresh mind)
        if initial_state is not None:
            if initial_state not in CIRCADIAN_STATES:
                raise ValueError(f"invalid circadian state: {initial_state!r}")
            self._state = initial_state
        else:
            last = store.list(1, type_prefix=CIRCADIAN_STATE_CHANGED, order="desc")
            self._state = (last[0].content.get("to") if last and last[0].content.get("to") in CIRCADIAN_STATES
                           else ACTIVE)

    # -------------------------------------------------------------- current

    @property
    def state(self) -> str:
        return self._state

    def current_state(self) -> str:
        """The state to report externally (for telemetry / prompt context)."""
        return self._state

    # -------------------------------------------------------------- evidence

    @staticmethod
    def assemble_evidence(store: EventStore, now_iso: str, current: str) -> CircadianEvidence:
        """Derive the decision evidence from the store + reference ``now``."""
        p = CircadianParams()  # thresholds only; the machine's own params are applied in decide
        now = _parse_iso(now_iso)
        hour = now.hour + now.minute / 60.0 if now else 12.0

        def _since(type_prefixes: tuple[str, ...]) -> float:
            latest = None
            for prefix in type_prefixes:
                for e in store.list(200, type_prefix=prefix, order="desc"):
                    t = _parse_iso(e.created_at)
                    if t is not None and (latest is None or t > latest):
                        latest = t
            if latest is None or now is None:
                return _NO_ACTIVITY_S
            return max(0.0, (now - latest).total_seconds())

        since_user = _since(("conversation.user_message",))
        since_meaningful = _since_meaningful(store, now_iso)
        since_external = _since(("conversation.user_message", "experience.created"))
        since_consolidation = _since(("sleep.completed",))

        # recent wake density: wake.started within the density window
        recent_wakes = 0
        if now is not None:
            window_start = (now.timestamp() - p.wake_density_window_s)
            for e in store.list(500, type_prefix="wake.started", order="desc"):
                t = _parse_iso(e.created_at)
                if t is None:
                    continue
                if t.timestamp() < window_start:
                    break
                recent_wakes += 1

        n_active = len(MentalState.from_store(store).active_threads)
        return CircadianEvidence(
            now=now_iso,
            local_hour=hour,
            since_user_s=since_user,
            since_meaningful_s=since_meaningful,
            since_external_s=since_external,
            recent_wake_count=recent_wakes,
            n_active_threads=n_active,
            current=current,
            since_consolidation_s=since_consolidation,
        )

    # --------------------------------------------------------------- decide

    def decide(self, ev: CircadianEvidence) -> tuple[str, list[str]]:
        """Pick the target state from the five evidence signals + the current
        state. Pure: same evidence → same state. Returns (target, reasons).

        The descent is driven by **user (external) silence**: the local clock
        (night window), recent **wake density** (tiredness), and the
        **active-thread state** (engagement) enter as bounded corrections. A
        recent **meaningful** self-activity holds the mind awake for a short
        grace window, but it must not veto a needed rest — a mind left alone by
        the user should sleep even while it keeps having private thoughts (that
        is precisely what sleep consolidates). Because each of the five signals
        moves the outcome, the decision is not a fixed-clock cron: the same
        clock at different evidence reads a different state.
        """
        p = self.params
        cur = ev.current
        reasons: list[str] = []

        # ---------- asleep: stickiness. Stay asleep unless woken. ----------
        if cur in ASLEEP_STATES:
            woke = self._wake_trigger(ev)
            if woke:
                return WAKE, woke
            return SLEEP, ["asleep; silence + schedule hold (no wake trigger)"]

        # ---------- freshly re-opened: the mind just woke and is active; it
        # descends from here as user silence grows (a just-got-up mind is up).
        if cur == WAKE:
            return ACTIVE, ["re-opened; freshly awake"]

        # ---------- user present -> active ----------------------------------
        if ev.since_user_s < p.active_user_s:
            return ACTIVE, [f"user active ({_hours(ev.since_user_s)} h ago)"]

        # ---------- the morning re-open --------------------------------------
        # A mind that is up in the morning window is *up and about* — the
        # local-clock bias for the day's start. It does not force any particular
        # thought; it keeps the mind out of deep sleep through the morning so a
        # fresh re-open is not instantly re-slept by the accumulated overnight
        # silence (and the morning window does not re-wake it on every beat).
        if _in_window(ev.local_hour, p.wake_window):
            return ACTIVE, [f"morning window ({ev.local_hour:.0f} h): up and about"]

        # ---------- the user-silence descent (bounded clock/density/
        # thread-state corrections) --------------------------------------------
        relax = self._bias(ev)  # 0..~0.45: night + tiredness ease the thresholds
        quiet_t = p.quiet_after_s * (1.0 - relax)
        drowsy_t = p.drowsy_after_s * (1.0 - relax)
        sleep_t = p.sleep_after_s * (1.0 - relax)
        # a busy mind RESISTS sleep: each active thread beyond the baseline
        # lengthens the silence required to fully sleep. This is a bounded
        # correction (the mind still sleeps when sufficiently silent / at night),
        # never a permanent veto — otherwise a mind with a few long-lived active
        # threads would never rest, no matter how long the user is gone.
        thread_resist = max(0, ev.n_active_threads - p.max_active_for_sleep)
        sleep_t = sleep_t * (1.0 + 0.25 * thread_resist)

        if ev.since_user_s >= sleep_t:
            return SLEEP, self._descent_reasons(ev, "user silence past the (bias-adjusted) sleep threshold")
        # a fresh thought (mid-stream) holds the mind awake while it is not yet
        # time to sleep — the grace window, not a stay-awake license
        if ev.since_meaningful_s < p.active_meaningful_s:
            return ACTIVE, [f"recent self-activity ({_hours(ev.since_meaningful_s)} h ago)"]
        if ev.since_user_s >= drowsy_t:
            return DROWSY, self._descent_reasons(ev, "user silence past the drowsy threshold")
        if ev.since_user_s >= quiet_t:
            return QUIET, self._descent_reasons(ev, "user silence past the quiet threshold")
        return ACTIVE, ["user silence recent; mind awake"]

    def _bias(self, ev: CircadianEvidence) -> float:
        """A bounded correction (0..~0.45) that eases the descent thresholds:
        the night window + recent wake density make the mind *easier* to put to
        sleep. This is the local clock's role — a *bias*, never the decider.
        (A busy mind's resistance to *deep* sleep is handled separately by the
        sleep-threshold scaling in :meth:`decide`, not by cancelling this bias.)"""
        p = self.params
        night = _in_window(ev.local_hour, p.night_window)
        tired = _clamp01(ev.recent_wake_count / max(1e-9, p.wake_density_full))
        bias = (p.night_bias if night else 0.0) + 0.10 * tired
        return min(0.45, bias)

    def _descent_reasons(self, ev: CircadianEvidence, lead: str) -> list[str]:
        p = self.params
        r = [lead, f"user silent {_hours(ev.since_user_s)} h (self {_hours(ev.since_meaningful_s)} h)"]
        if _in_window(ev.local_hour, p.night_window):
            r.append("night window")
        if ev.recent_wake_count >= p.wake_density_full * 0.5:
            r.append(f"high recent wake density ({ev.recent_wake_count} in window)")
        r.append(f"{ev.n_active_threads} active thread(s)")
        return r

    def _wake_trigger(self, ev: CircadianEvidence) -> list[str] | None:
        """A wake from sleep is allowed by: the user returning, a resurge of
        *external* activity (a user message or experience — not the mind's own
        sleep/consolidation work, which must not re-open it), or the local clock
        entering the morning wake window (a *bias* that enables a re-open — it
        does not force any particular thought). None ⇒ stay asleep.

        The external-only rule is what makes a night *stable*: a sleep
        consolidation may legitimately produce a consolidated memory or dream,
        and that must not jolt the mind awake — only a genuine external stimulus
        or the morning re-open does."""
        p = self.params
        if ev.since_user_s < p.active_user_s:
            return [f"user returned ({_hours(ev.since_user_s)} h ago)"]
        if ev.since_external_s < p.active_meaningful_s:
            return [f"external activity resumed ({_hours(ev.since_external_s)} h ago)"]
        if _in_window(ev.local_hour, p.wake_window):
            return [f"morning wake window ({ev.local_hour:.0f} h)"]
        return None

    # --------------------------------------------------------------- assess

    def assess(self, evidence: CircadianEvidence) -> Decision:
        """Decide and, if the state changed, record a ``circadian.state_changed``
        event (from → to, with the evidence). Returns the :class:`Decision`."""
        target, reasons = self.decide(evidence)
        changed = target != self._state
        frm = None if not changed else self._state
        if changed:
            self._record_change(frm, target, evidence, reasons)
            self._state = target
        return Decision(state=target, changed=changed, from_state=frm, reasons=reasons)

    def _record_change(
        self, frm: str | None, to: str, ev: CircadianEvidence, reasons: list[str]
    ) -> None:
        p = self.params
        # link to the events that evidence the decision (best-effort, must exist)
        causes: list[str] = []
        latest_user = self.store.list(1, type_prefix="conversation.user_message", order="desc")
        if latest_user:
            causes.append(latest_user[0].id)
        self.store.append(
            EventCreate(
                type=CIRCADIAN_STATE_CHANGED,
                visibility="system",
                content={
                    "from": frm,
                    "to": to,
                    "reasons": reasons,
                    # the five evidence signals (provenance: why the transition)
                    "evidence": {
                        "local_hour": round(ev.local_hour, 2),
                        "user_silence_h": _hours(ev.since_user_s),
                        "meaningful_silence_h": _hours(ev.since_meaningful_s),
                        "recent_wake_count": ev.recent_wake_count,
                        "n_active_threads": ev.n_active_threads,
                        "since_consolidation_h": _hours(ev.since_consolidation_s),
                    },
                    "night_window": list(p.night_window),
                    "params": {
                        "quiet_after_h": p.quiet_after_s / 3600,
                        "drowsy_after_h": p.drowsy_after_s / 3600,
                        "sleep_after_h": p.sleep_after_s / 3600,
                        "night_bias": p.night_bias,
                    },
                },
                links={"caused_by": causes} if causes else {},
                provenance={"source": "circadian"},
            )
        )

    # ------------------------------------------- consolidation sub-state

    def begin_consolidation(self, evidence: CircadianEvidence) -> Decision:
        """SLEEP → CONSOLIDATING: the SleepEngine is about to run a cycle."""
        frm = self._state
        self._state = CONSOLIDATING
        reasons = ["consolidation cycle starting (sleep)"]
        if frm != CONSOLIDATING:
            self._record_change(frm, CONSOLIDATING, evidence, reasons)
        return Decision(state=CONSOLIDATING, changed=frm != CONSOLIDATING,
                        from_state=frm, reasons=reasons)

    def end_consolidation(self, evidence: CircadianEvidence) -> Decision:
        """CONSOLIDATING → SLEEP: the cycle finished (it may have been a no-op)."""
        frm = self._state
        self._state = SLEEP
        reasons = ["consolidation cycle finished"]
        if frm != SLEEP:
            self._record_change(frm, SLEEP, evidence, reasons)
        return Decision(state=SLEEP, changed=frm != SLEEP, from_state=frm, reasons=reasons)


# ------------------------------------------------------------------ module


def _since_meaningful(store: EventStore, now_iso: str) -> float:
    """Seconds since the last *meaningful* event: a non-system event that is not
    one of the runtime bookkeeping families (wake./sleep./reentry./telemetry.).
    A thought, an experience, or a user message all count; the mind's own
    wake/sleep bookkeeping does not (so an idle mind *can* drift to sleep)."""
    now = _parse_iso(now_iso)
    if now is None:
        return _NO_ACTIVITY_S
    runtime_prefixes = ("wake.", "sleep.", "reentry.", "telemetry.", "circadian.")
    for e in store.list(300, order="desc"):
        if e.visibility == "system":
            continue
        if e.type.startswith(runtime_prefixes):
            continue
        t = _parse_iso(e.created_at)
        if t is not None:
            return max(0.0, (now - t).total_seconds())
    return _NO_ACTIVITY_S


# ---------------------------------------------------------------- orchestrator


class CircadianOrchestrator:
    """Drives the real engines (``MindLoop`` / ``SleepEngine`` / ``ReentryEngine``)
    on a circadian rhythm. This is what turns the state machine into a *life*:
    each beat it assembles evidence from the store at a reference ``now``, asks
    the state machine for the target state, and acts on the resulting state —
    waking the mind (awake states / re-opening from sleep) or running a real
    consolidation cycle (SLEEP).

    The engine objects are duck-typed (not imported here) so this module stays
    free of a circular import with :mod:`resident.mind_loop`; the sim passes in
    the real engines and the live scheduler does the same.

    **Not a cron.** The beat *samples* time (a cadence), but *what happens* —
    wake, consolidate, or nothing — is decided by the evidence-driven state
    machine, not by "tick every N seconds and do X". In SLEEP the mind takes no
    wakes; in awake states it wakes on a per-state cadence and always promptly
    when a new user message arrives. A sleep runs the real ``SleepEngine``, which
    may legitimately do nothing (no forced thought — ADR-0004).
    """

    def __init__(
        self,
        store: EventStore,
        mind,          # MindLoop
        sleep_engine,  # SleepEngine
        reentry=None,  # ReentryEngine (optional)
        machine: CircadianStateMachine | None = None,
        params: CircadianParams | None = None,
        world_engine=None,  # Phase 6: WorldWindowEngine (optional; None = no world)
    ):
        self.store = store
        self.mind = mind
        self.sleep_engine = sleep_engine
        self.reentry = reentry
        self.machine = machine or CircadianStateMachine(store, params=params)
        self.params = params or self.machine.params
        self.world_engine = world_engine  # Phase 6 (ADR-0008): opens on awake beats
        self.beats = 0
        self.wakes = 0
        self.consolidations = 0
        self.state_trace: list[dict] = []

    # ------------------------------------------------------------------ beat

    async def beat(self, now_iso: str) -> dict:
        """Advance the circadian life one step at reference time ``now_iso``.

        The store's clock should already be at (or behind) ``now_iso`` so any
        event this beat appends carries the correct (virtual) timestamp.
        Returns a summary of what the beat did.
        """
        self.beats += 1
        ev = self.machine.assemble_evidence(self.store, now_iso, self.machine.state)
        decision = self.machine.assess(ev)
        state = decision.state
        action = "none"
        detail: dict = {}

        if state in ASLEEP_STATES:
            if self._consolidation_due(ev):
                self.machine.begin_consolidation(ev)
                sres = await self.sleep_engine.sleep_once("night")
                self.machine.end_consolidation(ev)
                self.consolidations += 1
                action = "consolidate"
                detail = {"sleep_result": sres["result"], "sleep_actions": sres["actions"]}
        elif state == WAKE:
            res = await self.mind.wake_once("circadian_wake", circadian_state=state)
            self._maybe_promote()
            self.wakes += 1
            action = "wake"
            detail = {"route": res["route"], "result": res["result"]}
        else:  # awake: ACTIVE / QUIET / DROWSY
            if self._wake_due(now_iso, state):
                res = await self.mind.wake_once("circadian", circadian_state=state)
                self._maybe_promote()
                self.wakes += 1
                action = "wake"
                detail = {"route": res["route"], "result": res["result"]}

        # Phase 6 (ADR-0008): the World Window opens on awake beats — a separate
        # mechanism (not a mind route), influenced by the circadian state but
        # independent of the mind's own wake decision. It runs whether or not the
        # mind woke this beat, and is suppressed entirely while asleep.
        if self.world_engine is not None and state not in ASLEEP_STATES:
            detail["world"] = self.world_engine.maybe_open(now_iso, circadian_state=state)

        self.state_trace.append({
            "t": now_iso, "state": state, "changed": decision.changed,
            "action": action,
        })
        return {"state": state, "changed": decision.changed, "action": action, **detail}

    # --------------------------------------------------------------- helpers

    def _maybe_promote(self) -> None:
        if self.reentry is not None:
            self.reentry.promote()

    def _wake_due(self, now_iso: str, state: str) -> bool:
        """In an awake state, wake when the per-state cadence has elapsed — or
        immediately when a user message is newer than the last wake (a returning
        user re-engages the mind promptly, producing the activity bursts)."""
        p = self.params
        interval = {
            ACTIVE: p.active_wake_interval_s,
            QUIET: p.quiet_wake_interval_s,
            DROWSY: p.drowsy_wake_interval_s,
        }.get(state, p.active_wake_interval_s)
        now = _parse_iso(now_iso)
        last_wake = self.store.list(1, type_prefix="wake.started", order="desc")
        last_wake_t = _parse_iso(last_wake[0].created_at) if last_wake else None
        if last_wake_t is None:
            return True  # first beat: take a wake
        # a fresh user message (newer than the last wake) always wakes
        last_user = self.store.list(1, type_prefix="conversation.user_message", order="desc")
        if last_user:
            ut = _parse_iso(last_user[0].created_at)
            if ut is not None and ut > last_wake_t:
                return True
        if now is None:
            return False
        return (now - last_wake_t).total_seconds() >= interval

    def _consolidation_due(self, ev: CircadianEvidence) -> bool:
        """Consolidate on the first beat of a sleep episode (never consolidated
        before) and then on the consolidation cadence."""
        if ev.since_consolidation_s >= _NO_ACTIVITY_S:
            return True
        return ev.since_consolidation_s >= self.params.consolidation_interval_s

    # -------------------------------------------------------------- run loop

    async def run_until(
        self,
        end_iso: str,
        step_seconds: float,
        start_iso: str | None = None,
    ) -> list[dict]:
        """Drive beats from ``start_iso`` (or the store's latest time) to
        ``end_iso`` in ``step_seconds`` increments. Returns the per-beat trace.
        (The caller owns the virtual clock; this only computes the step times.)"""
        start = _parse_iso(start_iso) if start_iso else _parse_iso(self.store.latest().created_at)
        if start is None:
            return []
        end = _parse_iso(end_iso)
        from datetime import timedelta
        beats: list[dict] = []
        t = start
        while t <= end:
            beats.append(await self.beat(t.isoformat()))
            t += timedelta(seconds=step_seconds)
        return beats
