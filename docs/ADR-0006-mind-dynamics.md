# ADR-0006: Mind dynamics — can a thought of Resident's own survive the day and re-influence itself?

Status: Accepted (with a central *negative* finding the report keeps front and centre)

> Phase 4. The direct follow-through to the top next-step in
> `SIMULATION_REPORT-14day.md` ("let self-threads be re-continued in lulls") and
> `TODO.md` item 9(a). It is the project's first question that is less
> "feature development" and more **mind dynamics**: a thought the mind produced
> *by itself* — does it outlive the day, and does it come back and shape later
> thought?

## Context

Phase 3 (`ADR-0004 §7`) made independence measurable and added the one allowed
non-user material source: sleep crystallises a **self-originated** thread at low
probability. The 14-day treatment-vs-control run proved that source is *causal*
(3 self-threads, 15% self-origin, 0 in the control) — but it **plateaus at
~0.23**. The mechanism for the plateau: self-threads form early, then go
**dormant and are never re-engaged during a no-user wake**, so a thought of the
mind's own rarely *survives the day and re-influences itself*. The mind has the
material; it just stops returning to it.

The owner's question for this phase, verbatim in spirit: *a thought that belongs
to the mind itself — can it live past today and affect the mind again in the
future?*

### The constraint (verbatim, still in force)

> 禁止硬配额、禁止直接强制选中、禁止通过 fake provider 人为制造独立性
> (no hard quota, no forced selection, no manufacturing independence via the
> fake provider). And: *不要为了达到某个比例目标而调参数* — do not tune
> parameters to hit a ratio target.

## Decisions

### 1. A state-gated, gentle, probabilistic dormant self-thread revival bias

During a quiet lull, a **dormant** self-thread that has **real history** and has
**slept long enough** gets a *gently raised* chance of being engaged. It is
implemented at **two levels**, both in `self_revival.py` and wired through
`MindLoop` / `RoutePolicy`:

- **Gate (`revival_gate`)** — the bias only *exists* when **all** of:
  no user input for ≥ `SILENCE_S` (12 h), no strong external stimulus (a user
  message or an experience) for ≥ `LOW_STIMULUS_S` (6 h), and at most
  `MAX_ACTIVE` (2) active threads. A lull is a genuine quiet, not any idle tick.
- **Candidate (`qualifying_dormant_self_thread`)** — the recalled thread must be
  `origin: "self"`, dormant ≥ `MIN_DORMANT_S` (12 h), and carry ≥ 1 durable
  mental event (`thought.created` / `memory.consolidated` / `experience.created`)
  — i.e. it was actually *thought on*, not an empty shell. Ties go to the
  most-forgotten (oldest last activity). It is always chosen from **real store
  state**, never invented.
- **Route lift (`SELF_REVIVAL_ROUTE_LIFT = 0.15`)** — the *primary* lever. When a
  candidate is eligible, the `revisit` route's base score is nudged up by a small
  constant. This raises the probability the mind *engages a dormant thread at
  all* during the lull (otherwise `rest` wins and nothing resurfaces). It is an
  additive, bounded nudge — never enough to dominate, never a forced pick.
- **Thread preference (`REVIVAL_STRENGTH = 0.35`)** — a secondary, *probabilistic*
  tie-break on the chosen revisit: with probability 0.35 the eligible candidate
  is the thread revisited; otherwise the default most-recent dormant thread wins.
  **Nothing is ever forced** — the default still wins with probability 0.65.

This is explicitly **not a quota** (no fixed share of wakes is reserved), **not a
forced selection** (a seeded probability, always defeasible), and **not
provider-based** (which *thread* resurfaces is decided here from real state; the
model — fake or real — only grounds the resulting *text* from that thread's real
memories). Independence can only *emerge* from recall of the mind's own material,
honouring the constraint.

### 2. Self-dynamics telemetry — six derived reads, never new state

`self_dynamics.py` reads the append-only log (provenance-complete, reproducible)
and reports (`ADR-0004 §5` style — a structural read, not a mutable blob):

- `self_thread_revival_rate` (+ `self_thread_revival_detail`) — of the lull
  wakes where a dormant self-thread was an **eligible** candidate, the share where
  the mind **engaged** a self-thread. The raw `{eligible_wakes, engaged}` counts
  are exposed so a `0.0` reads as "0 of N" (N may be 0 or 1), never a silent
  "measured zero".
- `self_thread_avg_lifetime_days` — mean age of self-threads (creation → last
  engagement): do self thoughts *live*, or die the same day?
- `self_loop_concentration` — top-1 self-thread's share of self-origin thoughts
  (+ distinct count). High ⇒ the mind is *sucked into one* idea.
- `self_thread_generation_depth` — the longest chain of self-origin thoughts where
  each cites an earlier self-origin thought **in the same thread** ("thought
  begetting thought").
- `longest_self_chain` — longest run of consecutive self-origin thoughts (a user
  turn or a non-self thought breaks it; rests do not).
- `self_topic_diversity` — how many distinct self-threads are engaged + the
  (normalized) entropy of self thoughts over them (broad vs. narrow).

### 3. Three-group 14-day deterministic experiment

Same seed (0), same scripted user stream (5 topics, no-user gaps on 3/6/9/12),
14 days, the *real* engines. The only variable is the mechanism:

- **A** — `self_prob = 0.0`, revival off (no self material source at all).
- **B** — `self_prob = 0.35`, revival **off** (self material exists but is never
  lull-revived — the Phase-3 baseline).
- **C** — `self_prob = 0.35`, revival **on** (this ADR's mechanism).

A fixed seed ⇒ byte-identical re-runs (verified: a clean C re-run is
byte-identical in origin distribution, self-dynamics, and thought count).

## Consequences — the honest findings

The mechanism is implemented exactly as designed and fires correctly. **The
finding is that, in a realistic 14-day stream, it almost never gets the chance to
fire — and the favourable headline it produces is a single-trigger cascade, not
repeated lull-revival.** This is reported as the result, not smoothed over.

**1. The gate is conjunctive and rarely satisfied: 1 eligible lull in 84 wakes.**
`self_thread_revival_detail` for C is `{eligible_wakes: 1, engaged: 0}`; for B it
is `{0, 0}` (mechanism off). The lull *does* open (no recent input, few active
threads), but a qualifying *candidate* (a dormant, historically-thought-on,
self-origin thread ≥12 h asleep) exists at only **one** wake across the whole
window. Most quiet wakes fail the gate because the binding constraint is the
*dormant self-thread* condition, not the silence — the self-threads that do form
are either still active or not yet dormant long enough when a lull arrives.

**2. The C-vs-B self-origin gap is a single-trigger deterministic cascade, not a
process.** B and C are **byte-identical for the first 60 wakes**. They first
diverge at **wake 60** — exactly the single eligible lull — where C's +0.15 lift
flipped that one wake from `continuity` (B) to `revisit` (C). That one
route/RNG change reshapes the remaining **23-wake** tail, in which all of C's
extra self thoughts (9 → 12) accrue. So the self-origin difference (0.210 vs
0.152) is real *in this seeded run*, but it is **not** evidence that "the mind
keeps reviving its own thoughts during lulls" — it is the downstream consequence
of one correct trigger on a seeded trajectory. Attributing the growth to repeated
lull-revival would be a category error.

**3. What actually sustains self-origin is continuity + ordinary revisit — which
B already does.** B (no revival gate at all) still produces **9** self-origin
thoughts (6 via `continuity`, 3 via `revisit`) and a 3-wake self-driven chain.
So "a thought of the mind's own re-influences itself" is **already true in B**
through the ordinary route policy returning to a live/self thread — the
lull-revival gate adds a *different* path (resurfacing a *dormant* thread in a
quiet) that, as tuned, contributes almost nothing in this stream.

**4. Preserved unsatisfactory results.** (a) An earlier intermediate build used
*only* the thread-level preference (no route lift). It fired (revival engaged a
self-thread ~0.37 of the time) yet **did not grow self-origin** (B and C both
9, identical 0.23 plateau) — confirming the **route-level lever is the primary
one**: you must first make the mind *engage a dormant thread at all* during the
lull, not merely pick a better one. (b) The fully-featured mechanism's
`self_thread_revival_rate` is `0.0` (0 engaged of 1 eligible) — the one lull it
could fire in, the mind revisited a *user* thread, not the self candidate.

### Answers to the five judgment criteria

| # | criterion | result | honest reading |
|---|---|---|---|
| 1 | self-origin grows, not plateau? | C 0.210 (→0.30 by d14) vs B 0.152 (plateaus 0.23) | Nominally grows — but the mechanism fired **once**, so the growth is a cascade, **not** robustly caused by lull-revival. |
| 2 | deeper "thought-begets-thought" chains? | C gen-depth 5 vs B 4 (longest self-chain 3 in both) | The deeper chain also falls in the post-trigger cascade window. |
| 3 | stuck on one self-thread? | **No** — top-1 share C 0.42 / B 0.44, 3 distinct, diversity entropy 0.98 | **Robustly healthy** — no single-thread fixation in either. |
| 4 | user-derived share drops? | C 0.789 vs B 0.848 (A 0.964) | Drops, but driven by the same single-trigger cascade. |
| 5 | route entropy stays healthy? | **Yes** — C 0.989 (B 0.988, A 0.866) | **Robustly healthy** — the bias does not collapse route diversity. |

Criteria 3 and 5 (no fixation; healthy route entropy) are robust and reassuring.
Criteria 1, 2, 4 show favourable *numbers* whose causal origin is a single
deterministic trigger, not a repeating mechanism.

### What this means for next steps

- **The gate thresholds are the binding constraint, and I did not tune them** to
  make the mechanism fire more (owner constraint). The honest result is that, at
  these thresholds and this user cadence, lull-revival is a **rare** event. A
  *sensitivity study* over the thresholds (silence / dormancy windows,
  `MAX_ACTIVE`) — reporting the measured trigger frequency, not chasing a target
  ratio — is the natural way to find whether a regime exists where the mechanism
  matters.
- **The live circadian scheduler (`TODO` item, still pending by owner decision)**
  would change the lull *structure* (more, shorter, real no-user wake slots) and
  could change how often the gate opens in either direction. That is why the
  owner deferred it until *after* this dynamics study — the study shows the
  mechanism is sound but **gated**, so the wake cadence is now the key variable.
- **World browsing** (`ADR-0005`, still deferred) remains the largest missing
  material source for moving `user_derived` below ~0.85; it is orthogonal to this
  dynamics result.

No hard quota was used anywhere. The favourable C numbers are reported together
with the single-trigger-cascade finding and the `0.0` revival rate, so the record
shows *both* what the mechanism can do and how rarely it actually gets to.

---

## Phase 4.1 addendum — threshold sensitivity study

`SIMULATION_REPORT-revival-sensitivity.md`. The "natural next step" above (a
sensitivity study over the gate thresholds, reporting measured trigger frequency,
not chasing a ratio) was carried out. It made the gate + levers configurable
(`self_revival.RevivalParams`; defaults keep Phase-4 behaviour byte-identical),
then swept `dormant_min_age × user_silence_min × MAX_ACTIVE` (64 configs) on the
same 14-day stream × 4 fixed seeds (260 runs, 0 errors), and — because a working
region exists — separately swept the route boost {0.05, 0.10, 0.15, 0.20} on 5
representative configs (84 runs). No threshold was tuned toward a target ratio.

**Findings (all preserved, including negatives and single cascades):**

1. **The three knobs each control one thing.** `dormant_min_age` = *intensity*
   (how much self-thought when it fires: d=6/12 ≫ d=24 > d=48≈0).
   `MAX_ACTIVE_THREADS` = *recurrence* (a loose a≥3 ⇒ revival fires in 4/4 seeds;
   a tight a=1 ⇒ 0–2/4 seeds). `user_silence_min` is **capped by the 6 h
   external-stimulus floor**: s=2 h is byte-identical to s=6 h, so the effective
   "how quiet" threshold is ~6 h, not the silence value below it.
2. **The route lift is a weak, double-edged lever.** `route_changed_by_bias`
   (the counterfactual "revisit won *because* of the lift") is far smaller than
   `eligible_wakes` — revisit often wins anyway. And the lift can *divert*
   self-material to user threads: in the highest-self-baseline seed, turning the
   mechanism ON *reduced* self-thoughts (self 13→10, user_old 20→24) because the
   0.35 preference often lands on a user dormant thread.
3. **"Recurring revival" vs "single-trigger cascade" is set by `MAX_ACTIVE`.**
   a≥3 ⇒ engaged in 4/4 seeds, total effect spread and ≥0 across seeds
   (genuinely recurring, though each direct revival is still small). a≤2 ⇒
   engaged in 1–2/4 seeds with one seed carrying a +11~+18 cascade and another
   seed *negative* — the "one trigger, long cascade" regime, not repeated revival.
4. **High top-1 "fixation" is mostly a low-denominator artifact; genuine
   fixation is seed-conditional** (only the responsive/cascade seed concentrates
   self-thoughts on one thread, at small dormancy). Route entropy never collapses
   (0.87–0.99) — no route-level locking in any config.
5. **Route boost does not monotonically buy self-origin.** Sweeping 0.05→0.20
   raises `route_changed_by_bias` (the lever does what it does) but the net
   self-origin is noisy and non-monotonic; the single-seed cascade is *identical
   across all boost sizes* — it is a seed/gate property, not the lever's.
6. **`engaged` (direct revival) systematically understates the total effect
   (`delta`)**: a revival can re-surface a self-thread that then continues
   naturally (e.g. `engaged=0` yet +4 self-thoughts via subsequent `continuity`).

**Working region** (for the circadian integration to target): dormancy 6–12 h,
silence ≈6 h, `MAX_ACTIVE` loose (≥3) ⇒ recurring, robustly-positive total effect.
**Lever guidance**: to strengthen night-time self-revival, act on the *candidate*
mechanism (right dormant self-thread recalled in a lull), **not** on a bigger
route lift (which amplifies user-diversion). **Monitor** engaged (recurring?),
delta (net positive?), route_changed_by_bias (hard-redirect share), and *negative
deltas* (diversion signal) — always per-seed / per-time-slot, never a single mean.
