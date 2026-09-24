"""Core guarantees: restart persistence and the MindLoop no-op contract."""
import pytest

from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.models import EventCreate


def test_event_survives_reopen(tmp_path):
    db = tmp_path / "events.sqlite3"
    a = EventStore(db)
    e = a.append(EventCreate(type="thought.created", content={"text": "persistent"}))
    b = EventStore(db)
    assert b.get(e.id).content["text"] == "persistent"


async def test_noop_is_valid(tmp_path):
    """A wake on an empty store is a legitimate no-op (no fake activity)."""
    s = EventStore(tmp_path / "events.sqlite3")
    m = MindLoop(s)
    result = await m.wake_once("test")
    assert result["result"] == "noop"
    assert result["route"] == "rest"
    types = [e.type for e in s.list(50, type_prefix="wake")]
    assert "wake.noop" in types
    assert "wake.completed" in types
    # nothing durable was invented
    assert s.count("thought") == 0


async def test_wake_links_resolve(tmp_path):
    """Every event a wake appends carries resolvable provenance/links."""
    s = EventStore(tmp_path / "events.sqlite3")
    m = MindLoop(s)
    await m.wake_once("test")
    for e in s.list(100):
        for v in e.links.values():
            ids = v if isinstance(v, list) else [v]
            for i in ids:
                if isinstance(i, str) and i.startswith("evt_"):
                    assert s.get(i) is not None, f"dangling link {i} in {e.id}"
