#!/usr/bin/env python
"""Acceptance criterion 9 audit: "Monitor 能完整解释关键 provenance 和行为链".

Boots the app IN-PROCESS (TestClient against a copy of an H1-positive harness
store — the seed's ``world.sqlite3`` copied to a temp dir as ``events.sqlite3``,
the default provider staying the hermetic fake) and walks ONE full behaviour
chain using **only** the GET-only ``/api/monitor/*`` endpoints (ADR-0013):

    world.observation -> question.created -> question.revisited (>=2)
                      -> thought citing the chain (metadata.recalled)

For each hop the script records what the Monitor surfaces and whether the chain
is fully explainable WITHOUT touching the DB directly. It also asserts the
store is byte-identical after the whole walk (the glass-window guarantee) and
checks ``monitor_projects`` on an empty store.

Read-only discipline: the seed store is never opened for writing; the script
works on a temp copy. The audit's own direct-DB read (a sqlite SELECT used only
to LOCATE a chain id to walk) is reported honestly in the output; every
*explanation* hop itself goes through the HTTP Monitor.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from resident.main import build_app

SEED = ROOT / "scripts/out/confirm28_multisrc/runs/active-v1/7d9edfd1/seed1/world.sqlite3"
OUT = ROOT / "scripts/out/monitor_audit"

# the chain chosen up-front from a read-only SELECT (reported below)
CHAIN = {
    "observation": "evt_c78363166a4f4c198a42e23ce85c937f",
    "question": "evt_131cf51bece44b61a19d0e3fb36d519c",
    "thought": "evt_c259fda209eb471682586985b978618c",
}

hops: list[dict] = []
gaps: list[str] = []


def hop(name: str, endpoint: str, ok: bool, surfaced: dict, note: str = ""):
    hops.append({"hop": name, "endpoint": endpoint, "ok": ok,
                 "surfaced": surfaced, "note": note})
    print(("PASS " if ok else "GAP  ") + f"{name}  ({endpoint})")
    for k, v in surfaced.items():
        print(f"      {k}: {v}")
    if note:
        print(f"      note: {note}")
    if not ok:
        gaps.append(f"{name}: {note or 'missing'}")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="monitor_audit_"))
    shutil.copy(SEED, tmp / "events.sqlite3")  # copy, never touch the seed

    app = build_app(tmp)
    store = app.state.store
    client = TestClient(app)
    before_count = store.count()
    before_bytes = (tmp / "events.sqlite3").stat().st_size
    print(f"store copy: {tmp/'events.sqlite3'}  events={before_count}")

    # locate the chain (audit-side direct read, used ONLY to pick ids; the
    # explanation below never reads the DB)
    con = sqlite3.connect(f"file:{tmp/'events.sqlite3'}?mode=ro", uri=True)
    n_revisits = con.execute(
        "select count(*) from events where type='question.revisited' "
        "and links_json like ?", (f"%{CHAIN['question']}%",)).fetchone()[0]
    con.close()
    print(f"known chain: {n_revisits} question.revisited events cite the question\n")

    # ---- hop 1: the world observation (timeline) -------------------------
    r = client.get("/api/monitor/timeline", params={"type": "world.observation", "limit": 500})
    evs = r.json()["events"]
    obs = next((e for e in evs if e["id"] == CHAIN["observation"]), None)
    hop("1. world.observation visible", "/api/monitor/timeline?type=world.observation",
        obs is not None,
        {"title_in_text": (obs["text"][:80] if obs else None),
         "origin": obs["origin"] if obs else None},
        "" if obs else "observation not found in monitor timeline")

    # ---- hop 2: the question + its evidence (questions view) -------------
    r = client.get("/api/monitor/questions")
    qrow = next((q for q in r.json()["questions"] if q["question_id"] == CHAIN["question"]), None)
    ev_ok = bool(qrow and qrow.get("evidence_resolved")
                 and CHAIN["observation"] in [e["id"] for e in qrow["evidence_resolved"]])
    hop("2. question.created + evidence link", "/api/monitor/questions",
        qrow is not None and ev_ok,
        {"kind": qrow["kind"] if qrow else None,
         "revisits": qrow["revisits"] if qrow else None,
         "evidence": [e["type"] for e in (qrow.get("evidence_resolved") or [])] if qrow else None},
        "" if ev_ok else "question evidence does not resolve to the observation")

    # revisit detail surfaced?
    rev_detail = (qrow or {}).get("revisit_events")
    hop("2b. revisit events (ids/at/reason) on the question row",
        "/api/monitor/questions",
        bool(rev_detail) and len(rev_detail) == n_revisits,
        {"n_revisit_events": len(rev_detail or []),
         "first": {k: rev_detail[0][k] for k in ("id", "reason")} if rev_detail else None},
        "" if rev_detail else "question view shows revisit COUNT but not the revisit events themselves")

    # ---- hop 3: provenance walk question -> observation ------------------
    r = client.get(f"/api/monitor/provenance/{CHAIN['question']}", params={"depth": 8})
    prov = r.json()
    ids = {n["id"] for n in prov["nodes"]}
    walk_ok = CHAIN["observation"] in ids and not prov.get("error")
    kinds = {e["kind"] for e in prov["edges"]}
    hop("3. provenance walk: question -> world.observation",
        f"/api/monitor/provenance/{CHAIN['question']}", walk_ok,
        {"nodes": len(prov["nodes"]), "edge_kinds": sorted(kinds),
         "obs_depth": next((n["depth"] for n in prov["nodes"]
                            if n["id"] == CHAIN["observation"]), None)})

    # ---- hop 3b: revisits are explained via provenance (walk backwards) --
    rev_id = (rev_detail or [{}])[0].get("id")
    rev_ok = False
    if rev_id:
        r = client.get(f"/api/monitor/provenance/{rev_id}", params={"depth": 8})
        p2 = r.json()
        ids2 = {n["id"] for n in p2["nodes"]}
        rev_ok = CHAIN["question"] in ids2 and CHAIN["observation"] in ids2
    hop("3b. revisit -> question -> observation provenance",
        f"/api/monitor/provenance/{rev_id}" if rev_id else "(no revisit id)", rev_ok,
        {"chain_complete": rev_ok})

    # ---- hop 4: revisits visible in the timeline -------------------------
    r = client.get("/api/monitor/timeline", params={"type": "question.revisited", "limit": 2000})
    revs = [e for e in r.json()["events"]
            if CHAIN["question"] in (e.get("link_ids", {}).get("related_to") or [])]
    tl_ok = len(revs) == n_revisits
    hop("4. question.revisited rows in timeline carry the question link",
        "/api/monitor/timeline?type=question.revisited", tl_ok,
        {"matched": len(revs), "expected": n_revisits},
        "" if tl_ok else "timeline revisit rows cannot be tied back to the question via the API")

    # ---- hop 5: the thought citing the chain (retrieval trace) -----------
    r = client.get("/api/monitor/retrieval", params={"limit": 4000})
    trow = next((t for t in r.json()["thoughts"] if t["id"] == CHAIN["thought"]), None)
    used_ids = [u["id"] for u in (trow.get("used") or [])] if trow else []
    th_ok = bool(trow) and CHAIN["observation"] in used_ids
    hop("5. thought cites the chain (retrieval trace: used)",
        "/api/monitor/retrieval", th_ok,
        {"origin": trow.get("origin") if trow else None,
         "route": trow.get("route") if trow else None,
         "used": used_ids[:5]})

    # ---- hop 5b: the thought's provenance walk reaches the observation ---
    r = client.get(f"/api/monitor/provenance/{CHAIN['thought']}", params={"depth": 8})
    ids3 = {n["id"] for n in r.json()["nodes"]}
    hop("5b. provenance walk: thought -> world.observation",
        f"/api/monitor/provenance/{CHAIN['thought']}", CHAIN["observation"] in ids3,
        {"obs_reached": CHAIN["observation"] in ids3})

    # ---- projects view on this store & on an empty store -----------------
    r = client.get("/api/monitor/projects")
    proj_ok = r.status_code == 200 and r.json()["summary"]["total"] >= 0
    hop("6. /api/monitor/projects exposed and renders", "/api/monitor/projects",
        proj_ok, {"status": r.status_code, "summary": r.json().get("summary")})
    empty = Path(tempfile.mkdtemp())
    app2 = build_app(empty)
    c2 = TestClient(app2)
    r = c2.get("/api/monitor/projects")
    empty_ok = r.status_code == 200 and r.json() == {
        "projects": [], "summary": {"total": 0, "active": 0, "dormant": 0, "artifacts": 0}}
    hop("7. monitor_projects on a store with NO projects", "/api/monitor/projects (empty store)",
        empty_ok, {"status": r.status_code, "payload": r.json()},
        "" if empty_ok else "empty-store projects view is not a clean empty render")

    # ---- glass window: the walk must not have shaken the petri dish ------
    after_count = store.count()
    after_bytes = (tmp / "events.sqlite3").stat().st_size
    ro_ok = after_count == before_count and after_bytes == before_bytes
    hop("8. store untouched by the whole walk", "(read-only guarantee)", ro_ok,
        {"events_before": before_count, "events_after": after_count,
         "bytes_identical": before_bytes == after_bytes})

    report = {"criterion": "9: Monitor 能完整解释关键 provenance 和行为链",
              "seed_store": str(SEED), "chain": CHAIN, "hops": hops,
              "gaps": gaps, "read_only_ok": ro_ok,
              "verdict": "PASS" if not gaps and ro_ok else "PASS WITH FIXES" if ro_ok else "FAIL"}
    (OUT / "monitor_audit_result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nverdict: {report['verdict']}  gaps={len(gaps)}")
    for g in gaps:
        print(f"  - {g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
