# ADR-0014 — Living profiles & the Accelerated Living Harness

**Status:** adopted (2026-09-12, owner-directed Active Living Experiment)
**Supersedes (operationally):** the low-activity baseline registered 2026-09-12
06:56 UTC (archived intact; remains the sealed reference arm).

## Context

The low-activity baseline proved the system runs, but its rhythm (≈4.6
wakes/day, 1 world look/day, every question dying dormant) is too sparse both
for fast iteration and — per the owner — for a *life*. The owner authorised a
new experiment with one governing principle:

> **增加机会，不增加欲望。**

## Decision

### 1. Living profiles (`resident/profiles.py`) — opportunity only

A `LivingProfile` sets exactly five knobs: the circadian per-state wake
cadence (ACTIVE/QUIET/DROWSY), the world cooldown, the world daily cap, the
re-observation horizon, and the beat rate. By construction it cannot touch a
cognitive criterion: thought formation, question rolls, revival probability,
route boosts, retrieval ranking, similarity thresholds, self-origin weights,
and every importance/curiosity-style score stay sealed. Registered tiers:

- `sealed-baseline` — the sealed values (reference; `reobservation_days`
  sentinel = never, byte-identical to the sealed sweeps — regression-checked);
- `active-v1` — the candidate living rhythm (ACTIVE 20 min / QUIET 60 min /
  DROWSY 90 min; cooldown 45 min; cap 14/day; re-observation after 2 days);
- `dense-debug` — stress test only (8/20/40 min; 15 min; 28/day; 1 day).

The **re-observation horizon** is a world-side affordance, not a desire knob:
real pages change and are re-checked; the propensity roll still decides. The
sealed sentinel reproduces "never re-observe" exactly.

### 2. The Accelerated Living Harness (`scripts/active_living_harness.py`)

Simulated days, not real weeks — and **time really passes**: a VirtualClock
drives every engine, so dormancy, absence, cooldown, circadian cadence,
revival and thread-age all read simulated time (the owner's hard requirement).
Per run: an independent event store + manifest-like config.json + report.json
(fingerprinted; every iteration kept, nothing overwritten), the **replay
world source over the real captured snapshots** (deterministic, network-free),
the deterministic fake provider (cognitive criteria identical across arms —
density is the only variable), questions + continuity + shadow on.

Two harness-only honesty rules learned by smoke-testing:

- **replay seeds are derived from the world's real captures** (latest
  status-200 snapshot per URL). A hand-written seed list mostly lacking
  captures burns the honest budget on 18/19 `not_in_snapshot` failures;
- a replay source **only offers what it can replay** — absorbed outbound
  links of captured pages stay discovered but are never offered (observing
  them in replay would deterministically fail).

## Results summary (14 simulated days × 4 seeds, 5-min beats)

See `docs/ACTIVE_LIVING_REPORT.md` for the full table, the activity frontier,
the failure-mode critical points, and the recommendation (active-v1 adopted
for real-time living, with the self-origin collapse recorded as the principal
open finding — a cognitive-criteria question, deliberately not tuned here).
