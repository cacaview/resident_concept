#!/usr/bin/env python
"""Phase 7 shadow audit (ADR-0016 §6): the in-mind H1 genesis detector run
SHADOW-first over the historical harness stores.

For every event store under scripts/out/{confirm28*,round4,...}, this script
computes ``resident.private_projects.genesis_candidates`` — the SAME structural
criteria the harness audit computes in ``h1_genesis_chains`` — independently,
in-mind, and reports how many projects WOULD have been created.

Honesty note (do not delete): **H1=0 in every harness run so far.** Every
proto-chain (20–27 day spans, 37 revisits, evidence entering self-thought
recall 195+ times) failed on exactly one criterion — source diversity — which
is a content property of the replay world, not a cognition gap. Therefore the
EXPECTED count from this audit is 0, and a clean run of 0s IS the shadow-first
verification: the in-mind detector and the harness audit detector agree that
no genesis has occurred. A nonzero count here would be a finding to
investigate against the harness report's ``h1_genesis_chains`` detail — never
a reason to loosen a threshold.

Read-only: the script never appends to any store.

Usage:
    python scripts/phase7_shadow_audit.py [OUT_DIR ...]   # default: scripts/out
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore
from resident.private_projects import genesis_candidates

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = BACKEND / "scripts" / "out"
STORE_NAME = "world.sqlite3"


def iter_run_stores(roots: list[Path]):
    """Every harness event store under the given roots, with its harness
    report (report.json) if one sits beside it."""
    for root in roots:
        if not root.exists():
            continue
        for db in sorted(root.rglob(STORE_NAME)):
            run_dir = db.parent
            report = run_dir / "report.json"
            yield db, (report if report.exists() else None)


def audit_store_full(db: Path) -> dict:
    store = EventStore(db)
    cands = genesis_candidates(store)
    self_thoughts = sum(
        1 for t in store.list(5000, type_prefix="thought.created")
        if (t.metadata or {}).get("origin") in ("self_thread", "private_project"))
    return {
        "n_questions": store.count("question.created"),
        "n_revisits": store.count("question.revisited"),
        "n_self_thoughts": self_thoughts,
        "h1_in_mind": len(cands),
        "detail": cands,
    }


def main() -> int:
    roots = [Path(a) if Path(a).is_absolute() else BACKEND / a
             for a in sys.argv[1:]] or [DEFAULT_ROOT]
    rows = []
    for db, report_path in iter_run_stores(roots):
        res = audit_store_full(db)
        harness_h1 = None
        if report_path is not None:
            try:
                harness_h1 = json.loads(report_path.read_text(encoding="utf-8")) \
                    .get("h1_genesis_chains", {}).get("count")
            except (json.JSONDecodeError, OSError):
                harness_h1 = None
        label = str(db.relative_to(roots[0]) if roots[0] != DEFAULT_ROOT
                    else db.relative_to(DEFAULT_ROOT))
        rows.append((label, res, harness_h1))

    print("Phase 7 shadow audit — in-mind H1 genesis detector over historical stores")
    print("(ADR-0016 §6; criteria identical to the harness h1_genesis_chains audit)")
    print()
    total = 0
    for label, res, harness_h1 in rows:
        total += res["h1_in_mind"]
        agree = "" if harness_h1 is None else (
            "  [harness agrees]" if harness_h1 == res["h1_in_mind"]
            else f"  !! HARNESS MISMATCH (harness={harness_h1})")
        print(f"{label}")
        print(f"  questions={res['n_questions']} revisits={res['n_revisits']} "
              f"self_thoughts={res['n_self_thoughts']} "
              f"would_have_genesis={res['h1_in_mind']}{agree}")
        for c in res["detail"]:
            print(f"    -> {c}")
    print()
    print(f"TOTAL would-have-genesis: {total} over {len(rows)} stores")
    print()
    print("Honesty note: H1=0 in every harness run so far (single-source replay")
    print("content, see docs/SELF_ORIGIN_REPORT.md §八-九), so the expected count")
    print("is 0. A clean run of 0s is the shadow-first verification that the")
    print("in-mind detector agrees with the harness audit detector. A nonzero")
    print("count is a finding to reconcile against the harness detail — never a")
    print("reason to tune a threshold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
