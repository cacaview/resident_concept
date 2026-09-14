#!/usr/bin/env python3
"""H1 recompute (2026-09-13): replay the FIXED h1_genesis_chains detector
over existing world.sqlite3 stores — no simulation is re-run.

Background: out/confirm28_multisrc/H1_FORENSICS.md §5 found (a) the evidence-
source criterion read only the question's birth-window metadata (structurally
single-source) while the docstring defines the chain as "re-struck from >=2
distinct sources across >=7 days", and (b) a stale loop variable `t` leaking
into criterion (d) so every question was tested against the log's LAST
thought. The fix in active_living_harness.build_report aligns measurement
with the documented definition. This script recomputes the formal verdict for
all seeds of confirm28_multisrc / confirm28 / confirm28_enriched2 /
confirm28_enriched_s457 and writes h1_recompute.json per experiment dir
(past reports are left untouched).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from active_living_harness import build_report

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from resident.event_store import EventStore

OUT = Path(__file__).resolve().parent / "out"
EXPERIMENTS = [
    "confirm28_multisrc",
    "confirm28",
    "confirm28_enriched2",
    "confirm28_enriched_s457",
]
FORMAL = ("h1 FORMAL cross-source confirm (>=2 distinct sources, >=7d span, "
          "ends in self-origin thought, provenance independent of conversation)")


def recompute(exp: str) -> dict:
    exp_dir = OUT / exp
    comp = json.loads((exp_dir / "comparison.json").read_text())
    days = comp.get("days", 28)
    results = []
    for db in sorted(exp_dir.glob("runs/active-v1/*/seed*/world.sqlite3")):
        seed_dir = db.parent
        fp = seed_dir.parent.name
        seed = seed_dir.name
        store = EventStore(db)
        report = build_report(store, arm="active-v1", cfg={}, fp=fp,
                              days=days, run_dir=seed_dir)
        h1 = report["h1_genesis_chains"]
        results.append({
            "fingerprint": fp,
            "seed": seed,
            "days": days,
            "h1_count": h1["count"],
            "chains": h1["detail"],
            "formal_confirm": "PASSED" if h1["count"] >= 1 else "NOT_PASSED",
        })
        print(f"{exp} {fp}/{seed}: h1={h1['count']}")
    out = {"experiment": exp, "detector": "fixed 2026-09-13 (see "
           "out/confirm28_multisrc/DETECTOR_FIX_NOTE.md)",
           "criteria": ">=2 revisits, >=2 distinct sources (birth evidence "
           "∪ revisit caused_by world.observation), span >=7d, self-origin "
           "thought citing question material, no conversation evidence, "
           "revisits caused_by world.observation",
           "seeds": results}
    (exp_dir / "h1_recompute.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    all_out = [recompute(e) for e in EXPERIMENTS]
    print("\n==== FORMAL VERDICT SUMMARY ====")
    for exp_out in all_out:
        for s in exp_out["seeds"]:
            chains = s["chains"]
            print(f"{exp_out['experiment']} {s['fingerprint']}/{s['seed']}: "
                  f"h1={s['h1_count']} {FORMAL}={s['formal_confirm']}")
            for c in chains:
                print(f"    Q {c['question_id']} [{c['topic']}] "
                      f"sources={c['sources']} span={c['span_days']}d "
                      f"revisits={c['revisits']} citing={c['citing_thoughts']}")


if __name__ == "__main__":
    main()
