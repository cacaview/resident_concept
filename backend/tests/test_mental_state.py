"""Tests for MentalState: derived thread/thought/question views rebuilt
from the event log (deterministic, restart-safe by construction).

Events here are built with explicit id/created_at/seq so ordering is
fully controlled (the MentalState sort key is ``(seq, created_at)``).

``test_current_thoughts_window_returns_most_recent_n`` guards a previously
buggy window (it returned the oldest n thoughts); now fixed in
``mental_state.py``.
"""
from __future__ import annotations

from pathlib import Path

from resident.event_store import EventStore
from resident.mental_state import MentalState, QuestionView, ThreadView
from resident.models import Event, EventCreate


def _e(seq: int, type: str, *, content: dict | None = None, links: dict | None = None) -> Event:
    """Build an Event with deterministic id / created_at / seq."""
    return Event(
        type=type,
        actor="resident",
        visibility="private",
        content=content or {},
        links=links or {},
        id=f"evt_{seq:04d}",
        created_at=f"2026-01-01T00:{seq:02d}:00+00:00",
        seq=seq,
    )


# ------------------------------------------------------------------ threads

def test_thread_lifecycle_state_transitions():
    e1 = _e(1, "thread.created", content={"thread_id": "thr_1", "title": "睡眠"})
    e2 = _e(2, "thread.activated", content={"thread_id": "thr_1"})
    e3 = _e(3, "thread.dormant", content={"thread_id": "thr_1"})
    e4 = _e(4, "thread.revisited", content={"thread_id": "thr_1"})
    e5 = _e(5, "thread.closed", content={"thread_id": "thr_1"})

    # created
    ms = MentalState([e1])
    assert ms.threads["thr_1"].state == "created"
    assert ms.active_threads == [] and ms.dormant_threads == []

    # activated
    ms = MentalState([e1, e2])
    assert ms.threads["thr_1"].state == "activated"
    assert [v.thread_id for v in ms.active_threads] == ["thr_1"]
    assert ms.dormant_threads == []

    # dormant: an activated-then-dormant thread is dormant, NOT active
    ms = MentalState([e1, e2, e3])
    assert ms.threads["thr_1"].state == "dormant"
    assert ms.active_threads == []
    assert [v.thread_id for v in ms.dormant_threads] == ["thr_1"]

    # revisited re-activates
    ms = MentalState([e1, e2, e3, e4])
    assert ms.threads["thr_1"].state == "activated"
    assert [v.thread_id for v in ms.active_threads] == ["thr_1"]

    # closed: neither active nor dormant
    ms = MentalState([e1, e2, e3, e4, e5])
    assert ms.threads["thr_1"].state == "closed"
    assert ms.active_threads == []
    assert ms.dormant_threads == []


def test_thread_event_ids_and_last_event_tracking():
    e1 = _e(1, "thread.created", content={"thread_id": "thr_1", "title": "T"})
    e2 = _e(2, "thread.activated", content={"thread_id": "thr_1"})
    ms = MentalState([e1, e2])
    v = ms.threads["thr_1"]
    assert v.title == "T"
    assert v.event_ids == [e1.id, e2.id]
    assert v.last_event_id == e2.id
    assert v.last_event_at == e2.created_at


def test_thread_referenced_without_created_yields_minimal_view():
    e = _e(1, "thread.dormant", content={"thread_id": "thr_2"})
    ms = MentalState([e])
    assert "thr_2" in ms.threads
    v = ms.threads["thr_2"]
    assert isinstance(v, ThreadView)
    assert v.state == "dormant"
    assert v.dormant is True and v.active is False
    # title falls back to the thread id
    assert v.title == "thr_2"
    assert [x.thread_id for x in ms.dormant_threads] == ["thr_2"]


# ----------------------------------------------------------------- thoughts

def test_current_thoughts_newest_first():
    a = _e(1, "thought.created", content={"text": "first thought"})
    b = _e(2, "thought.created", content={"text": "second thought"})
    c = _e(3, "thought.created", content={"text": "third thought"})
    ms = MentalState([a, b, c])
    # with at most n thoughts, the default window shows all of them, newest first
    assert [e.id for e in ms.current_thoughts()] == [c.id, b.id, a.id]
    assert [e.id for e in ms.current_thoughts(n=10)] == [c.id, b.id, a.id]


def test_current_thoughts_window_returns_most_recent_n():
    a = _e(1, "thought.created", content={"text": "first"})
    b = _e(2, "thought.created", content={"text": "second"})
    c = _e(3, "thought.created", content={"text": "third"})
    d = _e(4, "thought.created", content={"text": "fourth"})
    ms = MentalState([a, b, c, d])
    # the 2 most recent thoughts, newest first
    assert [e.id for e in ms.current_thoughts(n=2)] == [d.id, c.id]


def test_empty_thoughts():
    ms = MentalState([])
    assert ms.current_thoughts() == []


def test_thought_with_thread_link_updates_thread_last_event():
    t = _e(1, "thread.created", content={"thread_id": "thr_1", "title": "T"})
    other = _e(2, "thought.created", content={"text": "unrelated thought"})
    linked = _e(3, "thought.created", content={"text": "about the thread"}, links={"thread_id": "thr_1"})
    ms = MentalState([t, other, linked])
    v = ms.threads["thr_1"]
    assert v.last_event_id == linked.id
    assert v.last_event_at == linked.created_at
    assert linked.id in v.event_ids
    # the unlinked thought must not have touched the thread
    assert other.id not in v.event_ids


# ---------------------------------------------------------------- questions

def test_question_created_is_open():
    q = _e(1, "question.created", content={"question_id": "q_1", "text": "为什么我总在三点醒来？"})
    ms = MentalState([q])
    assert "q_1" in ms.questions
    view = ms.questions["q_1"]
    assert isinstance(view, QuestionView)
    assert view.status == "open"
    assert view.text == "为什么我总在三点醒来？"
    assert view.created_event_id == q.id
    assert [x.question_id for x in ms.open_questions] == ["q_1"]


def test_question_resolved_excluded_from_open():
    q = _e(1, "question.created", content={"question_id": "q_1", "text": "why?"})
    r = _e(2, "question.resolved", content={"question_id": "q_1"})
    ms = MentalState([q, r])
    assert ms.questions["q_1"].status == "resolved"
    assert ms.questions["q_1"].resolved_event_id == r.id
    assert ms.open_questions == []


def test_question_abandoned_like_resolved():
    q = _e(1, "question.created", content={"question_id": "q_1", "text": "why?"})
    a = _e(2, "question.abandoned", content={"question_id": "q_1"})
    ms = MentalState([q, a])
    assert ms.questions["q_1"].status == "abandoned"
    assert ms.questions["q_1"].resolved_event_id == a.id
    assert ms.open_questions == []


def test_question_mixed_lifecycle_only_resolved_and_abandoned_close():
    q1 = _e(1, "question.created", content={"question_id": "q_1", "text": "one"})
    q2 = _e(2, "question.created", content={"question_id": "q_2", "text": "two"})
    r = _e(3, "question.resolved", content={"question_id": "q_1"})
    a = _e(4, "question.abandoned", content={"question_id": "q_2"})
    ms = MentalState([q1, q2])
    assert {q.question_id for q in ms.open_questions} == {"q_1", "q_2"}
    ms = MentalState([q1, q2, r])
    assert [q.question_id for q in ms.open_questions] == ["q_2"]
    ms = MentalState([q1, q2, r, a])
    assert ms.open_questions == []


# ---------------------------------------------------------------- mind_brief

def test_mind_brief_default_when_state_empty():
    ms = MentalState([])
    assert ms.mind_brief() == "（还没有什么值得说的）"


def test_mind_brief_nonempty_for_active_thread_open_question_and_thought():
    t = _e(1, "thread.activated", content={"thread_id": "thr_1", "title": "睡眠"})
    q = _e(2, "question.created", content={"question_id": "q_1", "text": "一个未解的问题"})
    th = _e(3, "thought.created", content={"text": "一个最近的想法"})
    ms = MentalState([t, q, th])
    brief = ms.mind_brief()
    assert isinstance(brief, str)
    assert brief != "（还没有什么值得说的）"
    assert "活跃线程" in brief and "睡眠" in brief
    assert "未解问题" in brief
    assert "最近的想法" in brief


def test_mind_brief_single_component():
    th = _e(1, "thought.created", content={"text": "只有想法"})
    ms = MentalState([th])
    brief = ms.mind_brief()
    assert brief == f"最近的想法「只有想法」"


# ------------------------------------------------------------- determinism

def test_rebuild_determinism_from_store(tmp_path: Path):
    store = EventStore(tmp_path / "state.db")
    store.append(EventCreate(type="thread.created", content={"thread_id": "thr_1", "title": "T"}))
    store.append(EventCreate(type="thread.activated", content={"thread_id": "thr_1"}))
    store.append(EventCreate(type="thought.created", content={"text": "x"}, links={"thread_id": "thr_1"}))
    store.append(EventCreate(type="question.created", content={"question_id": "q_1", "text": "q"}))
    store.append(EventCreate(type="question.resolved", content={"question_id": "q_1"}))

    def snapshot(ms: MentalState) -> dict:
        return {
            "threads": {
                tid: (v.title, v.state, v.last_event_id, v.last_event_at, tuple(v.event_ids))
                for tid, v in ms.threads.items()
            },
            "questions": {
                qid: (q.text, q.status, q.thread_id, q.created_event_id, q.resolved_event_id)
                for qid, q in ms.questions.items()
            },
            "thoughts": [e.id for e in ms.current_thoughts()],
        }

    m1 = MentalState.from_store(store)
    m2 = MentalState.from_store(store)
    assert snapshot(m1) == snapshot(m2)
    # and the views are sane
    assert snapshot(m1)["threads"]["thr_1"][1] == "activated"
    assert snapshot(m1)["questions"]["q_1"][1] == "resolved"
