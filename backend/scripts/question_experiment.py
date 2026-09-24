#!/usr/bin/env python3
"""v0.2 Step 3 (ADR-0011) experiment: World → Question, off vs on.

Runs the C_all world arm (all four entry modes), 4 seeds × 14 days, once with
the question layer OFF (control — the sealed behaviour) and once ON. The
question roll runs on its own rng stream, so the on-arm's window-selection
trajectory starts identical; the log itself diverges (question events are real
mental content), which is exactly the measured effect.

Readings (characterisation, NOT tuning — every negative is kept):
- rarity: questions per open; the share of opens leaving nothing at all;
- life: dormancy at end-of-run (questions die — derived, no events);
- re-meeting: revisits and survival (re-touched, never "answered");
- mind: route entropy, no-op ratio, thought origins, world thought rates,
  seen_left_nothing — deltas between arms reported as they come out.
"""
import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore  # noqa: E402
from resident.world import WORLD_ALL, question_state  # noqa: E402
from resident.world_sim import run_world_simulation  # noqa: E402

OUT = Path(__file__).resolve().parent / "out" / "question_experiment"
SEEDS = (0, 1, 2, 3)
DAYS = 14


def run_cell(task):
    arm_on, seed = task
    out = OUT / ("on" if arm_on else "off") / f"seed{seed}"
    rep = asyncio.run(run_world_simulation(
        out, days=DAYS, seed=seed, world_params=WORLD_ALL, questions=arm_on,
    ))
    (out / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8")
    return str(out)


def pull_questions(db: Path) -> list[dict]:
    """Qualitative readout: every question of that life, with its derived state."""
    store = EventStore(db)
    state = question_state(store)
    return [
        {k: row[k] for k in ("question_id", "kind", "topic", "text",
                             "created_at", "revisits", "status")}
        for row in state["questions"]
    ]


def _mean(rows, *path):
    vals = []
    for r in rows:
        cur = r
        for p in path:
            cur = cur[p]
        vals.append(float(cur))
    return round(sum(vals) / len(vals), 3) if vals else 0.0


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = [(on, seed) for on in (False, True) for seed in SEEDS]
    with ProcessPoolExecutor(max_workers=4) as ex:
        for _ in ex.map(run_cell, tasks):
            pass

    arms: dict[str, list[dict]] = {"off": [], "on": []}
    for on in (False, True):
        key = "on" if on else "off"
        for seed in SEEDS:
            d = OUT / key / f"seed{seed}"
            arms[key].append(json.loads((d / "report.json").read_text(encoding="utf-8")))

    # per-run question readouts (from the persisted event stores)
    questions_by_seed = {
        seed: pull_questions(OUT / "on" / f"seed{seed}" / "world.sqlite3")
        for seed in SEEDS
    }

    metrics = (
        ("n_wakes", ("meta", "n_wakes")),
        ("no_op_ratio", ("no_op_ratio",)),
        ("route_entropy", ("route_entropy",)),
        ("user_derived_share", ("user_derived_share",)),
        ("self_origin_share", ("self_origin_share",)),
        ("n_thoughts", ("meta", "n_thoughts")),
        ("n_world_thoughts", ("meta", "n_world_thoughts")),
        ("n_windows_opened", ("world", "n_windows_opened")),
        ("world_to_thought_rate", ("world", "world_to_thought_rate")),
        ("seen_left_nothing_rate", ("world", "seen_left_nothing_rate")),
        ("world_to_question_rate", ("world", "world_to_question_rate")),
        ("n_questions", ("world", "questions", "n_questions")),
        ("n_question_revisits", ("world", "questions", "n_revisits")),
        ("question_survival_rate", ("world", "questions", "question_survival_rate")),
        ("questions_dormant_now", ("world", "questions", "dormant_now")),
    )
    table = {}
    for label, path in metrics:
        table[label] = {"off": _mean(arms["off"], *path), "on": _mean(arms["on"], *path)}

    n_q_total = sum(len(v) for v in questions_by_seed.values())
    # each row's "revisits" counts that question's question.revisited events
    n_revisits_total = sum(q["revisits"] for v in questions_by_seed.values() for q in v)
    result = {
        "meta": {"days": DAYS, "seeds": list(SEEDS), "arm": "C_all",
                 "note": "characterisation, not tuned; every negative kept"},
        "metrics_off_vs_on": table,
        "questions_by_seed": questions_by_seed,
        "totals": {
            "questions_all_seeds": n_q_total,
            "revisit_rows_all_seeds": n_revisits_total,
        },
    }
    out_json = Path(__file__).resolve().parent / "out" / "question_experiment_results.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"{'metric':<28}{'off':>10}{'on':>10}")
    for label, path in metrics:
        o = table[label]["off"]
        n = table[label]["on"]
        print(f"{label:<28}{o:>10}{n:>10}")
    print(f"\nquestions across all seeds: {n_q_total}")
    for seed, qs in questions_by_seed.items():
        for q in qs:
            print(f"  seed{seed} [{q['kind']}] rev={q['revisits']} {q['status']}: "
                  f"{q['text'][:72]}")
    print(f"\n[written to {out_json}]")


if __name__ == "__main__":
    main()
