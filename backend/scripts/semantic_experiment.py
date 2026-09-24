#!/usr/bin/env python3
"""v0.2 Step 2 (ADR-0010): the Shadow → Treatment → Rollback experiment.

Three deterministic phases over the existing experiment families:

- SHADOW: the sealed legacy mind runs while a real-vector layer answers the
  same retrieval questions in parallel (audit sidecar only). This yields the
  divergence profile and the case studies: the same wake, what legacy recalled
  vs what semantic would have recalled, and the explainable bridge.
- TREATMENT: the semantic layer takes over retrieval (bridge distant, mid-band
  serendipity, cluster concentration), same seeds + user stream + mechanisms.
  Compared against fresh legacy runs on: route entropy, no-op, memory age,
  revisit/resurrection, self-origin, user-derived, world-origin, topic
  entropy, cross-source association, thought count.
- ROLLBACK: the legacy path re-run must be byte-identical to the sealed
  reports (the 12-cell world regression is the formal proof; here we also
  re-run one legacy cell and compare its headline metrics).

NOT a success metric: "numbers improved". The questions are which associations
changed, which chains died/revived, whether Chinese content forms real topic
structure, whether distant/serendipity got richer or collapsed, and whether
Day-14 Resident shows an explainably different trajectory.
"""
import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore  # noqa: E402
from resident.semantic import ShadowLog, semantic_profile  # noqa: E402
from resident.world_sim import ARM_PARAMS, run_world_simulation  # noqa: E402

OUT = Path("/tmp/semantic_experiment")
SEEDS = (0, 1, 2, 3)
DAYS = 14
ARM = "C_all"


def cell(task):
    """One (family, mode, seed) run; returns its headline metrics."""
    family, mode, seed = task
    d = OUT / family / mode / f"seed{seed}"
    if family == "world":
        rep = asyncio.run(run_world_simulation(
            d, days=DAYS, seed=seed, world_params=ARM_PARAMS[ARM],
            embeddings=("semantic" if mode == "treatment" else "legacy"),
            shadow=(mode == "shadow")))
    elif family == "circadian":
        from resident.circadian_sim import run_circadian_simulation

        rep = asyncio.run(run_circadian_simulation(
            d, days=DAYS, seed=seed, self_revival=True,
            embeddings=("semantic" if mode == "treatment" else "legacy"),
            shadow=(mode == "shadow")))
    else:  # "life7" — the 7-day psychological-time family
        from resident.simulation import run_simulation

        rep = asyncio.run(run_simulation(
            d, days=7, seed=seed, self_prob=0.35, self_revival=True,
            embeddings=("semantic" if mode == "treatment" else "legacy"),
            shadow=(mode == "shadow")))
    row = {
        "family": family, "mode": mode, "seed": seed,
        "route_entropy": rep.get("route_entropy"),
        "no_op_ratio": rep.get("no_op_ratio"),
        "n_thoughts": rep.get("meta", {}).get("n_thoughts"),
        "self_origin_share": rep.get("self_origin_share") or
                             (rep.get("independence") or {}).get("self_origin_share"),
        "user_derived_share": rep.get("user_derived_share") or
                              (rep.get("independence") or {}).get("user_derived_share"),
        "world_origin_share": ((rep.get("world") or {}).get("origin_shares") or {}).get("world_origin"),
        "memory_age": (rep.get("memory_age_at_activation_days") or {}) if family == "life7" else None,
        "resurrection": (rep.get("independence") or {}).get("independence_share"),
        "report": rep,
    }
    (d / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return row


def aggregate(rows):
    """rows -> {family: {mode: mean-of-metrics}}"""
    out = {}
    for r in rows:
        fam = out.setdefault(r["family"], {}).setdefault(r["mode"], [])
        fam.append(r)
    summary = {}
    for fam, modes in out.items():
        summary[fam] = {}
        for mode, rs in modes.items():
            def m(key):
                vals = [r[key] for r in rs if isinstance(r.get(key), (int, float))]
                return round(sum(vals) / len(vals), 4) if vals else None
            summary[fam][mode] = {
                "n_seeds": len(rs),
                "route_entropy": m("route_entropy"),
                "no_op_ratio": m("no_op_ratio"),
                "n_thoughts": m("n_thoughts"),
                "self_origin_share": m("self_origin_share"),
                "user_derived_share": m("user_derived_share"),
                "world_origin_share": m("world_origin_share"),
            }
    return summary


def case_studies(shadow_dir: Path, store_path: Path, k: int = 6):
    """Pick the sharpest divergence cases: top-1 differs, biggest age gap or
    cross-cluster jump — with the actual texts, so the report can say WHY."""
    records = [r for r in ShadowLog(shadow_dir / "semantic_shadow.jsonl").read()
               if r.get("kind") == "shadow"]
    store = EventStore(store_path)

    def text_of(eid):
        e = store.get(eid)
        return (e.text or "(no text)")[:80] if e else "(missing)"

    scored = []
    for r in records:
        if not r.get("top1_differs") or not r.get("legacy_top") or not r.get("semantic_top"):
            continue
        la, sa = r.get("legacy_top1_age_days"), r.get("semantic_top1_age_days")
        gap = abs((la or 0) - (sa or 0))
        cross = (r.get("legacy_top1_cluster") != r.get("semantic_top1_cluster"))
        scored.append((gap + (5.0 if cross else 0.0), cross, r))
    scored.sort(key=lambda t: -t[0])
    cases = []
    for _, cross, r in scored[:k]:
        lid, sid = r["legacy_top"][0], r["semantic_top"][0]
        cases.append({
            "wake_id": r.get("wake_id"), "route": r.get("route"),
            "legacy_recalled": text_of(lid), "legacy_age_days": r.get("legacy_top1_age_days"),
            "semantic_recalled": text_of(sid), "semantic_age_days": r.get("semantic_top1_age_days"),
            "cross_cluster": cross,
            "legacy_cluster": r.get("legacy_top1_cluster"),
            "semantic_cluster": r.get("semantic_top1_cluster"),
            "rank_displacement_mean": r.get("rank_displacement_mean"),
        })
    return cases


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = []
    for seed in SEEDS:
        tasks.append(("world", "legacy", seed))
        tasks.append(("world", "treatment", seed))
        if seed == 0:  # shadow only on seed 0 — one divergence profile suffices
            tasks.append(("world", "shadow", seed))
        tasks.append(("circadian", "legacy", seed))
        tasks.append(("circadian", "treatment", seed))
        tasks.append(("life7", "legacy", seed))
        tasks.append(("life7", "treatment", seed))
    print(f"{len(tasks)} cells, {6} workers…", flush=True)
    rows = []
    done = 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for row in ex.map(cell, tasks):
            done += 1
            rows.append(row)
            print(f"[{done}/{len(tasks)}] {row['family']}/{row['mode']}/seed{row['seed']}",
                  flush=True)

    summary = aggregate(rows)
    # the shadow profile + case studies (seed 0, world family)
    shadow_store = OUT / "world" / "shadow" / "seed0" / "world.sqlite3"
    profile = semantic_profile(
        EventStore(shadow_store),
        shadow_log=ShadowLog(OUT / "world" / "shadow" / "seed0" / "semantic_shadow.jsonl"))
    cases = case_studies(OUT / "world" / "shadow" / "seed0", shadow_store)

    result = {
        "summary": summary,
        "shadow_profile": profile,
        "cases": cases,
        "rows": [{k: v for k, v in r.items() if k != "report"} for r in rows],
    }
    (OUT / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nshadow profile: {json.dumps(profile, ensure_ascii=False)[:400]}")
    print(f"\ncases: {len(cases)} (full text in results.json)")
    print(f"\n[written to {OUT / 'results.json'}]")


if __name__ == "__main__":
    main()
