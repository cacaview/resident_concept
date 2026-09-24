"""Thought-origin classification (ADR-0004): *where* does each thought come from?

Phase 3's core question is "after it wakes, **who/what** decides what appears in
Resident's head?". Every persistent thought is tagged with a PRIMARY origin so
the independence profile is measurable: is the mind mostly growing from the
user's recent input, or from its own threads / old memories / chance?

Categories
----------
- ``user_recent``     — grows from a RECENT (≤ RECENT_WINDOW) user interaction;
- ``user_old``        — grows from an OLDER, user-seeded thread not recently
                        touched (still user-derived, just not the fresh pull);
- ``self_thread``     — grows from Resident's OWN self-originated thread
                        (its independent material — the independence signal);
- ``private_project`` — grows from a private project/artifact it is working on;
- ``memory_revival``  — a deliberate re-encounter with old/distant material;
- ``serendipity``     — a chance boundary sample from under-visited memory;
- ``world``           — external world (unbound in v0.1 → never emitted; kept so
                        the category is measurable the moment world is bound).

The classification is **deterministic and grounded**: it is derived from the
route taken, the thread the thought follows (and that thread's *origin* —
``user`` vs ``self``, recorded on ``thread.created``), and the recalled
material's provenance. It is never asked of the model and never invented. The
origin is stored on the thought's ``metadata["origin"]`` at persist time.

Independence is NOT forced by a quota: the ``self_thread`` category can only
fill up if Resident actually accumulates its own threads (seeded by sleep
consolidation, see ``sleep.py``) and the state-driven route policy actually
continues them. That is the whole point — the metric reveals whether the mind
has its own material, not whether we dialled one in.
"""
from __future__ import annotations

from datetime import datetime

from .models import Event

RECENT_WINDOW_S = 24 * 3600  # "recent" user input = within the last day (virtual time)

#: the seven origin labels (order = report order, most user-bound first)
THOUGHT_ORIGINS: tuple[str, ...] = (
    "user_recent",
    "user_old",
    "self_thread",
    "private_project",
    "memory_revival",
    "serendipity",
    "world",
)

# Origins that are Resident's own material (not derived from the user at all).
SELF_ORIGINS: frozenset[str] = frozenset({"self_thread", "private_project"})
# Origins that are user-derived (any recency).
USER_ORIGINS: frozenset[str] = frozenset({"user_recent", "user_old"})

_PROJECT_FAMILIES = ("project", "artifact")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def thread_origin(store, thread_id: str | None) -> str:
    """``"self"`` iff the thread was self-originated (``thread.created`` with
    ``content.origin == "self"``); otherwise ``"user"`` (the default — every
    conversation-seeded thread, live or simulated, is user-derived)."""
    if not thread_id:
        return "user"
    for e in store.list(1000, type_prefix="thread.created"):
        if e.content.get("thread_id") == thread_id:
            return e.content.get("origin", "user")
    return "user"


def _thread_recently_user_touched(store, thread_id: str, now_iso: str | None) -> bool:
    """True iff the newest user interaction tied to this thread is within the
    recent window. Distinguishes a live user topic from a stale one."""
    now = _parse_iso(now_iso)
    if now is None:
        return False
    newest: datetime | None = None
    for e in store.list(1000, order="asc"):
        if (
            e.links.get("thread_id") == thread_id
            and e.type in ("conversation.user_message", "experience.created")
        ):
            t = _parse_iso(e.created_at)
            if t is not None and (newest is None or t > newest):
                newest = t
    return newest is not None and (now - newest).total_seconds() <= RECENT_WINDOW_S


def _recall_has_self_thread(store, recalled) -> bool:
    for h in recalled:
        tid = h.event.links.get("thread_id") or h.event.content.get("thread_id")
        if tid and thread_origin(store, tid) == "self":
            return True
    return False


def _dominant_path(recalled) -> str:
    """The most common retrieval path among the recalled hits (ties: first)."""
    if not recalled:
        return ""
    counts: dict[str, int] = {}
    for h in recalled:
        counts[h.path] = counts.get(h.path, 0) + 1
    return max(counts, key=lambda p: (counts[p], -_path_order(p)))


def _path_order(p: str) -> int:
    # stable deterministic tie-break order
    order = {"serendipity": 0, "distant": 1, "forgotten": 2, "revisit": 3,
             "semantic_near": 4, "temporal": 5}
    return order.get(p, 9)


def classify_origin(
    store,
    *,
    route: str,
    thread_id: str | None,
    recalled,
    now_iso: str | None,
) -> str:
    """The PRIMARY origin of a thought about to be persisted (deterministic)."""
    # 1. world: unbound, but keep the label so it is measurable later.
    if route == "world":
        return "world"
    # 2. routes with an explicit anchor — the anchor decides.
    if route == "personal":
        return "user_recent"
    if route == "self":
        return "self_thread"
    if route in ("continuity", "revisit") and thread_id:
        if thread_origin(store, thread_id) == "self":
            return "self_thread"
        if _thread_recently_user_touched(store, thread_id, now_iso):
            return "user_recent"
        return "user_old"
    # 3. unanchored (distant / serendipity, or a no-thread continuity/revisit):
    #    classify by the material the thought is actually built from.
    if any(h.event.family in _PROJECT_FAMILIES for h in recalled):
        return "private_project"
    if _recall_has_self_thread(store, recalled):
        return "self_thread"
    if _dominant_path(recalled) == "serendipity":
        return "serendipity"
    return "memory_revival"
