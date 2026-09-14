"""Phase-6 World-Window A/B/C deterministic life simulation (ADR-0008).

The phase brief (verbatim, the governing constraint):

> 做三组 deterministic simulation：A. no world / B. follow + edge /
> C. follow + edge + alien + accident。使用多个固定 seed，保持用户流/circadian/
> self-thread 机制完全相同。不要以降低 user_derived 为优化目标，不要调参数追漂亮比例。

So the three arms run the **same** user stream, the **same** seed, the **same**
circadian orchestrator (driven on a 30-min virtual beat), the **same** mind
(``self_revival=True``, the same seeded rng) and the **same** self-thread
mechanism (``self_prob``). The **only** thing that differs is the World Window:

- **A. no world**      — ``world_engine=None`` (the true control: no world
  machinery at all — no ``world.*`` / ``impression.*`` events, and no
  world-origin thoughts/threads the mind can see);
- **B. follow + edge** — ``WORLD_FOLLOW_EDGE`` (on-topic + cognitive boundary);
- **C. all four**      — ``WORLD_ALL`` (follow + edge + alien + accident).

Isolation: the world engine carries its **own** ``random.Random`` (seeded
``seed + 2000``), distinct from the mind's (``seed``) and sleep's (``seed + 1000``)
streams, so it never disturbs the mind's rng draw sequence. What the arms share
is the *mechanism* (the code path, the seeds, the user script); what B/C add on
top is one new *material source*. B/C's mind can (and does) read the world
material it has gathered — that is exactly how the "alien/accident revisited days
later" and "world + old memory → thought" signals the phase asks us to watch are
measured. It is **not** claimed that B/C's mind output is byte-identical to A's
(that would be false: they differ by precisely the world experience they gained);
it **is** claimed — and verified — that re-running any single arm is
byte-reproducible, and that A is the clean no-world control.

The report is an **honest characterisation, not a tuned win**. Per the owner
constraint, the goal is **not** to drive ``user_derived`` down and **not** to tune
toward a pretty ratio — the question is whether the World becomes a genuine third
kind of *experience* rather than turning Resident into a news summariser or a
recommender. The watch-metrics that answer that (all in :func:`world_profile`):

- ``seen_left_nothing_rate`` — the share of things looked at that left nothing
  (reading ≠ thought);
- ``world_to_thought_rate`` — how often a look became a durable thought (low =
  not churning);
- ``n_world_plus_memory_thoughts`` / ``cross_source_association_rate`` — world
  material genuinely connecting to the resident's *own* old memory;
- ``alien_survival_rate`` — alien/accident material naturally revisited days later;
- ``source_concentration`` / ``topic_concentration`` — adhesion to one source/topic
  (high = turning into a single-source reader / recommender — a *finding* if so);
- the three-way origin split (``user_derived`` / ``self_origin`` / ``world_origin``) —
  world as a *third* material source, not a dilution of the user's.

Every arm is deterministic (fixed seed + virtual clock) and byte-reproducible
into its own fresh directory. No threshold is tuned to hit a ratio; if an arm
does nothing for a day (0 opens), that is a legitimate, non-failure outcome.
"""
from __future__ import annotations

import asyncio
import dataclasses
import random
from datetime import timedelta
from pathlib import Path
from typing import Optional

from .circadian import CircadianOrchestrator, CircadianStateMachine
from .continuity import ContinuityEngine
from .event_store import EventStore
from .mind_loop import MindLoop
from .memory import MemoryRetrieval
from .reentry import ReentryEngine
from .self_revival import RevivalParams
from .simulation import (
    DEFAULT_USER_SCRIPT,
    SIM_START,
    UserTurn,
    VirtualClock,
    _inject_turn,
)
from .sleep import DEFAULT_SELF_PROB, SleepEngine
from .telemetry import Telemetry
from .world import WORLD_ALL, WORLD_FOLLOW_EDGE, WorldParams, WorldWindowEngine, world_profile
from .world_corpus import DEFAULT_WORLD_CORPUS

#: The three arms, keyed by label. ``None`` params = no world engine at all
#: (arm A, the true control).
ARM_PARAMS: dict[str, Optional[WorldParams]] = {
    "A_no_world": None,
    "B_follow_edge": WORLD_FOLLOW_EDGE,
    "C_all": WORLD_ALL,
}
_ARM_ORDER = ("A_no_world", "B_follow_edge", "C_all")

# The world engine's rng is offset from the mind's (seed) and sleep's (seed+1000)
# streams so it never disturbs either. The question layer (v0.2 Step 3, ADR-0011)
# gets a third stream so its roll never perturbs the window-selection trajectory.
_WORLD_RNG_OFFSET = 2000
_QUESTION_RNG_OFFSET = 3000


# --------------------------------------------------------------------------- run

async def run_world_simulation(
    output_dir: Path | str,
    *,
    days: int = 14,
    seed: int = 0,
    user_script: list[UserTurn] | None = None,
    world_params: WorldParams | None = None,
    self_prob: float = DEFAULT_SELF_PROB,
    self_revival: bool = True,
    beat_minutes: int = 30,
    embeddings: str = "legacy",
    shadow: bool = False,
    questions: bool = False,
    continuity: bool = False,
) -> dict:
    """Drive the real engines (circadian + world) over a virtual ``days`` timeline.

    ``world_params=None`` runs the no-world control (arm A). Deterministic (seeded
    rng + virtual clock); a pre-existing ``world.sqlite3`` is removed first.
    v0.2 Step 2 (ADR-0010): ``embeddings="semantic"`` switches the treatment
    (real-vector retrieval + bridge strategies); ``shadow=True`` attaches the
    audit-only semantic shadow (legacy main only). Both default off — the
    sealed arms are untouched. Vectors cache under the output dir, so a re-run
    is deterministic without the network.
    v0.2 Step 3 (ADR-0011): ``questions=True`` enables the World→Question layer
    (default off — the sealed arms are byte-identical). The question roll runs
    on its own rng stream (seed + 3000), so the window-selection trajectory is
    the same as the questions-off arm's at the moment the layer turns on.
    v0.2 Step 4 (ADR-0012): ``continuity=True`` attaches the absence fact layer
    (absence.detected / reentry.candidate / reentry.selected|noop) at every
    user turn; default off — the sealed arms are untouched.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    db = out / "world.sqlite3"
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
                    self_revival=self_revival, revival=RevivalParams() if self_revival else None,
                    shadow_semantic=shadow_layer)
    sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(seed + 1000), self_prob=self_prob)
    reentry = ReentryEngine(store, memory=mem)
    machine = CircadianStateMachine(store)
    world_engine = None
    if world_params is not None:
        params = dataclasses.replace(world_params, enable_questions=True) if questions else world_params
        world_engine = WorldWindowEngine(
            store, memory=mem, corpus=DEFAULT_WORLD_CORPUS, params=params,
            rng=random.Random(seed + _WORLD_RNG_OFFSET),
            question_rng=random.Random(seed + _QUESTION_RNG_OFFSET),
        )
    orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry,
                                 machine=machine, world_engine=world_engine)
    continuity_engine = ContinuityEngine(store, memory=mem) if continuity else None
    tel = Telemetry(store, mem.index)

    threads: dict[str, str] = {}
    turns = sorted(
        ((SIM_START + timedelta(days=t.day - 1, hours=t.hour), t) for t in script),
        key=lambda x: x[0],
    )
    turn_idx = 0
    step = timedelta(minutes=beat_minutes)
    end = SIM_START + timedelta(days=days)
    now = SIM_START
    while now <= end:
        clock.set(now)
        while turn_idx < len(turns) and turns[turn_idx][0] <= now:
            u_evt = _inject_turn(store, turns[turn_idx][1], threads)
            if continuity_engine is not None and u_evt is not None:
                continuity_engine.on_user_return(u_evt)
            turn_idx += 1
        await orch.beat(now.isoformat())
        now += step

    arm = "A_no_world" if world_params is None else f"modes={tuple(world_params.enabled_modes)}"
    return _build_arm_report(store, mem, tel, machine, days, seed, arm, world_params,
                             questions, continuity)


def _build_arm_report(store, mem, tel, machine, days, seed, arm, world_params,
                      questions=False, continuity=False) -> dict:
    since = SIM_START.isoformat()
    latest = store.latest()
    now_iso = latest.created_at if latest else since
    win = tel.window_report(since, now_iso=now_iso, window=f"{days}d")
    prof = world_profile(store)
    n_world_threads = store.count("thread.created")
    n_world_threads_only = sum(1 for e in store.list(1000, type_prefix="thread.created")
                               if e.content.get("origin") == "world")
    # per-day exposure (to show 0/day days are legitimate, and the rhythm of looking)
    per_day: dict[str, int] = {}
    for e in store.list(5000, type_prefix="world.window_opened", order="asc"):
        d = e.created_at[:10]
        per_day[d] = per_day.get(d, 0) + 1
    return {
        "meta": {
            "arm": arm,
            "days": days,
            "seed": seed,
            "world_modes": tuple(world_params.enabled_modes) if world_params else (),
            "questions": bool(questions),
            "continuity": bool(continuity),
            "self_prob": DEFAULT_SELF_PROB,
            "self_revival": True,
            "n_wakes": store.count("wake.started"),
            "n_noops": store.count("wake.noop"),
            "n_thoughts": store.count("thought.created"),
            "n_world_thoughts": prof["n_world_thoughts"],
            "n_threads": n_world_threads,
            "n_world_threads": n_world_threads_only,
            "n_total_events": store.count(),
        },
        # mind-level (mechanism) readings — identical configuration across arms
        "route_entropy": win["route_entropy"],
        "no_op_ratio": win["no_op_ratio"],
        "origin_base": win["origin_base"],
        "self_origin_share": win["self_origin_share"],
        "user_derived_share": win["user_derived_share"],
        "user_recent_share": win["user_recent_share"],
        # the world window's own profile (all phase-brief metrics)
        "world": prof,
        "world_per_day": per_day,
        # v0.2 Step 4 (ADR-0012): the continuity fact layer (all-zero when off)
        "continuity": {
            "n_absences": store.count("absence.detected"),
            "n_candidates": store.count("reentry.candidate"),
            "n_selected": store.count("reentry.selected"),
            "n_noop": store.count("reentry.noop"),
        },
        "circadian_state_final": machine.state,
    }


# --------------------------------------------------------------------------- compare

def run_world_comparison(
    output_dir: Path | str,
    *,
    days: int = 14,
    seeds: list[int] | tuple[int, ...] = (0, 1, 2, 3),
    user_script: list[UserTurn] | None = None,
    arms: list[str] | tuple[str, ...] = _ARM_ORDER,
    questions: bool = False,
) -> dict:
    """Run all arms × seeds into fresh sibling dirs and build the A/B/C comparison.

    Same user stream + seed + circadian + self-thread mechanism across every arm;
    the only variable is the world window (A none / B follow+edge / C all four).
    ``questions=True`` enables the ADR-0011 question layer in every world arm.
    Returns a nested dict: ``arms[arm][seed]`` = that arm's report, plus a
    ``comparison`` table of the watch-metrics across arms.
    """
    out = Path(output_dir)
    script = user_script
    results: dict[str, dict[int, dict]] = {a: {} for a in arms}
    for seed in seeds:
        for arm in arms:
            params = ARM_PARAMS[arm]
            rep = asyncio.run(run_world_simulation(
                out / arm / f"seed{seed}",
                days=days, seed=seed, user_script=script, world_params=params,
                questions=questions,
            ))
            results[arm][seed] = rep

    return {
        "meta": {
            "days": days,
            "seeds": list(seeds),
            "user_turns": len(script) if script is not None else len(DEFAULT_USER_SCRIPT),
            "arms": list(arms),
            "questions": bool(questions),
            "arm_world_modes": {a: list(ARM_PARAMS[a].enabled_modes) if ARM_PARAMS[a] else [] for a in arms},
        },
        "arms": results,
        "comparison": _compare_world(results, arms, seeds),
    }


def _compare_world(results, arms, seeds) -> dict:
    """The honest watch-metrics across arms, per seed + mean. No ratio is tuned;
    these are the readings that tell us whether the world became a genuine third
    experience source (vs a news summariser / recommender)."""

    def _g(arm: str, seed: int, *path) -> object:
        rep = results[arm][seed]
        cur = rep
        for p in path:
            cur = cur[p]
        return cur

    def _mean(arm: str, path) -> float:
        vals = [float(_g(arm, s, *path)) for s in seeds]
        return round(sum(vals) / len(vals), 3) if vals else 0.0

    watch = (
        ("n_windows_opened", "world"),
        ("world_exposure_rate", "world"),
        ("world_to_thought_rate", "world"),
        ("seen_left_nothing_rate", "world"),
        ("n_world_thoughts", "world"),
        ("n_world_plus_memory_thoughts", "world"),
        ("cross_source_association_rate", "world"),
        ("alien_survival_rate", "world"),
        ("accident_max_chain_depth", "world"),
        ("source_concentration", "world"),
        ("topic_concentration", "world"),
        ("origin_shares__world_origin", None),   # special-cased below
        ("origin_shares__user_derived", None),
        ("origin_shares__self_origin", None),
    )
    table: dict[str, dict] = {}
    for key, _ in watch:
        per_arm: dict[str, dict] = {}
        for arm in arms:
            if key.startswith("origin_shares__"):
                sub = key.split("__")[1]
                per_seed = {s: float(_g(arm, s, "world", "origin_shares", sub)) for s in seeds}
                mean = round(sum(per_seed.values()) / len(seeds), 3) if seeds else 0.0
            else:
                per_seed = {s: float(_g(arm, s, "world", key)) for s in seeds}
                mean = round(sum(per_seed.values()) / len(seeds), 3) if seeds else 0.0
            per_arm[arm] = {"per_seed": per_seed, "mean": mean}
        table[key] = per_arm
    # entry-mode distribution is a dict-of-dicts, not a scalar; expose per-arm mean shape
    mode_table: dict[str, dict[str, float]] = {}
    for arm in arms:
        agg: dict[str, float] = {}
        for s in seeds:
            dist = _g(arm, s, "world", "entry_mode_distribution") or {}
            for m, c in dist.items():
                agg[m] = agg.get(m, 0) + float(c)
        mode_table[arm] = agg
    return {
        "watch_metrics": table,
        "entry_mode_distribution": mode_table,
        "note": ("No ratio tuned. A low world_to_thought_rate / high "
                 "seen_left_nothing_rate is a GOOD sign (reading != thought); high "
                 "source/topic concentration would be a FINDING (adhesion -> "
                 "summariser/recommender) and is reported, not tuned away."),
    }


# --------------------------------------------------------------------------- render

def comparison_markdown(combined: dict) -> str:
    m = combined["meta"]
    L: list[str] = []
    L.append("# Resident World-Window A/B/C life simulation (Phase 6)\n")
    L.append(f"Days: **{m['days']}** · seeds: **{m['seeds']}** · user turns: **{m['user_turns']}** · "
             f"arms: {', '.join(m['arms'])}\n")
    L.append(f"World modes per arm: " +
             "; ".join(f"**{a}** = {m['arm_world_modes'][a] or 'none'}" for a in m["arms"]) + "\n")
    L.append("Every arm runs the **same** user stream, seed, circadian orchestrator, and "
             "self-thread mechanism; the **only** variable is the World Window (A none / "
             "B follow+edge / C all four). No threshold was tuned toward a target ratio.\n")

    L.append("\n## Watch-metrics across arms (mean over seeds)\n")
    L.append("| metric | " + " | ".join(m["arms"]) + " |")
    L.append("|---" * (len(m["arms"]) + 1) + "|")
    cmp_ = combined["comparison"]
    for key, per_arm in cmp_["watch_metrics"].items():
        row = " | ".join(f"{per_arm[a]['mean']:.3f}" if a in per_arm else "—" for a in m["arms"])
        L.append(f"| {key} | {row} |")
    L.append("")

    L.append("\n## Entry-mode distribution (sum over seeds)\n")
    modes_all: list[str] = []
    for arm in m["arms"]:
        for k in cmp_["entry_mode_distribution"].get(arm, {}):
            if k not in modes_all:
                modes_all.append(k)
    L.append("| mode | " + " | ".join(m["arms"]) + " |")
    L.append("|---" * (len(m["arms"]) + 1) + "|")
    for mode in modes_all:
        row = " | ".join(f"{cmp_['entry_mode_distribution'].get(a, {}).get(mode, 0):.0f}"
                         for a in m["arms"])
        L.append(f"| {mode} | {row} |")
    L.append("")

    # Per-arm reading, honest incl. negatives
    L.append("\n## Reading the result\n")
    for arm in m["arms"]:
        a = combined["arms"][arm]
        if not a:
            continue
        rep = next(iter(a.values()))  # representative seed for the prose
        w = rep["world"]
        L.append(f"### {arm} (modes {m['arm_world_modes'][arm] or 'none'})")
        L.append(f"- windows opened (mean): "
                 f"{cmp_['watch_metrics']['n_windows_opened'][arm]['mean']:.2f}; "
                 f"exposure rate: {cmp_['watch_metrics']['world_exposure_rate'][arm]['mean']:.2f}/day "
                 f"(cap 8/day — 0/day is legal).")
        if m["arm_world_modes"][arm]:
            L.append(f"- **reading ≠ thought**: seen-left-nothing "
                     f"{cmp_['watch_metrics']['seen_left_nothing_rate'][arm]['mean']:.1%} "
                     f"(world→thought {cmp_['watch_metrics']['world_to_thought_rate'][arm]['mean']:.1%}).")
            L.append(f"- world + own old memory → thought: "
                     f"{cmp_['watch_metrics']['n_world_plus_memory_thoughts'][arm]['mean']:.1f}/run; "
                     f"cross-source association "
                     f"{cmp_['watch_metrics']['cross_source_association_rate'][arm]['mean']:.2f}.")
            L.append(f"- **adhesion check**: source concentration "
                     f"{cmp_['watch_metrics']['source_concentration'][arm]['mean']:.2f} / topic "
                     f"{cmp_['watch_metrics']['topic_concentration'][arm]['mean']:.2f} "
                     f"(low = spread, not a single-source reader; high = a finding).")
            L.append(f"- alien survival (revisited days later): "
                     f"{cmp_['watch_metrics']['alien_survival_rate'][arm]['mean']:.2f}; "
                     f"accident max chain depth "
                     f"{cmp_['watch_metrics']['accident_max_chain_depth'][arm]['mean']:.1f}.")
            L.append(f"- origin split: user-derived "
                     f"{cmp_['watch_metrics']['origin_shares__user_derived'][arm]['mean']:.2f} / "
                     f"self {cmp_['watch_metrics']['origin_shares__self_origin'][arm]['mean']:.2f} / "
                     f"world {cmp_['watch_metrics']['origin_shares__world_origin'][arm]['mean']:.2f}.")
        else:
            L.append("- No world machinery (control): 0 windows, 0 world-origin thoughts — "
                     "the baseline the other arms are measured against.")
    L.append("")
    return "\n".join(L) + "\n"


def main() -> None:
    import sys
    args = sys.argv[1:]
    out = Path(args[0]) if args and not args[0].startswith("--") else Path("out/world_sim")
    rest = args[1:] if (args and not args[0].startswith("--")) else args
    days, seeds = 14, (0, 1, 2, 3)
    i = 0
    while i < len(rest):
        if rest[i] == "--days":
            days = int(rest[i + 1]); i += 2
        elif rest[i] == "--seeds":
            seeds = tuple(int(x) for x in rest[i + 1].split(",")); i += 2
        else:
            i += 1
    combined = run_world_comparison(out, days=days, seeds=seeds)
    md = comparison_markdown(combined)
    (out / "COMPARISON.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"\n[written to {out / 'COMPARISON.md'}]")


if __name__ == "__main__":
    main()
