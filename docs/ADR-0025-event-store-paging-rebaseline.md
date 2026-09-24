# ADR-0025 — Re-baseline the sealed world-sim after the event-store paging fix (`f596088`): the 09-16 seal is invalidated by a correct recall fix, not a regression

**Status:** Adopted (2026-09-20), owner-directed. Resolves the post-`f596088`
sealed-regression RED via **option 2** — bisect to isolate the offending commit,
confirm it is a correct fix, then re-seal.

## Context

The standalone sealed world-sim regression (`backend/scripts/sealed_regression_v02.py`)
byte-compares a world-sim sweep (3 arms × 4 seeds, 14 days, fixture world) against
`backend/out/world_sim`, sealed at **2026-09-16** (ADR-0021, `150ff02`). Since 2026-09-16
it has been **RED (12/12 cells)**.

This was first surfaced while verifying the ADR-0024 thin-shell work. Investigation
established, in three independent ways, that **ADR-0024 is not a contributor**:

1. `world_sim.py` and its whole engine path (circadian / mind / memory / event_store /
   world) never import `main.py` or `agency/` — the only files ADR-0024 touched.
2. `git diff f283250..HEAD -- backend/src/resident/world_sim.py` is empty across the two
   ADR-0024 commits (`31d5856`, `a9b1cd1`).
3. The fresh sim output at HEAD is **byte-identical** to the pre-ADR-0024 commit
   `f283250` (the regression output diff is empty).

And the seal is internally consistent: it is **GREEN at its own commit** `150ff02`.
So the RED is entirely **post-seal drift** — the baseline was sealed 09-16 and never
re-baselined, while life-loop engine commits landed after it.

## Decision

1. **Bisect the four post-seal commits that touch the sim engine** (oldest → newest,
   each run in an isolated worktree against the 09-16 seal):

   | Commit | Change | Verdict |
   |---|---|---|
   | `672172c` | ADR-0022 reentry provenance gate | GREEN (0/12 differ) |
   | `95cea68` | hollow-shell cleanup batch | GREEN (0/12 differ) |
   | `67f509c` | ADR-0023 circadian tz pin | GREEN (0/12 differ) — its "pin UTC preserves the sealed baseline" claim **holds** |
   | `f596088` | event-store paging + `recent_thoughts` order | **RED (12/12 differ) — the sole culprit** |

2. **`f596088` is a correct bug fix, not a regression — it is not reverted.** Before it,
   `MemoryIndex._build` called `self.store.list(100_000, order="asc")`, but the event store
   caps a single `list()` call at **1000 rows**, so the memory index was silently built from
   only the **oldest 1000 events**, dropping the most recent — the most recall-relevant — ones.
   `f596088` pages past that cap via `store.since_seq` (completing the index) and fixes
   `recent_thoughts` ordering. Every world-sim cell generates ~1400–1600 events, so the fix
   legitimately changes recall → moves the sealed dynamics. Reverting it would reintroduce the
   truncation.

3. **Re-baseline `backend/out/world_sim`** to the current engine at HEAD. Performed in this
   ADR's execution (owner-directed), unlike ADR-0021, which left the re-seal to the owner.
   The 09-16 seal is **preserved, not destroyed**: copied to
   `backend/out/world_sim_SEALED_2026-09-16.bak` (git-ignored, matching the
   `world_sim_SEALED_2026-09-11.bak` precedent).

## Framing — this is re-baselining, not a claim the 09-16 baseline still holds

This ADR is **re-baselining to a post-`f596088` state, NOT a claim that the 09-16 baseline
still holds.** The old seal is **invalidated by design**: the paging fix legitimately changes
cell dynamics because more of the event history now feeds recall (e.g. `C_all/seed3`
`n_thoughts` 65→68, `origin_base` 65→68, `self_origin_share` 0.154→0.118; aggregate
`self_origin` C_all 0.155 mean, `world_to_thought_rate` B_follow_edge 0.425). That is the
intended consequence of a genuine recall fix, not a regression to be tuned away.

The new baseline pins exactly: **event-store paging ON (memory index complete past 1000
events), `recent_thoughts` ordering fixed, ADR-0022/0023 present, ADR-0024 thin-shell
Agency present (proven zero sim effect).**

## Evidence

- Bisect run (4 isolated worktrees vs the 09-16 seal): `672172c` / `95cea68` / `67f509c`
  GREEN, `f596088` RED 12/12 — the single flip.
- `git show f596088 -- backend/src/resident/memory_index.py` — `list(100_000)` → `since_seq`
  pagination past the 1000-row cap.
- ADR-0024 non-contribution: byte-identical sim output HEAD vs `f283250`; empty
  `world_sim.py` diff across the ADR-0024 commits; seal GREEN at its own commit `150ff02`.
- Re-seal verification: `sealed_regression_v02.py` at HEAD against the new seal →
  **12/12 GREEN** (byte-identical except the documented additive `world.live` telemetry key).

## Consequence carried forward

The event-store **1000-row `list()` cap is still a latent footgun** for any other caller
that assumes a single `list(N)` returns all N rows. `f596088` fixed the memory-index and
mental-state paths, but the underlying cap remains. That is out of scope here (a store-API
decision, not a re-baseline) and is **flagged for a future ADR** if more recall paths emerge.
**No mind-dynamics or behavior-semantics change is introduced by this ADR** — it only re-points
the comparison baseline to reflect an already-shipped, correct fix.
