"""Drive ``py-claw`` as an isolated subprocess — stream-json host mode (ADR-0018/0024).

The Agency never imports ``py_claw`` — it spawns the ``py-claw`` binary as a
long-lived **stream-json host session**, in a **per-action sandbox** working
directory, under a **wall-clock timeout** that kills the process. This is the
real process boundary of the Resident System (ADR-0018): a hung or failed
action is a subprocess we can time out and kill, and it cannot reach the
life's store. ADR-0024 widens only the I/O contract — the supervision
(sandbox, ``PYCLAW_FS_ROOT``, XDG isolation, timeout/kill) is unchanged.

Containment is FOUR layers (ADR-0019, re-targeted by ADR-0024):

1. **Policy capability gate** — the deterministic policy decides which rule
   surface opens at all; the Mind or a human cannot grant themselves more.
   Its output is py-claw's own permission-rule grammar (ADR-0024), not a
   Resident-side tool tuple.
2. **Rule injection** — the policy-granted rules are written into the
   sandbox's ``.claude/settings.local.json`` as py-claw ``allow`` rules with
   ``defaultMode: "default"``. That is py-claw's RECOGNIZED localSettings
   source (``py_claw/settings/loader.py``), read fresh by the permission
   engine for EVERY tool evaluation — so it works even on the very first tool
   call, before any control round-trip, which is exactly why this seam (not
   the ``apply_flag_settings`` control request) is used: ``apply_flag_settings``
   merges into ``state.flag_settings`` on receipt and would apply only if it
   arrived and was processed before the first ``evaluate``; the settings file
   is applied by the engine itself, per evaluation, with no ordering hazard.
   Fail-closed: any tool not matched by an allow rule ends as ``ask`` under
   ``defaultMode "default"`` — never an implicit allow.
3. **The ask → authorization channel (ADR-0024, new)** — an ``ask`` outcome is
   no longer a silent denial. py-claw emits a ``can_use_tool`` control request
   over stream-json; the runner records ``action.permission_asked`` and waits
   for the owner's answer (delivered in-process through the runner's
   ``answer_queue``, surfaced by the WebUI) or a deny on expiry (auto-deny).
   The answer goes back as a ``control_response`` echoing the ACTUAL
   ``request_id`` py-claw emitted (the host may also use the ``__PENDING__``
   placeholder, which py-claw substitutes).
4. **Runtime FS root** — the spawn env always carries
   ``PYCLAW_FS_ROOT=<resolved sandbox dir>``, honored by the py-claw runtime:
   every file action is resolved to its real path and contained inside the
   sandbox (out-of-sandbox absolute paths, symlinks, ``..`` normalisation, and
   Glob/Grep search roots). Path containment is not path-scoped rules: py-claw
   matches an allow rule against the *raw* path the model passes, case- and
   slash-sensitively — a path-scoped rule is unreliable cross-platform.
   ``PYCLAW_FS_ROOT`` also reaches py-claw's bash tool, which derives
   ``GIT_CEILING_DIRECTORIES`` from it (blocks a git repository search from a
   subdirectory from walking up past the sandbox); the runner additionally
   plants an EMPTY git repository at the sandbox root (blocks a search that
   starts at the root itself — the two are complementary, neither suffices
   alone). The run's full stream-json is persisted as
   ``<action_id>.pyclaw-stream.jsonl`` next to the per-action sandbox
   (``RunResult.stream_path``) for every outcome.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Protocol

__all__ = ["RunResult", "PyClawRunner", "AskObserver", "PermissionAsk", "permission_argument_digest"]

logger = logging.getLogger(__name__)

#: A permission ask expires (auto-deny) after at most this long; the runner
#: caps it at the action's remaining budget (ADR-0024).
MAX_ASK_TIMEOUT_S: float = 300.0


@dataclass(frozen=True)
class RunResult:
    """Objective outcome of one py-claw run (facts only — ADR-0020)."""

    ok: bool
    #: the final result text (success) or an honest error summary (failure)
    summary: str
    exit_code: int | None
    duration_ms: int
    num_turns: int | None = None
    cost_usd: float | None = None
    stop_reason: str | None = None
    #: tools the engine DENIED during the run (permission backstop provenance)
    denied_tools: list[str] = field(default_factory=list)
    #: "timeout" | "error" | "malformed_output" | "permission_timeout" | None
    reason: str | None = None
    detail: str | None = None
    session_id: str | None = None
    #: raw py-claw stream-json persisted next to the deed sandbox (all
    #: outcomes, incl. timeout — appended line-by-line so a killed run keeps
    #: everything it emitted). None when persistence could not be started.
    stream_path: str | None = None


@dataclass(frozen=True)
class PermissionAsk:
    """One live ``can_use_tool`` ask awaiting the owner's answer (ADR-0024).

    The in-flight record the WebUI reads (``pending_permission_asks``) and
    answers (``submit_permission_answer``). ``ask_id`` is the actual
    ``request_id`` py-claw emitted — the host echoes it back in its
    ``control_response``, so it is the wire identity of the ask.
    """

    ask_id: str
    action_id: str
    tool_use_id: str | None
    tool: str
    argument_digest: str
    created_at: datetime
    expires_at: datetime


class AskObserver(Protocol):
    """Receives permission asks and answers as they happen (service.py records
    the ``action.permission_asked`` / ``action.permission_answered`` events;
    tests spy on them). Both methods are optional (a plain object that
    implements just one can be passed); the runner calls whichever exist."""

    def on_permission_ask(self, ask: PermissionAsk) -> None: ...

    def on_permission_answered(
        self,
        *,
        ask: PermissionAsk,
        answer: str,
        answered_by: str,
        latency_ms: int,
    ) -> None: ...


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


def permission_argument_digest(tool: str, tool_input: dict | None) -> str:
    """A bounded, redacted fingerprint of the requested arguments (ADR-0024).

    The digest is what the owner sees in the WebUI and what the deed records —
    NEVER the full input: for ``Bash`` the command head (≤200 chars) plus a
    short hash of the full command; for file tools the path (bounded); for
    anything else a compact JSON dump (bounded) plus a hash. Secrets stay in
    the full input, which never crosses this boundary.
    """
    data = tool_input or {}

    def _bounded(text: str, limit: int = 200) -> str:
        text = " ".join(str(text).split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]

    if tool == "Bash":
        command = str(data.get("command") or "")
        head = _bounded(command)
        return f"{head}…sha={_digest(command)}" if len(command) > 200 else f"{command} sha={_digest(command)}"
    for key in ("file_path", "path", "url", "pattern", "query"):
        if isinstance(data.get(key), str) and data[key]:
            value = data[key]
            bounded = _bounded(value, 120)
            return f"{key}={bounded}" if len(value) <= 120 else f"{key}={bounded} sha={_digest(value)}"
    if data:
        text = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
        if len(text) > 200:
            return text[:199] + "… sha=" + _digest(text)
        return text
    return "(no arguments)"


async def _kill(proc: asyncio.subprocess.Process | None) -> None:
    if proc is None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001 - best-effort reap after a kill
        pass


class PyClawRunner:
    """Spawns and supervises the ``py-claw`` stream-json host session for one
    action (ADR-0024)."""

    def __init__(self, *, pyclaw_bin: str, sandbox_root: Path | str,
                 api_config: dict | None = None):
        self.pyclaw_bin = str(pyclaw_bin)
        self.sandbox_root = Path(sandbox_root)
        # Deployment-controlled py-claw model config, e.g.
        # {"api": {"api_url": ..., "api_key": ..., "model": ...}}. When set, each
        # run gets an isolated XDG_CONFIG_HOME (per-action) so the Agency uses the
        # endpoint it is deployed with — never silently relying on (or mutating)
        # the user's global ~/.config/py-claw/config.json. When None, py-claw
        # falls back to its default config.
        self.api_config = api_config
        # ADR-0024: the owner-answer channel. An in-process asyncio queue: the
        # WebUI route handler (same process, same event loop) calls
        # submit_permission_answer(), the host loop awaiting the answer wakes
        # here. One deed runs at a time (service.py's single-flight lock), so
        # at most one ask is pending — the queue is still a queue, because
        # asks are matched by ask_id and a late/stale answer must not resolve
        # a live ask.
        self._answer_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        # ADR-0024: live asks awaiting the owner (WebUI reads this).
        self._live_asks: dict[str, PermissionAsk] = {}
        # Optional observer (service.py records action.permission_asked).
        self.ask_observer: AskObserver | None = None
        # Overridable ask budget; capped per ask at the remaining action budget
        # and at MAX_ASK_TIMEOUT_S.
        self.ask_timeout_s: float = MAX_ASK_TIMEOUT_S

    # ------------------------------------------------------------- answer channel

    def submit_permission_answer(self, ask_id: str, answer: str) -> None:
        """Deliver the owner's answer (``"allow"`` | ``"deny"``) to the host
        loop awaiting that ask. Synchronous by contract (the WebUI route
        handler calls it from the running event loop); a no-op if the ask is
        unknown or already answered — a stale answer must never touch a live
        one."""
        if ask_id not in self._live_asks:
            return
        self._answer_queue.put_nowait((ask_id, answer))

    def pending_permission_asks(self) -> list[PermissionAsk]:
        """The live asks awaiting the owner, oldest first (for the WebUI)."""
        return sorted(self._live_asks.values(), key=lambda a: a.expires_at)

    # -------------------------------------------------------------------- env

    def _subprocess_env(self, sandbox: Path) -> dict:
        """The subprocess environment, ALWAYS a fresh dict (never inherited
        as-is): the process env plus the containment seam.

        ``PYCLAW_FS_ROOT`` (layer 4) is the runtime containment root the
        py-claw runtime honors — the resolved real sandbox path, so symlinks
        and ``..`` cannot escape. When an ``api_config`` is deployed, the XDG
        state dirs are also pointed at per-run dirs inside the sandbox so the
        subprocess reads *our* config and never the user's global XDG state.

        ``PYTHONIOENCODING=utf-8`` forces the subprocess to speak UTF-8 on
        stdout/stderr. We capture its output as raw bytes and decode UTF-8;
        without this, a non-UTF-8 default locale (e.g. a Chinese-locale
        Windows host, cp936/GBK) makes py-claw emit its stream-json in the
        locale encoding, which the UTF-8 decode turns into mojibake. UTF-8
        stdio is the one encoding the runner and the executor agree on.
        """
        env = dict(os.environ)
        env["PYCLAW_FS_ROOT"] = str(sandbox.resolve())
        env["PYTHONIOENCODING"] = "utf-8"
        if self.api_config:
            xdg = sandbox / ".xdg"
            cfg_dir = xdg / "py-claw"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "config.json").write_text(
                json.dumps(self.api_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            env["XDG_CONFIG_HOME"] = str(xdg)
            env["XDG_DATA_HOME"] = str(xdg / "data")
            env["XDG_CACHE_HOME"] = str(xdg / "cache")
        return env

    def sandbox_dir(self, action_id: str) -> Path:
        """The per-action sandbox dir. Intake only ever passes
        ``act_<uuid4-hex>``; anything that could escape the sandbox root is
        rejected outright (belt and braces)."""
        if (
            not action_id
            or action_id in {".", ".."}
            or "/" in action_id
            or "\\" in action_id
        ):
            raise ValueError("invalid action id")
        return self.sandbox_root / action_id

    # ------------------------------------------------------------- rule injection

    def _permission_rules(self, allowed_tools: list[str]) -> list[str]:
        """Map the approved rule surface to py-claw allow-list entries
        (ADR-0019 layer 2 / ADR-0024 layer 2).

        Each approved tool is allow-listed **bare** (by name) — the allow-list
        authorizes *which tools* run, not *which paths*. Path containment is
        layer 4 (``PYCLAW_FS_ROOT``), which resolves and contains every file
        path to the per-action sandbox. A path-scoped rule
        (``Read(<root>/*)``) would be unreliable cross-platform: py-claw
        matches it against the *raw* path the model passes with a case- and
        slash-sensitive glob, so on Windows (backslash paths, either slash
        direction in the model's spelling) it denied legitimate in-sandbox
        reads. Bare tool names + the runtime FS root is the robust boundary.
        """
        return [str(tool) for tool in allowed_tools]

    def _write_permission_settings(self, sandbox: Path, allowed_tools: list[str]) -> None:
        """ADR-0024 layer 2: write the policy-granted rules as py-claw
        ``allow`` rules into the sandbox's ``.claude/settings.local.json``.

        ``defaultMode: "default"`` is the fail-closed base: py-claw's engine
        evaluates deny > ask > allow, and any tool matched by no allow rule
        ends as ``ask`` — routed to the owner over the ``can_use_tool``
        channel (layer 3), or denied on ask expiry. Nothing unlisted is ever
        implicitly allowed. The file (not the ``apply_flag_settings`` control
        request) is the seam: it is a recognized settings source read by the
        engine for EVERY tool evaluation, so the rules hold from the very
        first tool call with no control round-trip ordering hazard.
        """
        settings_dir = sandbox / ".claude"
        settings_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "permissions": {
                "defaultMode": "default",
                "allow": self._permission_rules(allowed_tools),
            }
        }
        (settings_dir / "settings.local.json").write_text(
            json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _sandbox_violation(self, sandbox: Path) -> str | None:
        """Fail-closed sandbox checks before anything is spawned (ADR-0019).

        Containment only holds if the sandbox root is a real directory and the
        per-action dir resolves inside it; a symlinked path (or a resolved
        path outside the root) would let the subprocess's file actions reach
        elsewhere. Returns a detail string for the violation, or ``None``.
        """
        root = Path(self.sandbox_root)
        if root.is_symlink():
            return f"sandbox_root is a symlink: {root}"
        resolved_root = root.resolve()
        if sandbox.is_symlink():
            return f"sandbox path is a symlink: {sandbox}"
        resolved = sandbox.resolve()
        if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
            return (
                f"sandbox {sandbox} resolves to {resolved}, "
                f"outside the sandbox root {resolved_root}"
            )
        return None

    def _init_protective_repo(self, sandbox: Path) -> None:
        """Plant an EMPTY git repository at the sandbox root (layer-4
        complement, git containment).

        A bare ``git`` command run at the sandbox root (no ``.git`` there)
        makes git walk UP the directory tree looking for a repository root —
        and find one OUTSIDE the sandbox: the host checkout the sandbox was
        carved out of — where it can create branches and commits. With an
        empty repository at the root, that walk stops at the sandbox itself
        (verified empirically). This covers a search that starts EXACTLY at
        the root; the py-claw-side ``GIT_CEILING_DIRECTORIES`` env (set from
        ``PYCLAW_FS_ROOT`` in the bash child env) covers searches that start
        strictly BELOW the root, including after this protective ``.git``
        has been removed. The two layers are complementary, not redundant.

        Best-effort: without a git binary the git-escape threat is absent
        too, so a failure here is logged, never fatal to the run.
        """
        git = shutil.which("git")
        if git is None:
            logger.warning("git not found; protective sandbox repo not initialized")
            return
        try:
            subprocess.run(
                [git, "-C", str(sandbox), "init", "-q"],
                check=True, capture_output=True, timeout=10,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            logger.warning("protective sandbox git init failed: %s", exc)

    # ------------------------------------------------------------------ run

    async def run(
        self,
        *,
        prompt: str,
        action_id: str,
        allowed_tools: list[str],
        timeout_s: float,
    ) -> RunResult:
        """Run one py-claw turn in a fresh sandbox (stream-json host mode,
        ADR-0024) and return the outcome."""
        sandbox = self.sandbox_dir(action_id)
        t0 = time.monotonic()
        violation = self._sandbox_violation(sandbox)
        if violation is not None:
            # fail closed: the containment does not hold, so nothing is spawned.
            # The honest one-liner goes in ``summary`` (not only ``detail``):
            # the intake records ``summary`` on the failed deed, so an empty
            # summary would leave an empty ``action.failed`` claim (ADR-0020).
            return RunResult(
                ok=False, summary=violation, exit_code=None, duration_ms=_ms(t0),
                reason="sandbox_violation", detail=violation,
            )
        sandbox.mkdir(parents=True, exist_ok=True)
        self._write_permission_settings(sandbox, allowed_tools)
        self._init_protective_repo(sandbox)
        # Forensics: the full stream-json of the host session is persisted
        # line-by-line to a SIBLING file next to the deed sandbox — outside
        # the agent's cwd, so it never appears in the deed's workspace — and
        # stays complete even when the run is killed (timeout).
        stream_path = Path(self.sandbox_root) / f"{action_id}.pyclaw-stream.jsonl"
        try:
            # ADR-0024: the I/O contract widens — no ``--print`` one-shot. The
            # process is a stream-json host session: we send the task as a
            # user message, answer ``can_use_tool`` asks, and read the
            # query-output stream until the terminal ``result`` message.
            proc = await asyncio.create_subprocess_exec(
                self.pyclaw_bin,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                cwd=str(sandbox),
                env=self._subprocess_env(sandbox),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            one_liner = f"py-claw binary not found: {self.pyclaw_bin}"
            return RunResult(
                ok=False, summary=one_liner, exit_code=None, duration_ms=_ms(t0),
                reason="error", detail=one_liner,
            )

        # The whole host loop is ONE asyncio task per action (service.py runs
        # one deed in flight): it owns stdin writes (the task message, the
        # control responses) and the stdout read; the wall-clock timeout is
        # cancelled on the loop itself, so a kill is never orphaned.
        loop_task = asyncio.create_task(
            self._host_loop(
                proc, prompt=prompt, action_id=action_id,
                deadline=t0 + float(timeout_s),
                stream_path=stream_path,
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(loop_task), timeout=timeout_s)
        except asyncio.TimeoutError:
            # wall-clock budget exhausted: kill the process, exactly as today.
            loop_task.cancel()
            await _kill(proc)
            try:
                await loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            one_liner = f"py-claw exceeded the {timeout_s:g}s budget; subprocess killed"
            return RunResult(
                ok=False, summary=one_liner, exit_code=None, duration_ms=_ms(t0),
                reason="timeout", detail=one_liner,
                stream_path=str(stream_path),
            )
        except Exception:  # noqa: BLE001 - the loop died; the process must not leak
            await _kill(proc)
            one_liner = "py-claw host loop crashed"
            return RunResult(
                ok=False, summary=one_liner, exit_code=None, duration_ms=_ms(t0),
                reason="error", detail=one_liner,
                stream_path=str(stream_path),
            )

        # The loop owns the process teardown (drains stderr, reaps the child);
        # nothing else to do here — just return its RunResult.
        return result

    # ------------------------------------------------------------ the host loop

    async def _write_line(self, proc: asyncio.subprocess.Process, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        assert proc.stdin is not None
        proc.stdin.write(line.encode("utf-8"))
        await proc.stdin.drain()

    async def _respond_to_control_request(
        self, proc: asyncio.subprocess.Process, request_id: str, response: dict,
    ) -> None:
        """A ``control_response`` the loop writes back to py-claw. ``response``
        is the inner ``response.response`` payload."""
        await self._write_line(proc, {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": response,
            },
        })

    async def _answer_can_use_tool(
        self, proc: asyncio.subprocess.Process, action_id: str, request: dict,
        deadline: float,
    ) -> None:
        """Record, wait out, and answer one ``can_use_tool`` control request
        (ADR-0024 layer 3). Writes the ``control_response`` echoing the ACTUAL
        ``request_id`` from the emitted envelope (never ``__PENDING__`` — we
        read the id, so we echo it)."""
        request_id = str(request.get("request_id") or "")
        tool_name = str(request.get("tool_name") or "?")
        tool_input = request.get("input")
        tool_input = tool_input if isinstance(tool_input, dict) else {}
        tool_use_id = request.get("tool_use_id")
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(
            seconds=max(0.0, min(self.ask_timeout_s, deadline - time.monotonic()))
        )
        ask = PermissionAsk(
            ask_id=request_id,
            action_id=action_id,
            tool_use_id=tool_use_id if isinstance(tool_use_id, str) and tool_use_id else None,
            tool=tool_name,
            argument_digest=permission_argument_digest(tool_name, tool_input),
            created_at=created_at,
            expires_at=expires_at,
        )
        self._live_asks[request_id] = ask
        if self.ask_observer is not None:
            self.ask_observer.on_permission_ask(ask)
        t_ask = time.monotonic()
        behavior: str | None = None
        message: str | None = None
        try:
            answer = await asyncio.wait_for(
                self._next_answer(request_id),
                timeout=(expires_at - datetime.now(UTC)).total_seconds(),
            )
            if answer == "allow":
                behavior, message = "allow", None
            else:
                behavior, message = "deny", f"Owner denied permission for {tool_name}"
        except asyncio.TimeoutError:
            # nobody answered before expires_at: the headless default is deny,
            # and the deed records reason=permission_timeout (ADR-0024).
            behavior, message = "deny", f"Permission ask for {tool_name} timed out"
        finally:
            self._live_asks.pop(request_id, None)
            answer_kind = (
                "allow" if behavior == "allow"
                else "deny" if message and "denied" in message
                else "auto_deny_expired"
            )
            answered_by = "owner-webui" if answer_kind in ("allow", "deny") else "policy-timeout"
            if self.ask_observer is not None:
                try:
                    self.ask_observer.on_permission_answered(
                        ask=ask,
                        answer=answer_kind,
                        answered_by=answered_by,
                        latency_ms=_ms(t_ask),
                    )
                except Exception:  # noqa: BLE001 - observing must never break the run
                    pass

        await self._write_line(proc, {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": {
                    "behavior": behavior,
                    **( {"updatedInput": tool_input} if behavior == "allow" else {}),
                    **( {"message": message} if behavior == "deny" else {}),
                },
            },
        })

    async def _next_answer(self, request_id: str) -> str:
        """The owner's answer for THIS ask. A stale answer (a different
        ``ask_id``) is discarded and we keep waiting — one deed at a time
        means at most one live ask, but a late answer must never resolve a
        new one."""
        while True:
            ask_id, answer = await self._answer_queue.get()
            if ask_id == request_id:
                return answer

    async def _host_loop(
        self, proc: asyncio.subprocess.Process, *, prompt: str,
        action_id: str, deadline: float, stream_path: Path,
    ) -> RunResult:
        """The stream-json host loop (ADR-0024): send the task, read query
        output + control requests, answer ``can_use_tool`` asks, and return
        the RunResult when the turn ends.

        End conditions (the five-way failure taxonomy stays recognizable):
        - a terminal ``result`` message (success → ok; error → reason="error",
          including the host deny/timeout denials that surface as the turn's
          error — the deny TEXT is the summary);
        - the stream closes / the process exits before a result → reason
          "malformed_output" (exit 0) / "error" (non-zero);
        - the wall-clock timeout (handled by :meth:`run`, not here).
        """
        t0 = time.monotonic()
        assert proc.stdout is not None and proc.stdin is not None
        # Stream forensics: persist every stdout line (the raw stream-json)
        # as it arrives. Appending per line — not flushing at the end — means
        # a timeout-killed run still keeps everything it emitted. Best-effort:
        # a persistence failure must never break the run itself.
        stream_file = None
        persisted_path: str | None = None
        try:
            stream_file = stream_path.open("a", encoding="utf-8")
            persisted_path = str(stream_path)
        except OSError as exc:
            logger.warning("stream persistence unavailable for %s: %s", action_id, exc)
        # The task is a user message — the stream-json input the main loop
        # reads (``parent_tool_use_id: null``, no parent tool).
        await self._write_line(proc, {
            "type": "user",
            "message": {"role": "user", "content": prompt},
            "parent_tool_use_id": None,
        })

        payload: dict | None = None
        stdout_lines: list[str] = []
        # ADR-0024: did any ask auto-expire (owner never answered) during this
        # run? If so and the run fails, the reason is permission_timeout.
        permission_timed_out = False
        # The stdout reader keeps the pipe drained at ALL times — including
        # while the loop is blocked waiting for the owner's answer on an ask.
        # py-claw's permission channel (inside the executor) does a BLOCKING
        # stdin read while awaiting the host response; if our loop is NOT
        # reading its stdout, the pipe buffer fills and the child's next
        # stdout write blocks, which starves the child's scheduler and its
        # blocking stdin read never gets scheduled → deadlock. So: a
        # dedicated reader task feeds a queue, and the main loop consumes it.
        out_q: asyncio.Queue[bytes] = asyncio.Queue()

        async def _stdout_reader() -> None:
            try:
                while True:
                    b = await proc.stdout.readline()
                    if not b:
                        break
                    await out_q.put(b)
            finally:
                await out_q.put(b"")  # sentinel: EOF

        reader_task = asyncio.create_task(_stdout_reader())

        async def _read_stdout_line() -> bytes:
            """The next stdout line; ``b""`` on EOF (the reader's sentinel).
            A momentary empty is NOT EOF — the reader only emits ``b""`` when
            the process has actually closed stdout (exited)."""
            return await out_q.get()

        try:
            while True:
                line_bytes = await _read_stdout_line()
                if not line_bytes:
                    break  # EOF: the process exited
                line = line_bytes.decode("utf-8", "replace").strip()
                if not line:
                    continue
                stdout_lines.append(line)
                if stream_file is not None:
                    try:
                        stream_file.write(line + "\n")
                    except OSError:
                        stream_file.close()
                        stream_file = None
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue  # non-JSON noise: ignore, like --print mode did
                if not isinstance(data, dict):
                    continue
                mtype = data.get("type")
                if mtype == "control_request":
                    request = data.get("request")
                    request = request if isinstance(request, dict) else {}
                    request_id = data.get("request_id")
                    if request.get("subtype") == "can_use_tool":
                        # layer 3: the ask → authorization channel (records
                        # action.permission_asked via the observer, answers
                        # over the owner channel or auto-denies on expiry).
                        # The request_id is on the ENVELOPE (top-level), not
                        # the inner request — pass it explicitly.
                        if request_id:
                            request = dict(request)
                            request["request_id"] = request_id
                        await self._answer_can_use_tool(proc, action_id, request, deadline)
                    elif request_id:
                        # other control requests (initialize, ...): answer
                        # minimally so the loop never stalls.
                        await self._respond_to_control_request(proc, str(request_id), {})
                elif mtype == "result":
                    # the terminal fact: success (SDKResultSuccess) or error
                    # (SDKResultError with `errors`). Fills RunResult exactly
                    # as the --print payload used to.
                    payload = data
                    break
                # session_state / tool_progress / assistant / stream_event:
                # accumulated as context (tool progress is visible in the
                # stdout tail for forensics); the run ends on the result.
        finally:
            # the stream file is the forensics of THIS run; close it even when
            # the loop is cancelled (timeout kill) so nothing is lost.
            if stream_file is not None:
                try:
                    stream_file.close()
                except OSError:
                    pass
            # stop the reader and let it finish; it is the sole owner of the
            # stdout pipe, so no double-read.
            if not reader_task.done():
                reader_task.cancel()
            try:
                await reader_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

        exit_code = proc.returncode
        if exit_code is None:
            # py-claw keeps running after its turn (it reads stdin until EOF),
            # so we close our write end to let it exit, then reap the child.
            try:
                proc.stdin.close()
            except Exception:  # noqa: BLE001 - best-effort
                pass
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                await _kill(proc)
                exit_code = proc.returncode

        # drain stderr (best-effort; the process is exiting)
        stderr_text = ""
        if proc.stderr is not None:
            try:
                stderr_text = (await asyncio.wait_for(proc.stderr.read(), timeout=2)).decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                stderr_text = ""
        tail = "\n".join(stdout_lines)[:500] or None

        def _finish(ok: bool, summary: str, reason: str | None, detail: str | None) -> RunResult:
            p = payload or {}
            return RunResult(
                ok=ok, summary=summary, exit_code=exit_code, duration_ms=_ms(t0),
                num_turns=p.get("num_turns"),
                cost_usd=p.get("total_cost_usd"),
                stop_reason=p.get("stop_reason"),
                denied_tools=[
                    str(d.get("tool_name"))
                    for d in (p.get("permission_denials") or [])
                    if isinstance(d, dict)
                ],
                reason=reason,
                detail=(detail if detail is not None else (stderr_text[:500] if stderr_text else None)),
                session_id=p.get("session_id"),
                stream_path=persisted_path,
            )

        if payload is None:
            tail = "\n".join(stdout_lines)[:500] or None
            if exit_code is not None and exit_code != 0:
                # a non-zero exit is an error, whatever the stream says
                return _finish(
                    False,
                    f"py-claw exited {exit_code} without a result message",
                    "error",
                    tail,
                )
            # exit 0 (or no exit code yet) but no parseable result: an honest
            # failure, never a silent empty success — a completed deed must
            # have a result to record.
            return _finish(
                False,
                "py-claw exited 0 but produced no parseable result message",
                "malformed_output",
                tail,
            )
        if payload.get("is_error"):
            errors = payload.get("errors")
            if errors:
                summary = "; ".join(str(e) for e in errors)[:500]
            elif payload.get("stop_reason"):
                summary = f"stopped: {payload['stop_reason']}"
            else:
                summary = "py-claw reported an error without a message"
            # A permission denial (owner deny / ask expiry) surfaces as the
            # turn's error; keep its exact text as the honest one-liner.
            return _finish(False, summary, "error", tail if tail else None)
        summary = str(payload.get("result") or "").strip()
        if not summary:
            # a result payload with no result text: not a success — an empty
            # summary would record a deed that did nothing.
            return _finish(
                False,
                "py-claw result message carried no result text",
                "malformed_output",
                tail if tail else None,
            )
        return _finish(True, summary, None, None)
