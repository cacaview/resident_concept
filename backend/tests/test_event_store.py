"""Tests for the append-oriented event store (foundation module).

Covers: append semantics, round-trip fidelity, read APIs (get/list/since/
since_seq/latest/count/last_user_interaction), restart persistence,
invariant violations (EventStoreError), and Event.text / Event.family.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from resident.event_store import EventStore, EventStoreError
from resident.models import Event, EventCreate

_EVENT_ID_RE = re.compile(r"^evt_[0-9a-f]{32}$")
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00$")


@pytest.fixture()
def store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "events.db")


# ------------------------------------------------------------------- append

def test_append_returns_event_with_evt_id_iso_created_at_and_seq(store: EventStore):
    e = store.append(EventCreate(type="thought.created", content={"text": "first"}))
    assert isinstance(e, Event)
    assert _EVENT_ID_RE.match(e.id)
    assert _ISO_UTC_RE.match(e.created_at)
    assert e.seq == 1

    e2 = store.append(EventCreate(type="thought.created", content={"text": "second"}))
    assert e2.seq == e.seq + 1
    assert e2.id != e.id


def test_append_round_trips_content_links_provenance_metadata(store: EventStore):
    data = EventCreate(
        type="conversation.user_message",
        actor="user",
        visibility="user_visible",
        content={"text": "今天天气很好，我想去公园散步。", "n": 42},
        links={"thread_id": "thr_1"},
        provenance={"source": "chat", "model": "fake/deterministic-v0"},
        metadata={"lang": "zh"},
    )
    e = store.append(data)
    got = store.get(e.id)
    assert got is not None
    assert got.type == data.type
    assert got.actor == data.actor
    assert got.visibility == data.visibility
    assert got.content == data.content
    assert got.links == data.links
    assert got.provenance == data.provenance
    assert got.metadata == data.metadata
    # non-ASCII text survives byte-for-byte (ensure_ascii=False storage)
    assert got.text == "今天天气很好，我想去公园散步。"
    assert got.id == e.id
    assert got.created_at == e.created_at
    assert got.seq == e.seq


# --------------------------------------------------------------------- get

def test_get_returns_stored_event_and_none_for_unknown(store: EventStore):
    e = store.append(EventCreate(type="wake.started", content={}))
    assert store.get(e.id) is not None
    assert store.get("evt_does_not_exist") is None


# -------------------------------------------------------------------- list

def test_list_default_desc_order_and_limit(store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    c = store.append(EventCreate(type="thought.created", content={"text": "c"}))

    assert [e.id for e in store.list()] == [c.id, b.id, a.id]  # newest first
    assert [e.id for e in store.list(limit=2)] == [c.id, b.id]


def test_list_asc_order(store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    assert [e.id for e in store.list(order="asc")] == [a.id, b.id]


def test_list_type_prefix_filter(store: EventStore):
    conv = store.append(EventCreate(type="conversation.user_message", content={"text": "m"}))
    store.append(EventCreate(type="thought.created", content={"text": "t"}))
    store.append(EventCreate(type="conversation.resident_reply", content={"text": "r"}))
    got = store.list(type_prefix="conversation")
    assert {e.type for e in got} <= {"conversation.user_message", "conversation.resident_reply"}
    assert conv in got or any(e.id == conv.id for e in got)
    assert not any(e.type.startswith("thought.") for e in got)


def test_list_since_is_strictly_greater(store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))

    # strict `created_at > since`: the boundary event itself is excluded
    got = store.list(since=a.created_at)
    assert all(e.id != a.id for e in got)
    # a timestamp before everything returns the full log
    assert {e.id for e in store.list(since="2000-01-01T00:00:00+00:00")} == {a.id, b.id}
    # a timestamp at/after the newest event returns nothing newer
    assert all(e.id != b.id for e in store.list(since=b.created_at))


def test_since_method_strict_and_oldest_first(store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    c = store.append(EventCreate(type="thought.created", content={"text": "c"}))
    got = store.since(a.created_at)
    assert all(e.id != a.id for e in got)  # strict >
    ids = [e.id for e in got]
    # whatever is returned is oldest-first
    assert ids == sorted(ids, key=lambda i: next(e.seq for e in got if e.id == i))
    assert c.id in ids


# --------------------------------------------------------------- since_seq

def test_since_seq_strictly_after_oldest_first(store: EventStore):
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    c = store.append(EventCreate(type="thought.created", content={"text": "c"}))

    assert [e.id for e in store.since_seq(a.seq)] == [b.id, c.id]
    assert [e.id for e in store.since_seq(0)] == [a.id, b.id, c.id]
    assert store.since_seq(c.seq) == []


# --------------------------------------------- latest / count / interaction

def test_latest_is_most_recently_appended(store: EventStore):
    assert store.latest() is None
    a = store.append(EventCreate(type="thought.created", content={"text": "a"}))
    b = store.append(EventCreate(type="thought.created", content={"text": "b"}))
    assert store.latest().id == b.id
    assert store.latest().id != a.id


def test_count_total_and_per_type_prefix(store: EventStore):
    assert store.count() == 0
    store.append(EventCreate(type="conversation.user_message", content={}))
    store.append(EventCreate(type="conversation.resident_reply", content={}))
    store.append(EventCreate(type="thought.created", content={}))
    assert store.count() == 3
    assert store.count("conversation") == 2
    assert store.count("thought") == 1
    assert store.count("nope") == 0


def test_last_user_interaction_newest_and_none_when_absent(store: EventStore):
    assert store.last_user_interaction() is None
    store.append(EventCreate(type="thought.created", content={}))
    m1 = store.append(EventCreate(type="conversation.user_message", content={"text": "one"}))
    m2 = store.append(EventCreate(type="conversation.user_message", content={"text": "two"}))
    got = store.last_user_interaction()
    assert got is not None
    assert got.id == m2.id
    assert got.content == {"text": "two"}


# -------------------------------------------------------- restart persistence

def test_restart_persistence_same_ids_and_seq(tmp_path: Path):
    path = tmp_path / "events.db"
    store = EventStore(path)
    e1 = store.append(
        EventCreate(
            type="thread.created",
            content={"thread_id": "thr_1", "title": "睡眠"},
        )
    )
    e2 = store.append(
        EventCreate(
            type="thought.created",
            content={"text": "想弄清睡眠的边界"},
            links={"thread_id": "thr_1"},
        )
    )
    del store  # simulate process exit: no explicit close needed

    reloaded = EventStore(path)
    assert reloaded.count() == 2
    got1 = reloaded.get(e1.id)
    got2 = reloaded.get(e2.id)
    assert got1 is not None and got2 is not None
    assert got1.seq == e1.seq == 1
    assert got2.seq == e2.seq == 2
    assert got1.content == e1.content
    assert got2.content == e2.content
    assert got2.links == e2.links
    assert reloaded.latest().id == e2.id
    # append after restart continues the seq
    e3 = reloaded.append(EventCreate(type="wake.started", content={}))
    assert e3.seq == 3


# ------------------------------------------------------------------ errors

def test_error_type_is_value_error():
    assert issubclass(EventStoreError, ValueError)


@pytest.mark.parametrize("bad_type", ["Bad Type", "noundot", "UPPER.case", "a.b.c", ""])
def test_append_invalid_event_type_rejected(store: EventStore, bad_type: str):
    with pytest.raises(EventStoreError):
        store.append(EventCreate(type=bad_type, content={}))


def test_append_empty_actor_rejected(store: EventStore):
    with pytest.raises(EventStoreError):
        store.append(EventCreate(type="thought.created", actor="", content={}))


def test_append_dangling_event_link_rejected(store: EventStore):
    with pytest.raises(EventStoreError):
        store.append(
            EventCreate(
                type="thought.created",
                content={"text": "x"},
                links={"related_to": ["evt_does_not_exist"]},
            )
        )
    with pytest.raises(EventStoreError):
        store.append(
            EventCreate(
                type="thought.created",
                content={"text": "x"},
                links={"caused_by": ["evt_x"]},
            )
        )
    # string form of the link is checked too
    with pytest.raises(EventStoreError):
        store.append(
            EventCreate(
                type="thought.created",
                content={"text": "x"},
                links={"related_to": "evt_missing"},
            )
        )


def test_non_event_link_ids_not_checked_and_existing_event_links_allowed(store: EventStore):
    # thread/entity ids are not events: must be allowed
    t = store.append(
        EventCreate(
            type="thread.created",
            content={"thread_id": "thr_1", "title": "T"},
            links={"thread_id": "thr_1"},
        )
    )
    assert t is not None
    # linking to an EXISTING event id (str and list form) is allowed
    ok1 = store.append(
        EventCreate(
            type="thought.created",
            content={"text": "about that thread"},
            links={"thread_id": "thr_1", "related_to": t.id},
        )
    )
    ok2 = store.append(
        EventCreate(
            type="thought.created",
            content={"text": "more"},
            links={"related_to": [t.id, ok1.id]},
        )
    )
    assert ok1 is not None and ok2 is not None
    # and the store still rejects a dangling one in the same batch shape
    with pytest.raises(EventStoreError):
        store.append(
            EventCreate(
                type="thought.created",
                content={"text": "bad"},
                links={"related_to": [t.id, "evt_never_appended"]},
            )
        )


# ------------------------------------------------------------- Event helpers

def test_event_text_property_first_non_empty_of_known_keys():
    e = Event.new(EventCreate(type="thought.created", content={"text": "the text"}))
    assert e.text == "the text"

    # whitespace-only falls through to the next key
    e2 = Event.new(EventCreate(type="thought.created", content={"text": "   ", "summary": "sum"}))
    assert e2.text == "sum"

    # key precedence is text > summary > claim > title > reply > note
    e3 = Event.new(EventCreate(type="thought.created", content={"claim": "c", "title": "t"}))
    assert e3.text == "c"
    e4 = Event.new(EventCreate(type="thought.created", content={"title": "t", "reply": "r", "note": "n"}))
    assert e4.text == "t"
    e5 = Event.new(EventCreate(type="thought.created", content={"reply": "r"}))
    assert e5.text == "r"
    e6 = Event.new(EventCreate(type="thought.created", content={"note": "n"}))
    assert e6.text == "n"

    # no known key (or empty content) -> ""
    e7 = Event.new(EventCreate(type="wake.started", content={}))
    assert e7.text == ""
    e8 = Event.new(EventCreate(type="wake.started", content={"other": "x"}))
    assert e8.text == ""


def test_event_family_property_is_type_prefix_before_dot():
    assert Event.new(EventCreate(type="conversation.user_message")).family == "conversation"
    assert Event.new(EventCreate(type="thought.created")).family == "thought"
    assert Event.new(EventCreate(type="thread.activated")).family == "thread"
