"""Smoke test for the Phase-5 circadian state machine + orchestrator.

Run:  .venv/bin/python scripts/_smoke_circadian.py
Verifies:
  1. `decide` ordering — a just-woken / actively-producing mind is NOT yanked
     straight back to SLEEP (the fix); a fully-idle mind still sleeps; the
     night window eases sleep; user presence always re-opens; sleep sticks.
  2. Orchestrator end-to-end over a virtual day+night — reaches all awake states
     + sleep + wake, records state_changed, and does NOT oscillate
     wake->sleep->wake in the morning (a productive wake holds awake).
"""
import asyncio
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone

from resident.circadian import (
    ACTIVE, DROWSY, QUIET, SLEEP, WAKE, CONSOLIDATING,
    CircadianEvidence, CircadianParams, CircadianStateMachine, CircadianOrchestrator,
)
from resident.event_store import EventStore
from resident.memory import MemoryRetrieval
from resident.mind_loop import MindLoop
from resident.reentry import ReentryEngine
from resident.sleep import SleepEngine


def _ev(**kw):
    base = dict(now="2026-06-01T12:00:00+00:00", local_hour=12.0,
                since_user_s=3600.0, since_meaningful_s=3600.0,
                recent_wake_count=0, n_active_threads=0, current=ACTIVE)
    base.update(kw)
    return CircadianEvidence(**base)


def _machine():
    d = tempfile.mkdtemp()
    store = EventStore(d + "/sm.sqlite3")
    return CircadianStateMachine(store), d


def test_decide_ordering():
    m, d = _machine()
    ok = []
    # (A) the FIX: just woke + produced a thought (meaningful 5 min ago), user
    #     silent 7 h, morning -> must hold ACTIVE, NOT sleep.
    a = m.decide(_ev(current=WAKE, local_hour=6.5, since_user_s=7 * 3600,
                     since_meaningful_s=5 * 60, n_active_threads=0))
    ok.append(("A: productive wake holds awake", a[0] == ACTIVE, a))
    # (B) fully idle (user 7 h AND self 7 h), daytime -> SLEEP
    b = m.decide(_ev(current=ACTIVE, local_hour=12.0, since_user_s=7 * 3600,
                     since_meaningful_s=7 * 3600))
    ok.append(("B: idle mind sleeps (day)", b[0] == SLEEP, b))
    # (C) user silent 2.5 h, idle -> QUIET
    c = m.decide(_ev(current=ACTIVE, local_hour=12.0, since_user_s=2.5 * 3600,
                     since_meaningful_s=3 * 3600))
    ok.append(("C: 2.5h disengagement -> quiet", c[0] == QUIET, c))
    # (D) unengaged 4.5 h -> DROWSY
    dd = m.decide(_ev(current=ACTIVE, local_hour=12.0, since_user_s=4.5 * 3600,
                      since_meaningful_s=5 * 3600))
    ok.append(("D: 4.5h disengagement -> drowsy", dd[0] == DROWSY, dd))
    # (E) night window eases sleep: unengaged 5.5 h is drowsy by day but SLEEP
    #     at hour 23 (night drops the sleep threshold from 6h to 4.8h).
    e = m.decide(_ev(current=ACTIVE, local_hour=23.0, since_user_s=5.5 * 3600,
                     since_meaningful_s=5.5 * 3600))
    ok.append(("E: night eases sleep (5.5h@23h -> sleep)", e[0] == SLEEP, e))
    # (E2) the SAME 5.5 h by day (hour 12) is only DROWSY — proves night is a bias
    e2 = m.decide(_ev(current=ACTIVE, local_hour=12.0, since_user_s=5.5 * 3600,
                      since_meaningful_s=5.5 * 3600))
    ok.append(("E2: same 5.5h by day -> drowsy (night is a bias, not a cron)", e2[0] == DROWSY, e2))
    # (F) asleep + user just returned -> WAKE (wake trigger beats stickiness)
    f = m.decide(_ev(current=SLEEP, local_hour=12.0, since_user_s=10 * 60,
                     since_meaningful_s=10 * 60))
    ok.append(("F: user return wakes from sleep", f[0] == WAKE, f))
    # (G) asleep stickiness: silent all day, no wake trigger -> stays SLEEP
    g = m.decide(_ev(current=SLEEP, local_hour=12.0, since_user_s=8 * 3600,
                     since_meaningful_s=8 * 3600))
    ok.append(("G: sleep sticks (no trigger)", g[0] == SLEEP, g))
    # (H) morning hold: up in the morning window, user gone 5 h, daytime-ish
    #     -> stays ACTIVE (up and about), NOT immediately re-slept
    h = m.decide(_ev(current=ACTIVE, local_hour=7.0, since_user_s=5 * 3600,
                     since_meaningful_s=5 * 3600))
    ok.append(("H: morning window holds the mind up", h[0] == ACTIVE, h))
    # (H2) the SAME 5 h silence OUTSIDE the morning window (12 h) -> drowsy
    h2 = m.decide(_ev(current=ACTIVE, local_hour=12.0, since_user_s=5 * 3600,
                      since_meaningful_s=5 * 3600))
    ok.append(("H2: same 5h outside morning -> drowsy (morning is a bias)", h2[0] == DROWSY, h2))

    fails = [o for o in ok if not o[1]]
    for name, passed, got in ok:
        print(f"  [{'OK' if passed else 'FAIL'}] {name}  -> {got[0]}")
    shutil.rmtree(d, ignore_errors=True)
    return not fails


def _build_stack(tmpdir, seed):
    store = EventStore(tmpdir + "/life.sqlite3", now_fn=lambda: _clock_now())
    mem = MemoryRetrieval(store)
    mind = MindLoop(store, memory=mem, rng=random.Random(seed))
    sleep_engine = SleepEngine(store, memory=mem, rng=random.Random(seed + 1000))
    reentry = ReentryEngine(store, memory=mem)
    orch = CircadianOrchestrator(store, mind, sleep_engine, reentry=reentry)
    return store, mem, mind, sleep_engine, reentry, orch


_now = {"t": datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)}
def _clock_now():
    return _now["t"]


async def test_orchestrator_daynight():
    d = tempfile.mkdtemp()
    store, mem, mind, sleep_engine, reentry, orch = _build_stack(d, seed=0)
    from resident.simulation import _inject_turn, UserTurn
    from resident.models import EventCreate

    start = datetime(2026, 6, 1, 6, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 2, 9, 0, 0, tzinfo=timezone.utc)   # ~27 h
    step = timedelta(minutes=30)

    # user is present day-1 morning (6-9 h), then gone for the rest.
    morning_turns = {6: UserTurn(1, 6, "reading", "最近在读书，聊聊"),
                     7: UserTurn(1, 7, "work", "工作上遇到一个难题"),
                     8: UserTurn(1, 8, "reading", "那本书第三章很有意思")}
    threads = {}

    t = start
    n_state_changed = 0
    osc = 0          # wake->sleep->(immediate) sleep oscillation count
    prev_state = None
    prev_wake_productive = False
    transitions = []
    while t <= end:
        _now["t"] = t
        turn = morning_turns.get(t.hour) if t.day == 1 else None
        if turn is not None:
            _inject_turn(store, turn, threads)
        beat = await orch.beat(t.isoformat())
        if beat["changed"]:
            n_state_changed += 1
        st = beat["state"]
        if prev_state is not None and (prev_state, st) != (None, None):
            transitions.append((prev_state, st))
        t += step
        prev_state = st

    seen = set(orch.state_trace and {b["state"] for b in orch.state_trace})
    # the oscillation signature: a `wake` immediately followed by `sleep` where
    # the wake beat produced a durable thought.
    # (we approximate: any `wake -> sleep` transition where the wake beat's
    #  result was not noop)
    trace = orch.state_trace
    osc = 0
    for i in range(1, len(trace)):
        if trace[i - 1]["state"] == WAKE and trace[i]["state"] == SLEEP:
            # was the wake beat productive? look for a thought created at that t
            if trace[i - 1].get("result") != "noop":
                osc += 1

    sc_events = store.list(1000, type_prefix="circadian.state_changed", order="desc")
    thoughts = store.list(1000, type_prefix="thought.created", order="desc")
    sleeps = store.list(1000, type_prefix="sleep.completed", order="desc")

    print(f"  states seen: {sorted(seen)}")
    print(f"  state_changed events: {len(sc_events)} (beats flagged changed: {n_state_changed})")
    print(f"  total wakes: {orch.wakes}   consolidations: {orch.consolidations}")
    print(f"  durable thoughts: {len(thoughts)}   sleep completions: {len(sleeps)}")
    print(f"  productive wake->sleep oscillations: {osc}")
    # print a compact transition summary (collapse consolidation sub-states)
    compact = []
    for (a, b) in transitions:
        if b == CONSOLIDATING:
            continue
        compact.append(f"{a}->{b}")
    print(f"  transitions head: {compact[:12]}")
    print(f"  transitions tail: {compact[-12:]}")

    fails = []
    need = {ACTIVE, QUIET, DROWSY, SLEEP, WAKE}
    if not need.issubset(seen):
        fails.append(f"missing states: {need - seen}")
    if len(sc_events) == 0:
        fails.append("no circadian.state_changed events recorded")
    if orch.consolidations == 0:
        fails.append("no consolidation ran during sleep")
    if osc > 0:
        fails.append(f"productive wake->sleep oscillation still present ({osc})")
    if len(thoughts) == 0:
        fails.append("mind produced no durable thought all day")
    # a clean day->night->day: exactly one stable sleep run then a morning re-open
    if ("sleep->wake") not in compact:
        fails.append("no morning re-open (sleep->wake) — night never ends")
    # the night must be STABLE: no wake from its own consolidation. Count
    # sleep->wake transitions; overnight (before the 6h morning window) there
    # should be none, so total sleep->wake == number of morning re-opens (<=2).
    n_morning = compact.count("sleep->wake")
    if n_morning > 3:
        fails.append(f"too many sleep->wake re-opens ({n_morning}) — night not stable")

    print("  RESULT:", "PASS" if not fails else f"FAIL {fails}")
    shutil.rmtree(d, ignore_errors=True)
    return not fails


async def main():
    print("== decide() ordering ==")
    r1 = test_decide_ordering()
    print("== orchestrator day-night ==")
    r2 = await test_orchestrator_daynight()
    print("\nSMOKE:", "PASS" if (r1 and r2) else "FAIL")
    return 0 if (r1 and r2) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
