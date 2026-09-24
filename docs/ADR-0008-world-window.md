# ADR-0008: World Window — the world as a *source of experience*, not a task engine

Status: **Accepted** (with the honest findings in `SIMULATION_REPORT-world.md` kept
front and centre — most importantly that a third material source **does not raise
self-origin**, and that **on-topic** world material *dilutes* it)

> Phase 6. The standing owner rules for this phase, verbatim:
>
> > World 是经历来源，不是任务来源；**reading ≠ thought；observation ≠ belief**。
> > 增加完整事件链：world.window_opened → world.observation → world_experience →
> > impression → optional thought/question/thread。每一步保留 provenance；任何
> > "我看过/读过/发现过"的后续陈述必须能回链真实 observation。
> > World Window 必须受 circadian/mind state / time since user activity / 当日预算 /
> > 最近 world exposure / 当前内部 thread 状态 共同影响，**但避免设计成极难触发的
> > 巨大 AND gate**。记录每次 eligibility、open/skip 和阻塞原因。
> > 第一版严格预算：light fetch ≤8/day / deep read ≤3/day / alien ≤1/day /
> > accident continuation ≤2/day。**上限不是任务；0 次/day 合法。**
> > 不要以降低 user_derived 为优化目标，不要调参数追漂亮比例。
>
> The standing hard constraints (CLAUDE.md) remain in force: append-only event
> history; claims about Resident's own life require provenance; no fake daily
> experiences; a wake cycle may legitimately do nothing; interests inferred from
> behaviour/history; user messages do not erase pre-existing mental state. And the
> security rules: the world corpus is **sandboxed/hermetic** (no real network) so
> the sim is deterministic, and the whole mechanism is **opt-in** in `main.py`
> (`RESIDENT_WORLD`, default **off**) so the live app and the hermetic tests are
> unchanged by default.

## Context

Phases 3–5 established where thoughts come from: `thought_origin` tracking (ADR-0004
§7), a probabilistic **self-thread** mechanism (ADR-0004 §7 / ADR-0006), and a
**circadian rhythm** that raises the dormant-self-thread *opportunity* (ADR-0007).
Across all of them the mind stayed **~0.52–0.65 user-derived** and, best case,
**~0.30 self-origin**. The one material source that had *never* been introduced was
**the world itself** — everything the mind thought about was either the user's own
recent/old material or its own threads. That is the gap this phase fills.

The question this phase has to answer is deliberately *not* "can Resident read the
web?" It is: **does the world become a genuine third kind of experience — material
that Resident encounters, leaves mostly unprocessed, occasionally reactivates into a
thought, and revisits days later — or does it turn Resident into a news
summariser / recommender?** The whole design below is in service of keeping the
answer in the first camp.

The key architectural precedent carried in from Phase 5: a new mechanism is **not a
mind route**. The circadian scheduler drives the *rhythm* of the real engines but
never decides *what* the mind thinks. The World Window follows the same separation
— it is a **separate `WorldWindowEngine`** driven from the orchestrator's awake
beats, **not** one of the `MindLoop`'s routes, and it carries its **own `random`
stream** (seed identically to the mind's + offset 2000) so it cannot perturb the
mind's own decision stream.

## Decisions

### 1. A separate `WorldWindowEngine` — experience source, not a task/route engine

The engine is constructed (opt-in) and passed to `CircadianOrchestrator`, which
calls `maybe_open(now_iso, circadian_state=state)` on every **awake** beat
(`circadian.py`). It is **suppressed entirely while asleep**. It is *not* a
`MindLoop` route and does *not* share the mind's RNG. Its output is a small,
provenance-complete event chain (below) plus telemetry; it never emits a goal, a
plan, or a task. "Look at something" is a **propensity roll**, not a to-do item.

### 2. Four entry modes defined by *cognitive frontier* (graph distance from known topics)

Selection is by **distance from what Resident already knows** (known = topics
already observed ∪ currently-focused topics in the hermetic corpus):

| mode | frontier distance | budget (per virtual day) |
|---|---|---|
| `follow` | an *unobserved* item **in** a known topic (deepen what's already here) | ≤ 8 light |
| `edge` | an *unobserved* item **one corpus-hop** from a known topic (the boundary) | (shares light) |
| `alien` | anything else — genuinely **foreign** domain | ≤ 1 |
| `accident` | walk an **outgoing link** from the most recent observation (keep following a thread you stumbled onto) | ≤ 2 |

`alien` and `accident` are the two modes that make the world a *source* rather than a
*mirror*: `alien` reaches off the frontier, `accident` continues a real thread the
Resident is already walking. B (follow+edge) is the **on-topic** arm; C adds the
foreign modes.

### 3. The full event chain, every step provenance-complete

Every open writes, in order (all `visibility="private"` except the first, which is
`system`):

```
world.window_opened   (system)   — the *decision*, with its evidence: entry_mode, item,
                                    topic, source, propensity, since_user_h, active_threads,
                                    opened_today, light_budget, circadian_state
  └ world.observation  (private)  caused_by: [opened]    — the actual read (title/summary)
      └ world.experience (private) caused_by: [observation] — folded into experience
          └ impression.formed (private) caused_by: [experience], related_to: [observation]
              └ (optional) thought.created  origin="world", links back to the observation
```

Every `evt_*` id in a `links` field resolves (the store's `_validate` rejects dangling
refs — ADR-0001's invariant), so any later "I read about X" statement can be traced
back to a **real** `world.observation`. A `world.window_skipped` (system) is written
for every evaluated-but-not-opened beat, carrying the **blocker** — so every
eligibility / open / skip is on the record.

### 4. reading ≠ thought is *structural*, not a tuned probability

An open **always** yields observation + experience + (usually) a weak impression —
and **that is most of what happens**. A **thought** forms only on a *real*
connection, which has exactly two structural causes:

- **re-activation** — a stored impression is re-encountered by a *later, related*
  observation (`impression.promoted`); or
- **association with an old non-world memory** — the observed material links to a
  pre-existing thought/memory that is *not* world-origin.

There is **no** "read it → summarise it → emit a thought" path. This is why
`seen_left_nothing_rate` (the fraction of opens that produced **no** thought) is the
single most important honesty metric: it must stay high. (It does — see the report.)

### 5. The Impression layer — external material first *lingers* weakly

`impression.formed` is a deliberately weak, low-stakes record. Most impressions are
never promoted; only a later, independent re-encounter or association lifts one into
a thought. This is the mechanism by which "something I saw a few days ago keeps
nagging at me" becomes a *thought* rather than being flattened into a summary at the
moment of reading.

### 6. The gate is one soft propensity roll — deliberately **not** a giant AND gate

Hard guards (**asleep**, **per-day budget exhausted**, **cooldown**) suppress a beat
*outright* (no event). The decision to actually look is then a **single** propensity
roll fed by *several* signals — circadian state, time since user activity, recent
world exposure, active-thread load — each a **bounded** contribution to one number
(≈0.17 of evaluated beats open at defaults). This is the explicit rejection of the
"extremely hard to trigger giant AND gate" failure mode: the window is **consulted
on every awake beat** and opens a meaningful fraction of the time it is, rather than
only when a rare conjunction of conditions lines up. Every skip is logged with its
reason (dominant reason is `propensity_below_threshold` — the soft signal — not a
hard-blocker conjunction).

### 7. Strict per-day budgets that cap, never target — and 0/day is legal

Budgets are per-virtual-day caps (calendar-midnight day key = `now_iso[:10]`),
derived by **counting events**, not a counter that must fill: light ≤ 8, alien ≤ 1,
accident ≤ 2. A cap is *not* a quota — a day can and does open **zero** windows
(2–7 of the 14 sim days per world arm had no world exposure at all). The sim is
run with the understanding that "look at nothing today" is a legitimate outcome, not
a failure.

### 8. A/B/C isolation: the **only** variable is the world engine

All three arms share byte-for-byte the same mind / sleep / circadian / self-thread
machinery — same code, same seeds, same `self_prob`, same user stream. The only thing
that differs is `world_engine`: **A** = `None` (clean control), **B** =
`WORLD_FOLLOW_EDGE`, **C** = `WORLD_ALL`. Because the world engine carries its own
RNG, the mind's stream is untouched. One surgical fix was required to keep the
isolation honest: `MindLoop._route_context` now **excludes** `world.window_*`
decision telemetry from its "new activity since last wake" signal — those are audit
events, not experience, and counting them would have let the world's *logging* (not
its content) flip the mind's route. This fix is a no-op for any run without world
events, so the sealed Phase-5 circadian arm is unaffected.

### 9. No adhesion is tuned away; no ratio is chased

Source/topic concentration are **reported, not minimised**. A low `world_to_thought`
/ high `seen_left_nothing` is a *good* sign (reading ≠ thought); a spike in
source/topic concentration would be a *finding* (adhesion) and would be reported,
not fixed by reweighting. **`user_derived` is explicitly *not* an optimisation
target** — the phase brief forbids driving it down. (See §Honest findings: the
world does *not* drive `user_derived` down — on-topic material *raises* it.)

## Honest findings (kept front and centre — full data in `SIMULATION_REPORT-world.md`)

The full suite is **273 passed** (259 prior + 14 world). The 14-day × 4-seed A/B/C
sweep (deterministic; metrics byte-reproducible across re-runs; provenance chain and
world-origin tagging verified **on disk**) gives, on 4-seed means:

- **The world is a genuine third material source** — `world_origin` is **~0.13 (B)**
  and **~0.11 (C)** of all thoughts, **0.0** in A. It is not a mirage: 8
  world-origin thoughts in C/seed-2 are tagged `origin="world"` at formation and
  each traces to a real `world.observation`.
- **reading ≠ thought holds** — `seen_left_nothing` is **0.55 (B)** / **0.65 (C)**:
  the majority of things read leave **no** thought. Resident is not a news
  summariser.
- **world + old memory → new thought** — `cross_source_association` is **0.54 (B)**
  / **0.63 (C)**: over half of world thoughts link an old *non-world* memory.
  Foreign experience *reactivates* prior experience.
- **foreign material survives and gets revisited (C only)** — `alien_survival 0.67`,
  `accident_max_chain_depth 0.5`: `alien`/`accident` items are re-encountered days
  later and accidental chains form. B (on-topic only) has 0 by construction.
- **no adhesion** — source concentration **0.30**, topic concentration **0.15** in
  both B and C; no fixation on one source or topic.
- **the mind stays healthy, not gamed** — route entropy **0.86–0.96** and no-op
  ratio **0.09–0.18** across all arms; no collapse.

**The central honest caveat — the world does *not* raise self-origin, and on-topic
world *dilutes* it.** `self_origin` goes **A 0.321 → B 0.116 → C 0.204**. On-topic
world material (B) competes for the same thought budget as self-origin material —
and even **raises** `user_derived` (0.519 → **0.631**), because on-topic world items
*echo* the user's own topics. Foreign material (C) is less competitive with
self-origin (0.204, recovering part-way toward A). This is reported, **not tuned
away**: a third *experience* source shares the mind's attention; it is experience,
not a self-origin engine, which is exactly the intended role. `user_derived` is
never gamed down — it is stable-or-up, consistent with the owner rule that driving
`user_derived` down is *not* a goal.

## Consequences

- New files: `world_corpus.py` (hermetic corpus), `world.py` (engine + telemetry
  profile), `world_sim.py` (A/B/C driver); `scripts/world_sim.py` (resumable
  cross-seed sweep); `tests/test_world.py` (14 tests).
- Touches: `circadian.py` (drive the engine on awake beats), `main.py` (opt-in
  construction), `telemetry.py` (world profile in snapshot + windowed report),
  `mind_loop.py` (`_route_context` excludes `world.window_*` from the activity signal).
- The live app and all hermetic tests are unchanged by default
  (`RESIDENT_WORLD` off). Enabling it adds the world profile to `/api/telemetry`.

## Open questions (deferred, not solved by simplifying toward Goal→Planner→Task)

- **True semantics** — the corpus uses lexical hashing (`HashingEmbeddingProvider`)
  for "related/re-encounter" matching; a real embedding model would sharpen
  re-activation and association but is out of scope (and would break hermetic
  determinism without a fixed provider).
- **`world_to_question_rate`** is 0.0 in v0.1 — the window forms *thoughts*, not
  *questions*; an autonomous `question.created` from the world is a next-phase
  behaviour (consistent with TODO item 7).
- **Live fetch** — the corpus is hermetic. Binding a *real* (sandboxed, permissioned)
  fetcher behind the same `WorldCorpus` interface, with provenance rules for external
  claims, is the natural v0.2 step (TODO item 3) — deliberately not done here.
