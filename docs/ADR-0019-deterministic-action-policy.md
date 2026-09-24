# ADR-0019 — A deterministic safety policy gates every action (conservative by default)

**Status:** Accepted (Resident v1.0, 2026-09-15).
**Part of:** the Agency (ADR-0018). This is the policy the Agency runs between an
intent and the py-claw subprocess.

## The problem

Owner principle 1 & 3: *the Mind may propose an action but cannot grant itself
permission; every permission, budget, and tool call must pass a deterministic safety
policy.* And principle 8: *development may grant some permissions, but not excessive
ones — actions must be conservative by default; dangerous, high-cost, irreversible, or
externally-side-effecting operations must be approved or rejected.*

The threat model has two distinct actors and the policy must not blur them:

- **The Mind** can *notice an opportunity* and state it as an intent (Phase B). If the
  policy trusted the model's self-assessment of risk, the model could talk its way into
  a tool. So the policy must decide from something the model **cannot inflate**.
- **A human** supplies an intent directly (Phase A). The human is the agent of the act,
  but the *execution* must still be bounded — a human saying "and while you're at it,
  push to origin" must not silently open a network/destructive tool surface.

## Decision

The policy is a **pure, versioned, non-model function** — `agency/policy.evaluate(intent,
policy) -> Decision`. It makes its decision from **structured** intent fields, never from
a model's interpretation of free text. Same intent in, same decision out, always.

### Capability classes are the unit of authorization

An intent declares a **capability** — the *maximum tool surface* the executor may open:

| Capability | Meaning (tool surface opened in py-claw) | v1 default |
|---|---|---|
| `read_only` | Read / Glob / Grep over the sandbox (no write, no shell-write, no network) | **allow** |
| `file_write_local` | + Write/Edit *within* the sandbox dir | deny (opt-in) |
| `network` | + WebFetch / WebSearch | deny (opt-in) |
| `destructive` | + destructive / irreversible Bash (rm, force-push, …) | deny (opt-in) |
| *(anything else / missing)* | — | **deny** |

The capability is a **ceiling**, not a promise about the prompt text:

- If an intent declares `read_only`, only read tools are enabled, so a prompt that asks
  to "delete everything" *cannot* delete — the tool surface simply does not contain a
  write/delete tool. The declared capability bounds what the executor can physically do.
- If an intent (Mind or human) declares `destructive`, the **policy denies it** under the
  v1 default, regardless of who declared it. Escalation requires an explicit, out-of-band
  config change (the owner granting a capability) — never a model field, never a prompt.

This is exactly principle 1 made mechanical: the Mind states an opportunity (and a
capability), but the *permission* is a static table the Mind does not own.

### Hard budgets are enforced before and during execution

- **`timeout_s`** (default 300): wall-clock cap on the py-claw subprocess. On expiry the
  subprocess is killed and the action is recorded `action.failed` with
  `reason=timeout` (a real, honest failure — never silently "succeeded").
- **Tool allow-list**: the py-claw permission settings written to the sandbox open
  *exactly* the approved capability's tool surface. This is a second, independent layer:
  even a bug in the policy's capability→tool mapping is caught by py-claw's own
  allow-list.
- **Sandbox working directory**: the subprocess `cwd` is a per-action directory
  (`<home>/agency_workspace/<action_id>/`), never the resident store, never the repo.
  File actions cannot see the life's data.
- **`ask`→deny backstop**: in py-claw's headless mode, any tool the permission engine
  leaves as `ask` is silently denied (no ask callback exists in `--print`/stream-json).
  So "only what is explicitly allowed" holds at the executor itself.

*Cost / token / max-turns budgets* are a **documented future enhancement**: py-claw's
`cost_budget_usd` / `token_budget` exist on its runtime state but have no `--print` CLI
surface today (they are set only programmatically). The MVP's conservative posture
(relaxing the tool surface to `read_only`, the sandbox, and the timeout) does not depend
on them; exposing them is deferred to a small, separately-reviewed py-claw surface
change.

### Three layers, one table

```
intent (structured: text, capability, source)
   │
   ▼
LAYER 1 · policy.evaluate()          ← pure, versioned, deterministic
   │  decision: allow | deny  (reason, limits)
   ▼
[deny]  → intent.rejected (recorded; nothing executed)
[allow] → LAYER 2: write the TOOL-SURFACE allow-list to the sandbox
   │          (the capability's tools, bare by name — Read/Glob/Grep for
   │           read_only; a tool the policy did not open matches no rule).
   │          It authorizes WHICH TOOLS run, not WHICH PATHS: py-claw matches
   │          an allow rule against the *raw* path the model passes with a
   │          case-/slash-sensitive glob, so path-scoping it
   │          (Read(<root>/*)) is unreliable cross-platform — it denied
   │          in-sandbox reads on Windows. The WHERE is layer 3.
   │        + sandbox cwd + PYCLAW_FS_ROOT env + timeout
   ▼
py-claw --print --output-format json   ← its own allow-list re-gates every tool call
   │
   ▼
LAYER 3 · runtime FS root (PYCLAW_FS_ROOT, honored by the py-claw runtime):
          the PATH boundary — every file action is resolved to its real path
          and contained inside the sandbox: out-of-sandbox absolute paths,
          symlinks, `..` normalisation, and Glob/Grep search roots (the search
          directory is not part of a Glob/Grep rule, so it is only containable
          here)
```

**What each layer catches:**

- *Capability escalation* — a tool outside the declared capability (e.g.
  `Bash` in a `read_only` intent) is caught by **layer 1** (the gate), and
  independently by **layer 2** (the allow-list carries only the granted tool
  surface by name, so even a bug in the capability→tool mapping cannot open a
  tool).
- *Out-of-sandbox absolute path* — `Read($HOME/.ssh/id_ed25519)`, or the
  life's own Event Store at an absolute path: the tool is on the allow-list
  (so layer 2 does not stop it — it gates *tools*, not *paths*), but the
  runtime resolves the real path and refuses it outside the sandbox →
  **layer 3**.
- *Symlink / `..` / search-root escape* — `Read(<sandbox>/link_to_home)`, a
  `..` path, or a `Glob`/`Grep` whose search directory sits outside the
  sandbox (the search directory is not part of a tool's allow-list entry) →
  **layer 3**: the runtime resolves the real path and contains the search root
  inside the sandbox's real path.
- *Anything left `ask`* → **denied by the headless backstop** (no ask
  callback in `--print` mode): the allow-list is allow-only, and the default
  is deny.

**Why layer 2 does not path-scope (the cross-platform fix, 2026-09-16):** an
earlier revision scoped `Read`/`Edit`/`Write` to `Read(<sandbox>/*)`. py-claw
matches an allow rule against the *raw* path the model passes, with a
case- and slash-sensitive glob (`fnmatchcase`); it does not resolve or
normalize the path first. On Windows that denied **every** in-sandbox read —
the model's backslash (or mixed-slash) path never matched the forward-slash
glob — so a `read_only` deed could never succeed. The robust boundary is the
runtime FS root, which resolves every path (absolute, relative, symlink,
`..`) and contains it to the sandbox on any platform. The allow-list therefore
authorizes the tool surface, and the FS root is the path boundary. (A future
py-claw change — resolve + normalize the rule's path before matching — could
restore path-scoping at layer 2 as defense-in-depth, but is not required for
containment, which layer 3 already provides.)

**Documented residual (v1):** the subprocess still inherits `HOME` (and the
OS-level process environment). No read-tier tool surface can reach it —
`Read`/`Edit`/`Write` resolve every path and are contained to the sandbox by
the runtime FS root (layer 3), and there is no `Bash` — so it is a
*documented residual*, not an open hole. OS-level containment
(sandbox-exec / landlock) is explicitly out of scope for v1.

## What "conservative by default" means concretely

- v1 allow-list = `{read_only}`. The resident can *analyze and summarize* real material
  in its sandbox (a genuine Claude-Code-grade read task) and nothing more, until the
  owner explicitly grants a higher capability in the policy config.
- No network. No writes. No destructive ops. No self-granting. A denied intent is
  *recorded* (`intent.rejected`) so the life can reflect on the boundary — being told
  "no" is itself an experience, and it is provable in the log.

## Impact on existing experiments

None (off by default; the policy and the Agency exist only for `active-v3` /
`RESIDENT_AGENCY=1`). No life-loop, memory, or world change.

## Verification

- **Unit tests, hermetic:** every capability maps to the correct tier; the v1 allow-list
  is exactly `{read_only}`; an unknown/missing capability is denied; a `network` and a
  `destructive` intent are denied **even when `source=user`** (the capability, not the
  source, is the gate).
- **Budget tests:** a subprocess that exceeds `timeout_s` is killed and recorded as
  `action.failed(reason=timeout)`; the sandbox cwd is created and isolated.
- **Layer 2 test:** the sandbox settings file authorizes the read_only tool
  surface by name (`Read`/`Glob`/`Grep`) and carries no write/net/destructive
  tool for `read_only`; the allow-list is NOT path-scoped (path containment is
  layer 3). **Layer 3 test:** the spawn env always carries `PYCLAW_FS_ROOT`
  set to the resolved sandbox path — the runtime FS root that resolves and
  contains every file path.
- **Layer guard tests:** a symlinked sandbox (or symlinked sandbox root) fails
  closed as `action.failed(reason=sandbox_violation)` with nothing spawned; an
  action id that could escape the sandbox root is rejected.
- **End-to-end:** the Phase A loop (ADR-0018) with a `read_only` intent succeeds and is
  fully provenanced; the same loop with a denied capability produces `intent.rejected`
  and no subprocess launch.
