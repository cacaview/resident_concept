"""Phase 5.1 — circadian-vs-fixed cross-seed robustness sweep (ADR-0007).

Phase 5's 14-day deliverable (``SIMULATION_REPORT-circadian.md``) is a **single-seed
(0) characterisation**. The one remaining gap before sealing Phase 5 is whether the
effect is **stable across seeds** or a seed-0 fluke. This sweep runs the same
circadian-vs-fixed comparison (``resident.circadian_sim.run_comparison`` — same user
stream + self-mechanism, only the scheduler differs) across a range of seeds and
reports, per seed and in aggregate:

    eligible frequency      the circadian-recognised quiet opened the revival gate
    engaged frequency       a dormant self-thread actually resurfaced
    revival delta           within-window self-origin thoughts in revived self-threads
    negative delta rate     seeds where circadian *lowers* self-origin vs fixed (diversion)
    self-origin delta       circadian minus fixed self-origin share
    route entropy           does route diversity hold (not collapse)?
    no-op ratio             is "nothing happened" still a reachable outcome?
    activity clustering     intra-day wake-gap CV + night wake share (natural clusters,
                            a real quiet night — not uniform tiling)

**No parameter is tuned.** The defaults are the Phase-4.1 working region already baked
into ``RevivalParams`` / ``CircadianParams``.

The four stability checks the sweep answers (Phase 5 seals if all hold):
  1. eligible frequency rises across seeds (circadian > fixed)
  2. activity clustering appears across seeds (circadian gap-CV > fixed, and high)
  3. route entropy does not collapse (stays high across seeds)
  4. negative deltas do not clearly worsen (few/no seeds where circadian < fixed)

Run:
    .venv/bin/python scripts/circadian_cross_seed.py <outdir> --seeds 0-15 --days 14

Writes ``<outdir>/results.json`` + ``<outdir>/REPORT.md``. Deterministic per seed;
``--resume`` skips a seed whose results already exist.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from resident.circadian_sim import run_comparison


def extract(c: dict, seed: int) -> dict:
    """Pull the per-seed circadian-vs-fixed metrics out of a run_comparison result."""
    f, cc, cmp_ = c["fixed"], c["circadian"], c["comparison"]
    n_wf, n_wc = cmp_["n_wakes"]["fixed"], cmp_["n_wakes"]["circadian"]
    eng_f, eng_c = cmp_["revival_engaged"]["fixed"], cmp_["revival_engaged"]["circadian"]
    cl_f, cl_c = cmp_["clustering"]["fixed"], cmp_["clustering"]["circadian"]
    return {
        "seed": seed,
        # opportunity
        "eligible_freq_fixed": cmp_["revival_eligible_frequency"]["fixed"],
        "eligible_freq_circ": cmp_["revival_eligible_frequency"]["circadian"],
        "engaged_freq_fixed": round(eng_f / n_wf, 4) if n_wf else 0.0,
        "engaged_freq_circ": round(eng_c / n_wc, 4) if n_wc else 0.0,
        # result
        "self_origin_delta": cmp_["self_origin_delta"],          # circadian - fixed
        "self_origin_fixed": cmp_["self_origin_share"]["fixed"],
        "self_origin_circ": cmp_["self_origin_share"]["circadian"],
        "user_derived_circ": cmp_["user_derived_share"]["circadian"],
        "revival_delta_fixed": f["revival_delta"],
        "revival_delta_circ": cc["revival_delta"],
        "route_changed_by_bias_circ": cmp_["route_changed_by_bias"]["circadian"],
        # health
        "route_entropy_fixed": cmp_["route_entropy"]["fixed"],
        "route_entropy_circ": cmp_["route_entropy"]["circadian"],
        "no_op_ratio_fixed": cmp_["no_op_ratio"]["fixed"],
        "no_op_ratio_circ": cmp_["no_op_ratio"]["circadian"],
        "n_wakes_fixed": n_wf,
        "n_wakes_circ": n_wc,
        # clustering
        "intraday_cv_fixed": cl_f["intraday_gap_cv"],
        "intraday_cv_circ": cl_c["intraday_gap_cv"],
        "night_wake_share_fixed": cl_f["night_wake_share"],
        "night_wake_share_circ": cl_c["night_wake_share"],
    }


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 4) if xs else 0.0


def summarise(rows: list[dict]) -> dict:
    """Aggregate the per-seed rows: means + the four stability checks."""
    n = len(rows)
    elig_up = sum(1 for r in rows if r["eligible_freq_circ"] > r["eligible_freq_fixed"])
    clus_up = sum(1 for r in rows if r["intraday_cv_circ"] > r["intraday_cv_fixed"])
    clus_high = sum(1 for r in rows if r["intraday_cv_circ"] > 0.3)
    neg_delta = sum(1 for r in rows if r["self_origin_delta"] < 0)
    route_min = min((r["route_entropy_circ"] for r in rows), default=0.0)
    checks = {
        "eligible_rises": {"pass": elig_up >= n * 0.75, "count": elig_up, "of": n},
        "clustering_appears": {"pass": clus_up >= n * 0.75, "count": clus_up, "of": n},
        "clustering_high": {"pass": clus_high >= n * 0.75, "count": clus_high, "of": n},
        "route_entropy_holds": {"pass": route_min >= 0.7, "min": route_min},
        "no_negative_delta": {"pass": neg_delta <= max(1, n // 8), "count": neg_delta, "of": n},
    }
    return {
        "n_seeds": n,
        "means": {
            "eligible_freq_circ": _mean([r["eligible_freq_circ"] for r in rows]),
            "eligible_freq_fixed": _mean([r["eligible_freq_fixed"] for r in rows]),
            "engaged_freq_circ": _mean([r["engaged_freq_circ"] for r in rows]),
            "self_origin_delta": _mean([r["self_origin_delta"] for r in rows]),
            "route_entropy_circ": _mean([r["route_entropy_circ"] for r in rows]),
            "no_op_ratio_circ": _mean([r["no_op_ratio_circ"] for r in rows]),
            "intraday_cv_circ": _mean([r["intraday_cv_circ"] for r in rows]),
            "night_wake_share_circ": _mean([r["night_wake_share_circ"] for r in rows]),
            "revival_delta_circ": _mean([r["revival_delta_circ"] for r in rows]),
            "route_changed_by_bias_circ": _mean([r["route_changed_by_bias_circ"] for r in rows]),
        },
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks.values()),
    }


# -------------------------------------------------------------------- render


def report_markdown(rows: list[dict], summary: dict, *, days: int, seeds: list[int]) -> str:
    m, ck = summary["means"], summary["checks"]
    L = [
        "# Resident 5.1 — circadian-vs-fixed 跨 seed 稳健性（封板验证）",
        "",
        f"> 目的：Phase 5 的 14 天结论只验证了 seed 0。本轮扫 **seed {seeds[0]}–{seeds[-1]}**"
        f"（{len(seeds)} 个）确认 circadian 的效果**跨 seed 稳定**，而不是单 seed 偶然。"
        f"**没有调任何参数**（用 Phase-4.1 工作区间的默认值）。",
        "",
        f"状态：{len(rows)} 个 seed × 2 臂（fixed / circadian）确定性运行，0 错误。"
        "每个 seed 同 stream、同 self-mechanism，**唯一变量是调度器**。",
        "",
        "## 1. 每 seed 对比（circadian 列，Δ = circadian − fixed）",
        "",
        "| seed | eligible freq (f→c) | engaged freq (f→c) | self-origin Δ | route entropy (f→c) | no-op (f→c) | intra-day CV (f→c) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        L.append(
            f"| {r['seed']} | {r['eligible_freq_fixed']:.3f} → **{r['eligible_freq_circ']:.3f}** "
            f"| {r['engaged_freq_fixed']:.3f} → {r['engaged_freq_circ']:.3f} "
            f"| {r['self_origin_delta']:+.3f} "
            f"| {r['route_entropy_fixed']:.3f} → {r['route_entropy_circ']:.3f} "
            f"| {r['no_op_ratio_fixed']:.3f} → {r['no_op_ratio_circ']:.3f} "
            f"| {r['intraday_cv_fixed']:.3f} → **{r['intraday_cv_circ']:.3f}** |"
        )
    L += [
        "",
        "### 均值",
        "",
        f"- eligible frequency：fixed **{m['eligible_freq_fixed']:.3f}** → circadian **{m['eligible_freq_circ']:.3f}**",
        f"- engaged frequency（circadian）：**{m['engaged_freq_circ']:.3f}**",
        f"- self-origin Δ（circadian − fixed）：**{m['self_origin_delta']:+.3f}**",
        f"- route entropy（circadian）：**{m['route_entropy_circ']:.3f}**",
        f"- no-op ratio（circadian）：**{m['no_op_ratio_circ']:.3f}**",
        f"- intra-day gap CV（circadian）：**{m['intraday_cv_circ']:.3f}** · night wake share：**{m['night_wake_share_circ']:.3f}**",
        f"- revival delta（circadian，窗口内）：**{m['revival_delta_circ']:.2f}** · route_changed_by_bias：**{m['route_changed_by_bias_circ']:.2f}**",
        "",
        "## 2. 四项封板检查",
        "",
        "| 检查 | 结果 | 通过 |",
        "|---|---|---|",
        f"| eligible 稳定上升（c>f） | {ck['eligible_rises']['count']}/{ck['eligible_rises']['of']} seeds | {'✅' if ck['eligible_rises']['pass'] else '❌'} |",
        f"| 活动聚类出现（CV c>f） | {ck['clustering_appears']['count']}/{ck['clustering_appears']['of']} seeds | {'✅' if ck['clustering_appears']['pass'] else '❌'} |",
        f"| 聚类显著（CV>0.3） | {ck['clustering_high']['count']}/{ck['clustering_high']['of']} seeds | {'✅' if ck['clustering_high']['pass'] else '❌'} |",
        f"| route 熵不坍塌（min≥0.7） | min={ck['route_entropy_holds']['min']:.3f} | {'✅' if ck['route_entropy_holds']['pass'] else '❌'} |",
        f"| 无负向 delta（c<f 的 seed 数） | {ck['no_negative_delta']['count']}/{ck['no_negative_delta']['of']} seeds | {'✅' if ck['no_negative_delta']['pass'] else '❌'} |",
        "",
    ]
    verdict = "**Phase 5 可封板**" if summary["all_pass"] else "**未完全通过，见下方未过项**"
    L.append(f"**结论：{verdict}。**")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("outdir", nargs="?", default="out/circadian_cross_seed")
    ap.add_argument("--seeds", default="0-15", help="e.g. '0-15' or '0,3,7'")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--resume", action="store_true", help="skip a seed whose results.json exists")
    args = ap.parse_args()

    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    if "-" in args.seeds:
        lo, hi = map(int, args.seeds.split("-"))
        seeds = list(range(lo, hi + 1))
    else:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]

    rows = []
    for i, seed in enumerate(seeds):
        sdir = out / f"seed{seed:02d}"
        marker = sdir / "done.json"
        if args.resume and marker.exists():
            rows.append(json.loads(marker.read_text(encoding="utf-8")))
            print(f"[{i+1}/{len(seeds)}] seed {seed}: resumed", flush=True)
            continue
        c = run_comparison(sdir, days=args.days, seed=seed, self_revival=True)
        row = extract(c, seed)
        row["_run_at"] = datetime.now(timezone.utc).isoformat()
        marker.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
        rows.append(row)
        print(f"[{i+1}/{len(seeds)}] seed {seed}: eligible {row['eligible_freq_fixed']:.3f}→"
              f"{row['eligible_freq_circ']:.3f}  selfΔ {row['self_origin_delta']:+.3f}  "
              f"CV {row['intraday_cv_fixed']:.3f}→{row['intraday_cv_circ']:.3f}  "
              f"ent {row['route_entropy_circ']:.3f}", flush=True)

    rows.sort(key=lambda r: r["seed"])
    summary = summarise(rows)
    summary["meta"] = {"days": args.days, "seeds": seeds, "n_seeds": len(rows)}
    (out / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = report_markdown(rows, summary, days=args.days, seeds=seeds)
    (out / "REPORT.md").write_text(md, encoding="utf-8")
    print("\n=== SUMMARY ===")
    print(json.dumps(summary["means"], ensure_ascii=False, indent=2))
    print("checks:", json.dumps(summary["checks"], ensure_ascii=False))
    print("ALL_PASS:", summary["all_pass"])
    print(f"\n[written to {out / 'results.json'} and {out / 'REPORT.md'}]")


if __name__ == "__main__":
    sys.exit(main())
