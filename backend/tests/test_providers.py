"""Tests for the deterministic fake model provider and the hashing
embedding provider.

DeterministicFakeProvider must be a pure function of its input (no
network, no randomness): same prompt in, byte-identical output out.
"""
from __future__ import annotations

import json
import math

import pytest

from resident.providers import DeterministicFakeProvider, HashingEmbeddingProvider

SYSTEM = "test-system"


def _call(provider: DeterministicFakeProvider, req: dict) -> dict:
    out = provider.complete(system=SYSTEM, prompt=json.dumps(req, ensure_ascii=False))
    return json.loads(out)


def _route(provider: DeterministicFakeProvider, **ctx) -> dict:
    return _call(provider, {"task": "route", **ctx})


# -------------------------------------------------------------- determinism

@pytest.mark.parametrize(
    "req",
    [
        {"task": "route", "new_since_last_wake": 1, "active_threads": [{"thread_id": "thr_1"}]},
        {
            "task": "thought",
            "route": "continuity",
            "thread": {
                "thread_id": "thr_1",
                "title": "睡眠",
                "last_events": [{"id": "evt_a", "text": "stop"}],
            },
        },
        {"task": "chat", "user_message": "今晚想吃面", "mind_brief": "（还没有什么值得说的）"},
    ],
)
def test_same_prompt_gives_byte_identical_output(req: dict):
    p = DeterministicFakeProvider()
    a = p.complete(system=SYSTEM, prompt=json.dumps(req, ensure_ascii=False))
    b = p.complete(system=SYSTEM, prompt=json.dumps(req, ensure_ascii=False))
    assert a == b  # byte-identical JSON strings


def test_route_deterministic_across_instances():
    prompt = json.dumps({"task": "route", "new_since_last_wake": 1, "total_events": 3})
    a = DeterministicFakeProvider().complete(system=SYSTEM, prompt=prompt)
    b = DeterministicFakeProvider().complete(system=SYSTEM, prompt=prompt)
    assert a == b


# ------------------------------------------------------------- route policy

def test_route_nothing_new_is_rest_noop():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=0)
    assert out["route"] == "rest"
    assert out["decision"] == "noop"


def test_route_active_thread_is_continuity():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1,
                 active_threads=[{"thread_id": "thr_1", "title": "T"}])
    assert out["route"] == "continuity"
    assert out["decision"] == "persist"


def test_route_dormant_only_is_revisit():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1,
                 dormant_threads=[{"thread_id": "thr_2", "title": "Old"}])
    assert out["route"] == "revisit"
    assert out["decision"] == "persist"


def test_route_recent_user_message_is_personal():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1,
                 has_recent_user_message=True)
    assert out["route"] == "personal"
    assert out["decision"] == "persist"


def test_route_high_topic_concentration_is_distant():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1,
                 topic_concentration=0.9)
    assert out["route"] == "distant"
    assert out["decision"] == "persist"


def test_route_recent_exploration_is_serendipity():
    # concentration below the 0.7 threshold, no user message, no threads
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1,
                 topic_concentration=0.3,
                 has_exploration_recently=True)
    assert out["route"] == "serendipity"
    assert out["decision"] == "persist"


def test_route_short_history_is_self():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1, total_events=3)
    assert out["route"] == "self"
    assert out["decision"] == "persist"


def test_route_long_history_with_nothing_else_is_rest_noop():
    out = _route(DeterministicFakeProvider(), new_since_last_wake=1, total_events=8)
    assert out["route"] == "rest"
    assert out["decision"] == "noop"


def test_route_rule_precedence():
    p = DeterministicFakeProvider()
    # active_threads beats dormant_threads
    out = _route(p, new_since_last_wake=1,
                 active_threads=[{"thread_id": "a"}],
                 dormant_threads=[{"thread_id": "d"}])
    assert out["route"] == "continuity"
    # dormant_threads beats personal
    out = _route(p, new_since_last_wake=1,
                 dormant_threads=[{"thread_id": "d"}],
                 has_recent_user_message=True)
    assert out["route"] == "revisit"
    # personal beats distant
    out = _route(p, new_since_last_wake=1,
                 has_recent_user_message=True,
                 topic_concentration=0.9)
    assert out["route"] == "personal"
    # and nothing-new (rule 1) beats everything
    out = _route(p, new_since_last_wake=0,
                 active_threads=[{"thread_id": "a"}],
                 has_recent_user_message=True)
    assert out["route"] == "rest"


# ---------------------------------------------------------- scripted routes

def test_scripted_routes_consumed_fifo_then_policy_resumes():
    p = DeterministicFakeProvider(scripted_routes=["distant", "self"])
    # a context that would policy-route to continuity, but script wins FIFO
    out1 = _route(p, new_since_last_wake=1, active_threads=[{"thread_id": "x"}])
    assert out1["route"] == "distant"
    assert out1["decision"] == "persist"

    out2 = _route(p, new_since_last_wake=1, active_threads=[{"thread_id": "x"}])
    assert out2["route"] == "self"
    assert out2["decision"] == "persist"

    # script exhausted -> policy resumes (nothing new -> rest/noop)
    out3 = _route(p, new_since_last_wake=0)
    assert out3["route"] == "rest"
    assert out3["decision"] == "noop"


def test_scripted_world_route_is_noop_decision():
    p = DeterministicFakeProvider(scripted_routes=["world"])
    out = _route(p, new_since_last_wake=1, active_threads=[{"thread_id": "x"}])
    assert out["route"] == "world"
    assert out["decision"] == "noop"


# ------------------------------------------------------------------ thought

def test_thought_continuity_mentions_thread_and_links_to_it():
    p = DeterministicFakeProvider()
    out = _call(p, {
        "task": "thought",
        "route": "continuity",
        "thread": {
            "thread_id": "thr_1",
            "title": "睡眠时间",
            "last_events": [
                {"id": "evt_a", "text": "last stop a"},
                {"id": "evt_b", "text": "last stop b"},
            ],
        },
        "recent": [{"id": "evt_c", "type": "thought.created", "text": "recent stuff"}],
    })
    assert out["decision"] == "persist"
    assert "睡眠时间" in out["text"]
    assert out["links"]["thread_id"] == "thr_1"
    # related_to must only cite events actually given in the thread payload
    assert set(out["links"]["related_to"]) <= {"evt_a", "evt_b"}
    assert out["links"]["related_to"]  # non-empty: both last_events have ids


def test_thought_personal_contains_user_message_text():
    p = DeterministicFakeProvider()
    out = _call(p, {
        "task": "thought",
        "route": "personal",
        "user_message": {"id": "evt_u", "text": "我今天加班到十点"},
        "mind_brief": "（还没有什么值得说的）",
    })
    assert out["decision"] == "persist"
    assert "我今天加班到十点" in out["text"]
    assert out["links"]["related_to"] == ["evt_u"]


def test_thought_rest_route_is_noop():
    p = DeterministicFakeProvider()
    out = _call(p, {"task": "thought", "route": "rest"})
    assert out["decision"] == "noop"
    assert out["text"] == ""
    assert out["links"] == {}


def test_thought_noop_decision_short_circuits_even_with_payload():
    p = DeterministicFakeProvider()
    out = _call(p, {
        "task": "thought",
        "route": "continuity",
        "decision": "noop",
        "thread": {"thread_id": "thr_1", "title": "T", "last_events": [{"id": "evt_a", "text": "x"}]},
    })
    assert out["decision"] == "noop"
    assert out["text"] == ""
    assert out["links"] == {}


# --------------------------------------------------------------------- chat

def test_chat_reply_contains_user_message_and_mind_brief():
    p = DeterministicFakeProvider()
    brief = "活跃线程「睡眠」"
    out = _call(p, {"task": "chat", "user_message": "今晚想吃面", "mind_brief": brief})
    reply = out["reply"]
    assert "今晚想吃面" in reply
    assert brief in reply


def test_chat_deterministic():
    p = DeterministicFakeProvider()
    req = {"task": "chat", "user_message": "hi", "mind_brief": "b"}
    a = p.complete(system=SYSTEM, prompt=json.dumps(req, ensure_ascii=False))
    b = p.complete(system=SYSTEM, prompt=json.dumps(req, ensure_ascii=False))
    assert a == b


# ---------------------------------------------------------------- embeddings

def test_embedding_deterministic_same_text_same_vector():
    e = HashingEmbeddingProvider()
    assert e.embed(["hello world"])[0] == e.embed(["hello world"])[0]
    # non-ASCII text is deterministic too
    assert e.embed(["我想睡觉"])[0] == e.embed(["我想睡觉"])[0]


def test_embedding_cosine_of_vector_with_itself_is_one():
    e = HashingEmbeddingProvider()
    v = e.embed(["persistent digital resident"])[0]
    assert HashingEmbeddingProvider.cosine(v, v) == pytest.approx(1.0)


def test_embedding_l2_normalized_for_nonempty_text():
    e = HashingEmbeddingProvider()
    for text in ("hello world", "一个测试", "abc_123"):
        v = e.embed([text])[0]
        norm = math.sqrt(sum(x * x for x in v))
        assert norm == pytest.approx(1.0)


def test_embedding_empty_text_gives_zero_vector_without_exception():
    e = HashingEmbeddingProvider()
    v = e.embed([""])[0]
    assert len(v) == e.dim
    assert all(x == 0.0 for x in v)
    # cosine with a zero vector is 0, no exception
    w = e.embed(["something"])[0]
    assert HashingEmbeddingProvider.cosine(v, w) == 0.0


def test_embedding_cosine_is_symmetric():
    e = HashingEmbeddingProvider()
    a = e.embed(["alpha beta gamma"])[0]
    b = e.embed(["delta epsilon"])[0]
    assert HashingEmbeddingProvider.cosine(a, b) == HashingEmbeddingProvider.cosine(b, a)


def test_embedding_lexical_overlap_gives_higher_cosine():
    e = HashingEmbeddingProvider()
    q = e.embed(["sleep and dreams"])[0]
    overlapping = e.embed(["dreams about sleep"])[0]
    disjoint = e.embed(["noodles for dinner"])[0]
    assert HashingEmbeddingProvider.cosine(q, overlapping) > HashingEmbeddingProvider.cosine(q, disjoint)
