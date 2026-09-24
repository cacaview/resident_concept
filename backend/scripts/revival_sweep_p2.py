"""Phase-4.1, PHASE 2: sweep the route boost (route_lift) — ONLY if phase 1 found a
plausible working region. Target a small set of (dormant, silence, max_active)
configs chosen from the phase-1 map, and sweep route_lift over
{0.05, 0.10, 0.15, 0.20}. Everything else (seed, stream, thread_strength,
low_stimulus) is held fixed. Same metric layers + revival_leverage as phase 1.
"""
from __future__ import annotations

import sys
from concurrent.futures import ProcessPoolExecutor

from revival_sweep import (SEEDS, WORK, aggregate, classify_zone, run_one, _rate, _mean)

ROUTE_LIFTS = (0.05, 0.10, 0.15, 0.20)
# configs chosen AFTER reading the phase-1 map (dormant_h, silence_h, max_active):
# one per regime so we can read how route-boost size acts in each:
TARGETS: list[tuple] = [
    (6, 6, 5),    # high-freq effective (most opportunity)
    (12, 6, 5),   # high-freq, cascade-prone
    (24, 6, 3),   # stable-sporadic, low intensity
    (12, 24, 1),  # sparse single-cascade corner
    (48, 6, 5),   # near-no-trigger
]


def build_tasks() -> list:
    tasks = [{"kind": "B", "seed": s} for s in SEEDS]
    for rl in ROUTE_LIFTS:
        for (dh, sh, ma) in TARGETS:
            for s in SEEDS:
                tasks.append({"kind": "C", "seed": s, "dormant_h": dh,
                              "silence_h": sh, "max_active": ma, "route_lift": rl})
    return tasks


def run_one_p2(task: dict) -> dict:
    """run_one but with a per-task route_lift override + tagged dir."""
    rl = task.get("route_lift", 0.15)
    import asyncio
    import resident.simulation as sim
    from resident.self_revival import RevivalParams
    import revival_sweep as S
    try:
        if task["kind"] == "B":
            d = WORK / f"P2_B_s{task['seed']}"
            rep = asyncio.run(sim.run_simulation(d, days=14, seed=task["seed"],
                user_script=S.SCRIPT14, self_prob=0.35, self_revival=False, revival=None))
            return {"kind": "B", "seed": task["seed"], "tag": f"P2_B_s{task['seed']}",
                    "self_thoughts": S._self_thoughts(rep)}
        tag = f"P2_s{task['seed']}_rl{rl}_d{task['dormant_h']}h_s{task['silence_h']}h_a{task['max_active']}"
        d = WORK / tag
        rev = RevivalParams(silence_s=task["silence_h"] * 3600, low_stimulus_s=6 * 3600,
                            max_active=task["max_active"], min_dormant_s=task["dormant_h"] * 3600,
                            min_self_events=1, route_lift=rl, thread_strength=0.35)
        rep = asyncio.run(sim.run_simulation(d, days=14, seed=task["seed"],
            user_script=S.SCRIPT14, self_prob=0.35, self_revival=True, revival=rev))
        from resident.event_store import EventStore
        store = EventStore(d / "sim.sqlite3")
        iv = S.intervention_metrics(store, rl)
        sd = rep["self_dynamics"]; nw = rep["meta"]["n_wakes"]
        return {
            "kind": "C", "seed": task["seed"], "tag": tag,
            "dormant_h": task["dormant_h"], "silence_h": task["silence_h"],
            "max_active": task["max_active"], "route_lift": rl,
            "eligible_wakes": iv["eligible_wakes"], "eligible_rate": round(iv["eligible_wakes"] / nw, 4),
            "candidate_count": iv["candidate_count"], "revisit_in_lull": iv["revisit_in_lull"],
            "route_changed_by_bias": iv["route_changed_by_bias"],
            "self_candidate_selected": iv["self_candidate_selected"], "revival_engaged": iv["revival_engaged"],
            "self_thoughts": S._self_thoughts(rep),
            "self_origin_share": rep["independence"]["self_origin_share"],
            "user_derived_share": rep["independence"]["user_derived_share"],
            "generation_depth": sd["self_thread_generation_depth"],
            "longest_self_chain": sd["longest_self_chain"], "route_entropy": rep["route_entropy"],
            "conc_top1": sd["self_loop_concentration"]["top1_share"],
            "conc_total": sd["self_loop_concentration"]["total"],
            "div_n": sd["self_topic_diversity"]["n_distinct"],
            "div_entropy": sd["self_topic_diversity"]["entropy"],
            "avg_lifetime_days": sd["self_thread_avg_lifetime_days"],
            "n_wakes": nw, "n_self_threads": rep["meta"]["n_self_threads"],
        }
    except Exception as exc:
        return {"kind": task["kind"], "seed": task["seed"], "tag": task["seed"], "error": repr(exc)}


def aggregate_p2(rows, targets):
    import json
    b_by_seed = {r["seed"]: r["self_thoughts"] for r in rows if r["kind"] == "B"}
    by_cfg = {}
    for r in rows:
        if r["kind"] != "C" or "error" in r:
            continue
        key = (r["route_lift"], r["dormant_h"], r["silence_h"], r["max_active"])
        by_cfg.setdefault(key, []).append(r)
    out = []
    for (rl, dh, sh, ma), crows in by_cfg.items():
        per_seed = []
        for s in SEEDS:
            cr = next((r for r in crows if r["seed"] == s), None)
            if cr is None:
                continue
            sb = b_by_seed.get(s); e = cr["revival_engaged"]
            delta = (cr["self_thoughts"] - sb) if sb is not None else None
            per_seed.append({"seed": s, "eligible_wakes": cr["eligible_wakes"],
                             "candidate_count": cr["candidate_count"],
                             "route_changed_by_bias": cr["route_changed_by_bias"],
                             "self_candidate_selected": cr["self_candidate_selected"],
                             "revival_engaged": e, "self_thoughts": cr["self_thoughts"],
                             "self_delta": delta,
                             "leverage": (round(delta / e, 3) if (e > 0 and delta is not None) else None),
                             "self_origin_share": cr["self_origin_share"],
                             "route_entropy": cr["route_entropy"], "conc_top1": cr["conc_top1"]})
        agg = {"route_lift": rl, "dormant_h": dh, "silence_h": sh, "max_active": ma,
               "eligible_wakes": _mean([p["eligible_wakes"] for p in per_seed]),
               "route_changed_by_bias": _mean([p["route_changed_by_bias"] for p in per_seed]),
               "self_candidate_selected": _mean([p["self_candidate_selected"] for p in per_seed]),
               "revival_engaged": _mean([p["revival_engaged"] for p in per_seed]),
               "self_delta": _mean([p["self_delta"] for p in per_seed]),
               "leverage": _mean([p["leverage"] for p in per_seed]),
               "self_origin_share": _mean([p["self_origin_share"] for p in per_seed]),
               "route_entropy": _mean([p["route_entropy"] for p in per_seed]),
               "conc_top1": _mean([p["conc_top1"] for p in per_seed]), "per_seed": per_seed}
        out.append(agg)
    out.sort(key=lambda a: (a["dormant_h"], a["silence_h"], a["max_active"], a["route_lift"]))
    return {"route_lifts": list(ROUTE_LIFTS), "targets": [list(t) for t in targets],
            "self_baseline_by_seed": b_by_seed, "configurations": out}


def main():
    if not TARGETS:
        print("TARGETS empty — fill in the phase-1 interesting configs first.")
        return 2
    WORK.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks()
    print(f"[sweep-p2] {len(tasks)} runs, route_lifts={list(ROUTE_LIFTS)}, "
          f"targets={len(TARGETS)}", flush=True)
    with ProcessPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(run_one_p2, tasks))
    errs = [r for r in rows if "error" in r]
    print(f"[sweep-p2] done; {len(rows) - len(errs)} ok, {len(errs)} errors", flush=True)
    for r in errs[:10]:
        print("  ERR", r.get("tag"), r.get("error"), flush=True)
    import json
    agg = aggregate_p2(rows, TARGETS)
    (WORK / "results_p2.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    hdr = f"{'rl':>5} {'d':>3} {'s':>3} {'a':>2} | {'elig':>4} {'chg':>4} {'sel':>4} {'eng':>4} | {'dS':>4} {'lev':>5} {'self%':>6} {'ent':>5} {'top1':>5}"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for a in agg["configurations"]:
        def f(x, n=1):
            return ("-" if x is None else (f"{x:.{n}f}" if isinstance(x, float) else f"{x}"))
        print(f"{a['route_lift']:>5} {a['dormant_h']:>3} {a['silence_h']:>3} {a['max_active']:>2} | "
              f"{f(a['eligible_wakes'],0):>4} {f(a['route_changed_by_bias'],0):>4} "
              f"{f(a['self_candidate_selected'],0):>4} {f(a['revival_engaged'],0):>4} | "
              f"{f(a['self_delta']):>4} {f(a['leverage']):>5} {f(a['self_origin_share'],3):>6} "
              f"{f(a['route_entropy'],3):>5} {f(a['conc_top1'],2):>5}")
    print(f"\n[written to {WORK / 'results_p2.json'}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
