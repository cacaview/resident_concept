# Architecture v0.1

## High-level system

```text
                         ┌─────────────────────┐
                         │       WebUI         │
                         └──────────┬──────────┘
                                    │
┌───────────────────────────────────▼───────────────────────────────────┐
│                              residentd                               │
│                                                                      │
│ Observer → Event Store → Memory → Mind Loop → Exploration           │
│                 ↑          ↑          ↓              ↓               │
│                 └──── Provenance ← Private Workspace                │
│                                                                      │
│ Re-entry Engine   Open-endedness   Incubation   Self Monitor        │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │   Model Router      │
                    │ small / deep / emb  │
                    └─────────────────────┘
```

## Architectural rule

**The central loop must not be Goal → Planner → Task Queue → Tool → Done.**

Resident starts from observation and memory, chooses a cognitive route, may persist something, may explore, and may do nothing.

## Core modules

### Event Store
Append-oriented historical source of truth. Meaningful activity becomes an event. Derived views are rebuildable.

### Memory
v0.1 needs episodic retrieval, recency, semantic similarity hook, event links, thread links, and dormant/revisit candidates.

### Mind Loop
Reference lifecycle:

```text
sleep
 ↓
wake
 ↓
observe recent changes
 ↓
retrieve memories
 ↓
choose cognitive route
 ↓
continue / revisit / distant / serendipity / self / world / rest
 ↓
reflect
 ↓
nothing? ── yes → wake.noop → sleep
 ↓ no
persist thought/question/thread update
 ↓
optional bounded exploration
 ↓
persist experience/artifact
 ↓
sleep
```

### Open-endedness / Serendipity
Prevent one topic from swallowing the entire cognitive life. Route selection should support:

- `continuity`: strongest current thread
- `revisit`: old unfinished material
- `distant`: weakly related memories
- `serendipity`: cross-domain boundary sampling
- `self`: own history and runtime behavior
- `personal`: user-shared context
- `world`: external exploration
- `rest`: intentionally do nothing

### Incubation
Thoughts and questions can become dormant without being deleted, and later revive when new events resonate with them.

### Private Workspace
Resident owns a sandboxed workspace for journal, thoughts, projects, creations, reading, and self-experiments. v0.1 may mutate only this workspace.

### Provenance
Any factual claim about Resident's autonomous life must be backed by event IDs and, where relevant, artifacts/logs.

### Re-entry
When the user returns after an absence, Resident examines events since the last interaction and chooses among `share_now`, `mention_later`, or `nothing_to_share`. It must not invent activity.

### Conversation
Prompt assembly should include Resident's recent life and current thought state **before** the new user message. User input is important but must not erase pre-existing state.

### Self Monitor
v0.1 is read-only. It may inspect memory growth, topic concentration, wake/no-op ratio, project churn, and model/tool failures. No live self-modification yet.

## Models

Use provider interfaces, preferably OpenAI-compatible HTTP:

```text
small_model → cheap event extraction, routing, lightweight reflection
deep_model  → deeper thinking, coding, research, creation
embedding   → semantic retrieval
```

No architecture should depend on one model family.

## Scheduling

Support event-driven wake and low-frequency time-driven wake. Timer wakes should be jittered and skippable. Do not wake frequently merely to “look alive”.

## Security boundary for v0.1

Allowed: read/write Resident workspace, read explicitly mounted user dirs, sandboxed execution, optional public web browsing.

Disallowed: arbitrary host mutation, deleting user data, payments, contacting third parties, production changes, changing Genome.

## WebUI

Two top-level views, switched in the view bar:

- **Resident** (the face — the conversation view): 对话 Conversation,
  此刻的内心 Mind, Re-entry, 时间线 Life Timeline.
- **显微镜 · Monitor** (the microscope — strictly read-only: opening it
  triggers no retrieval, consolidation, or wake): Life Timeline, Provenance
  Inspector, Threads, Questions, Retrieval Trace, Continuity.

Projects and Creations are **not** current screens: they belong to the Phase 7
private-projects roadmap (ADR-0016) — machinery adopted as a candidate behind a
default-off flag (`RESIDENT_PRIVATE_PROJECTS`), with emission enabled only after
the shadow audit agrees and a natural genesis chain is observed (ADR-0016 §6).
The "Self" screen is realised as the read-only Monitor view (Self Monitor,
ADR-0013); Self Modification is explicitly deferred (v0.2 roadmap,
RELEASE-v0.1). There is no Permissions screen: the v0.1 security boundary above
is enforced policy-side, not in the UI.

Expose durable thoughts and events, **not hidden chain-of-thought**.
