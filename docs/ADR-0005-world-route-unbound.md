# ADR-0005: The `world` route is unbound in v0.1

Status: Accepted

## Context

ARCHITECTURE.md defines eight cognitive routes. `world` (external
exploration) requires bounded network access, sandboxing of fetched
content, and provenance rules for externally-sourced claims. v0.1 has
no external side effects at all (CLAUDE.md hard constraint).

## Decision

`world` remains a valid route in the route space, but the deterministic
provider never auto-selects it, and a `world` wake is a **legitimate
no-op** with the recorded reason "world route unbound in v0.1". The
route is therefore visible in route-distribution diagnostics as an
explicit "intentionally empty" category rather than being silently
removed.

## Consequences

- Wake-cycle tests can exercise `world` via scripted routes and assert
  the no-op path (no fabricated external experience ever enters the log).
- Bounded exploration (Phase 3) will bind `world` to a sandboxed
  fetcher; until then any `world`-routed activity is a provenance
  violation and must not exist.
- See `docs/TODO.md` (external exploration, embedding provider).
