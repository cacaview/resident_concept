# Instructions for Claude Code

Read `VISION.md`, `ARCHITECTURE.md`, `GENOME.md`, `EVENT_MODEL.md`, and `DEVELOPMENT.md` before changing architecture.

## Mission

Build **Resident v0.1 Proof-of-Life**, not a generic task agent.

## Hard constraints

- Preserve append-oriented event history.
- Claims about Resident's own life require provenance.
- Do not invent fake daily experiences.
- A wake cycle may legitimately do nothing.
- Do not encode personality as numerical meters.
- Interests should be inferred from behavior/history.
- User messages do not erase Resident's pre-existing mental state.
- Private projects may sleep or be abandoned.
- Self-modification is out of scope initially.
- External side effects remain sandboxed and permissioned.

## First implementation task

1. Make backend/frontend/tests runnable.
2. Implement EventStore append/get/list/since reliably.
3. Implement Chat + Life Timeline.
4. Implement a deterministic fake model provider for tests.
5. Implement a minimal MindLoop and scheduler while preserving no-op behavior.
6. Add a minimal Memory Retrieval interface.
7. Add Thought/Question/Thread persistence.
8. Implement Re-entry candidate selection from real events only.
9. Add tests proving restart persistence, no-op wake, linked thoughts, provenance-safe re-entry, and route recording.
10. For uncertain architecture, write ADR/TODO instead of simplifying toward Goal → Planner → Task → Done.
