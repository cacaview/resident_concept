"""Phase-4.1 revival-gate sensitivity sweep (route boost FIXED at 0.15).

Goal (per the phase brief): NOT to tune toward a target self-origin ratio, but to
map the *working region* of the dormant self-thread revival bias. We sweep three
gate/candidate knobs over a small grid, on the SAME 14-day user stream and a set
of fixed seeds, and record three metric layers plus a new *leverage* measure:

  - opportunity: eligible_wakes / eligible_rate / candidate_count
  - intervention: route_changed_by_bias / self_candidate_selected / revival_engaged
  - outcome:      self_origin_share / user_derived_share / generation_depth /
                  longest_self_chain / route_entropy / self_loop_concentration /
                  self_topic_diversity
  - revival_leverage = (S_C - S_B) / E   (self thoughts added PER genuine
    engagement; separates "high-freq weak effect" from "low-freq cascade")

Everything is deterministic (fixed seed + virtual clock). All anomalies,
negative results and single-trigger cascades are preserved in the raw output.
"""
from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from resident.event_store import EventStore
from resident.models import ROUTES
from resident.self_dynamics import _revival_counts
from resident.self_revival import RevivalParams
from resident.simulation import UserTurn, run_simulation

# --------------------------------------------------------------------------- config
DAYS = 14
SELF_PROB = 0.35          # B and C both use this (the A-group control is out of scope)
ROUTE_LIFT = 0.15         # FIXED in phase 1 (swept separately in phase 2)
THREAD_STRENGTH = 0.35    # FIXED
LOW_STIMULUS_H = 6        # NOT swept in phase 1 (kept at the Phase-4 default)

DORMANT_H = (6, 12, 24, 48)     # dormant_min_age
SILENCE_H = (2, 6, 12, 24)      # user_silence_min
MAX_ACTIVE = (1, 2, 3, 5)       # MAX_ACTIVE_THREADS
SEEDS = (0, 1, 2, 3)            # fixed seeds

# the SAME 14-day user stream used by the Phase-4 A/B/C experiment + tests.
SCRIPT14 = [
    UserTurn(1, 6, "pasta", "周末想做意面，有什么简单的酱吗？"),
    UserTurn(1, 15, "work", "下周有个项目 deadline，压力有点大。"),
    UserTurn(2, 9, "work", "deadline 相关，得重新排一下优先级。"),
    UserTurn(4, 9, "pasta", "那个意面后来做成功了，味道还不错。"),
    UserTurn(5, 6, "reading", "最近在读一本讲记忆的书，很有意思。"),
    UserTurn(7, 9, "work", "deadline 终于过去了，松了口气。"),
    UserTurn(7, 15, "reading", "记忆那本书读完了，想再聊聊。"),
    UserTurn(8, 6, "music", "最近开始学吉他，有点上头。"),
    UserTurn(10, 9, "pasta", "又想做意面了，这次想换个做法。"),
    UserTurn(11, 15, "work", "新项目开始了，又是一堆 deadline。"),
    UserTurn(13, 9, "reading", "又想起那本讲记忆的书，想重读。"),
    UserTurn(13, 15, "music", "吉他有点手感了，想继续。"),
    UserTurn(14, 6, "cooking", "想学做甜点，有什么入门的吗？"),
    UserTurn(14, 15, "work", "新项目的中期检查，有点紧张。"),
]

WORK = Path("/tmp/resident_sweep")


# ------------------------------------------------------------------ intervention
def intervention_metrics(store, route_lift: float) -> dict:
    """Opportunity + intervention layer, read straight from the append-only log.

    - eligible_wakes / candidate_count : opportunity (the gate opened; how many
      distinct dormant self-threads were ever eligible).
    - revisit_in_lull                  : of the eligible wakes, how many took the
      "revisit" route (the only route the lift can push).
    - route_changed_by_bias            : counterfactual — among eligible wakes that
      took revisit, how many would NOT have taken it without the lift (computed
      from the recorded score vector, holding the exploration draw constant).
    - self_candidate_selected          : of the genuine self-revivals, how many
      landed on the *specific* eligible candidate (vs. some other self thread).
    - revival_engaged                  : genuine self-thread revivals (the ground
      truth "a sleeping thought of its own resurfaced").
    """
    rs = store.list(100000, type_prefix="wake.route_selected")
    eligible = [e for e in rs if e.content.get("self_revival_candidate")]
    cand_ids = {e.content.get("self_revival_candidate_thread") for e in eligible
                if e.content.get("self_revival_candidate_thread")}
    revisit_in_lull = 0
    changed = 0
    for e in eligible:
        c = e.content
        if c.get("route") != "revisit":
            continue
        revisit_in_lull += 1
        scores = dict(c.get("scores") or {})
        if not scores:
            continue
        scores["revisit"] = scores.get("revisit", 0.0) - route_lift
        # replicate max(ROUTES, key=...) tie-breaking: first maximal in ROUTES order
        if max(ROUTES, key=lambda r: scores.get(r, float("-inf"))) != "revisit":
            changed += 1
    # self_candidate_selected: genuine self-revivals whose thread == the wake's candidate
    thoughts = {e.id: e for e in store.list(100000, type_prefix="thought.created")}
    rs_by_id = {e.id: e for e in rs}
    engaged = 0
    selected = 0
    for e in store.list(100000, type_prefix="thread.revisited"):
        c = e.content
        if not c.get("self_revival"):
            continue
        engaged += 1
        tid = c.get("thread_id")
        rid = None
        for cid in (e.links.get("caused_by") or []):
            t = thoughts.get(cid)
            if t:
                for c2 in (t.links.get("caused_by") or []):
                    if c2 in rs_by_id:
                        rid = c2
                if rid:
                    break
        if rid:
            cand = rs_by_id[rid].content.get("self_revival_candidate_thread")
            if cand and cand == tid:
                selected += 1
    return {
        "eligible_wakes": len(eligible),
        "candidate_count": len(cand_ids),
        "revisit_in_lull": revisit_in_lull,
        "route_changed_by_bias": changed,
        "self_candidate_selected": selected,
        "revival_engaged": engaged,
    }


# ----------------------------------------------------------------------- workers
def _self_thoughts(report: dict) -> int:
    od = report["origin_distribution"]
    return od.get("self_thread", 0) + od.get("private_project", 0)


def run_one(task: dict) -> dict:
    """Run one (kind, seed, config) simulation and return a metric row."""
    kind = task["kind"]
    seed = task["seed"]
    try:
        if kind == "B":
            self_revival, revival = False, None
            tag = f"B_s{seed}"
        else:
            revival = RevivalParams(
                silence_s=task["silence_h"] * 3600,
                low_stimulus_s=LOW_STIMULUS_H * 3600,
                max_active=task["max_active"],
                min_dormant_s=task["dormant_h"] * 3600,
                min_self_events=1,
                route_lift=ROUTE_LIFT,
                thread_strength=THREAD_STRENGTH,
            )
            self_revival = True
            tag = (f"C_s{seed}_d{task['dormant_h']}h_s{task['silence_h']}h"
                   f"_a{task['max_active']}")
        d = WORK / tag
        report = asyncio.run(run_simulation(
            d, days=DAYS, seed=seed, user_script=SCRIPT14,
            self_prob=SELF_PROB, self_revival=self_revival, revival=revival))
        store = EventStore(d / "sim.sqlite3")
        iv = intervention_metrics(store, ROUTE_LIFT)
        del store
        sd = report["self_dynamics"]
        n_wakes = report["meta"]["n_wakes"]
        row = {
            "kind": kind, "seed": seed, "tag": tag,
            "dormant_h": task.get("dormant_h"), "silence_h": task.get("silence_h"),
            "max_active": task.get("max_active"),
            "route_lift": ROUTE_LIFT, "thread_strength": THREAD_STRENGTH,
            "low_stimulus_h": LOW_STIMULUS_H,
            # opportunity
            "eligible_wakes": iv["eligible_wakes"],
            "eligible_rate": round(iv["eligible_wakes"] / n_wakes, 4) if n_wakes else 0.0,
            "candidate_count": iv["candidate_count"],
            # intervention
            "revisit_in_lull": iv["revisit_in_lull"],
            "route_changed_by_bias": iv["route_changed_by_bias"],
            "self_candidate_selected": iv["self_candidate_selected"],
            "revival_engaged": iv["revival_engaged"],
            # outcome
            "self_thoughts": _self_thoughts(report),
            "self_origin_share": report["independence"]["self_origin_share"],
            "user_derived_share": report["independence"]["user_derived_share"],
            "generation_depth": sd["self_thread_generation_depth"],
            "longest_self_chain": sd["longest_self_chain"],
            "route_entropy": report["route_entropy"],
            "conc_top1": sd["self_loop_concentration"]["top1_share"],
            "conc_total": sd["self_loop_concentration"]["total"],
            "div_n": sd["self_topic_diversity"]["n_distinct"],
            "div_entropy": sd["self_topic_diversity"]["entropy"],
            "avg_lifetime_days": sd["self_thread_avg_lifetime_days"],
            "n_wakes": n_wakes,
            "n_self_threads": report["meta"]["n_self_threads"],
        }
        return row
    except Exception as exc:  # keep the pool alive; record the failure
        return {"kind": kind, "seed": seed, "tag": tag, "error": repr(exc)}


def build_tasks() -> list[dict]:
    tasks = [{"kind": "B", "seed": s} for s in SEEDS]
    for dh in DORMANT_H:
        for sh in SILENCE_H:
            for ma in MAX_ACTIVE:
                for s in SEEDS:
                    tasks.append({"kind": "C", "seed": s,
                                  "dormant_h": dh, "silence_h": sh, "max_active": ma})
    return tasks


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def aggregate(rows: list[dict]) -> dict:
    """Group C rows by config, average over seeds, attach per-seed spread + leverage."""
    b_by_seed = {r["seed"]: r["self_thoughts"] for r in rows if r["kind"] == "B"}
    by_cfg: dict[tuple, list[dict]] = {}
    for r in rows:
        if r["kind"] == "C" and "error" in r:
            continue
        if r["kind"] != "C":
            continue
        key = (r["dormant_h"], r["silence_h"], r["max_active"])
        by_cfg.setdefault(key, []).append(r)

    out = []
    for (dh, sh, ma), crows in by_cfg.items():
        per_seed = []
        for s in SEEDS:
            cr = next((r for r in crows if r["seed"] == s), None)
            if cr is None:
                continue
            sb = b_by_seed.get(s)
            e = cr["revival_engaged"]
            delta = (cr["self_thoughts"] - sb) if sb is not None else None
            lev = (round(delta / e, 3) if (e > 0 and delta is not None) else None)
            per_seed.append({
                "seed": s,
                "eligible_wakes": cr["eligible_wakes"],
                "candidate_count": cr["candidate_count"],
                "route_changed_by_bias": cr["route_changed_by_bias"],
                "self_candidate_selected": cr["self_candidate_selected"],
                "revival_engaged": e,
                "self_thoughts": cr["self_thoughts"],
                "self_baseline": sb,
                "self_delta": delta,
                "leverage": lev,
                "self_origin_share": cr["self_origin_share"],
                "route_entropy": cr["route_entropy"],
                "conc_top1": cr["conc_top1"],
                "generation_depth": cr["generation_depth"],
                "longest_self_chain": cr["longest_self_chain"],
            })
        agg = {
            "dormant_h": dh, "silence_h": sh, "max_active": ma,
            "eligible_wakes": _mean([p["eligible_wakes"] for p in per_seed]),
            "eligible_rate": _mean([_rate(p["eligible_wakes"]) for p in per_seed]),
            "candidate_count": _mean([p["candidate_count"] for p in per_seed]),
            "route_changed_by_bias": _mean([p["route_changed_by_bias"] for p in per_seed]),
            "self_candidate_selected": _mean([p["self_candidate_selected"] for p in per_seed]),
            "revival_engaged": _mean([p["revival_engaged"] for p in per_seed]),
            "self_delta": _mean([p["self_delta"] for p in per_seed]),
            "leverage": _mean([p["leverage"] for p in per_seed]),
            "self_origin_share": _mean([p["self_origin_share"] for p in per_seed]),
            "route_entropy": _mean([p["route_entropy"] for p in per_seed]),
            "conc_top1": _mean([p["conc_top1"] for p in per_seed]),
            "generation_depth": _mean([p["generation_depth"] for p in per_seed]),
            "longest_self_chain": _mean([p["longest_self_chain"] for p in per_seed]),
            "per_seed": per_seed,
        }
        agg["zone"] = classify_zone(agg)
        out.append(agg)
    # order: dormant_h, silence_h, max_active
    out.sort(key=lambda a: (a["dormant_h"], a["silence_h"], a["max_active"]))
    return {
        "n_configs": len(out), "seeds": list(SEEDS),
        "self_baseline_by_seed": b_by_seed,
        "configurations": out,
    }


def _rate(elig: int | None) -> float | None:
    return round(elig / 84, 4) if elig is not None else None  # 14d x 6 wakes = 84


def classify_zone(a: dict) -> str:
    """A *descriptive* reading of where a config sits, from the aggregated metrics.
    This is a map, not an optimisation: it names the regime, it does not pick a
    'best' config. Thresholds are generous and meant to be read alongside the
    raw numbers, not to be tuned."""
    elig = a["eligible_wakes"] or 0
    eng = a["revival_engaged"] or 0
    delta = a["self_delta"] if a["self_delta"] is not None else 0
    top1 = a["conc_top1"] or 0
    # barely opens / never engages
    if eng < 0.25:
        return "几乎不触发区" if elig < 1.0 else "有机会·未接住区"
    # engages at least sometimes
    if top1 >= 0.70 and delta >= 4:
        return "过度自我吸附区"
    if eng >= 3.0 and delta >= 4:
        return "高频有效区"
    return "稳定偶发区"


def main() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    tasks = build_tasks()
    print(f"[sweep] {len(tasks)} runs "
          f"({sum(1 for t in tasks if t['kind'] == 'C')} C + "
          f"{sum(1 for t in tasks if t['kind'] == 'B')} B), seeds={list(SEEDS)}",
          flush=True)
    with ProcessPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(run_one, tasks))
    errors = [r for r in rows if "error" in r]
    print(f"[sweep] done; {len(rows) - len(errors)} ok, {len(errors)} errors", flush=True)
    for r in errors[:10]:
        print("  ERR", r.get("tag"), r.get("error"), flush=True)
    agg = aggregate(rows)
    (WORK / "results.json").write_text(json.dumps(agg, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    # compact phase map
    hdr = (f"{'d':>3} {'s':>3} {'a':>2} | {'elig':>5} {'cand':>4} {'chg':>4} "
           f"{'sel':>4} {'eng':>4} | {'dS':>4} {'lev':>5} {'self%':>6} {'ent':>5} "
           f"{'top1':>5} | zone")
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for a in agg["configurations"]:
        def f(x, n=1):
            return ("-" if x is None else (f"{x:.{n}f}" if isinstance(x, float) else f"{x}"))
        print(f"{a['dormant_h']:>3} {a['silence_h']:>3} {a['max_active']:>2} | "
              f"{f(a['eligible_wakes'],0):>5} {f(a['candidate_count'],0):>4} "
              f"{f(a['route_changed_by_bias'],0):>4} {f(a['self_candidate_selected'],0):>4} "
              f"{f(a['revival_engaged'],0):>4} | "
              f"{f(a['self_delta']):>4} {f(a['leverage']):>5} "
              f"{f(a['self_origin_share'],3):>6} {f(a['route_entropy'],3):>5} "
              f"{f(a['conc_top1'],2):>5} | {a['zone']}")
    print(f"\nB baseline self-thoughts by seed: {agg['self_baseline_by_seed']}")
    print(f"\n[written to {WORK / 'results.json'}]")


if __name__ == "__main__":
    sys.exit(main())
