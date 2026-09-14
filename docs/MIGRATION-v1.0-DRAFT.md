# MIGRATION — v1.0 — **DRAFT (NOT a release claim)**

Draft 2026-09-13. Companion to `docs/RELEASE_NOTES-v1.0-DRAFT.md`. v1.0 is
**not releasable today**; these steps describe the intended migration path
only.

## 0. Prerequisites

- Existing deployment with the sealed event store intact (tags `v0.1` /
  sealed regression artifacts untouched; never rewrite `backend/scripts/out/`
  archives).
- Flag-off invariance is the safety net: with no new env vars set, behaviour
  is byte-identical to sealed v0.2 (12/12 sealed regression cells).

## 1. Profile activation (`active-v2`)

- Profiles are selected via the existing profile mechanism (ADR-0014;
  `RESIDENT_PROFILE`-style selection as in current code — the registered
  names are `sealed-baseline`, `active-v1`, `dense-debug`, and the new
  `active-v2`).
- `active-v2` = active-v1 rhythm + `recall_pool_hygiene` (E2) +
  `thread_selection: material` + `context_exclude_prev_thread` (VA). These
  cognitive flags are defaults **inside the profile only**; no env var turns
  them on globally, and the MindLoop/SleepEngine defaults stay `False`.
- Timing: ADR-0017 promotion is **prospective** — switch the live process to
  `active-v2` only after the observation window closes (Day 7 = 2026-09-19)
  and the day-7 reading supports it.

## 2. Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `HARNESS_SNAPSHOTS` | Directory of immutable content-addressed replay-world snapshots for harness runs (the 78 multisrc captures live in `backend/scripts/out/harness_snapshots`) | unset (harness-only; the live process is unaffected) |
| `RESIDENT_PRIVATE_PROJECTS` | Phase 7 private-projects detector/emission flag (ADR-0016) | **off** — do not enable until a natural live H1 chain is observed and the shadow audit agrees |
| `RESIDENT_WORLD_SOURCE` | World source seam: fixture / replay / live (ADR-0009) | fixture; live is opt-in |
| `RESIDENT_AUTO_WAKE` | Autonomous circadian wake loop (ADR-0007) | off |

Note: `HARNESS_SNAPSHOTS` affects offline harness/experiment scripts only;
pointing it at `backend/scripts/out/harness_snapshots` reproduces the
multi-source replay world. It never touches the running resident.

## 3. Relay / model provider config

- The real-model path needs the relay: `<legacy-host>:8002` (qwen,
  OpenAI-compatible relay). Configure via the existing provider env
  (`RESIDENT_MODEL_BASE_URL` + `RESIDENT_MODEL_API_KEY` +
  `RESIDENT_MODEL_NAME`); without all three, the deterministic fake provider
  stays the default and tests remain hermetic.
- **Known outage:** the relay has been DOWN since 2026-09-13 ~06:21 UTC. The
  resident wake loop keeps running and honestly noops ("model provider
  unavailable") — this is correct failure accounting, not a bug. No migration
  action is required beyond restoring the relay host; do not disable the
  accounting or force substantive wakes while it is down.

## 4. Safety invariants to preserve during migration

- Flag-off ⇒ byte-identical dynamics (verified: sealed regression, wake-stream
  identity, determinism fingerprint `7136241b`).
- E2 hygiene excludes at retrieval time only — the event log is never
  rewritten.
- `RESIDENT_PRIVATE_PROJECTS` default-off; enabling it only changes what
  happens on a genuine genesis (one appended `project.created` event).
- Sealed tags and all historical experiment archives are immutable.
