#!/usr/bin/env python3
"""Living Baseline analysis (v0.2, post-Step-5) — READ-ONLY.

Runs against a *living* Resident's event store (default: the real
``resident_home/events.sqlite3``) and answers the four baseline questions
without touching anything:

1. 哪些 question 会自己活下来？   → section "questions"
2. 哪些 thread 会反复回来？       → section "threads"
3. 哪些 world impression 真正留下长期痕迹？ → section "world_traces"
4. 哪些 re-entry candidate 经常出现？       → section "continuity"

Plus vitals (the honest frame: noop share, route distribution, origins) and,
when the Step-2 shadow sidecar is on, the transcription-vs-thing-itself
reading (legacy vs semantic overlap / top-1 divergence).

This script is part of the observation kit, NOT part of the mind: it never
appends, retrieves, or wakes (the glass-window rule, ADR-0013). Run it at
day 1 / day 7 / day 14 of the living window and keep every output.
"""
import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore  # noqa: E402
from resident.monitor import monitor_continuity, monitor_threads  # noqa: E402
from resident.world import question_state  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "baseline_manifest.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO,
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def _seed_set_hash(seeds_path: str) -> str | None:
    p = Path(seeds_path)
    if not p.exists():
        return None
    entries = json.loads(p.read_text(encoding="utf-8"))
    canon = json.dumps(sorted(json.dumps(e, sort_keys=True) for e in entries),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _semantic_fields(env: dict) -> dict:
    main = env.get("RESIDENT_EMBEDDINGS", "legacy")
    shadow_on = env.get("RESIDENT_SEMANTIC_SHADOW", "0") != "0"
    if env.get("RESIDENT_EMBEDDING_BASE_URL") and env.get("RESIDENT_EMBEDDING_MODEL"):
        prov = f"openai-compatible:{env['RESIDENT_EMBEDDING_MODEL']}"
    else:
        prov = "local/sketch-semantic-v1"
    return {"semantic_main": main,
            "semantic_shadow": (f"{prov} on" if shadow_on else "off")}


def _config_fingerprint(env: dict, seeds_hash: str | None) -> str:
    payload = json.dumps({"env": env, "seeds": seeds_hash}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _baseline_env(environ: dict) -> dict:
    """The observation-relevant subset of the environment (never secrets)."""
    keys = ("RESIDENT_WORLD", "RESIDENT_WORLD_SOURCE", "RESIDENT_WORLD_SEED",
            "RESIDENT_WORLD_SEEDS",
            "RESIDENT_ALLOW_PROXY_DNS", "RESIDENT_QUESTIONS", "RESIDENT_CONTINUITY",
            "RESIDENT_SEMANTIC_SHADOW", "RESIDENT_EMBEDDINGS", "RESIDENT_BEAT_INTERVAL",
            "RESIDENT_MODEL_NAME", "RESIDENT_MODEL_BASE_URL", "RESIDENT_MODEL_TIMEOUT",
            "RESIDENT_PROFILE")
    return {k: environ.get(k) for k in keys if environ.get(k) is not None}


def write_manifest(store_path: str, seeds_path: str, force: bool = False) -> dict:
    """--init: write the baseline manifest ONCE (infrastructure writes a file
    beside the store, never the store itself)."""
    manifest_path = Path(store_path).parent / MANIFEST_NAME
    if manifest_path.exists() and not force:
        raise SystemExit(f"refusing to overwrite {manifest_path} — a baseline is already "
                         "registered here (use --force only if you intend to void it)")
    env = _baseline_env(os.environ)
    seeds_hash = _seed_set_hash(seeds_path)
    manifest = {
        "baseline_start_at": _utcnow(),
        "resident_commit": _git_commit(),
        "world_source": env.get("RESIDENT_WORLD_SOURCE", "fixture"),
        "seed_set_hash": seeds_hash,
        "config_fingerprint": _config_fingerprint(env, seeds_hash),
        "head_seq_at_start": _head_seq(store_path),
        "store": str(store_path),
        "env": env,
        **_semantic_fields(env),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    print(f"[manifest] {manifest_path}")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


def _head_seq(store_path: str) -> int | None:
    if not Path(store_path).exists():
        return 0
    store = EventStore(store_path)
    latest = store.latest()
    return latest.seq if latest else 0


def load_manifest(store_path: str) -> dict | None:
    p = Path(store_path).parent / MANIFEST_NAME
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _ts(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _span_days(store: EventStore) -> float | None:
    first = store.list(1, order="asc")
    last = store.latest()
    t0, t1 = _ts(first[0].created_at) if first else None, _ts(last.created_at) if last else None
    if not t0 or not t1:
        return None
    return round((t1 - t0).total_seconds() / 86400.0, 2)


def section_vitals(store: EventStore) -> dict:
    n = store.count()
    routes = Counter(e.content.get("route") for e in store.list(5000, type_prefix="wake.route_selected"))
    # wake.completed is written for EVERY finished wake; a genuine noop is
    # result=="noop" (rest route / unbound world / nothing to draw on)
    completed = store.list(5000, type_prefix="wake.completed")
    noops = sum(1 for e in completed if e.content.get("result") == "noop")
    n_wakes = store.count("wake.started")
    origins = Counter((e.metadata or {}).get("origin") for e in store.list(5000, type_prefix="thought.created"))
    return {
        "span_days": _span_days(store),
        "n_events": n,
        "n_wakes": n_wakes,
        "n_noops": noops,
        "noop_share": round(noops / n_wakes, 3) if n_wakes else None,
        "route_distribution": dict(routes.most_common()),
        "thought_origins": dict(origins.most_common()),
        "n_world_observations": store.count("world.observation"),
        "n_impressions": store.count("impression.formed"),
        "n_promotions": store.count("impression.promoted"),
    }


def section_questions(store: EventStore) -> dict:
    state = question_state(store)
    rows = state["questions"]
    alive = [q for q in rows if q["status"] == "open" and q["revisits"] >= 1]
    rev_counts = Counter(q["revisits"] for q in rows)
    return {
        "total": state["n_total"],
        "open": state["n_open"],
        "dormant": state["n_dormant"],
        "revisit_distribution": dict(sorted(rev_counts.items())),
        "revisits_total": sum(q["revisits"] for q in rows),
        "alive_survivors": [
            {"kind": q["kind"], "topic": q["topic"], "age_days": None,
             "revisits": q["revisits"], "text": (q["text"] or "")[:80],
             "last_touched": q["last_touched_at"]}
            for q in alive
        ],
        "dormant_without_remeet": [
            {"kind": q["kind"], "topic": q["topic"], "revisits": q["revisits"]}
            for q in rows if q["status"] == "dormant" and q["revisits"] == 0
        ][:10],
    }


def section_threads(store: EventStore, now_iso: str | None) -> dict:
    tv = monitor_threads(store, now_iso=now_iso)
    rows = []
    for t in tv["threads"]:
        ivals = []
        times = [_ts(r["at"]) for r in t["revisits"]]
        times = [x for x in times if x]
        for a, b in zip(times, times[1:]):
            ivals.append(round((b - a).total_seconds() / 86400.0, 2))
        rows.append({
            "thread_id": t["thread_id"], "title": t["title"], "origin": t["origin"],
            "state": t["state"], "revisits": len(t["revisits"]),
            "revisit_interval_days": ivals,
            "age_days": t["age_days"], "n_events": t["n_events"],
        })
    rows.sort(key=lambda r: (-r["revisits"], -(r["age_days"] or 0)))
    return {"threads": rows[:12],
            "summary": tv["summary"],
            "revisit_total": sum(r["revisits"] for r in rows)}


def section_world_traces(store: EventStore) -> dict:
    """Which world material left a LONG trace: cited again by a later thought,
    especially days later or more than once."""
    obs = store.list(3000, type_prefix="world.observation", order="asc")
    cited: dict[str, list[datetime]] = {}
    for th in store.list(5000, type_prefix="thought.created", order="asc"):
        refs = list((th.metadata or {}).get("recalled") or [])
        rel = th.links.get("related_to") or []
        refs += rel if isinstance(rel, list) else [rel]
        t_th = _ts(th.created_at)
        for rid in refs:
            if isinstance(rid, str) and any(rid == o.id for o in obs):
                if t_th:
                    cited.setdefault(rid, []).append(t_th)
    rows = []
    for o in obs:
        t_obs = _ts(o.created_at)
        cites = sorted(cited.get(o.id, []))
        if not cites:
            continue
        lag_days = round((cites[-1] - t_obs).total_seconds() / 86400.0, 2) if t_obs else None
        rows.append({
            "title": (o.content.get("title") or "")[:50],
            "topic": o.content.get("topic"),
            "cited_n": len(cites),
            "last_cite_lag_days": lag_days,
            "long_trace": len(cites) >= 2 or (lag_days is not None and lag_days >= 1.0),
        })
    rows.sort(key=lambda r: (-r["cited_n"], -(r["last_cite_lag_days"] or 0)))
    return {
        "n_observations": len(obs),
        "n_cited_ever": len(rows),
        "n_long_trace": sum(1 for r in rows if r["long_trace"]),
        "cited": rows[:12],
    }


def section_continuity(store: EventStore) -> dict:
    cv = monitor_continuity(store)
    cls_counter = Counter(c["cls"] for w in cv["windows"] for c in w["candidates"])
    gaps = [w["gap_s"] for w in cv["windows"] if w["gap_s"]]
    return {
        "absences": cv["summary"]["absences"],
        "selected": cv["summary"]["selected"],
        "noop": cv["summary"]["noop"],
        "candidate_class_distribution": dict(cls_counter.most_common()),
        "gap_hours_mean": round(statistics.mean(gaps) / 3600.0, 2) if gaps else None,
    }


def section_shadow(path: str | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    overlaps, top1 = [], 0
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            if isinstance(r.get("overlap"), (int, float)):
                overlaps.append(float(r["overlap"]))
            if r.get("top1_differs"):
                top1 += 1
    if not n:
        return None
    return {"records": n, "overlap_mean": round(statistics.mean(overlaps), 3) if overlaps else None,
            "top1_divergence": round(top1 / n, 3)}


def main() -> None:
    ap = argparse.ArgumentParser(description="Living Baseline read-only analysis")
    ap.add_argument("store", nargs="?", default=str(REPO / "resident_home" / "events.sqlite3"))
    ap.add_argument("--shadow", default=None, help="semantic shadow sidecar JSONL (optional)")
    ap.add_argument("--json", dest="json_out", default=None, help="also write the report as JSON")
    ap.add_argument("--init", action="store_true",
                    help="write the baseline manifest (once) instead of analysing")
    ap.add_argument("--seeds", default=str(REPO / "backend" / "seeds" / "world_seeds.json"))
    ap.add_argument("--force", action="store_true",
                    help="with --init: overwrite an existing manifest (voids the baseline)")
    args = ap.parse_args()

    if args.init:
        write_manifest(args.store, args.seeds, force=args.force)
        return

    if args.shadow is None:
        guess = Path(args.store).parent / "semantic_shadow.jsonl"
        if guess.exists():
            args.shadow = str(guess)

    store = EventStore(args.store)
    before_count, before_seq = store.count(), (store.latest().seq if store.latest() else None)
    latest = store.latest()
    now_iso = latest.created_at if latest else None
    manifest = load_manifest(args.store)

    # provenance of the ANALYSIS itself (the owner-mandated metadata block):
    # when the manifest exists it is authoritative; otherwise the start is
    # derived from the store and flagged as such
    if manifest:
        provenance = {
            "baseline_start_at": manifest.get("baseline_start_at"),
            "baseline_start_source": "manifest",
            "resident_commit": manifest.get("resident_commit"),
            "config_fingerprint": manifest.get("config_fingerprint"),
            "world_source": manifest.get("world_source"),
            "semantic_main": manifest.get("semantic_main"),
            "semantic_shadow": manifest.get("semantic_shadow"),
            "seed_set_hash": manifest.get("seed_set_hash"),
            "head_seq_at_start": manifest.get("head_seq_at_start"),
        }
    else:
        first = store.list(1, order="asc")
        provenance = {
            "baseline_start_at": first[0].created_at if first else None,
            "baseline_start_source": "derived-from-store (no manifest)",
            "resident_commit": _git_commit(),
            "config_fingerprint": None,
            "world_source": None, "semantic_main": None, "semantic_shadow": None,
            "seed_set_hash": None, "head_seq_at_start": None,
        }
    provenance.update({
        "analysis_at": _utcnow(),
        "analysis_commit": _git_commit(),
        "head_seq": latest.seq if latest else 0,
        "events_now": store.count(),
    })

    report = {
        "store": str(args.store),
        "analysis_provenance": provenance,
        "vitals": section_vitals(store),
        "questions": section_questions(store),
        "threads": section_threads(store, now_iso),
        "world_traces": section_world_traces(store),
        "continuity": section_continuity(store),
        "shadow": section_shadow(args.shadow),
    }
    # the glass-window rule holds for the analysis kit too
    assert store.count() == before_count and (
        store.latest().seq if store.latest() else None) == before_seq, "store mutated!"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        print(f"\n[written to {args.json_out}]", file=sys.stderr)


if __name__ == "__main__":
    main()
