"""The Self Monitor's read-only view layer (v0.2 Step 5, ADR-0013).

The Monitor is a **microscope, not Resident's face**. Its single governing
principle: **it may only READ facts** — it never appends an event, never runs
a retrieval, never consolidates, never wakes. Opening the page must not shake
the petri dish. Every function here is a pure projection of the event store
(and, for the retrieval trace, of already-recorded sidecar files); the HTTP
layer exposes them as GET endpoints only, and the test suite asserts that
calling every builder leaves the store byte-count-identical.

It shows only verifiable facts. No mood/curiosity/loneliness percentages —
pseudo-personality numbers would smuggle back in exactly the unearned
internality the project removed. Where a value would be a score, the Monitor
shows the *events* instead (revisits, activations, ages — all derived from
the log).

Views (the phase brief's five):
1. ``monitor_timeline``         — every event incl. system noops, filterable;
2. ``monitor_provenance``       — walk any event's chain back through its
                                  ``caused_by`` / ``related_to`` links;
3. ``monitor_threads``          — active/dormant/quiet threads + revisit
                                  history (no importance score, by design);
4. ``monitor_questions``        — the question lifecycle (Step 3's derived
                                  state: open/dormant, revisits, evidence);
5. ``monitor_continuity``       — absence windows: last seen → gap →
                                  candidate set → return message → selected/noop;
6. ``monitor_retrieval_trace``  — what each thought actually reached for
                                  (route, origin, age, the used material) and,
                                  when a shadow sidecar exists, the legacy vs
                                  semantic comparison — where "transcription
                                  vs the thing itself" becomes visible.
7. ``monitor_projects``         — the private-project lifecycle (ADR-0016:
                                  derived genesis chain, active/dormant,
                                  artifacts); reads empty while the flag is
                                  off and no genesis has occurred.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .event_store import EventStore
from .mental_state import MentalState
from .origin import SELF_ORIGINS, USER_ORIGINS
from .private_projects import project_state
from .world import question_state


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _age_days(created_at: str, now_iso: str | None) -> float | None:
    t = _parse_iso(created_at)
    now = _parse_iso(now_iso)
    if t is None or now is None:
        return None
    return round((now - t).total_seconds() / 86400.0, 2)


def _brief(e) -> dict:
    """The common read-only projection of one event."""
    links = e.links or {}
    return {
        "id": e.id, "seq": e.seq, "type": e.type,
        "visibility": e.visibility, "created_at": e.created_at,
        "text": (e.text or "")[:200],
        "thread_id": e.links.get("thread_id") or e.content.get("thread_id"),
        "origin": (e.metadata or {}).get("origin"),
        "links": {k: (len(v) if isinstance(v, list) else 1)
                  for k, v in links.items() if v},
        # the actual link TARGETS for the causal kinds, so a chain can be
        # followed row-to-row in the timeline without touching the store
        "link_ids": {k: (v if isinstance(v, list) else [v])
                     for k, v in links.items()
                     if k in ("caused_by", "related_to") and v},
    }


# ------------------------------------------------------------------ 1. timeline


def monitor_timeline(store: EventStore, *, limit: int = 200,
                     type_prefix: str | None = None) -> dict:
    """Everything that happened — including system noops. The timeline is the
    honest spine of the monitor: a wake that decided to do nothing is as much
    a fact as a thought."""
    events = store.list(limit, type_prefix=type_prefix, order="desc")
    return {
        "events": [_brief(e) for e in events],
        "counts": {
            "total": store.count(),
            "noops": store.count("wake.completed"),
            "thoughts": store.count("thought.created"),
            "questions": store.count("question.created"),
            "world_observations": store.count("world.observation"),
            "absences": store.count("absence.detected"),
        },
    }


# ---------------------------------------------------------------- 2. provenance


def monitor_provenance(store: EventStore, event_id: str, *, depth: int = 8,
                       max_nodes: int = 80) -> dict:
    """Walk an event's chain back through ``caused_by`` / ``related_to``.

    BFS from the root; every cited id must resolve (the store's dangling-link
    invariant guarantees this upstream, and we tolerate nothing else here).
    Cycles are structurally impossible in a causal log but guarded anyway.
    """
    root = store.get(event_id)
    if root is None:
        return {"error": f"unknown event {event_id!r}", "nodes": [], "edges": []}

    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    frontier = [(root, 0)]
    while frontier and len(nodes) < max_nodes:
        e, d = frontier.pop(0)
        if e.id in nodes:
            nodes[e.id]["depth"] = min(nodes[e.id]["depth"], d)
            continue
        nodes[e.id] = {**_brief(e), "depth": d}
        if d >= depth:
            continue
        # the walk follows the event's own record of what caused/evidenced it:
        # the link fields, and — for thoughts — the recall it grew from
        # (metadata.recalled), which is the retrieval chain made visible
        upstream: list[tuple[str, str]] = []
        for kind in ("caused_by", "related_to"):
            vals = e.links.get(kind) or []
            upstream.extend((kind, v) for v in (vals if isinstance(vals, list) else [vals]))
        recalled = (e.metadata or {}).get("recalled") or []
        upstream.extend(("recalled", v) for v in
                        (recalled if isinstance(recalled, list) else [recalled]))
        for kind, rid in upstream:
            parent = store.get(rid) if isinstance(rid, str) else None
            if parent is None:
                continue
            edge = (e.id, parent.id, kind)
            if edge not in seen:
                seen.add(edge)
                edges.append({"from": e.id, "to": parent.id, "kind": kind})
            frontier.append((parent, d + 1))
    ordered = sorted(nodes.values(), key=lambda n: (n["depth"], n["seq"] or 0))
    return {"root": event_id, "nodes": ordered, "edges": edges}


# ------------------------------------------- 3. threads & 4. questions


def monitor_threads(store: EventStore, *, now_iso: str | None = None) -> dict:
    """All threads with their current state and revisit history.

    There is deliberately NO importance/salience number here: a thread's
    weight *is* its event history, which the reader can see.
    """
    state = MentalState.from_store(store)
    revisits: dict[str, list[dict]] = {}
    for e in store.list(2000, type_prefix="thread.revisited", order="asc"):
        tid = e.content.get("thread_id") or e.links.get("thread_id")
        revisits.setdefault(tid, []).append(
            {"at": e.created_at, "reason": e.content.get("reason", "")})
    creations = {e.content.get("thread_id"): e
                 for e in store.list(2000, type_prefix="thread.created", order="asc")}
    rows = []
    for tid, v in state.threads.items():
        created = creations.get(tid)
        # real thread.created events carry origin in content; tolerate metadata
        origin = None
        if created is not None:
            origin = created.content.get("origin") or (created.metadata or {}).get("origin")
        rows.append({
            "thread_id": tid,
            "title": v.title,
            "origin": origin,
            "created_at": created.created_at if created else None,
            "state": v.state,
            "last_event_at": v.last_event_at,
            "n_events": len(v.event_ids),
            "age_days": _age_days(created.created_at, now_iso) if created else None,
            "revisits": revisits.get(tid, []),
        })
    rows.sort(key=lambda r: (r["last_event_at"] or ""), reverse=True)
    return {
        "threads": rows,
        "summary": {
            "total": len(rows),
            "active": sum(1 for r in rows if r["state"] == "activated"),
            "dormant": sum(1 for r in rows if r["state"] == "dormant"),
        },
    }


def monitor_questions(store: EventStore, *, now_iso: str | None = None,
                      dormant_s: float | None = None) -> dict:
    """The question lifecycle (Step 3's derived state) + resolved evidence."""
    kw = {"now_iso": now_iso}
    if dormant_s is not None:
        kw["dormant_s"] = dormant_s
    state = question_state(store, **kw)
    rows = []
    for q in state["questions"]:
        ev_types = []
        for eid in q["evidence"]:
            e = store.get(eid)
            if e is not None:
                ev_types.append({"id": eid, "type": e.type})
        # the revisit events themselves (not just the count): each carries its
        # id, timestamp and reason, and cites the question via related_to, so
        # the whole revisit chain is explainable from the view alone
        revisit_events = [{"id": e.id, "at": e.created_at,
                           "reason": e.content.get("reason", "")}
                          for e in store.list(5000, type_prefix="question.revisited",
                                              order="asc")
                          if e.content.get("question_id") == q["question_id"]
                          or q["question_id"] in (e.links.get("related_to") or [])]
        rows.append({**q, "evidence_resolved": ev_types,
                     "revisit_events": revisit_events})
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"questions": rows,
            "summary": {"total": state["n_total"], "open": state["n_open"],
                        "dormant": state["n_dormant"],
                        "revisits": sum(r["revisits"] for r in rows)}}


def monitor_projects(store: EventStore, *, now_iso: str | None = None) -> dict:
    """The private-project lifecycle (ADR-0016, read-only): derived genesis
    chain, status (active/dormant), artifacts. Pure projection of the log —
    reads empty when the layer is off (no project.created exists)."""
    state = project_state(store, now_iso=now_iso)
    rows = []
    for p in state["projects"]:
        ev_types = []
        for eid in (p["genesis"].get("evidence") or []):
            e = store.get(eid)
            if e is not None:
                ev_types.append({"id": eid, "type": e.type})
        rows.append({**p, "evidence_resolved": ev_types})
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return {"projects": rows,
            "summary": {"total": state["n_total"], "active": state["n_active"],
                        "dormant": state["n_dormant"],
                        "artifacts": sum(len(r["artifacts"]) for r in rows)}}


# -------------------------------------------------------------- 5. continuity


def monitor_continuity(store: EventStore, *, limit: int = 20) -> dict:
    """Absence windows: last seen → gap → candidates → return → decision.

    Every continuity event is ``caused_by`` the return message, so the
    windows group naturally by that id. Pure log projection.
    """
    last_user = store.last_user_interaction()
    absences = store.list(200, type_prefix="absence.detected", order="desc")[:limit]
    cand_events = store.list(500, type_prefix="reentry.candidate", order="desc")
    sel_events = {e.links.get("caused_by", [None])[0]: e
                  for e in store.list(200, type_prefix="reentry.selected", order="desc")}
    noop_events = {e.links.get("caused_by", [None])[0]: e
                   for e in store.list(200, type_prefix="reentry.noop", order="desc")}

    windows = []
    for a in absences:
        ret_id = (a.links.get("caused_by") or [None])[0]
        ret = store.get(ret_id) if ret_id else None
        since = store.get(a.content.get("since_event_id"))
        cands = [{"cls": e.content.get("cls"), "quote": e.content.get("quote"),
                  "at": e.created_at,
                  "evidence": e.links.get("related_to") or []}
                 for e in cand_events
                 if (e.links.get("caused_by") or [None])[0] == ret_id]
        sel = sel_events.get(ret_id)
        noop = noop_events.get(ret_id)
        windows.append({
            "detected_at": a.created_at,
            "gap_s": a.content.get("gap_s"),
            "since": {"id": a.content.get("since_event_id"),
                      "at": since.created_at if since else None,
                      "text": (since.text or "")[:80] if since else None},
            "return": {"id": ret_id, "at": ret.created_at if ret else None,
                       "text": (ret.content.get("text", "") if ret else "")[:80]},
            "candidates": cands,
            "decision": ("selected" if sel else "noop" if noop else None),
            "selected_classes": sorted(sel.content.get("classes") or []) if sel else [],
        })
    return {
        "last_user_seen": last_user.created_at if last_user else None,
        "windows": windows,
        "summary": {"absences": store.count("absence.detected"),
                    "selected": store.count("reentry.selected"),
                    "noop": store.count("reentry.noop")},
    }


# --------------------------------------------------------- 6. retrieval trace


def monitor_retrieval_trace(store: EventStore, *, limit: int = 40,
                            now_iso: str | None = None,
                            shadow_log_path: str | None = None) -> dict:
    """What each thought actually reached for — and, when a Step-2 shadow
    sidecar exists, what real vectors WOULD have recalled.

    Everything here is already recorded (thought metadata, sidecar file): the
    monitor never retrieves. The raw recall QUERY is only recorded when the
    shadow is on — v1 does not add query logging to the mind's wakes, because
    that would be new mind machinery just for the UI.
    """
    rows = []
    for t in store.list(limit * 3, type_prefix="thought.created", order="desc"):
        if len(rows) >= limit:
            break
        recalled = (t.metadata or {}).get("recalled") or []
        if isinstance(recalled, str):
            recalled = [recalled]
        used = []
        for rid in recalled:
            e = store.get(rid)
            if e is None:
                continue
            used.append({
                "id": rid, "type": e.type,
                "origin": (e.metadata or {}).get("origin"),
                "age_days": _age_days(e.created_at, t.created_at),
                "text": (e.text or "")[:90],
            })
        rows.append({
            "id": t.id, "created_at": t.created_at,
            "route": (t.metadata or {}).get("route"),
            "origin": (t.metadata or {}).get("origin"),
            "thread_id": t.links.get("thread_id") or t.content.get("thread_id"),
            "text": (t.text or "")[:140],
            "used": used,
        })
    shadow = None
    if shadow_log_path and Path(shadow_log_path).exists():
        records = []
        with open(shadow_log_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                records.append({
                    "route": r.get("route"), "query": (r.get("query") or "")[:80],
                    "legacy_top": [(m.get("id"), round(m.get("score", 0.0), 3))
                                   for m in (r.get("legacy_top") or [])[:5]],
                    "semantic_top": [(m.get("id"), round(m.get("score", 0.0), 3))
                                     for m in (r.get("semantic_top") or [])[:5]],
                    "overlap": r.get("overlap"),
                    "top1_differs": r.get("top1_differs"),
                })
        shadow = {"path": str(shadow_log_path), "records": records[-limit:]}
    return {"thoughts": rows, "shadow": shadow,
            "note": ("read-only: the monitor never retrieves; the recall query "
                     "is only on record when the semantic shadow is on")}
