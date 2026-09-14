"""v0.2 Step 5 (ADR-0013): the Self Monitor — read-only view tests.

The governing principle under test: **the monitor may only READ facts.** Every
view builder (and every HTTP endpoint) must leave the store byte-count-
identical — opening the page must not shake the petri dish. Plus: the five
views show real, provenance-backed facts, and no pseudo-personality number
(mood/curiosity/loneliness/importance…) exists anywhere in any payload.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from resident.continuity import ContinuityEngine
from resident.event_store import EventStore
from resident.main import build_app
from resident.memory import MemoryRetrieval
from resident.models import EventCreate
from resident.monitor import (
    monitor_continuity,
    monitor_provenance,
    monitor_questions,
    monitor_retrieval_trace,
    monitor_threads,
    monitor_timeline,
)

T0 = datetime(2026, 6, 1, 9, 0, 0, tzinfo=timezone.utc)

#: words that must NEVER appear as payload keys — pseudo-personality numbers
#: would smuggle back the unearned internality the project removed
BANNED_KEYS = {"mood", "curiosity", "loneliness", "importance", "happiness",
               "affinity", "sentiment", "energy_level"}


class Clock:
    def __init__(self):
        self.now = T0


def _seed_life(clock: Clock) -> EventStore:
    """A compact but complete life: user contact, threads, a thought that
    reached for material, a question (created + revisited), a gap with a
    world-changed mind, a continuity decision, and system noops."""
    import tempfile

    tmp = tempfile.mkdtemp()
    store = EventStore(f"{tmp}/m.sqlite3", now_fn=lambda: clock.now)
    store.append(EventCreate(  # system bookkeeping: a wake that did nothing
        type="wake.started", visibility="system", content={"trigger": "circadian"},
        provenance={"source": "test"}))
    store.append(EventCreate(
        type="wake.completed", visibility="system",
        content={"route": "rest", "result": "noop"}, provenance={"source": "test"}))
    u1 = store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "我先走了，晚上聊"}, provenance={"source": "test"}))
    store.append(EventCreate(
        type="thread.created", visibility="private",
        content={"thread_id": "thr_pasta", "title": "pasta", "origin": "user"},
        provenance={"source": "test"}))
    store.append(EventCreate(
        type="thread.activated", visibility="private",
        content={"thread_id": "thr_pasta"},
        links={"thread_id": "thr_pasta"}, provenance={"source": "test"}))
    store.append(EventCreate(
        type="thread.created", visibility="private",
        content={"thread_id": "thr_self_reading", "title": "reading", "origin": "self"},
        provenance={"source": "test"}))
    store.append(EventCreate(
        type="thread.dormant", visibility="private",
        content={"thread_id": "thr_self_reading"},
        links={"thread_id": "thr_self_reading"}, provenance={"source": "test"}))
    exp = store.append(EventCreate(
        type="experience.created", visibility="private",
        content={"summary": "与用户谈到「pasta」：我先走了，晚上聊"},
        links={"caused_by": [u1.id], "thread_id": "thr_pasta"},
        provenance={"source": "test"}))
    store.append(EventCreate(
        type="thought.created", visibility="private",
        content={"text": "关于意面的想法，引用了刚才的经历。"},
        metadata={"origin": "user_recent", "route": "personal", "recalled": [exp.id]},
        links={"thread_id": "thr_pasta"}, provenance={"source": "test"}))
    obs = store.append(EventCreate(
        type="world.observation", visibility="private",
        content={"title": "面团的筋度从何而来", "topic": "food",
                 "summary": "面筋由麦谷蛋白吸水交联形成。"},
        provenance={"source": "test"}))
    q = store.append(EventCreate(
        type="question.created", visibility="private",
        content={"question": "为什么面团会筋度不一样？", "kind": "tension",
                 "topic": "food", "status": "open"},
        metadata={"question": {"kind": "tension", "evidence": [obs.id]}},
        provenance={"source": "test"}))
    store.append(EventCreate(
        type="question.revisited", visibility="private",
        content={"question_id": q.id, "reason": "world re-encounter"},
        links={"related_to": [q.id]}, provenance={"source": "test"}))
    store.append(EventCreate(
        type="thread.revisited", visibility="private",
        content={"thread_id": "thr_pasta", "reason": "revival"},
        provenance={"source": "test"}))
    # the absence: 8h of life, a world thought, then a return
    store.append(EventCreate(
        type="thought.created", visibility="private",
        content={"text": "睡眠重放和意面面团的静置连起来了。"},
        metadata={"origin": "world", "recalled": [obs.id]},
        links={"related_to": [obs.id]},
        provenance={"source": "test"}))
    clock.now = clock.now + timedelta(hours=8)
    u2 = store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "我回来了"}, provenance={"source": "test"}))
    eng = ContinuityEngine(store, memory=MemoryRetrieval(store))
    eng.on_user_return(u2)
    return store


def _walk_no_banned_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert str(k).lower() not in BANNED_KEYS, f"banned key {k} at {path}"
            _walk_no_banned_keys(v, f"{path}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _walk_no_banned_keys(v, f"{path}[{i}]")


def test_all_views_are_read_only():
    """THE glass-window guarantee: no builder appends anything."""
    clock = Clock()
    store = _seed_life(clock)
    before_count, before_latest = store.count(), store.latest().seq
    latest = store.latest()
    now = latest.created_at
    monitor_timeline(store)
    monitor_timeline(store, type_prefix="question")
    monitor_provenance(store, latest.id)
    monitor_threads(store, now_iso=now)
    monitor_questions(store, now_iso=now)
    monitor_continuity(store)
    monitor_retrieval_trace(store, now_iso=now)
    assert store.count() == before_count
    assert store.latest().seq == before_latest


def test_all_views_carry_no_pseudo_personality_numbers():
    clock = Clock()
    store = _seed_life(clock)
    latest = store.latest()
    now = latest.created_at
    for payload in (monitor_timeline(store), monitor_threads(store, now_iso=now),
                    monitor_questions(store, now_iso=now),
                    monitor_continuity(store),
                    monitor_retrieval_trace(store, now_iso=now)):
        _walk_no_banned_keys(payload)


class TestViews:
    def test_timeline_includes_system_noops_and_filters(self):
        clock = Clock()
        store = _seed_life(clock)
        tl = monitor_timeline(store)
        types = [e["type"] for e in tl["events"]]
        assert "wake.completed" in types and "conversation.user_message" in types
        assert tl["counts"]["noops"] >= 1 and tl["counts"]["absences"] == 1
        qtl = monitor_timeline(store, type_prefix="question")
        assert {e["type"] for e in qtl["events"]} <= {"question.created", "question.revisited"}

    def test_provenance_walks_the_whole_chain_back(self):
        clock = Clock()
        store = _seed_life(clock)
        # the world thought sits on the observation; walk from it
        th = next(e for e in store.list(100, type_prefix="thought.created", order="desc")
                  if (e.metadata or {}).get("origin") == "world")
        prov = monitor_provenance(store, th.id)
        ids = {n["id"] for n in prov["nodes"]}
        obs = next(e for e in store.list(100, type_prefix="world.observation"))
        assert obs.id in ids                      # ← the world observation
        kinds = {(e["kind"]) for e in prov["edges"]}
        assert "recalled" in kinds and kinds <= {"caused_by", "related_to", "recalled"}
        depths = {n["id"]: n["depth"] for n in prov["nodes"]}
        assert depths[th.id] == 0 and depths[obs.id] >= 1
        assert monitor_provenance(store, "evt_missing")["error"]

    def test_threads_states_and_history_without_scores(self):
        clock = Clock()
        store = _seed_life(clock)
        tv = monitor_threads(store, now_iso=store.latest().created_at)
        by_id = {t["thread_id"]: t for t in tv["threads"]}
        assert by_id["thr_pasta"]["state"] == "activated"
        assert by_id["thr_pasta"]["origin"] == "user"
        assert by_id["thr_self_reading"]["state"] == "dormant"
        assert by_id["thr_pasta"]["revisits"], "revisit history is shown"
        assert tv["summary"]["active"] >= 1 and tv["summary"]["dormant"] >= 1

    def test_questions_lifecycle_and_evidence(self):
        clock = Clock()
        store = _seed_life(clock)
        qv = monitor_questions(store, now_iso=store.latest().created_at)
        assert qv["summary"]["total"] == 1
        row = qv["questions"][0]
        assert row["kind"] == "tension" and row["status"] == "open" and row["revisits"] == 1
        assert row["evidence_resolved"][0]["type"] == "world.observation"

    def test_timeline_rows_carry_causal_link_targets(self):
        """A chain must be followable row-to-row in the timeline alone: each
        brief includes the actual ``caused_by`` / ``related_to`` target ids,
        not just link counts."""
        clock = Clock()
        store = _seed_life(clock)
        tl = monitor_timeline(store)
        rev = next(e for e in tl["events"] if e["type"] == "question.revisited")
        q = next(e for e in tl["events"] if e["type"] == "question.created")
        assert q["id"] in rev["link_ids"]["related_to"]
        assert "link_ids" in rev and rev["link_ids"]["related_to"]

    def test_questions_rows_list_the_revisit_events(self):
        """Not just the revisit COUNT: the revisit events themselves (id, at,
        reason) are on the question row, so the revisit chain is explainable
        without touching the store."""
        clock = Clock()
        store = _seed_life(clock)
        qv = monitor_questions(store, now_iso=store.latest().created_at)
        row = qv["questions"][0]
        assert row["revisits"] == 1
        assert len(row["revisit_events"]) == 1
        rev = row["revisit_events"][0]
        assert rev["reason"] == "world re-encounter" and rev["at"]
        # and the revisit event really cites the question (verifiable upstream)
        e = store.get(rev["id"])
        assert row["question_id"] in (e.links.get("related_to") or [])

    def test_continuity_window_assembly(self):
        clock = Clock()
        store = _seed_life(clock)
        cv = monitor_continuity(store)
        assert cv["last_user_seen"] is not None
        assert len(cv["windows"]) == 1
        w = cv["windows"][0]
        assert abs(w["gap_s"] - 8 * 3600) < 1.0
        assert w["return"]["text"] == "我回来了"
        assert w["decision"] in ("selected", "noop")
        assert all("cls" in c and "quote" in c for c in w["candidates"])

    def test_retrieval_trace_shows_what_was_used(self):
        clock = Clock()
        store = _seed_life(clock)
        rt = monitor_retrieval_trace(store, now_iso=store.latest().created_at)
        row = next(r for r in rt["thoughts"] if r["route"] == "personal")
        assert row["used"], "the thought's recalled material is on record"
        u = row["used"][0]
        assert u["type"] == "experience.created" and "origin" in u
        assert u["age_days"] is not None
        assert rt["shadow"] is None  # no sidecar in this store's tmp dir

    def test_retrieval_trace_reads_shadow_sidecar(self, tmp_path):
        clock = Clock()
        store = _seed_life(clock)
        sidecar = tmp_path / "semantic_shadow.jsonl"
        sidecar.write_text(json.dumps({
            "route": "continuity", "query": "意面",
            "legacy_top": [{"id": "evt_a", "score": 0.31}, {"id": "evt_b", "score": 0.2}],
            "semantic_top": [{"id": "evt_c", "score": 0.28}],
            "overlap": 0.0, "top1_differs": True,
        }), encoding="utf-8")
        rt = monitor_retrieval_trace(store, shadow_log_path=str(sidecar))
        assert rt["shadow"] is not None and len(rt["shadow"]["records"]) == 1
        rec = rt["shadow"]["records"][0]
        assert rec["top1_differs"] is True and rec["legacy_top"][0][0] == "evt_a"


class TestHttpGlassWindow:
    def test_monitor_endpoints_are_get_only_and_leave_the_store_untouched(self):
        """The HTTP layer of the same guarantee: every monitor endpoint returns
        200 and the store is byte-count-identical after the whole sweep."""
        import tempfile

        data = tempfile.mkdtemp()
        app = build_app(data)
        # give the app's own store a life
        st = app.state.store
        clock = Clock()
        # reuse the seeded-life events against the app's store via injection:
        st.append(EventCreate(
            type="wake.completed", visibility="system",
            content={"route": "rest", "result": "noop"}, provenance={"source": "t"}))
        u = st.append(EventCreate(
            type="conversation.user_message", actor="user", visibility="user_visible",
            content={"text": "我回来了"}, provenance={"source": "t"}))
        ContinuityEngine(st, memory=MemoryRetrieval(st)).on_user_return(u)
        del clock
        before = st.count()
        client = TestClient(app)
        for path in ("/api/monitor/timeline?limit=50",
                     "/api/monitor/threads", "/api/monitor/questions",
                     "/api/monitor/continuity", "/api/monitor/retrieval",
                     "/api/monitor/projects"):
            r = client.get(path)
            assert r.status_code == 200, path
            _walk_no_banned_keys(r.json())
        r = client.get(f"/api/monitor/provenance/{u.id}")
        assert r.status_code == 200
        r404 = client.get("/api/monitor/provenance/evt_nope")
        assert r404.status_code == 200 and r404.json()["error"]
        assert st.count() == before, "opening the monitor must not shake the petri dish"

    def test_projects_endpoint_empty_store_renders_empty(self):
        """/api/monitor/projects (ADR-0016, exposed as a GET view) shows a
        clean empty lifecycle on a store with no projects — empty, not error."""
        import tempfile

        app = build_app(tempfile.mkdtemp())
        client = TestClient(app)
        r = client.get("/api/monitor/projects")
        assert r.status_code == 200
        assert r.json() == {"projects": [], "summary": {
            "total": 0, "active": 0, "dormant": 0, "artifacts": 0}}
