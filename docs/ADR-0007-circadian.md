# ADR-0007: Live circadian scheduler — an evidence-driven rhythm, and the revival opportunity it opens

Status: **Accepted** (with the honest findings in `SIMULATION_REPORT-circadian.md` kept front and centre)

> Phase 5. The follow-through to the Phase-4.1 sensitivity study
> (`ADR-0006` §addendum), which established the dormant self-thread revival's
> **working region** (dormancy 6–12 h, effective user silence ≈6 h, `MAX_ACTIVE`
> loose) and showed the *binding constraint* is not the gate's thresholds but the
> **structure of the lulls**: how many genuine, quiet, no-user wake slots actually
> exist. A fixed-interval scheduler tiles waking uniformly (no real night, no
> genuine quiet), so the gate — which needs a long quiet lull — barely opens. This
> ADR records the decision to replace the default scheduler with an
> **evidence-driven circadian rhythm** and to wire it so it *raises the
> opportunity* for a dormant self-thread to resurface — never forces it.

## Context

`ADR-0006` proved the revival mechanism is sound but **gated**: in a fixed-interval
14-day stream it had **1 eligible lull in 84 wakes**. The Phase-4.1 addendum
concluded that to make the mechanism matter, the *wake cadence* — the rhythm of
activity and silence — is the key variable, and deferred the live circadian to
*after* that study. This ADR is that decision, taken now.

### The constraint (verbatim, still in force)

> 不允许简单固定时钟 cron；禁止强制生成 thought；夜/长沉默**只允许**提高
> OPPORTUNITY（休眠自我线程候选进入 recall），**不允许**直接强制选择 self-thread，
> 也**不要**增大 route boost 作为主要策略；用户重现**不**清空当前自我线程 / mind 态。
> And, from the standing owner rules: 禁止硬配额；不要为了达到某个比例目标而调参数。

## Decisions

### 1. An evidence-driven `CircadianStateMachine` — a rhythm, not a cron

Six states: `ACTIVE` / `QUIET` / `DROWSY` / `SLEEP` / `CONSOLIDATING` / `WAKE`.
`decide()` is a **pure function of five signals + the current state**; the local
clock is **one bounded bias, never the decider**:

| signal | role |
|---|---|
| since user (external) activity | **drives the descent** (the mind sleeps when the *user* is gone) |
| since meaningful activity | a short **grace window** (a fresh self-thought holds `ACTIVE` <1 h) — not a stay-awake license |
| recent wake density | "tiredness" eases the thresholds (bounded) |
| active-thread state | **bounded resistance** to deep sleep (a busy mind sleeps *later*, never *never*) |
| local clock (night + morning window) | a **bias** (night eases sleep; morning holds the mind "up and about") |

Because every signal moves the outcome, the same clock at different evidence reads
a different state — this is what makes it *not* a fixed-clock cron (proved by
`test_decide_descends_through_all_awake_states_and_reopens` and the smoke's
"same silence, day vs night" cases). Every transition is the machine's sole write
of `circadian.state_changed` (`from`/`to` + `reasons` + the five `evidence`
signals), so the rhythm is provenance-complete and reproducible, and the state is
**reconstructed from the log** on restart (the log stays the source of truth —
ADR-0001).

### 2. Descent is driven by *user* silence, not the mind's own thoughts

The core modelling decision (and the fix that makes a churning mind able to rest):
the mind descends on **external** silence (`since_user_s`). A mind left alone by
the user **sleeps even while it keeps having private thoughts** — that is precisely
what sleep consolidates. Three supporting choices:
- a **fresh self-thought** is a short grace window (holds `ACTIVE` < 1 h), not a
  stay-awake license;
- **active-thread count** is a *bounded* resistance (it lengthens the sleep
  threshold by 0.25 per thread over the baseline, never a permanent veto) — a
  busy mind sleeps later, not never;
- **sleep is stable**: the mind's own consolidation/dream does **not** wake it
  (the wake trigger is *external-only* — a user message / experience — or the
  morning window). This is what makes a night *stick*.

### 3. `CircadianOrchestrator` drives the real engines; a beat *samples*, it does not *decide*

The orchestrator calls the **real** `MindLoop` / `SleepEngine` / `ReentryEngine`
on a cadence (`CircadianScheduler` on the wall clock live; a virtual 30-min beat
in the sim). What a beat *does* — wake / consolidate / nothing — is decided by the
state machine, not by "tick every N seconds and do X". In `SLEEP` the real
`SleepEngine` runs and **may legitimately do nothing** (no forced thought — ADR-0004,
`test_sleep_can_do_nothing`). In awake states it wakes on a per-state cadence and
**always promptly on a new user message** (the activity bursts). On `WAKE` the mind
re-opens and its **threads and mental state are not cleared** — a returning
conversation is new *input*, not a reset (`test_reappearance_interrupts_sleep_preserving_threads`).

### 4. The circadian raises the revival OPPORTUNITY only — it never forces a selection

A recognised quiet state (`QUIET` / `DROWSY` / `SLEEP`) is passed to the mind loop
as `in_quiet_state`, which drops the revival gate's **user-silence bar from 12 h to
the ~6 h working region** (`QUIET_GATE_SILENCE_S`). That is the *only* effect:
- it does **not** change which thread is picked — selection stays the bounded
  `REVIVAL_STRENGTH = 0.35` probabilistic preference + the bounded `route_lift`,
  both **independent of circadian state** (`test_circadian_raises_opportunity_not_force`);
- it does **not** enlarge the route lift, and the lift is *not* the primary lever
  (Phase-4.1: the candidate mechanism + natural continuity after revival are).
So night / long-silence makes a dormant self-thread **more likely to enter recall**,
never **guaranteed to be chosen**.

### 5. `/api/telemetry` — a windowed, store-consistent read

`/api/telemetry?window=24h|7d|30d` returns `Telemetry.window_report` over
`[now-window, now]`: every value is **derived from events in the window** (ISO
`created_at` compares as a string), so the report is provenance-complete and
reproducible from the log (`test_window_report_consistent_with_store`,
`test_telemetry_endpoint_windows_and_consistency`). It exposes the circadian state
(a live read, not an aggregate), the silences, wake/no-op/sleep counts, route +
topic entropy, origin shares, and the revival metrics (`eligible_wakes`,
`revival_engaged`, `route_changed_by_bias` — the counterfactual "revisit won
*because of* the lift", `revival_delta`, `revival_leverage`) plus the
self-dynamics reads.

### 6. The live scheduler is the default; the fixed interval is kept as a baseline

`CircadianScheduler` (asyncio, wall clock, with a little jitter) is the default in
`build_app`; the `WakeScheduler` fixed-interval loop is retained for comparison /
fallback (and is what the sim's `fixed` arm reproduces). World browsing remains
**off** (ADR-0005, still deferred).

### 7. The deliverable: a deterministic circadian-vs-fixed sim

`resident.circadian_sim` runs **both arms on the same user stream + seed +
self-mechanism settings, differing only in scheduler**, over ≥7 virtual days, into
fresh directories, byte-reproducible. It isolates the scheduler and characterises
the revival opportunity, wake/no-op ratio, route entropy, self-origin /
user-derived, and **activity clustering** (inter-wake-gap CV + hour-of-day profile).

## Consequences — the honest findings

The mechanism is implemented exactly as decided. The 14-day, seed-0 result
(`SIMULATION_REPORT-circadian.md`) — reported as a **characterisation, not a tuned
win**:

1. **The opportunity rose**: eligible frequency **0.036 → 0.210** (~5.8×);
   `revival_engaged` 1 → 6. The recognised quiet states dropped the gate's silence
   bar, so dormant self-threads enter recall more often — `route_changed_by_bias`
   only 3 → 6 (still far below eligible), confirming the lift is not the lever.
2. **Self-origin rose +0.047** (0.267 → 0.314); **user-derived fell to 0.569** —
   the downstream of more genuine lulls, not a hard push.
3. **The rhythm is natural, not tiled**: intra-day wake-gap CV **0.000 → 0.939**;
   a real **quiet night** (00–05 h: 0 wakes) and a **morning burst** (06–08 h).
   no-ops remain a reachable outcome (17.7%); route entropy stays healthy (0.950).

**Caveats, preserved:**
- The `fixed` arm's "night wake share 0.333" is a **shared offset artifact** — the
  `06:00` `SIM_START` base is *added to* the wake-hour offsets, so two of its six
  daily slots land at 00:00/03:00. Both arms inherit the same convention; the
  circadian arm's **0.0** is the real signal. Not a bug introduced here; preserved
  so the record doesn't hide it.
- This is a **single-seed (0) characterisation**. Cross-seed robustness of the
  circadian *benefit* is an open measurement (Phase-4.1's multi-seed method), not
  yet run here. The causal reading of self-origin growth stays bounded by
  `ADR-0006` (single-trigger cascade vs repeated revival must be checked per-seed).

### Test coverage (`backend/tests/test_circadian.py`, 10 tests)

| scenario | test |
|---|---|
| (a) active→quiet→drowsy→sleep→wake | `test_decide_descends...` (decision level) + `test_daynight_reaches_all_states_and_reopens` (orchestrator timeline) |
| every change recorded (`from`/`to`+evidence) | `test_state_change_records_from_to_and_evidence` |
| restart persists the state | `test_machine_reconstructs_state_from_log` |
| (b) user re-appearance interrupts sleep, threads preserved | `test_reappearance_interrupts_sleep_preserving_threads` |
| (c) sleep may do nothing (no forced thought) | `test_sleep_can_do_nothing` |
| (d) opportunity raised, selection never forced | `test_circadian_raises_opportunity_not_force` |
| (e) telemetry consistent with the store | `test_window_report_consistent_with_store` + `test_telemetry_endpoint_windows_and_consistency` |
| sim deterministic + clusters (not uniform tiling) | `test_circadian_vs_fixed_sim_deterministic_and_clusters` |

Full suite: **259 passed, 0 failed.**

### Cross-seed addendum (Phase 5.1, 2026-09-11 — sealing validation)

The single-seed (0) finding above was a *characterisation*; `scripts/circadian_cross_seed.py`
then swept **seed 0–15** (16 seeds, 14 days, both arms, same stream + self-mechanism,
**only the scheduler differs**, **no parameter tuned**) to confirm the effect is stable across
seeds. **Confirmed — the three structural claims hold in every seed:** eligible frequency
rises 16/16 (mean 0.057 → 0.322, ~5.7×); natural clustering appears 16/16 (intra-day gap CV
0.90–0.94 vs fixed 0.000; night wake share ≈0.004 = a real quiet night); route entropy never
collapses (0.869–0.967). **Preserved caveat:** the *self-origin share* gain is a net positive
(mean **+0.073**, 13/16 seeds) but **seed-conditional** — 3/16 seeds (6/8/9) show a small net
*reduction* (−0.130/−0.119/−0.024; seed 6 byte-reproducible at −0.1296) from the known
**double-edged bounded route lift** (Phase 4.1): more lulls → the `revisit` lift diverts
would-be self-origin thoughts into revisiting user material (self→user). Seed 8 is the
clearest (largest eligible jump 0.024→0.435 + highest engagement 1→10, yet *still* negative) —
"more opportunity/engagement" is **not a monotonic** guarantee of "more self-origin". **Phase 5
is sealed** on its core, seed-invariant claims with this caveat; the causal reading of
self-origin growth stays bounded by ADR-0006, now confirmed across 16 seeds.

### What is NOT done / open

- **Cross-seed robustness** — **DONE (Phase 5.1, 2026-09-11)**: 16-seed sweep confirms the
  structural claims are 16/16 stable; self-origin share is net-positive but seed-conditional
  (see the cross-seed addendum above).
- **World browsing** (ADR-0005) remains the largest missing *material* source for
  pushing `user_derived` below ~0.85 — orthogonal to this rhythm result, still deferred.
- Self-modification remains out of scope; external side effects stay sandboxed.
