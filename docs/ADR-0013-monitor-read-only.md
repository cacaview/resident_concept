# ADR-0013 — The Self Monitor: a read-only microscope

**Status:** adopted (v0.2 Step 5, 2026-09-12)

## Context

v0.2 Steps 1–4 gave Resident a real world (input), real questions (curiosity
grown from experience), and continuity across absence (the other half of its
timeline). All of that is invisible from the outside: the existing WebUI is
Resident's *face* — the conversation. Living with it for 2–4 weeks (the gate
before Self Modification is even considered) requires a way to *observe*
without *touching*: a **microscope, not Resident's face**.

Two failure modes are designed out:

- the **cyberpet dashboard** — `Mood: 82% / Curiosity: 74% / Loneliness: 31%`
  would smuggle pseudo-personality numbers back in, re-inventing exactly the
  unearned internality the project removed. Where a score would go, the
  Monitor shows the *events* instead (revisits, activations, ages — all
  derived from the log; the payload-key blacklist is tested).
- the **observer effect** — a monitor that wakes the mind, triggers
  retrieval, or writes "UI audit" events would change the very thing it
  observes (the ADR-0010 lesson: an audit record inside the log shifts the
  mind's bounded windows and perturbs everything).

## Decision

**The Monitor may only READ facts.** Concretely, in the architecture:

1. `backend/src/resident/monitor.py` — pure projections of the event store
   (and, for the retrieval trace, of already-recorded sidecar files). No
   builder appends, retrieves, consolidates, or wakes. The test suite calls
   every builder and asserts the store is byte-count-identical, and sweeps
   every HTTP payload for banned pseudo-personality keys.
2. `main.py` exposes them as **GET-only** endpoints
   (`/api/monitor/timeline|provenance/{id}|threads|questions|continuity|retrieval`).
   No POST anywhere in the monitor surface. The HTTP test re-asserts the
   glass-window guarantee through the app layer.
3. The monitor never adds mind machinery for its own convenience. The recall
   QUERY, for instance, is only on record when the Step-2 semantic shadow is
   on (its sidecar); v1 does **not** add query logging to the mind's wakes —
   that would be new mind machinery just for the UI.
4. The frontend (`MonitorPage`) gets its own view toggle ("Resident" vs
   "显微镜 · Monitor"), polls slowly (5 s) with GETs only, and shows the
   read-only principle in its own header bar.

## The five views (the phase brief)

1. **Life Timeline** — every event including system noops (a wake that did
   nothing is as much a fact as a thought), filterable by type family.
2. **Provenance Inspector** — click any event; BFS back through
   `caused_by` / `related_to` and — for thoughts — `metadata.recalled`
   (the retrieval chain made visible): selected ← candidate ←
   question.revisited ← question.created ← evidence ← world observation.
3. **Threads & Questions** — active/dormant states, origin (user/self/world),
   revisit history; the question lifecycle from Step 3 (open/dormant,
   re-encounter counts, evidence resolved to types). No importance score.
4. **Memory / Retrieval Trace** — per thought: route, origin, and the
   material it actually used (type, origin, age). With the Step-2 shadow
   sidecar present: the same-query legacy-vs-semantic comparison —
   "transcription vs the thing itself" becomes directly observable.
5. **Continuity View** — absence windows assembled from the Step-4 fact
   layer: last seen → gap → candidate set (class + quote) → return message →
   selected/noop.

## Consequences

- Observation can never perturb the observed: the seal regression is
  unaffected (the monitor writes nothing), and the 2–4-week living period can
  be watched continuously without changing what is being watched.
- The Monitor's completeness is bounded by what the mind already records.
  Gaps (e.g. recall queries without the shadow on) are *displayed as gaps*,
  not papered over with new instrumentation — new instrumentation is a mind
  change, and goes through the same measured-process discipline as Steps 2–4.
