"""Re-entry: candidates come from real events only; private life is not shared."""
from resident.event_store import EventStore
from resident.models import EventCreate
from resident.reentry import ReentryEngine


def make_store(tmp_path, name="e.sqlite3"):
    return EventStore(tmp_path / name)


def append_chat(s, message="回来啦"):
    u = s.append(
        EventCreate(
            type="conversation.user_message",
            actor="user",
            visibility="user_visible",
            content={"text": message},
        )
    )
    s.append(
        EventCreate(
            type="conversation.resident_message",
            visibility="user_visible",
            content={"text": "嗯。"},
            links={"caused_by": [u.id]},
        )
    )
    return u


def test_empty_store_nothing_to_share(tmp_path):
    s = make_store(tmp_path)
    res = ReentryEngine(s).evaluate()
    assert res["decision"] == "nothing_to_share"
    assert res["candidates"] == []
    assert res["baseline"] is None
    assert s.count() == 0  # evaluate() is pure


def test_shareable_thought_after_user_message_is_shared(tmp_path):
    s = make_store(tmp_path)
    u = append_chat(s)
    t = s.append(
        EventCreate(
            type="thought.created",
            visibility="shareable",
            content={"text": "我在想边界问题"},
            links={"caused_by": [u.id]},
        )
    )
    res = ReentryEngine(s).evaluate()
    assert res["decision"] == "share_now"
    assert len(res["candidates"]) == 1
    c = res["candidates"][0]
    assert c["event_ids"] == [t.id]
    assert c["strength"] == 2
    assert "边界问题" in c["claim"]
    # anti-fabrication end-to-end: every cited id resolves
    for i in c["event_ids"]:
        assert s.get(i) is not None
    assert ReentryEngine(s).validate_candidate(c) is True


def test_private_thought_is_not_auto_shared(tmp_path):
    s = make_store(tmp_path)
    append_chat(s)
    s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "私密的念头"})
    )
    res = ReentryEngine(s).evaluate()
    assert res["decision"] == "nothing_to_share"
    assert res["candidates"] == []


def test_weak_candidates_mention_later(tmp_path):
    s = make_store(tmp_path)
    u = append_chat(s)
    s.append(
        EventCreate(
            type="exploration.note",  # strength 1
            visibility="shareable",
            content={"note": "路过"},
            links={"caused_by": [u.id]},
        )
    )
    res = ReentryEngine(s).evaluate()
    assert res["decision"] == "mention_later"
    assert res["candidates"][0]["strength"] == 1


def test_validate_candidate_rejects_nonexistent_event(tmp_path):
    s = make_store(tmp_path)
    eng = ReentryEngine(s)
    assert eng.validate_candidate({"event_ids": ["evt_does_not_exist"]}) is False
    assert eng.validate_candidate({"event_ids": []}) is False


def test_run_records_events_with_resolvable_links(tmp_path):
    s = make_store(tmp_path)
    u = append_chat(s)
    s.append(
        EventCreate(
            type="thought.created",
            visibility="shareable",
            content={"text": "值得分享的想法"},
            links={"caused_by": [u.id]},
        )
    )
    res = ReentryEngine(s).run()
    assert res["decision"] == "share_now"
    evaluated = s.get(res["evaluated_event_id"])
    assert evaluated is not None and evaluated.type == "reentry.evaluated"
    proposed = s.get(res["proposed_event_id"])
    assert proposed is not None and proposed.type == "reentry.proposed"
    for e in (evaluated, proposed):
        for v in e.links.values():
            ids = v if isinstance(v, list) else [v]
            for i in ids:
                if isinstance(i, str) and i.startswith("evt_"):
                    assert s.get(i) is not None

    # the empty path records evaluated + skipped
    s2 = make_store(tmp_path, "e2.sqlite3")
    res2 = ReentryEngine(s2).run()
    assert res2["decision"] == "nothing_to_share"
    assert s2.get(res2["evaluated_event_id"]).type == "reentry.evaluated"
    assert s2.get(res2["skipped_event_id"]).type == "reentry.skipped"


# ------------------------------------------------- deferred promotion tests

def test_no_later_reflection_no_promotion(tmp_path):
    s = make_store(tmp_path)
    t = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "安静的念头"})
    )
    eng = ReentryEngine(s)
    assert eng.consider_promotions() == []
    assert eng.promote() == []
    assert s.count("memory.share_candidate") == 0
    assert s.get(t.id).visibility == "private"  # untouched


def test_later_thought_citation_promotes(tmp_path):
    s = make_store(tmp_path)
    t = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "关于边界的私密想法"})
    )
    later = s.append(
        EventCreate(
            type="thought.created",
            visibility="private",
            content={"text": "我又想到了那件事"},
            metadata={"route": "revisit"},  # a deliberate step-back reflection
            links={"related_to": [t.id]},
        )
    )
    eng = ReentryEngine(s)
    promos = eng.consider_promotions()
    assert len(promos) == 1
    assert promos[0]["source_event_id"] == t.id
    assert promos[0]["justified_by"] == [later.id]
    assert "关于边界的私密想法" in promos[0]["claim"]

    created = eng.promote()
    assert len(created) == 1
    cand = s.get(created[0]["event_id"])
    assert cand is not None
    assert cand.type == "memory.share_candidate"
    assert cand.visibility == "shareable"
    assert created[0]["source_event_id"] == t.id
    assert created[0]["justified_by"] == [later.id]
    related = cand.links["related_to"]
    assert t.id in related and later.id in related
    # provenance points at the original thought and the later reflection
    assert cand.provenance["promotion_of"] == t.id
    assert cand.provenance["justified_by"] == [later.id]
    # the original private thought is NOT mutated
    assert s.get(t.id).visibility == "private"
    assert s.count("memory.share_candidate") == 1


def test_promote_twice_no_duplicate(tmp_path):
    s = make_store(tmp_path)
    t = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "想法A"})
    )
    s.append(
        EventCreate(
            type="thought.created",
            content={"text": "回想"},
            metadata={"route": "revisit"},  # a deliberate step-back reflection
            links={"related_to": [t.id]},
        )
    )
    eng = ReentryEngine(s)
    assert len(eng.promote()) == 1
    assert eng.promote() == []
    assert s.count("memory.share_candidate") == 1


def test_share_candidate_surfaces_in_evaluate(tmp_path):
    s = make_store(tmp_path)
    t = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "独特的边界思考"})
    )
    s.append(
        EventCreate(
            type="thought.created",
            content={"text": "再想"},
            metadata={"route": "revisit"},  # a deliberate step-back reflection
            links={"related_to": [t.id]},
        )
    )
    eng = ReentryEngine(s)
    created = eng.promote()
    assert len(created) == 1
    res = eng.evaluate()
    c = next(x for x in res["candidates"] if x["event_ids"] == [created[0]["event_id"]])
    assert c["strength"] == 2
    assert c["claim"]  # non-empty
    assert eng.validate_candidate(c) is True  # every cited id resolves
    assert res["decision"] == "share_now"


def test_rest_route_or_nothing_never_promotes(tmp_path):
    s = make_store(tmp_path)
    t1 = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "被搁置的念头"})
    )
    # a REST route citing the thought is not a durable reflection
    s.append(
        EventCreate(
            type="wake.route_selected",
            visibility="system",
            content={"route": "rest", "decision": "noop", "reason": "resting"},
            links={"related_to": [t1.id]},
        )
    )
    # a thought cited by nothing durable is not promoted either
    s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": "无人回想的念头"})
    )
    eng = ReentryEngine(s)
    assert eng.consider_promotions() == []
    assert eng.promote() == []
    assert s.count("memory.share_candidate") == 0


def test_promotion_claim_derived_from_original_thought(tmp_path):
    s = make_store(tmp_path)
    original_text = "我想把记忆组织成时间结构"
    t = s.append(
        EventCreate(type="thought.created", visibility="private", content={"text": original_text})
    )
    s.append(
        EventCreate(
            type="thought.created",
            content={"text": "再次想起"},
            metadata={"route": "revisit"},  # a deliberate step-back reflection
            links={"related_to": [t.id]},
        )
    )
    eng = ReentryEngine(s)
    created = eng.promote()
    assert len(created) == 1
    cand = s.get(created[0]["event_id"])
    # the claim carries the ORIGINAL thought's wording — never invented
    assert original_text in cand.content["claim"]
    assert original_text in cand.text
    res = eng.evaluate()
    c = next(x for x in res["candidates"] if x["event_ids"] == [created[0]["event_id"]])
    assert original_text in c["claim"]
