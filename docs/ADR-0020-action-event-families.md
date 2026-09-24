# ADR-0020 — Action event families: objective deeds, subjective understanding, full provenance

**Status:** Accepted (Resident v1.0, 2026-09-15).
**Part of:** the Agency (ADR-0018 / ADR-0019). Defines which events an action produces
and how they keep the store's invariants (ADR-0001) and the owner's objective/subjective
separation (principle 6 & 7).

## The problem

An action must enter the unified Event Store with **provenance, causality, and
replayability** (principle 6): from the log alone one must be able to reconstruct *what
was intended, what the policy decided, what ran, what it produced, and what the outcome
was*. And it must respect principle 7: **Agency records what happened (objective); the
Mind later forms the thought / learning / question about it (subjective)** — the two
must not collapse into each other.

The store already enforces two invariants that the action families must honor:

- **Append-only, immutable** (ADR-0001) — an action is a new event, never a rewrite.
- **No dangling event links** (`EventStore._validate`) — every `caused_by` /
  `related_to` id that looks like an event (`evt_*`) must already exist.

## Decision — the event family

A single action produces a small, fixed set of events. Every one is append-only and
carries provenance + resolvable links:

| Event | actor | visibility | Content (objective facts only) | Links |
|---|---|---|---|---|
| `intent.proposed` | `user` (Phase A) / `resident` (Phase B) | `private` | `text` (the intent), `capability`, `source` | `caused_by`: the human conversation event (A) or the proposing thought (B) |
| `intent.rejected` | `resident` | `private` | `reason` (the policy's denial reason), `capability` | `caused_by`: [`intent.proposed.id`] |
| `action.started` | `resident` | `private` | `capability`, `tool_surface` (the tools the policy granted), `sandbox`, `policy` (policy version) | `caused_by`: [`intent.proposed.id`] |
| `action.completed` | `resident` | `shareable` | `summary` (the result text), `capability`, `tool_surface` (tools opened), `denied_tools`, `exit`, `num_turns`, `duration_ms`, `sandbox` | `caused_by`: [`intent.proposed.id`] |
| `action.failed` | `resident` | `private` | `reason` (`timeout`/`error`/`denied`/`budget`/`malformed_output`/`sandbox_violation`/`process_restart`), `capability`, `summary` (honest one-line outcome, e.g. `Read requires permission`), `detail` (raw stderr, often empty), `exit`, `num_turns`, `duration_ms`, `denied_tools`, `sandbox` | `caused_by`: [`intent.proposed.id`] (or the `action.started` child) |
| `action.tool_call` *(optional)* | `resident` | `system` | `tool`, `ok`, `bytes` | `caused_by`: [`intent.proposed.id`] |
| `experience.created` | `resident` | `private` | `summary` (the memory anchor, e.g. "行动：<intent>") | `caused_by`: [`action.completed/failed.id`] |

Minimal-terminal-state property: **every** action ends in exactly one of
`action.completed`, `action.failed`, or `intent.rejected`. Nothing disappears silently;
a denial is as recorded as a success. `action.started` is **non-terminal** — it records
that an allowed deed began executing (so a crash mid-run leaves a recoverable *open* deed
that startup recovery closes), and the deed still ends in exactly one of the three
terminal events; the property is unchanged. `intent.accepted` is deliberately **not** a
separate event — an allowed intent is simply one that proceeds to `action.*`, and its
acceptance (policy decision + capability) is recorded in the `action.*` provenance.

### Objective vs. subjective — the hard line

- The **Agency writes only the objective rows** above. Their content is *facts*: the
  capability, the tools that ran, the output text, the exit status, the duration, the
  sandbox path. There is no "meaning", no "how I felt", no interpretation in any
  `intent.*` / `action.*` field. `provenance` carries the machine record
  (`source: "agency"`, `runner: "py-claw"`, `policy_version`, `capability`, `sandbox`,
  `exit`, `duration_ms`).
- The **Mind writes the subjective rows, later and only in response.** In Phase B, after
  an action's objective events exist, a subsequent Mind `wake` may recall the
  `experience.created` anchor and produce a `thought.created` / `question.created` that
  *cites* the `action.*` events via `related_to` / `caused_by`. The subjective event is
  always **downstream** of the objective record (higher `seq`), never a rewrite of it.
  This is what makes action an *experience* the life can reflect on, rather than a task
  result it optimizes for — the distinction that keeps the Life Loop from degrading into
  a Goal → Tool → Done loop (ADR-0002, ADR-0018).

### Provenance & replayability

- **Causality** is a resolvable chain: `intent.proposed → action.* → experience.created →
  (later) thought.created`. Every hop is a real, existing event id, so the
  no-dangling invariant holds by construction and `monitor_provenance` walks the whole
  deed end-to-end.
- **Replayability:** from the log one can reconstruct the entire deed — the intent, the
  policy's decision and the capability it permitted, the tool surface, the output, the
  outcome. Re-running the same intent under the same policy and capability reproduces the
  *same authorization* (ADR-0019 is deterministic); the *output* may differ (real model),
  which is honest and recorded, not hidden.

### Re-entry

A **completed** deed surfaces on re-entry directly: `action.completed` is `shareable` and
a strength-2 candidate, so when the user returns the concrete deed (with its real output
`summary`) is "news about my day" — the life did something real with its tools. v1's
conservative surface is `read_only`, so a shareable deed is always a safe, read-only
report. The `experience.created` anchor stays `private`: it is the Mind's **recall** seam
(§memory anchor), not a re-entry candidate. The `intent.*` family and a *failed* attempt
(`action.failed`) are likewise **not** re-entry candidates: the intent is the *impulse*
and a failure is an unmet boundary — both are recalled and reflected on by the Mind, not
pushed to the user.

### The memory anchor

The `experience.created` event is what makes the action **recallable** by the Mind: it
has real `summary` text, so it enters the recall pool like any other experience, and
`record_activation` treats it like any recalled memory. This is the seam by which
"action → experience → recall → reflection" becomes a real loop (Phase A closes the loop
at *recall*; Phase B adds the *reflection* the Mind forms about it).

## Impact on existing experiments

None. These are **new** event families; no existing event type, schema, or family is
changed. The families are emitted only by the Agency (off by default), so sealed /
active-v1 / active-v2 runs are byte-identical. The one shared touchpoint is re-entry's
candidate/strength tables gaining `action.completed` — inert unless such an event exists
(i.e., only in `active-v3` / `RESIDENT_AGENCY=1` runs).

## Verification

- **Invariants test:** a completed action's full chain (`intent.proposed →
  action.completed → experience.created`) appends cleanly and every link resolves;
  `monitor_provenance` walks it end-to-end.
- **Separation test:** an `action.*` event contains no subjective field (content is
  facts + provenance only); a Mind thought about the action is a *separate* later event
  citing it (Phase B).
- **Denial test:** a denied intent appends `intent.proposed → intent.rejected` with no
  `action.*` and no subprocess launch.
- **Re-entry test:** a completed action is a re-entry candidate (via the experience
  anchor and/or `action.completed`); the sealed arms (no `action.*` events) produce
  identical re-entry output as before (the new candidate prefix is inert).
- **No-dangling test:** an `action.*` event that tried to cite a nonexistent intent id is
  rejected by the store (the invariant is not weakened by the new family).
