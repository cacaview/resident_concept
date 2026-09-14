"""ADR-0016 — Private Projects: hermetic tests.

Owner constraints, mechanically:

1. genesis is DERIVED, structural, in-mind — the H1 detector computed from the
   log (≥2 revisits caused by world.observation, ≥2 distinct sources, ≥7d span,
   ending in self-thought activity, provenance independent of conversation);
   no numeric curiosity/importance/motivation float anywhere;
2. a negative stays a negative — a single-source chain (the observed H1=0
   situation) yields NO project, ever;
3. lifecycle — created → active → dormant is derived (no tombstone event);
   later chain re-engagement revives (derived again);
4. artifacts re-enter the experience system as world-independent experiences
   (retrievable, recallable, re-entry-eligible) and consume NO world slot;
5. flag off = byte-identical behaviour (no event, no shape change).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.monitor import monitor_projects
from resident.private_projects import (
    DEFAULT_PROJECT_DORMANT_S,
    check_genesis,
    genesis_candidates,
    project_state,
    record_artifact,
)
from resident.reentry import ReentryEngine

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self):
        self.now = T0


def _store(tmp_path) -> tuple[EventStore, Clock]:
    clock = Clock()
    # a real file: EventStore opens a fresh connection per operation, so an
    # in-memory db would not persist between them
    return EventStore(tmp_path / "events.sqlite3", now_fn=lambda: clock.now), clock


def _obs(store, clock, *, source: str, topic: str = "memory", days: float = 0.0):
    clock.now = T0 + timedelta(days=days)
    return store.append(EventCreate(
        type="world.observation", visibility="private",
        content={"title": f"{topic} {source}", "source": source, "topic": topic,
                 "text": f"关于{topic}的材料（{source}）"},
        provenance={"source": "world_window"},
    ))


def _question(store, clock, *, evidence, days: float):
    clock.now = T0 + timedelta(days=days)
    ev_ids = [e.id for e in evidence]
    return store.append(EventCreate(
        type="question.created", visibility="private",
        content={"question": "为什么？", "kind": "tension", "topic": "memory",
                 "status": "open"},
        links={"caused_by": [ev_ids[-1]], "related_to": ev_ids},
        provenance={"source": "world_window"},
        metadata={"question": {"kind": "tension", "evidence": ev_ids}},
    ))


def _revisit(store, clock, *, question_id, caused_by, days: float):
    clock.now = T0 + timedelta(days=days)
    return store.append(EventCreate(
        type="question.revisited", visibility="private",
        content={"question_id": question_id, "topic": "memory",
                 "reason": "world re-encounter"},
        links={"caused_by": [caused_by.id], "related_to": [question_id]},
        provenance={"source": "world_window"},
    ))


def _self_thought(store, clock, *, recalled, days: float):
    clock.now = T0 + timedelta(days=days)
    return store.append(EventCreate(
        type="thought.created", visibility="private",
        content={"text": "[self] 这件事我还在想"},
        links={"related_to": list(recalled), "caused_by": [recalled[0]]},
        provenance={"source": "mind_loop", "route": "self"},
        metadata={"route": "self", "recalled": list(recalled),
                  "origin": "self_thread"},
    ))


def _genesis_chain(store, clock, *, sources=("a", "b")):
    """A full H1 chain: two sources, two revisits, 8-day span, self-thought."""
    o1 = _obs(store, clock, source=sources[0], days=0)
    o2 = _obs(store, clock, source=sources[1], days=1)
    q = _question(store, clock, evidence=[o1, o2], days=1.01)
    o3 = _obs(store, clock, source=sources[0], days=3)
    o4 = _obs(store, clock, source=sources[1], days=8)
    _revisit(store, clock, question_id=q.id, caused_by=o3, days=3.01)
    _revisit(store, clock, question_id=q.id, caused_by=o4, days=8.01)
    t = _self_thought(store, clock, recalled=[o1.id, q.id], days=9)
    return q, t


# ------------------------------------------------------------------- genesis


def test_genesis_detected_on_synthetic_chain(tmp_path):
    store, clock = _store(tmp_path)
    q, _t = _genesis_chain(store, clock)
    cands = genesis_candidates(store)
    assert len(cands) == 1
    c = cands[0]
    assert c["question_id"] == q.id
    assert c["revisits"] >= 2
    assert c["span_days"] >= 7.0
    assert len(c["sources"]) >= 2


def test_single_source_chain_is_no_genesis(tmp_path):
    """The observed H1=0 situation: same-source evidence pairs never count."""
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock, sources=("nasa", "nasa"))
    assert genesis_candidates(store) == []


def test_conversation_taint_kills_genesis(tmp_path):
    """Provenance independence: evidence from the conversation family fails."""
    store, clock = _store(tmp_path)
    o1 = _obs(store, clock, source="a", days=0)
    clock.now = T0 + timedelta(days=0.5)
    user_msg = store.append(EventCreate(
        type="conversation.user_message", visibility="user_visible",
        content={"text": "帮我看看这个", "topic": "memory"},
    ))
    _obs(store, clock, source="b", days=1)
    q = _question(store, clock, evidence=[o1, user_msg], days=1.01)
    o3 = _obs(store, clock, source="a", days=3)
    o4 = _obs(store, clock, source="b", days=8)
    _revisit(store, clock, question_id=q.id, caused_by=o3, days=3.01)
    _revisit(store, clock, question_id=q.id, caused_by=o4, days=8.01)
    _self_thought(store, clock, recalled=[user_msg.id, q.id], days=9)
    assert genesis_candidates(store) == []


def test_no_score_anywhere_in_events(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)
    for e in store.list(1000, order="asc"):
        blob = json.dumps({**e.content, **e.metadata, **e.provenance},
                          ensure_ascii=False)
        for banned in ("curiosity", "importance", "motivation", "priority", "score"):
            assert banned not in blob, (e.type, banned)


def test_genesis_emission_idempotent_and_flag_gated(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)
    n0 = store.count()
    # flag off (default): byte-identical — nothing is appended
    assert check_genesis(store) == []
    assert store.count() == n0
    # flag on: one project with full provenance
    created = check_genesis(store, enabled=True)
    assert len(created) == 1
    p = store.get(created[0]["project_id"])
    assert p.type == "project.created"
    assert p.provenance["criterion"] == "h1_genesis_chain"
    assert p.provenance["genesis"]["span_days"] >= 7.0
    assert store.count() == n0 + 1
    # idempotent: a second pass is a no-op
    assert check_genesis(store, enabled=True) == []
    assert store.count() == n0 + 1


# ----------------------------------------------------------------- lifecycle


def test_dormancy_is_derived_and_revival_reactivates(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)
    clock.now = T0 + timedelta(days=10)
    check_genesis(store, enabled=True)

    # at day 10 (activity at day 9): active
    st = project_state(store, now_iso=(T0 + timedelta(days=10)).isoformat())
    assert st["n_total"] == 1 and st["n_active"] == 1

    # day 30, untouched for > dormant_s: DORMANT — derived only, no new event
    n = store.count()
    st = project_state(store, now_iso=(T0 + timedelta(days=30)).isoformat())
    assert st["n_dormant"] == 1
    assert store.count() == n  # no tombstone event, ever

    # revival: a later world re-encounter re-mets the question (the question-
    # revival semantics: the engine does not resurrect — the re-encounter does)
    o5 = _obs(store, clock, source="c", days=31)
    _revisit(store, clock, question_id=st["projects"][0]["question_id"],
             caused_by=o5, days=31.01)
    st2 = project_state(store, now_iso=(T0 + timedelta(days=31.1)).isoformat())
    assert st2["n_active"] == 1


def test_default_dormancy_window_is_an_envelope_rail(tmp_path):
    assert DEFAULT_PROJECT_DORMANT_S == 14 * 86400.0


def test_monitor_projects_is_read_only(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)
    check_genesis(store, enabled=True)
    record_artifact(store, project_id=store.list(
        1, type_prefix="project.created")[0].id, text="一份笔记", caused_by=[])
    before = [(e.id, e.type, e.seq) for e in store.list(1000, order="asc")]
    view = monitor_projects(store)
    after = [(e.id, e.type, e.seq) for e in store.list(1000, order="asc")]
    assert before == after
    assert view["summary"]["total"] == 1
    assert view["summary"]["artifacts"] == 1
    assert view["projects"][0]["status"] == "active"


# ----------------------------------------------------------------- artifacts


def test_artifact_reenters_experience_and_recall(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)
    check_genesis(store, enabled=True)
    p = store.list(1, type_prefix="project.created")[0]
    a = record_artifact(store, project_id=p.id, text="我整理了一份关于记忆的多源对照笔记",
                        caused_by=[])
    art = store.get(a["artifact_id"])
    assert art.type == "artifact.created"
    assert art.visibility == "private"
    assert p.id in (art.links.get("related_to") or [])
    # NO world-observation slot consumed: the budget semantics are recorded,
    # and the artifact is not a world event
    assert art.provenance["world_budget_slot_consumed"] is False
    assert art.family == "artifact"

    mem = MemoryRetrieval(store)
    # retrievable as a world-independent experience (semantic path)
    hits = mem.semantic_near("记忆 多源 对照 笔记", n=5)
    assert any(h.event.id == art.id for h in hits)
    # keyword search finds it too
    assert any(e.id == art.id for e in mem.keyword("多源对照"))

    # and it is re-entry material ("我做出了一个东西"), with valid provenance.
    # Like a private thought, the artifact surfaces only through the justified
    # promotion path: a LATER durable reflection must genuinely re-engage it.
    clock.now = T0 + timedelta(days=10)
    store.append(EventCreate(
        type="thought.created", visibility="private",
        content={"text": "[revisit] 那份笔记我想再改一改"},
        links={"related_to": [art.id], "caused_by": [art.id]},
        provenance={"source": "mind_loop", "route": "revisit"},
        metadata={"route": "revisit", "recalled": [art.id],
                  "origin": "private_project"},
    ))
    engine = ReentryEngine(store, mem)
    promoted = engine.promote(since=(T0 + timedelta(days=1)).isoformat())
    assert any(p["source_event_id"] == art.id for p in promoted)
    res = engine.evaluate(since=(T0 + timedelta(days=1)).isoformat())
    claims = [c["claim"] for c in res["candidates"]]
    assert any(c.startswith("我做出了一个东西") for c in claims)
    for c in res["candidates"]:
        assert engine.validate_candidate(c)


def test_artifact_requires_a_real_project(tmp_path):
    store, _ = _store(tmp_path)
    with pytest.raises(ValueError):
        record_artifact(store, project_id="evt_nope", text="x")


# ------------------------------------------------- mind-loop wiring (flagged)


def _scrub(o):
    """Random ids (uuid4) differ between two identical runs; nothing else may."""
    import re
    if isinstance(o, str):
        return re.sub(r"(evt|wake)_[0-9a-f]{8,}", r"\1_X", o)
    if isinstance(o, dict):
        return {k: _scrub(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_scrub(v) for v in o]
    return o


def _same_stream(a: EventStore, b: EventStore) -> bool:
    ka = [(e.type, e.created_at, e.visibility, _scrub(e.content),
           _scrub(e.provenance)) for e in a.list(1000, order="asc")]
    kb = [(e.type, e.created_at, e.visibility, _scrub(e.content),
           _scrub(e.provenance)) for e in b.list(1000, order="asc")]
    return ka == kb


def _wakes(store, *, n=3, private_projects=False):
    import asyncio
    import random as _random

    from resident.mind_loop import MindLoop
    from resident.providers import DeterministicFakeProvider
    loop = MindLoop(store, memory=MemoryRetrieval(store),
                    provider=DeterministicFakeProvider(),
                    rng=_random.Random(7), force_route="self",
                    private_projects=private_projects)
    for _ in range(n):
        asyncio.run(loop.wake_once("test"))


def test_flag_off_wake_is_byte_identical_to_sealed(tmp_path):
    a, _clock_a = _store(tmp_path / "a")
    b, _clock_b = _store(tmp_path / "b")
    _wakes(a)                              # sealed behaviour
    _wakes(b, private_projects=True)       # flag on, but NO genesis chain
    assert _same_stream(a, b)              # nothing to emit → identical
    assert b.count("project.created") == 0


def test_flag_on_wake_emits_project_after_genesis(tmp_path):
    store, clock = _store(tmp_path)
    _genesis_chain(store, clock)           # a full H1 chain is in the log
    n0 = store.count()
    _wakes(store, n=1, private_projects=True)
    assert store.count("project.created") == 1
    assert store.count() >= n0 + 1
    st = project_state(store)
    assert st["n_total"] == 1 and st["n_active"] == 1
