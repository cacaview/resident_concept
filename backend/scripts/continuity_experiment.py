#!/usr/bin/env python3
"""v0.2 Step 4 (ADR-0012) experiment: the SAME absence, four different returns.

Same seed, same user stream, same internal life (same gap window) — the ONLY
variable is the first thing the user says on return:
    "我回来了" / "刚刚那个问题继续" / "今天好累" / "你这段时间有想到什么吗？"

Readings (characterisation, not tuning; every negative kept):
- validity gate: everything before the return message must be identical across
  the four message arms of one (gap, seed) cell;
- the absence facts (gap_s) and the eligibility set (candidates) per gap;
- the decision matrix: does the re-entry layer select a DIFFERENT past per
  message — or is it a mechanical "offline daily report"?
"""
import asyncio
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from resident.event_store import EventStore  # noqa: E402
from resident.simulation import UserTurn  # noqa: E402
from resident.world import WORLD_ALL  # noqa: E402
from resident.world_sim import run_world_simulation  # noqa: E402

OUT = Path(__file__).resolve().parent / "out" / "continuity_experiment"
BASE = [
    UserTurn(1, 6, "pasta", "周末想试试做意面，有什么简单的酱吗？"),
    UserTurn(1, 15, "work", "下周有个项目 deadline，压力有点大。"),
    UserTurn(2, 9, "pasta", "意面做成功了，不过总觉得面团有点不对劲。"),
]
RETURNS = {6: (2, 15), 12: (2, 21), 24: (3, 9)}  # gap hours -> (day, hour)
MESSAGES = ("我回来了", "刚刚那个问题继续", "今天好累", "你这段时间有想到什么吗？")
SEEDS = (0, 1)
DAYS = 4
GAPS = (6, 12, 24)


def run_cell(task):
    gap_h, msg_idx, seed = task
    day, hour = RETURNS[gap_h]
    script = BASE + [UserTurn(day, hour, "return", MESSAGES[msg_idx])]
    out = OUT / f"gap{gap_h}h" / f"seed{seed}" / f"msg{msg_idx}"
    rep = asyncio.run(run_world_simulation(
        out, days=DAYS, seed=seed, user_script=script,
        world_params=WORLD_ALL, questions=True, continuity=True))
    (out / "report.json").write_text(
        json.dumps(rep, ensure_ascii=False, sort_keys=True, default=str),
        encoding="utf-8")
    return str(out)


def read_cell(db: Path) -> dict:
    store = EventStore(db)
    absence = store.list(5, type_prefix="absence.detected", order="desc")
    if not absence:
        return {"absence": None}
    a = absence[0]
    until_seq = None
    for e in store.list(5, type_prefix="conversation.user_message", order="desc"):
        if e.id == a.content.get("until_event_id"):
            until_seq = e.seq
    cands = [{"cls": e.content.get("cls"), "quote": e.content.get("quote")}
             for e in store.list(50, type_prefix="reentry.candidate")
             if e.content.get("gap_s") == a.content.get("gap_s")]
    sel = store.list(5, type_prefix="reentry.selected", order="desc")
    decision = "selected" if sel else ("noop" if store.count("reentry.noop") else None)
    classes = sorted(sel[0].content.get("classes") or []) if sel else []
    return {"absence": {"gap_s": a.content.get("gap_s")}, "candidates": cands,
            "decision": decision, "selected_classes": classes,
            "until_seq": until_seq}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    tasks = [(g, m, s) for g in GAPS for m in range(len(MESSAGES)) for s in SEEDS]
    with ProcessPoolExecutor(max_workers=4) as ex:
        for _ in ex.map(run_cell, tasks):
            pass

    result = {"meta": {"gaps": list(GAPS), "messages": list(MESSAGES),
                       "seeds": list(SEEDS), "days": DAYS,
                       "questions": True, "continuity": True},
              "cells": {}}
    print(f"{'gap':>5} {'seed':>5} {'message':<22}{'decision':<10} selected")
    for g in GAPS:
        for s in SEEDS:
            cells = [read_cell(OUT / f"gap{g}h" / f"seed{s}" / f"msg{m}" / "world.sqlite3")
                     for m in range(len(MESSAGES))]
            # validity gate: identical life before the return message
            projs = []
            for c in cells:
                store = EventStore(OUT / f"gap{g}h" / f"seed{s}" / "msg0" / "world.sqlite3")
                projs.append(c.get("until_seq"))
            pre = []
            import re

            uid = re.compile(r"[a-z]+_[0-9a-f]{6,}")  # run-level random labels

            def scrub(v):
                if isinstance(v, str):
                    return uid.sub("<id>", v)
                if isinstance(v, dict):
                    return {k: scrub(x) for k, x in v.items()}
                if isinstance(v, (list, tuple)):
                    return [scrub(x) for x in v]
                return v

            for m in range(len(MESSAGES)):
                st = EventStore(OUT / f"gap{g}h" / f"seed{s}" / f"msg{m}" / "world.sqlite3")
                pre.append([(e.type, scrub(e.content)) for e in st.list(4000, order="asc")
                            if (e.seq or 0) < (cells[m]["until_seq"] or 10**9)])
            identical = all(p == pre[0] for p in pre)
            n_cands = len(cells[0]["candidates"] or [])
            result["cells"][f"gap{g}h/seed{s}"] = {
                "pre_return_identical": identical,
                "gap_s": cells[0]["absence"]["gap_s"] if cells[0]["absence"] else None,
                "candidates": cells[0]["candidates"],
                "arms": [{"message": MESSAGES[m], "decision": cells[m]["decision"],
                          "selected_classes": cells[m]["selected_classes"]}
                         for m in range(len(MESSAGES))],
            }
            print(f"  candidates in gap: {n_cands} | pre-return identical: {identical}")
            for m in range(len(MESSAGES)):
                c = cells[m]
                sel = ",".join(c["selected_classes"]) or "-"
                print(f"{g:>5} {s:>5} {MESSAGES[m]:<22}{str(c['decision']):<10} {sel}")
            print()

    out_json = Path(__file__).resolve().parent / "out" / "continuity_experiment_results.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[written to {out_json}]")


if __name__ == "__main__":
    main()
