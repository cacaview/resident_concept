"""Phase-4 dormant self-thread revival + self-dynamics telemetry.

The revival bias is, by design, STATE-GATED, GENTLE and PROBABILISTIC — and it
is the A/B control's on/off switch. These tests pin the contract:

- the gate opens only in a quiet lull (no recent user input, no strong external
  stimulus, few active threads) and is blocked by any of those being violated;
- a qualifying candidate is a DORMANT, self-originated thread with real history
  that has slept long enough — never a user thread, never a fresh thread, never
  an empty thread;
- the recall preference is a *probability*, never a forced pick (a seeded roll
  above ``REVIVAL_STRENGTH`` leaves the default thread in place);
- with the mechanism OFF (the default) the flag is never set and no revival is
  counted, whatever the state;
- the route lift, when a candidate is available, only nudges ``revisit``;
- the six self-dynamics metrics are correct derived reads (and the revival
  rate is a real 0..1 ratio with its raw counts exposed).
"""
import random
from datetime import datetime, timedelta, timezone

from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.mental_state import MentalState
from resident.models import EventCreate
from resident.origin import thread_origin
from resident.route_policy import RoutePolicy, RouteState
from resident.self_dynamics import (
    self_dynamics_profile,
    self_thread_revival_rate,
)
from resident.self_revival import (
    REVIVAL_STRENGTH,
    SELF_REVIVAL_ROUTE_LIFT,
    qualifying_dormant_self_thread,
    revival_gate,
)

BASE = datetime(2026, 9, 1, tzinfo=timezone.utc)


class HourClock:
    """Deterministic virtual clock: +1h per append (no wall clock in tests)."""

    def __init__(self, start=BASE):
        self._t = start

    def __call__(self):
        ts = self._t
        self._t += timedelta(hours=1)
        return ts


class ScriptedRng:
    """Returns a scripted first draw (the revival roll) then a constant, so
    the recall's downstream randomness is stable and the preference roll is
    fully controlled."""

    def __init__(self, first: float, fill: float = 0.5):
        self._first = first
        self._fill = fill
        self._used = False

    def random(self) -> float:
        if not self._used:
            self._used = True
            return self._first
        return self._fill


def make_store(tmp_path, name="r.sqlite3") -> EventStore:
    return EventStore(tmp_path / name, now_fn=HourClock())


def hours_later(store, hours: float) -> str:
    last = store.latest()
    base = datetime.fromisoformat(last.created_at) if last else BASE
    return (base + timedelta(hours=hours)).isoformat()


def created(store, tid, origin, title=""):
    return store.append(EventCreate(
        type="thread.created",
        content={"thread_id": tid, "title": title, "origin": origin}))


def activated(store, tid):
    store.append(EventCreate(type="thread.activated",
                             content={"thread_id": tid},
                             links={"thread_id": tid}))


def dormant(store, tid):
    store.append(EventCreate(type="thread.dormant",
                             content={"thread_id": tid},
                             links={"thread_id": tid}))


def thought_on(store, tid, text="一个想法。", origin="self_thread", related_to=()):
    links = {"thread_id": tid}
    if related_to:
        links["related_to"] = list(related_to)
    return store.append(EventCreate(
        type="thought.created", content={"text": text},
        links=links, metadata={"origin": origin}))


def filler(store, n):
    for i in range(n):
        store.append(EventCreate(type="thought.created",
                                 content={"text": f"填充{i}。"}))


# ------------------------------------------------------------------------ gate

def test_gate_opens_in_quiet_lull(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    # no user message, no external stimulus, few active threads -> lull
    assert revival_gate(s, n_active=1, now_iso=hours_later(s, 24)) is True


def test_gate_blocked_by_recent_user_message(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    dormant(s, "thr_s")
    s.append(EventCreate(type="conversation.user_message",
                         content={"text": "新消息"}))
    assert revival_gate(s, n_active=1, now_iso=hours_later(s, 1)) is False


def test_gate_blocked_by_too_many_active_threads(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    dormant(s, "thr_s")
    assert revival_gate(s, n_active=3, now_iso=hours_later(s, 48)) is False


def test_gate_blocked_by_recent_external_stimulus(tmp_path):
    # user message is old (silence OK) but a fresh experience breaks low-stimulus
    s = make_store(tmp_path)
    s.append(EventCreate(type="conversation.user_message",
                         content={"text": "旧消息"}))
    filler(s, 13)  # push the clock > 12h past the user message
    s.append(EventCreate(type="experience.created",
                         content={"text": "新经历"}))  # recent stimulus
    assert revival_gate(s, n_active=1, now_iso=hours_later(s, 1)) is False


# -------------------------------------------------------------------- candidate

def test_candidate_is_dormant_self_thread_with_history(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    activated(s, "thr_s")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    now = hours_later(s, 24)
    c = qualifying_dormant_self_thread(MentalState.from_store(s), s, now)
    assert c is not None and c.thread_id == "thr_s"


def test_candidate_rejects_user_origin_thread(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_u", "user")
    activated(s, "thr_u")
    thought_on(s, "thr_u", origin="user_old")
    dormant(s, "thr_u")
    assert qualifying_dormant_self_thread(MentalState.from_store(s), s,
                                          hours_later(s, 24)) is None


def test_candidate_rejects_fresh_self_thread(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    # only 1h dormant -> not "slept long enough"
    assert qualifying_dormant_self_thread(MentalState.from_store(s), s,
                                          hours_later(s, 1)) is None


def test_candidate_rejects_self_thread_without_history(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    dormant(s, "thr_s")  # never thought on -> no real history
    assert qualifying_dormant_self_thread(MentalState.from_store(s), s,
                                          hours_later(s, 24)) is None


def test_candidate_prefers_most_forgotten(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_old", "self")
    thought_on(s, "thr_old")
    dormant(s, "thr_old")
    created(s, "thr_new", "self")
    thought_on(s, "thr_new")
    dormant(s, "thr_new")
    c = qualifying_dormant_self_thread(MentalState.from_store(s), s,
                                       hours_later(s, 24))
    assert c is not None and c.thread_id == "thr_old"


# --------------------------------------------------- gentle, probabilistic, not forced

def _two_dormant(tmp_path):
    """A dormant SELF thread (older) + a dormant USER thread (newer, so it is
    the default ``_most_recent`` choice). The self thread is the candidate."""
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    created(s, "thr_u", "user")
    thought_on(s, "thr_u", origin="user_old")
    dormant(s, "thr_u")
    return s


def test_preference_rolls_below_strength_picks_candidate(tmp_path):
    s = _two_dormant(tmp_path)
    now = hours_later(s, 24)
    cand = qualifying_dormant_self_thread(MentalState.from_store(s), s, now)
    assert cand is not None and cand.thread_id == "thr_s"
    mind = MindLoop(s, rng=ScriptedRng(0.0))  # roll < REVIVAL_STRENGTH
    _, sel, _q = mind._recall_for_route("revisit", MentalState.from_store(s), cand)
    assert sel is not None and sel.thread_id == "thr_s"


def test_preference_rolls_above_strength_keeps_default_not_forced(tmp_path):
    s = _two_dormant(tmp_path)
    now = hours_later(s, 24)
    cand = qualifying_dormant_self_thread(MentalState.from_store(s), s, now)
    assert cand is not None and cand.thread_id == "thr_s"
    mind = MindLoop(s, rng=ScriptedRng(0.99))  # roll > REVIVAL_STRENGTH
    _, sel, _q = mind._recall_for_route("revisit", MentalState.from_store(s), cand)
    # NOT forced: the default (newer user) thread still wins
    assert sel is not None and sel.thread_id == "thr_u"


def test_preference_strength_is_bounded_probability():
    # the bias is a probability strictly between 0 and 1: never always, never never
    assert 0.0 < REVIVAL_STRENGTH < 1.0


# ----------------------------------------------------------- off-by-default switch

async def test_disabled_mechanism_never_sets_candidate(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    filler(s, 13)  # a lull that WOULD qualify if the mechanism were on
    mind = MindLoop(s, rng=random.Random(0), self_revival=False)
    await mind.wake_once()
    sel = s.list(1, type_prefix="wake.route_selected", order="desc")[0]
    assert sel.content.get("self_revival_candidate") is False


async def test_enabled_mechanism_sets_candidate_in_qualifying_lull(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    filler(s, 13)
    mind = MindLoop(s, rng=random.Random(0), self_revival=True)
    await mind.wake_once()
    sel = s.list(1, type_prefix="wake.route_selected", order="desc")[0]
    assert sel.content.get("self_revival_candidate") is True


async def test_disabled_mechanism_counts_zero_revival(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    thought_on(s, "thr_s")
    dormant(s, "thr_s")
    filler(s, 13)
    mind = MindLoop(s, rng=random.Random(0), self_revival=False)
    for _ in range(4):
        await mind.wake_once()
    assert self_thread_revival_rate(s) == 0.0
    prof = self_dynamics_profile(s)
    assert prof["self_thread_revival_detail"] == {"eligible_wakes": 0, "engaged": 0}


# --------------------------------------------------------------------- route lift

def test_route_lift_only_nudges_revisit():
    policy = RoutePolicy()
    base = policy.base_scores(RouteState())
    # the lift is carried on the per-wake RouteState (sweepable), gated by
    # self_revival_available — so a plain state has no lift
    assert policy.base_scores(RouteState(self_revival_available=True))["revisit"] == \
        base["revisit"]  # no lift unless the size is set
    lifted = policy.base_scores(RouteState(
        self_revival_available=True, self_revival_route_lift=SELF_REVIVAL_ROUTE_LIFT))
    assert abs(lifted["revisit"] - base["revisit"] - SELF_REVIVAL_ROUTE_LIFT) < 1e-9
    # every other route is untouched by the lift
    for r in ("continuity", "distant", "serendipity", "personal", "self",
              "rest", "world"):
        assert lifted[r] == base[r]


# ----------------------------------------------------------------------- metrics

def test_self_dynamics_metrics_on_constructed_log(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")           # t0
    created(s, "thr_s2", "self")          # t1
    # a 3-deep "thought begetting thought" chain inside one self thread:
    #   t2 cites nothing, t3 cites t2, t4 cites t3 (same thread thr_s)
    t2 = thought_on(s, "thr_s")          # t2
    t3 = thought_on(s, "thr_s", related_to=[t2.id])   # t3
    t4 = thought_on(s, "thr_s", related_to=[t3.id])   # t4
    thought_on(s, "thr_s2")              # t5 a second self thread, 1 thought
    prof = self_dynamics_profile(s)
    # generation depth: the same-thread chain t2->t3->t4 is 3 deep
    assert prof["self_thread_generation_depth"] == 3
    assert prof["longest_self_chain"] == 4  # four consecutive self thoughts
    conc = prof["self_loop_concentration"]
    assert conc["total"] == 4 and conc["n_distinct"] == 2
    assert conc["top1_share"] == 0.75       # 3 of 4 live in thr_s
    div = prof["self_topic_diversity"]
    assert div["n_distinct"] == 2 and 0.0 < div["entropy"] < 1.0
    # lifetime: each thread spans 4h -> mean 4h = 1/6 day (metric rounds to 3dp)
    assert abs(prof["self_thread_avg_lifetime_days"] - (4 / 24)) < 1e-3
    assert prof["self_thread_revival_rate"] == 0.0
    assert prof["self_thread_revival_detail"] == {"eligible_wakes": 0, "engaged": 0}


def test_revival_rate_is_a_real_ratio(tmp_path):
    s = make_store(tmp_path)
    # two eligible lull wakes (self_revival_candidate True), one not
    for flag in (True, True, False):
        s.append(EventCreate(type="wake.route_selected",
                             content={"self_revival_candidate": flag, "route": "revisit"}))
    # one genuine self-thread revisit (self_revival True), one ordinary resurface
    s.append(EventCreate(type="thread.revisited",
                         content={"thread_id": "x", "self_revival": True},
                         links={"thread_id": "x"}))
    s.append(EventCreate(type="thread.revisited",
                         content={"thread_id": "y", "reason": "resurfaced via recall"},
                         links={"thread_id": "y"}))
    assert self_thread_revival_rate(s) == 0.5  # 1 engaged / 2 eligible
    assert self_dynamics_profile(s)["self_thread_revival_detail"] == \
        {"eligible_wakes": 2, "engaged": 1}


def test_revival_rate_zero_when_eligible_but_not_engaged(tmp_path):
    s = make_store(tmp_path)
    s.append(EventCreate(type="wake.route_selected",
                         content={"self_revival_candidate": True, "route": "revisit"}))
    # the mind revisited a NON-self thread -> no self_revival flag
    s.append(EventCreate(type="thread.revisited",
                         content={"thread_id": "u"}, links={"thread_id": "u"}))
    assert self_thread_revival_rate(s) == 0.0
    assert self_dynamics_profile(s)["self_thread_revival_detail"] == \
        {"eligible_wakes": 1, "engaged": 0}


def test_candidate_tie_and_origin_helpers(tmp_path):
    s = make_store(tmp_path)
    created(s, "thr_s", "self")
    created(s, "thr_u", "user")
    assert thread_origin(s, "thr_s") == "self"
    assert thread_origin(s, "thr_u") == "user"
    assert thread_origin(s, "nope") == "user"  # unknown -> default user
