#!/usr/bin/env python3
"""Round 11 qualitative extraction: per-seed self-thread titles, per-thread
thought counts, and the dominant thread's thought texts (first 8, truncated).

Usage: round11_traces.py <run_dir> [<run_dir> ...]  (dirs containing runs/active-v1/<fp>/seedN)
"""
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

MAX_TEXT = 160


def load_events(db_path):
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "select id, type, content_json, links_json, metadata_json, created_at"
        " from events order by seq").fetchall()
    evs = []
    for eid, typ, cj, lj, mj, ts in rows:
        evs.append({"id": eid, "type": typ,
                    "content": json.loads(cj or "{}"),
                    "links": json.loads(lj or "{}"),
                    "metadata": json.loads(mj or "{}"),
                    "ts": ts})
    return evs


def extract(seed_dir):
    evs = load_events(seed_dir / "world.sqlite3")
    report = json.loads((seed_dir / "report.json").read_text()) \
        if (seed_dir / "report.json").exists() else {}
    titles = {}
    for e in evs:
        if e["type"] == "thread.created" and e["content"].get("origin") == "self":
            titles[e["content"].get("thread_id") or e["id"]] = e["content"].get("title")
    by_thread = defaultdict(list)
    for e in evs:
        if e["type"] == "thought.created" and \
                e["metadata"].get("origin") in ("self_thread", "private_project"):
            tid = e["links"].get("thread_id") or e["content"].get("thread_id")
            by_thread[tid].append(e)
    counts = Counter({t: len(v) for t, v in by_thread.items()})
    dom_tid, dom_n = (counts.most_common(1)[0] if counts else (None, 0))
    route_of_dom = Counter(
        t["content"].get("route") or t["metadata"].get("route") or "?"
        for t in by_thread.get(dom_tid, []))
    out = {
        "seed_dir": str(seed_dir),
        "conc": report.get("revival", {}).get("self_thread_concentration"),
        "self_thoughts": report.get("revival", {}).get("self_thoughts"),
        "h1": report.get("h1_genesis_chains", {}).get("count"),
        "threads": [
            {"thread_id": t, "title": titles.get(t), "thoughts": n}
            for t, n in counts.most_common()],
        "dominant": {
            "thread_id": dom_tid, "title": titles.get(dom_tid), "n": dom_n,
            "routes": dict(route_of_dom),
            "texts": [t["content"].get("text", "")[:MAX_TEXT]
                      for t in by_thread.get(dom_tid, [])[:8]],
        },
    }
    return out


def main():
    for run_dir in sys.argv[1:]:
        rd = Path(run_dir)
        print(f"=== {rd} ===")
        for seed_dir in sorted(rd.glob("runs/active-v1/*/seed*")):
            print(json.dumps(extract(seed_dir), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
