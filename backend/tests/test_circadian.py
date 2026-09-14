"""Phase-5 circadian integration tests (ADR-0007).

The five live/integration scenarios required by the phase brief, plus the
windowed ``/api/telemetry`` endpoint and the circadian-vs-fixed deterministic
sim:

- (a) the state machine descends ``ACTIVE -> QUIET -> DROWSY -> SLEEP`` and
  re-opens (``WAKE``) — at the pure decision level AND driven over a virtual
  day+night timeline by the real orchestrator (which also proves every
  transition is recorded as a provenance-complete ``circadian.state_changed``);
- (b) a returning user interrupts sleep (``SLEEP -> WAKE``) WITHOUT erasing the
  mind's pre-existing threads (the log is append-only; the mental state is
  preserved — user input is new *input*, not a reset);
- (c) a sleep may legitimately do nothing (no forced thought);
- (d) the circadian raises the revival OPPORTUNITY (a recognised quiet drops the
  gate's silence bar) but never FORCES a self-thread selection (the levers stay
  bounded and probabilistic < 1, and are independent of circadian state);
- (e) the windowed telemetry is consistent with the event store.

Everything is hermetic: injectable virtual clock + seeded rng + the deterministic
fake provider. No wall-clock reads, no real model, no network.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from resident.circadian import (
    ACTIVE, ASLEEP_STATES, CIRCADIAN_STATE_CHANGED, CONSOLIDATING, DROWSY, QUIET,
    SLEEP, WAKE, CircadianEvidence, CircadianOrchestrator, CircadianStateMachine,
)
from resident.circadian_sim import comparison_markdown, run_comparison
from resident.event_store import EventStore
from resident.main import build_app
from resident.memory import MemoryRetrieval
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.mental_state import MentalState
from resident.reentry import ReentryEngine
from resident.self_revival import (
    QUIET_GATE_SILENCE_S, REVIVAL_STRENGTH, SELF_REVIVAL_ROUTE_LIFT, SILENCE_S,
    RevivalParams, revival_gate,
)
from resident.simulation import UserTurn, _inject_turn
from resident.sleep import SleepEngine
from resident.telemetry import Telemetry


# ------------------------------------------------------------------ helpers


def _ev(**kw) -> CircadianEvidence:
    """A decision evidence bundle with idle defaults (the smoke-test convention)."""
    base = dict(now="2026-06-01T12:00:00+00:00", local_hour=12.0,
                since_user_s=3600.0, since_meaningful_s=3600.0,
                recent_wake_count=0, n_active_threads=0, current=ACTIVE)
    base.update(kw)
    return CircadianEvidence(**base)


class _Clock:
    """A mutable virtual clock the orchestrator-driven tests advance by hand."""

    def __init__(self, dt: datetime):
        self.now = dt

    def set(self, dt: datetime) -> None:
        self.now = dt


def _build_stack(tmp_path, start: datetime, *, seed: int = 0, self_prob: float = 0.0) -> SimpleNamespace:
    """The real engines wired to a virtual clock (the same wiring as the sim)."""
    clock = _Clock(start)
    store = EventStore(tmp_path / "life.sqlite3", now_fn=lambda: clock.now)
    mem = MemoryRetrieval(store)
    mind = MindLoop(store, memory=mem, rng=random.Random(seed), self_revival=True)
    sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(seed + 1000), self_prob=self_prob)
    reentry = ReentryEngine(store, memory=mem)
    machine = CircadianStateMachine(store)
    orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry, machine=machine)
    return SimpleNamespace(store=store, mem=mem, mind=mind, sleep=sleep_engine,
                           reentry=reentry, machine=machine, orch=orch, clock=clock)


def _thread_ids(store) -> set[str]:
    return {e.content["thread_id"] for e in store.list(1000, type_prefix="thread.created")}


def _self_thread_ids(store) -> set[str]:
    return {e.content["thread_id"] for e in store.list(1000, type_prefix="thread.created")
            if e.content.get("origin") == "self"}


def _thought_ids(store) -> set[str]:
    return {e.id for e in store.list(1000, type_prefix="thought.created")}


# ================================================================ (a) descent


def test_decide_descends_through_all_awake_states_and_reopens(tmp_path):
    """Pure decision level: a left-alone mind descends active->quiet->drowsy->sleep
    as user silence grows, holds asleep, and re-opens when the user returns."""
    m = CircadianStateMachine(EventStore(tmp_path / "a.sqlite3"))
    day = 12.0  # daytime: no night/morning bias, so only the silence drives it
    assert m.decide(_ev(local_hour=day, since_user_s=10 * 60))[0] == ACTIVE, "user just active -> awake"
    assert m.decide(_ev(local_hour=day, since_user_s=2.5 * 3600, since_meaningful_s=3 * 3600))[0] == QUIET
    assert m.decide(_ev(local_hour=day, since_user_s=4.5 * 3600, since_meaningful_s=5 * 3600))[0] == DROWSY
    assert m.decide(_ev(local_hour=day, since_user_s=7 * 3600, since_meaningful_s=7 * 3600))[0] == SLEEP
    # asleep is sticky: still silent, no external stimulus -> stays asleep
    assert m.decide(_ev(current=SLEEP, local_hour=day, since_user_s=8 * 3600, since_meaningful_s=8 * 3600))[0] == SLEEP
    # the user returns -> re-open
    assert m.decide(_ev(current=SLEEP, local_hour=day, since_user_s=10 * 60))[0] == WAKE
    # a freshly re-opened mind is up and about
    assert m.decide(_ev(current=WAKE, local_hour=day, since_user_s=7 * 3600))[0] == ACTIVE


def test_state_change_records_from_to_and_evidence(tmp_path):
    """Requirement: every state change writes circadian.state_changed with from/to
    + the evidence that justified it (provenance-complete)."""
    m = CircadianStateMachine(EventStore(tmp_path / "b.sqlite3"))
    d = m.assess(_ev(local_hour=12.0, since_user_s=2.5 * 3600, since_meaningful_s=3 * 3600))
    assert d.changed is True
    assert d.from_state == ACTIVE and d.state == QUIET
    evts = m.store.list(1, type_prefix=CIRCADIAN_STATE_CHANGED, order="desc")
    assert len(evts) == 1
    c = evts[0].content
    assert c["from"] == ACTIVE and c["to"] == QUIET
    assert c["reasons"], "a transition must carry its reasons"
    assert "evidence" in c and "user_silence_h" in c["evidence"]


def test_machine_reconstructs_state_from_log(tmp_path):
    """Restart persistence: the log is the source of truth, so a fresh machine on
    the same store picks up where the previous one left off."""
    store = EventStore(tmp_path / "c.sqlite3")
    m1 = CircadianStateMachine(store)
    m1.assess(_ev(local_hour=12.0, since_user_s=7 * 3600, since_meaningful_s=7 * 3600))
    assert m1.state == SLEEP
    m2 = CircadianStateMachine(store)  # a "restarted" process
    assert m2.state == SLEEP


async def test_daynight_reaches_all_states_and_reopens(tmp_path):
    """(a, timeline) The orchestrator drives the real engines over a virtual
    ~27 h: a user-present morning, an afternoon descent to sleep, a quiet night,
    and a morning re-open. All five states are actually reached, transitions are
    recorded, a real consolidation ran, and the night ends in a re-open."""
    start = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)
    st = _build_stack(tmp_path, start)
    # the user is present 06:00-08:00, then gone.
    turns = {
        start + timedelta(hours=0): UserTurn(1, 6, "reading", "最近在读一本讲记忆的书"),
        start + timedelta(hours=1): UserTurn(1, 7, "work", "项目 deadline 有点紧"),
        start + timedelta(hours=2): UserTurn(1, 8, "reading", "那本书第三章很有意思"),
    }
    threads: dict[str, str] = {}
    states: list[str] = []
    t, end = start, start + timedelta(hours=27)  # 06:00 d1 -> 09:00 d2
    while t <= end:
        st.clock.set(t)
        if t in turns:
            _inject_turn(st.store, turns[t], threads)
        states.append((await st.orch.beat(t.isoformat()))["state"])
        t += timedelta(minutes=30)

    need = {ACTIVE, QUIET, DROWSY, SLEEP, WAKE}
    assert need.issubset(set(states)), f"missing states: {need - set(states)}"
    # every transition is recorded, and a real consolidation ran during sleep
    assert st.store.count(CIRCADIAN_STATE_CHANGED) > 0
    assert st.store.count("sleep.completed") >= 1
    # the night actually ends: an asleep -> WAKE re-open happens
    tr = st.orch.state_trace
    assert any(tr[i - 1]["state"] in ASLEEP_STATES and tr[i]["state"] == WAKE
               for i in range(1, len(tr))), "no morning re-open (sleep->wake)"


# ========================================================== (b) re-appearance


async def test_reappearance_interrupts_sleep_preserving_threads(tmp_path):
    """(b) A returning user wakes a sleeping mind and does NOT erase the mind's
    pre-existing threads — a conversation is new *input*, not a mental reset."""
    start = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)
    st = _build_stack(tmp_path, start, self_prob=1.0)  # a grounded self thread can form
    threads: dict[str, str] = {}
    # two topics in the morning -> real cross-topic material
    st.clock.set(start)
    _inject_turn(st.store, UserTurn(1, 6, "work", "下周有个项目 deadline，压力有点大。"), threads)
    _inject_turn(st.store, UserTurn(1, 7, "reading", "最近在读一本讲记忆的书。"), threads)
    # let the mind run through the afternoon; the user is gone after 07:00, so by
    # mid-afternoon it descends to SLEEP and a real SleepEngine cycle crystallises
    # a grounded self thread from the two topics.
    t = start + timedelta(hours=9)
    while t <= start + timedelta(hours=16):
        st.clock.set(t)
        await st.orch.beat(t.isoformat())
        t += timedelta(minutes=30)
    assert st.machine.state in ASLEEP_STATES, f"mind should be asleep (got {st.machine.state})"

    pre_threads, pre_self, pre_thoughts = _thread_ids(st.store), _self_thread_ids(st.store), _thought_ids(st.store)
    assert pre_threads, "the mind should have threads before the user leaves"
    assert pre_self, "a grounded self thread should have formed during sleep"

    # the user re-appears at 16:30 (a fresh message, a new topic)
    st.clock.set(start + timedelta(hours=16, minutes=30))
    _inject_turn(st.store, UserTurn(1, 16, "pasta", "周末想试试做意面。"), threads)
    await st.orch.beat(st.clock.now.isoformat())

    assert st.machine.state == WAKE, f"user re-appearance must wake a sleeping mind (got {st.machine.state})"
    # NOTHING was destroyed: every pre-existing thread/thought is still in the log
    assert pre_threads <= _thread_ids(st.store), "pre-existing threads were erased"
    assert pre_self <= _self_thread_ids(st.store), "the mind's own self thread was erased"
    assert pre_thoughts <= _thought_ids(st.store), "pre-existing thoughts were erased"
    # and the mind's own thread is still live in its mental state (active or dormant)
    ms = MentalState.from_store(st.store)
    present = {t.thread_id for t in ms.active_threads} | {t.thread_id for t in ms.dormant_threads}
    assert pre_self <= present, "the self thread is no longer in the mind's state"


# ============================================================= (c) sleep noop


async def test_sleep_can_do_nothing(tmp_path):
    """(c) A sleep may legitimately do nothing — no forced thought (ADR-0004, and
    the phase's 'no forced thought in sleep' constraint)."""
    # an empty mind: nothing qualifies, so the cycle is a first-class no-op
    store = EventStore(tmp_path / "empty.sqlite3")
    sleep = SleepEngine(store, rng=random.Random(0), self_prob=1.0, dream_prob=1.0)
    res = await sleep.sleep_once("night")
    assert res["result"] == "noop"
    assert res["event_ids"] == []
    assert store.count("thought.created") == 0
    assert store.list(1, type_prefix="sleep.completed", order="desc")[0].content["result"] == "noop"

    # a mind with a single isolated thought (below the dream/self thresholds)
    # also adds nothing new — the pre-existing thought is untouched
    store2 = EventStore(tmp_path / "one.sqlite3")
    store2.append(EventCreate(type="thought.created", visibility="private",
                              content={"text": "一个孤立的念头"}, metadata={"origin": "user_recent"}))
    sleep2 = SleepEngine(store2, rng=random.Random(0), self_prob=1.0, dream_prob=1.0)
    await sleep2.sleep_once("night")
    assert store2.count("thought.created") == 1, "sleep must not manufacture a thought"


# ================================================ (d) opportunity, not force


def test_circadian_raises_opportunity_not_force(tmp_path):
    """(d) The circadian's recognised quiet drops the gate's user-silence bar
    (12h -> ~6h), so a lull that is too short for the default bar opens the
    OPPORTUNITY only when the circadian has confirmed the quiet. It never FORCES
    which thread is picked: the selection levers stay bounded, probabilistic < 1,
    and independent of circadian state."""
    now = datetime(2026, 6, 1, 14, 0, 0, tzinfo=timezone.utc)
    clock = _Clock(now)
    store = EventStore(tmp_path / "gate.sqlite3", now_fn=lambda: clock.now)
    # the user was last active 8 h ago: below the 12 h default bar, above the 6 h
    # quiet floor.
    clock.set(now - timedelta(hours=8))
    store.append(EventCreate(type="conversation.user_message", actor="user",
                             visibility="user_visible", content={"text": "hi"}))
    clock.set(now)
    now_iso = now.isoformat()

    # outside a recognised quiet the gate stays closed (8 h < 12 h)
    assert revival_gate(store, n_active=0, now_iso=now_iso, in_quiet_state=False) is False
    # the circadian has recognised the lull -> the bar drops to ~6 h -> gate open
    assert revival_gate(store, n_active=0, now_iso=now_iso, in_quiet_state=True) is True
    # the quiet floor is the Phase-4.1 ~6 h working region, below the default
    assert 0 < QUIET_GATE_SILENCE_S < SILENCE_S
    # ...but the circadian never FORCES a self selection: the levers that decide
    # WHICH thread are bounded and strictly < 1 (the default still wins sometimes),
    # and they are the same constants regardless of circadian state.
    assert 0.0 < REVIVAL_STRENGTH < 1.0
    assert 0.0 < SELF_REVIVAL_ROUTE_LIFT < 1.0
    p = RevivalParams()
    assert 0.0 < p.thread_strength < 1.0
    assert 0.0 < p.route_lift < 1.0


# ========================================================== (e) telemetry


def test_window_report_consistent_with_store(tmp_path):
    """(e) The windowed telemetry is a read over the log: its counts match the
    store exactly (no fabricated signal)."""
    store = EventStore(tmp_path / "tel.sqlite3")
    for r in ("continuity", "revisit", "rest", "continuity"):
        store.append(EventCreate(type="wake.started", visibility="system", content={}))
        store.append(EventCreate(type="wake.route_selected", visibility="system",
                                 content={"route": r, "scores": {r: 1.0}}))
    store.append(EventCreate(type="wake.noop", visibility="system", content={}))
    for o in ("user_recent", "user_recent", "user_recent", "self_thread"):
        store.append(EventCreate(type="thought.created", visibility="private",
                                 content={"text": "x"}, metadata={"origin": o}))
    store.append(EventCreate(type="sleep.completed", visibility="system", content={"result": "noop"}))
    store.append(EventCreate(type=CIRCADIAN_STATE_CHANGED, visibility="system",
                             content={"from": "active", "to": "quiet", "reasons": []}))

    tel = Telemetry(store)
    since = "2000-01-01T00:00:00+00:00"  # comfortably before every event
    rep = tel.window_report(since, now_iso=store.latest().created_at,
                            circadian_state="quiet", window="24h")
    # headline counts agree with the store
    assert rep["n_wakes"] == store.count("wake.started") == 4
    assert rep["noops"] == store.count("wake.noop") == 1
    assert rep["no_op_ratio"] == pytest.approx(1 / 4)
    assert rep["n_sleeps"] == store.count("sleep.completed") == 1
    assert rep["route_distribution"] == {"continuity": 2, "revisit": 1, "rest": 1}
    assert rep["route_entropy"] > 0.0
    # origin shares: 1 of 4 thoughts is self
    assert rep["origin_base"] == 4
    assert rep["self_origin_share"] == pytest.approx(0.25)
    assert rep["user_recent_share"] == pytest.approx(0.75)
    # the live circadian state + window are echoed, not aggregated
    assert rep["circadian_state"] == "quiet"
    assert rep["window"] == "24h"
    # the revival/self-dynamics metrics are present and well-formed (0 here: no
    # eligible lull, no revived thread in this tiny history)
    for k in ("eligible_wakes", "revival_engaged", "route_changed_by_bias",
              "revival_delta", "revival_leverage", "negative_delta_count",
              "self_loop_concentration", "self_topic_diversity", "generation_depth"):
        assert k in rep
    assert rep["eligible_wakes"] == 0


# ===================================================== /api/telemetry endpoint


def test_telemetry_endpoint_windows_and_consistency(tmp_path):
    """The HTTP layer: 24h/7d/30d windows, ~18 metrics, store-consistent counts,
    and window validation (400 on an unknown window)."""
    app = build_app(tmp_path)
    store = app.state.store
    with TestClient(app) as client:
        for r in ("continuity", "revisit", "rest"):
            store.append(EventCreate(type="wake.started", visibility="system", content={}))
            store.append(EventCreate(type="wake.route_selected", visibility="system",
                                     content={"route": r}))
        store.append(EventCreate(type="wake.noop", visibility="system", content={}))
        store.append(EventCreate(type="thought.created", visibility="private",
                                 content={"text": "x"}, metadata={"origin": "self_thread"}))

        r24 = client.get("/api/telemetry", params={"window": "24h"})
        assert r24.status_code == 200
        body = r24.json()
        assert body["window"] == "24h"
        assert body["scheduler"] == "circadian"
        # consistent with the store
        assert body["n_wakes"] == store.count("wake.started") == 3
        assert body["no_op_ratio"] == pytest.approx(1 / 3, abs=1e-4)  # report rounds to 4 dp
        assert body["route_distribution"] == {"continuity": 1, "revisit": 1, "rest": 1}
        assert body["self_origin_share"] == pytest.approx(1.0)
        # the full metric surface is exposed
        for k in ("circadian_state", "n_wakes", "noops", "no_op_ratio", "n_sleeps",
                  "n_consolidations", "route_distribution", "route_entropy", "topic_entropy",
                  "origin_base", "user_recent_share", "user_derived_share", "self_origin_share",
                  "eligible_wakes", "revival_engaged", "route_changed_by_bias", "revival_delta",
                  "negative_delta_count", "revival_leverage", "self_loop_concentration",
                  "self_topic_diversity", "generation_depth", "user_silence_h", "meaningful_silence_h"):
            assert k in body, f"missing telemetry metric {k}"
        # the other two windows are accepted; an unknown window is rejected
        assert client.get("/api/telemetry", params={"window": "7d"}).status_code == 200
        assert client.get("/api/telemetry", params={"window": "30d"}).status_code == 200
        assert client.get("/api/telemetry", params={"window": "99d"}).status_code == 400


# ==================================================== circadian-vs-fixed sim


def test_circadian_vs_fixed_sim_deterministic_and_clusters(tmp_path):
    """The deliverable sim: deterministic for a fixed seed, and the circadian
    produces natural activity clusters + a real quiet night (not the fixed
    baseline's uniform wake tiling) while raising the revival opportunity."""
    c1 = run_comparison(tmp_path / "s1", days=7, seed=0)
    c2 = run_comparison(tmp_path / "s2", days=7, seed=0)
    # determinism: a same-seed re-run is identical in meta + comparison + rhythm
    assert c1["fixed"]["meta"] == c2["fixed"]["meta"]
    assert c1["circadian"]["meta"] == c2["circadian"]["meta"]
    assert c1["comparison"] == c2["comparison"]
    assert [t["route"] for t in c1["circadian"]["timeline"]] == \
           [t["route"] for t in c2["circadian"]["timeline"]]

    # the circadian does NOT tile wakes uniformly like the fixed baseline
    cl = c1["comparison"]["clustering"]
    assert cl["fixed"]["intraday_gap_cv"] < 0.2, "fixed baseline should tile near-uniformly"
    assert cl["circadian"]["intraday_gap_cv"] > cl["fixed"]["intraday_gap_cv"], \
        "the circadian should cluster waking, not tile it"
    assert cl["circadian"]["night_wake_share"] < cl["fixed"]["night_wake_share"], \
        "the circadian should have a real quiet night"
    # the circadian raised the revival opportunity (recognised quiet states)
    elig = c1["comparison"]["revival_eligible_frequency"]
    assert elig["circadian"] > elig["fixed"]
    # the report renders
    md = comparison_markdown(c1)
    assert "Headline comparison" in md and "circadian" in md
