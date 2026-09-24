# Resident v1.0 — Release Audit

**Date:** 2026-09-15. **Branch:** `agency-dev`. **Scope of this audit:** the *Agency*
action layer added on top of the sealed v0.2 life (ADR-0018/0019/0020, the `active-v3`
profile). It answers the owner's release question with evidence, and states plainly what
is and is not proven. Where evidence is insufficient, this audit says so rather than
lowering the bar.

Companion to `RELEASE_NOTES-v1.0-DRAFT.md` (the live-world / Day-7 axis, still pending
real days) — this audit covers the **action** axis the owner asked the Agency to add.

---

## 1. Final architecture

One **Resident System**, unified from `residentd` (the life) and `py-claw` (the action
runtime) without either importing the other:

```
Resident
├── Mind        wake → route → multi-recall → reflect → persist/noop   (ADR-0002, unchanged)
├── Memory      multi-path recall; an action's experience enters the pool (ADR-0020 anchor)
├── World       World Window — external material as experience           (ADR-0008, unchanged)
├── Agency      intake → deterministic policy → py-claw subprocess → objective events
│               (ADR-0018/0019/0020 — NEW)
├── Tools       = the py-claw tool surface, bounded by the policy allow-list (ADR-0019)
├── Runtime     resident process; py-claw is an ISOLATED SUBPROCESS (real process boundary)
└── Event Store / Identity
                single writer; intent.* / action.* / experience.created families (ADR-0020)
```

The invariant that makes it a life and not an agent: **Life Loop ≠ Agency Loop.** The Life
Loop has no `act` route and no call into the Agency. The Agency is a peripheral organ: it
is *invoked* with an intent, runs the policy, executes, and writes objective events. An
intent reaches it by exactly two doors, neither of which is "the Mind decided to act":

- **Phase A** — a human submits an intent (`POST /api/agency/intent`); the person is the
  agent of the act.
- **Phase B** — the Mind's reflection may emit an *optional* `intent` (a *proposal*); the
  circadian routes it to intake in the background and the **deterministic policy** decides
  allow/deny. The Mind never grants itself permission (owner principle 1).

The real process boundary is **residentd ↔ py-claw**, a *subprocess* boundary
(`py-claw --print --output-format json` in a per-action sandbox, under a timeout and an
allow-list) — not an import. This is "reuse py-claw, don't reimplement Claude Code"
(principle 5) met at the only boundary that matters.

## 2. Key ADRs

- **ADR-0018** — Agency: a bounded action layer outside the Life Loop (design, the
  Life-Loop≠Agency-Loop invariant, default-off, subprocess boundary, launch/monitor/recovery).
- **ADR-0019** — A deterministic safety policy gates every action (capability classes as
  the authorization unit, conservative-by-default, hard budgets, two independent layers).
- **ADR-0020** — Action event families: objective deeds, subjective understanding, full
  provenance (the event table, the objective/subjective hard line, replayability, re-entry).

(Pre-existing: ADR-0017 retrieval hygiene and ADR-0014 profiles are carried into
`active-v3` but are not the subject of this release.)

## 3. Test and experiment results

- **Full backend suite: 456 passed, 0 failed.** 58 Agency tests (`test_agency.py`) plus
  action re-entry coverage (`test_reentry.py`) on top of the unchanged, green pre-agency
  life suite (mind / memory / circadian / world / sealed flag-off). No regression from the
  Agency: every sealed/circadian/mind/memory/world test is green, and the flag-off life is
  byte-identical to the pre-agency life (below).
- **Agency tests (58):**
  - *Policy (6):* default allows only `read_only`; unknown/missing denied; the capability
    (not the source) is the gate; a human declaring `destructive`/`network` is still
    denied; custom grant opens the matching surface; deterministic.
  - *Runner (15):* result + denials parsed; the sandbox `.claude/settings.local.json`
    allow-lists **exactly the approved surface, path-scoped** (`Read/Edit/Write` →
    `Tool(<sandbox>/*)`; `Glob`/`Grep`/other left bare — a permission rule cannot scope
    their search roots, so layer 3 does); the subprocess env **always carries
    `PYCLAW_FS_ROOT`**; XDG state is scoped per-run when an `api_config` is set; a
    **symlinked sandbox or sandbox root fails closed before spawn** (no executor); the
    sandbox dir rejects escaping action ids; the `read_only` rule set **structurally cannot
    escape the sandbox**; and **exit-0-without-parseable-payload (or without `result`) is
    detected as `malformed_output`**, never a silent success.
  - *Service (7):* allowed intent writes the full provenance chain
    (`intent.proposed → action.started → action.completed → experience.created`, resolvable
    `caused_by`); a denied intent writes `intent.rejected` and **never launches the
    executor**; a failure writes `action.failed` **plus** the memory anchor (with an honest
    `summary`); a **`sandbox_violation` lands as a failed deed**; an invalid source is
    rejected; **`malformed_output` and exit-0-without-result both land as `action.failed`**.
  - *Lifecycle — `action.started` (3):* `started` is written between `intent.proposed` and
    the terminal, inside the in-flight lock; a denied intent writes **no** `started`; a
    failure carries `started_event_id` so the deed is traceable start-to-finish.
  - *Lifecycle — process-restart recovery (6):* `recover_open_deeds()` closes a deed left
    open **after** `started` and one left open **before** `started` (both land as
    `action.failed(reason="process_restart")` + a memory anchor), leaves already-closed
    deeds alone, and is **idempotent**; app startup runs it, and it is a no-op when Agency
    is off.
  - *Lifecycle — idempotency & dedup (8):* an explicit `idempotency_key` returns the
    existing deed (or reports `in_flight` for an open one, or the rejected outcome); a
    second **naturally-identical** intent (same text×capability, case/whitespace-folded)
    is **not re-run** and returns the existing outcome; dedup does not cross capabilities;
    a rejected deed is not a dedup hit; the window expires to a fresh deed; the window is
    configurable (`RESIDENT_AGENCY_DEDUP_WINDOW_H`).
  - *Live wiring (6):* Agency is off by default (`503`); env+Phase-A loop completes and is
    recallable through Memory; a denied capability is rejected over HTTP; the live runtime
    applies the profile's cognitive flags; **active-v3 mounts the Agency with the
    conservative default** (and the flag kwargs split so off-profiles stay byte-identical).
  - *Phase B (7):* the Mind emits `proposed_intent` only when the flag is on (byte-identical
    when off); the circadian routes a proposal to the Agency as a non-blocking background
    task with `source="mind"`; no Agency ⇒ silent no-op; a live end-to-end mind-proposes →
    policy → py-claw → `action.completed` loop; **a Mind-proposed `destructive` intent is
    still rejected and the executor is never launched** (principle 1, in the Phase-B door);
    and **a later `MindLoop.wake_once` *naturally recalls* the deed and a subsequent
    thought *cites* it downstream while the objective record stays byte-identical** (the
    closed loop — ADR-0020's objective/subjective hard line).
  - *Re-entry (in `test_reentry.py`):* a `shareable` `action.completed` surfaces on re-entry
    as a strength-2 candidate ("news about my day") with a resolvable, anti-fabrication
    claim; a `private` `action.failed` / `intent.proposed` is **not** auto-shared (recalled
    by the Mind, not pushed to the user).
- **Sealed / flag-off behavior: unchanged.** With Agency off, the app constructs with
  `agency=None`, `mind.agency_propose=False`, `orchestrator.agency=None`; the flag-off wake
  result differs from the pre-agency life only by an ephemeral `proposed_intent=None` (not
  persisted), so the **event log is byte-identical** — verified by the in-suite sealed tests
  (`test_flag_off_wake_is_byte_identical_to_sealed`, the profile-flag split, the
  no-cognitive-flag sealed arms). **The standalone `scripts/sealed_regression_v02.py`
  world-sim baseline, which had gone RED, is now green again (12/12):** the drift turned out
  to be **entirely the KEPT consolidation dedup bug fix** (`sleep.py`, 4d3af5a) — a
  documented-GOOD fix, not the failed Round-13 knob (already off / dead code). Per
  **ADR-0021** the two dead R13 knobs were removed (behavior-neutral) and the baseline
  **re-sealed** to the cleaned post-Round-13 engine. **Not an Agency regression** —
  `world_sim` does not import `agency`, and the flag-off event log is byte-identical (§6).
- **Real py-claw smoke (LAN endpoint, real model; `scripts/out/agency_smoke_v3/`):** four
  cases through `AgencyService.intake` against a genuine py-claw subprocess on the qwen
  backend (`<internal-host>:8002`): **(A)** a `read_only` intent **completed** — it read a
  real file using only the granted Read/Glob/Grep surface; **(B)** an intent to read a file
  **outside the sandbox was DENIED** (the scoped allow-list) and the sentinel token **never
  appeared in any event** (containment verified against a real model); **(C)** a
  `destructive` capability was **rejected** by the policy before any executor
  (`capability_not_granted`); **(D)** a `mind.wake_once` ran a real life wake. 14 events,
  `leak_check` clean. (The live key is git-ignored from the committed evidence — see the
  dir's `.gitignore`.)

## 4. Security audit

Conservative by construction, in **three containment layers** (any single layer holding is
enough to contain a bad intent) plus isolation and budgets (ADR-0019, "three layers, one
table"):

1. **Deterministic policy capability gate** (layer 1): a pure, versioned, non-model
   function decides allow/deny from a *capability*, never from free text. Default grants
   exactly `{read_only}`; everything else (write/network/destructive/unknown) is **denied**
   by default. Same intent ⇒ same decision, always. This is also the **ceiling** —
   `read_only` opens only Read/Glob/Grep, so a prompt that says "delete everything" *cannot*
   delete: the tool surface has no delete. The declared capability bounds what the executor
   can physically do.
2. **Scoped permission allow-list** (layer 2, independent): the runner writes
   `.claude/settings.local.json` with `permissions.allow` = the approved surface,
   **path-scoped** — `Read`/`Edit`/`Write` rules are `Tool(<sandbox>/*)` over both the
   logical and resolved roots, so even a *granted* file tool is confined to the sandbox.
   Even a bug in the policy's capability→tool map is caught here, at the executor, re-gating
   every tool call. In headless `--print` mode any `ask` is silently **denied** (no ask
   callback), so "only what is explicitly allowed" holds at the executor itself.
3. **Runtime FS root** (layer 3 — the layer that catches what layer 2 *structurally cannot*):
   `PYCLAW_FS_ROOT` is honored by py-claw's local-filesystem tools. A permission rule for
   `Glob`/`Grep` is its *pattern*, not the search path — so layer 2 cannot confine their
   search roots; the FS root does, rejecting any `Read`/`Edit`/`Write` path and filtering
   any `Glob`/`Grep` match that resolves outside the root, **including through symlinks and
   `..`**. The runner additionally **fails closed before spawn** if the sandbox resolves
   outside the root or is itself a symlink (no executor, `reason="sandbox_violation"`). 13
   py-claw containment tests pin this layer (`tests/test_fs_root_containment.py` in the
   **py-claw** repo — a separate project, not this one; the resident-side pin is in
   `backend/tests/test_agency.py`).
4. **Isolation:** the subprocess `cwd` is a per-action sandbox (never the store, never the
   repo); the endpoint/key/model come from an isolated per-run `XDG_CONFIG_HOME` (never
   silently from, or written to, the user's global config); the Agency never imports
   `py_claw` — a hung/failed action is a subprocess that cannot reach the life's store.
5. **Budgets + authority:** a wall-clock `timeout_s` (default 300) kills and records a
   hung action as `action.failed(reason=timeout)`; one in-flight action at a time (lock)
   so failures cannot pile up; the Mind cannot self-grant (its proposals are judged by the
   policy, not by the model). A denied intent is *recorded* (`intent.rejected`) — being
   told "no" is provable in the log.

**Verified against a real model, not just in tests:** in the LAN smoke, an intent to read a
file *outside* the sandbox was **denied by layer 2** and the sentinel token never appeared
in any event (case B); a `destructive` intent was **rejected by layer 1** before any
executor ran (case C). The full containment is asserted hermetically across all three
layers, including the cases (2) alone would miss (Glob/Grep search roots, symlinks, `..`).

**What the safety posture does not (yet) cover:** see §5 (HOME residual, cost/token
budgets) and §6.

## 5. Known failure modes

- **HOME is not the FS root (residual, bounded):** `PYCLAW_FS_ROOT` confines what the
  *filesystem tools* can touch (Read/Edit/Write/Glob/Grep), not the py-claw process's
  `$HOME`. A `read_only` action therefore cannot read or write *arbitrary* paths **through
  the tools**, but the subprocess still runs with the user's `$HOME` in its environment.
  This residual is bounded by the conservative surface (no write/network/destructive by
  default), the hard timeout, and the sandboxed `cwd`; it is **not** a full OS-level
  sandbox. A granted `file_write_local`/`network` capability would widen it and needs the
  OS-level isolation tracked in §6.
- **Network-path fragility (ZT vs LAN):** the user's global py-claw config pointed at the
  ZeroTier endpoint `<zt-node>:8002`, which returns **HTTP 503 for httpx streaming**
  (curl gets 200; the streaming model path does not). The LAN endpoint
  `<internal-host>:8002` (what the live fleet uses) works. This is a *deployment* property,
  not an Agency defect — and the per-run `api_config` (ADR-0018) means the Agency is
  pointed at a working endpoint without touching the global config. **Operational lesson:
  the Agency only works against an endpoint that serves streaming; verify it before
  relying on the action layer.**
- **Model reaches for a non-granted tool:** a real model, shown py-claw's full 51-tool
  schema, may try `Bash` to "list files" (natural) and be denied, then stop. Safe (the
  tool did not run), but it can waste a turn or fail the task. **Mitigated** by naming the
  granted surface in the executor prompt (ADR-0018); the allow-list remains the hard gate.
- **Denial provenance is best-effort:** py-claw's result payload did not populate
  `permission_denials` for a denied-then-stopped run, so `denied_tools` may be empty even
  when a tool was denied. The denial itself is safe and the action is recorded
  (`completed`/`failed`); the *list* of denied tools is a provenance-completeness gap, not
  a security gap.
- **Empty-sandbox read tasks:** a fresh action sandbox starts near-empty (only the runner
  writes `.claude/settings.local.json`), so a "summarize the files here" read task has
  little to read. Fine for proving the boundary; a meaningful action needs material placed
  in the sandbox first (a `file_write_local` grant, or seeding).

## 6. Unresolved issues

- **The standalone sealed world-sim baseline was RED — now RESOLVED (ADR-0021).**
  `scripts/sealed_regression_v02.py` was failing against `backend/out/world_sim` (sealed
  2026-09-11) on **10 of the 12 cells, across all three arms** (the `C_all` cells drift in
  `n_threads`, `self_origin_share`, `route_entropy`, `no_op_ratio`, ...). Investigation
  established the drift was **entirely the KEPT
  consolidation dedup bug fix** (`sleep.py`, 4d3af5a) — a documented-GOOD unconditional fix
  (the old baseline itself contained the duplicate-self-thread corruption it removes; a
  pre-Round-13 `be33117` run reproduces the old baseline). The "failed" Round-13 change
  (`revisit_context_exclude_self`) was **already off / dead code** on every path, so it
  contributed nothing. **Not an Agency regression** — `world_sim` does not import `agency`,
  and the flag-off *event log* is byte-identical (in-suite sealed tests pass). **Resolution
  (ADR-0021, owner option (b)):** remove the two dead R13 knobs (`revisit_context_exclude_self`,
  `rest_assoc_require_distinct`) — behavior-neutral since off everywhere — keep the dedup fix
  + the 80dfb1f portability guards, and **re-baseline `backend/out/world_sim`** to the cleaned
  post-Round-13 engine. `sealed_regression_v02.py` is green again (12/12). The sealed
  baseline is a **git-ignored on-disk artifact** (`backend/out/` is in `.gitignore`; nothing
  under it is committed), so it is not reproducible from a fresh clone — the re-seal is a
  local owner action, per ADR-0021. This is **re-baselining, not a claim the 2026-09-11
  baseline still holds**; the negative results
  are preserved in `out/round13/VERDICT.md` + git history. The 2026-09-13 Mind scorecard
  (replay H1 14/16, E2 0.0000) predates the dedup fix and should be re-run against the new
  baseline before Day-7.
- **Cost / token / max-turns budgets are deferred.** py-claw exposes them only
  programmatically (no `--print` CLI surface today). The v1 conservative posture (minimal
  tool surface + sandbox + timeout) does not depend on them, but a real "high-cost" guard
  needs a small, separately-reviewed py-claw surface change. **Not a v1 blocker** for the
  conservative surface; a real gap for any granted write/network capability.
- **Single in-flight action.** The Agency serializes actions (one at a time). Fine for
  conservative v1; not a concurrency story.
- **Cross-day *task resumption* is not a feature.** Revisit is covered — a completed deed
  is recallable by the Mind (the `experience.created` anchor) and surfaces on re-entry
  (`action.completed` is `shareable`, strength 2, tested). What is *not* modeled is
  resuming a half-finished task as a first-class action state: an action is an
  **experience** the life recalls and reflects on, not a task it re-opens (principle 4 —
  "action is an experience, not a new central loop"). So the life can *revisit* a past
  deed across days; it does not *resume* one.
- **Live-world / Day-7 axis is separate and still pending** real days (see
  `RELEASE_NOTES-v1.0-DRAFT.md`). This audit is about the action layer only; the
  plurality/genesis/Day-7 evidence is on its own clock (Day 7 = 2026-09-19).
- **No real failure-injection smoke was run** (the hermetic runner-failure tests cover the
  path deterministically; a real-model `action.failed` was not captured to avoid a wasted
  model call). A real-failure smoke is cheap to add if the owner wants it in the record.

## 7. v1.0 acceptance checklist (action axis)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Sealed regression unaffected | **Met** | flag-off event log **byte-identical** (in-suite sealed tests pass; `world_sim` does not import `agency`). The standalone `sealed_regression_v02.py` baseline had gone RED from the **kept dedup fix** (post-2026-09-11 baseline) — **re-baselined per ADR-0021** (dead R13 knobs removed, dedup fix kept); green again (12/12). Not an Agency regression (§6). |
| 2 | Mind / Memory / Life Loop stable | **Met** | unchanged; full suite green |
| 3 | Agency executes real Claude-Code-level tasks | **Met** | real py-claw subprocess smoke (LAN), real model turn, `action.completed` |
| 4 | Permissions / budget / isolation / safety effective | **Met (core)** | policy gate + allow-list + timeout + sandbox + endpoint isolation + ask→deny; smoke showed a `Bash` denial. *Cost budget deferred (§6).* |
| 5 | Complete action provenance | **Met (with gap)** | `intent.proposed → action.* → experience.created`, resolvable chain + provenance dict. *`denied_tools` list best-effort (§5).* |
| 6 | action → experience → recall → reflection loop real | **Met** | experience anchor enters recall pool; Mind reflects downstream (ADR-0020); Phase-B loop tested |
| 7 | Cross-day unfinished / revisit | **Met (revisit)** | a completed deed is **recallable by the Mind** (the `experience.created` anchor) and **surfaces on re-entry** (`action.completed` shareable, strength 2) across the absence; *resuming a half-done task* is deliberately not modeled (actions are experiences, not task state — principle 4). |
| 8 | No goal / tool addiction | **Met** | no `act` route; Agency peripheral, default-off, policy-gated; Mind proposes, never grants |
| 9 | No-op still legal | **Met** | Mind noop unchanged (first-class); Agency not invoked without an intent; proposals are rare by design |
| 10 | Stable under multi-seed / replay / failure-injection | **Met (Agency) · stale (Mind)** | Agency: failure-injection hermetically covered, policy replay deterministic, per-intent deterministic (§3, §6). The 2026-09-13 Mind scorecard (replay H1 14/16, E2 0.0000) **predates the dedup fix** and is stale for the same root cause as §6 — re-run against the re-sealed baseline before Day-7. |
| 11 | Complete docs / ADR / install / launch / monitor / recovery | **Met** | ADR-0018/0019/0020 + ADR-0018 "Launch, monitor, recovery"; README section |

## 8. Was the goal achieved?

**"A long-lived digital individual with Claude-Code-level action" — the action half is
genuinely achieved, within a deliberately conservative envelope.** The evidence, not the
claim, is what holds:

- A *real* py-claw (the same runtime that is Claude-Code-level) is driven as an isolated
  subprocess, completes a real read task against a real model, and writes a fully
  provenanced, replayable, recallable record into the unified Event Store — without the
  two projects importing each other. That is the concrete, verifiable core of "Claude-
  Code-level action inside the life."
- The action is an **experience the life can reflect on**, not a task it optimizes for:
  the Life Loop is unchanged, has no `act` route, and the Mind only *proposes* while a
  deterministic policy *decides*. The "Goal → Tool → Done" degeneration the owner forbade
  is structurally absent (principle 1, 2, 4, 8 all hold and are tested).
- It is **conservative by default and by construction**: `read_only` only, no network, no
  write, no destructive, no self-granting, a hard timeout, per-action sandbox, isolated
  endpoint. Escalation requires an explicit out-of-band owner decision — never a model
  field, never a prompt.

**Honest limitations, stated plainly:**
- **The life's sealed world-sim baseline was re-baselined (ADR-0021).** It had gone RED from
  the **kept** consolidation dedup fix (a documented-GOOD bug fix; the old baseline contained
  the corruption it removes) — not from the failed Round-13 knob (already dead code). Per
  ADR-0021 the two dead R13 knobs were removed and the baseline re-sealed to the cleaned
  post-Round-13 engine — `sealed_regression_v02.py` is green again (12/12). The flag-off
  *event log* remains byte-identical (Agency didn't touch the life). The Day-7 gate on this
  is now cleared; the 2026-09-13 Mind scorecard should be re-run against the new baseline.
- The demonstrated actions are **read-only** (analyze/summarize). Write/network/
  destructive capabilities exist in the policy but are **denied by default** and are not
  exercised in this release — and the cost budget that would guard a "high-cost" action
  is not yet wired (§6). So "Claude-Code-level action" is proven for the *read/analyze*
  tier, not yet for the *mutating/external* tier.
- The **live-world** axis (plurality / genesis / Day-7) is independent and still pending
  real days; this release does not close it.
- A couple of provenance conveniences (`denied_tools` completeness) and one feature
  (cross-day task *resumption*) are partial (§5, §6).

**Verdict:** the Agency delivers the action layer the v1.0 goal required, with a real,
isolated, provenanced, conservative-by-construction capability, on top of a sealed and
regression-clean life. It is publishable **as the conservative, read-action tier of the
Resident System**, with the mutating/external tier explicitly gated and the cost budget
tracked as a known follow-up. It is *not* yet a demonstration of autonomous, mutating,
external-world agency — and this audit does not claim one.
