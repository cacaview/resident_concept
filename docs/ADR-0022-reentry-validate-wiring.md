# ADR-0022 — Re-entry: wire the anti-fabrication guarantee into the production path

**Status:** Accepted (2026-09-17).

## Context

The module docstring of `backend/src/resident/reentry.py` makes a specific guarantee:

> Anti-fabrication guarantee: every candidate is derived *only* from real,
> already-appended events, and each candidate carries the ``event_ids`` that
> support its claim. ``ReentryEngine.validate_candidate`` rejects any candidate
> citing an event that does not exist in the store — a sentence about
> Resident's life must never outrun its provenance.

The predicate itself is tested (`test_validate_candidate_rejects_nonexistent_event`,
plus `validate_candidate` assertions across `tests/test_reentry.py` and
`tests/test_private_projects.py`). But the **production path never calls it**:
`POST /api/reentry` (`main.py`) → `ReentryEngine.run()` → `evaluate()` → persist.
`run()` appends whatever `evaluate()` returns straight into `reentry.proposed`
content without a single provenance check. The guarantee is a hollow shell on the
only path that matters: a candidate citing a nonexistent event is never rejected
by the engine — at most it is tripped up *incidentally* by the store.

The prior, partial protection is the `EventStore._validate` dangling-link check:
at `append` time it rejects links pointing at nonexistent events. That is not
sufficient coverage for this guarantee:

1. It covers only the `related_to` / `caused_by` **link graph**, not `event_ids`
   carried in candidate **content** in general.
2. When it fires, it aborts the whole append with an unstructured
   `EventStoreError` (`dangling event link: ...`) — and in `run()` the
   `reentry.evaluated` event has *already* been persisted by then, leaving a
   half-written run (an `evaluated` with no `proposed`/`skipped`) and a 500 at
   the endpoint. An abort is not a decision.

Today the candidates `evaluate()` *happens to* produce always resolve (each
`event_ids` is a single real event id read back from the store), so the hole is
latent — but the engine's contract is about every candidate it persists, and the
path that persists them performs no validation. This ADR closes the gap at the
point of persistence.

## Decision

Wire `validate_candidate` into `ReentryEngine.run()`, at the persistence
boundary — inside the engine, not in the controller:

1. **Validate every candidate before anything is written.** After
   `evaluate()` and before any `store.append`, each candidate is passed to
   `validate_candidate` (the existing, already-tested predicate — no new
   validation logic is invented). Candidates are split into `valid` and
   `rejected`.
2. **Rejected candidates are recorded, not raised.** Each rejected candidate
   produces one `reentry.skipped` event in the existing shape (visibility
   `system`, `links = {"caused_by": [evaluated.id]}`,
   `provenance = {"source": "reentry_engine"}`), with the content carrying the
   failure reason:
   `{"reason": "provenance_failed", "candidate_event_ids": [...],
   "missing_event_ids": [...], "candidate_type": ...}`.
   A rejection is thus a replayable event, not an exception.
3. **Rejected candidates never enter `reentry.proposed`** — not in `content`
   and not in the `related_to` links. The persisted `decision` and
   `candidate_count` are derived from the *valid* candidates only, with the
   same rule `evaluate()` uses (any strength-2 → `share_now`, else
   `mention_later`; none → `nothing_to_share`). When no candidate survives
   validation, the run degrades to the existing `nothing_to_share` shape,
   except the skip reason is the provenance failure.
4. **Result dict is additive.** New keys `rejected_candidates` (the raw
   candidate dicts) and `rejected_event_ids` (the ids of the rejection
   `reentry.skipped` events) appear **only when a rejection occurred**;
   `skipped_event_id` still names the run's skipped event (the last rejection
   skip when all candidates were rejected). No existing key changes meaning.
5. **`main.py` is untouched.** The guarantee is enforced inside
   `ReentryEngine.run()`, so every caller (the endpoint, any script, any test)
   gets it for free. The controller stays a thin passthrough.

## Consequences

- **The production path now honors the documented guarantee.** No sentence
  about the resident's life can be persisted through re-entry without
  resolvable provenance; a candidate that cites a nonexistent event is
  rejected *as a decision*, recorded with the missing ids, and never proposed.
- **Valid candidates behave byte-identically.** When no candidate fails
  validation, the events appended — `reentry.evaluated`,
  `reentry.proposed`, `reentry.skipped` — are unchanged in type, content,
  links, and provenance, and the result dict carries exactly the keys it
  carried before. All existing `test_reentry.py` and
  `test_private_projects.py` cases pass unmodified (no existing case
  fossilized the old "phantom candidate slips through" behavior, so none had
  to be rewritten).
- **Rejection is now a first-class, replayable record** instead of an
  incidental store abort: a `reentry.skipped(reason=provenance_failed)` event
  with the offending ids, appended after a complete `reentry.evaluated`.
  The store's dangling-link check remains as the deep backstop; it can no
  longer fire from `run()` because rejected candidates never reach a link.
- **Test evidence.** New tests in `tests/test_reentry.py` drive the
  production `run()` path with a candidate whose `event_ids` cite a
  nonexistent event: a mixed case (one valid + one phantom candidate) and an
  all-rejected case. Before the wiring they failed (the phantom candidate
  either crashed the run at the store's dangling-link check or would have
  been proposed unchecked); after the wiring both pass: the phantom is
  recorded as `reentry.skipped(reason=provenance_failed)` with the missing id,
  is absent from `reentry.proposed` content and links, and the all-rejected
  run yields `nothing_to_share` with no `reentry.proposed` at all.
