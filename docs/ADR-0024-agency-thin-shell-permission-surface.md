# ADR-0024 — Agency goes thin: py-claw's permission surface replaces Resident's static allow-list

**Status:** Proposed (2026-09-19).
**Scope:** rewrites the *executor* half of the Agency (`runner.py`'s permission model,
`policy.py`'s output) and introduces a user-authorization channel for `ask` outcomes.
The Life Loop, Memory, World, the event model (ADR-0020), the deed lifecycle
(`service.py`: dedup, single-flight, recovery), and the "Mind proposes, does not grant"
invariant are **not** modified. ADR-0018/0019/0020 remain authoritative for the parts
this ADR does not explicitly supersede.

## Context

ADR-0018/0019 established the Agency as a thin module that drives py-claw as an
isolated subprocess, with three containment layers:

1. a deterministic **capability gate** (policy → fixed tool tuple),
2. a **static per-action tool allow-list** written into `.claude/settings.local.json`
   (anything left as `ask` is denied, because `--print` mode has no ask callback),
3. **`PYCLAW_FS_ROOT`** path containment (honored inside py-claw, `local_fs.py`).

Investigation (2026-09-19) of the actual py-claw runtime (the public upstream
`github.com/cacaview/py-claw`, then at `61c226e`) shows:

- **Execution already lives entirely in py-claw.** 50+ built-in tools (Read/Edit/Write,
  `Bash` with prefix rules, WebFetch/WebSearch, Agent, MCP), a full
  allow/ask/deny rule engine (glob + `Bash(prefix :*)` + `mcp__server__*` matching,
  YOLO fallback, `dontAsk` mode), multi-source settings with **live programmatic
  injection** (`apply_flag_settings` control request), and `PermissionRequest`
  hooks. Resident's runner adds no execution logic — only sandbox
  bookkeeping, timeout/kill, XDG isolation, and stdout-JSON parsing.
- **py-claw's interactive permission prompt exists and is wired**: the TUI installs
  `RuntimeState.permission_ask_callback` (`ui/textual_app.py:215` → Allow /
  **Always allow** / Deny dialog). The `--print` path deliberately never sets that
  callback, which is *why* ask→deny today.
- **Known gap:** the stream-json `can_use_tool` control request has a host-side
  handler (`cli/control.py:292`) but the query engine **never emits it** (no writer in
  `src/`; tests cover only the handler). Host-side prompting therefore does not work
  out of the box in a spawned process.
- **GitHub maintenance needs no dedicated development.** `Bash(gh :*)` prefix rules
  plus `gh` on PATH (or a GitHub MCP server) cover the whole
  issue → research → fix → PR → CI → release chain with py-claw's existing tools and
  rule grammar. This ADR deliberately adds **no GitHub-specific mechanism**.

## Decision

The Agency becomes a **thin shell**. py-claw owns the executor and all tool
capabilities; Resident owns exactly three things:

1. **Deed lifecycle & objective ledger** (`service.py`, unchanged):
   `intent.proposed → policy → action.started → terminal + experience.created`,
   dedup, single-flight, crash recovery, `caused_by` links.
2. **Capability → rule injection** (policy, re-targeted): the policy gate keeps
   deciding *what a proposed intent may do*, but its output is no longer "a fixed
   tool tuple that Resident writes as a bare allow-list". It is a set of
   **py-claw permission rules** (`allow`/`ask`/`deny` in py-claw's own grammar)
   injected via `apply_flag_settings` (or `policySettings` when embedded). The
   capability vocabulary stays; the encoding moves to the executor's native one.
   Per-repo graduated authorization (owner directive) becomes: rules are scoped
   per repository/remote, granted by the owner per repo, recorded on the deed.
3. **The ask → authorization channel** (new): an `ask` outcome is no longer a
   silent denial. It becomes a first-class, objective event (`action.permission_asked`,
   ADR-0020 family extension — tool name + argument digest, no secrets) surfaced to
   the owner through the **Resident WebUI**. The owner's allow / always-allow / deny
   answer is delivered back to py-claw and recorded (`action.permission_answered`,
   `answered_by: owner-webui`, decision, tool, rule delta). "Always allow" appends
   the corresponding rule to the *policy-granted* rule set for that repository —
   **not** to a file the model can edit — preserving "Mind cannot self-grant":
   the model can *trigger* a prompt, only the owner *answers* it.

### What is deleted vs kept

| Piece (ADR-0019) | Fate |
|---|---|
| Capability gate (Mind proposes → policy decides, static table, versioned) | **Kept.** Output re-targeted to py-claw rule grammar (above). |
| Resident writes `.claude/settings.local.json` allow-list | **Deleted** as the *primary* boundary; py-claw's native settings + `apply_flag_settings` are the mechanism. A fail-closed `deny`-all base rule is still injected per action, so an un-asked tool never runs. |
| `--print` one-shot with ask→deny as the backstop | **Replaced** by the ask→authorization channel. Headless default when no owner is reachable stays **deny** (a prompt the owner never answers expires to deny; the deed records `reason=permission_timeout`). |
| Per-action sandbox dir, `PYCLAW_FS_ROOT`, XDG isolation, timeout/kill, JSON-result normalization | **Kept.** They are process-boundary guarantees (ADR-0018: killable, cannot drag the life down, config isolation), independent of the permission model. |
| `service.py` (deeds/dedup/recovery/ledger) | **Kept unchanged.** |

### py-claw integration form

- py-claw enters the Resident repo as a **git submodule** under `py-claw/`
  (revised **2026-09-20** from the original vendored-subtree plan: an upstream
  update is now a plain re-pin — `cd py-claw && git pull && git add py-claw` —
  rather than re-copying ~670 files). `sync_public.sh` still exports via
  `git show` blobs and cannot carry a gitlink, so the **public mirror ships the
  *pinned* py-claw source as plain files**: it enumerates the pinned commit's
  files and stages each from the submodule's object store at the pinned SHA
  (self-contained mirror; no `git submodule update` at deploy time). The pin is
  the gitlink (`git ls-tree HEAD py-claw`); `PYCLAW-VENDOR.md` (repo root) records
  which upstream commit that is + the re-sync command. Upstream
  `github.com/cacaview/py-claw` stays the canonical development location.
- **The spawn mode changes from `--print` to `--input-format stream-json
  --output-format stream-json`**, long-lived per action: Resident sends the task as a
  user message, receives tool/permission traffic, and answers `ask` via the control
  protocol. The runner keeps its supervision (timeout/kill of the *process*,
  per-action sandbox, XDG) — only the I/O contract widens.
- The missing `can_use_tool` **emitter** on py-claw's side is a small, protocol-level
  patch to upstream py-claw (query engine: when evaluation resolves to `ask` and no
  TUI callback is installed, emit a `can_use_tool` control request and await the
  host response instead of raising `ToolPermissionError`), landed upstream first, then
  re-synced into the vendor tree. This is the only py-claw code change this ADR
  requires; no fork, no shadow implementation in Resident.
- **`PermissionRequest` hooks** are the sanctioned fallback/accelerator: for rules the
  owner has already granted (session or repo-level), a hook resolves them
  programmatically without a round-trip prompt, keeping unattended runs fast.
  Hooks answer only *already-granted* patterns; anything new still goes to the owner.
- In-process embedding (`import py_claw`) remains **rejected** (ADR-0018 alternative
  stands: no `proc.kill()` for hung turns, cross-action env bleed, Python ≥3.12 floor
  and py-claw's full dependency tree pushed into the life process). The subprocess
  boundary is the cheap, load-bearing part of ADR-0018 and survives this ADR intact.

### Guarantees preserved (checklist)

1. **Mind cannot self-grant** — invariant unchanged, now enforced at the *answer*
   side: only an owner answer (WebUI) or an already-owner-granted rule can allow a
   new pattern; prompts the owner does not answer expire to **deny**.
2. **Killable** — per-action subprocess under wall-clock budget (300 s default,
   policy-set), `action.failed(reason=timeout)` unchanged.
3. **Cannot drag the life down** — process boundary, per-action sandbox + XDG
   isolation unchanged.
4. **Objective ledger** — every prompt and every answer is an event
   (ADR-0020 family extension below); a denied/timeout prompt is a fact in the deed,
   not silence.
5. **Deterministic policy** — "same intent in, same decision out" holds for the
   gate itself (static, versioned, owner-owned); the *authorization* step is
   explicitly human-by-design and recorded as such.

## Event family extension (ADR-0020)

- `action.permission_asked` — `{action_id, tool, argument_digest, rule_matched|null,
  expires_at}`. `argument_digest` is a bounded, redacted fingerprint (e.g. for `Bash`
  the command head + hashes; never full secrets).
- `action.permission_answered` — `{action_id, answer: allow|always_allow|deny,
  answered_by: owner-webui, rule_delta|null, latency_ms}`.
- `action.failed(reason=permission_timeout)` — the owner did not answer before
  `expires_at`; the run is killed or the tool denied at the executor, deed closed.

These two new event types (plus the new failure reason) are the only event-model
changes; they are additive and backward-compatible (old deeds have no prompts).

## Consequences

- `backend/src/resident/agency/runner.py` — I/O contract: `--print` → stream-json
  control loop; permission-settings writer becomes a rule injector
  (`apply_flag_settings`); ask handler + answer delivery added; timeout/kill/sandbox
  unchanged.
- `backend/src/resident/agency/policy.py` — output type changes from tool tuples to
  py-claw rule lists (allow/ask/deny), still static, versioned, owner-granted;
  per-repo rule scoping added (rules carry a repo scope; unscoped rules apply only
  to the local-sandbox default repo).
- Resident WebUI — new panel: pending permission asks (tool, digest, repo, age,
  allow/always-allow/deny buttons), and a history of answered prompts.
- `sync_public.sh` allow-list — the *pinned* py-claw submodule source is exported
  to the public mirror **as plain files** (upstream is already public; keeps the
  mirror self-contained and the Windows deploy source complete, with no gitlink /
  no `git submodule update` at deploy time) — **chosen**, with the binary/secret
  leak-check unchanged (py-claw/ excluded from both, as before).
- Windows deploy scripts — `RESIDENT_PYCLAW_BIN` points at the in-repo py-claw venv;
  `01_deploy.ps1` needs no separate py-claw clone (the mirror ships py-claw/ as
  plain files; the dev repo carries it as a submodule via `.gitmodules`).
- `backend/tests/test_agency.py` — the fake-binary strategy extends: the fake
  `py-claw` speaks stream-json and can emit a `can_use_tool` request mid-run;
  new tests cover ask→answer round-trip, always-allow rule delta, permission_timeout,
  and deny-all base rule fail-closed.
- Sealed regression (world_sim) must pass before and after: no Life-Loop code is
  touched, but the rule is the rule.

## Risks & mitigations

- **Prompt answered by the wrong party.** Only `answered_by: owner-webui` is accepted
  (the channel is authenticated by the existing WebUI session); hook answers are
  restricted to pre-granted patterns; no "auto-allow" path exists.
- **Unattended box, prompt nobody sees.** Asks carry `expires_at`; expiry → deny +
  `permission_timeout` deed fact. The resident can retry the intent later (normal
  dedup/revival semantics) — a GitHub action simply waits for a human, like a human
  maintainer waiting in a PR review.
- **Vendor drift.** The submodule gitlink pins the upstream SHA (recorded in
  `PYCLAW-VENDOR.md`); a re-sync is an explicit, reviewed commit that moves the
  gitlink; the `can_use_tool` emitter must be present in the pinned SHA (checked
  by a Resident test that asserts the stream-json surface).
- **stream-json parsing bugs in the runner.** The runner already normalizes five
  failure modes; stream-json adds envelope validation with the same
  fail-closed taxonomy (malformed envelope → `action.failed(reason=malformed_output)`).

## Rejected alternatives

- **In-process embedding of py-claw** — rejected, ADR-0018 reasons stand
  (timeout-kill, isolation, dependency/version coupling).
- **Keep the static allow-list, add GitHub-specific tooling in Resident** — rejected:
  duplicates py-claw's existing rule grammar and MCP surface; violates
  "py-claw 能轻松处理，不为单场景做开发".
- **Submodule instead of subtree** — *originally rejected* (2026-09-19) on the
  grounds that `sync_public.sh`'s blob export cannot carry a gitlink and the mirror
  would need a `git submodule update` step. **Adopted 2026-09-20** after the export
  concern was resolved: the dev repo carries a submodule (easy re-pin), while
  `sync_public.sh` keeps the mirror self-contained by extracting the pinned source
  as plain files — so mirror consumers never need network access to the submodule
  host.
- **TUI as the authorization channel** — rejected: the live service is headless
  (no TTY); Textual cannot render there. WebUI is the channel.
- **Auto-answer hooks for new patterns** — rejected: that *is* self-grant by
  another name; hooks may only resolve already-owner-granted rules.
