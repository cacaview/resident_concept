# ADR-0021 — Round-13 cleanup: remove the failed revisit self-justification exclusion and the unvalidated rest-distinctness knob; keep the consolidation dedup fix and portability guards; re-baseline the sealed world-sim

**Status:** Adopted (2026-09-15), owner-directed. Closes the `docs/RELEASE-AUDIT-v1.0.md` §6
open item via **option (b)** — resolve the Round-13 drift first, then re-seal.

## Context

The standalone sealed world-sim regression (`backend/scripts/sealed_regression_v02.py`) byte-compares
a world-sim sweep against `backend/out/world_sim` (sealed **2026-09-11**). It went **RED** from
post-baseline engine changes. Investigation (recorded in
`backend/scripts/out/round13/VERDICT.md`) established the drift is **entirely** from the **kept**
consolidation dedup fix in `backend/src/resident/sleep.py` (`4d3af5a`) — a genuine data-quality bug
fix that the old baseline itself is missing (the sealed baseline still contains the duplicate-self-thread
corruption the fix removes). It is a documented-GOOD, unconditional fix.

The same `4d3af5a` commit also introduced two **Round-13** knobs. Both were already **off by default**
and are **dead code on every current path**: no profile and no world-sim arm enables them — only the
**failed** R13 harness arm (`REVIVAL_VARIANTS["R13"]`) ever did. They are:

1. `revisit_context_exclude_self` (`mind_loop.py`) — the revisit route's material-match justification
   context excludes the candidate dormant threads' own prior thoughts.
2. `rest_assoc_require_distinct` (`world.py` `WorldParams`) — world reactivation/association thoughts
   are emitted only when the juxtaposed items are structurally distinct.

Both are inert today; removing them is behavior-neutral (the full suite stays green with no test
referencing either).

## Decision

1. **Remove `revisit_context_exclude_self`** — documented **FAILED**. `VERDICT.md` (round-13, real-model
   28d × 4 seeds) records it as *HARMFUL*: it **starves** (seed3: 1 self-thought) or **collapses to a
   single thread** (seed2 0.976, seed0 0.576). Removing the thread's own thoughts from the justification
   context leaves the material rule nothing to match, so the self-citation loop does not break — it
   degenerates. Same pattern as rounds 5–12: selection-rule surgery starves or migrates.

2. **Remove `rest_assoc_require_distinct`** — part of the same failed R13 arm; it was **never
   individually validated**, is **untested**, and is **inert today** (no current path enables it). This
   is the **one judgment call** in this ADR: the negative result is not a measured per-knock-out verdict
   the way #1 is — it is the arm-level failure of the R13 variant it shipped in. The negative result
   remains in `VERDICT.md` and git history (`4d3af5a`); nothing here re-opens it.

3. **Keep the consolidation dedup fix** (`sleep.py`, `4d3af5a`) — documented **GOOD** and unconditional
   (a data-quality fix, not a tuning knob). `VERDICT.md`: it makes the concentration metric honest by
   removing near-duplicate threads that split one topic across several ids. `backend/src/resident/sleep.py`
   and its three tests in `backend/tests/test_sleep.py` are untouched.

4. **Keep the `80dfb1f` portability guards** — the `EventStore._connect` contextmanager and the
   `world_profile` empty-count guard at the `max(...)` lines. Behavior-preserving; unrelated to the
   Round-13 drift.

5. **Re-baseline `backend/out/world_sim`** to the cleaned post-Round-13 engine. The re-seal is performed
   separately by the owner (this ADR does not re-seal).

## Framing — this is re-baselining, not a claim the old baseline still holds

This ADR is **re-baselining to a clean post-Round-13 state, NOT a claim that the 2026-09-11 baseline still
holds.** The old baseline is **invalidated by design**: the kept dedup fix legitimately changes cell
dynamics (e.g. `C_all/seed0` `n_threads` 8→6). That is the intended consequence of a genuine bug fix, not
a regression to be reverted.

The new baseline pins exactly: **dedup fix ON, R13 knobs absent, `80dfb1f` portability guards present,
agency flag-off.**

## Evidence

- `backend/scripts/out/round13/VERDICT.md` — the round-13 verdict (R13 FAILED; dedup fix GOOD; root cause
  is the frozen harness world context).
- The sealed-drift diffs on the `C_all` cells (`n_threads`, `self_origin_share`, `route_entropy`,
  `no_op_ratio`, …) from `scripts/sealed_regression_v02.py`.
- `docs/RELEASE-AUDIT-v1.0.md` §6 — the RED-baseline open item and its two resolution options.

## Consequence carried forward

`VERDICT.md`'s architecture-level finding **stands**: plurality is measured in the **live world**, not the
frozen harness — any residual concentration in the harness once the world freezes is a frozen-context
artifact, not the resident's steady-state plurality. **No plurality surgery is re-opened by this ADR.**
