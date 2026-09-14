"""Tests for behavioral telemetry (ADR-0004).

All metrics are structural reads over the log — these tests assert the
metrics respond correctly to constructed histories (no fabricated signal).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.telemetry import Telemetry, _normalized_entropy


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "tel.db")


def _entropy_counts():
    assert _normalized_entropy([5]) == 0.0
    assert _normalized_entropy([0, 0]) == 0.0
    # uniform over 4 bins -> 1.0
    assert _normalized_entropy([1, 1, 1, 1]) == 1.0
    # one dominant bin -> low
    assert _normalized_entropy([97, 1, 1, 1]) < 0.5


def test_route_entropy_monotone_vs_diverse(store):
    mono = EventStore(store.path.parent / "mono.db")
    div = EventStore(store.path.parent / "div.db")
    for _ in range(10):
        mono.append(EventCreate(type="wake.route_selected", visibility="system", content={"route": "rest"}))
    for r in ("continuity", "revisit", "distant", "rest", "self", "personal"):
        for _ in range(5):
            div.append(EventCreate(type="wake.route_selected", visibility="system", content={"route": r}))
    tel_mono = Telemetry(mono).snapshot()
    tel_div = Telemetry(div).snapshot()
    assert tel_mono["route_entropy"] == 0.0
    assert tel_div["route_entropy"] > 0.7
    assert tel_mono["revisit_rate"] == 0.0
    assert tel_div["revisit_rate"] == pytest.approx(5 / 30, abs=0.01)


def test_no_op_ratio(store):
    for _ in range(2):
        store.append(EventCreate(type="wake.started", visibility="system", content={}))
        store.append(EventCreate(type="wake.noop", visibility="system", content={}))
    store.append(EventCreate(type="wake.started", visibility="system", content={}))
    snap = Telemetry(store).snapshot()
    assert snap["no_op_ratio"] == pytest.approx(2 / 3, abs=0.01)
    assert snap["n_wakes"] == 3


def test_user_topic_dependency_high_when_thoughts_cite_user(store):
    store.append(EventCreate(type="thread.created", content={"thread_id": "t", "title": "t"}))
    u = store.append(EventCreate(type="conversation.user_message", actor="user", visibility="user_visible", content={"text": "hi"}))
    store.append(EventCreate(type="thought.created", content={"text": "reacting to you"}, links={"related_to": [u.id]}))
    # a thought citing only its own material
    own = store.append(EventCreate(type="thought.created", content={"text": "my own thread"}, links={"thread_id": "t"}))
    store.append(EventCreate(type="thought.created", content={"text": "also mine"}, links={"related_to": [own.id]}))
    snap = Telemetry(store).snapshot()
    # 1 of 2 thoughts-with-links cites the user
    assert snap["user_topic_dependency"] == pytest.approx(0.5, abs=0.01)
    assert snap["user_topic_dependency_base"] == 2


def test_user_topic_dependency_zero_when_autonomous(store):
    store.append(EventCreate(type="thought.created", content={"text": "a"}, links={"related_to": []}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    store.append(EventCreate(type="thought.created", content={"text": "c"}, links={"related_to": [b.id]}))
    snap = Telemetry(store).snapshot()
    assert snap["user_topic_dependency"] == 0.0


def test_thread_resurrection_rate(store):
    store.append(EventCreate(type="thread.created", content={"thread_id": "t", "title": "t"}))
    store.append(EventCreate(type="thread.dormant", visibility="private", content={"thread_id": "t"}))
    store.append(EventCreate(type="thread.revisited", visibility="private", content={"thread_id": "t"}))
    snap = Telemetry(store).snapshot()
    assert snap["thread_resurrection_rate"] == pytest.approx(1.0, abs=0.01)


def test_memory_age_at_activation_recorded(store):
    mem = MemoryRetrieval(store)
    old = store.append(EventCreate(type="thought.created", content={"text": "old"}))
    store.append(EventCreate(type="thought.created", content={"text": "new, using old"}, links={"related_to": [old.id]}))
    mem.index.rebuild()
    snap = Telemetry(store, mem.index).snapshot()
    assert snap["memory_age_at_activation"]["count"] >= 1
    assert snap["memory_age_at_activation"]["mean"] >= 0.0


def test_snapshot_has_all_expected_keys(store):
    snap = Telemetry(store).snapshot()
    for key in (
        "route_distribution", "route_entropy", "topic_entropy", "revisit_rate",
        "thread_resurrection_rate", "no_op_ratio", "user_topic_dependency",
        "memory_age_at_activation",
    ):
        assert key in snap
