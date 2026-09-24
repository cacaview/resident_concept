"""Private projects (ADR-0016): a project is DERIVED state, not a scored trigger.

Genesis is the harness H1 structural detector (``active_living_harness.py``
``h1_genesis_chains``), computed **in-mind** from the log the way
``question_state`` (ADR-0011) computes dormancy:

- a ``question.created`` whose re-meet graph has >=2 ``question.revisited``
  each caused by a ``world.observation`` (independent re-encounters);
- evidence + revisits spanning >=2 distinct sources and >=7 days;
- the chain ends in self-thought activity (a later ``self_thread`` /
  ``private_project``-origin thought citing the question's material);
- provenance independent of conversation: no evidence event from the
  conversation family.

No numeric curiosity/importance/motivation trigger exists anywhere (ADR-0013
blacklist): genesis either happened in the log or it did not.

Lifecycle (all derived except the single ``project.created`` event):

    project.created -> active -> dormant (derived, no tombstone)
    later chain re-engagement -> revived (derived; question-revival semantics)

"done" is not an event kind; pause/fail/death are facts, not failures.
Artifacts (``artifact.created``, private) carry provenance to the project
chain and re-enter the experience system as world-independent experiences:
retrievable by every existing path, consuming NO world-observation slot (the
world daily cap counts window opens; a creation opens none) — but it IS a
deep read of one's own mind, paying the same attention cost every thought
pays (bounded recent-window shift).

Default OFF (``private_projects_enabled=False`` / env
``RESIDENT_PRIVATE_PROJECTS``): nothing runs, no rng is consumed, no event
shape changes — byte-identical behaviour (tested).
"""
from __future__ import annotations

import os
from datetime import datetime

from .event_store import EventStore
from .models import Event, EventCreate


def flag_enabled() -> bool:
    """The default-off flag (ADR-0016 §6): ``RESIDENT_PRIVATE_PROJECTS``.
    Off = nothing runs, no event shape changes — byte-identical behaviour."""
    return os.environ.get("RESIDENT_PRIVATE_PROJECTS", "0").strip().lower() in (
        "1", "on", "true", "yes")

#: untouched this long → a project is derived-dormant (ADR-0016). An envelope
#: rail mirroring DEFAULT_QUESTION_DORMANT_S, not a tuned value.
DEFAULT_PROJECT_DORMANT_S = 14 * 86400.0
#: the pre-registered H1 structural bars (research note §3; NOT tuned — a
#: negative result keeps them and reports "no genesis", it never lowers them).
MIN_SOURCES = 2
MIN_SPAN_DAYS = 7.0
MIN_REVISITS = 2


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def genesis_candidates(store: EventStore, *, now_iso: str | None = None) -> list[dict]:
    """Pure: every question satisfying the H1 structural genesis criterion.

    Mirrors the harness audit detector exactly (the shadow audit cross-checks
    the two). Reads only event types, ids, sources, timestamps, citations —
    no score anywhere."""
    # one in-memory read: the historical stores hold tens of thousands of
    # events and the citation walk must not hit sqlite per id
    events: dict[str, Event] = {e.id: e for e in store.list(100_000, order="asc")}
    questions = [e for e in events.values() if e.type == "question.created"]
    questions.sort(key=lambda e: e.seq or 0)
    revisits = [e for e in events.values() if e.type == "question.revisited"]
    revisits.sort(key=lambda e: e.seq or 0)
    by_q: dict[str, list] = {}
    for r in revisits:
        qid = r.content.get("question_id")
        if qid is not None:
            by_q.setdefault(qid, []).append(r)
    self_thoughts = [
        t for t in events.values()
        if t.type == "thought.created"
        and (t.metadata or {}).get("origin") in ("self_thread", "private_project")
    ]
    # pre-resolve each self-thought's transitive citation set ONCE (the graph
    # of recalled ids, cycle-guarded); the per-question check is then a set op
    transitive: dict[str, set[str]] = {}

    def _reachable(eid: str, seen: set[str]) -> set[str]:
        if eid in transitive:
            return transitive[eid]
        if eid in seen:
            return set()
        seen.add(eid)
        e = events.get(eid)
        reach = {eid}
        if e is not None:
            recalled = (e.metadata or {}).get("recalled") or []
            for rid in recalled if isinstance(recalled, list) else [recalled]:
                reach |= _reachable(rid, seen)
        transitive[eid] = reach
        return reach

    for t in self_thoughts:
        _reachable(t.id, set())

    out: list[dict] = []
    for q in questions:
        ev_ids = list(((q.metadata or {}).get("question") or {}).get("evidence") or [])
        target_ids = set(ev_ids) | {q.id}
        ev_sources: set[str] = set()
        ev_ok = True
        for eid in ev_ids:
            e = events.get(eid)
            if e is None:
                continue
            if e.family == "conversation":
                ev_ok = False
            ev_sources.add(e.content.get("source") or e.type)
        rvs = by_q.get(q.id, [])
        revisit_ok = all(
            (cid in events and events[cid].type == "world.observation")
            for r in rvs for cid in (r.links.get("caused_by") or [])
        )
        q_t = _parse_iso(q.created_at)
        span = None
        if q_t and rvs:
            parsed = [p for p in (_parse_iso(r.created_at) for r in rvs)
                      if p is not None]
            if parsed:
                span = (max(parsed) - q_t).total_seconds() / 86400.0
        linked_self = any(
            transitive.get(t.id, set()) & target_ids for t in self_thoughts)
        if (len(rvs) >= MIN_REVISITS and len(ev_sources) >= MIN_SOURCES
                and span is not None and span >= MIN_SPAN_DAYS
                and linked_self and ev_ok and revisit_ok):
            out.append({
                "question_id": q.id,
                "topic": q.content.get("topic"),
                "revisits": len(rvs),
                "span_days": round(span, 2),
                "sources": sorted(ev_sources),
                "evidence": ev_ids,
                "created_at": q.created_at,
            })
    return out


def project_state(
    store: EventStore,
    *,
    now_iso: str | None = None,
    dormant_s: float = DEFAULT_PROJECT_DORMANT_S,
) -> dict:
    """Derived project lifecycle (ADR-0016). ``project.created`` is the only
    written event; active/dormant/revived are derived from chain activity,
    never tombstoned. ``now_iso=None`` reads the store's own latest event.

    Status: a project with chain activity (any event citing it, incl. a
    ``question.revisited`` of its genesis question or an artifact) within
    ``dormant_s`` is ``active``; otherwise ``dormant``. A project that was
    dormant and has newer activity than its own last old activity is
    ``active`` again — revival is the same derivation run later (question-
    revival semantics), nothing is resurrected by the engine."""
    created = store.list(5000, type_prefix="project.created", order="asc")
    artifacts = store.list(5000, type_prefix="artifact.created", order="asc")
    revisits = store.list(5000, type_prefix="question.revisited", order="asc")
    thoughts = store.list(5000, type_prefix="thought.created", order="asc")
    now = _parse_iso(now_iso)
    if now is None:
        latest = store.latest()
        now = _parse_iso(latest.created_at) if latest is not None else None

    items: list[dict] = []
    for p in created:
        q_id = p.content.get("question_id")
        # the chain = the genesis question + its evidence (related_to) + the
        # project event itself; ANY later event engaging chain material is
        # chain activity (a thought citing it, an artifact, a re-meet)
        chain = {p.id, *(p.links.get("related_to") or [])}
        activity: list[tuple[str, str]] = []  # (created_at, type)
        for a in artifacts:
            rel = a.links.get("related_to") or []
            rel = rel if isinstance(rel, list) else [rel]
            if set(rel) & chain or a.links.get("project_id") == p.id:
                activity.append((a.created_at, a.type))
        for t in thoughts:
            recalled = (t.metadata or {}).get("recalled") or []
            recalled = recalled if isinstance(recalled, list) else [recalled]
            rel = t.links.get("related_to") or []
            rel = rel if isinstance(rel, list) else [rel]
            if chain & (set(recalled) | set(rel)):
                activity.append((t.created_at, t.type))
        for r in revisits:
            if q_id and r.content.get("question_id") == q_id:
                activity.append((r.created_at, r.type))
        activity.sort()
        last_touch = activity[-1][0] if activity else p.created_at
        status = "active"
        lt = _parse_iso(last_touch)
        if now is not None and lt is not None and (now - lt).total_seconds() > dormant_s:
            status = "dormant"
        items.append({
            "project_id": p.id,
            "name": p.content.get("name"),
            "question_id": q_id,
            "topic": p.content.get("topic"),
            "created_at": p.created_at,
            "last_touched_at": last_touch,
            "status": status,
            "artifacts": [
                {"id": a.id, "text": a.text, "created_at": a.created_at}
                for a in artifacts
                if set(a.links.get("related_to") or []) & chain
                or a.links.get("project_id") == p.id
            ],
            "genesis": p.provenance.get("genesis") or {},
        })
    return {"projects": items, "n_total": len(items),
            "n_active": sum(1 for i in items if i["status"] == "active"),
            "n_dormant": sum(1 for i in items if i["status"] == "dormant")}


def check_genesis(
    store: EventStore,
    *,
    now_iso: str | None = None,
    provenance: dict | None = None,
    enabled: bool | None = None,
) -> list[dict]:
    """Emission (flag-gated): append one ``project.created`` per genesis
    candidate that does not have one yet, keyed by question_id (a second pass
    is a no-op). ``enabled=None`` reads the default-off flag; when off, the
    store is left byte-identical. Returns the created projects' summaries.
    When nothing qualifies, appends nothing (the common case, and the only
    case observed so far: H1=0 in every run to date)."""
    if enabled is None:
        enabled = flag_enabled()
    if not enabled:
        return []
    existing = {
        e.content.get("question_id")
        for e in store.list(5000, type_prefix="project.created", order="asc")
    }
    out: list[dict] = []
    for c in genesis_candidates(store, now_iso=now_iso):
        if c["question_id"] in existing:
            continue
        last_rv = store.list(
            1, type_prefix="question.revisited", order="desc")
        caused = [r.id for r in last_rv
                  if r.content.get("question_id") == c["question_id"]]
        p = store.append(EventCreate(
            type="project.created", visibility="private",
            content={
                "name": f"关于「{c['topic']}」的事",
                "question_id": c["question_id"],
                "topic": c["topic"],
                "status": "active",
            },
            links={
                "caused_by": caused,
                "related_to": [c["question_id"], *c["evidence"]],
            },
            provenance={
                "source": "private_projects",
                "criterion": "h1_genesis_chain",
                "genesis": c,
                **(provenance or {}),
            },
        ))
        out.append({"project_id": p.id, "question_id": c["question_id"],
                    "topic": c["topic"]})
    return out


def record_artifact(
    store: EventStore,
    *,
    project_id: str,
    text: str,
    caused_by: list[str] | None = None,
    provenance: dict | None = None,
) -> dict:
    """Append one ``artifact.created`` (private) with provenance to the
    project chain. The creation re-enters the experience system as a
    world-independent experience: it is retrievable by every existing path
    and consumes NO world-observation slot (ADR-0016 §4)."""
    project = store.get(project_id)
    if project is None or project.type != "project.created":
        raise ValueError(f"unknown project {project_id!r}")
    chain = [e for e in (project.links.get("related_to") or []) if e != project_id]
    a = store.append(EventCreate(
        type="artifact.created", visibility="private",
        content={"text": text, "project_id": project_id},
        links={
            "caused_by": caused_by or [project_id],
            "related_to": [project_id, *chain],
            "project_id": project_id,
        },
        provenance={
            "source": "private_projects",
            "project_id": project_id,
            "world_budget_slot_consumed": False,
            **(provenance or {}),
        },
    ))
    return {"artifact_id": a.id, "project_id": project_id}
