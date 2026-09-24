# ADR-0004: Psychological time structure (stateful routes, multi-path memory, sleep consolidation)

Status: Accepted

> **Phase 3 addendum** (independence — where thoughts come from): see
> Decision 7 below and the 14-day evidence in `SIMULATION_REPORT-14day.md`.
>
> **Phase 4 follow-up** (mind dynamics — can a self thought survive the day and
> re-influence itself): the state-gated dormant self-thread revival bias and the
> A/B/C dynamics experiment, in **`ADR-0006-mind-dynamics.md`** and
> `SIMULATION_REPORT-selfthread-dynamics.md`.

## Context

v0.1 (Proof-of-Life) proved the event-sourced loop runs: a wake can rest,
persist a provenance-complete thought, and survive a restart. But the mind
had **no time structure**: routes leaned on the model or a die, retrieval was
a single similarity top-k, sleep did not exist as a real phase, and a private
thought's shareability was fixed at birth. The Phase-2 goal is to let Resident
evolve from "can wake on its own" to "has a real psychological time
structure" — while staying a persistent individual, **not** a goal/task
agent (CLAUDE.md, ADR-0002).

## Decisions

### 1. Route selection is state-driven, randomness is a perturbation

The route a wake takes is chosen by `RoutePolicy.select(state, rng)`, from
Resident's *state*: active/dormant threads, events since the last wake, topic
concentration, recent route history, and time since meaningful activity. A
**repetition-fatigue** term lifts the "departure" routes (revisit / distant /
serendipity / rest) when one route or topic keeps looping, so the mind wanders
instead of spinning. Randomness enters only as small bounded exploration
noise (`RoutePolicy.exploration`), never as the primary decider. The policy is
deterministic for a fixed seed, which is what makes the accelerated life
simulation reproducible. Every decision records its full score vector, so a
route choice is provenance-complete — the log says *why* a route won, not just
which one.

> Anti-pinning: continuing an active thread is favoured, but when nothing new
> has happened since the last wake, the bonus is halved so `rest` can win.
> This was the single largest defect the first simulation surfaced (continuity
> was 50% of wakes, re-chewing one thread with no new material).

### 2. Retrieval is multi-path, never a single top-k

`MemoryRetrieval.multi_recall` interleaves several of six retrieval paths —
`semantic_near` / `temporal` / `revisit` / `distant` / `forgotten` /
`serendipity` — with MMR diversification, so a recall does not collapse onto
the one most-similar memory. Embeddings are pluggable: `HashingEmbeddingProvider`
is the hermetic default for tests; a real embedding provider can be swapped in
behind the same interface. The memory index is a **rebuildable derived
cache** (activation stats, age-at-activation samples, retrieval diversity,
forgetfulness) — never the source of truth, which stays the event log
(ADR-0001).

### 3. Sleep is state-gated consolidation, not a content cron

`SleepEngine.sleep_once` runs on the night phase and, based on real state, may
compress redundant material, move stale threads to dormancy, reactivate old
memories, form a limited cross-memory link, or do nothing. A low-probability
"free-association" dream is allowed but **only** as a durable
thought/question/link that re-connects two *real* memories — never a
personified "dream performance". A sleep may legitimately produce no new
thought. Every sleep event carries provenance (`source: sleep, run_id`).

### 4. Re-entry: deferred share-candidate promotion

A private thought's shareability is **not** decided at birth. A thought can
later surface as a *new*, shareable `memory.share_candidate` once a LATER
durable event genuinely re-engages it — a `memory.consolidated`, a
`thread.revisited`, or a later thought/wake on a deliberate step-back route
(revisit / distant / serendipity) — citing it. The original private thought is
immutable; the promotion is a new event with provenance pointing at both the
source thought and the justifying reflection(s). Incident recall while
continuing another thread does **not** qualify. This is the correction the
phase brief asked for: a thought "may become worth sharing after it has been
revisited and reflected upon," with provenance.

### 5. Behavioral telemetry is a structural read, not new state

`Telemetry` derives the requested metrics — `route_entropy`,
`topic_entropy`, `memory_age_at_activation`, `revisit_rate`,
`thread_resurrection_rate`, `no_op_ratio`, `user_topic_dependency` — directly
over the event log + derived index. Periodic `telemetry.snapshot` events make
the behavioural profile inspectable without adding a mutable state blob.

### 6. Virtual clock injection for reproducible accelerated life

`EventStore` accepts `now_fn`, so the accelerated 7-day simulation
(`resident.simulation`) drives the *real* MindLoop/Sleep/Reentry engines over a
virtual 7-day timeline with a seeded rng. The live backend keeps wall-clock;
only the simulation injects time. A fixed seed ⇒ identical route sequence, so
the simulation report is **evidence**, not a demo.

### 7. Phase 3: thought-origin, independence telemetry, and the self-thread mechanism

The Phase-2 finding that motivated this: **user-topic dependency ≈ 0.78** — the
mind mostly revolves around the user. Phase 3 makes that measurable and asks
the sharper question: *after it wakes, who/what decides what appears in
Resident's head?*

**Thought-origin is a deterministic, grounded label — never from the model.**
Every persistent thought is tagged, at persist time, with a PRIMARY origin in
`thought.metadata["origin"]`, one of seven:
`user_recent` / `user_old` / `self_thread` / `private_project` /
`memory_revival` / `serendipity` / `world` (`origin.py`). The label is derived
from the route taken, the thread it follows **and that thread's own origin**
(`user` vs `self`, recorded on `thread.created`), and the recalled material's
provenance. "Recent" user input is a fixed 24 h (virtual) window, so a live user
topic and a stale user topic are told apart (`user_recent` vs `user_old`). It
is never invented and never asks the model.

**Independence is a read, not a dial.** `Telemetry` adds
`origin_distribution` and three shares: `user_recent_share`, `user_derived_share`
(any user recency), `self_origin_share` (Resident's own material), and
`independence_share = 1 − user_recent_share`. The report also measures the
**longest self-driven thought-run** — consecutive self-origin thoughts where a
rest/noop does *not* break the chain but a fresh user turn or non-self thought
does. This answers "does it sustain a thought-chain that does not depend on the
user's recent input?"

**The self-thread mechanism is the one allowed non-user material source — and
it is NOT a quota.** With low probability (default `self_prob=0.35`) and only
when the mind has recently been across ≥2 *distinct* user topics, sleep
crystallises a NEW **self-originated** `thread.created` (`origin: "self"`)
grounded in the two most-connected *real* cross-topic memories
(`sleep.py:_best_cross_topic_pair`). It is a probabilistic side-effect of
consolidation, capped on total count (`SELF_MAX_THREADS`, bounds growth — it
does not steer any single thought), and the route policy then continues it like
any other thread. Independence can therefore only *emerge* if the mind actually
accumulates its own material and keeps returning to it — never by forcing a
share. This honours the Phase-3 constraint: no hard quota; only retrieval,
thread persistence, sleep consolidation, and (later) serendipity/world may
produce it.

## Consequences

- The accelerated simulation is honest by construction: it observes emergent
  behaviour (route distribution, memory age at activation, thread
  dormancy→resurrection, cross-topic association, user-topic pull) and reports
  weak/absent signals as findings rather than hiding them.
- Findings from the first 7-day run (before fixes): continuity pinning at 50%,
  and share-candidate over-promotion at 81% of thoughts — both fixed by
  decisions 1 and 4 respectively. After fixes: continuity 17%, rest (a
  first-class "nothing happened") 36%, over-promotion halved to 52%, route
  entropy 0.77 → 0.87. Residual user-topic dependency (~0.78) is reported as a
  finding, not "fixed", because strong pull toward recent user topics is partly
  expected behaviour and deserves owner attention rather than a hidden dial.
- World browsing remains unbound (ADR-0005); the simulation exercises only
  the internal time structure.
- **Phase-3 14-day finding** (treatment `self_prob=0.35` vs control `self_prob=0.0`,
  same user stream, same seed — `SIMULATION_REPORT-14day.md`): the self-thread
  mechanism is **causal** — it produces 3 self-threads and a 15% self-origin
  share in the treatment, and **0** in the control. Self-origin **rises over
  time** (0 → ~0.23 by day 11, plateauing) and a **sustained self-driven
  chain** (3 consecutive self thoughts across a no-user gap) forms — in the
  treatment only. Honest caveats the report keeps front and centre:
  `independence_share` (1 − user-recent) rises in *both* runs because the
  user's stream dilutes and old user topics accumulate — so the raw
  "independence" number is **not** the signal; the real signal is self-origin
  (0.15 vs 0.00) and self-driven chain formation (run 3 vs 0). And the mind
  stays predominantly **user-derived** (~0.85) — the next material source to
  diversify it is world browsing (still deferred). Independence emerged from
  the mechanism, not from a quota.
