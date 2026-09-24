"""Tests for MemoryRetrieval (recency, keyword, semantic, topic signals).

The "recent" window tests (``test_recent_returns_most_recent_n_...``,
``test_topic_concentration_reflects_recent_window``,
``test_has_recent_window_boundary_...``) guard a previously-buggy behavior:
the window was implemented as ``store.list(n, order="asc")`` (the OLDEST n
events) instead of the most recent n. Fixed in ``memory.py``; these tests
now pass and would regress if the window direction were flipped again.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import EventCreate


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "memory.db")


@pytest.fixture()
def mem(store: EventStore) -> MemoryRetrieval:
    return MemoryRetrieval(store)


# ------------------------------------------------------------------- recent

def test_recent_oldest_first_and_excludes_system(mem: MemoryRetrieval, store: EventStore):
    e1 = store.append(EventCreate(type="conversation.user_message", content={"text": "first"}))
    store.append(EventCreate(type="system.heartbeat", content={"note": "beat"}, visibility="system"))
    e3 = store.append(EventCreate(type="thought.created", content={"text": "second"}))
    e4 = store.append(EventCreate(type="thought.created", content={"text": "third"}))

    out = mem.recent(10)
    # oldest first, system event excluded
    assert [e.id for e in out] == [e1.id, e3.id, e4.id]
    assert all(e.visibility != "system" for e in out)


def test_recent_empty_store_returns_empty(mem: MemoryRetrieval):
    assert mem.recent(10) == []


def test_recent_returns_most_recent_n_when_store_larger_than_n(tmp_path: Path):
    store = EventStore(tmp_path / "memory.db")
    oldest = store.append(
        EventCreate(type="conversation.user_message", content={"text": "old message"})
    )
    for i in range(11):
        store.append(EventCreate(type="thought.created", content={"text": f"t{i}"}))

    out = MemoryRetrieval(store).recent(10)
    # the most recent 10 of 12 events are seq 3..12; the oldest event
    # (seq 1) must fall OUTSIDE the recent window
    assert all(e.id != oldest.id for e in out)
    assert [e.seq for e in out] == list(range(3, 13))


# ------------------------------------------------------------------ keyword

def test_keyword_case_insensitive_substring_over_type_and_text(mem: MemoryRetrieval, store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "I SLEEP too much lately"}))
    store.append(EventCreate(type="wake.started", content={"text": "no matching term here"}))
    c = store.append(EventCreate(type="thought.created", content={"text": "sleep cycles are interesting"}))

    hits = mem.keyword("sleep")
    assert {e.id for e in hits} == {a.id, c.id}
    # hits are returned oldest first
    assert [e.id for e in hits] == [a.id, c.id]
    # case-insensitive
    assert [e.id for e in mem.keyword("SLEEP")] == [a.id, c.id]


def test_keyword_matches_event_type(mem: MemoryRetrieval, store: EventStore):
    w = store.append(EventCreate(type="wake.started", content={"text": "a plain cycle"}))
    t = store.append(EventCreate(type="thought.created", content={"text": "wake me not"}))
    # 'wake' appears in the TYPE of the first event and in the TEXT of the second
    hits = mem.keyword("wake")
    assert {e.id for e in hits} == {w.id, t.id}


def test_keyword_empty_term_returns_empty(mem: MemoryRetrieval, store: EventStore):
    store.append(EventCreate(type="thought.created", content={"text": "anything"}))
    assert mem.keyword("") == []
    assert mem.keyword("   ") == []


# ----------------------------------------------------------------- semantic

def test_semantic_ranks_lexical_overlap_first(mem: MemoryRetrieval, store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "sleep and dreams"}))
    store.append(EventCreate(type="thought.created", content={"text": "eat noodles for dinner"}))

    out = mem.semantic("sleep")
    assert out, "expected at least one lexically overlapping event"
    assert out[0].id == a.id
    assert a.id in [e.id for e in out]


def test_semantic_no_overlap_returns_empty_without_raising(mem: MemoryRetrieval, store: EventStore):
    store.append(EventCreate(type="thought.created", content={"text": "eat noodles for dinner"}))
    assert mem.semantic("zzzqqq") == []
    # a query with no tokens at all must not raise either
    assert mem.semantic("!!! ???") == []


def test_semantic_empty_store_returns_empty(mem: MemoryRetrieval):
    assert mem.semantic("anything") == []


def test_semantic_excludes_system_events(mem: MemoryRetrieval, store: EventStore):
    store.append(
        EventCreate(type="system.heartbeat", content={"text": "sleep monitor"}, visibility="system")
    )
    assert mem.semantic("sleep") == []


# ---------------------------------------------------------- topic_concentration

def test_topic_concentration_all_conversation_is_one(mem: MemoryRetrieval, store: EventStore):
    for i in range(5):
        store.append(EventCreate(type="conversation.user_message", content={"text": f"m{i}"}))
    assert mem.topic_concentration() == 1.0


def test_topic_concentration_evenly_mixed_families_below_0_7(mem: MemoryRetrieval, store: EventStore):
    for t in (
        "conversation.user_message",
        "conversation.user_message",
        "thought.created",
        "thought.created",
        "exploration.created",
        "exploration.created",
        "wake.started",
        "wake.started",
    ):
        store.append(EventCreate(type=t, content={"text": "x"}))
    assert mem.topic_concentration() < 0.7


def test_topic_concentration_empty_store_is_zero(mem: MemoryRetrieval):
    assert mem.topic_concentration() == 0.0


def test_topic_concentration_reflects_recent_window(tmp_path: Path):
    store = EventStore(tmp_path / "memory.db")
    for i in range(45):
        store.append(EventCreate(type="conversation.user_message", content={"text": f"m{i}"}))
    for i in range(5):
        store.append(EventCreate(type="exploration.created", content={"text": f"x{i}"}))

    mem = MemoryRetrieval(store)
    # most recent 45 = 40 conversation + 5 exploration -> 40/45, not 1.0
    assert mem.topic_concentration(45) == pytest.approx(40 / 45)


# ----------------------------------------------------------------- has_recent

def test_has_recent_true_and_false_cases(mem: MemoryRetrieval, store: EventStore):
    store.append(EventCreate(type="thought.created", content={"text": "t"}))
    assert mem.has_recent("conversation.user_message") is False
    assert mem.has_recent("thought") is True

    store.append(EventCreate(type="conversation.user_message", content={"text": "m"}))
    assert mem.has_recent("conversation.user_message") is True


def test_has_recent_excludes_system_events(mem: MemoryRetrieval, store: EventStore):
    for _ in range(10):
        store.append(EventCreate(type="system.heartbeat", content={}, visibility="system"))
    assert mem.has_recent("system", n_recent=10) is False


def test_has_recent_window_boundary_excludes_older_message(tmp_path: Path):
    store = EventStore(tmp_path / "memory.db")
    store.append(EventCreate(type="conversation.user_message", content={"text": "old"}))
    for i in range(11):
        store.append(EventCreate(type="thought.created", content={"text": f"t{i}"}))

    mem = MemoryRetrieval(store)
    assert mem.has_recent("conversation.user_message", n_recent=10) is False
    # control: a wider window that does include the message must be True
    assert mem.has_recent("conversation.user_message", n_recent=12) is True
