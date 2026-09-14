"""thought-origin classification contract (Phase 3, ADR-0004).

Every persistent thought is tagged with a PRIMARY origin — the thing that
actually decided what appeared in Resident's head. This module proves the
classification is (a) complete over the seven categories, (b) grounded (it
reads real store state, never invents), and (c) the independence signal the
simulation/telemetry build on.
"""
from datetime import datetime, timedelta, timezone

from resident.event_store import EventStore
from resident.models import Event, EventCreate
from resident.origin import (
    THOUGHT_ORIGINS,
    classify_origin,
    thread_origin,
)
from resident.telemetry import Telemetry

FIXED = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
LATE = FIXED + timedelta(days=30)


class Hit:
    """Minimal RetrievalHit stand-in: origin only reads .event and .path."""

    def __init__(self, event: Event, path: str):
        self.event = event
        self.path = path


def make_store(tmp_path, name="o.sqlite3") -> EventStore:
    """A store frozen at FIXED so recency tests are deterministic.

    Populated with: a user thread (thr_user) with a recent user interaction,
    a self thread (thr_self), a project event, and an old standalone thought.
    """
    s = EventStore(tmp_path / name, now_fn=lambda: FIXED)
    # user thread + a recent user interaction tied to it
    c = s.append(EventCreate(
        type="thread.created",
        content={"thread_id": "thr_user", "title": "用户话题", "origin": "user"},
    ))
    u = s.append(EventCreate(
        type="conversation.user_message", actor="user",
        content={"text": "周末想做意面"}, links={"thread_id": "thr_user"},
    ))
    s.append(EventCreate(
        type="experience.created",
        content={"summary": "与用户谈到意面"}, links={"caused_by": [u.id], "thread_id": "thr_user"},
    ))
    # a self-originated thread (the independence material)
    c2 = s.append(EventCreate(
        type="thread.created",
        content={"thread_id": "thr_self", "title": "我自己的想法", "origin": "self"},
    ))
    s.append(EventCreate(
        type="thought.created",
        content={"text": "我顺着自己那条线想了一下。"},
        links={"thread_id": "thr_self", "caused_by": [c2.id]},
    ))
    # a project event (family "project")
    proj = s.append(EventCreate(
        type="project.updated", content={"summary": "我的私人项目推进了一点。"},
    ))
    # an old standalone thought (distant/revival material)
    old = s.append(EventCreate(
        type="thought.created", content={"text": "一段很旧的想法。"},
    ))
    s._test_fixtures = {"user_thread": "thr_user", "self_thread": "thr_self",
                        "proj": proj, "old": old}
    return s


def _recent(store, now_iso):
    return dict(thread_id="thr_user", now_iso=now_iso)


def test_seven_categories_are_the_complete_label_set():
    assert THOUGHT_ORIGINS == (
        "user_recent", "user_old", "self_thread", "private_project",
        "memory_revival", "serendipity", "world",
    )


def test_thread_origin_self_vs_user(tmp_path):
    s = make_store(tmp_path)
    assert thread_origin(s, "thr_self") == "self"
    assert thread_origin(s, "thr_user") == "user"
    assert thread_origin(s, "thr_unknown") == "user"  # unknown defaults to user
    assert thread_origin(s, None) == "user"          # no thread -> user


def test_world_route_is_world(tmp_path):
    s = make_store(tmp_path)
    assert classify_origin(s, route="world", thread_id=None, recalled=[], now_iso=FIXED.isoformat()) == "world"


def test_personal_route_is_user_recent(tmp_path):
    s = make_store(tmp_path)
    assert classify_origin(s, route="personal", thread_id=None, recalled=[], now_iso=FIXED.isoformat()) == "user_recent"


def test_self_route_is_self_thread(tmp_path):
    s = make_store(tmp_path)
    assert classify_origin(s, route="self", thread_id=None, recalled=[], now_iso=FIXED.isoformat()) == "self_thread"


def test_continuity_on_recently_touched_user_thread_is_user_recent(tmp_path):
    s = make_store(tmp_path)
    assert classify_origin(s, route="continuity", thread_id="thr_user", recalled=[],
                           now_iso=FIXED.isoformat()) == "user_recent"


def test_continuity_on_stale_user_thread_is_user_old(tmp_path):
    s = make_store(tmp_path)
    # 30 days later: the user interaction is no longer "recent"
    assert classify_origin(s, route="continuity", thread_id="thr_user", recalled=[],
                           now_iso=LATE.isoformat()) == "user_old"


def test_continuity_on_self_thread_is_self_thread(tmp_path):
    s = make_store(tmp_path)
    # a self thread is self regardless of recency
    assert classify_origin(s, route="continuity", thread_id="thr_self", recalled=[],
                           now_iso=LATE.isoformat()) == "self_thread"
    assert classify_origin(s, route="revisit", thread_id="thr_self", recalled=[],
                           now_iso=FIXED.isoformat()) == "self_thread"


def test_unanchored_project_material_is_private_project(tmp_path):
    s = make_store(tmp_path)
    proj = s._test_fixtures["proj"]
    recalled = [Hit(proj, "distant")]
    assert classify_origin(s, route="distant", thread_id=None, recalled=recalled,
                           now_iso=FIXED.isoformat()) == "private_project"


def test_unanchored_self_thread_material_is_self_thread(tmp_path):
    s = make_store(tmp_path)
    # a thought linked to the self thread, recalled unanchored
    mat = s.list(50, type_prefix="thought.created")
    mat = [e for e in mat if e.links.get("thread_id") == "thr_self"][0]
    recalled = [Hit(mat, "distant")]
    assert classify_origin(s, route="distant", thread_id=None, recalled=recalled,
                           now_iso=FIXED.isoformat()) == "self_thread"


def test_unanchored_serendipity_path_is_serendipity(tmp_path):
    s = make_store(tmp_path)
    old = s._test_fixtures["old"]
    recalled = [Hit(old, "serendipity"), Hit(old, "serendipity")]
    assert classify_origin(s, route="serendipity", thread_id=None, recalled=recalled,
                           now_iso=FIXED.isoformat()) == "serendipity"


def test_unanchored_plain_recall_is_memory_revival(tmp_path):
    s = make_store(tmp_path)
    old = s._test_fixtures["old"]
    # a plain old thought recalled via a non-serendipity path, no project, no self
    recalled = [Hit(old, "distant"), Hit(old, "forgotten")]
    assert classify_origin(s, route="distant", thread_id=None, recalled=recalled,
                           now_iso=FIXED.isoformat()) == "memory_revival"


def test_classification_is_deterministic(tmp_path):
    s = make_store(tmp_path)
    args = dict(route="continuity", thread_id="thr_user", recalled=[], now_iso=FIXED.isoformat())
    assert classify_origin(s, **args) == classify_origin(s, **args)


# --------------------------------------------------- independence telemetry


def _store_with_thoughts(tmp_path, origins: list[str]) -> EventStore:
    s = EventStore(tmp_path / "t.sqlite3", now_fn=lambda: FIXED)
    for o in origins:
        s.append(EventCreate(
            type="thought.created", content={"text": f"一条想法 {o}"},
            metadata={"origin": o},
        ))
    return s


def test_independence_telemetry_counts_origins(tmp_path):
    s = _store_with_thoughts(tmp_path, [
        "user_recent", "user_recent", "user_old", "self_thread", "memory_revival",
    ])
    tel = Telemetry(s)
    snap = tel.snapshot()
    assert snap["origin_base"] == 5
    assert snap["origin_distribution"]["user_recent"] == 2
    assert snap["origin_distribution"]["self_thread"] == 1
    assert abs(snap["user_recent_share"] - 2 / 5) < 1e-6
    assert abs(snap["user_derived_share"] - 3 / 5) < 1e-6  # recent + old
    assert abs(snap["self_origin_share"] - 1 / 5) < 1e-6
    assert abs(snap["independence_share"] - 3 / 5) < 1e-6  # 1 - user_recent
    # every reported label is one of the seven (and counts sum to the base)
    assert sum(snap["origin_distribution"].values()) == snap["origin_base"]


def test_independence_telemetry_ignores_unoriginated_thoughts(tmp_path):
    s = EventStore(tmp_path / "u.sqlite3", now_fn=lambda: FIXED)
    s.append(EventCreate(type="thought.created", content={"text": "无 origin 的旧想法"}))
    s.append(EventCreate(type="thought.created", content={"text": "新想法"},
                         metadata={"origin": "self_thread"}))
    snap = Telemetry(s).snapshot()
    assert snap["origin_base"] == 1          # unoriginated thought excluded
    assert snap["self_origin_share"] == 1.0
    assert snap["independence_share"] == 1.0
