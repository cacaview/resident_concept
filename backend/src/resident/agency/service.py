"""The Agency: intake → policy → execute → objective events (ADR-0018/0019/0020).

This is the single door an action enters the life through. It is a peripheral
organ, not a Mind route: it is *invoked* with an intent, runs the deterministic
policy, drives the isolated py-claw subprocess, and writes back **objective**
events. The Mind never calls it directly to "decide to act" (ADR-0018, ADR-0002);
an intent arrives either from a human (Phase A) or as a *proposal* the Mind's
reflection emits and the policy then judges (Phase B).

All Event Store writes happen here, in-process, against the life's store — the
single-writer invariant and the no-dangling-link check are preserved trivially.
"""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

from ..event_store import EventStore
from ..models import (
    ACTION_COMPLETED,
    ACTION_FAILED,
    ACTION_STARTED,
    INTENT_PROPOSED,
    INTENT_REJECTED,
    Event,
    EventCreate,
)
from .policy import ActionPolicy
from .runner import AskObserver, PermissionAsk, PyClawRunner, RunResult

#: ADR-0024: the permission prompt events (the ADR-0020 family extension).
ACTION_PERMISSION_ASKED = "action.permission_asked"
ACTION_PERMISSION_ANSWERED = "action.permission_answered"

_SOURCES = frozenset({"user", "mind"})

#: The terminal event of an action, by type (ADR-0020 minimal-terminal-state).
_TERMINAL_STATUS = {
    ACTION_COMPLETED: "completed",
    ACTION_FAILED: "failed",
    INTENT_REJECTED: "rejected",
}

#: Bounded NEWEST-FIRST deed scan window (events). The newest deeds always fall
#: inside it: an open deed is by definition recent (at most one deed runs at a
#: time, under a wall-clock budget), the natural-dedup window is far smaller
#: still, and explicit-key lookups only name deeds the caller recently created.
#: Newest-first also keeps the window closed under a deed's DESCENDANTS
#: (started / terminal / anchor all have a higher seq than the intent), so an
#: in-window intent can never look open merely because its newer terminal fell
#: out of the scan. (An oldest-first scan dropped the NEWEST deeds once the
#: store grew past the limit: recent open deeds went unrecovered and recent
#: terminals were misreported as in-flight.)
_DEED_SCAN_LIMIT = 100_000

_WS_RUN = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """The natural dedup identity of an intent: casefold + collapsed whitespace."""
    return _WS_RUN.sub(" ", (text or "").strip()).casefold()


def _parse_iso(ts: str) -> datetime:
    """Parse a store ``created_at`` (UTC ISO; 'Z'-suffixed or '+00:00')."""
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _deed_index(events: list[Event]) -> tuple[dict[str, Event], dict[str, Event]]:
    """Map each intent id to its ``action.started`` child and to its terminal
    child. A terminal may cite the intent directly (the live path) or the deed's
    ``action.started`` child (the recovery path)."""
    started: dict[str, Event] = {}
    terminal: dict[str, Event] = {}
    for e in events:
        causes = e.links.get("caused_by")
        if not isinstance(causes, list):
            continue
        if e.type == ACTION_STARTED:
            for c in causes:
                started.setdefault(c, e)
        elif e.type in _TERMINAL_STATUS:
            for c in causes:
                terminal.setdefault(c, e)
    return started, terminal


def _deed_action_id(intent: Event, started: Event | None) -> str | None:
    """The action_id a deed was run under, as recorded on its events."""
    return (
        intent.provenance.get("action_id")
        or (started.provenance.get("action_id") if started is not None else None)
    )


#: How the UN-granted part of py-claw's tool surface is presented in the
#: prompt's "do not call" list: the network tools are shown as one
#: "网络请求" item (what the model actually reasons about), and the order
#: matches the v1 read_only wording, so the default read_only prompt stays
#: byte-identical to the pre-derivation text.
_FORBIDDEN_TOOL_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Bash", ("Bash",)),
    ("Write", ("Write",)),
    ("Edit", ("Edit",)),
    ("网络请求", ("WebFetch", "WebSearch")),
)


def _prompt_for(text: str, tools: tuple[str, ...] | list[str] | None = None) -> str:
    """Frame the intent as a task for the sandboxed executor. Adds no
    capability — the tool surface is bounded entirely by the policy allow-list.

    The model is shown py-claw's full tool schema, but the permission engine only
    *permits* the tools the policy granted. We name those granted tools so the
    executor works within its (already-bounded) surface instead of burning turns
    on denied tools (e.g. reaching for ``Bash`` to list files when ``Glob`` is
    what it is allowed). The "do not call" list is the COMPLEMENT of the
    actually-granted surface, so a granted tool is never listed as forbidden (a
    ``file_write_local`` deed is granted Write/Edit; a ``network`` deed is
    granted WebFetch/WebSearch). The allow-list remains the hard gate — this
    only informs the model of what it may use, it grants nothing new
    (ADR-0019)."""
    granted = "、".join(tools) if tools else "策略授予你的工具"
    granted_set = set(tools) if tools else set()
    forbidden = "、".join(
        label
        for label, members in _FORBIDDEN_TOOL_GROUPS
        if any(m not in granted_set for m in members)
    )
    return (
        "你运行在一个隔离的沙箱工作目录中，只能使用被授予的工具。"
        f"你被授权使用的工具是：{granted}。"
        f"请只使用这些工具完成任务，不要调用其它任何工具（例如 {forbidden}等）"
        "——它们不在授权范围内，会被拒绝。\n"
        "请完成下面的任务，并用中文给出简洁的结果说明。\n\n"
        f"任务：{text}"
    )


class _AskEvents(AskObserver):
    """The runner's ask observer: records the ADR-0024 prompt events into the
    store. ``action.permission_asked`` lands the moment the executor asks
    (tool + argument digest, no secrets; bounded by the ask's ``expires_at``);
    ``action.permission_answered`` lands when the answer is delivered (owner
    via the WebUI, or the policy's timeout auto-deny). Both are NON-terminal:
    the terminal-state invariant is untouched — every ``action.started`` still
    ends in exactly one of completed / failed."""

    def __init__(self, store: EventStore, *, action_id: str, intent_id: str,
                 policy: ActionPolicy):
        self._store = store
        self._action_id = action_id
        self._intent_id = intent_id
        self._policy = policy

    def on_permission_ask(self, ask: PermissionAsk) -> None:
        self._store.append(
            EventCreate(
                type=ACTION_PERMISSION_ASKED,
                visibility="private",
                content={
                    "tool": ask.tool,
                    "argument_digest": ask.argument_digest,
                    "expires_at": ask.expires_at.isoformat(),
                },
                links={"caused_by": [self._intent_id]},
                provenance={
                    "source": "agency",
                    "action_id": self._action_id,
                    "ask_id": ask.ask_id,
                    "tool_use_id": ask.tool_use_id,
                    "policy": self._policy.version,
                },
            )
        )

    def on_permission_answered(
        self, *, ask: PermissionAsk, answer: str, answered_by: str, latency_ms: int
    ) -> None:
        # v1: ``rule_delta`` is always null — "always allow" (appending the
        # corresponding rule to the owner-granted rule set) is a later ADR-0024
        # capability; the channel records allow/deny only for now.
        self._store.append(
            EventCreate(
                type=ACTION_PERMISSION_ANSWERED,
                visibility="private",
                content={
                    "tool": ask.tool,
                    "answer": answer,
                    "answered_by": answered_by,
                    "rule_delta": None,
                    "latency_ms": latency_ms,
                },
                links={"caused_by": [self._intent_id]},
                provenance={
                    "source": "agency",
                    "action_id": self._action_id,
                    "ask_id": ask.ask_id,
                    "policy": self._policy.version,
                },
            )
        )


class AgencyService:
    """Bounded action layer. One in-flight action at a time (conservative)."""

    def __init__(
        self,
        store: EventStore,
        runner: PyClawRunner,
        policy: ActionPolicy | None = None,
        *,
        dedup_window_h: float = 24.0,
        deed_scan_limit: int = _DEED_SCAN_LIMIT,
    ):
        self.store = store
        self.runner = runner
        self.policy = policy or ActionPolicy()
        self._lock = asyncio.Lock()
        # How long an identical (text, capability) deed dedups a later proposal
        # (natural dedup; guards against the Mind re-proposing the same
        # opportunity it already acted on).
        self.dedup_window_h = float(dedup_window_h)
        # The newest-first deed scan window, in events (see _DEED_SCAN_LIMIT).
        # An instance attribute so tests can shrink it to exercise store-scale
        # behaviour cheaply.
        self.deed_scan_limit = int(deed_scan_limit)

    # ------------------------------------------------- ADR-0024 owner channel

    def pending_permission_asks(self) -> list[dict]:
        """The live permission asks awaiting the owner (for the WebUI):
        the in-flight ``can_use_tool`` asks with their tool, argument digest,
        age and expiry — the objective mirror of the runner's live asks.
        ``age_s`` is seconds since the ask was created; the runner recorded
        ``created_at`` on the ask at creation, so the age is an exact fact,
        not a re-derivation."""
        now = datetime.now(UTC)
        out: list[dict] = []
        for ask in self.runner.pending_permission_asks():
            age_s = max(0.0, (now - ask.created_at).total_seconds())
            out.append({
                "ask_id": ask.ask_id,
                "action_id": ask.action_id,
                "tool": ask.tool,
                "argument_digest": ask.argument_digest,
                "created_at": ask.created_at.isoformat(),
                "expires_at": ask.expires_at.isoformat(),
                "age_s": round(age_s, 1),
            })
        return out

    def answer_permission_ask(self, ask_id: str, answer: str) -> dict:
        """Deliver the owner's answer (``"allow"`` | ``"deny"``) to the runner's
        host loop awaiting that ask. The WebUI POSTs here; the in-process seam
        is the runner's asyncio queue (same process, same event loop — the
        loop awaiting the ask wakes and writes the ``control_response`` to
        py-claw's stdin). Raises ``ValueError`` for unknown/stale asks or a
        bad answer value; the route maps that to 404/400."""
        if answer not in ("allow", "deny"):
            raise ValueError("answer must be 'allow' or 'deny'")
        if ask_id not in self.runner._live_asks:
            raise ValueError("unknown or already answered permission ask")
        self.runner.submit_permission_answer(ask_id, answer)
        return {"ask_id": ask_id, "answer": answer}

    async def intake(
        self,
        *,
        text: str,
        capability: str | None,
        source: str = "user",
        caused_by: list[str] | None = None,
        thread_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Run the full Phase-A loop for one intent and return a summary.

        The terminal outcome is always recorded: a denied intent lands as
        ``intent.rejected`` (nothing runs); an executed one as ``action.completed``
        or ``action.failed`` (with an ``experience.created`` memory anchor).

        Dedup (before ANY event is appended): an identical deed already in the
        store is returned as-is, never re-executed. An explicit
        ``idempotency_key`` finds the deed it names (any terminal, including a
        rejection — the caller chose this key for this deed); otherwise the
        natural identity (normalised text + capability) matches deeds within
        ``dedup_window_h``. A deed whose only terminal is ``intent.rejected``
        does NOT satisfy a natural match — a rejection is not an outcome that
        fulfils a later, identical proposal (the denial may have been lifted),
        so that proposal is gated fresh by the policy.
        """
        if source not in _SOURCES:
            raise ValueError(f"intent source must be one of {sorted(_SOURCES)}")
        text = (text or "").strip()
        if not text:
            raise ValueError("intent text is required")

        # 0. dedup — an identical existing deed is returned, not re-run (no new
        #    events). Conservative: a deed that is still open is reported
        #    "in_flight" rather than executed a second time.
        if idempotency_key is not None:
            hit = self._deed_by_key(idempotency_key)
            if hit is not None:
                outcome = self._deed_outcome(hit)
                if outcome is not None:
                    return outcome
        norm_text = _normalize(text)
        cap_key = (capability or "").strip().lower()
        events: list[Event] | None = None
        for cand in self._recent_deed_candidates(norm_text, cap_key):
            if events is None:
                events = self._recent_deed_events()
            outcome = self._deed_outcome(cand, events, dedup=True)
            if outcome is not None:
                return outcome

        action_id = f"act_{uuid.uuid4().hex[:12]}"

        # 1. record the intent — the deed's causal root (objective).
        intent_links: dict = {}
        if caused_by:
            intent_links["caused_by"] = list(caused_by)
        if thread_id:
            intent_links["thread_id"] = thread_id
        intent_provenance: dict = {
            "source": "agency",
            "intake": "human" if source == "user" else "mind",
            "action_id": action_id,
        }
        if idempotency_key:
            intent_provenance["idempotency_key"] = idempotency_key
        intent = self.store.append(
            EventCreate(
                type=INTENT_PROPOSED,
                actor="user" if source == "user" else "resident",
                visibility="private",
                content={"text": text, "capability": capability, "source": source},
                links=intent_links,
                provenance=intent_provenance,
            )
        )

        # 2. deterministic policy gate — the Mind (or a human) cannot grant
        #    itself a capability the policy has not granted (ADR-0019).
        decision = self.policy.evaluate(capability, source=source)
        if not decision.allowed:
            rejected = self.store.append(
                EventCreate(
                    type=INTENT_REJECTED,
                    visibility="private",
                    content={
                        "reason": decision.reason,
                        "capability": decision.capability,
                        "policy": self.policy.version,
                    },
                    links={"caused_by": [intent.id]},
                    provenance={"source": "agency", "policy": self.policy.version},
                )
            )
            return {
                "action_id": action_id,
                "status": "rejected",
                "reason": decision.reason,
                "intent_id": intent.id,
                "rejected_event_id": rejected.id,
            }

        # 3. execute in the isolated py-claw subprocess (bounded, time-boxed).
        async with self._lock:
            # ADR-0024: the ask → authorization channel. The runner records
            # action.permission_asked / action.permission_answered (via this
            # observer, in-process against the same store — the single-writer
            # invariant holds), and the owner's answer reaches the runner's
            # host loop through the in-process queue (answer_permission_ask).
            self.runner.ask_observer = _AskEvents(
                self.store, action_id=action_id, intent_id=intent.id,
                policy=self.policy,
            )
            # ``action.started``: the allowed deed is now in flight. NON-terminal —
            # it records the start so a crash mid-run leaves a recoverable open
            # deed, and the deed still ends in exactly one of completed / failed
            # (ADR-0020). Objective facts only.
            started = self.store.append(
                EventCreate(
                    type=ACTION_STARTED,
                    visibility="private",
                    content={
                        "capability": decision.capability,
                        # ADR-0024: the gate's OUTPUT is the py-claw rule list
                        # (recorded as the granted surface); ``tool_surface``
                        # is the derived human view of it (unchanged shape).
                        "permission_rules": list(decision.rules),
                        "tool_surface": list(decision.tools),
                        "sandbox": str(self.runner.sandbox_dir(action_id)),
                        "policy": self.policy.version,
                    },
                    links={"caused_by": [intent.id]},
                    provenance={
                        "source": "agency",
                        "action_id": action_id,
                        "policy": self.policy.version,
                    },
                )
            )
            result = None
            t0 = time.monotonic()
            try:
                result = await self.runner.run(
                    prompt=_prompt_for(text, decision.tools),
                    action_id=action_id,
                    allowed_tools=list(decision.rules),
                    timeout_s=decision.timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - converted below, never dropped
                # A post-``action.started`` execution error (EMFILE/EACCES, a
                # runner crash, ...) must still land the deed in a terminal
                # state — the deed is already in flight, so ``action.failed``
                # is the honest terminal (ADR-0020). Converting it into a
                # failure RunResult routes it through the normal recording path
                # below (failed deed + memory anchor + honest summary) instead
                # of propagating out of intake with the deed left open.
                one_liner = (
                    f"execution error after action.started: "
                    f"{type(exc).__name__}: {exc}"
                )[:500]
                result = RunResult(
                    ok=False,
                    summary=one_liner,
                    exit_code=None,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    reason="error",
                    detail=one_liner,
                )

        # 4. record the objective outcome + the memory anchor.
        sandbox = self.runner.sandbox_dir(action_id)
        if result.ok:
            completed = self.store.append(
                EventCreate(
                    type=ACTION_COMPLETED,
                    # a completed deed is "news about my day" — it surfaces on
                    # re-entry (shareable, strength 2, ADR-0020); the failed
                    # attempt and the intent stay private (an impulse / an
                    # unmet boundary is recalled by the Mind, not pushed to the
                    # user). v1's conservative surface is read_only, so a
                    # shareable deed is always a safe, read-only report.
                    visibility="shareable",
                    content={
                        "summary": result.summary,
                        "capability": decision.capability,
                        "tool_surface": list(decision.tools),
                        "denied_tools": result.denied_tools,
                        "exit": result.exit_code,
                        "num_turns": result.num_turns,
                        "duration_ms": result.duration_ms,
                        "sandbox": str(sandbox),
                        "stream": result.stream_path,
                    },
                    links={"caused_by": [intent.id]},
                    provenance={
                        "source": "agency",
                        "runner": "py-claw",
                        "policy": self.policy.version,
                        "capability": decision.capability,
                        "session_id": result.session_id,
                        "cost_usd": result.cost_usd,
                        "stop_reason": result.stop_reason,
                    },
                )
            )
            experience = self.store.append(
                EventCreate(
                    type="experience.created",
                    visibility="private",
                    content={"summary": f"行动：{text[:120]}"},
                    links={"caused_by": [completed.id]},
                    provenance={"source": "agency", "action_id": action_id},
                )
            )
            return {
                "action_id": action_id,
                "status": "completed",
                "intent_id": intent.id,
                "event_id": completed.id,
                "started_event_id": started.id,
                "experience_id": experience.id,
                "summary": result.summary,
                "denied_tools": result.denied_tools,
                "sandbox": str(sandbox),
                "stream_path": result.stream_path,
            }

        failed = self.store.append(
            EventCreate(
                type=ACTION_FAILED,
                visibility="private",
                content={
                    "reason": result.reason or "error",
                    "capability": decision.capability,
                    # honest one-line outcome (e.g. "Read requires permission" for a
                    # denial) — the objective record of WHY the deed failed; ``detail``
                    # is raw stderr (often empty). ADR-0020 provenance.
                    "summary": result.summary,
                    "detail": result.detail,
                    "exit": result.exit_code,
                    "num_turns": result.num_turns,
                    "duration_ms": result.duration_ms,
                    "denied_tools": result.denied_tools,
                    "sandbox": str(sandbox),
                    "stream": result.stream_path,
                },
                links={"caused_by": [intent.id]},
                provenance={
                    "source": "agency",
                    "runner": "py-claw",
                    "policy": self.policy.version,
                    "capability": decision.capability,
                },
            )
        )
        # A failed attempt is still an experience the life can recall.
        experience = self.store.append(
            EventCreate(
                type="experience.created",
                visibility="private",
                content={"summary": f"行动（未完成，{result.reason or 'error'}）：{text[:120]}"},
                links={"caused_by": [failed.id]},
                provenance={"source": "agency", "action_id": action_id},
            )
        )
        return {
            "action_id": action_id,
            "status": "failed",
            "reason": result.reason or "error",
            "detail": result.detail,
            "intent_id": intent.id,
            "event_id": failed.id,
            "started_event_id": started.id,
            "experience_id": experience.id,
        }

    # ------------------------------------------------------------------ dedup

    def _recent_deed_events(self) -> list[Event]:
        """The store's recent deed window: up to ``deed_scan_limit`` NEWEST
        events, returned in ascending seq order (so the index keeps its
        oldest-wins semantics for events inside the window). Newest-first is
        what makes the bound safe at any total store size — see
        ``_DEED_SCAN_LIMIT``."""
        events = self.store.list(self.deed_scan_limit, order="desc")
        events.reverse()
        return events

    def _deed_by_key(self, idempotency_key: str) -> Event | None:
        """The most recent deed recorded under this explicit idempotency key."""
        for e in self.store.list(200, type_prefix=INTENT_PROPOSED, order="desc"):
            if e.provenance.get("idempotency_key") == idempotency_key:
                return e
        return None

    def _recent_deed_candidates(self, norm_text: str, cap_key: str) -> list[Event]:
        """Deeds whose natural identity (normalised text + capability) matches,
        newest first, created within the dedup window."""
        cutoff = datetime.now(UTC) - timedelta(hours=self.dedup_window_h)
        out: list[Event] = []
        for e in self.store.list(200, type_prefix=INTENT_PROPOSED, order="desc"):
            if _parse_iso(e.created_at) < cutoff:
                break  # newest-first: everything after is outside the window
            if _normalize(e.content.get("text") or "") != norm_text:
                continue
            if (e.content.get("capability") or "").strip().lower() != cap_key:
                continue
            out.append(e)
        return out

    def _deed_outcome(
        self,
        intent: Event,
        events: list[Event] | None = None,
        *,
        dedup: bool = False,
    ) -> dict | None:
        """The existing outcome of a deed — no new events are written.

        A deed with a terminal child returns that terminal's status and ids; an
        open deed (no terminal yet) returns ``"in_flight"``. Under natural dedup
        (``dedup=True``) a deed whose only terminal is ``intent.rejected`` yields
        ``None`` (not a satisfying outcome — see :meth:`intake`); an explicit-key
        lookup (``dedup=False``) returns even a rejection as-is.
        """
        if events is None:
            events = self._recent_deed_events()
        started_map, terminal_map = _deed_index(events)
        started = started_map.get(intent.id)
        terminal = terminal_map.get(intent.id) or (
            terminal_map.get(started.id) if started is not None else None
        )
        action_id = _deed_action_id(intent, started)
        if terminal is None:
            return {
                "action_id": action_id,
                "status": "in_flight",
                "intent_id": intent.id,
                "deduplicated": True,
                "in_flight": True,
            }
        if dedup and terminal.type == INTENT_REJECTED:
            return None
        out: dict = {
            "action_id": action_id,
            "status": _TERMINAL_STATUS[terminal.type],
            "intent_id": intent.id,
            "event_id": terminal.id,
            "deduplicated": True,
        }
        reason = terminal.content.get("reason")
        if reason:
            out["reason"] = reason
        return out

    # --------------------------------------------------------------- recovery

    def recover_open_deeds(self) -> list[dict]:
        """Close deeds left open by a process restart (ADR-0020 terminal property).

        A crash mid-action can leave an ``intent.proposed`` (and perhaps its
        ``action.started``) with no terminal child — violating the minimal-
        terminal-state property. Each such open deed is closed with
        ``action.failed(reason="process_restart")`` plus the usual memory anchor.
        A crash BETWEEN the terminal and its ``experience.created`` append
        leaves a correctly-terminal deed with no memory anchor (never recalled —
        the closed loop broken for that deed); those are back-filled
        idempotently as well (entries carry ``reason="missing_memory_anchor"``
        and ``backfilled=True``).

        Both passes scan the bounded NEWEST-FIRST window
        (``self.deed_scan_limit`` events): an open deed is by definition recent
        (at most one deed runs at a time, under a wall-clock budget), so the
        window covers recovery at ANY total store size. Idempotent: a deed that
        already has a terminal (resp. anchor) child is left alone, so a second
        run appends nothing.
        """
        events = self._recent_deed_events()
        started_map, terminal_map = _deed_index(events)
        recovered: list[dict] = []

        # Pass 1: open deeds (no terminal child) are closed.
        for intent in events:
            if intent.type != INTENT_PROPOSED:
                continue
            started = started_map.get(intent.id)
            terminal = terminal_map.get(intent.id) or (
                terminal_map.get(started.id) if started is not None else None
            )
            if terminal is not None:
                continue  # the deed is closed — nothing to recover
            capability = intent.content.get("capability")
            sandbox = started.content.get("sandbox") if started is not None else None
            failed = self.store.append(
                EventCreate(
                    type=ACTION_FAILED,
                    visibility="private",
                    content={
                        "reason": "process_restart",
                        "capability": capability,
                        "detail": (
                            "the resident process restarted before this action reached "
                            "a terminal state; recorded by startup recovery"
                        ),
                        **({"sandbox": sandbox} if sandbox else {}),
                    },
                    links={"caused_by": [started.id if started is not None else intent.id]},
                    provenance={
                        "source": "agency",
                        "runner": "py-claw",
                        "policy": self.policy.version,
                        "capability": capability,
                    },
                )
            )
            action_id = _deed_action_id(intent, started)
            # A failed attempt is still an experience the life can recall.
            experience = self.store.append(
                EventCreate(
                    type="experience.created",
                    visibility="private",
                    content={
                        "summary": (
                            f"行动（未完成，process_restart）："
                            f"{(intent.content.get('text') or '')[:120]}"
                        )
                    },
                    links={"caused_by": [failed.id]},
                    provenance={"source": "agency", "action_id": action_id},
                )
            )
            recovered.append({
                "intent_id": intent.id,
                "action_id": action_id,
                "event_id": failed.id,
                "experience_id": experience.id,
                "reason": "process_restart",
            })

        # Pass 2: terminal deeds (completed/failed) that lost their memory
        # anchor are back-filled so the closed loop (deed -> memory -> recall)
        # holds. A rejected deed has no anchor by design.
        by_id = {e.id: e for e in events}
        anchored: set[str] = set()
        for e in events:
            if e.type != "experience.created":
                continue
            for c in e.links.get("caused_by") or []:
                if isinstance(c, str):
                    anchored.add(c)
        for e in events:
            if e.type not in (ACTION_COMPLETED, ACTION_FAILED) or e.id in anchored:
                continue
            # Resolve the deed's intent (the terminal cites the intent on the
            # live path, the started child on the recovery path).
            causes = e.links.get("caused_by") or []
            root = by_id.get(causes[0]) if causes and isinstance(causes[0], str) else None
            if root is not None and root.type == ACTION_STARTED:
                s_causes = root.links.get("caused_by") or []
                root = by_id.get(s_causes[0]) if s_causes and isinstance(s_causes[0], str) else None
            intent = root if root is not None and root.type == INTENT_PROPOSED else None
            started = started_map.get(intent.id) if intent is not None else None
            action_id = _deed_action_id(intent, started) if intent is not None else None
            text = (intent.content.get("text") or "")[:120] if intent is not None else ""
            if e.type == ACTION_COMPLETED:
                summary = f"行动：{text}" if text else "行动：任务完成"
            else:
                reason = e.content.get("reason") or "error"
                summary = f"行动（未完成，{reason}）：{text}" if text else f"行动（未完成，{reason}）"
            anchor = self.store.append(
                EventCreate(
                    type="experience.created",
                    visibility="private",
                    content={"summary": summary},
                    links={"caused_by": [e.id]},
                    provenance={
                        "source": "agency",
                        **({"action_id": action_id} if action_id else {}),
                    },
                )
            )
            recovered.append({
                "intent_id": intent.id if intent is not None else None,
                "action_id": action_id,
                "event_id": e.id,
                "experience_id": anchor.id,
                "reason": "missing_memory_anchor",
                "backfilled": True,
            })
        return recovered
