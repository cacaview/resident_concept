# ADR-0011 — World → Question: the question lifecycle

**Status:** adopted (v0.2 Step 3, 2026-09-12)
**Supersedes:** the reserved-but-unimplemented `question.created` slot of the
Phase-6 chain (ADR-0008 docstring) and the `world_to_question_rate: 0.0`
placeholder in `world_profile`.

## Context

v0.2 Step 3 extends the world chain `world → impression → "为什么?"`. The
owner's governing warnings (verbatim intent): this step touches **attention
and interest formation** — what makes the resident keep thinking about
something — and the failure mode to avoid is the "automatic question
generator": an LLM piped `world observation → generate a question` would
produce a digital five-year-old asking "为什么?" at every RSS item. The vast
majority of observations must keep producing **nothing**.

## Decision

### 1. A question is the residue of an unthought connection

A question can exist **only where a thought could have formed** — the same
evidence class the Phase-6 thought machinery already requires:

- **tension** (world ↔ world): a prior, *un-promoted* impression of connected
  material is re-activated by the new observation — two independent
  encounters (often from different sources) coexist without ever having been
  thought through together. Un-promoted is load-bearing: material a thought
  already covered cannot become a tension question — it is *unresolved* by
  construction.
- **gap** (world ↔ own memory): the new material associates with a real,
  non-world memory of the resident's.

Bare exposure — first contact with no connection — can never ask anything.
The formation order inside an open: the thought stage runs first and
unchanged; only an open that formed **no** thought may leave a question, and
only on a further low-probability roll (`question_prob`, same envelope family
as the other Phase-6 rolls). A deep-read-cap-blocked open leaves only an
impression: satiety includes not asking.

### 2. The engine owns existence; nothing is self-rated

Candidate detection is structural and deterministic; a single roll decides;
the text is a grounded template over the actual evidence (titles, sources,
the relation — *same topic* vs *corpus-linked adjacent*, named honestly). No
model is in the loop, and no importance/curiosity/score float exists anywhere
in the event. A question's value shows up only as **later re-meetings**
(derived citation graph), never as a self-assessed number.

### 3. Lifecycle: created → re-met → (derived) dormant

```
question.created  (private; caused_by → world.observation; related_to → evidence)
question.revisited (a later, INDEPENDENT experience hits the open question's
                    topic — honest bookkeeping like thread.revisited; open
                    questions only)
[7 days untouched → "dormant"]  — DERIVED state (question_state()), never a
                                  tombstone event; the log stays append-only
```

Salience = revisit count + last touch, both derived from the log. The
window cannot resurrect a dormant question (resurrection would need the
mind, not the window — a later step).

### 4. No answer pipeline

Nothing in the log is ever *caused by* a question except a re-meeting. A
question triggers no fetch, consumes no budget slot, schedules nothing, and
never routes into a `question → search → answer → done` loop. Enforced
structurally (the store's dangling-link invariant) and by test.

### 5. Default OFF; comparable, rollbackable

`WorldParams.enable_questions=False` (the sealed default): no question
machinery runs, no rng draw is consumed, `_open`'s return shape and
`world_profile` values are unchanged (the additive `questions` telemetry
block reads all-zero). The sealed 12-cell regression stays byte-identical.
Opt-in: `questions=True` on the sim, `RESIDENT_QUESTIONS=on` for the live
app. Experiment arms pass a **dedicated question rng** (`seed + 3000`) so
the question roll never perturbs the window-selection trajectory — in the
off-vs-on experiment the two arms observe the *same* 20 windows and form
the *same* 7 world thoughts; the only variable is the question layer.

## Consequences and boundaries

- Question events enter the event log, so they shift the mind's bounded
  recent-windows and route context like any mental event — a real, measured
  micro-dynamics change (see `docs/QUESTION_REPORT.md`; small: ≈6 extra
  events per 14-day life).
- `_recall_related_old_memory` still runs on the **legacy hashing**
  retriever, so gap questions inherit its known weakness: it pairs world
  material with the mind's own *route-meta* echoes instead of primary
  material — the exact "transcription vs the thing itself" finding of
  ADR-0010. Running this path through the semantic layer when the treatment
  is on is a recorded follow-up, deliberately not folded into this step.
- Mind-side re-meeting (a wake's recall surfacing an old question) is not
  yet recorded as `question.revisited` — v1 records world-chain re-meetings
  only; questions are retrievable events, so the mind can still re-find them.
- Not in this step, by order of the roadmap: question → private project
  (Phase 7's natural seed), question-driven attention for the world
  selector (Step 3 deliberately leaves "why go look" untouched).
