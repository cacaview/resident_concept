from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import uuid

Visibility = Literal["private", "shareable", "user_visible", "system"]

VISIBILITIES: tuple[str, ...] = ("private", "shareable", "user_visible", "system")

# v0.1 cognitive routes. Deliberately NOT a goal/task pipeline.
# `personal` and `world` exist as routes but are unbound in v0.1:
# a world wake is a legitimate no-op (no external access yet).
ROUTES: tuple[str, ...] = (
    "continuity", "revisit", "distant", "serendipity", "self", "personal", "world", "rest",
)

# Phase 2: the routes that "step away" from the current focus. When the mind
# keeps looping on one topic/route, these are the ones whose weight rises
# (route_policy.py, repetition fatigue). `rest` is the legitimate
# "nothing happened" outcome and belongs in the departure set.
DEPARTURE_ROUTES: frozenset[str] = frozenset({"revisit", "distant", "serendipity", "rest"})

# Phase 2 event types (psychological time structure — see docs/ADR-0004).
# These are new *event types*, not new storage: the log stays append-oriented
# and every one of these carries provenance + links to the real events that
# justified it (EVENT_MODEL.md).
SLEEP_STARTED = "sleep.started"          # system: a consolidation cycle begins
SLEEP_COMPLETED = "sleep.completed"      # system: result = noop | consolidated | dreamed
MEMORY_CONSOLIDATED = "memory.consolidated"   # private: a durable consolidation action
MEMORY_SHARE_CANDIDATE = "memory.share_candidate"  # shareable: a private thought promoted, with provenance
TELEMETRY_SNAPSHOT = "telemetry.snapshot"       # system: periodic behavioral metrics

# v1.0 Agency (ADR-0020): the action event family. These are OBJECTIVE deeds —
# written by the Agency (the action layer), never by the Mind. The Mind later
# reflects on them (a downstream thought/question citing them, Phase B), which
# is what separates "what happened" from "what I make of it" (owner principle 7).
# Every one is append-only and carries resolvable causal links (ADR-0001).
INTENT_PROPOSED = "intent.proposed"      # private: an intent exists (source: user | mind)
INTENT_REJECTED = "intent.rejected"      # private: the policy denied it (nothing ran)
ACTION_STARTED = "action.started"        # private: the allowed action began (non-terminal: precedes exactly one terminal)
ACTION_COMPLETED = "action.completed"    # shareable: objective record of an executed action (surfaces on re-entry)
ACTION_FAILED = "action.failed"          # private: objective record of a failed action
ACTION_TOOL_CALL = "action.tool_call"    # system: per-tool detail (optional)


class EventCreate(BaseModel):
    type: str
    actor: str = "resident"
    visibility: Visibility = "private"
    content: dict[str, Any] = Field(default_factory=dict)
    links: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Event(EventCreate):
    id: str
    created_at: str
    seq: int | None = None

    @classmethod
    def new(cls, data: EventCreate, *, created_at: str | None = None) -> "Event":
        # created_at is injectable so the accelerated simulation can run a
        # virtual timeline; the real backend leaves it None (wall-clock).
        return cls(
            **data.model_dump(),
            id=f"evt_{uuid.uuid4().hex}",
            created_at=created_at or datetime.now(timezone.utc).isoformat(),
        )

    @property
    def text(self) -> str:
        """Best-effort human-readable text of this event."""
        c = self.content
        for key in ("text", "summary", "claim", "title", "reply", "note"):
            v = c.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""

    @property
    def family(self) -> str:
        return self.type.split(".", 1)[0]


class ChatRequest(BaseModel):
    message: str
