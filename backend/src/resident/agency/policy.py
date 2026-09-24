"""Deterministic action safety policy (ADR-0019, re-targeted by ADR-0024).

A *pure* function: the same declared capability under the same policy always
yields the same decision — no model call, no randomness. This is what makes
"the Mind may propose but cannot grant itself permission" mechanical: the
decision is a static allow-list the model does not own.

An intent declares a **capability** — a *ceiling* on the tool surface the
executor may open. The policy checks that capability against the granted set:

- a capability the policy has not granted is **denied**, whoever declared it
  (a human declaring ``destructive`` is still denied under the v1 default);
- a granted capability opens *exactly* its rule surface in the executor, so
  the prompt text cannot exceed the bound (a ``read_only`` run has no
  write/delete tool to run, whatever the prompt asks).

ADR-0024: the OUTPUT of the gate is no longer a fixed tool tuple that Resident
writes into ``settings.local.json``. It is the gate's verdict plus the set of
**py-claw permission rules** (py-claw's own ``allow``-rule grammar: bare tool
names, ``Tool(pattern)`` globs, ``Bash(prefix :*)`` prefix rules,
``mcp__server__*`` rules) that the runner injects into the executor. The
capability vocabulary is unchanged; the encoding moved to the executor's
native grammar, so owner-granted per-repo additions (e.g. ``Bash(gh :*)``)
are later, trivial entries in the same rule list — never a Resident mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

#: Every capability the policy knows. Anything else is denied.
KNOWN_CAPABILITIES: frozenset[str] = frozenset(
    {"read_only", "file_write_local", "network", "destructive"}
)

#: Conservative v1 default: only read-only is permitted (ADR-0019, principle 8).
DEFAULT_ALLOWED_CAPABILITIES: FrozenSet[str] = frozenset({"read_only"})

#: Wall-clock budget for one action (ADR-0019). On expiry the subprocess is
#: killed and the action is recorded ``action.failed(reason=timeout)``.
DEFAULT_TIMEOUT_S: float = 300.0

#: Wall-clock budget for ONE permission ask the owner must answer (ADR-0024).
#: An ask nobody answers expires to ``deny`` and the deed lands as
#: ``action.failed(reason=permission_timeout)``. The runner caps it at the
#: action's remaining budget (``min(remaining, ASK_TIMEOUT_S)``).
ASK_TIMEOUT_S: float = 300.0

#: The rule surface each capability opens (least → most capable), in py-claw's
#: own permission-rule grammar (ADR-0024). Every entry is an ``allow`` rule;
#: the union grows with the capability. These are the *base* rules the policy
#: grants: py-claw's engine evaluates deny > ask > allow, and under
#: ``defaultMode "default"`` any tool not matched by an allow rule ends as
#: ``ask`` — which the host channel then routes to the owner (allow / deny),
#: or to a deny on expiry. A rule list is the unit the owner can later extend
#: per repository (e.g. appending ``"Bash(gh :*)``" once granted for a repo)
#: without any Resident-side change.
_CAPABILITY_RULES: dict[str, tuple[str, ...]] = {
    "read_only": ("Read", "Glob", "Grep"),
    "file_write_local": ("Read", "Glob", "Grep", "Write", "Edit"),
    "network": ("Read", "Glob", "Grep", "WebFetch", "WebSearch"),
    "destructive": ("Read", "Glob", "Grep", "Write", "Edit", "Bash"),
}


def _capability_tools(rules: tuple[str, ...]) -> tuple[str, ...]:
    """The tool names a rule surface opens (the bare tool-name prefix of each
    rule). Used for the prompt's "granted tools" wording and the
    ``tool_surface`` deed fact — the human-facing view of the same surface."""
    out: list[str] = []
    for rule in rules:
        name = rule.split("(", 1)[0]
        if name not in out:
            out.append(name)
    return tuple(out)


@dataclass(frozen=True)
class PolicyDecision:
    """The outcome of :meth:`ActionPolicy.evaluate`."""

    allowed: bool
    #: the normalised capability the decision is about
    capability: str
    #: "allowed" | "unknown_capability:<cap>" | "capability_not_granted:<cap>"
    reason: str
    #: py-claw permission ``allow`` rules to inject (ADR-0024; empty unless
    #: allowed). py-claw's own grammar: bare tool names, ``Tool(pattern)``,
    #: ``Bash(prefix :*)``, ``mcp__server__*``.
    rules: tuple[str, ...] = ()
    #: the tool names the rule surface opens (derived; for prompts + the
    #: ``tool_surface`` deed fact).
    tools: tuple[str, ...] = field(default_factory=tuple)
    timeout_s: float = DEFAULT_TIMEOUT_S
    ask_timeout_s: float = ASK_TIMEOUT_S


class ActionPolicy:
    """A versioned, deterministic capability gate (ADR-0019, ADR-0024).

    ``allowed_capabilities`` is the set the *owner* has granted (default
    ``{read_only}``). The Mind or a human may *declare* any known capability in
    an intent, but a capability not in this set is denied — declaration is not
    authorization. When a capability IS granted, the gate emits the capability's
    base py-claw rule list; the owner may extend the effective rule set per
    action (per-repo grants) by appending to that list — the list output makes
    that a data change, never a code change.
    """

    version: str = "agency-policy-v2"

    def __init__(
        self,
        allowed_capabilities: FrozenSet[str] | set[str] | None = None,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ask_timeout_s: float = ASK_TIMEOUT_S,
    ) -> None:
        self.allowed_capabilities: FrozenSet[str] = (
            frozenset(allowed_capabilities)
            if allowed_capabilities is not None
            else DEFAULT_ALLOWED_CAPABILITIES
        )
        self.timeout_s = float(timeout_s)
        self.ask_timeout_s = float(ask_timeout_s)

    def evaluate(self, capability: str | None, *, source: str = "user") -> PolicyDecision:
        """Decide from the declared capability alone (deterministic).

        ``source`` is recorded for provenance but is NOT a decision input: the
        capability, not the requester, is the gate (ADR-0019).
        """
        cap = (capability or "").strip().lower()
        if cap not in KNOWN_CAPABILITIES:
            return PolicyDecision(
                allowed=False,
                capability=cap or "missing",
                reason=f"unknown_capability:{cap or 'missing'}",
            )
        if cap not in self.allowed_capabilities:
            return PolicyDecision(
                allowed=False, capability=cap,
                reason=f"capability_not_granted:{cap}",
            )
        rules = _CAPABILITY_RULES[cap]
        return PolicyDecision(
            allowed=True,
            capability=cap,
            reason="allowed",
            rules=rules,
            tools=_capability_tools(rules),
            timeout_s=self.timeout_s,
            ask_timeout_s=self.ask_timeout_s,
        )
