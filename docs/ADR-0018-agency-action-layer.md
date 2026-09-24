# ADR-0018 — Agency: a bounded action layer outside the Life Loop

**Status:** Accepted (Resident v1.0, 2026-09-15).
**Scope:** adds an *Agency* to the Resident System. The Mind, Memory, World, and the
Life Loop are **not** modified by this ADR (see ADR-0020 for the event families the
Agency emits, and ADR-0019 for the policy that gates it).

## The problem

The owner's goal for v1.0 is a long-lived digital individual that both *lives*
(observation → recall → reflection → persist/noop, ADR-0002) **and** can take real
Claude-Code-grade actions using the existing `py-claw` runtime. The hard constraint is
that this must **not** turn Resident into a Goal → Tool → Done agent: the Life Loop is
the identity, and action is an *organ*, not a new central loop.

Two sub-problems:

1. **Coupling.** `residentd` (the life) and `py-claw` (the action runtime) are separate
   Python projects — different runtime, different config surface, different version.
   They must be unified into one *Resident System* without becoming two codebases that
   import each other.
2. **Authority.** The Mind can form a thought and even *notice* an opportunity to act,
   but it must not be able to grant itself the ability to act. Permission must come from
   something outside the model's own decision (owner principle 1 & 3).

## Decision

**Agency is a self-contained module (`backend/src/resident/agency/`) that lives in the
resident process but drives `py-claw` as an isolated subprocess.** The real process
boundary is **residentd ↔ py-claw**, and it is a *subprocess* boundary — not an import.

Concretely:

- The Agency exposes one narrow interface — `AgencyService.intake(intent) -> result` —
  covering intake → policy → execution → result. Nothing else in the life calls it.
- It invokes `py-claw` the way its own CLI is meant to be invoked
  (`py-claw --print --output-format json`), in a **per-action sandbox working
  directory**, under a **timeout** and a **turn/cost budget**, with a **permission
  settings file** that allow-lists only the tools the (policy-approved) capability
  permits. In py-claw's headless mode any tool the permission engine leaves as `ask`
  is silently denied, so an allow-list is conservative by construction.
- The Agency **never imports `py_claw`**; it only spawns the `py-claw` binary and reads
  its stdout/result. This is the "no mutual imports" constraint, met at the only
  boundary that matters (life ↔ action runtime).
- **All Event Store writes stay inside residentd.** The Agency appends `intent.*` /
  `action.*` events to the *same* `EventStore` instance the life uses (single writer,
  append-only invariant and the dangling-link check are preserved trivially).
- **Endpoint isolation.** The py-claw subprocess is driven against the endpoint the
  Agency is *deployed with* (env `RESIDENT_AGENCY_PYCLAW_URL` / `_KEY` / `_MODEL`): each
  run gets a per-action `XDG_CONFIG_HOME` whose `py-claw/config.json` names that
  endpoint, so the Agency never silently relies on — or mutates — the user's global
  `~/.config/py-claw/config.json`. Unset, it falls back to py-claw's default config.
- **Prompt names the granted surface, grants nothing new.** The executor prompt tells
  the model exactly which tools the policy permitted (Read/Glob/Grep for `read_only`),
  so it works *within* its bounded surface instead of burning turns reaching for a
  denied tool (e.g. `Bash`). The allow-list remains the hard gate (ADR-0019); naming the
  surface only informs the model of what it may already use.

### Why this shape (and not the alternatives)

| Alternative | Rejected because |
|---|---|
| `residentd` imports `py_claw` and runs its `ToolRuntime` in-process | Couples two runtimes (venv/version/config mismatch); a bug or hang in the action path can corrupt the life; violates "reuse, don't reimplement" by forcing residentd to orchestrate tools itself. |
| Make Agency a *Mind route* (an `act` route) | Reintroduces the Goal → Tool → Done shape ADR-0002 forbids: the mind would *choose* to act as a cognitive route, coupling selection and execution. |
| A fully separate Agency OS process speaking HTTP both ways | Maximum isolation, but doubles the plumbing (intake **and** result over the network, a second store connection, extra process lifecycle) for no benefit at this stage, since the heavy/external execution is *already* an isolated subprocess. Kept as a documented **future** option, not the MVP. |

The subprocess boundary gives the three properties that actually matter: **reuse**
(principle 5 — py-claw is driven exactly as a py-claw), **isolation** (a hung/failed
action is a subprocess we can time out and kill; it cannot take the life down), and
**authority** (the policy, below, sits between the intent and the subprocess).

## The invariant: Life Loop ≠ Agency Loop

- The **Life Loop** is unchanged: `wake → assemble → route → recall → reflect →
  persist/noop` (ADR-0002). It has no `act` route and no call into the Agency.
- The **Agency** is a peripheral organ with its own lifecycle: it is *invoked* with an
  intent, runs its policy, executes, and writes objective events. It does not wake,
  recall, or reflect.
- An intent reaches the Agency by **one of two** doors, neither of which is "the Mind
  decided to act":
  1. **Phase A (this milestone):** a *human* supplies the intent (`POST /api/agency/intent`).
     The person, not the model, is the agent of the act.
  2. **Phase B (implemented):** the Mind's `reflect` may emit an *optional*
     `intent` field in its output. That intent is **proposed**, not acted upon: it is
     routed to the Agency's intake (by the circadian, as a background task that cannot
     block a life beat), where the **deterministic policy** (ADR-0019) decides
     allow/deny. The Mind never grants the permission; it only states an opportunity.
- Objective events (`action.*`) are written by the Agency; subjective understanding
  (thought/question/reflection *about* the action) is written later by the Mind, citing
  the `action.*` events (ADR-0020). Action is therefore *experienced*, not *decided* —
  which is what keeps it a life and not a task loop.

## Default-off, and why that preserves the sealed regression

The Agency (its service, endpoints, and any event-family wiring) is enabled **only** when
the active `LivingProfile` carries the `agency` cognitive flag (the `active-v3` profile)
or when `RESIDENT_AGENCY=1` is set explicitly. Every existing profile
(`sealed-baseline`, `active-v1`, `dense-debug`, `active-v2`) leaves the flag off, so the
life's construction path is byte-identical and the sealed regression is untouched.

## Impact on existing experiments

None. The life loop, the event store, the world window, memory, and every existing event
family are unchanged. The Day-7 experiment stays paused (per the owner's directive) until
the Agency's closed loop passes its own regression and an independent verification.

## Verification

- **Hermetic unit tests** for the policy (every capability → allow/deny tier) and the
  runner (fake `py-claw` on PATH): no network, no real model, deterministic.
- **End-to-end Phase A loop test:** a human intent is admitted, policy-allowed, executed
  by a (fake) py-claw, and the result lands as `intent.proposed → action.completed →
  experience.created` in the store with a resolvable `caused_by` chain; a subsequent
  Mind `wake` recalls the experience.
- **Sealed regression** unchanged (flag-off is byte-identical).
- **One real smoke** against the configured py-claw (qwen backend), run manually and
  recorded — not part of the hermetic suite — proving the subprocess boundary actually
  executes a real Claude-Code-grade task: a `read_only` intent completed end-to-end
  (real model turn, `action.completed` + `experience.created` with a resolvable
  `caused_by` chain), and a `Bash`-reaching attempt was denied by the allow-list.

## Launch, monitor, and recovery

**Launch.** Two equivalent ways to mount the Agency on a running life:

- **Profile (preferred):** run with `RESIDENT_PROFILE=active-v3` — the profile carries
  the `agency` flag plus the active-v2 retrieval hygiene, and mounts the Agency with the
  conservative default capability set (`{read_only}`, 300 s timeout).
- **Explicit env:** any profile + `RESIDENT_AGENCY=1` (e.g. test the Agency on
  `sealed-baseline` cognition without its hygiene flags).

The executor is the `py-claw` binary; its endpoint/key/model are deployment-controlled
and never read from the life's config unless you say so:

| Env | Default | Meaning |
|---|---|---|
| `RESIDENT_PYCLAW_BIN` | `py-claw` | the py-claw executable to spawn |
| `RESIDENT_AGENCY_SANDBOX` | `<data>/agency_workspace` | per-action sandbox root |
| `RESIDENT_AGENCY_TIMEOUT_S` | `300` | wall-clock cap per action (ADR-0019) |
| `RESIDENT_AGENCY_CAPABILITIES` | *(empty → `{read_only}`)* | comma list of granted capabilities |
| `RESIDENT_AGENCY_PYCLAW_URL` / `_KEY` / `_MODEL` | *(empty → py-claw's own config)* | per-run endpoint (isolated `XDG_CONFIG_HOME`) |

**Phase A door (human intent):** `POST /api/agency/intent`
`{"text": "...", "capability": "read_only", "related_event_ids": ["evt_..."]}` →
`200` with the terminal outcome (`completed` / `failed` / `rejected`), or `503` when the
Agency is not mounted for the profile.

**Phase B (Mind-proposed):** no endpoint — when the Agency is mounted, the Mind's
reflection may emit an optional `intent`, which the circadian routes to intake in the
background (source `mind`); the deterministic policy still gates it.

**Monitor.** Read-only, like the rest of the life (ADR-0013):
- `GET /api/agency/health` → `{enabled, policy, allowed_capabilities, timeout_s}`.
- The Event Store: every action is a small, resolvable chain — filter on
  `intent.proposed` / `action.completed` / `action.failed` / `intent.rejected` /
  `experience.created`. A life with a mounted Agency but no recent actions shows none
  (proposals are rare by design; no-op is the common, legal outcome).

**Recovery.** An action is an isolated subprocess: a hang hits `RESIDENT_AGENCY_TIMEOUT_S`
and is killed, recorded `action.failed(reason=timeout)`; a crash/exit is recorded
`action.failed`. One action at a time (in-flight lock), so a failure cannot cascade into
the life or pile up processes. The sandbox is per-action and disposable; the store is
append-only, so a failed action leaves a clean, replayable record — no state to roll back.
