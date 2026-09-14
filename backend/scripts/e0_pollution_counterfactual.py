#!/usr/bin/env python3
"""E0 — de-pollution counterfactual (RESEARCH-transcription-pollution.md).

AUDIT-ONLY, provably non-perturbing (ADR-0010 shadow discipline):
- Reads an EXISTING run's world.sqlite3 read-only; all replay happens in a
  throwaway temp copy of the log. The source store is never written, no live
  process is contacted, no event is appended anywhere, no main-path mind code
  runs. Nothing here can perturb the resident or any baseline.

Question answered: same queries, legacy pool vs legacy pool with
  (P1) route-meta stubs and (P3) echo-copies excluded.
How much of the meta-recall pollution is supply-side (P1+P3)?

Method (per store):
1. Copy the log (raw rows, original ids/seq/created_at) into a temp sqlite.
2. Replay chronologically. At every event carrying ``metadata.recalled``
   (the recorded actual recall), re-derive the recall query + retrieval paths
   the same way ``MindLoop._recall_for_route`` does, and run the SAME legacy
   retrieval twice:
     - replayed-actual : full pool        (fidelity anchor vs the log)
     - counterfactual  : pool minus E0-excluded events (P1+P3 below)
3. Report the harness meta_recall_share (active_living_harness) for the
   recorded, replayed-actual and counterfactual recall lists, plus pool
   composition, recall counts, type entropy and mean recall age.

Exclusion rules (explicit, mechanism-based per the research note, NOT tuned):
- P1 route-meta stub: a ``thought.created`` whose text starts with a route-meta
  prefix (the same _META_PREFIXES the harness uses). These are the
  transcription stubs the fake provider makes the modal store text.
  [world]-tagged observations are KEPT: they are primary material (P4).
- P3 echo-copy: a ``memory.consolidated`` / ``memory.share_candidate`` event
  whose text is a transcription of an earlier event's text — i.e. its
  normalized text contains the normalized text (>=12 chars) of one of the
  events it links to (related_to / source_thought_id / promotion_of), or its
  normalized text is a verbatim duplicate of any earlier event's text.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import math
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.mind_loop import _META_PREFIXES
from resident.providers import HashingEmbeddingProvider

META_PREFIXES = tuple(_META_PREFIXES)
STUB_TYPES = {"thought.created"}
ECHO_TYPES = {"memory.consolidated", "memory.share_candidate"}

# route -> retrieval paths, copied from MindLoop._recall_for_route (sealed).
ROUTE_PATHS = {
    "continuity": ("semantic_near", "revisit"),
    "revisit": ("revisit", "forgotten"),
    "personal": ("semantic_near", "temporal", "revisit"),
    "distant": ("distant", "serendipity", "forgotten"),
    "serendipity": ("serendipity", "distant", "forgotten"),
    "self": ("forgotten", "semantic_near"),
    "": ("semantic_near", "forgotten"),
}
RECALL_SOURCES = ("thought.created", "question.created")


def _norm(t: str) -> str:
    return "".join(t.split()).lower()


def _text_of(content_json: str) -> str:
    try:
        c = json.loads(content_json)
    except json.JSONDecodeError:
        return ""
    for k in ("text", "summary", "claim", "title", "reply", "note"):
        v = c.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _parse_iso(ts):
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _shannon(counter: Counter) -> float:
    n = sum(counter.values())
    if not n:
        return 0.0
    return round(-sum((c / n) * math.log2(c / n) for c in counter.values()), 3)


class _FilteredStore:
    """An EventStore view whose pool EXCLUDES the given event ids.

    Delegates to a real EventStore; every listing drops excluded events
    BEFORE the limit is applied (with a slack fetch), so bounded-window
    retrievals (recent 300/500, recent 10, ...) see the same window size
    over the cleaned pool. Read-only: nothing is ever written.
    """

    def __init__(self, store: EventStore, excluded: set[str]):
        self._store = store
        self._excluded = excluded

    path = property(lambda self: self._store.path)

    def list(self, limit=100, *, type_prefix=None, since=None, order="desc"):
        if limit is None or int(limit) <= 0:
            limit = 100
        raw = self._store.list(int(limit) + len(self._excluded) + 5000,
                               type_prefix=type_prefix, since=since, order=order)
        kept = [e for e in raw if e.id not in self._excluded]
        return kept[: int(limit)]

    def get(self, event_id):
        if event_id in self._excluded:
            return None
        return self._store.get(event_id)

    def latest(self):
        evs = self.list(1, order="desc")
        return evs[0] if evs else None

    def count(self, type_prefix=None):
        return len(self.list(100000, type_prefix=type_prefix, order="asc"))

    def last_user_interaction(self):
        evs = self.list(1, type_prefix="conversation.user_message", order="desc")
        return evs[0] if evs else None


def _copy_log(src: Path, dst: Path) -> int:
    """Create an EMPTY temp store with the same schema (rows are streamed in
    during the chronological replay so no retrieval ever sees the future)."""
    EventStore(dst)  # create schema
    return 0


def _is_echo(row, prior_norm_texts: dict[str, str], rows_by_id: dict) -> bool:
    """P3 rule (mechanism-based): a consolidated/share event whose text is a
    transcription of earlier event text. Causal: judged against prior rows."""
    text = _text_of(row[5])
    if not text:
        return False
    nt = _norm(text)
    try:
        links = json.loads(row[6]) or {}
    except json.JSONDecodeError:
        links = {}
    cand_ids = [lid for lid in (links.get("related_to") or [])
                if isinstance(lid, str)]
    try:
        content = json.loads(row[5]) or {}
    except json.JSONDecodeError:
        content = {}
    src = content.get("source_thought_id")
    if isinstance(src, str):
        cand_ids.append(src)
    try:
        prov = json.loads(row[7]) or {}
    except json.JSONDecodeError:
        prov = {}
    p = prov.get("promotion_of") or prov.get("source_thought_id")
    if isinstance(p, str):
        cand_ids.append(p)
    for cid in cand_ids:
        src_text = prior_norm_texts.get(cid) or (
            rows_by_id[cid]["text"] if cid in rows_by_id else "")
        snt = _norm(src_text)
        if len(snt) >= 12 and snt in nt:
            return True
    return nt in prior_norm_texts.values()  # verbatim duplicate of earlier text


def _recall_query(route: str, recalled_rows: list[dict], store: EventStore) -> tuple[str, str | None, str | None]:
    """Re-derive the query the way _recall_for_route / _thread_query do:
    personal -> last user message text; continuity/revisit -> newest
    substantive (non-meta) text of the thread the recall came from.
    Returns (query, anchor, thread_id)."""
    if route == "personal":
        last = store.last_user_interaction()
        if last is not None:
            return (last.content.get("text") or ""), last.created_at, None
        return "", None, None
    if route in ("continuity", "revisit"):
        thread_id = None
        for r in recalled_rows:
            tid = r["links"].get("thread_id") or r["content"].get("thread_id")
            if tid:
                thread_id = tid
                break
        if not thread_id:
            return "", None, None
        for ev in reversed(store.list(20000, order="asc")):
            tid = ev.links.get("thread_id") or ev.content.get("thread_id")
            if tid != thread_id or not ev.text:
                continue
            if not ev.text.startswith(META_PREFIXES):
                return ev.text, None, thread_id
        return "", None, thread_id
    return "", None, None


def _row_metrics(ids: list[str], rows_by_id: dict, id_ts: dict, t_ts: float) -> dict:
    stub = echo = 0
    ages = []
    fams = Counter()
    for rid in ids:
        r = rows_by_id.get(rid)
        if r is None:
            continue
        text = r["text"]
        if text.startswith(META_PREFIXES) and r["type"] in STUB_TYPES:
            stub += 1
        elif text.startswith(META_PREFIXES):
            stub += 1  # harness meta_recall counts any meta-prefixed source
        if r["echo"]:
            echo += 1
        fams[r["type"]] += 1
        s = id_ts.get(rid)
        if s is not None:
            ages.append((t_ts - s) / 86400.0)
    return {
        "n": len(ids),
        "meta_rows": stub,
        "echo_rows": echo,
        "meta_share": round(stub / len(ids), 4) if ids else None,
        "echo_share": round(echo / len(ids), 4) if ids else None,
        "mean_age_days": round(statistics.mean(ages), 3) if ages else None,
        "type_entropy": _shannon(fams),
    }


def analyze_store(src: Path) -> dict | None:
    tmpdir = Path(tempfile.mkdtemp(prefix="e0_replay_"))
    try:
        tmp_db = tmpdir / "replay.sqlite3"
        n_rows = _copy_log(src, tmp_db)
        store = EventStore(tmp_db)

        con = sqlite3.connect(src)
        rows = con.execute(
            "SELECT id,created_at,type,actor,visibility,content_json,links_json,"
            "provenance_json,metadata_json FROM events ORDER BY seq"
        ).fetchall()
        con.close()

        rows_by_id: dict[str, dict] = {}
        id_ts: dict[str, float] = {}
        for r in rows:
            try:
                md = json.loads(r[8]) or {}
                links = json.loads(r[6]) or {}
                content = json.loads(r[5]) or {}
            except json.JSONDecodeError:
                md, links, content = {}, {}, {}
            rows_by_id[r[0]] = {
                "id": r[0], "type": r[2], "visibility": r[4], "text": _text_of(r[5]),
                "echo": False, "stub": False, "metadata": md, "links": links,
                "content": content, "created_at": r[1],
            }
            t = _parse_iso(r[1])
            id_ts[r[0]] = t.timestamp() if t else None

        embedder = HashingEmbeddingProvider()
        mr_act = MemoryRetrieval(store, embedder)
        cf_store = _FilteredStore(store, set())
        mr_cf = MemoryRetrieval(cf_store, embedder)
        # both indexes start with EMPTY retrieval counters; the exact actual
        # history is replayed below via record_retrieval (same for both, so
        # the only difference between the arms is the pool composition).
        for ix in (mr_act.index, mr_cf.index):
            ix._retrieval_count.clear()
            ix._last_retrieved.clear()

        excluded: set[str] = set()
        prior_norm: dict[str, str] = {}
        samples = []
        wake_routes = Counter()
        pool_counts = {"n_events": 0, "n_text_events": 0, "stubs": 0, "echoes": 0}
        n_recall_thoughts = 0

        # chronological streaming: the temp store only ever contains the log
        # PREFIX, so no replayed retrieval can see the future.
        out = sqlite3.connect(tmp_db)
        ptr = 0
        INSERT_SQL = ("INSERT OR IGNORE INTO events(id,created_at,type,actor,"
                      "visibility,content_json,links_json,provenance_json,"
                      "metadata_json) VALUES(?,?,?,?,?,?,?,?,?)")

        for idx, r in enumerate(rows):
            while ptr <= idx:  # stream rows up to and including this one
                out.execute(INSERT_SQL, rows[ptr])
                ptr += 1
            out.commit()
            rid, created_at, etype, vis = r[0], r[1], r[2], r[4]
            rec = rows_by_id[rid]
            if vis == "system":
                continue
            text = rec["text"]
            # P1: route-meta stub (thoughts only; [world]-tagged observations
            # are primary material and stay in the pool)
            if etype in STUB_TYPES and text.startswith(META_PREFIXES):
                rec["stub"] = True
                excluded.add(rid)
                pool_counts["stubs"] += 1
            # P3: echo-copy
            if etype in ECHO_TYPES and _is_echo(r, prior_norm, rows_by_id):
                rec["echo"] = True
                excluded.add(rid)
                pool_counts["echoes"] += 1
            pool_counts["n_events"] += 1
            if text:
                pool_counts["n_text_events"] += 1
            if text:
                prior_norm[rid] = _norm(text)

            cf_store._excluded = excluded
            md = rec["metadata"]
            recalled = md.get("recalled") if isinstance(md.get("recalled"), list) else None
            if recalled is None and etype not in RECALL_SOURCES:
                continue
            # replay the retrieval history exactly as the log records it
            valid_ids = [i for i in (recalled or []) if isinstance(i, str) and i in rows_by_id]
            if etype in RECALL_SOURCES:
                for ix in (mr_act.index, mr_cf.index):
                    ix.record_retrieval(valid_ids, created_at)
            if etype != "thought.created" or not valid_ids:
                continue
            n_recall_thoughts += 1
            route = md.get("route") or ""
            wake_routes[route] += 1
            t_ts = id_ts[rid]
            q, anchor, thread_id = _recall_query(route, [rows_by_id[i] for i in valid_ids], store)
            paths = ROUTE_PATHS.get(route, ROUTE_PATHS[""])
            rng = random.Random(f"e0:{rid}")
            kw = {"paths": paths, "n_each": 3, "total": 6, "rng": rng}
            if "temporal" in paths and anchor:
                kw["anchor"] = anchor
            if "revisit" in paths and thread_id:
                kw["thread_id"] = thread_id
            hits_act = [h.id for h in mr_act.multi_recall(q, **kw)]
            hits_cf = [h.id for h in mr_cf.multi_recall(q, **kw)]
            m_rec = _row_metrics(valid_ids, rows_by_id, id_ts, t_ts)
            m_act = _row_metrics(hits_act, rows_by_id, id_ts, t_ts)
            m_cf = _row_metrics(hits_cf, rows_by_id, id_ts, t_ts)
            ov = len(set(valid_ids) & set(hits_act))
            samples.append({
                "route": route, "recorded": m_rec, "replay": m_act,
                "cf": m_cf,
                "fidelity_overlap": round(ov / len(valid_ids), 3) if valid_ids else None,
                "fidelity_exact": hits_act == valid_ids,
            })

        def agg(key):
            vals = [s[key] for s in samples if s[key] is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        def agg_of(field, key):
            vals = [s[field][key] for s in samples if s[field][key] is not None]
            return round(sum(vals) / len(vals), 4) if vals else None

        def sum_of(field, key):
            return sum(s[field][key] or 0 for s in samples)

        pool = {
            **pool_counts,
            "stub_share_of_text_pool": round(pool_counts["stubs"] / pool_counts["n_text_events"], 4)
            if pool_counts["n_text_events"] else None,
            "cf_pool_events": pool_counts["n_events"] - pool_counts["stubs"] - pool_counts["echoes"],
        }
        return {
            "store": str(src),
            "n_events": n_rows,
            "n_recall_thoughts": n_recall_thoughts,
            "pool": pool,
            "recorded_meta_share_harness": agg_of("recorded", "meta_share"),
            "recorded": {k: agg_of("recorded", k) for k in
                         ("n", "meta_rows", "echo_rows", "meta_share", "echo_share",
                          "mean_age_days", "type_entropy")},
            "replay": {k: agg_of("replay", k) for k in
                       ("n", "meta_rows", "echo_rows", "meta_share", "echo_share",
                        "mean_age_days", "type_entropy")},
            "counterfactual": {k: agg_of("cf", k) for k in
                               ("n", "meta_rows", "echo_rows", "meta_share", "echo_share",
                                "mean_age_days", "type_entropy")},
            "totals": {
                "recorded_recalls": sum_of("recorded", "n"),
                "replay_recalls": sum_of("replay", "n"),
                "cf_recalls": sum_of("cf", "n"),
                "recorded_meta_rows": sum_of("recorded", "meta_rows"),
                "replay_meta_rows": sum_of("replay", "meta_rows"),
                "cf_meta_rows": sum_of("cf", "meta_rows"),
            },
            "fidelity": {
                "mean_overlap_with_recorded": agg("fidelity_overlap"),
                "exact_list_match_rate": round(
                    sum(1 for s in samples if s["fidelity_exact"]) / len(samples), 3)
                if samples else None,
            },
            "wake_route_entropy": _shannon(wake_routes),
        }
    finally:
        with contextlib.suppress(Exception):
            out.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


DEFAULT_STORES = [
    "scripts/out/confirm28/runs/active-v1/*/seed*/world.sqlite3",
    "scripts/out/confirm28_enriched2/runs/active-v1/*/seed*/world.sqlite3",
    "scripts/out/confirm28_enriched/runs/active-v1/*/seed*/world.sqlite3",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stores", nargs="*", help="world.sqlite3 paths or globs")
    ap.add_argument("--out", default=None, help="output json path")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    pats = args.stores or DEFAULT_STORES
    paths: list[Path] = []
    for p in pats:
        pp = Path(p)
        if not pp.is_absolute():
            pp = root / "backend" / p
        if pp.is_file():
            paths.append(pp)
        else:
            import glob as _g
            paths.extend(sorted(Path(x) for x in _g.glob(str(pp))))
    if not paths:
        print("no stores found", file=sys.stderr)
        return 1

    results = []
    for p in paths:
        print(f"[e0] {p} ...", flush=True)
        res = analyze_store(p)
        if res:
            results.append(res)
            print(json.dumps({
                "store": res["store"],
                "recorded_meta_share": res["recorded_meta_share_harness"],
                "replay_meta_share": res["replay"]["meta_share"],
                "cf_meta_share": res["counterfactual"]["meta_share"],
                "fidelity_overlap": res["fidelity"]["mean_overlap_with_recorded"],
            }, ensure_ascii=False))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
