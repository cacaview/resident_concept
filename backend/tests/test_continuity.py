"""v0.2 Step 4 (ADR-0012): Continuity across absence — hermetic tests.

The five owner constraints, mechanically:
1. absence is a FACT (gap duration + boundary ids; no sentiment fields);
2. re-entry ≠ summary (candidate texts are quotes of real evidence);
3. only state-changing experiences qualify (each class tested; plain wakes /
   skips / fetch failures never become candidates);
4. silence on return is first-class (no candidates → noop; candidates but no
   contextual recall → noop);
5. "happened" vs "worth mentioning now" are separate judgments — eligibility is
   structural, relevance is the return message's OWN retrieval footprint
   (deterministic; a different message selects a different past).

Distractor events pad the stores below so ``semantic_near``'s top-k actually
ranks — on a tiny store every event fits in top-k and nothing discriminates.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from resident.continuity import ContinuityEngine, ContinuityParams
from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.providers import HashingEmbeddingProvider
from resident.world_sim import ARM_PARAMS, run_world_simulation

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0


def _store(tmp_path, clock, name="c.sqlite3"):
    return EventStore(tmp_path / name, now_fn=lambda: clock.now)


def _engine(store, **param_over) -> ContinuityEngine:
    params = ContinuityParams(absence_threshold_s=4 * 3600.0, **param_over)
    return ContinuityEngine(store, memory=MemoryRetrieval(store), params=params)


def _user_msg(store, text):
    return store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": text}, provenance={"source": "test"},
    ))


def _distractors(store, n=12):
    """Unrelated life events so top-k retrieval must rank, not return-everything."""
    topics = ("排版网格的基线节奏", "行星吸积的原行星盘", "真菌菌丝的网络信号",
              "古道上贸易网络的扩散", "衬线字体的收刀痕迹", "熵增与恒星燃烧",
              "菌根共生的磷交换", "口述传统的韵律保存", "网格系统的 gutter",
              "原行星盘的角动量", "短链脂肪酸与菌群", "无衬线字体的场景选择")
    for i in range(n):
        store.append(EventCreate(
            type="experience.created", visibility="private",
            content={"summary": f"留意到{topics[i % len(topics)]}的资料（{i}）。"},
            provenance={"source": "test"},
        ))


def _seed_two_candidates(store):
    """Two unrelated state-changing experiences inside one gap: a pasta question
    and a guitar thought — the raw material for the context-sensitivity test."""
    store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "周末想做意面。"}, provenance={"source": "test"},
    ))
    obs = store.append(EventCreate(
        type="world.observation", visibility="private",
        content={"title": "面团的筋度从何而来", "topic": "food",
                 "summary": "面筋由麦谷蛋白与醇溶蛋白吸水交联形成，决定意面的嚼劲。"},
        provenance={"source": "test"},
    ))
    q = store.append(EventCreate(
        type="question.created", visibility="private",
        content={"question": "「无麸质面食的取舍」和「面团的筋度从何而来」是同一个话题。"
                             "两次经历摆在一起，为什么它们会同时成立？",
                 "kind": "tension", "topic": "food", "status": "open"},
        metadata={"question": {"kind": "tension", "evidence": [obs.id]}},
        provenance={"source": "test"},
    ))
    store.append(EventCreate(
        type="question.revisited", visibility="private",
        content={"question_id": q.id, "reason": "world re-encounter"},
        links={"related_to": [q.id]}, provenance={"source": "test"},
    ))
    store.append(EventCreate(
        type="experience.created", visibility="private",
        content={"summary": "练了一小时吉他，和声进行还是卡。"},
        provenance={"source": "test"},
    ))
    th = store.append(EventCreate(
        type="thought.created", visibility="private",
        content={"text": "吉他和声的功能推进让我想起练琴时手指自己记得位置。"},
        metadata={"origin": "self_thread", "route": "self",
                  # a real thought carries the recall it grew from
                  "recalled": [store.list(1, type_prefix="experience.created",
                                          order="desc")[0].id]},
        provenance={"source": "test"},
    ))
    return q, th


class TestAbsenceFacts:
    def test_short_gap_is_the_same_conversation(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "在忙什么？")
        clock.now = clock.now + timedelta(hours=1)
        u2 = _user_msg(store, "继续刚才的话")
        out = eng.on_user_return(u2)
        assert out["absence"] is None
        assert store.count("absence.detected") == 0
        assert store.count("reentry.") == 0

    def test_absence_event_is_pure_fact(self, tmp_path):
        """Constraint 1: only the duration and the boundary events — exact key
        sets, no sentiment, no prose."""
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        u1 = _user_msg(store, "我先去开会了")
        clock.now = clock.now + timedelta(hours=7, minutes=13)
        u2 = _user_msg(store, "我回来了")
        out = eng.on_user_return(u2)
        a = store.get(out["absence"]["event_id"])
        assert a.type == "absence.detected"
        assert abs(a.content["gap_s"] - (7 * 3600 + 13 * 60)) < 1.0
        assert set(a.content.keys()) == {"gap_s", "since_event_id", "until_event_id"}
        assert a.links["related_to"] == [u1.id] and a.links["caused_by"] == [u2.id]

    def test_first_ever_message_is_no_absence(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        u = _user_msg(store, "你好")
        assert eng.on_user_return(u)["absence"] is None


class TestCandidates:
    def test_question_revisited_class(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        q = store.append(EventCreate(
            type="question.created", visibility="private",
            content={"question": "为什么面团会筋度不一样？", "kind": "tension",
                     "topic": "food", "status": "open"},
            provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=2)
        rv = store.append(EventCreate(
            type="question.revisited", visibility="private",
            content={"question_id": q.id, "reason": "world re-encounter"},
            links={"related_to": [q.id]}, provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=8)
        u2 = _user_msg(store, "我回来了")
        out = eng.on_user_return(u2)
        assert "question_revisited" in [c["cls"] for c in out["candidates"]]
        row = next(e for e in store.list(50, type_prefix="reentry.candidate")
                   if e.content["cls"] == "question_revisited")
        assert q.id in row.links["related_to"] and rv.id in row.links["related_to"]
        assert "面团" in row.content["quote"]  # a quote of real evidence, not prose

    def test_impression_thread_and_world_classes(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        obs = store.append(EventCreate(
            type="world.observation", visibility="private",
            content={"title": "记忆的巩固与重激活", "topic": "memory",
                     "summary": "睡眠中海马体会将新记忆反复重放。"},
            provenance={"source": "test"},
        ))
        imp = store.append(EventCreate(
            type="impression.formed", visibility="private",
            content={"impression": "对「记忆的巩固」有了印象", "topic": "memory"},
            links={"related_to": [obs.id]}, provenance={"source": "test"},
        ))
        store.append(EventCreate(
            type="impression.promoted", visibility="private",
            content={"impression_id": imp.id, "reason": "reactivated by re-encounter"},
            links={"related_to": [imp.id]}, provenance={"source": "test"},
        ))
        store.append(EventCreate(
            type="thread.revisited", visibility="private",
            content={"thread_id": "thr_pasta", "reason": "revival"},
            provenance={"source": "test"},
        ))
        store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": "睡眠重放和记忆巩固连起来了。"},
            metadata={"origin": "world", "recalled": [obs.id]},
            provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=10)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        clses = {c["cls"] for c in out["candidates"]}
        assert {"impression_collision", "thread_revisited", "world_changed_mind"} <= clses

    def test_user_derived_thought_and_noise_are_not_candidates(self, tmp_path):
        """Constraint 3's negative side: a user-derived thought grows from the
        user, not from the absence; plain bookkeeping is not a life change."""
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": "关于 deadline 的想法。"},
            metadata={"origin": "user_recent"},
            provenance={"source": "test"},
        ))
        store.append(EventCreate(
            type="world.window_skipped", visibility="system",
            content={"blocker": "propensity_below_threshold"},
            provenance={"source": "test"},
        ))
        store.append(EventCreate(
            type="world.fetch_failed", visibility="system",
            content={"reason": "network_error"},
            provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=9)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        assert out["candidates"] == []
        assert out["decision"] == "noop"
        assert store.count("reentry.noop") == 1

    def test_went_dormant_class(self, tmp_path):
        """A question open at the gap's left edge, dormant by the return: the
        absence itself is when it died. (Last touch 6d20h before the gap
        starts → still open there; 7d2h before the return → dormant now.)"""
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        q = store.append(EventCreate(
            type="question.created", visibility="private",
            content={"question": "为什么练习和记忆会互相抢地方？", "kind": "gap",
                     "topic": "memory", "status": "open"},
            provenance={"source": "test"},
        ))
        rv = store.append(EventCreate(
            type="question.revisited", visibility="private",
            content={"question_id": q.id, "reason": "world re-encounter"},
            links={"related_to": [q.id]}, provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(days=6, hours=20)
        u1 = _user_msg(store, "先走了")
        clock.now = clock.now + timedelta(hours=6)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        assert "went_dormant" in [c["cls"] for c in out["candidates"]]
        assert rv.created_at < u1.created_at  # the touch predates the gap: it faded during it


class TestSelection:
    def test_context_selects_different_past(self, tmp_path):
        """Constraint 5 / the experiment's core: same absence, same internal
        life, a different return message reaches a different past."""
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store, retrieval_k=4)
        _user_msg(store, "先走了，晚上聊")
        _seed_two_candidates(store)
        _distractors(store)
        clock.now = clock.now + timedelta(hours=8)

        u2 = _user_msg(store, "意面到底为什么筋度不一样？")
        out_food = eng.on_user_return(u2)
        food_clses = {c["cls"] for c in out_food["selected"]}
        assert "question_revisited" in food_clses
        assert "self_thought_extended" not in food_clses  # the guitar chain is not alive here

        # the music-shaped message, against the SAME eligibility set (a second
        # absence event is not needed — selection alone is under test)
        pure = _engine(store, retrieval_k=4)
        u3 = _user_msg(store, "吉他和声的功能推进怎么练？")
        cands = pure._candidates(
            store.list(20, type_prefix="conversation.user_message", order="asc")[0],
            u2, 8 * 3600)
        music_sel = pure._select(u3, cands)
        assert any(c["cls"] == "self_thought_extended" for c in music_sel)
        assert not any(c["cls"] == "question_revisited" for c in music_sel)

    def test_message_matching_nothing_degrades_to_silence(self, tmp_path):
        """Constraint 4: eligibility is structural, but selection is contextual
        — decision always matches what the message actually reached."""
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store, retrieval_k=4)
        _user_msg(store, "先走了")
        _seed_two_candidates(store)
        _distractors(store, n=20)
        clock.now = clock.now + timedelta(hours=8)
        u2 = _user_msg(store, "我回来了")
        out = eng.on_user_return(u2)
        assert (out["decision"] == "selected") == (len(out["selected"]) > 0)
        assert store.count("reentry.selected") + store.count("reentry.noop") == 1

    def test_no_candidates_at_all_noops(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        clock.now = clock.now + timedelta(hours=9)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        assert out["decision"] == "noop" and out["candidates"] == []

    def test_retrieval_outage_degrades_to_noop(self, tmp_path):
        """An embedding outage never crashes re-entry; it degrades to silence
        with the structural facts still recorded."""
        clock = Clock()
        store = _store(tmp_path, clock)
        _user_msg(store, "先走了")
        _seed_two_candidates(store)
        clock.now = clock.now + timedelta(hours=8)
        u2 = _user_msg(store, "意面为什么筋度不一样？")

        class _Failing(HashingEmbeddingProvider):
            def embed(self, texts):
                from resident.semantic import EmbeddingUnavailable
                raise EmbeddingUnavailable("provider down")

        eng = ContinuityEngine(store, memory=MemoryRetrieval(store, embedder=_Failing()))
        out = eng.on_user_return(u2)
        assert out["decision"] == "noop"
        assert out["candidates"], "eligibility is structural, independent of retrieval"
        assert store.count("absence.detected") == 1


class TestShape:
    def test_candidates_are_quotes_not_summaries(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": "自origin的一句话，原样引用。"},
            metadata={"origin": "self_thread"}, provenance={"source": "test"},
        ))
        clock.now = clock.now + timedelta(hours=9)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        cand = next(c for c in out["candidates"] if c["cls"] == "self_thought_extended")
        assert cand["quote"].startswith("自origin的一句话")

    def test_candidate_cap_is_a_rail(self, tmp_path):
        clock = Clock()
        store = _store(tmp_path, clock)
        eng = _engine(store)
        _user_msg(store, "先走了")
        for i in range(12):
            store.append(EventCreate(
                type="thought.created", visibility="private",
                content={"text": f"自己的第{i}个想法。"},
                metadata={"origin": "self_thread"}, provenance={"source": "test"},
            ))
        clock.now = clock.now + timedelta(hours=9)
        u2 = _user_msg(store, "回来了")
        out = eng.on_user_return(u2)
        assert len(out["candidates"]) == 12
        assert store.count("reentry.candidate") == 8  # the rail, not a quota target

    def test_deterministic(self, tmp_path):
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

        def run(name: str):
            clock = Clock()
            store = _store(tmp_path, clock, name=name)
            eng = _engine(store, retrieval_k=4)
            _user_msg(store, "先走了")
            _seed_two_candidates(store)
            clock.now = clock.now + timedelta(hours=8)
            u2 = _user_msg(store, "意面筋度是为什么？")
            eng.on_user_return(u2)
            return [(e.type, scrub(e.content)) for e in store.list(200, order="asc")]

        assert run("a.sqlite3") == run("b.sqlite3")

    @pytest.mark.asyncio
    async def test_sim_integration_default_off_and_on(self, tmp_path):
        # default off: no continuity events at all
        rep = await run_world_simulation(tmp_path / "off", days=3, seed=0,
                                         world_params=ARM_PARAMS["C_all"])
        assert rep["continuity"]["n_absences"] == 0
        # on: the layer runs, facts exist, every absence reaches a decision
        rep_on = await run_world_simulation(
            tmp_path / "on", days=3, seed=0, world_params=ARM_PARAMS["C_all"],
            questions=True, continuity=True)
        assert rep_on["continuity"]["n_absences"] >= 1
        assert (rep_on["continuity"]["n_selected"] + rep_on["continuity"]["n_noop"]
                == rep_on["continuity"]["n_absences"])
