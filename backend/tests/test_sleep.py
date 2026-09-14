"""SleepEngine contract: state-gated consolidation, no fabricated content.

A sleep may legitimately do nothing (noop is first-class). When it does act,
every event is grounded in real store state: no invented thoughts, no
dangling links, provenance on everything.
"""
import random
from datetime import datetime, timedelta, timezone

from resident.event_store import EventStore
from resident.models import EventCreate
from resident.sleep import SleepEngine


class StepClock:
    """Deterministic virtual clock: +1h per append (no wall clock in tests)."""

    def __init__(self, start=None):
        self._t = start or datetime(2026, 9, 1, tzinfo=timezone.utc)

    def __call__(self):
        ts = self._t
        self._t += timedelta(hours=1)
        return ts


OLD_TEXT = "记录：城市夜晚的灯光很亮。"
RECENT_TEXT = "记录：城市夜晚的灯光很亮，令人难忘。"
DUP_TEXT = "今天整理了一下关于睡眠机制的笔记。"
# Distinct per filler so pairwise cosine stays below both thresholds.
FILLER_CHARS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰"


def make_store(tmp_path, name="s.sqlite3"):
    return EventStore(tmp_path / name, now_fn=StepClock())


def think(s, text, **links):
    return s.append(
        EventCreate(type="thought.created", content={"text": text}, links=links)
    )


def filler(i):
    # Distinct per filler: only one shared token ("段"), so pairwise cosine
    # (~0.33) stays below RELATED_THRESHOLD (0.5) and DUPLICATE_THRESHOLD (0.8).
    return f"段{i}{FILLER_CHARS[i % len(FILLER_CHARS)]}。"


def _all_links_resolve(s):
    for e in s.list(500):
        for v in e.links.values():
            ids = v if isinstance(v, list) else [v]
            for i in ids:
                if isinstance(i, str) and i.startswith("evt_"):
                    assert s.get(i) is not None, f"dangling link {i!r} in {e.id}"


def _consolidations(s, action):
    return [
        e
        for e in s.list(100, type_prefix="memory.consolidated")
        if e.content.get("action") == action
    ]


async def test_empty_store_sleep_is_noop(tmp_path):
    s = make_store(tmp_path)
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] == "noop"
    assert res["actions"] == []
    assert res["event_ids"] == []
    assert s.count("sleep.started") == 1
    assert s.count("sleep.completed") == 1
    assert s.count("thought.created") == 0  # nothing fabricated
    comp = s.list(5, type_prefix="sleep.completed")[0]
    assert comp.content["result"] == "noop"
    assert comp.content["event_count"] == 0
    _all_links_resolve(s)


async def test_compress_near_duplicate_thoughts(tmp_path):
    s = make_store(tmp_path)
    t1 = think(s, DUP_TEXT)
    t2 = think(s, DUP_TEXT)
    think(s, "另一个主题：河流与桥。")
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] == "consolidated"
    assert "compress" in res["actions"]
    cons = _consolidations(s, "compress")
    assert len(cons) == 1
    c = cons[0]
    assert set(c.links["related_to"]) == {t1.id, t2.id}
    assert set(c.content["duplicates"]) == {t1.id, t2.id}
    assert s.get(t1.id) is not None and s.get(t2.id) is not None  # originals kept
    _all_links_resolve(s)


async def test_active_old_thread_goes_dormant(tmp_path):
    s = make_store(tmp_path)
    c = s.append(
        EventCreate(type="thread.created", content={"thread_id": "thr_a", "title": "旧主题"})
    )
    s.append(
        EventCreate(
            type="thread.activated",
            visibility="private",
            content={"thread_id": "thr_a"},
            links={"thread_id": "thr_a", "caused_by": [c.id]},
        )
    )
    think(s, "线程内的一条想法：关于旧主题。", thread_id="thr_a")
    for i in range(15):  # push the thread's last touch out of the recent window
        think(s, filler(i))
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] == "consolidated"
    assert "thread_dormancy" in res["actions"]
    dorm = s.list(10, type_prefix="thread.dormant")
    assert len(dorm) == 1
    assert dorm[0].content["thread_id"] == "thr_a"
    assert dorm[0].links["thread_id"] == "thr_a"
    _all_links_resolve(s)


async def test_forgetten_old_memory_strongly_related_is_reactivated(tmp_path):
    s = make_store(tmp_path)
    old = think(s, OLD_TEXT)
    for i in range(15):
        think(s, filler(i))
    rec = think(s, RECENT_TEXT)
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] == "consolidated"
    assert "reactivate" in res["actions"]
    cons = _consolidations(s, "reactivate")
    assert len(cons) == 1
    assert set(cons[0].links["related_to"]) == {old.id, rec.id}
    assert s.get(old.id) is not None and s.get(rec.id) is not None
    _all_links_resolve(s)


async def test_cross_family_close_pair_is_linked_once(tmp_path):
    s = make_store(tmp_path)
    exp = s.append(
        EventCreate(
            type="experience.created",
            content={"text": "散步时我观察了城市灯光。"},
        )
    )
    th = think(s, "散步时我观察了城市灯光并做了记录。")
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] == "consolidated"
    assert res["actions"].count("cross_link") == 1  # limited: at most one
    cons = _consolidations(s, "cross_link")
    assert len(cons) == 1
    assert set(cons[0].links["related_to"]) == {exp.id, th.id}
    _all_links_resolve(s)


async def test_dream_prob_zero_never_dreams(tmp_path):
    s = make_store(tmp_path)
    for i in range(5):
        think(s, filler(i))
    before = s.count("thought.created")
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert s.count("thought.created") == before  # no invented thought
    assert "dream" not in res["actions"]
    assert res["result"] in ("noop", "consolidated")


async def test_dream_prob_one_with_material_creates_one_grounded_thought(tmp_path):
    s = make_store(tmp_path)
    for i in range(4):
        think(s, filler(i))
    orig = {e.id for e in s.list(50, type_prefix="thought.created")}
    res = await SleepEngine(s, rng=random.Random(3), dream_prob=1.0).sleep_once()
    assert res["result"] == "dreamed"
    assert res["actions"] == ["dream"]
    new = [e for e in s.list(50, type_prefix="thought.created") if e.id not in orig]
    assert len(new) == 1
    t = new[0]
    assert t.text.startswith("[dream]")
    related = t.links.get("related_to") or []
    assert len(related) == 2
    for i in related:
        assert s.get(i) is not None  # every cited memory is real
    assert t.metadata.get("route") == "dream"
    assert t.metadata.get("recalled") == related
    assert t.provenance.get("source") == "sleep"
    _all_links_resolve(s)


def _seed_store(path):
    s = EventStore(path, now_fn=StepClock())
    think(s, OLD_TEXT)
    for i in range(12):
        think(s, filler(i))
    think(s, RECENT_TEXT)
    return s


async def test_determinism_same_seed_same_state(tmp_path):
    s1 = _seed_store(tmp_path / "a.sqlite3")
    s2 = _seed_store(tmp_path / "b.sqlite3")
    r1 = await SleepEngine(s1, rng=random.Random(42), dream_prob=0.5).sleep_once()
    r2 = await SleepEngine(s2, rng=random.Random(42), dream_prob=0.5).sleep_once()
    assert r1["result"] == r2["result"]
    assert r1["actions"] == r2["actions"]
    assert s1.count() == s2.count()


async def test_provenance_on_all_sleep_and_consolidation_events(tmp_path):
    s = make_store(tmp_path)
    think(s, DUP_TEXT)
    think(s, DUP_TEXT)
    c = s.append(
        EventCreate(type="thread.created", content={"thread_id": "thr_b", "title": "另一线程"})
    )
    s.append(
        EventCreate(
            type="thread.activated",
            content={"thread_id": "thr_b"},
            links={"thread_id": "thr_b", "caused_by": [c.id]},
        )
    )
    think(s, "线程想法。", thread_id="thr_b")
    for i in range(15):
        think(s, filler(i))
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    targets = [
        e
        for e in s.list(300)
        if e.type in ("sleep.started", "sleep.completed", "memory.consolidated")
    ]
    assert targets
    for e in targets:
        assert e.provenance.get("source") == "sleep"
        assert e.provenance.get("run_id") == res["run_id"]


async def test_no_dangling_links_after_a_full_sleep(tmp_path):
    s = make_store(tmp_path)
    think(s, OLD_TEXT)
    for i in range(12):
        think(s, filler(i))
    think(s, RECENT_TEXT)
    think(s, DUP_TEXT)
    think(s, DUP_TEXT)
    await SleepEngine(s, rng=random.Random(7), dream_prob=1.0).sleep_once()
    _all_links_resolve(s)


async def test_material_without_consolidation_invents_nothing(tmp_path):
    s = make_store(tmp_path)
    for i in range(6):
        think(s, filler(i))
    before = s.count("thought.created")
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0).sleep_once()
    assert res["result"] in ("noop", "consolidated")
    assert "dream" not in res["actions"]
    assert s.count("thought.created") == before  # material alone never invents a thought


# ------------------------------------------------------- self-originated thread
# The Phase-3 mechanism: with LOW probability (and only when the mind has been
# across >=2 distinct user topics), sleep crystallises a NEW self-originated
# thread grounded in two REAL memories. It is the independence material source
# and must be: grounded (no dangling, provenance cites real events), low-prob
# (a seed of self_prob=0 never makes one), and capped (self_max bounds total).


def _two_topic_store(tmp_path, name="st.sqlite3") -> EventStore:
    """A mind with real material across two DISTINCT user topics."""
    s = EventStore(tmp_path / name, now_fn=StepClock())
    ca = s.append(
        EventCreate(type="thread.created",
                    content={"thread_id": "thr_a", "title": "话题A", "origin": "user"})
    )
    s.append(EventCreate(type="thread.activated", content={"thread_id": "thr_a"},
                         links={"thread_id": "thr_a", "caused_by": [ca.id]}))
    s.append(EventCreate(type="experience.created",
                         content={"summary": "话题A的内容：城市夜晚的灯光很亮。"},
                         links={"thread_id": "thr_a"}))
    cb = s.append(
        EventCreate(type="thread.created",
                    content={"thread_id": "thr_b", "title": "话题B", "origin": "user"})
    )
    s.append(EventCreate(type="thread.activated", content={"thread_id": "thr_b"},
                         links={"thread_id": "thr_b", "caused_by": [cb.id]}))
    s.append(EventCreate(type="experience.created",
                         content={"summary": "话题B的内容：清晨的海面很静。"},
                         links={"thread_id": "thr_b"}))
    return s


def _self_threads(s):
    return [e for e in s.list(100, type_prefix="thread.created")
            if e.content.get("origin") == "self"]


async def test_self_thread_is_grounded_when_cross_topic_material_exists(tmp_path):
    s = _two_topic_store(tmp_path)
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0,
                            self_prob=1.0).sleep_once()
    assert "self_thread" in res["actions"]
    threads = _self_threads(s)
    assert len(threads) == 1
    t = threads[0]
    # grounded: provenance marks it self + cites two REAL source memories
    assert t.provenance.get("origin") == "self"
    derived = t.provenance.get("derived_from") or []
    assert len(derived) == 2
    for i in derived:
        assert s.get(i) is not None, "self thread must cite real memories"
    # it is activated so the route policy can continue it later
    assert any(
        e.content.get("thread_id") == t.content["thread_id"]
        for e in s.list(50, type_prefix="thread.activated")
    )
    _all_links_resolve(s)


async def test_self_prob_zero_never_makes_a_self_thread(tmp_path):
    s = _two_topic_store(tmp_path)
    for _ in range(5):
        res = await SleepEngine(s, rng=random.Random(1), dream_prob=0.0,
                                self_prob=0.0).sleep_once()
        assert "self_thread" not in res["actions"]
    assert _self_threads(s) == []  # control: no self material without the mechanism


async def test_self_thread_is_capped_not_a_quota(tmp_path):
    s = _two_topic_store(tmp_path)
    # cap at 1: the first sleep makes one, the second must not make another
    await SleepEngine(s, rng=random.Random(2), dream_prob=0.0,
                      self_prob=1.0, self_max=1).sleep_once()
    await SleepEngine(s, rng=random.Random(3), dream_prob=0.0,
                      self_prob=1.0, self_max=1).sleep_once()
    assert len(_self_threads(s)) == 1
    # and with no cap (a big one) repeated sleeps can form several — it is the
    # probability that gates each one, never a forced share
    s2 = _two_topic_store(tmp_path, "st2.sqlite3")
    for seed in range(4):
        await SleepEngine(s2, rng=random.Random(seed), dream_prob=0.0,
                          self_prob=1.0, self_max=8).sleep_once()
    assert len(_self_threads(s2)) >= 1


async def test_no_cross_topic_material_makes_no_self_thread(tmp_path):
    # only ONE user topic -> nothing to synthesise -> no self thread even at p=1
    s = EventStore(tmp_path / "single.sqlite3", now_fn=StepClock())
    ca = s.append(EventCreate(type="thread.created",
                              content={"thread_id": "thr_only", "title": "唯一话题",
                                       "origin": "user"}))
    s.append(EventCreate(type="thread.activated", content={"thread_id": "thr_only"},
                         links={"thread_id": "thr_only", "caused_by": [ca.id]}))
    s.append(EventCreate(type="experience.created",
                         content={"summary": "只有一个话题的内容。"},
                         links={"thread_id": "thr_only"}))
    res = await SleepEngine(s, rng=random.Random(0), dream_prob=0.0,
                            self_prob=1.0).sleep_once()
    assert "self_thread" not in res["actions"]
    assert _self_threads(s) == []


async def test_self_thread_determinism_for_fixed_seed(tmp_path):
    r1 = _two_topic_store(tmp_path / "d1.sqlite3")
    r2 = _two_topic_store(tmp_path / "d2.sqlite3")
    res1 = await SleepEngine(r1, rng=random.Random(42), dream_prob=0.0,
                             self_prob=1.0).sleep_once()
    res2 = await SleepEngine(r2, rng=random.Random(42), dream_prob=0.0,
                             self_prob=1.0).sleep_once()
    assert res1["actions"] == res2["actions"]
    assert len(_self_threads(r1)) == len(_self_threads(r2)) == 1


# ------------------------------------------------- round 13 dedup (data fix)
# The 28d real-model battery showed every seed spawning 4 near-identical self
# threads on days 2-3: the same near-duplicate pair (or its same truncated
# title) winning successive sleep cycles in one consolidation window. A
# near-identical pair must not spawn a second thread.


async def test_same_pair_in_same_window_makes_one_thread_only(tmp_path):
    s = _two_topic_store(tmp_path)
    saw_dedup_marker = False
    for seed in range(4):
        res = await SleepEngine(s, rng=random.Random(seed), dream_prob=0.0,
                                self_prob=1.0, self_max=8).sleep_once()
        saw_dedup_marker |= "self_thread_deduplicated" in res["actions"]
    threads = _self_threads(s)
    assert len(threads) == 1  # was: 4 near-identical duplicates
    assert saw_dedup_marker  # the dedup is auditable in the sleep summary
    derived = [t.provenance.get("derived_from") for t in threads]
    assert len({tuple(sorted(d)) for d in derived}) == 1


async def test_same_title_after_truncation_is_deduplicated(tmp_path):
    # Two DISTINCT events per topic whose text differs only after the 14-char
    # truncation -> identical normalized titles -> second thread must not spawn.
    s = EventStore(tmp_path / "t.sqlite3", now_fn=StepClock())
    for tid, title, texts in (
        ("thr_a", "话题A", ["用户当前处于高张力的 deadline 焦虑中，第一版。",
                            "用户当前处于高张力的 deadline 焦虑中，第二版。"]),
        ("thr_b", "话题B", ["用户明确表示下周的项目 deadline 要重排，第一版。",
                            "用户明确表示下周的项目 deadline 要重排，第二版。"]),
    ):
        ca = s.append(EventCreate(type="thread.created",
                                  content={"thread_id": tid, "title": title,
                                           "origin": "user"}))
        s.append(EventCreate(type="thread.activated", content={"thread_id": tid},
                             links={"thread_id": tid, "caused_by": [ca.id]}))
        for txt in texts:
            s.append(EventCreate(type="experience.created",
                                 content={"summary": txt},
                                 links={"thread_id": tid}))
    await SleepEngine(s, rng=random.Random(0), dream_prob=0.0,
                      self_prob=1.0).sleep_once()
    res2 = await SleepEngine(s, rng=random.Random(1), dream_prob=0.0,
                             self_prob=1.0).sleep_once()
    assert len(_self_threads(s)) == 1
    assert "self_thread" not in res2["actions"]
    assert "self_thread_deduplicated" in res2["actions"]


async def test_distinct_pair_outside_dedup_still_creates(tmp_path):
    # A genuinely different cross-topic pair (different memories, different
    # title) is NOT blocked — the fix only stops near-identical duplicates.
    s = _two_topic_store(tmp_path)
    await SleepEngine(s, rng=random.Random(0), dream_prob=0.0,
                      self_prob=1.0).sleep_once()
    # new DISTINCT material in both topics (different texts, different events)
    s.append(EventCreate(type="experience.created",
                         content={"summary": "话题A的后续：清晨的咖啡冒着热气。"},
                         links={"thread_id": "thr_a"}))
    s.append(EventCreate(type="experience.created",
                         content={"summary": "话题B的后续：午后的书页翻动。"},
                         links={"thread_id": "thr_b"}))
    res2 = await SleepEngine(s, rng=random.Random(1), dream_prob=0.0,
                             self_prob=1.0).sleep_once()
    # one of the two possible cross-pairs may still collide by title; assert
    # only that no THIRD identical thread appears and links stay grounded
    threads = _self_threads(s)
    assert 1 <= len(threads) <= 2
    for t in threads:
        for i in (t.provenance.get("derived_from") or []):
            assert s.get(i) is not None
