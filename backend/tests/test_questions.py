"""v0.2 Step 3 (ADR-0011): World → Question — hermetic tests.

The five owner constraints, mechanically (the brief verbatim in spirit):

1. provenance — a question exists only where a *thought could have* (real
   connection evidence), linked to the observation; the store's dangling-link
   invariant makes fabricated evidence unrepresentable;
2. questions can die — dormancy is DERIVED (no tombstone event, ever);
3. no answer pipeline — a question triggers no fetch, no window, no schedule,
   no user conversation; nothing in the log is caused by a question except a
   re-meeting;
4. re-meeting — a later INDEPENDENT experience records ``question.revisited``
   (and only while the question is still open; the window cannot resurrect);
5. no self-rating — no importance/curiosity float anywhere; salience is the
   derived citation graph.

Plus the rarity discipline (bare exposure can never ask; default OFF = sealed
behaviour, rng untouched), determinism, and the kind priority (tension before
gap).
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from resident.circadian import CircadianOrchestrator
from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.providers import DeterministicFakeProvider
from resident.sleep import SleepEngine
from resident.world import (
    WorldParams,
    WorldWindowEngine,
    question_state,
    world_profile,
)
from resident.world_corpus import DEFAULT_WORLD_CORPUS

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0


def _item(item_id: str):
    it = DEFAULT_WORLD_CORPUS.get(item_id)
    assert it is not None
    return it


def _params(**over) -> WorldParams:
    """Open-forcing rails (Phase-6 test convention) + question layer on with a
    certain roll; the thought paths are OFF unless a test asks for them."""
    base = dict(
        base_propensity=1.0, propensity_cap=1.0, cooldown_s=0.0,
        impression_prob=1.0, promotion_prob=0.0, association_prob=0.0,
        thread_prob=0.0,
        enable_questions=True, question_prob=1.0,
    )
    base.update(over)
    return WorldParams(**base)


def _engine(store, params, *, seed=0) -> WorldWindowEngine:
    return WorldWindowEngine(
        store, memory=MemoryRetrieval(store), params=params,
        rng=random.Random(seed), question_rng=random.Random(seed + 5000))


def _open(eng, now_iso, item_id, mode="follow"):
    return eng._open(now_iso, mode, _item(item_id), {"propensity": 1.0})


def _log_projection(store):
    """Behaviour projection: event ids are uuid4 (never deterministic) — event
    IDs and id-shaped values inside content are normalised away; the
    deterministic claims are the TYPE, CONTENT shape and link SHAPE."""
    import re

    eid = re.compile(r"^evt_[0-9a-f]{32}$")

    def scrub(v):
        if isinstance(v, str) and eid.match(v):
            return "<evt>"
        if isinstance(v, dict):
            return {k: scrub(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [scrub(x) for x in v]
        return v

    return [(e.type, scrub(e.content), {k: len(v) if isinstance(v, list) else 1
                                        for k, v in e.links.items()})
            for e in store.list(10_000, order="asc")]


# ------------------------------------------------------------------ existence


class TestQuestionExistence:
    def test_bare_exposure_never_asks(self, tmp_path):
        """The first open has NO connection evidence — no question, even with a
        certain roll (question_prob=1.0). 看到 ≠ 留下印象；留下印象 ≠ 产生问题."""
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: T0)
        eng = _engine(store, _params())
        out = _open(eng, T0.isoformat(), "w_pasta_science")
        assert out["result"] == "opened"
        assert out["question_id"] is None
        assert store.count("question.created") == 0

    def test_tension_question_from_two_unintegrated_encounters(self, tmp_path):
        """A second world experience of connected material (the prior impression
        never became a thought) leaves a TENSION question, evidence-linked to
        both encounters."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        first = _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        assert second["thought_id"] is None          # the thought path was off
        assert second["question_kind"] == "tension"
        q = store.get(second["question_id"])
        assert q is not None and q.type == "question.created"
        assert q.content["kind"] == "tension"
        # provenance: caused by the second observation; evidence = the prior
        # impression + the first observation (both real, both resolvable)
        assert q.links["caused_by"] == [second["observation_id"]]
        assert first["impression_id"] in q.links["related_to"]
        assert first["observation_id"] in q.links["related_to"]
        # the text is grounded: names both encounters
        assert "面团的筋度从何而来" in q.content["question"]
        assert "鲜味的化学基础" in q.content["question"]

    def test_gap_question_from_own_old_memory(self, tmp_path):
        """World material meeting the resident's OWN memory (no prior world
        impression) leaves a GAP question — the world↔own-memory kind."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        store.append(EventCreate(
            type="experience.created", visibility="private",
            content={"text": "我今晚揉了意面面团，醒面一小时之后筋度完全不一样。"},
            provenance={"source": "test"},
        ))
        eng = _engine(store, _params(assoc_min_sim=0.01))
        out = _open(eng, clock.now.isoformat(), "w_pasta_science")
        assert out["question_kind"] == "gap"
        q = store.get(out["question_id"])
        assert q.content["kind"] == "gap"
        assert q.links["related_to"], "gap evidence must be linked"
        assoc = store.get(q.links["related_to"][0])
        assert assoc.type == "experience.created"    # own memory, not world echo
        assert "意面面团" in q.content["question"]

    def test_tension_wins_over_gap(self, tmp_path):
        """Both candidates present → exactly one question, the TENSION one
        (world↔world is the more specific structure)."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params(assoc_min_sim=0.01))
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        # the own memory appears BETWEEN the two encounters: now open2 has both
        # a prior un-promoted impression AND an own-memory association
        store.append(EventCreate(
            type="experience.created", visibility="private",
            content={"text": "我今晚揉了意面面团，醒面一小时之后筋度完全不一样。"},
            provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        assert second["question_kind"] == "tension"
        assert store.count("question.created") == 1

    def test_prob_zero_mechanism_not_quota(self, tmp_path):
        """question_prob=0.0 with candidates present → zero questions. The roll
        is the mechanism; nothing is forced and nothing is quota-fed."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params(question_prob=0.0))
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        _open(eng, clock.now.isoformat(), "w_umami")
        assert store.count("question.created") == 0

    def test_thought_wins_question_is_exclusive(self, tmp_path):
        """An open that forms a THOUGHT does not also form a question (the
        question is the residue of an *unthought* connection)."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params(promotion_prob=1.0))
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        assert second["thought_id"] is not None
        assert second["question_id"] is None
        assert store.count("question.created") == 0

    def test_deep_cap_blocks_questions_too(self, tmp_path):
        """A capped mind stops engaging with the material — satiety includes
        not asking: a cap-blocked open leaves an impression, not a question."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params(max_deep_per_day=0))
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        assert second["thought_id"] is None
        assert second["question_id"] is None
        assert store.count("question.created") == 0


# ------------------------------------------------------------------ constraints


class TestOwnerConstraints:
    def test_provenance_complete_and_resolvable(self, tmp_path):
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        q = store.get(second["question_id"])
        # exact key sets — nothing extra (no scores, no answers)
        assert set(q.content.keys()) == {"question", "kind", "topic", "item_id", "source", "status"}
        assert set(q.metadata.keys()) == {"route", "origin", "mode", "question"}
        assert set(q.provenance.keys()) == {"source", "route", "question_kind"}
        assert q.content["status"] == "open"  # status AT CREATION; current state is derived
        # every link resolves; the store's invariant makes fabrication impossible
        for eid in q.links["caused_by"] + q.links["related_to"]:
            assert store.get(eid) is not None
        with pytest.raises(ValueError, match="dangling"):
            store.append(EventCreate(
                type="question.created",
                content={"question": "fake", "kind": "tension"},
                links={"related_to": ["evt_does_not_exist"]},
            ))

    def test_no_self_rating_anywhere(self, tmp_path):
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        q = store.get(second["question_id"])

        def _no_floats(obj):
            if isinstance(obj, float):
                return False
            if isinstance(obj, dict):
                return all(_no_floats(v) for v in obj.values())
            if isinstance(obj, (list, tuple)):
                return all(_no_floats(v) for v in obj)
            return True

        assert _no_floats(q.content) and _no_floats(q.metadata) and _no_floats(q.provenance)
        state = question_state(store)
        assert all("importance" not in i and "curiosity" not in i and "score" not in i
                   for i in state["questions"])

    def test_no_answer_pipeline(self, tmp_path):
        """Nothing in the log is caused by a question except a re-meeting; no
        fetch/window/conversation ever follows from one."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        qid = second["question_id"]
        clock.now = clock.now + timedelta(hours=3)
        # the third open runs with a declined roll (prob 0.0) so it can only
        # re-meet the existing question, not mint a new one
        quiet = _engine(store, _params(question_prob=0.0), seed=3)
        third = _open(quiet, clock.now.isoformat(), "w_gluten_free")
        assert qid in third["questions_revisited"]
        for e in store.list(10_000, order="asc"):
            caused = e.links.get("caused_by") or []
            if isinstance(caused, str):
                caused = [caused]
            if qid in caused:
                assert e.type == "question.revisited"
        q = store.get(qid)
        assert "answer" not in q.content and "answered" not in q.content
        # and the mind itself never writes questions (only the world chain does)
        memory = MemoryRetrieval(store)
        mind = MindLoop(store, memory=memory, provider=DeterministicFakeProvider())
        for _ in range(4):
            clock.now = clock.now + timedelta(minutes=30)
            asyncio.run(mind.wake_once("circadian"))
        assert store.count("question.created") == 1

    def test_remeeting_records_revisit_and_extends_life(self, tmp_path):
        """A later independent experience on the same topic re-meets the open
        question (constraint 4); the revisit resets the dormancy clock."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        qid = second["question_id"]
        clock.now = clock.now + timedelta(days=5)
        third = _open(eng, clock.now.isoformat(), "w_gluten_free")
        assert qid in third["questions_revisited"]
        rv = [e for e in store.list(100, type_prefix="question.revisited")]
        assert len(rv) == 1
        assert rv[0].links["related_to"] == [qid]
        assert rv[0].links["caused_by"] == [third["observation_id"]]
        assert rv[0].content["reason"] == "world re-encounter"
        state = question_state(store, now_iso=(T0 + timedelta(days=11)).isoformat())
        row = next(i for i in state["questions"] if i["question_id"] == qid)
        assert row["revisits"] == 1 and row["status"] == "open"  # 6d since the re-meeting < 7d
        state2 = question_state(store, now_iso=(T0 + timedelta(days=13)).isoformat())
        row2 = next(i for i in state2["questions"] if i["question_id"] == qid)
        assert row2["status"] == "dormant"

    def test_dormancy_is_derived_never_written(self, tmp_path):
        """Death leaves no trace in the log: the count is identical before and
        after the derived read, and a dormant question is not re-met."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        _open(eng, clock.now.isoformat(), "w_umami")
        before = store.count()
        state = question_state(store, now_iso=(T0 + timedelta(days=8)).isoformat())
        assert state["n_total"] == 1 and state["n_dormant"] == 1 and state["n_open"] == 0
        assert store.count() == before, "dormancy must never be written as an event"
        # the window cannot resurrect: a later food open touches nothing
        clock.now = T0 + timedelta(days=8, hours=1)
        out = _open(eng, clock.now.isoformat(), "w_gluten_free")
        assert out["questions_revisited"] == []
        assert store.count("question.revisited") == 0


# ------------------------------------------------------------------ sealed & shaped


class TestSealedAndShaped:
    def test_default_off_is_sealed_behaviour(self, tmp_path):
        """Without the flag the layer does not exist: no question events, no
        extra return keys, all-zero telemetry — even where candidates exist."""
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        params = WorldParams(base_propensity=1.0, propensity_cap=1.0, cooldown_s=0.0,
                             impression_prob=1.0, promotion_prob=0.0, association_prob=0.0)
        eng = _engine(store, params)
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        assert "question_id" not in second and "questions_revisited" not in second
        assert store.count("question.") == 0
        prof = world_profile(store)
        assert prof["world_to_question_rate"] == 0.0
        assert all(v == 0 or v == {} or v == 0.0 for v in prof["questions"].values())

    def test_telemetry_reads_the_layer(self, tmp_path):
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        eng = _engine(store, _params())
        _open(eng, clock.now.isoformat(), "w_pasta_science")
        clock.now = clock.now + timedelta(hours=3)
        second = _open(eng, clock.now.isoformat(), "w_umami")
        _ = second
        prof = world_profile(store, now_iso=(T0 + timedelta(hours=4)).isoformat())
        assert prof["world_to_question_rate"] == 0.5   # 1 question / 2 opens
        assert prof["questions"]["n_questions"] == 1
        assert prof["questions"]["kind_distribution"] == {"tension": 1}
        assert prof["questions"]["question_survival_rate"] == 0.0
        assert prof["questions"]["open_now"] == 1 and prof["questions"]["dormant_now"] == 0

    def test_question_state_empty_store(self, tmp_path):
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: T0)
        state = question_state(store)
        assert state == {"questions": [], "n_total": 0, "n_open": 0, "n_dormant": 0}

    def test_deterministic_same_seed_same_life(self, tmp_path):
        def run(root: str):
            clock = Clock()
            store = EventStore(tmp_path / root, now_fn=lambda: clock.now)
            eng = _engine(store, _params(), seed=7)
            _open(eng, clock.now.isoformat(), "w_pasta_science")
            clock.now = clock.now + timedelta(hours=3)
            _open(eng, clock.now.isoformat(), "w_umami")
            clock.now = clock.now + timedelta(hours=4)
            _open(eng, clock.now.isoformat(), "w_gluten_free")
            return _log_projection(store)

        assert run("a.sqlite3") == run("b.sqlite3")

    def test_orchestrator_beats_never_crash_with_questions_on(self, tmp_path):
        clock = Clock()
        store = EventStore(tmp_path / "q.sqlite3", now_fn=lambda: clock.now)
        memory = MemoryRetrieval(store)
        mind = MindLoop(store, memory=memory, provider=DeterministicFakeProvider())
        sleep_engine = SleepEngine(store, memory=memory)
        eng = _engine(store, _params())
        orch = CircadianOrchestrator(store, mind, sleep_engine, world_engine=eng)
        for _ in range(8):
            clock.now = clock.now + timedelta(minutes=30)
            res = asyncio.run(orch.beat(clock.now.isoformat()))
            assert "state" in res
        for e in store.list(10_000, type_prefix="question.created"):
            obs = store.get((e.links.get("caused_by") or [None])[0])
            assert obs is not None and obs.type == "world.observation"
