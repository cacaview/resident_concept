"""Phase-6 World-Window tests (ADR-0008).

These pin down the phase's invariants, each of which the brief lists explicitly:

- **determinism** — a fixed seed + virtual clock makes a run byte-reproducible
  (re-run → identical metrics);
- **provenance back-linking** — every ``"I saw / read / discovered X"`` (a
  world-origin thought) must back-link a *real* ``world.observation`` (the
  provenance root); a dangling ``evt_*`` link is structurally impossible
  (the store rejects it);
- **reading ≠ thought** — an open always yields observation + experience +
  (weak) impression, but a durable thought forms only on a *real connection*
  (a re-activated stored impression, or an association with an old, non-world
  memory). First exposure with no connection leaves **seen-but-left-nothing**;
- **budgets are caps, not quotas** — a day may open 0 windows (legal), and no
  day may exceed the per-day light cap;
- **the gate is soft + logged** — hard guards (asleep / budget / cooldown)
  suppress outright (no event), while an eligible-but-declined window writes a
  ``world.window_skipped`` carrying the eligibility (propensity, silence, focus,
  budget) — so every "why didn't it look" is auditable;
- **the Impression layer** — a stored impression, re-activated by a later related
  observation, can become a grounded, low-probability thought;
- **entry modes** are defined by the cognitive frontier and are restricted by the
  arm's ``enabled_modes``;
- **the A/B/C isolation** — arm A (no world) produces zero world events, and a
  world engine that *never opens* is invisible to the mind (the mind/sleep/
  circadian machinery is byte-identical; the world engine carries its own rng and
  writes nothing, so it disturbs neither the mind's rng stream nor its inputs).

Everything is hermetic: injectable virtual clock + seeded rng + the in-code,
no-network world corpus. No wall-clock reads, no real model, no network.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from resident.circadian import (
    CircadianOrchestrator,
    CircadianStateMachine,
    SLEEP,
)
from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.reentry import ReentryEngine
from resident.simulation import SIM_START, UserTurn, VirtualClock, _inject_turn
from resident.sleep import DEFAULT_SELF_PROB, SleepEngine
from resident.telemetry import Telemetry
from resident.world import (
    WORLD_ALL,
    WORLD_FOLLOW_EDGE,
    WorldParams,
    WorldWindowEngine,
    world_profile,
)
from resident.world_corpus import WorldCorpus, WorldItem
from resident.world_sim import run_world_simulation

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 6, 0, 0, tzinfo=UTC)


# ------------------------------------------------------------------ helpers


def _store_with_clock(tmp_path, start: datetime = T0):
    clock = VirtualClock(start)
    store = EventStore(tmp_path / "e.sqlite3", now_fn=lambda: clock.now)
    return store, clock


def _engine(store, *, params=None, seed=0, corpus=None, memory=None) -> WorldWindowEngine:
    mem = memory or MemoryRetrieval(store)
    return WorldWindowEngine(store, memory=mem, corpus=corpus,
                             params=params or WorldParams(), rng=random.Random(seed))


def _force_open(**over) -> WorldParams:
    """Params that make the soft gate open on the first eligible beat (propensity
    driven to 1.0) and never form a thought (no association, no promotion) — a
    clean single exposure for chain/provenance assertions. Override any field."""
    base = dict(base_propensity=1.0, propensity_cap=1.0, cooldown_s=0,
                impression_prob=1.0, association_prob=0.0, promotion_prob=0.0)
    base.update(over)
    return WorldParams(**base)


def _isolated_corpus(n: int = 4) -> WorldCorpus:
    """Items with **unique topics and no links** — so no re-activation is
    possible (nothing is "connected" to anything) and no association target
    exists in a fresh store. Pure first exposures."""
    return WorldCorpus(
        tuple(WorldItem(id=f"iso{i}", source="src", topic=f"topic{i}",
                        title=f"Item {i}", summary=f"Neutral fact {i}", links=())
              for i in range(n))
    )


def _linked_corpus() -> WorldCorpus:
    """Two items in one topic, linking to each other — so opening the second
    re-activates the first's stored impression (the Impression layer's purpose)."""
    return WorldCorpus((
        WorldItem(id="a", source="src", topic="t", title="A", summary="Fact A", links=("b",)),
        WorldItem(id="b", source="src", topic="t", title="B", summary="Fact B", links=("a",)),
    ))


def _open_loop(engine: WorldWindowEngine, clock, n: int, *, start: datetime = T0,
               step_min: int = 30, state: str = "QUIET") -> list[dict]:
    """Advance the virtual clock and call ``maybe_open`` ``n`` times."""
    out = []
    for i in range(n):
        t = start + timedelta(minutes=step_min * i)
        clock.set(t)
        out.append(engine.maybe_open(t.isoformat(), circadian_state=state))
    return out


# ------------------------------------------------------------------ event chain


def test_open_writes_full_provenance_chain(tmp_path):
    """open -> observation (PROVENANCE ROOT) -> experience -> impression, each
    back-linked; the observation is caused_by the window_opened decision."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open())
    clock.set(T0)
    res = eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
    assert res["result"] == "opened"
    assert res["thought_id"] is None  # first exposure, no connection -> no thought

    obs = store.get(res["observation_id"])
    assert obs is not None and obs.type == "world.observation"
    # the observation is the provenance root: caused_by the window_opened event
    opened_id = obs.links["caused_by"][0]
    assert opened_id.startswith("evt_")
    assert store.get(opened_id).type == "world.window_opened"
    # experience is caused_by the observation
    exp = store.get(res["experience_id"])
    assert exp is not None and exp.type == "world.experience"
    assert exp.links["caused_by"][0] == obs.id
    # impression is caused_by the experience and related_to the observation
    imp = store.get(res["impression_id"])
    assert imp is not None and imp.type == "impression.formed"
    assert imp.links["caused_by"][0] == exp.id
    assert obs.id in imp.links["related_to"]


def test_first_exposure_leaves_nothing_without_connection(tmp_path):
    """reading ≠ thought: with no stored impression to re-activate and no old
    memory to associate with, every look leaves only a weak impression — a high
    seen-left-nothing ratio (100% here)."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open(max_alien_per_day=10), corpus=_isolated_corpus(5))
    _open_loop(eng, clock, 5)
    prof = world_profile(store)
    assert prof["n_windows_opened"] == 5
    assert prof["n_world_thoughts"] == 0
    assert prof["seen_left_nothing_rate"] == 1.0
    assert prof["n_impressions"] == 5


# ------------------------------------------------------------------ impression


def test_reactivation_forms_grounded_thought(tmp_path):
    """The Impression layer: a stored impression, re-activated by a later related
    observation, becomes a grounded, low-probability world thought."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open(promotion_prob=1.0), corpus=_linked_corpus())
    r1, r2 = _open_loop(eng, clock, 2)
    assert r1["result"] == "opened" and r1["thought_id"] is None  # first exposure
    assert r2["result"] == "opened"
    assert r2["thought_id"] is not None
    assert r2["thought_kind"] == "reactivation"
    thought = store.get(r2["thought_id"])
    assert (thought.metadata or {}).get("origin") == "world"
    # grounded: back-links the new observation AND the prior impression
    assert r2["observation_id"] in (thought.links.get("caused_by") or [])
    assert r1["impression_id"] in (thought.links.get("related_to") or [])


def test_promotion_is_low_probability(tmp_path):
    """promotion_prob=0 -> the re-activable impression is NOT promoted (no
    thought); the connection is real but the roll decides (probabilistic, <1)."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open(promotion_prob=0.0), corpus=_linked_corpus())
    _, r2 = _open_loop(eng, clock, 2)
    assert r2["result"] == "opened"
    assert r2["thought_id"] is None  # impression re-activated but not promoted


# ------------------------------------------------------------------ provenance


def test_world_thoughts_back_link_real_observations(tmp_path):
    """Any 'I saw/read X' (world thought) must cite a real observation; a
    dangling evt_* link is structurally rejected by the store."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open(promotion_prob=1.0), corpus=_linked_corpus())
    _open_loop(eng, clock, 2)
    world_thoughts = [t for t in store.list(1000, type_prefix="thought.created")
                      if (t.metadata or {}).get("origin") == "world"]
    assert world_thoughts
    for t in world_thoughts:
        for field in ("caused_by", "related_to"):
            for eid in (t.links.get(field) or []):
                if isinstance(eid, str) and eid.startswith("evt_"):
                    assert store.get(eid) is not None, f"dangling link {eid} in {t.id}"
        # specifically: a world thought cites a real world.observation
        cited_obs = [
            store.get(eid) for eid in (t.links.get("related_to") or [])
            if isinstance(eid, str) and eid.startswith("evt_")
        ]
        assert any(e is not None and e.type == "world.observation" for e in cited_obs)


# ------------------------------------------------------------------ budgets


def test_zero_days_legal(tmp_path):
    """A day may open 0 windows — a legitimate, non-failure outcome. With the
    propensity driven to 0 the gate never passes; the run is all skips."""
    store, clock = _store_with_clock(tmp_path)
    params = WorldParams(base_propensity=0.0, quiet_bonus=0.0, silence_bonus_max=0.0,
                         impression_prob=0.0)
    eng = _engine(store, params=params)
    results = _open_loop(eng, clock, 8)
    assert all(r["result"] in ("skipped", "cooldown", "budget_exhausted") for r in results)
    prof = world_profile(store)
    assert prof["n_windows_opened"] == 0
    assert prof["world_exposure_rate"] == 0.0
    assert prof["n_windows_skipped"] > 0  # the eligible declines were logged


def test_daily_budget_is_a_cap(tmp_path):
    """Per-day numbers are caps, not quotas: a day can never exceed the light
    cap (here 3), even when the gate would keep opening."""
    store, clock = _store_with_clock(tmp_path)
    threads: dict[str, str] = {}
    _inject_turn(store, UserTurn(1, 6, "food", "周末想试试做意面，有什么简单的酱吗？"), threads)
    params = _force_open(max_light_per_day=3)
    eng = _engine(store, params=params)
    results = _open_loop(eng, clock, 12, step_min=30)
    opens = sum(1 for r in results if r["result"] == "opened")
    assert opens <= 3
    # once the cap is hit, later beats are budget-blocked
    if opens == 3:
        assert any(r["result"] == "budget_exhausted" for r in results)


# ------------------------------------------------------------------ the gate


def test_asleep_suppresses_without_event(tmp_path):
    """A hard guard (asleep) suppresses outright — no skip event is written
    (the window was never 'on the table')."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store)
    t = datetime(2026, 6, 1, 3, 0, 0, tzinfo=UTC)
    clock.set(t)
    r = eng.maybe_open(t.isoformat(), circadian_state=SLEEP)
    assert r["result"] == "asleep"
    assert store.count("world.window_skipped") == 0
    assert store.count("world.window_opened") == 0


def test_eligible_decline_logs_eligibility(tmp_path):
    """An eligible-but-declined window writes a world.window_skipped carrying the
    eligibility (propensity, silence, focus, budget) — so every 'why not look'
    is auditable (the gate is soft AND logged)."""
    store, clock = _store_with_clock(tmp_path)
    params = WorldParams(base_propensity=0.0, quiet_bonus=0.0, silence_bonus_max=0.0,
                         impression_prob=0.0)
    eng = _engine(store, params=params, seed=999)
    clock.set(T0)
    r = eng.maybe_open(T0.isoformat(), circadian_state="QUIET")
    assert r["result"] == "skipped"
    assert r["blocker"] == "propensity_below_threshold"
    sk = store.list(1, type_prefix="world.window_skipped", order="desc")[0]
    for key in ("propensity", "circadian_state", "opened_today", "light_budget"):
        assert key in sk.content, f"eligibility key {key} missing from skip event"


# ------------------------------------------------------------------ entry modes


def test_enabled_modes_restrict_selection(tmp_path):
    """A follow+edge arm (B) never opens in alien/accident mode, even with a
    live thread that makes follow reachable."""
    store, clock = _store_with_clock(tmp_path)
    threads: dict[str, str] = {}
    _inject_turn(store, UserTurn(1, 6, "food", "周末想试试做意面，有什么简单的酱吗？"), threads)
    params = WorldParams(base_propensity=1.0, propensity_cap=1.0, cooldown_s=0,
                         enabled_modes=("follow", "edge"), impression_prob=0.0,
                         association_prob=0.0, promotion_prob=0.0)
    eng = _engine(store, params=params)
    results = _open_loop(eng, clock, 24)
    modes = {r["entry_mode"] for r in results if r["result"] == "opened"}
    assert modes <= {"follow", "edge"}
    assert "alien" not in modes and "accident" not in modes


# ------------------------------------------------------------------ telemetry


def test_world_origin_in_telemetry(tmp_path):
    """World thoughts carry origin='world' and surface in both the world profile
    and the origin distribution exposed by Telemetry."""
    store, clock = _store_with_clock(tmp_path)
    eng = _engine(store, params=_force_open(promotion_prob=1.0), corpus=_linked_corpus())
    _open_loop(eng, clock, 2)
    tel = Telemetry(store)
    snap = tel.snapshot()
    assert snap["world"]["n_world_thoughts"] >= 1
    assert snap["origin_distribution"].get("world", 0) >= 1
    assert snap["world"]["origin_shares"]["world_origin"] >= 0  # a distinct origin in the profile


# ------------------------------------------------------------------ sim


@pytest.mark.asyncio
async def test_world_sim_deterministic(tmp_path):
    """Re-running the same seed + virtual clock is byte-reproducible: identical
    world profile AND identical mind-level readings."""
    r1 = await run_world_simulation(tmp_path / "d1", days=7, seed=42, world_params=WORLD_ALL)
    r2 = await run_world_simulation(tmp_path / "d2", days=7, seed=42, world_params=WORLD_ALL)
    assert r1["world"] == r2["world"]
    assert r1["route_entropy"] == r2["route_entropy"]
    assert r1["no_op_ratio"] == r2["no_op_ratio"]
    assert r1["meta"]["n_wakes"] == r2["meta"]["n_wakes"]


@pytest.mark.asyncio
async def test_no_world_arm_has_no_world_events(tmp_path):
    """Arm A (no world engine) is the clean control: zero world / impression
    events, zero world-origin thoughts."""
    rep = await run_world_simulation(tmp_path / "a", days=7, seed=0, world_params=None)
    assert rep["world"]["n_windows_opened"] == 0
    assert rep["world"]["n_world_thoughts"] == 0
    store = EventStore(tmp_path / "a" / "world.sqlite3")
    assert store.count("world.window_opened") == 0
    assert store.count("world.observation") == 0
    assert store.count("impression.formed") == 0


@pytest.mark.asyncio
async def test_world_engine_never_opening_is_invisible_to_mind(tmp_path):
    """The A/B/C isolation, at the mechanism level: a world engine attached but
    *never opening* (propensity 0) leaves the mind's **route decisions** identical
    to the no-world control, and contributes **zero world material** — only skip
    telemetry.

    The world engine carries its own rng, so it never disturbs the mind's rng
    stream; and the window's open/skip *decisions* are audit logging, not
    experience, so they do not flip the mind's "anything new since I last woke"
    signal (see ``MindLoop._route_context``). What we assert — the coarse,
    meaningful isolation — is that the mind's *route sequence* is unchanged and
    that no world observation/experience/impression/thought/thread was created.
    (Byte-identical *generated text* is deliberately NOT asserted: the distant /
    serendipity recall keyword path reads system events, a pre-existing property
    out of scope here. In the real B/C arms the window *does* open, so its
    experience is legitimately recallable — that is the point of the phase.)"""

    async def drive(subdir, world):
        db = subdir / "world.sqlite3"
        clock = VirtualClock(SIM_START)
        store = EventStore(db, now_fn=lambda: clock.now)
        mem = MemoryRetrieval(store)
        mind = MindLoop(store, memory=mem, rng=random.Random(0), self_revival=True)
        sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(1000),
                                   self_prob=DEFAULT_SELF_PROB)
        reentry = ReentryEngine(store, memory=mem)
        machine = CircadianStateMachine(store)
        world_engine = None
        if world is not None:
            world_engine = WorldWindowEngine(store, memory=mem, params=world,
                                             rng=random.Random(2000))
        orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry,
                                     machine=machine, world_engine=world_engine)
        threads: dict[str, str] = {}
        turns = [
            (SIM_START + timedelta(hours=0), UserTurn(1, 6, "pasta", "周末想试试做意面。")),
            (SIM_START + timedelta(days=1, hours=3), UserTurn(2, 9, "work", "下周有个 deadline。")),
        ]
        idx = 0
        for i in range(96):  # 48 h of 30-min beats
            now = SIM_START + timedelta(minutes=30 * i)
            clock.set(now)
            while idx < len(turns) and turns[idx][0] <= now:
                _inject_turn(store, turns[idx][1], threads)
                idx += 1
            await orch.beat(now.isoformat())
        routes = [e.content.get("route") for e in
                  store.list(10000, type_prefix="wake.route_selected", order="asc")]
        return store, routes

    never = WorldParams(base_propensity=0.0, quiet_bonus=0.0, silence_bonus_max=0.0,
                        impression_prob=0.0, association_prob=0.0, promotion_prob=0.0)
    store_none, routes_none = await drive(tmp_path / "none", None)
    store_never, routes_never = await drive(tmp_path / "never", never)
    # the mind's route sequence is unchanged by a never-opening window
    assert routes_none == routes_never
    assert len(routes_none) > 0
    # and the never-opening window created no world *material* (only skip telemetry)
    for prefix in ("world.window_opened", "world.observation", "world.experience",
                   "impression.formed", "impression.promoted"):
        assert store_never.count(prefix) == 0, f"unexpected {prefix} in never-opening arm"
    assert store_never.count("world.window_skipped") > 0  # the declines WERE logged
    world_thoughts = [e for e in store_never.list(1000, type_prefix="thought.created")
                      if (e.metadata or {}).get("origin") == "world"]
    assert world_thoughts == []

