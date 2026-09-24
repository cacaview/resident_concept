"""Tests for the associative memory paths (ADR-0004).

Covers: the six retrieval paths, the multi_recall anti-degenerate-top-k
guarantee, and the rebuildable MemoryIndex (retrieval/activation stats).
"""
from __future__ import annotations

import random
from pathlib import Path

import pytest

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import Event, EventCreate
from resident.memory_index import MemoryIndex


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "mem.db")


@pytest.fixture()
def mem(store: EventStore) -> MemoryRetrieval:
    return MemoryRetrieval(store)


def _seed_varied(store: EventStore):
    """Old distinct topic (distant/forgotten material) + recent topic + thread."""
    # old, distinctive topic "pasta" — will sit outside the recent window
    pasta_ids = [
        store.append(EventCreate(type="thought.created", content={"text": f"cooking pasta recipe {i}"}))
        for i in range(4)
    ]
    # a dormant thread with old "quantum" material
    store.append(EventCreate(type="thread.created", content={"thread_id": "thr_q", "title": "quantum"}))
    store.append(EventCreate(type="thread.dormant", visibility="private", content={"thread_id": "thr_q"}, links={"thread_id": "thr_q"}))
    q_ids = [
        store.append(EventCreate(type="thought.created", content={"text": f"quantum superposition {i}"}, links={"thread_id": "thr_q"}))
        for i in range(4)
    ]
    # recent, dominating topic "work" — fills the recent window
    work_ids = [
        store.append(EventCreate(type="thought.created", content={"text": f"work deadline project {i}"}))
        for i in range(12)
    ]
    return pasta_ids, q_ids, work_ids


def test_semantic_near_ranks_overlap_and_is_deterministic(mem, store):
    _seed_varied(store)
    out = mem.semantic_near("quantum superposition", 3)
    assert out, "expected overlap hits"
    assert all("quantum" in h.event.text for h in out)
    # deterministic for a given store
    out2 = mem.semantic_near("quantum superposition", 3)
    assert [h.id for h in out] == [h.id for h in out2]


def test_semantic_near_no_overlap_returns_empty(mem, store):
    store.append(EventCreate(type="thought.created", content={"text": "eat noodles for dinner"}))
    assert mem.semantic_near("zzzqqq") == []
    assert mem.semantic_near("!!! ???") == []


def test_distant_returns_events_far_from_recent_focus(mem, store):
    _seed_varied(store)
    # recent window is dominated by "work"; distant should surface the far "pasta"
    out = mem.distant(5)
    texts = [h.event.text for h in out]
    assert any("pasta" in t for t in texts), f"distant should find far material, got {texts}"
    # none of the recent-window "work" events should be returned (they are the focus)
    assert not any(t.startswith("work deadline") for t in texts)


def test_forgotten_surfaces_old_rarely_recalled(mem, store):
    _seed_varied(store)
    out = mem.forgotten(5)
    assert out, "expected forgotten hits"
    # the top forgotten hit should be an OLD event (pasta or quantum), not a recent work one
    top = out[0].event
    assert not top.text.startswith("work deadline"), f"top forgotten should be old, got {top.text}"


def test_revisit_reaches_thread_and_old_query(mem, store):
    _seed_varied(store)
    via_thread = mem.revisit(thread_id="thr_q", n=3)
    assert via_thread and all(h.path == "revisit" for h in via_thread)
    assert any("quantum" in h.event.text for h in via_thread)
    via_query = mem.revisit(query="pasta recipe", n=3)
    assert any("pasta" in h.event.text for h in via_query)


def test_serendipity_deterministic_per_seed_and_samples(mem, store):
    _seed_varied(store)
    a = [h.id for h in mem.serendipity(3, rng=random.Random(7))]
    b = [h.id for h in mem.serendipity(3, rng=random.Random(7))]
    assert a == b, "serendipity must be deterministic for a fixed seed"
    assert 0 < len(a) <= 3


def test_multi_recall_spans_multiple_paths(mem, store):
    """The structural anti-degenerate-top-k guarantee: a multi-path recall
    must draw from more than one retrieval path when the store has material
    for several of them."""
    _seed_varied(store)
    out = mem.multi_recall("quantum superposition", paths=("semantic_near", "revisit", "distant", "forgotten"), n_each=3)
    paths = {h.path for h in out}
    assert len(paths) >= 2, f"multi_recall collapsed onto one path: {paths}"
    # no duplicate events in the merged result
    ids = [h.id for h in out]
    assert len(ids) == len(set(ids))


def test_multi_recall_respects_total_cap(mem, store):
    _seed_varied(store)
    out = mem.multi_recall("work deadline project", paths=("semantic_near", "revisit", "distant", "forgotten"), n_each=4, total=5)
    assert len(out) <= 5


# --------------------------------------------------------------------- index


def test_index_tracks_retrieval_and_activation_counts(store):
    mem = MemoryRetrieval(store)
    a = store.append(EventCreate(type="thought.created", content={"text": "old idea"}))
    store.append(EventCreate(type="thought.created", content={"text": "recalling it"}, metadata={"recalled": [a.id]}))
    store.append(EventCreate(type="thought.created", content={"text": "building on it"}, links={"related_to": [a.id]}))
    mem.index.rebuild()
    assert mem.index.retrieval_count(a.id) == 1
    assert mem.index.activation_count(a.id) == 1
    assert mem.index.last_retrieved(a.id)


def test_index_rebuild_is_reproducible(store):
    mem = MemoryRetrieval(store)
    a = store.append(EventCreate(type="thought.created", content={"text": "x"}))
    store.append(EventCreate(type="thought.created", content={"text": "y"}, metadata={"recalled": [a.id]}))
    c1 = mem.index.retrieval_count(a.id)  # 0 before rebuild (hot path not called)
    mem.index.rebuild()
    assert mem.index.retrieval_count(a.id) == 1
    # rebuild again -> same
    mem.index.rebuild()
    assert mem.index.retrieval_count(a.id) == 1
    assert c1 == 0


def test_index_rebuild_includes_events_after_first_hundred_thousand():
    source = Event(type="thought.created", actor="resident", visibility="private",
                   content={"text": "source"}, id="evt_source", seq=1,
                   created_at="2026-01-01T00:00:00+00:00")
    filler = Event(type="wake.completed", actor="resident", visibility="private",
                   id="evt_filler", seq=100_000,
                   created_at="2026-01-01T00:00:00+00:00")
    recall = Event(type="wake.completed", actor="resident", visibility="private",
                   id="evt_recall", seq=100_001,
                   created_at="2026-01-02T00:00:00+00:00",
                   metadata={"recalled": [source.id]})

    class LongStore:
        def list(self, limit=100, *, order="desc"):
            return [source, *([filler] * 99_999), recall][:limit]

        def since_seq(self, seq, limit=500):
            return [recall] if seq >= 100_000 and seq < recall.seq else []

        def latest(self):
            return recall

    index = MemoryIndex(LongStore())
    assert index.retrieval_count(source.id) == 1


def test_retrieval_lowers_forgetfulness(tmp_path):
    """At equal age, a retrieved event is less forgotten (count component)."""
    from datetime import datetime, timedelta, timezone

    t = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    store = EventStore(tmp_path / "cf.db", now_fn=lambda: t["now"])
    mem = MemoryRetrieval(store)
    a = store.append(EventCreate(type="thought.created", content={"text": "retrieved idea"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "quiet idea"}))
    # age both by 5 days, and retrieve only `a`
    t["now"] += timedelta(days=5)
    store.append(EventCreate(type="thought.created", content={"text": "recall"}, metadata={"recalled": [a.id]}))
    mem.index.rebuild()
    # same age, but `a` was retrieved once and `b` never -> a is less forgotten
    assert mem.index.forgetfulness(a) < mem.index.forgetfulness(b)


def test_age_increases_forgetfulness(tmp_path):
    """Older events are more forgotten (the age component), via virtual clock."""
    from datetime import datetime, timedelta, timezone

    t = {"now": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    store = EventStore(tmp_path / "age.db", now_fn=lambda: t["now"])
    mem = MemoryRetrieval(store)
    old = store.append(EventCreate(type="thought.created", content={"text": "ancient"}))
    t["now"] += timedelta(days=20)
    recent = store.append(EventCreate(type="thought.created", content={"text": "fresh"}))
    mem.index.rebuild()
    assert mem.index.forgetfulness(old) > mem.index.forgetfulness(recent)
    assert mem.index.age_seconds(old) > mem.index.age_seconds(recent)


def test_retrieval_diversity_measures_family_spread(store):
    mem = MemoryRetrieval(store)
    store.append(EventCreate(type="thought.created", content={"text": "a"}))
    store.append(EventCreate(type="conversation.user_message", actor="user", visibility="user_visible", content={"text": "b"}))
    store.append(EventCreate(type="exploration.created", content={"text": "c"}))
    hits = mem.multi_recall("a", paths=("semantic_near", "distant", "forgotten"), n_each=2)
    # diversity over a mixed-family recall is > 0
    if len(hits) > 1:
        assert mem.retrieval_diversity(hits) > 0.0
