# ADR-0016 — Private Projects: derived genesis, no task loop

**Status:** adopted as a candidate behind a default-off flag (2026-09-13)
**Follows:** ADR-0011 (question lifecycle), ADR-0013 (monitor read-only), ADR-0014 (living profiles)
**Deliberately NOT:** a Goal→Plan→Task agent (see ADR-0002), a scored trigger
(see ADR-0013's blacklist), a completion pipeline.

## Context

Phase 7's question (RESEARCH-phase7-genesis): 什么条件使一个长期未完成的认知
结构，从"值得继续想"跨越成"值得试试看"? The research note pre-registered the
answer shape: the value must be **structural** — something true in the event
log that any reader can verify — never a self-rated number. The harness runs
(ROUND 2–4, confirm28 × 4 arms, confirm28_enriched × 2, confirm28_multisrc)
established the honest negative that governs this ADR:

- **H1=0 in every run so far.** The chain *substance* appeared repeatedly
  (chains spanning 20–27 days, 37 revisits, question evidence entering
  self-thought recall 195+ times), but every proto-chain failed on exactly one
  criterion — **source diversity** — which is a property of the replay world's
  frozen single-source-per-topic content, not of the cognition. The
  confirm28_multisrc rerun with multi-source snapshots still produced
  same-source evidence pairs (the re-observation loop's same-source bias).
- Therefore: this ADR ships the **machinery and the criteria**, never a tuned
  threshold to force a genesis to exist. Until a genuine H1 chain is observed,
  the correct number of projects is zero, and the default flag state guarantees
  it.

## Decision

### 1. Genesis is DERIVED state, not a scored trigger

A project is a **named view over an existing structure**, computed like
`question_state` (ADR-0011) is — the research note's provenance option 1,
plus option 2's single event for the autobiography. The genesis criterion is
exactly the harness H1 structural detector (`active_living_harness.py`
`h1_genesis_chains`), now computed **in-mind** from the log:

- a `question.created` whose **re-meet graph** has ≥2 `question.revisited`
  caused by `world.observation` (independent re-encounters, not the asking
  open itself);
- evidence + revisits spanning **≥2 distinct sources** and **≥7 days**;
- the chain **ends in self-thought activity**: a later thought of origin
  `self_thread`/`private_project` whose recall cites the question's material;
- **provenance independent of conversation**: no evidence event may come from
  the conversation family — a chain that needs the user prompt to stand does
  not count.

**Forbidden, by ADR-0013's blacklist:** any numeric curiosity / importance /
motivation / mood trigger. No threshold on a score exists because no score
exists. The detector reads only event types, ids, sources, timestamps, and
citations — all verifiable by any reader. Genesis either happened in the log
or it did not; when it did not, nothing is emitted.

### 2. Lifecycle: created → active → dormant → dead → revivable

```
project.created   (private; caused_by → the chain's last revisit;
                   related_to → question + evidence + self-thought;
                   provenance → full genesis chain, incl. sources & span)
[chain activity continues]           → active (derived)
[dormant_s without chain activity]   → dormant (DERIVED, like questions;
                                       never a tombstone event)
[later re-meet restarts activity]    → revived (derived; reuses the question
                                       revival semantics: the mind can re-find
                                       a dormant structure via recall, and a
                                       genuine re-encounter — a revisit, a
                                       self-thought citing chain material, or
                                       an artifact — flips it back to active)
```

- **Dormancy and death are facts, not failures.** Like questions (ADR-0011),
  dormancy is derived (`project_state()`), the log stays append-only, and
  there is no tombstone event. Pausing and failing are legitimate outcomes;
  "done" is **not an event kind** — the lifecycle has no completion semantics
  (a project that fades is a fact; at most a creation gets made).
- Death is simply a dormant state that stayed dormant; it carries no additional
  machinery. Revival is the same derivation run later: the derived state moves
  back to active when the log shows real re-engagement. Nothing is resurrected
  by the engine — only by the mind's own later activity.

### 3. Work happens through the existing wake routes

Autonomous work is **self-route and continuity thoughts** — the machinery that
already exists (ADR-0004 origins, ADR-0015 thread selection). A project is a
named long-duration thread with provenance, **not a todo list**:

- **NO new task loop.** No scheduler ticks on a project, no `project.step`
  pipeline, no Goal→Plan→Task decomposition (ADR-0002 stands).
- A project triggers no fetch, consumes no world-budget slot, schedules
  nothing. Nothing in the log is *caused by* a project except thoughts and
  creations the mind itself produced through its ordinary routes.
- A `project.created` event is retrievable like any memory; when recall
  surfaces it, `classify_origin` already tags thoughts built from
  `project.*`/`artifact.*` material as `private_project` (origin.py's
  `_PROJECT_FAMILIES` — pre-registered and unchanged).

### 4. Artifacts: creation re-enters the experience system

The `artifact.*` / `creation.*` family (pre-registered in `reentry.py`) is the
closing of the loop 自己创造的东西重新进入经历系统:

- `artifact.created` (private, like a thought) carries provenance links to the
  project chain (`related_to` → project event + supporting chain events). It
  re-enters the experience system **as a world-independent experience**:
  retrievable by every existing path (semantic_near, distant, forgotten,
  serendipity — all read the non-system pool), recallable, citable by later
  thoughts, and re-entry-eligible ("我做出了一个东西").
- **World-budget semantics (decided):** a creation consumes **NO
  world-observation slot** — it is not a look at the world. But it IS a **deep
  read of one's own mind**: it is an experience event, so it shifts the mind's
  bounded recent-windows and route context exactly like any mental event (the
  same measured micro-dynamics class as question events, ADR-0011's
  consequences). The budget question is thereby answered structurally: the
  world window's daily cap counts *window opens*, and a creation opens none.
  The cost is paid in attention (context shift), not in world budget — and
  that cost is honest because it is the same cost every thought already pays.

### 5. Monitor: read-only projection

`monitor_projects(store)` mirrors `monitor_questions` (ADR-0013): a pure
projection of the derived lifecycle — each project's genesis chain (resolvable
evidence, sources, span), status (active/dormant), last activity, artifact
list. No writes, no retrieval, no importance score; the test suite asserts
calling it leaves the store byte-count-identical.

### 6. Rollout: shadow-first, default OFF

`private_projects_enabled=False` (env `RESIDENT_PRIVATE_PROJECTS`) is the
sealed default:

1. **Shadow phase (now):** `scripts/phase7_shadow_audit.py` computes the
   in-mind genesis detector over the historical harness stores (confirm28*,
   round4) and reports would-have-genesis counts, to be compared against the
   harness audit's own `h1_genesis_chains`. Both read zero today; agreement of
   the two independent computations of the same criteria is the shadow-first
   verification. **H1=0 so far means the expected count is 0** until
   confirm28_multisrc (or a live multi-source world) produces a genuine chain.
2. **Flag-off invariant:** with the flag off, no detector runs, no rng is
   consumed, no event shape changes — byte-identical behaviour (tested).
3. **Enabling emission** (`project.created`) happens only after the shadow
   audit agrees with the harness audit on historical stores AND a natural H1
   chain has been observed. The flag gates emission only — the derived views
   (`project_state`, `monitor_projects`) are safe read-only projections and
   simply read empty when nothing exists.

## Consequences and boundaries

- When a genesis finally occurs, `project.created` enters the log and shifts
  bounded recent-windows like any mental event — a real but tiny
  micro-dynamics change (one event per genuine genesis; expected frequency ≈
  the H1 rate, which today is zero).
- The detector mirrors the harness's H1 rather than importing it: two
  implementations of one criterion is deliberate redundancy (the shadow audit
  cross-check), and the harness stays script-side, out of the mind.
- Revival semantics reuse question-revival machinery *semantics* (derived
  state moves on genuine re-engagement), not its code — questions revive via
  `question.revisited` events; projects revive via any chain-material
  re-engagement. If the two need unifying later, that is a recorded follow-up.
- What waits on evidence: whether the criterion is ever met at all
  (confirm28_multisrc / live world), whether ≥7d+≥2 sources is the right
  structural bar (it is the pre-registered one; it is not tuned), and the
  artifact-creation trigger — nothing in this ADR decides *when* the mind
  makes something; that requires an observed impulse pattern first
  (RESEARCH §2's "想做点什么" event-vocabulary gap).
