"""Derived mental-state views, rebuilt from the event log.

Thoughts, questions, and threads are persisted *as events*
(``thought.created``, ``question.created``, ``thread.*``); this module
rebuilds the current view from history. Derived views are rebuildable —
never the source of truth (ADR-0001).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .event_store import EventStore
from .models import Event

THREAD_STATES = ("created", "activated", "dormant", "closed")


@dataclass
class ThreadView:
    thread_id: str
    title: str = ""
    state: str = "created"
    last_event_id: str | None = None
    last_event_at: str = ""
    event_ids: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state == "activated"

    @property
    def dormant(self) -> bool:
        return self.state == "dormant"


@dataclass
class QuestionView:
    question_id: str
    text: str
    thread_id: str | None
    status: str = "open"
    created_event_id: str = ""
    resolved_event_id: str | None = None


class MentalState:
    """Point-in-time view of Resident's durable mental state."""

    def __init__(self, events: list[Event]):
        # process oldest → newest
        self.threads: dict[str, ThreadView] = {}
        self.questions: dict[str, QuestionView] = {}
        self.thought_events: list[Event] = []
        self.question_events: list[Event] = []
        for e in sorted(events, key=lambda x: (x.seq or 0, x.created_at)):
            self._apply(e)

    @classmethod
    def from_store(cls, store: EventStore) -> "MentalState":
        return cls(store.list(1000, order="asc"))

    def _apply(self, e: Event) -> None:
        t = e.type
        if t == "thread.created":
            tid = e.content.get("thread_id")
            if tid:
                self.threads[tid] = ThreadView(
                    thread_id=tid,
                    title=e.content.get("title", ""),
                    state="created",
                    last_event_id=e.id,
                    last_event_at=e.created_at,
                    event_ids=[e.id],
                )
        elif t in ("thread.activated", "thread.dormant", "thread.closed", "thread.revisited"):
            tid = e.content.get("thread_id") or e.links.get("thread_id")
            state = {"thread.activated": "activated", "thread.dormant": "dormant",
                     "thread.closed": "closed", "thread.revisited": "activated"}[t]
            v = self.threads.get(tid)
            if v is None:
                # thread referenced without a created event: derive a minimal view
                v = ThreadView(thread_id=tid, title=e.content.get("title", tid), state="created")
                self.threads[tid] = v
            v.state = state
            v.last_event_id = e.id
            v.last_event_at = e.created_at
            v.event_ids.append(e.id)
        elif t == "thought.created":
            self.thought_events.append(e)
            self._touch_thread(e, e)
        elif t == "question.created":
            qid = e.content.get("question_id")
            if qid:
                self.questions[qid] = QuestionView(
                    question_id=qid,
                    text=e.content.get("text", ""),
                    thread_id=e.content.get("thread_id") or e.links.get("thread_id"),
                    status="open",
                    created_event_id=e.id,
                )
                self.question_events.append(e)
                self._touch_thread(e, e)
        elif t in ("question.resolved", "question.abandoned"):
            qid = e.content.get("question_id")
            q = self.questions.get(qid)
            if q:
                q.status = "resolved" if t == "question.resolved" else "abandoned"
                q.resolved_event_id = e.id
            self._touch_thread(e, e)
        elif t in ("experience.created", "conversation.user_message"):
            self._touch_thread(e, e)

    def _touch_thread(self, e: Event, _source: Event) -> None:
        """Record that event e touched its thread. Events arrive oldest→newest,
        so the latest touch simply becomes the thread's last activity."""
        tid = e.links.get("thread_id") or e.content.get("thread_id")
        if tid and tid in self.threads:
            v = self.threads[tid]
            if e.id not in v.event_ids:
                v.event_ids.append(e.id)
            v.last_event_id = e.id
            v.last_event_at = e.created_at

    # ---------------------------------------------------------------- views

    @property
    def active_threads(self) -> list[ThreadView]:
        return [v for v in self.threads.values() if v.active]

    @property
    def dormant_threads(self) -> list[ThreadView]:
        return [v for v in self.threads.values() if v.dormant]

    @property
    def open_questions(self) -> list[QuestionView]:
        return [q for q in self.questions.values() if q.status == "open"]

    def current_thoughts(self, n: int = 10) -> list[Event]:
        """The most recent n thoughts, newest first."""
        return list(reversed(self.thought_events[-n:]))

    def recent_life(self, n: int = 15) -> list[Event]:
        """Recent durable, non-system events — what prompt assembly shows the model."""
        events = [e for e in sorted(
            self.thought_events + self.question_events, key=lambda x: (x.seq or 0, x.created_at)
        ) if e.visibility in ("private", "shareable", "user_visible")]
        return list(reversed(events))[-n:]

    def mind_brief(self) -> str:
        """One-line deterministic summary of current state (for prompt assembly)."""
        parts = []
        if self.active_threads:
            t = self.active_threads[0]
            parts.append(f"活跃线程「{t.title or t.thread_id}」")
        if self.open_questions:
            q = self.open_questions[0]
            parts.append(f"未解问题「{q.text[:30]}」")
        if self.thought_events:
            parts.append(f"最近的想法「{self.thought_events[-1].text[:30]}」")
        return "；".join(parts) if parts else "（还没有什么值得说的）"
