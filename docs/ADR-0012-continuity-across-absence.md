# ADR-0012 — Continuity across absence

**Status:** adopted (v0.2 Step 4, 2026-09-12)
**Redefines:** the roadmap's "Real Re-entry / Absence Detection" item.

## Context

The naive reading — *user leaves → record how long → user returns → summarise
the gap* — is the Daily Brief trap. The actual question: when the
**conversational** timeline breaks, how does Resident notice that real time
passed, and which of the things that **actually changed it** during that gap
are still alive when the user speaks again? Not "what I did while you were
away" but "which things that happened while you were away are still alive now
that you're back."

## The five owner constraints, mechanically

1. **Absence is a fact, not an emotion.** `absence.detected` (system) records
   `gap_s` and the two boundary event ids — nothing else. No sentiment field
   exists anywhere in the module (exact-key-set tested). The threshold
   (default 4 h) is an envelope rail: a shorter gap is the same conversation.
2. **Re-entry ≠ summary.** Nothing compresses the gap into a broadcast.
   Candidate texts are *quotes* of real evidence. Candidate granularity is
   one fact per entity per absence (a thread re-surfacing twice in one gap is
   one fact).
3. **Only state-changing experiences qualify.** The candidate classes are the
   structural signatures of "this changed me": `thread_revisited`
   (a thread resurfaced), `question_revisited` (a question re-met — Step 3's
   layer feeding Step 4), `impression_collision` (an impression matured into a
   promotion), `self_thought_extended` (a self-origin thought continued a
   chain), `world_changed_mind` (world material genuinely entered a thought —
   the thought's `recalled` cites world-family material), `went_dormant`
   (derived: open at the gap's left edge, dormant at the return). Plain wakes,
   skips, fetch failures and user-derived thoughts are **not** candidates —
   they grew from the user, not from the absence.
4. **Silence on return is first-class.** No qualifying change →
   `reentry.noop`. Candidates exist but the return message recalls none →
   also `reentry.noop`. 发生过 ≠ 值得现在提.
5. **"Happened" and "worth mentioning now" are separate judgments.**
   Eligibility is structural (the scan above). Current relevance is the
   return message's **own retrieval footprint** — `memory.semantic_near(msg)`:
   a candidate is selected iff the message's recall reaches its evidence
   *chain* (the event, what it grew from, or — for questions — the primary
   material it grew from). Deterministic, no LLM, and the same retrieval the
   mind itself uses on a `personal` wake. Provenance decides truthfulness;
   conversational context decides aliveness; the two never merge into one
   model judgment.

## v1 boundary: the fact layer only

`absence.detected` / `reentry.candidate` / `reentry.selected` /
`reentry.noop` — and nothing else. **No "欢迎回来" language is generated**:
how to say something is a later, separate question from what is true. The
existing Phase-2 `ReentryEngine` (share-candidate promotion) is untouched.

## Wiring

- Live app: `RESIDENT_CONTINUITY=on` constructs the engine; `/api/chat` runs
  `on_user_return` at the moment the user speaks (it records facts; it does
  not shape the reply) and returns the summary additively.
- Sims: `run_world_simulation(..., continuity=True)`; default off — the
  sealed arms never run this code and the sealed regression stays
  byte-identical.

## Known properties and follow-ups

- **Selection is weakly discriminative in v1.** The experiment (see
  `docs/CONTINUITY_REPORT.md`) shows the message footprint selecting the same
  class in most cells: the store's meta-transcriptions ("[continuity] 继续
  线程…", promoted copies) crowd the retrieval top-k, so any message grazes a
  world chain. This is ADR-0010's "transcription vs the thing itself" playing
  out at the re-entry layer. Follow-ups, deliberately not folded in here:
  route-shaped selection (the mind's own wake recall), and running the
  selection retrieval through the Step-2 semantic layer.
- A continuation phrase like "刚刚那个问题继续" has no lexical bridge to any
  candidate — it noops. Honest for v1.
- Run-level labels (sleep-seeded self-thread ids, uuid4 event ids) are random
  per run: lives are *behaviour*-reproducible, not id-reproducible. The
  experiment's validity gate compares id-masked projections for this reason.
