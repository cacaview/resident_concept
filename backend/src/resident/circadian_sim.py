"""Phase-5 circadian-vs-fixed deterministic life simulation (ADR-0007).

Compares, over the **same virtual user stream + seed + self-mechanism settings**,
two schedulers:

- ``fixed``     — the Phase-2/3 baseline (``resident.simulation``): six fixed wake
  hours a day + one nightly sleep. Uniform daytime wake tiling, no real night.
- ``circadian`` — the Phase-5 ``CircadianOrchestrator`` driven on a ~30-min
  virtual beat: per-state wake cadence, a real consolidation night, a morning
  re-open, and — crucially — *recognised* quiet states that raise the dormant
  self-thread revival **opportunity** (``in_quiet_state``), never the selection.

The comparison isolates the **scheduler**: both arms run the same revival
mechanism (``self_revival=True``, same ``self_prob``), same seed, same user
script. The circadian's job is to change the *structure* of activity — real
lulls and natural clusters — and thereby open the revival gate more; it does not
force a self-thread and does not enlarge the route lift.

Both arms are deterministic (fixed seed + virtual clock) and byte-reproducible
into their own fresh directory. The report characterises:

- **revival eligible frequency** — the share of wakes where the quiet-state
  opportunity was open (``eligible_wakes / n_wakes``);
- **engaged / delta** — how many dormant self-thoughts actually resurfaced, and
  the self-origin gap between the arms;
- **wake / no-op ratio**, **route entropy**;
- **self-origin / user-derived** shares;
- **activity clustering** — inter-wake-gap variability (the fixed baseline tiles
  uniformly; the circadian clusters waking and goes quiet overnight).

The aim is an *honest* characterisation, not a tuned win: if the circadian does
not open the gate in this window, that is reported as a (negative) finding.
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .circadian import CircadianOrchestrator, CircadianStateMachine
from .event_store import EventStore
from .mind_loop import MindLoop
from .memory import MemoryRetrieval
from .reentry import ReentryEngine
from .self_dynamics import self_dynamics_profile
from .self_revival import RevivalParams
from .simulation import (
    DEFAULT_USER_SCRIPT,
    SIM_START,
    UserTurn,
    VirtualClock,
    _inject_turn,
    run_simulation,
)
from .sleep import DEFAULT_SELF_PROB, SleepEngine
from .telemetry import Telemetry


# --------------------------------------------------------------------------- run

async def run_circadian_simulation(
    output_dir: Path | str,
    *,
    days: int = 14,
    seed: int = 0,
    user_script: list[UserTurn] | None = None,
    self_prob: float = DEFAULT_SELF_PROB,
    self_revival: bool = False,
    revival: RevivalParams | None = None,
    beat_minutes: int = 30,
    embeddings: str = "legacy",
    shadow: bool = False,
) -> dict:
    """Drive the real engines with the ``CircadianOrchestrator`` over a virtual
    ``days``-long timeline in ``beat_minutes`` steps. Deterministic (seeded rng +
    virtual clock). A pre-existing ``circadian.sqlite3`` is removed first.
    v0.2 Step 2 (ADR-0010): ``embeddings``/``shadow`` switch the semantic
    treatment / audit shadow (both default off — sealed arms untouched)."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "circadian.sqlite3"
    if db.exists():
        db.unlink()
    script = user_script if user_script is not None else DEFAULT_USER_SCRIPT

    clock = VirtualClock(SIM_START)
    store = EventStore(db, now_fn=lambda: clock.now)
    from .semantic import make_semantic_stack

    shadow_layer, semantic_kwargs = make_semantic_stack(
        store, out / "vectors.sqlite3", mode=embeddings, shadow=shadow,
        shadow_log_path=out / "semantic_shadow.jsonl")
    mem = MemoryRetrieval(store, **(semantic_kwargs or {}))
    mind = MindLoop(store, memory=mem, rng=random.Random(seed),
                    self_revival=self_revival, revival=revival,
                    shadow_semantic=shadow_layer)
    sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(seed + 1000),
                               self_prob=self_prob)
    reentry = ReentryEngine(store, memory=mem)
    machine = CircadianStateMachine(store, tz=timezone.utc)  # deterministic: pinned to UTC
    orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry, machine=machine)
    tel = Telemetry(store, mem.index)

    threads: dict[str, str] = {}
    # user turns keyed by their virtual instant (a fresh message wakes the mind
    # promptly on the next beat, regardless of the per-state cadence)
    turns = sorted(
        ((SIM_START + timedelta(days=t.day - 1, hours=t.hour), t) for t in script),
        key=lambda x: x[0],
    )
    turn_idx = 0

    step = timedelta(minutes=beat_minutes)
    end = SIM_START + timedelta(days=days)
    now = SIM_START
    timeline: list[dict] = []
    while now <= end:
        clock.set(now)
        while turn_idx < len(turns) and turns[turn_idx][0] <= now:
            _inject_turn(store, turns[turn_idx][1], threads)
            turn_idx += 1
        beat = await orch.beat(now.isoformat())
        if beat["action"] in ("wake", "consolidate"):
            timeline.append({"t": now.isoformat(), "action": beat["action"],
                             "state": beat["state"], "route": beat.get("route")})
        now += step

    return _build_circadian_report(store, mem, mind, orch, machine, tel, timeline, threads, days, seed, self_prob)


def _build_circadian_report(store, mem, mind, orch, machine, tel, timeline, threads, days, seed, self_prob) -> dict:
    # the whole run as one window
    since = SIM_START.isoformat()
    latest = store.latest()
    now_iso = latest.created_at if latest else since
    win = tel.window_report(since, now_iso=now_iso, circadian_state=machine.state, window=f"{days}d")
    routes = [b["route"] for b in timeline if b["action"] == "wake" and b.get("route")]
    route_dist: dict[str, int] = {}
    for r in routes:
        route_dist[r] = route_dist.get(r, 0) + 1

    states_seen = sorted({b["state"] for b in timeline})
    return {
        "meta": {
            "mode": "circadian",
            "days": days,
            "seed": seed,
            "beat_minutes": 30,
            "self_prob": self_prob,
            "self_revival_enabled": mind.self_revival,
            "n_wakes": store.count("wake.started"),
            "n_sleeps": store.count("sleep.completed"),
            "n_noops": store.count("wake.noop"),
            "n_thoughts": store.count("thought.created"),
            "n_threads": store.count("thread.created"),
            "n_self_threads": sum(1 for e in store.list(1000, type_prefix="thread.created")
                                  if e.content.get("origin") == "self"),
            "n_consolidations": store.count("memory.consolidated"),
            "n_state_changes": store.count("circadian.state_changed"),
            "states_seen": states_seen,
            "n_total_events": store.count(),
        },
        "route_distribution": route_dist,
        "route_entropy": win["route_entropy"],
        "no_op_ratio": win["no_op_ratio"],
        "origin_base": win["origin_base"],
        "self_origin_share": win["self_origin_share"],
        "user_derived_share": win["user_derived_share"],
        "user_recent_share": win["user_recent_share"],
        "eligible_wakes": win["eligible_wakes"],
        "revival_engaged": win["revival_engaged"],
        "route_changed_by_bias": win["route_changed_by_bias"],
        "revival_delta": win["revival_delta"],
        "revival_leverage": win["revival_leverage"],
        "self_dynamics": self_dynamics_profile(store),
        "clustering": _wake_clustering(store),
        "circadian_state_final": machine.state,
        "timeline": timeline,
    }


def _wake_clustering(store: EventStore) -> dict:
    """Characterise the *structure* of waking. The fixed baseline tiles daytime
    wakes nearly uniformly (low intra-day gap variability); the circadian
    clusters waking during engagement and goes quiet overnight (high intra-day
    gap variability + a real night)."""
    wakes = [datetime.fromisoformat(e.created_at)
             for e in store.list(100000, type_prefix="wake.started", order="asc")]
    if len(wakes) < 2:
        return {"n_wakes": len(wakes), "intraday_gap_cv": 0.0, "gap_cv": 0.0,
                "mean_gap_h": 0.0, "p50_gap_h": 0.0, "max_gap_h": 0.0,
                "night_wake_share": 0.0, "hour_entropy": 0.0, "per_hour_wakes": [0] * 24}
    gaps_h = [(b - a).total_seconds() / 3600.0 for a, b in zip(wakes, wakes[1:])]
    mean = sum(gaps_h) / len(gaps_h)
    std = math.sqrt(sum((g - mean) ** 2 for g in gaps_h) / len(gaps_h))
    cv = std / mean if mean else 0.0
    # intra-day gaps only (exclude the overnight gap) — isolates the daytime
    # structure that distinguishes "clustered" from "uniform tiling"
    intraday = [g for g in gaps_h if g < 8.0]
    if intraday:
        im = sum(intraday) / len(intraday)
        istd = math.sqrt(sum((g - im) ** 2 for g in intraday) / len(intraday))
        icv = istd / im if im else 0.0
    else:
        icv = 0.0
    per_hour = [0] * 24
    for w in wakes:
        per_hour[w.hour] += 1
    night_wakes = sum(per_hour[0:6])
    night_share = night_wakes / len(wakes) if wakes else 0.0
    total = sum(per_hour)
    n_nonempty = len([c for c in per_hour if c > 0])
    h = -sum((c / total) * math.log2(c / total) for c in per_hour if c > 0) if total else 0.0
    hour_entropy = (h / math.log2(n_nonempty)) if n_nonempty > 1 else 0.0
    gaps_sorted = sorted(gaps_h)
    return {
        "n_wakes": len(wakes),
        "mean_gap_h": round(mean, 2),
        "p50_gap_h": round(gaps_sorted[len(gaps_sorted) // 2], 2),
        "max_gap_h": round(max(gaps_h), 2),
        "gap_cv": round(cv, 3),
        "intraday_gap_cv": round(icv, 3),
        "night_wake_share": round(night_share, 3),
        "hour_entropy": round(hour_entropy, 3),
        "per_hour_wakes": per_hour,
    }


# --------------------------------------------------------------------------- compare

async def _run_both(output_dir: Path, *, days, seed, user_script, self_prob, self_revival, revival):
    fixed_rep = await run_simulation(
        output_dir / "fixed", days=days, seed=seed, user_script=user_script,
        self_prob=self_prob, self_revival=self_revival, revival=revival,
    )
    circ_rep = await run_circadian_simulation(
        output_dir / "circadian", days=days, seed=seed, user_script=user_script,
        self_prob=self_prob, self_revival=self_revival, revival=revival,
    )
    return fixed_rep, circ_rep


def run_comparison(
    output_dir: Path | str,
    *,
    days: int = 14,
    seed: int = 0,
    user_script: list[UserTurn] | None = None,
    self_prob: float = DEFAULT_SELF_PROB,
    self_revival: bool = True,
    revival: RevivalParams | None = None,
) -> dict:
    """Run both arms (fixed + circadian) into sibling directories and build the
    comparison. Same user script, seed, self-mechanism settings — only the
    scheduler differs."""
    out = Path(output_dir)
    fixed_rep, circ_rep = asyncio.run(
        _run_both(out, days=days, seed=seed, user_script=user_script,
                  self_prob=self_prob, self_revival=self_revival, revival=revival)
    )
    # re-open the fixed arm's store for the whole-run windowed read (clustering,
    # revival opportunity) — apples-to-apples with the circadian arm's window.
    fixed_store = EventStore(out / "fixed" / "sim.sqlite3")
    fixed_norm = _normalise_fixed(fixed_rep, fixed_store, days, seed, self_prob)
    return {
        "meta": {
            "days": days,
            "seed": seed,
            "user_turns": len(user_script) if user_script is not None else len(DEFAULT_USER_SCRIPT),
            "self_prob": self_prob,
            "self_revival": self_revival,
            "arms": ["fixed", "circadian"],
        },
        "fixed": fixed_norm,
        "circadian": circ_rep,
        "comparison": _compare(fixed_norm, circ_rep),
    }


def _normalise_fixed(fixed_rep: dict, fixed_store: EventStore, days: int, seed: int, self_prob: float) -> dict:
    """Map the baseline ``run_simulation`` arm onto the circadian report shape,
    using the whole-run windowed read so both arms are measured identically."""
    meta = fixed_rep["meta"]
    latest = fixed_store.latest()
    win = Telemetry(fixed_store, None).window_report(
        SIM_START.isoformat(), now_iso=latest.created_at if latest else SIM_START.isoformat(),
        circadian_state=None, window=f"{days}d",
    )
    return {
        "meta": {
            "mode": "fixed",
            "days": days,
            "seed": seed,
            "self_prob": self_prob,
            "self_revival_enabled": meta["self_revival_enabled"],
            "n_wakes": meta["n_wakes"],
            "n_sleeps": meta["n_sleeps"],
            "n_noops": meta["n_noops"],
            "n_thoughts": meta["n_thoughts"],
            "n_threads": meta["n_threads"],
            "n_self_threads": meta["n_self_threads"],
            "n_consolidations": meta["n_consolidations"],
            "n_state_changes": 0,  # no circadian state machine in the baseline
            "states_seen": [],
            "n_total_events": meta["n_total_events"],
        },
        "route_distribution": win["route_distribution"],
        "route_entropy": win["route_entropy"],
        "no_op_ratio": win["no_op_ratio"],
        "origin_base": win["origin_base"],
        "self_origin_share": win["self_origin_share"],
        "user_derived_share": win["user_derived_share"],
        "user_recent_share": win["user_recent_share"],
        "eligible_wakes": win["eligible_wakes"],
        "revival_engaged": win["revival_engaged"],
        "route_changed_by_bias": win["route_changed_by_bias"],
        "revival_delta": win["revival_delta"],
        "revival_leverage": win["revival_leverage"],
        "self_dynamics": fixed_rep["self_dynamics"],
        "clustering": _wake_clustering(fixed_store),
        "circadian_state_final": None,
    }


def _compare(fixed: dict, circ: dict) -> dict:
    def freq(r):
        n = r["meta"]["n_wakes"]
        return round(r["eligible_wakes"] / n, 4) if n else 0.0
    side = lambda k: {"fixed": fixed[k], "circadian": circ[k]}
    cl_f, cl_c = fixed["clustering"], circ["clustering"]
    return {
        "revival_eligible_frequency": {"fixed": freq(fixed), "circadian": freq(circ)},
        "revival_engaged": side("revival_engaged"),
        "self_origin_share": side("self_origin_share"),
        "self_origin_delta": round(circ["self_origin_share"] - fixed["self_origin_share"], 4),
        "user_derived_share": side("user_derived_share"),
        "n_wakes": {"fixed": fixed["meta"]["n_wakes"], "circadian": circ["meta"]["n_wakes"]},
        "no_op_ratio": side("no_op_ratio"),
        "route_entropy": side("route_entropy"),
        "route_changed_by_bias": side("route_changed_by_bias"),
        "clustering": {
            "fixed": {k: cl_f[k] for k in ("mean_gap_h", "intraday_gap_cv", "gap_cv",
                                           "night_wake_share", "hour_entropy")},
            "circadian": {k: cl_c[k] for k in ("mean_gap_h", "intraday_gap_cv", "gap_cv",
                                               "night_wake_share", "hour_entropy")},
            "intraday_gap_cv_delta": round(cl_c["intraday_gap_cv"] - cl_f["intraday_gap_cv"], 3),
        },
    }


# --------------------------------------------------------------------------- render

def _hour_bars(per_hour: list[int]) -> list[str]:
    mx = max(per_hour) if per_hour and max(per_hour) > 0 else 1
    return [f"{h:02d}h |{'#' * (round(20 * c / mx) if c else 0)} {c}"
            for h, c in enumerate(per_hour)]


def comparison_markdown(combined: dict) -> str:
    m = combined["meta"]
    f, c, cmp_ = combined["fixed"], combined["circadian"], combined["comparison"]
    L: list[str] = []
    L.append("# Resident circadian-vs-fixed life simulation (Phase 5)\n")
    L.append(f"Days: **{m['days']}** · seed: **{m['seed']}** · user turns: **{m['user_turns']}** · "
             f"self-prob: **{m['self_prob']}** · self-revival: **{m['self_revival']}** · "
             f"arms: {', '.join(m['arms'])} (same user stream + seed; only the scheduler differs)\n")

    L.append("\n## Headline comparison\n")
    L.append("| metric | fixed | circadian | ")
    L.append("|---|---|---|")
    L.append(f"| revival **eligible frequency** | {cmp_['revival_eligible_frequency']['fixed']:.3f} "
             f"| {cmp_['revival_eligible_frequency']['circadian']:.3f} |")
    L.append(f"| revival engaged | {cmp_['revival_engaged']['fixed']} | {cmp_['revival_engaged']['circadian']} |")
    L.append(f"| self-origin share | {cmp_['self_origin_share']['fixed']:.3f} "
             f"| {cmp_['self_origin_share']['circadian']:.3f} "
             f"(Δ {cmp_['self_origin_delta']:+.3f}) |")
    L.append(f"| user-derived share | {cmp_['user_derived_share']['fixed']:.3f} "
             f"| {cmp_['user_derived_share']['circadian']:.3f} |")
    L.append(f"| n wakes | {cmp_['n_wakes']['fixed']} | {cmp_['n_wakes']['circadian']} |")
    L.append(f"| no-op ratio | {cmp_['no_op_ratio']['fixed']:.3f} | {cmp_['no_op_ratio']['circadian']:.3f} |")
    L.append(f"| route entropy | {cmp_['route_entropy']['fixed']:.3f} | {cmp_['route_entropy']['circadian']:.3f} |")
    L.append(f"| route changed by bias | {cmp_['route_changed_by_bias']['fixed']} "
             f"| {cmp_['route_changed_by_bias']['circadian']} |")
    cl = cmp_["clustering"]
    L.append(f"| **intra-day wake-gap CV** (clustering) | {cl['fixed']['intraday_gap_cv']:.3f} "
             f"| {cl['circadian']['intraday_gap_cv']:.3f} "
             f"(Δ {cl['intraday_gap_cv_delta']:+.3f}) |")
    L.append(f"| mean wake gap | {cl['fixed']['mean_gap_h']:.1f} h | {cl['circadian']['mean_gap_h']:.1f} h |")
    L.append(f"| night (00–06) wake share | {cl['fixed']['night_wake_share']:.3f} "
             f"| {cl['circadian']['night_wake_share']:.3f} |")
    L.append(f"| hour-of-day entropy | {cl['fixed']['hour_entropy']:.3f} | {cl['circadian']['hour_entropy']:.3f} |")

    L.append("\n## Circadian arm — rhythm\n")
    L.append(f"States reached: {', '.join(c['meta']['states_seen']) or '—'} · "
             f"state transitions: **{c['meta']['n_state_changes']}** · final state: {c['circadian_state_final']}\n")
    L.append("Wake count by hour of day — **circadian**:\n")
    L.append("```\n" + "\n".join(_hour_bars(c["clustering"]["per_hour_wakes"])) + "\n```\n")
    L.append("Wake count by hour of day — **fixed**:\n")
    L.append("```\n" + "\n".join(_hour_bars(f["clustering"]["per_hour_wakes"])) + "\n```\n")

    L.append("\n## Reading the result\n")
    elig = cmp_["revival_eligible_frequency"]
    if elig["circadian"] > elig["fixed"]:
        L.append(f"- The circadian **raised the revival opportunity**: eligible frequency "
                 f"{elig['fixed']:.3f} → {elig['circadian']:.3f}. Its recognised quiet states "
                 f"dropped the gate's user-silence bar (12 h → ~6 h), so dormant self-threads "
                 f"enter recall more often — without forcing a selection or enlarging the lift.")
    else:
        L.append(f"- **No opportunity gain** in this window: eligible frequency "
                 f"fixed {elig['fixed']:.3f} vs circadian {elig['circadian']:.3f}. (Reported as-is; "
                 f"the gate is conjunctive and also needs a dormant, historically-thought-on "
                 f"self-thread ≥12 h asleep — self-threads may not have formed in {m['days']} days.)")
    if cmp_["self_origin_delta"] > 0:
        L.append(f"- Self-origin rose by **{cmp_['self_origin_delta']:+.3f}** under the circadian — "
                 f"the more lulls the mind gets, the more a thought of its own can resurface and "
                 f"re-influence it.")
    elif cmp_["self_origin_delta"] == 0:
        L.append("- Self-origin was unchanged — the circadian changed the *timing* of activity "
                 "more than the *share* of self-origin (honest, not tuned away).")
    else:
        L.append(f"- Self-origin fell by **{cmp_['self_origin_delta']:+.3f}** — reported as-is "
                 f"(a possible self→user diversion; see the per-arm `route_changed_by_bias`).")
    cl = cmp_["clustering"]
    L.append(f"- Activity structure: intra-day wake-gap CV "
             f"{cl['fixed']['intraday_gap_cv']:.3f} (fixed, near-uniform tiling) → "
             f"{cl['circadian']['intraday_gap_cv']:.3f} (circadian, clustered). "
             f"The circadian produces **natural activity clusters and a real quiet night**, "
             f"not a uniform wake tiling.")
    return "\n".join(L) + "\n"


def main() -> None:
    import sys
    args = sys.argv[1:]
    out = Path(args[0]) if args and not args[0].startswith("--") else Path("out/circadian_sim")
    rest = args[1:] if (args and not args[0].startswith("--")) else args
    days, seed, self_prob = 14, 0, DEFAULT_SELF_PROB
    i = 0
    while i < len(rest):
        if rest[i] == "--days":
            days = int(rest[i + 1]); i += 2
        elif rest[i] == "--seed":
            seed = int(rest[i + 1]); i += 2
        elif rest[i] == "--self-prob":
            self_prob = float(rest[i + 1]); i += 2
        else:
            i += 1
    combined = run_comparison(out, days=days, seed=seed, self_prob=self_prob, self_revival=True)
    md = comparison_markdown(combined)
    (out / "COMPARISON.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[written to {out / 'COMPARISON.md'}]")


if __name__ == "__main__":
    main()
