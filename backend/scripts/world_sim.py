"""Phase 6 — World-Window A/B/C cross-seed deterministic sweep (ADR-0008).

Runs the three World-Window arms across a range of fixed seeds and reports, per
seed and in aggregate, the watch-metrics that answer the phase's real question —
*does the World become a genuine third kind of experience, or does it turn
Resident into a news summariser / recommender?*

Arms (same user stream + seed + circadian + self-thread mechanism in every arm;
**the only variable is the World Window**):

    A_no_world    world_engine = None        (the clean control)
    B_follow_edge follow + edge              (on-topic + cognitive boundary)
    C_all         follow + edge + alien + accident (all four entry modes)

Watch-metrics (per seed + mean): windows opened / exposure rate / world→thought /
seen-left-nothing / world+old-memory thoughts / cross-source association /
alien survival / accident chain depth / source & topic concentration (adhesion) /
the three-way origin split (user-derived / self / world).

**No parameter is tuned** toward a target ratio. A low world→thought / high
seen-left-nothing is a *good* sign (reading ≠ thought); high source/topic
concentration is a *finding* (adhesion) and is reported, not tuned away.

Run:
    .venv/bin/python scripts/world_sim.py <outdir> --seeds 0-3 --days 14

Writes ``<outdir>/results.json`` + ``<outdir>/REPORT.md``. Deterministic per
seed×arm; ``--resume`` skips a cell whose ``report.json`` already exists.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from resident.world_sim import ARM_PARAMS, _ARM_ORDER, comparison_markdown, run_world_simulation


def _parse_seeds(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = map(int, spec.split("-"))
        return list(range(lo, hi + 1))
    return [int(s) for s in spec.split(",") if s.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("outdir", nargs="?", default="out/world_sim")
    ap.add_argument("--seeds", default="0-3", help="e.g. '0-5' or '0,3,7'")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--resume", action="store_true", help="skip a cell whose report.json exists")
    args = ap.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    seeds = _parse_seeds(args.seeds)
    arms = _ARM_ORDER

    # results[arm][seed] = full arm report (the shape comparison_markdown expects)
    results: dict[str, dict[int, dict]] = {a: {} for a in arms}
    total = len(seeds) * len(arms)
    done = 0
    for seed in seeds:
        for arm in arms:
            params = ARM_PARAMS[arm]
            cdir = out / arm / f"seed{seed}"
            marker = cdir / "report.json"
            if args.resume and marker.exists():
                results[arm][seed] = json.loads(marker.read_text(encoding="utf-8"))
                done += 1
                print(f"[{done}/{total}] {arm} seed {seed}: resumed", flush=True)
                continue
            rep = asyncio.run(run_world_simulation(cdir, days=args.days, seed=seed, world_params=params))
            rep["meta"]["_run_at"] = datetime.now(timezone.utc).isoformat()
            marker.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
            results[arm][seed] = rep
            done += 1
            w = rep["world"]
            print(f"[{done}/{total}] {arm} seed {seed}: opened={w['n_windows_opened']} "
                  f"seen-left-nothing={w['seen_left_nothing_rate']:.2f} "
                  f"world-thoughts={w['n_world_thoughts']} "
                  f"src-conc={w['source_concentration']:.2f} "
                  f"topic-conc={w['topic_concentration']:.2f}", flush=True)

    # assemble the combined structure comparison_markdown / results.json expect
    comparison = _comparison_table(results, arms, seeds)
    combined = {
        "meta": {
            "days": args.days,
            "seeds": seeds,
            "user_turns": 7,  # DEFAULT_USER_SCRIPT length (see simulation.py)
            "arms": list(arms),
            "arm_world_modes": {a: list(ARM_PARAMS[a].enabled_modes) if ARM_PARAMS[a] else [] for a in arms},
        },
        "arms": results,
        "comparison": comparison,
    }
    (out / "results.json").write_text(json.dumps(combined["comparison"], ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    md = comparison_markdown(combined)
    (out / "REPORT.md").write_text(md, encoding="utf-8")
    print("\n=== COMPARISON (means over seeds) ===")
    for key, per_arm in comparison["watch_metrics"].items():
        row = "  ".join(f"{a}={per_arm[a]['mean']:.3f}" for a in arms)
        print(f"  {key}: {row}")
    print(f"\n[written to {out / 'results.json'} and {out / 'REPORT.md'}]")


def _comparison_table(results, arms, seeds) -> dict:
    """Mirror of resident.world_sim._compare_world (kept here so the resumable
    script builds the same table the one-shot run_world_comparison produces)."""
    def _g(arm, seed, *path):
        cur = results[arm][seed]
        for p in path:
            cur = cur[p]
        return cur

    keys = [
        "n_windows_opened", "world_exposure_rate", "world_to_thought_rate",
        "seen_left_nothing_rate", "n_world_thoughts", "n_world_plus_memory_thoughts",
        "cross_source_association_rate", "alien_survival_rate", "accident_max_chain_depth",
        "source_concentration", "topic_concentration",
    ]
    origin_keys = ["origin_shares__world_origin", "origin_shares__user_derived", "origin_shares__self_origin"]
    table: dict[str, dict] = {}
    for key in keys:
        table[key] = {
            arm: {
                "per_seed": {s: float(_g(arm, s, "world", key)) for s in seeds},
                "mean": round(sum(float(_g(arm, s, "world", key)) for s in seeds) / len(seeds), 3) if seeds else 0.0,
            } for arm in arms
        }
    for ok in origin_keys:
        sub = ok.split("__")[1]
        table[ok] = {
            arm: {
                "per_seed": {s: float(_g(arm, s, "world", "origin_shares", sub)) for s in seeds},
                "mean": round(sum(float(_g(arm, s, "world", "origin_shares", sub)) for s in seeds) / len(seeds), 3) if seeds else 0.0,
            } for arm in arms
        }
    mode_table: dict[str, dict[str, float]] = {}
    for arm in arms:
        agg: dict[str, float] = {}
        for s in seeds:
            for m, c in (_g(arm, s, "world", "entry_mode_distribution") or {}).items():
                agg[m] = agg.get(m, 0) + float(c)
        mode_table[arm] = agg
    return {"watch_metrics": table, "entry_mode_distribution": mode_table}


if __name__ == "__main__":
    sys.exit(main())
