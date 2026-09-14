#!/usr/bin/env python3
"""Live-world evidence collector (Day-7 decision package, ADR-0017/0016).

100% READ-ONLY audit of the live resident's store (resident_home/events.sqlite3):

- The store is opened with sqlite3 in ``mode=ro`` and a consistent snapshot is
  taken with the sqlite3 backup API into a TEMP directory. All analysis (and
  the reused harness ``build_report`` — which needs an EventStore that runs
  ``CREATE TABLE IF NOT EXISTS`` on open) happens on the TEMP COPY. The live
  store is never written, never locked beyond the read snapshot, and no POST
  endpoint is called. Raw artifacts go to NEW files only (one dated JSON +
  markdown per run; an existing file for today gets an ``_HHMM`` suffix —
  never overwritten).

Computes over a --since window (default: whole store) plus trailing 24h/7d
slices:
  - self_thread_concentration (trailing 7d) + per-thread counts + dominant
    thread texts (substantive vs route-meta via META_PREFIXES),
  - self/world/noop/user origin shares; noop ratio (wake.completed
    result==noop — wake.noop is never summed with wake.completed),
  - live H1 genesis chains via the FIXED harness detector (h1_recompute /
    active_living_harness.build_report) + pre-genesis candidates
    (multi-source >=7d questions not yet meeting full H1),
  - guardrail failure flags (harness thresholds),
  - questions formed/revisited/dormant, world obs/day, route entropy,
    mean recall age (from thought recall provenance, via build_report).

Usage:
  .venv/bin/python scripts/live_evidence_daily.py \
      [--store ../resident_home/events.sqlite3] [--since 2026-09-12T00:00:00+00:00] \
      [--out scripts/out/live_evidence]
"""
import argparse
import json
import os
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from active_living_harness import META_PREFIXES, build_report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from resident.event_store import EventStore  # noqa: E402
from resident.world import question_state  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_STORE = REPO / "resident_home" / "events.sqlite3"
DEFAULT_OUT = Path(__file__).resolve().parent / "out" / "live_evidence"


# --------------------------------------------------------------- read-only IO

def snapshot_store(src: Path, tmp: Path) -> None:
    """Consistent read-only snapshot: open the live store mode=ro and use the
    sqlite3 backup API into a temp file. The source is never written."""
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            con.backup(dst)
        finally:
            dst.close()
    finally:
        con.close()


def windowed_copy(snapshot: Path, since: str | None, tmp: Path) -> None:
    """Copy the snapshot (a temp file we own) and drop events older than
    `since`, so build_report reads a window-scoped store."""
    tmp.write_bytes(snapshot.read_bytes())
    if since is None:
        return
    con = sqlite3.connect(str(tmp))
    try:
        con.execute("DELETE FROM events WHERE created_at < ?", (since,))
        con.commit()
    finally:
        con.close()


def _ts(iso):
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------- live-specific optics

def thread_breakdown(tmp_db: Path, since: datetime | None) -> dict:
    """Per-thread self-thought counts + dominant thread texts, classified
    substantive vs route-meta via META_PREFIXES (trailing 7d by default)."""
    con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT id, created_at, content_json, links_json, metadata_json "
            "FROM events WHERE type='thought.created' ORDER BY seq ASC"
        ).fetchall()
    finally:
        con.close()
    threads: dict[str, dict] = {}
    n_self = 0
    for r in rows:
        meta = json.loads(r["metadata_json"] or "{}")
        if meta.get("origin") not in ("self_thread", "private_project"):
            continue
        t = _ts(r["created_at"])
        if since and t and t < since:
            continue
        n_self += 1
        content = json.loads(r["content_json"] or "{}")
        links = json.loads(r["links_json"] or "{}")
        tid = links.get("thread_id") or content.get("thread_id") or "(none)"
        text = content.get("text") or ""
        is_meta = text.startswith(META_PREFIXES)
        th = threads.setdefault(tid, {"thread_id": tid, "thoughts": 0,
                                      "meta": 0, "substantive": 0,
                                      "first": r["created_at"],
                                      "last": r["created_at"],
                                      "texts": []})
        th["thoughts"] += 1
        th["meta" if is_meta else "substantive"] += 1
        th["last"] = max(th["last"], r["created_at"])
        if len(th["texts"]) < 5:
            th["texts"].append({"meta": is_meta, "text": text[:160]})
    ranked = sorted(threads.values(), key=lambda th: -th["thoughts"])
    for th in ranked:
        th["concentration"] = (round(th["thoughts"] / n_self, 3)
                               if n_self else None)
    dominant = ranked[0] if ranked else None
    return {
        "self_thoughts": n_self,
        "n_threads": len(threads),
        "concentration": (round(dominant["thoughts"] / n_self, 3)
                          if dominant and n_self else None),
        "dominant_thread": dominant,
        "threads": ranked[:10],
        "note": ("meta = text starts with a _META_PREFIXES tag (route-meta "
                 "stub); substantive = everything else"),
    }


def genesis_candidates(tmp_db: Path) -> dict:
    """Pre-genesis candidates: questions whose evidence/revisits reach toward
    the H1 criteria (>=2 revisits caused_by world.observation, >=2 distinct
    sources, >=7d span) but do not (yet) complete the full H1 chain."""
    con = sqlite3.connect(f"file:{tmp_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        qs = con.execute(
            "SELECT id, created_at, content_json, links_json, metadata_json "
            "FROM events WHERE type='question.created' ORDER BY seq ASC"
        ).fetchall()
        rvs = con.execute(
            "SELECT id, created_at, content_json, links_json "
            "FROM events WHERE type='question.revisited' ORDER BY seq ASC"
        ).fetchall()
        ev_rows = {r["id"]: r for r in con.execute(
            "SELECT id, type, content_json, provenance_json FROM events")}
    finally:
        con.close()
    rv_by_q: dict[str, list] = {}
    for r in rvs:
        content = json.loads(r["content_json"] or "{}")
        links = json.loads(r["links_json"] or "{}")
        qid = content.get("question_id")
        caused = links.get("caused_by") or []
        if isinstance(caused, str):
            caused = [caused]
        by_obs = any(
            (ev_rows.get(cid) or {"type": ""})["type"] == "world.observation"
            for cid in caused)
        rv_by_q.setdefault(qid, []).append(
            {"at": r["created_at"], "caused_by_world_observation": by_obs,
             "caused_by": caused})
    candidates = []
    for q in qs:
        meta = json.loads(q["metadata_json"] or "{}")
        evidence = (meta.get("question") or {}).get("evidence") or []
        sources = set()
        conv_free = True
        for eid in evidence:
            row = ev_rows.get(eid)
            if row is None:
                continue
            prov = json.loads(row["provenance_json"] or "{}")
            if prov.get("family") == "conversation" or row["type"].startswith("message."):
                conv_free = False
            sources.add((json.loads(row["content_json"] or "{}").get("source"))
                        or row["type"])
        q_t = _ts(q["created_at"])
        mine = rv_by_q.get(q["id"], [])
        obs_rvs = [r for r in mine if r["caused_by_world_observation"]]
        # revisit-causing world.observations also contribute sources to the
        # chain's source set (mirrors the fixed H1 detector's measurement)
        for r in mine:
            for cid in r.get("caused_by") or []:
                row = ev_rows.get(cid)
                if row is not None and row["type"] == "world.observation":
                    sources.add(
                        (json.loads(row["content_json"] or "{}").get("source"))
                        or row["type"])
        span = None
        if mine and q_t:
            last = max((_ts(r["at"]) for r in mine), default=None)
            if last:
                span = round((last - q_t).total_seconds() / 86400.0, 2)
        candidates.append({
            "question_id": q["id"],
            "topic": (json.loads(q["content_json"] or "{}").get("topic")),
            "created_at": q["created_at"],
            "evidence_sources": sorted(sources),
            "n_evidence": len(evidence),
            "revisits": len(mine),
            "revisits_caused_by_world_observation": len(obs_rvs),
            "span_days": span,
            "provenance_conversation_free": conv_free,
            "h1_progress": {
                "revisits>=2": len(obs_rvs) >= 2,
                "sources>=2": len(sources) >= 2,
                "span>=7d": bool(span and span >= 7.0),
                "conversation_free": conv_free,
            },
        })
    return {"n_questions": len(candidates), "candidates": candidates}


# --------------------------------------------------------------------- report

def analyze(store_path: Path, since: str | None, out: Path) -> dict:
    now = datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="live_evidence_") as td:
        td = Path(td)
        snap = td / "snapshot.sqlite3"
        snapshot_store(store_path, snap)

        windows = {"24h": now - timedelta(hours=24),
                   "7d": now - timedelta(days=7)}
        results = {}
        for label, since_dt in windows.items():
            db = td / f"w_{label}.sqlite3"
            windowed_copy(snap, since_dt.isoformat(), db)
            store = EventStore(db)
            n_events = store.count()
            latest = store.latest()
            span_days = None
            if latest and n_events:
                first = store.list(1, order="asc")[0]
                d0, d1 = _ts(first.created_at), _ts(latest.created_at)
                if d0 and d1:
                    span_days = max(1.0, (d1 - d0).total_seconds() / 86400.0)
            report = build_report(store, arm="active-v1-live", cfg={
                "window": label, "since": since_dt.isoformat(),
                "store": str(store_path)}, fp="live", days=7, run_dir=td)
            report["window"] = {"label": label, "since": since_dt.isoformat(),
                                "n_events": n_events, "span_days": span_days}
            report["threads_7d"] = thread_breakdown(
                db, since_dt if label == "7d" else None) if label == "7d" \
                else None
            report["_span_days"] = span_days
            results[label] = report

        # whole store
        db = td / "whole.sqlite3"
        windowed_copy(snap, since, db)
        store = EventStore(db)
        whole = build_report(store, arm="active-v1-live",
                             cfg={"window": "whole", "since": since,
                                  "store": str(store_path)},
                             fp="live", days=7, run_dir=td)
        first = store.list(1, order="asc")[0] if store.count() else None
        latest = store.latest()
        span = None
        if first and latest:
            d0, d1 = _ts(first.created_at), _ts(latest.created_at)
            if d0 and d1:
                span = (d1 - d0).total_seconds() / 86400.0
        whole["window"] = {"label": "whole", "since": since,
                           "n_events": store.count(),
                           "first_event": first.created_at if first else None,
                           "span_days": round(span, 2) if span else None}
        whole["_span_days"] = span
        if span:
            whole["cognition"]["questions_per_day"] = round(
                whole["cognition"]["questions_total"] / max(span, 1), 2)
            whole["vitals"]["world_obs_per_day"] = round(
                whole["vitals"]["world_obs_per_day"] * 7 / max(span, 1), 2) \
                if span else None
        qs = question_state(store, now_iso=latest.created_at if latest else None)
        whole["questions"] = {
            "total": qs["n_total"],
            "with_revisits": len([q for q in qs["questions"] if q["revisits"]]),
            "dormant": qs["n_dormant"],
            "detail": [{"id": q["question_id"], "topic": q["topic"], "revisits": q["revisits"],
                        "status": q.get("status"), "last_activity": q.get("last_touched_at")}
                       for q in qs["questions"]],
        }
        whole["threads_7d"] = thread_breakdown(db, None)
        whole["genesis_candidates"] = genesis_candidates(db)
        results["whole"] = whole

    # assemble artifact
    def slim(r):
        r.pop("_span_days", None)
        return r

    artifact = {
        "kind": "live_evidence_reading",
        "generated_at": now.isoformat(),
        "store": str(store_path),
        "read_only": True,
        "method": ("sqlite mode=ro + backup-API snapshot to temp; harness "
                   "build_report (fixed 2026-09-13 H1 detector) on the temp "
                   "copy; live store never written"),
        "noop_accounting": ("noop ratio = wake.completed(result==noop) / "
                            "wake.started; wake.noop is never summed with "
                            "wake.completed"),
        "windows": {k: slim(v) for k, v in results.items()},
    }
    out.mkdir(parents=True, exist_ok=True)
    date = now.strftime("%Y%m%d")
    base = out / f"live_evidence_{date}"
    jp = base.with_suffix(".json")
    mp = base.with_suffix(".md")
    suffix = ""
    if jp.exists() or mp.exists():
        suffix = "_" + now.strftime("%H%M")
        jp = out / f"live_evidence_{date}{suffix}.json"
        mp = out / f"live_evidence_{date}{suffix}.md"
    jp.write_text(json.dumps(artifact, ensure_ascii=False, indent=2,
                             default=str), encoding="utf-8")
    mp.write_text(render_md(artifact), encoding="utf-8")
    return {"json": str(jp), "md": str(mp), "artifact": artifact}


def render_md(a: dict) -> str:
    w = a["windows"]
    lines = [f"# Live evidence reading — {a['generated_at'][:19]}Z",
             "",
             f"Store: `{a['store']}` (read-only snapshot; nothing written).",
             f"Method: {a['method']}",
             ""]
    for label in ("whole", "24h", "7d"):
        r = w.get(label)
        if not r:
            continue
        win = r["window"]
        lines += [f"## Window: {label} ({win.get('n_events')} events)",
                  ""]
        v = r["vitals"]
        lines += [
            f"- wakes {v['wakes']} (noop_ratio {v['noop_ratio']}), "
            f"world obs/day {v['world_obs_per_day']}, "
            f"night_wake_share {v['night_wake_share']}",
            f"- origin shares: {r['cognition']['origin_shares']}",
            f"- route entropy {r['cognition']['route_entropy']}, "
            f"questions {r['cognition']['questions_total']}, "
            f"revisit_rate {r['cognition']['question_revisit_rate']}, "
            f"dormant_rate {r['cognition']['question_dormant_rate']}",
            f"- mean_recall_age {r['memory']['mean_recall_age_days']} days, "
            f"meta_recall_share {r['memory']['meta_recall_share']}, "
            f"echo_share {r['memory']['echo_share']}",
            f"- self_thread_concentration {r['revival']['self_thread_concentration']} "
            f"(self thoughts {r['revival']['self_thoughts']})",
            f"- H1 chains: {r['h1_genesis_chains']['count']}",
            f"- failure flags: "
            f"{[k for k, ok in r['failure_modes'].items() if ok] or 'none'}",
            ""]
        if r.get("threads_7d"):
            t = r["threads_7d"]
            lines += [f"### Threads (self thoughts {t['self_thoughts']}, "
                      f"{t['n_threads']} threads, concentration "
                      f"{t['concentration']})", ""]
            for th in t["threads"][:5]:
                lines.append(f"- `{th['thread_id']}`: {th['thoughts']} thoughts "
                             f"({th['substantive']} substantive / {th['meta']} "
                             f"meta), last {th['last'][:19]}")
                for tx in th["texts"][:2]:
                    lines.append(f"  - [{'meta' if tx['meta'] else 'subst'}] "
                                 f"{tx['text']}")
            lines.append("")
    g = w["whole"].get("genesis_candidates") or {}
    lines += [f"## Genesis candidates ({g.get('n_questions', 0)} questions)", ""]
    for c in (g.get("candidates") or []):
        hp = c["h1_progress"]
        lines.append(f"- {c['topic'] or c['question_id']}: revisits "
                     f"{c['revisits_caused_by_world_observation']} (by world.obs), "
                     f"sources {c['evidence_sources']}, span {c['span_days']}d "
                     f"[{'/'.join(k for k, ok in hp.items() if ok) or 'none met'}]")
    q = w["whole"].get("questions") or {}
    lines += ["", f"## Questions: {q.get('total')} total, "
              f"{q.get('with_revisits')} revisited, {q.get('dormant')} dormant", ""]
    lines.append("\n---\nGenerated by backend/scripts/live_evidence_daily.py "
                 "(read-only). Append-only artifacts: never overwrite.\n")
    return "\n".join(lines)


def fleet() -> list[Path]:
    """Discover every live store: the main resident_home plus any
    resident_home_live_s* fleet instances. Each has its own events.sqlite3."""
    stores = []
    for d in sorted(REPO.glob("resident_home*")):
        s = d / "events.sqlite3"
        if s.exists():
            stores.append(s)
    return stores


def fleet_summary(since: str | None, out: Path) -> dict:
    """Run analyze() over every live store and write ONE combined fleet
    artifact (per-instance whole-window vitals) alongside the per-store ones."""
    per_instance = []
    for store in fleet():
        res = analyze(store, since, out)
        a = res["artifact"]
        whole = a["windows"]["whole"]
        prof = "active-v1"
        mpath = store.parent / "baseline_manifest.json"
        if mpath.exists():
            prof = json.loads(mpath.read_text()).get("env", {}).get(
                "RESIDENT_PROFILE", "unknown")
        per_instance.append({
            "instance": store.parent.name,
            "profile": prof,
            "store": str(store),
            "per_store_artifact": res["md"],
            "whole": {
                "n_events": whole["window"]["n_events"],
                "wakes": whole["vitals"]["wakes"],
                "noop_ratio": whole["vitals"]["noop_ratio"],
                "self_thread_concentration":
                    whole["revival"]["self_thread_concentration"],
                "self_thoughts": whole["revival"].get("self_thoughts"),
                "h1_chains": whole["h1_genesis_chains"]["count"],
                "meta_recall_share": whole["memory"].get("meta_recall_share"),
                "flags": [k for k, v in whole["failure_modes"].items() if v]
                    or "none",
                "origin_shares": whole["vitals"].get("origin_shares"),
            },
        })
    now = datetime.now(timezone.utc)
    artifact = {
        "kind": "live_evidence_fleet_reading",
        "generated_at": now.isoformat(),
        "read_only": True,
        "instances": per_instance,
    }
    out.mkdir(parents=True, exist_ok=True)
    date = now.strftime("%Y%m%d")
    base = out / f"live_evidence_fleet_{date}"
    suffix = ""
    if base.with_suffix(".json").exists():
        suffix = "_" + now.strftime("%H%M")
        base = out / f"live_evidence_fleet_{date}{suffix}"
    base.with_suffix(".json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    lines = [f"# Live fleet evidence reading — {artifact['generated_at'][:19]}Z", ""]
    for inst in per_instance:
        w = inst["whole"]
        lines += [
            f"## {inst['instance']} ({inst['profile']}) — {w['n_events']} events",
            f"- wakes {w['wakes']} (noop {w['noop_ratio']}), "
            f"conc {w['self_thread_concentration']}, "
            f"self_thoughts {w['self_thoughts']}, h1 {w['h1_chains']}, "
            f"meta_recall {w['meta_recall_share']}, flags {w['flags']}",
            f"- origin {w['origin_shares']}",
            f"- detail: `{inst['per_store_artifact']}`", ""]
    base.with_suffix(".md").write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(base.with_suffix('.json')),
            "md": str(base.with_suffix('.md')), "artifact": artifact}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=os.environ.get(
        "RESIDENT_STORE", str(DEFAULT_STORE)), type=Path)
    ap.add_argument("--since", default=None,
                    help="ISO timestamp; whole-store window starts here")
    ap.add_argument("--out", default=str(DEFAULT_OUT), type=Path)
    ap.add_argument("--fleet", action="store_true",
                    help="audit every resident_home* store (main + live fleet)")
    args = ap.parse_args()
    if args.fleet:
        res = fleet_summary(args.since, args.out)
        a = res["artifact"]
        print(f"[live_evidence_fleet] {len(a['instances'])} instances (read-only)")
        for inst in a["instances"]:
            w = inst["whole"]
            print(f"  {inst['instance']} ({inst['profile']}): "
                  f"{w['n_events']} events, conc={w['self_thread_concentration']}, "
                  f"h1={w['h1_chains']}, flags={w['flags']}")
        print(f"  json: {res['json']}")
        print(f"  md:   {res['md']}")
        return
    store = args.store.resolve()
    if not store.exists():
        raise SystemExit(f"store not found: {store}")
    res = analyze(store, args.since, args.out)
    a = res["artifact"]
    whole = a["windows"]["whole"]
    print(f"[live_evidence] store={a['store']} (read-only)")
    print(f"  whole: {whole['window']['n_events']} events, "
          f"wakes={whole['vitals']['wakes']} "
          f"noop_ratio={whole['vitals']['noop_ratio']} "
          f"conc={whole['revival']['self_thread_concentration']} "
          f"h1={whole['h1_genesis_chains']['count']} "
          f"flags={[k for k, v in whole['failure_modes'].items() if v] or 'none'}")
    print(f"  json: {res['json']}")
    print(f"  md:   {res['md']}")


if __name__ == "__main__":
    main()
