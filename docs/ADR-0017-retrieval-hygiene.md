# ADR-0017 — Retrieval hygiene for the active profile (recall_pool_hygiene + context_exclude_prev_thread)

**Status:** adopted as the *candidate* profile `active-v2` (2026-09-13, owner-directed formalization). **Prospective**: nothing in the live process changes. Live promotion is gated on the observation window closing (**Day 7 = 2026-09-19**) and the day-7 reading (see Rollout).

**Supersedes (operationally, for the candidate profile only):** the ADR-0014 rule that a `LivingProfile` tunes opportunity density only. `active-v2` is the one registered profile that carries cognitive flags; the sealed reference arms (`sealed-baseline`, `active-v1`, `dense-debug`) keep empty `cognitive_flags`, and the `MindLoop`/`SleepEngine` defaults stay `False`, so flag-off behaviour remains byte-identical to v0.2 (regression-checked).

## Context

The active-v1 living rhythm raised opportunity ~3.7x (17.1 wakes/day vs 4.6 sealed) and exposed a supply problem: the recall pool is saturated with **P1 route-meta stubs** (thoughts whose text starts with a `_META_PREFIXES` tag, written by the harness/mind itself at route time) and **P3 echo-copies** (consolidated/share-candidate events that duplicate their own provenance targets). The result is `meta_recall_share` ≈ 0.27-0.29 in every active run, above the 0.3 failure flag at peak, with self-thread monopolies downstream.

### Evidence base

- **E0 de-pollution counterfactual** (`backend/scripts/out/e0_pollution/E0_REPORT.md`): same queries, same routes, same legacy retrieval, two pools. With P1 stubs and P3 echoes removed from the pool, **zero** meta rows are recalled across all 12 historical stores (0 / 18,992 counterfactual recalled rows); meta_recall 0.37 → 0.0000 pooled. No recall starvation (counts ~10% *higher*), no diversity cost (type entropy 1.3 → 1.6, recall age fresher). **The pollution is ~100% supply-side.** Enrichment without hygiene *raises* echo share (the P3 feedback loop) — hygiene first, then selection.
- **E2 mechanism runs** (`conc_e2_seeds`, 14d × 4 seeds): meta_recall 0.0000, self volume 90-137, all guardrails pass.
- **Rounds 5-10** (`conc_va` … `conc_vd`, archived): every pure selection-rule surgery either starves or migrates the monopoly — EXCEPT **VA** (`thread_selection: material` + `context_exclude_prev_thread`): the only variant that never starved or migrated (14d seed1 0.845 → 0.667, volume held 27-63, no failure flags).
- **Round 11** (`round11/note_round11.md`): opportunity path refuted. E2 alone at 28d multisrc *amplifies* the stub monopoly (conc 0.627-0.982); E2T (creation-side title hygiene alone) starves self life entirely (0-2 self thoughts). Negatives kept.
- **Round 12** (`round12/note_round12.md`): the final legitimate combination **E2VA** (= E2 + VA) softened the monopoly further (mean conc 0.634 vs E2's 0.865; 2 of 4 seeds ≤ 0.60) with meta_recall 0.0000 and no failure flags — but never crossed 0.35, and every H1 chain remained stub-carried. This establishes the **harness architecture boundary**: the fake provider (`fake/deterministic-v0`) regenerates templated route-meta self material at *write* time, so no read-side rule can drive self_thread_concentration ≤ 0.35 in the harness.

## Decision

Register **`active-v2`** = the active-v1 rhythm (identical opportunity rails: 20/60/90 min cadence, 45 min world cooldown, 14/day cap, 2-day re-observation) plus two retrieval-hygiene measures default-ON:

1. **`recall_pool_hygiene` (E2) — default-ON.** Excludes P1 route-meta stubs and P3 echo-copies from the *recall pool only*, for every route. The event log stays complete (append-oriented history untouched; nothing is deleted or rewritten — exclusion is applied at retrieval time, before the limit). Justified by the E0 counterfactual: meta-recall pollution is supply-side, so cleaning the pool takes meta_recall to ~0 while *improving* every diversity guard.
2. **`context_exclude_prev_thread` (VA) — the accompanying selection rule.** The thread continued in the immediately previous continuity wake cannot be selected again. Kept because it is the only selection fix across rounds 5-12 that never starved or migrated self life, and in round 12 it softened the monopoly in 3 of 4 seeds without any failure flag.

Explicitly **NOT** in the profile (rejected alternatives, with their honest negatives):

- **E2T** (`thread_title_meta_filter` alone on E2) — creation-side hygiene only; starves self life (0-2 self thoughts per 28d seed, thread.revisited = 0). Round 11 Run B.
- **VE3 / VE4** — the full round-9/10 combinations. VE3 fixed creation but exposed the newborn-thread cold-start deadlock; VE4's `zero_history_bootstrap` is a recency-fallback bootstrap that never cleared the guards. Not re-litigated; archived under `conc_round9` / `conc_round10`.
- **E2-only (no VA)** — amplifies the stub monopoly at 28d multisrc (conc 0.627-0.982). Round 11 Run A.
- **Presentation-only fixes (E1 alone)** — fix the display channel but leave the pool polluted; E2 is the prerequisite, E1 remains a follow-up for the sibling `sleep.py _best_cross_topic_pair` display path.

### What harness evidence can and cannot establish

**Plurality (self_thread_concentration ≤ 0.35) evidence must come from the LIVE WORLD with the real model.** Rounds 5-12 jointly establish the harness architecture boundary: the deterministic fake provider regenerates templated route-meta self material at write time, so no legitimate read-side rule can reach ≤ 0.35 in the harness. Harness runs of `active-v2` (and every run in `ACTIVE_V2_BATTERY_REPORT.md`) are **mechanism and guardrail evidence only**: they prove hygiene removes meta-recall, volume and diversity guards hold, and no new failure flags appear. They cannot verify or falsify criterion 1 (plurality), H1 substantive genesis chains, or genesis quality.

### Rollout plan

1. **Now:** `active-v2` registered behind the profile flag; harness and tests support it; **sealed v0.2 byte-identity preserved when the flag is off** (sealed-baseline / active-v1 arms verified by the sealed regression and the test suite).
2. **The live process is untouched until Day 7 = 2026-09-19** (LIVING_BASELINE observation discipline). No live promotion before the window closes.
3. **Live promotion** only after the 2026-09-19 day-7 reading, and only if the reading supports it: the live world with the real model is the evidence venue for plurality (≤ 0.35), H1 genesis chains, and the absence of new failure modes under the real writer. If the reading is negative, the profile stays a candidate and the negatives are kept.
4. E1 (presentation-channel hygiene) remains the designated follow-up, strictly after E2-class hygiene is live.
