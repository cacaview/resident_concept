# RELEASE NOTES — v1.0 — **DRAFT (NOT a release claim)**

**Status: DRAFT — NOT A RELEASE.** Written 2026-09-13; **updated 2026-09-14.**
**v1.0 is NOT releasable today.** Everything below describes what a v1.0
release *would* contain if and when the remaining acceptance items are met.
The gaps are **live-world evidence**, which now requires only real days to
pass (baseline Day 7 = 2026-09-19): the qwen relay was **recovered**
2026-09-14 (switched to `<debian-relay>:8002`; old host `<legacy-host>` died),
and a **parallel live fleet** of 3 active-v2 instances (world seeds 1/2/3)
accelerates the plurality / H1 / genesis evidence without compressing the
calendar. No further mind-code surgery is on the critical path.

---

## What v1.0 would contain

1. **`active-v2` profile (ADR-0017).** active-v1 rhythm + `recall_pool_hygiene`
   (E2) + `thread_selection: material` + `context_exclude_prev_thread` (VA).
   Battery: sealed regression **byte-identical** across all 12 cells (3 arms ×
   4 seeds × 14d), full suite **390 passed**, meta_recall 0.0000, no failure
   flags per seed. Evidence: `docs/ACTIVE_V2_BATTERY_REPORT.md`,
   `docs/ADR-0017-retrieval-hygiene.md`. Live promotion gated on the Day-7
   reading.
2. **E2 recall hygiene.** Excludes P1 route-meta stubs and P3 echo-copies from
   the recall pool at retrieval time only; the event log stays complete
   (nothing deleted or rewritten). Measured: meta_recall 0.0000, no diversity
   loss, no starvation (`backend/scripts/out/conc_e2_seeds`).
3. **H1 formal cross-source confirm (harness-replay), after the detector
   measurement fix (commit e93a6f5).** 14/16 seeds PASSED across 4×28d
   experiments; two remaining zeros fail on genuine criterion-(d) substance.
   Evidence: `backend/scripts/out/confirm28_multisrc/DETECTOR_FIX_NOTE.md`,
   `H1_FORENSICS.md`, per-experiment `h1_recompute.json`. (The live-world H1
   item remains open — see scorecard.)
4. **Phase 7 private-projects skeleton (ADR-0016).** Flag-gated
   (`RESIDENT_PRIVATE_PROJECTS`, **default off**), shadow-first detector
   mirroring the harness H1 criterion, 129-store shadow audit **0/0
   agreement**, flag-off byte-identity tested. Emission (`project.created`)
   stays off until a natural H1 chain is observed live.
5. **E0/E2 pollution resolution.** E0 counterfactual
   (`backend/scripts/out/e0_pollution/E0_REPORT.md`): meta-recall pollution is
   ~100% supply-side (P1 stubs + P3 echoes); cleaned pool recalls 0 meta rows
   (0/18,992), fresher and more diverse. Hygiene-first ordering validated:
   enrichment without hygiene amplifies the echo loop.
6. **Rounds 5-12 negatives archive.** All concentration-loop iterations
   (`conc_va…conc_ve2`, `conc_e2_seeds`, `conc_round9/10`, `round11*`,
   `round12*`) preserved untouched, plus the honestly voided first multisrc
   confirm (`confirm28_multisrc_void_wrong_snapshots`). Conclusion recorded
   (`docs/SELF_ORIGIN_REPORT.md` §十): the harness has an **architecture
   boundary** — the fake provider regenerates templated route-meta self
   material at write time, so plurality ≤0.35 must be evidenced live-world.
7. **Multi-source replay world.** 78 snapshots, 9 strict multi-source topics
   (`backend/scripts/out/enrich_run2_note_20260913.md`); failed fetches
   honestly logged.

## Acceptance scorecard (per criterion)

| # | Criterion | Status | Evidence / blocker |
|---|---|---|---|
| 1 | Sealed v0.2 regression byte-identity (flag-off) | **met** | 12/12 cells byte-identical; 390 tests (`ACTIVE_V2_BATTERY_REPORT.md`) |
| 2 | E2 hygiene: meta_recall → ~0, no diversity loss | **met** (harness) | `conc_e2_seeds`; `e0_pollution/E0_REPORT.md` |
| 3 | H1 cross-source genesis chains, formal | **met in harness-replay** (14/16 seeds); live-world variant **pending** (real days; the 3-instance fleet widens the odds) | `confirm28_multisrc/DETECTOR_FIX_NOTE.md`; relay recovered 2026-09-14 |
| 4 | Self-thread plurality ≤0.35 (multi-seed) | **pending live-world evidence** — unreachable in harness (frozen-world context confound, Round 13); the 3 active-v2 instances (seeds 1/2/3) accumulate it live, measurable once each store's `self_thoughts > 0` | `SELF_ORIGIN_REPORT.md` §十, `round13/VERDICT.md`; relay recovered 2026-09-14 |
| 5 | Phase 7 genesis evidence + flag-on | **pending live-world evidence** — skeleton done (ADR-0016), emission gated on a *natural* live H1 chain in the fleet + shadow audit (the watch flags it; not auto-enabled) | relay recovered 2026-09-14; real days |
| 6 | Real-time living stability over days | **partially met** — active-v1 running since 2026-09-12 09:41:47 UTC (247 events, autonomous circadian wakes verified); the Day-7 (2026-09-19) reading is now **multi-seed** (v1 ref + 3 active-v2) | `docs/LIVING_BASELINE.md`; blocker: elapsed time (relay no longer degrading) |
| 7 | Model provider availability | **recovered** — qwen relay switched to `<debian-relay>:8002` 2026-09-14 (old host `<legacy-host>` died); verified HTTP 200 / 0.1 s; substantive wakes resume; the 6h watch keeps a live up/down check | live process logs; relay health check |

**Plain statement:** v1.0 is **NOT releasable today**. What is missing is not
code — it is live-world evidence (plurality, live H1, Phase 7 genesis). The
relay is recovered; the only remaining requirement is real days of living,
now accelerated (not compressed) by the 3-instance parallel live fleet.

## Migration notes (draft)

- **Flag-off byte-identity.** With all new flags off, behaviour is
  byte-identical to the sealed v0.2 reference (sealed regression 12/12 cells;
  only the documented additive `world.live` telemetry key differs). The
  sealed reference arms (`sealed-baseline`, `active-v1`, `dense-debug`) carry
  empty `cognitive_flags`; only `active-v2` carries flags.
- **Sealed tags untouched.** `v0.1` and the sealed regression artifacts under
  `backend/out/world_sim` were not modified; historical experiment archives
  under `backend/scripts/out/` are immutable (append-only, negatives kept,
  the voided multisrc run archived as-is).
- **Event log completeness.** E2 hygiene excludes at retrieval time only;
  nothing is deleted or rewritten from any store. Private-projects emission
  is flag-gated and default-off; when enabled it appends at most one event
  per genuine genesis.
- Profile activation, env vars, and provider config: see
  `docs/MIGRATION-v1.0-DRAFT.md`.
