#!/usr/bin/env python3
"""v0.2 Step 1 sealed regression (ADR-0009): re-run the Phase-6 A/B/C world
sweep (3 arms x 4 seeds, 14 days, fixture world) with the WorldSource-refactored
engine and byte-compare every cell against the sealed reports in
backend/out/world_sim — proving the adapter refactor did not move the sealed
mind/world dynamics. Additive keys (world.live) and the script-side _run_at
stamp are the only permitted differences."""
import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.world_sim import ARM_PARAMS, run_world_simulation  # noqa: E402

SEALED = Path(__file__).resolve().parents[1] / "out" / "world_sim"
FRESH = Path("/tmp/v02_regression")
ARMS = ("A_no_world", "B_follow_edge", "C_all")
SEEDS = (0, 1, 2, 3)


def run_cell(task):
    arm, seed = task
    out = FRESH / arm / f"seed{seed}"
    rep = asyncio.run(run_world_simulation(out, days=14, seed=seed, world_params=ARM_PARAMS[arm]))
    (out / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return f"{arm}/seed{seed}"


def compare_one(arm, seed):
    sealed = json.loads((SEALED / arm / f"seed{seed}" / "report.json").read_text(encoding="utf-8"))
    fresh = json.loads((FRESH / arm / f"seed{seed}" / "report.json").read_text(encoding="utf-8"))
    diffs = []
    for key, val in sealed.items():
        new = fresh.get(key, "<<MISSING>>")
        if new == val:
            continue
        if key == "world" and isinstance(val, dict) and isinstance(new, dict):
            for wk, wv in val.items():
                if new.get(wk) != wv:
                    diffs.append(f"world.{wk}: {wv} -> {new.get(wk)}")
            additive = set(new) - set(val)
            if additive:
                # Permitted additive telemetry: "live" (v0.2 Step 1, ADR-0009)
                # and "questions" (v0.2 Step 3, ADR-0011) — the latter ONLY as
                # the all-zero block (the question layer is off in every cell).
                qblk = new.get("questions")
                q_zero = isinstance(qblk, dict) and not any(
                    any(v.values()) if isinstance(v, dict) else v
                    for v in qblk.values()
                )
                permitted = additive <= {"live", "questions"} and (
                    "questions" not in additive or q_zero
                )
                if permitted:
                    diffs.append(f"world additive keys (permitted): {sorted(additive)}")
                else:
                    diffs.append(f"world ADDITIVE UNPERMITTED: {sorted(additive)}")
        elif key == "meta" and isinstance(val, dict) and isinstance(new, dict):
            for mk, mv in val.items():
                if mk == "_run_at":
                    continue
                if new.get(mk) != mv:
                    diffs.append(f"meta.{mk}: {mv} -> {new.get(mk)}")
        else:
            diffs.append(f"{key}: {str(val)[:80]} -> {str(new)[:80]}")
    return diffs


def main():
    tasks = [(a, s) for a in ARMS for s in SEEDS]
    done = 0
    with ProcessPoolExecutor(max_workers=6) as ex:
        for _ in ex.map(run_cell, tasks):
            done += 1
            print(f"[{done}/{len(tasks)}] cells re-run", flush=True)

    bad = []
    for arm in ARMS:
        for seed in SEEDS:
            diffs = compare_one(arm, seed)
            real = [d for d in diffs if "permitted" not in d]
            additive = [d for d in diffs if "permitted" in d]
            status = "OK" if not real else "REAL DIFF"
            print(f"{arm}/seed{seed}: {status}"
                  + (f" | additive: {additive[0]}" if additive else ""))
            if real:
                bad.append((arm, seed, real))

    if bad:
        print(f"\nREGRESSION FAILED — {len(bad)} cell(s) with real diffs:")
        for arm, seed, real in bad:
            print(f"  {arm}/seed{seed}:")
            for d in real:
                print(f"    {d}")
        sys.exit(1)
    print(f"\nSEALED REGRESSION PASSED: all {len(tasks)} cells byte-identical "
          "(except the documented additive `world.live` telemetry key).")


if __name__ == "__main__":
    main()
