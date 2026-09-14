"""MindLoop contract: no-op legitimacy, route recording, provenance safety."""
import json
import random

import pytest

from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.models import EventCreate
from resident.providers import DeterministicFakeProvider
from resident.route_policy import RoutePolicy


def make_store(tmp_path, name="e.sqlite3"):
    return EventStore(tmp_path / name)


def det_loop(s, **kw):
    """A MindLoop whose route selection is fully deterministic (no noise),
    so a state-driven test asserts the exact route the state implies."""
    return MindLoop(s, policy=RoutePolicy(exploration=0.0), rng=random.Random(0), **kw)


def append_conversation(s, message="今天读了一本书"):
    u = s.append(
        EventCreate(
            type="conversation.user_message",
            actor="user",
            visibility="user_visible",
            content={"text": message},
            provenance={"source": "test"},
        )
    )
    r = s.append(
        EventCreate(
            type="conversation.resident_message",
            visibility="user_visible",
            content={"text": "嗯。"},
            links={"caused_by": [u.id]},
        )
    )
    s.append(
        EventCreate(
            type="experience.created",
            visibility="private",
            content={"summary": f"对话：{message}"},
            links={"caused_by": [u.id, r.id]},
        )
    )
    return u, r


def _assert_all_links_resolve(s):
    for e in s.list(500):
        for v in e.links.values():
            ids = v if isinstance(v, list) else [v]
            for i in ids:
                if isinstance(i, str) and i.startswith("evt_"):
                    assert s.get(i) is not None, f"dangling link {i!r} in {e.id}"


async def test_first_wake_on_empty_store_is_noop(tmp_path):
    s = make_store(tmp_path)
    m = MindLoop(s)
    res = await m.wake_once("manual")
    assert res["result"] == "noop"
    assert res["route"] == "rest"
    assert any(e.type == "wake.noop" for e in s.list(50, type_prefix="wake"))
    assert any(e.type == "wake.completed" for e in s.list(50, type_prefix="wake"))
    assert s.count("thought.created") == 0  # no fabricated activity
    _assert_all_links_resolve(s)


async def test_second_wake_without_new_events_is_noop(tmp_path):
    s = make_store(tmp_path)
    m = MindLoop(s)
    r1 = await m.wake_once()
    r2 = await m.wake_once()
    assert r1["result"] == "noop"
    assert r2["result"] == "noop"  # nothing new since the last wake
    assert s.count("wake.noop") == 2


async def test_conversation_leads_to_personal_route_with_safe_links(tmp_path):
    s = make_store(tmp_path)
    u, _ = append_conversation(s)
    m = MindLoop(s)
    res = await m.wake_once("manual")
    assert res["route"] == "personal"
    assert res["result"] == "thought"
    thought = s.get(res["event_id"])
    assert thought is not None and thought.type == "thought.created"
    related = thought.links.get("related_to") or []
    assert related, "the thought must link the real user message"
    assert u.id in related
    for i in related:
        assert s.get(i) is not None  # provenance-safe
    _assert_all_links_resolve(s)


# ---------------------------------------------------------------------------
# State-DRIVEN routing end-to-end: the loop must pick the route the state
# implies (not a die) and do the route's bookkeeping. Route-policy scoring is
# unit-tested in test_route_policy.py; here we prove the state flows through
# the whole wake and into the recorded decision + thread bookkeeping.
# ---------------------------------------------------------------------------


async def test_state_driven_continuity_records_and_bookkeeps(tmp_path):
    s = make_store(tmp_path)
    c = s.append(
        EventCreate(type="thread.created", content={"thread_id": "thr_c", "title": "测试线程"})
    )
    s.append(
        EventCreate(
            type="thread.activated", visibility="private",
            content={"thread_id": "thr_c"},
            links={"thread_id": "thr_c", "caused_by": [c.id]},
        )
    )
    s.append(
        EventCreate(
            type="thought.created", visibility="private",
            content={"text": "继续思考这个线程"},
            links={"thread_id": "thr_c", "caused_by": [c.id]},
        )
    )
    m = det_loop(s)
    res = await m.wake_once("state")
    selected = s.list(50, type_prefix="wake.route_selected")
    assert selected[-1].content["route"] == "continuity", "an active thread should pull the route"
    assert res["result"] == "thought"
    assert res["thread_id"] == "thr_c"
    assert s.count("thread.activated") == 2  # the setup one + this wake's bookkeeping
    _assert_all_links_resolve(s)


async def test_state_driven_revisit_records_and_bookkeeps(tmp_path):
    s = make_store(tmp_path)
    for tid in ("thr_old1", "thr_old2"):
        c = s.append(
            EventCreate(type="thread.created", content={"thread_id": tid, "title": f"旧线程 {tid}"})
        )
        s.append(
            EventCreate(
                type="thought.created", visibility="private",
                content={"text": f"{tid} 的旧想法"},
                links={"thread_id": tid, "caused_by": [c.id]},
            )
        )
        s.append(
            EventCreate(
                type="thread.dormant", visibility="private",
                content={"thread_id": tid}, links={"thread_id": tid},
            )
        )
    m = det_loop(s)
    res = await m.wake_once("state")
    selected = s.list(50, type_prefix="wake.route_selected")
    assert selected[-1].content["route"] == "revisit", "dormant threads (no active one) surface revisit"
    assert res["result"] == "thought"
    assert res["thread_id"] in ("thr_old1", "thr_old2")
    assert any(e.type == "thread.revisited" for e in s.list(50, type_prefix="thread"))
    _assert_all_links_resolve(s)


# ---------------------------------------------------------------------------
# Route-recording MECHANICS: whichever route is chosen is recorded explicitly,
# with its full score vector (provenance of the decision). force_route pins a
# route so we test the recording/decision mechanics in isolation from policy.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["continuity", "revisit", "distant", "serendipity", "self", "personal"])
async def test_forced_route_is_recorded_with_scores(tmp_path, route):
    s = make_store(tmp_path)
    m = MindLoop(s, force_route=route)
    await m.wake_once("forced")
    selected = s.list(50, type_prefix="wake.route_selected")
    assert selected, "route selection must be explicitly recorded"
    assert selected[-1].content["route"] == route
    assert selected[-1].content["scores"], "the score vector is recorded (why the route won)"
    _assert_all_links_resolve(s)


async def test_world_route_is_a_noop(tmp_path):
    s = make_store(tmp_path)
    m = MindLoop(s, force_route="world")
    res = await m.wake_once("manual")
    assert res["result"] == "noop"
    assert res["route"] == "world"
    assert "unbound" in res["reason"]
    _assert_all_links_resolve(s)


class DanglingStubProvider:
    """A persist-route thought that cites an event that does not exist."""

    model_id = "stub/dangling"

    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "decision": "persist",
                "text": "一个想法",
                "links": {"related_to": ["evt_does_not_exist"]},
                "visibility": "private",
            }
        )


async def test_dangling_related_to_is_filtered_not_raised(tmp_path):
    s = make_store(tmp_path)
    append_conversation(s)  # drives the personal (persist) route
    m = MindLoop(s, provider=DanglingStubProvider())
    res = await m.wake_once("stub")  # must not raise
    assert res["result"] in ("thought", "noop")
    if res["result"] == "thought":
        t = s.get(res["event_id"])
        assert "evt_does_not_exist" not in (t.links.get("related_to") or [])
    _assert_all_links_resolve(s)  # store invariant holds


class EmptyTextStubProvider:
    model_id = "stub/empty"

    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps({"decision": "persist", "text": "", "links": {}})


async def test_empty_thought_degrades_to_noop(tmp_path):
    s = make_store(tmp_path)
    append_conversation(s)  # drives the personal (persist) route
    m = MindLoop(s, provider=EmptyTextStubProvider())
    res = await m.wake_once("stub")
    assert res["result"] == "noop"
    assert "no durable content" in res["reason"]
    assert s.count("thought.created") == 0
