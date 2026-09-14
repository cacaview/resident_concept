"""API wiring: health, chat (state-preserving), wake, life, mind, reentry."""
from fastapi.testclient import TestClient

from resident.main import build_app
from resident.models import EventCreate


def make_client(tmp_path):
    app = build_app(tmp_path)
    return TestClient(app), app


def test_health(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        r = client.get("/api/health")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["db"].endswith("events.sqlite3")
        assert body["events"] == 0
        assert body["scheduler_running"] is False


def test_chat_persists_events_and_preserves_mind_state(tmp_path):
    client, app = make_client(tmp_path)
    store = app.state.store
    with client:
        # Pre-existing mental state: a durable thought that predates the chat.
        store.append(
            EventCreate(
                type="thought.created",
                visibility="private",
                content={"text": "我在想边界采样"},
                provenance={"source": "test"},
            )
        )
        r1 = client.post("/api/chat", json={"message": "你好"})
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["reply"]
        assert "没有抹掉" in body1["reply"]  # reply acknowledges pre-existing state
        assert "边界采样" in body1["reply"]  # the pre-existing thought surfaces
        assert body1["user_event_id"].startswith("evt_")
        assert body1["resident_event_id"].startswith("evt_")
        assert store.count() == 4  # thought + user + resident + experience

        r2 = client.post("/api/chat", json={"message": "还在吗"})
        assert r2.status_code == 200
        body2 = r2.json()
        assert "边界采样" in body2["reply"]  # second reply still reflects the state
        assert store.count() == 7  # exactly 3 events persisted per chat

        # the resident message stores the mind brief it answered with
        resident_evt = store.get(body1["resident_event_id"])
        assert "边界采样" in resident_evt.content["mind_brief"]


def test_wake_endpoint(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        r = client.post("/api/wake", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["route"]
        assert body["result"] in ("thought", "noop")
        assert body["trigger"] == "manual"
        r2 = client.post("/api/wake", json={"trigger": "test"})
        assert r2.status_code == 200
        assert r2.json()["trigger"] == "test"


def test_life_endpoint(tmp_path):
    client, app = make_client(tmp_path)
    store = app.state.store
    with client:
        thought = store.append(
            EventCreate(
                type="thought.created",
                visibility="shareable",
                content={"text": "想法"},
                metadata={"route": "self"},
            )
        )
        client.post("/api/wake", json={"trigger": "test"})
        r = client.get("/api/life")
        assert r.status_code == 200
        body = r.json()
        assert "events" in body and "wakes" in body and "stats" in body
        by_id = {e["id"]: e for e in body["events"]}
        assert by_id[thought.id]["text"] == "想法"
        assert by_id[thought.id]["route"] == "self"
        assert len(body["wakes"]) == 1
        assert body["wakes"][0]["result"] in ("persisted", "noop")
        assert body["stats"]["wakes"] == 1
        assert body["stats"]["events"] >= 5


def test_mind_endpoint(tmp_path):
    client, app = make_client(tmp_path)
    store = app.state.store
    with client:
        c = store.append(
            EventCreate(type="thread.created", content={"thread_id": "thr_x", "title": "线程X"})
        )
        store.append(
            EventCreate(
                type="thread.activated",
                visibility="private",
                content={"thread_id": "thr_x"},
                links={"thread_id": "thr_x", "caused_by": [c.id]},
            )
        )
        store.append(
            EventCreate(
                type="question.created",
                content={"question_id": "q_1", "text": "为什么？", "thread_id": "thr_x"},
            )
        )
        store.append(
            EventCreate(type="thought.created", visibility="private", content={"text": "一个念头"})
        )
        r = client.get("/api/mind")
        assert r.status_code == 200
        body = r.json()
        assert "threads" in body and "open_questions" in body and "recent_thoughts" in body
        assert body["threads"][0]["thread_id"] == "thr_x"
        assert body["active_threads"][0]["thread_id"] == "thr_x"
        assert body["dormant_threads"] == []
        assert body["open_questions"][0]["question_id"] == "q_1"
        assert body["recent_thoughts"][0]["text"] == "一个念头"
        assert "线程X" in body["mind_brief"]


def test_reentry_endpoint(tmp_path):
    client, app = make_client(tmp_path)
    store = app.state.store
    with client:
        r = client.post("/api/reentry", json={})
        assert r.status_code == 200
        assert r.json()["decision"] == "nothing_to_share"
        u = store.append(
            EventCreate(
                type="conversation.user_message",
                actor="user",
                visibility="user_visible",
                content={"text": "hi"},
            )
        )
        store.append(
            EventCreate(
                type="thought.created",
                visibility="shareable",
                content={"text": "分享的想法"},
                links={"caused_by": [u.id]},
            )
        )
        r2 = client.post("/api/reentry")
        assert r2.status_code == 200
        assert r2.json()["decision"] == "share_now"


def test_events_endpoints_and_404(tmp_path):
    client, app = make_client(tmp_path)
    store = app.state.store
    with client:
        e = store.append(EventCreate(type="thought.created", content={"text": "hi"}))
        r = client.get("/api/events")
        assert r.status_code == 200
        assert [x["id"] for x in r.json()] == [e.id]
        r2 = client.get(f"/api/events/{e.id}")
        assert r2.status_code == 200
        assert r2.json()["id"] == e.id
        r3 = client.get("/api/events/evt_missing")
        assert r3.status_code == 404
        r4 = client.get("/api/events", params={"type": "thought"})
        assert len(r4.json()) == 1


def test_scheduler_control_endpoints(tmp_path):
    client, app = make_client(tmp_path)
    with client:
        r = client.post("/api/scheduler/start")
        assert r.status_code == 200
        assert r.json()["running"] is True
        r2 = client.post("/api/scheduler/stop")
        assert r2.status_code == 200
        assert r2.json()["running"] is False


def test_conversation_history_survives_restart_and_orders(tmp_path):
    """The WebUI hydrates chat from /api/events?type=conversation — the
    second timeline's conversation must persist across a process restart and
    be retrievable newest-first (order=desc) for the panel to reverse."""
    app1 = build_app(tmp_path)
    with TestClient(app1) as client:
        for i, text in enumerate(["你好", "还在吗"]):
            r = client.post("/api/chat", json={"message": text})
            assert r.status_code == 200

    # Rebuild the app on the same data dir: a fresh process, same store.
    app2 = build_app(tmp_path)
    with TestClient(app2) as client:
        r = client.get("/api/events", params={"type": "conversation", "order": "desc", "limit": 100})
        assert r.status_code == 200
        evts = r.json()
        # 2 chats x (user + resident) = 4 conversation events
        assert len(evts) == 4
        types = [e["type"] for e in evts]
        assert types == [
            "conversation.resident_message",
            "conversation.user_message",
            "conversation.resident_message",
            "conversation.user_message",
        ]
        # newest-first: the first user event is the second chat
        assert evts[1]["content"]["text"] == "还在吗"
        assert evts[3]["content"]["text"] == "你好"
        # the default order stays ascending for existing callers
        r_asc = client.get("/api/events", params={"type": "conversation"})
        assert [e["content"]["text"] for e in r_asc.json() if e["type"] == "conversation.user_message"] == ["你好", "还在吗"]
