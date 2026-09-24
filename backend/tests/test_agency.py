"""Agency (ADR-0018/0019/0020): policy, runner, Phase-A loop, live wiring.

Hermetic by construction:
- the **policy** is a pure function — tested directly;
- the **runner** is tested against a *fake* ``py-claw`` binary (a shell script
  that emits a canned result / sleeps / fails) — no real model, no network;
- the **service** Phase-A loop is tested against a stub runner (deterministic
  event writing) plus one real end-to-end HTTP test against the fake binary;
- the **live wiring** (default-off, flag-on, cognitive-flag application) is
  tested through ``build_app``.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from resident.agency.policy import ActionPolicy
from resident.agency.runner import PyClawRunner
from resident.agency.service import AgencyService, _prompt_for
from resident.event_store import EventStore
from resident.mind_loop import MindLoop
from resident.models import (
    ACTION_COMPLETED,
    ACTION_FAILED,
    ACTION_STARTED,
    INTENT_PROPOSED,
    INTENT_REJECTED,
    EventCreate,
)
from resident.route_policy import RoutePolicy

# -------------------------------------------------------------------------- policy


def test_policy_default_allows_only_read_only():
    policy = ActionPolicy()
    assert policy.evaluate("read_only").allowed is True
    for cap in ("file_write_local", "network", "destructive"):
        d = policy.evaluate(cap)
        assert d.allowed is False
        assert d.reason == f"capability_not_granted:{cap}"


def test_policy_denies_unknown_and_missing():
    policy = ActionPolicy()
    assert policy.evaluate(None).allowed is False
    assert "unknown_capability" in policy.evaluate(None).reason
    assert policy.evaluate("hacks").allowed is False
    assert policy.evaluate("").allowed is False


def test_policy_capability_not_source_is_the_gate():
    """A human declaring a dangerous capability is still denied (ADR-0019)."""
    policy = ActionPolicy()
    assert policy.evaluate("destructive", source="user").allowed is False
    assert policy.evaluate("network", source="user").allowed is False
    assert policy.evaluate("read_only", source="user").allowed is True


def test_policy_custom_grant_opens_matching_surface():
    policy = ActionPolicy(allowed_capabilities={"read_only", "file_write_local"})
    d = policy.evaluate("file_write_local")
    assert d.allowed is True
    # ADR-0024: the gate's output is the py-claw rule list (allow entries),
    # and the derived tool names for the prompt / tool_surface deed fact.
    assert d.rules == ("Read", "Glob", "Grep", "Write", "Edit")
    assert d.tools == ("Read", "Glob", "Grep", "Write", "Edit")


def test_policy_read_only_rule_surface():
    d = ActionPolicy().evaluate("read_only")
    # the base rules are bare tool names in py-claw's own grammar
    assert d.rules == ("Read", "Glob", "Grep")
    assert d.tools == ("Read", "Glob", "Grep")
    # a per-repo owner grant is a later, trivial list extension (ADR-0024):
    # adding "Bash(gh :*)" makes the rule list a data change, not a code change
    extended = d.rules + ("Bash(gh :*)",)
    assert "Bash(gh :*)" in extended
    assert "Read" in extended


def test_policy_denied_decision_has_no_rules():
    d = ActionPolicy().evaluate("destructive")
    assert d.allowed is False
    assert d.rules == ()
    assert d.tools == ()


def test_policy_is_deterministic():
    policy = ActionPolicy()
    a = policy.evaluate("network")
    b = policy.evaluate("network")
    assert a == b


# -------------------------------------------------------------------------- runner


def _write_fake(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable fake ``py-claw`` (a bash script) and return its path."""
    script = tmp_path / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _write_fake_py(tmp_path: Path, name: str, body: str) -> str:
    """Write an executable fake ``py-claw`` (a Python script, for the
    stream-json ask→answer tests — bash cannot easily echo back a parsed
    request_id) and return its path."""
    script = tmp_path / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\n" + body + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


_OK_BODY = (
    'echo \'{"type":"result","subtype":"success","duration_ms":42,'
    '"duration_api_ms":40,"is_error":false,"num_turns":2,'
    '"result":"完成了文件分析：工作区有 3 个文件。",'
    '"stop_reason":"end_turn","total_cost_usd":0.001,"usage":{},'
    '"modelUsage":{},"permission_denials":[{"tool_name":"Bash","tool_use_id":"t1",'
    '"tool_input":{}}],"uuid":"u1","session_id":"s1"}\''
)
_FAIL_BODY = (
    'echo \'{"type":"result","subtype":"error_during_execution","duration_ms":10,'
    '"duration_api_ms":10,"is_error":true,"num_turns":1,"stop_reason":"end_turn",'
    '"total_cost_usd":0.0,"usage":{},"modelUsage":{},'
    '"permission_denials":[],"errors":["boom"],"uuid":"u2","session_id":"s2"}\'\n'
    "exit 1"
)


async def test_runner_ok_extracts_result_and_denials(tmp_path):
    fake = _write_fake(tmp_path, "fake_ok.sh", _OK_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="分析工作区", action_id="act_test",
        allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
    )
    assert result.ok is True
    assert "文件分析" in result.summary
    assert result.num_turns == 2
    assert result.denied_tools == ["Bash"]
    assert result.session_id == "s1"
    assert result.reason is None


async def test_runner_writes_permission_allow_list(tmp_path):
    """The read_only allow-list authorizes the TOOL SURFACE by name (ADR-0019
    layer 2): Read/Glob/Grep, nothing else. It is NOT path-scoped — path
    containment is layer 3 (PYCLAW_FS_ROOT), which resolves and contains every
    file path. There is no Edit/Write, no Bash, no WebFetch/WebSearch."""
    fake = _write_fake(tmp_path, "fake_ok.sh", _OK_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    await runner.run(
        prompt="x", action_id="act_perm",
        allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
    )
    sandbox = tmp_path / "ws" / "act_perm"
    settings = (sandbox / ".claude" / "settings.local.json")
    data = json.loads(settings.read_text(encoding="utf-8"))
    allow = data["permissions"]["allow"]
    assert allow == ["Read", "Glob", "Grep"]
    for tool in ("Edit", "Write", "Bash", "WebFetch", "WebSearch"):
        assert tool not in allow


async def test_runner_timeout_kills_and_reports(tmp_path):
    fake = _write_fake(tmp_path, "fake_slow.sh", "sleep 30")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_timeout",
        allowed_tools=["Read"], timeout_s=0.3,
    )
    assert result.ok is False
    assert result.reason == "timeout"


async def test_runner_binary_missing_reports_error(tmp_path):
    runner = PyClawRunner(pyclaw_bin=str(tmp_path / "nope.sh"), sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_missing",
        allowed_tools=["Read"], timeout_s=5,
    )
    assert result.ok is False
    assert result.reason == "error"


async def test_runner_nonzero_exit_is_failure(tmp_path):
    fake = _write_fake(tmp_path, "fake_fail.sh", _FAIL_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_fail",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "error"
    assert "boom" in result.summary


# ------------------------------------------------------- stream forensics
# The runner persists the py-claw host session's raw stream-json as a SIBLING
# of the deed sandbox (outside the agent's cwd) on every outcome, including a
# timeout kill — that is what makes mid-run self-corrections reconstructable.


async def test_runner_persists_full_stream_next_to_sandbox(tmp_path):
    body = (
        'echo \'{"type":"assistant","message":{"role":"assistant","content":"line-one"}}\'\n'
        + _OK_BODY
    )
    fake = _write_fake(tmp_path, "fake_stream.sh", body)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_stream",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is True
    assert result.stream_path is not None
    sandbox = tmp_path / "ws" / "act_stream"
    stream = Path(result.stream_path)
    # sibling of the deed sandbox — never inside the agent's own workspace
    assert stream.parent == sandbox.parent
    assert not (sandbox / stream.name).exists()
    text = stream.read_text(encoding="utf-8")
    assert "line-one" in text
    assert '"type":"result"' in text


async def test_runner_timeout_keeps_partial_stream(tmp_path):
    """A killed run must still keep everything it emitted before the kill."""
    body = (
        'echo \'{"type":"assistant","message":{"role":"assistant","content":"thinking..."}}\'\n'
        "sleep 30"
    )
    fake = _write_fake(tmp_path, "fake_partial.sh", body)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_partial",
        allowed_tools=["Read"], timeout_s=1.0,
    )
    assert result.ok is False
    assert result.reason == "timeout"
    assert result.stream_path is not None
    text = Path(result.stream_path).read_text(encoding="utf-8")
    assert "thinking..." in text


async def test_runner_plants_protective_repo_in_host_repo_shape(tmp_path):
    """The round-2 incident shape: the sandbox lives INSIDE a host git
    repository. With the empty protective repository the runner plants at the
    sandbox root, a bare ``git`` run at the sandbox root resolves the sandbox
    ITSELF as its toplevel — never the host repo it was carved out of."""
    host_repo = tmp_path / "hostrepo"
    host_repo.mkdir()
    subprocess.run(["git", "-C", str(host_repo), "init", "-q"], check=True)
    fake = _write_fake(tmp_path, "fake_prot.sh", _OK_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=host_repo / "ws")
    result = await runner.run(
        prompt="x", action_id="act_prot",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is True
    sandbox = host_repo / "ws" / "act_prot"
    out = subprocess.run(
        ["git", "-C", str(sandbox), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    )
    assert out.returncode == 0
    assert out.stdout.strip() == os.path.realpath(str(sandbox))


# ------------------------------------------------------- allow-list + env seam
# ADR-0019 layer 2 (tool-surface allow-list) + layer 3 (PYCLAW_FS_ROOT env seam)
# + the fail-closed sandbox guards. A fake py-claw that dumps its spawn env
# lets the tests observe exactly what the subprocess was given.


_ENV_DUMP_BODY = (
    "printf 'PYCLAW_FS_ROOT=%s\\nXDG_CONFIG_HOME=%s\\nXDG_DATA_HOME=%s\\n"
    "XDG_CACHE_HOME=%s\\nPYTHONIOENCODING=%s\\n' "
    '"$PYCLAW_FS_ROOT" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$PYTHONIOENCODING" '
    "> env_dump.txt\n"
    + _OK_BODY
)


def _read_env_dump(sandbox: Path) -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in (sandbox / "env_dump.txt").read_text(encoding="utf-8").splitlines()
        if "=" in line
    }


def _expect_rules(allowed_tools: list[str]) -> list[str]:
    """The expected allow-list for a tool surface: each approved tool bare by
    name (layer 2 authorizes the tool surface, not paths; path containment is
    layer 3, the runtime FS root)."""
    return [str(tool) for tool in allowed_tools]


async def _run_and_read_allow(
    tmp_path: Path, name: str, tools: list[str],
    api_config: dict | None = None,
) -> tuple[list[str], Path]:
    """Run the env-dumping fake py-claw once and return (allow-list, sandbox)."""
    fake = _write_fake(tmp_path, f"fake_{name}.sh", _ENV_DUMP_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws",
                          api_config=api_config)
    await runner.run(prompt="x", action_id=f"act_{name}", allowed_tools=tools,
                     timeout_s=30)
    sandbox = tmp_path / "ws" / f"act_{name}"
    settings = sandbox / ".claude" / "settings.local.json"
    allow = json.loads(settings.read_text(encoding="utf-8"))["permissions"]["allow"]
    return allow, sandbox


async def test_runner_opens_write_tools_for_file_write_surface(tmp_path):
    """file_write_local opens Write and Edit (by name) exactly like Read — the
    executor's surface is the granted capability's tools; path containment is
    layer 3 (PYCLAW_FS_ROOT contains the write target inside the sandbox)."""
    tools = ["Read", "Glob", "Grep", "Write", "Edit"]
    allow, sandbox = await _run_and_read_allow(tmp_path, "fw", tools)
    assert allow == _expect_rules(tools)
    for tool in ("Bash", "WebFetch", "WebSearch"):
        assert tool not in allow


async def test_runner_lists_all_tools_bare(tmp_path):
    """Every approved tool appears BARE by name: the allow-list authorizes the
    tool surface, not paths — path containment is layer 3 (the runtime FS
    root), and Glob/Grep's permission content (the search pattern) was never
    scope-able at this layer anyway."""
    tools = ["Read", "Glob", "Grep", "WebFetch", "WebSearch", "Bash"]
    allow, sandbox = await _run_and_read_allow(tmp_path, "bare", tools)
    assert allow == _expect_rules(tools)
    for tool in tools:
        assert tool in allow


async def test_runner_env_always_carries_fs_root(tmp_path):
    """Layer-3 seam: PYCLAW_FS_ROOT and PYTHONIOENCODING=utf-8 are in the
    spawn env even when no api_config is deployed (the env is never inherited
    as-is; UTF-8 stdio is the one encoding the runner's UTF-8 decode agrees
    with, so a non-UTF-8 locale cannot corrupt the deeds)."""
    fake = _write_fake(tmp_path, "fake_env_ro.sh", _ENV_DUMP_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    await runner.run(prompt="x", action_id="act_env_ro", allowed_tools=["Read"],
                     timeout_s=30)
    sandbox = tmp_path / "ws" / "act_env_ro"
    env = _read_env_dump(sandbox)
    assert env["PYCLAW_FS_ROOT"] == str(sandbox.resolve())
    assert env["PYTHONIOENCODING"] == "utf-8"


async def test_runner_env_scopes_xdg_state_when_api_config_set(tmp_path):
    """With a deployed api_config, ALL XDG state dirs point inside the per-run
    sandbox: the subprocess reads our config and never the user's global XDG
    state."""
    fake = _write_fake(tmp_path, "fake_env_xdg.sh", _ENV_DUMP_BODY)
    runner = PyClawRunner(
        pyclaw_bin=fake, sandbox_root=tmp_path / "ws",
        api_config={"api": {"api_url": "http://u", "api_key": "k", "model": "m"}},
    )
    await runner.run(prompt="x", action_id="act_env_xdg", allowed_tools=["Read"],
                     timeout_s=30)
    sandbox = tmp_path / "ws" / "act_env_xdg"
    env = _read_env_dump(sandbox)
    xdg = sandbox / ".xdg"
    assert env["PYCLAW_FS_ROOT"] == str(sandbox.resolve())
    assert env["XDG_CONFIG_HOME"] == str(xdg)
    assert env["XDG_DATA_HOME"] == str(xdg / "data")
    assert env["XDG_CACHE_HOME"] == str(xdg / "cache")
    # the per-run py-claw config is still written where py-claw expects it
    cfg = xdg / "py-claw" / "config.json"
    assert json.loads(cfg.read_text(encoding="utf-8"))["api"]["model"] == "m"


# ---------------------------------------------------------- sandbox guards
# Fail closed BEFORE spawning: a sandbox that is a symlink, or resolves
# outside the sandbox root (which itself must be a real directory), is a
# sandbox_violation — the subprocess is never launched.


async def test_runner_symlinked_sandbox_fails_closed_without_spawn(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "spawned"
    fake = _write_fake(tmp_path, "fake_marker.sh", f"touch {marker}")
    root = tmp_path / "ws"
    root.mkdir()
    (root / "act_evil").symlink_to(outside)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=root)
    result = await runner.run(
        prompt="x", action_id="act_evil",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "sandbox_violation"
    assert "symlink" in (result.detail or "")
    assert not marker.exists()  # the subprocess was never spawned


async def test_runner_symlinked_sandbox_root_fails_closed(tmp_path):
    outside = tmp_path / "real_ws"
    outside.mkdir()
    root_link = tmp_path / "ws_link"
    root_link.symlink_to(outside)
    marker = tmp_path / "spawned_root"
    fake = _write_fake(tmp_path, "fake_marker_root.sh", f"touch {marker}")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=root_link)
    result = await runner.run(
        prompt="x", action_id="act_any",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "sandbox_violation"
    assert "sandbox_root" in (result.detail or "")
    assert not marker.exists()


def test_sandbox_dir_rejects_escaping_action_ids(tmp_path):
    runner = PyClawRunner(pyclaw_bin="unused", sandbox_root=tmp_path / "ws")
    for bad in ("", ".", "..", "../evil", "a/b", "a\\b"):
        with pytest.raises(ValueError, match="invalid action id"):
            runner.sandbox_dir(bad)
    assert runner.sandbox_dir("act_00") == tmp_path / "ws" / "act_00"


# -------------------------------------------- structural boundary (read_only)


async def test_read_only_boundary_is_tool_surface_plus_fs_root(tmp_path):
    """Structural boundary (the honest test of what the agency controls):
    with the read_only surface, the allow-list authorizes only Read/Glob/Grep
    by name (layer 2) — no shell, no network, no write tool — AND the spawn
    env carries the resolved sandbox as PYCLAW_FS_ROOT (layer 3), which
    resolves and contains every file path: an out-of-sandbox absolute path, a
    symlink, a ``..`` traversal, and Glob/Grep search roots (enforced inside
    py-claw's own tests). Path containment is the runtime FS root, not the
    allow-list."""
    tools = ["Read", "Glob", "Grep"]
    allow, sandbox = await _run_and_read_allow(tmp_path, "struct", tools)
    # layer 2: the tool surface is exactly the read_only tools, bare by name
    assert allow == ["Read", "Glob", "Grep"]
    for tool in ("Bash", "WebFetch", "WebSearch", "Read*", "Edit", "Write"):
        assert tool not in allow
    # layer 3: the spawn env carries the resolved sandbox as the FS root —
    # this is what actually contains file paths (enforced in py-claw)
    env = _read_env_dump(sandbox)
    assert env["PYCLAW_FS_ROOT"] == str(sandbox.resolve())


# -------------------------------------------------------------------------- service


class _StubRunner:
    """A runner stub that records calls and returns a canned result."""

    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    def sandbox_dir(self, action_id: str) -> Path:
        return Path("/sandbox") / action_id

    async def run(self, *, prompt, action_id, allowed_tools, timeout_s):
        self.calls.append(
            {"prompt": prompt, "action_id": action_id,
             "allowed_tools": list(allowed_tools), "timeout_s": timeout_s}
        )
        return self._result


def _ok_stub_result():
    from resident.agency.runner import RunResult

    return RunResult(
        ok=True, summary="完成了任务", exit_code=0, duration_ms=5,
        num_turns=1, cost_usd=0.0, denied_tools=[],
    )


def test_service_allowed_writes_full_provenance_chain(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="分析文件", capability="read_only", source="user")
    )
    assert out["status"] == "completed"

    intent = store.list(10, type_prefix=INTENT_PROPOSED)[0]
    completed = store.list(10, type_prefix=ACTION_COMPLETED)[0]
    experience = store.list(10, type_prefix="experience.created")[0]
    # causal chain: experience -> completed -> intent
    assert completed.links["caused_by"] == [intent.id]
    assert experience.links["caused_by"] == [completed.id]
    # every link resolves (store invariant)
    assert store.get(intent.id) is not None
    # the runner ran with the read-only ceiling
    assert stub.calls[0]["allowed_tools"] == ["Read", "Glob", "Grep"]
    # objective content has no subjective fields
    assert "summary" in completed.content
    assert completed.content["capability"] == "read_only"
    assert completed.provenance["runner"] == "py-claw"


def test_service_denied_writes_rejection_and_never_runs(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="删掉日志", capability="destructive", source="user")
    )
    assert out["status"] == "rejected"
    assert store.count(INTENT_PROPOSED) == 1
    assert store.count(INTENT_REJECTED) == 1
    assert store.count(ACTION_COMPLETED) == 0
    assert store.count(ACTION_FAILED) == 0
    assert stub.calls == []  # the executor was never launched
    rej = store.list(10, type_prefix=INTENT_REJECTED)[0]
    assert rej.content["reason"] == "capability_not_granted:destructive"


def test_service_failure_writes_failed_plus_memory_anchor(tmp_path):
    from resident.agency.runner import RunResult

    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(
        RunResult(ok=False, summary="boom", exit_code=1, duration_ms=3,
                  reason="error", detail="boom")
    )
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="x", capability="read_only", source="user")
    )
    assert out["status"] == "failed"
    assert store.count(ACTION_FAILED) == 1
    assert store.count("experience.created") == 1  # a failure is still an experience
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.content["reason"] == "error"
    # the objective record of WHY it failed (ADR-0020 provenance) — e.g.
    # "Read requires permission" for a denial — not just a bare reason code
    assert failed.content["summary"] == "boom"


def test_service_sandbox_violation_lands_as_failed_deed(tmp_path):
    """A sandbox_violation from the runner flows through unchanged as an
    honest ``action.failed(reason="sandbox_violation")`` + the experience
    anchor — the deed is recorded, nothing was ever executed."""
    outside = tmp_path / "real_ws"
    outside.mkdir()
    root_link = tmp_path / "ws_link"
    root_link.symlink_to(outside)
    fake = _write_fake(tmp_path / "bin", "fake_violation.sh",
                       f"touch {outside / 'spawned'}")
    store = EventStore(tmp_path / "e.sqlite3")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=root_link)
    agency = AgencyService(store, runner, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="x", capability="read_only", source="user")
    )
    assert out["status"] == "failed"
    assert out["reason"] == "sandbox_violation"
    assert store.count(ACTION_FAILED) == 1
    assert store.count("experience.created") == 1  # the experience anchor
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.content["reason"] == "sandbox_violation"
    # nothing was completed, and the runner never spawned (fail closed)
    assert store.count(ACTION_COMPLETED) == 0
    assert not (outside / "spawned").exists()


def test_service_rejects_invalid_source(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    with pytest.raises(ValueError):
        asyncio.run(
            agency.intake(text="x", capability="read_only", source="god")
        )


# ----------------------------------------------------------------------- profiles


def test_active_v3_registered_with_agency_flag():
    from resident.profiles import REGISTRY, ACTIVE_V3

    assert "active-v3" in REGISTRY
    assert ACTIVE_V3.cognitive_flags["agency"] is True
    # it inherits the active-v2 hygiene flags
    assert ACTIVE_V3.cognitive_flags["recall_pool_hygiene"] is True


def test_profile_flag_kwargs_split():
    from resident.main import _profile_flag_kwargs
    from resident.profiles import ACTIVE_V3, SEALED_BASELINE

    mind, sleep, world, agency = _profile_flag_kwargs(ACTIVE_V3)
    assert agency is True
    assert mind == {
        "recall_pool_hygiene": True,
        "thread_selection": "material",
        "context_exclude_prev_thread": True,
    }
    assert sleep == {"recall_pool_hygiene": True}
    assert world == {}

    # the sealed profile carries no flags -> byte-identical runtime
    mind, sleep, world, agency = _profile_flag_kwargs(SEALED_BASELINE)
    assert (mind, sleep, world, agency) == ({}, {}, {}, False)


# ------------------------------------------------------------------------ main


def test_agency_off_by_default(tmp_path):
    from resident.main import build_app

    app = build_app(tmp_path)
    client = TestClient(app)
    with client:
        assert client.get("/api/agency/health").json()["enabled"] is False
        r = client.post("/api/agency/intent", json={"text": "x", "capability": "read_only"})
        assert r.status_code == 503


def test_agency_enabled_via_env_and_phase_a_loop(tmp_path, monkeypatch):
    from resident.main import build_app

    fake = _write_fake(tmp_path / "bin", "fake_pyclaw.sh", _OK_BODY)
    monkeypatch.setenv("RESIDENT_AGENCY", "1")
    monkeypatch.setenv("RESIDENT_PYCLAW_BIN", fake)
    monkeypatch.setenv("RESIDENT_AGENCY_TIMEOUT_S", "30")

    app = build_app(tmp_path)
    assert app.state.agency is not None
    client = TestClient(app)
    with client:
        health = client.get("/api/agency/health").json()
        assert health["enabled"] is True
        assert health["allowed_capabilities"] == ["read_only"]

        r = client.post(
            "/api/agency/intent", json={"text": "分析工作区", "capability": "read_only"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        # the objective deed + memory anchor are in the store
        store = app.state.store
        assert store.count(INTENT_PROPOSED) == 1
        assert store.count(ACTION_COMPLETED) == 1
        assert store.count("experience.created") == 1
        # and the deed is recallable through memory (action -> Memory -> wake seam)
        recalled = app.state.memory.recent(10, type_prefix="experience.created")
        assert any("行动" in e.text for e in recalled)


def test_agency_intent_denied_over_http(tmp_path, monkeypatch):
    from resident.main import build_app

    fake = _write_fake(tmp_path / "bin", "fake_pyclaw.sh", _OK_BODY)
    monkeypatch.setenv("RESIDENT_AGENCY", "1")
    monkeypatch.setenv("RESIDENT_PYCLAW_BIN", fake)
    app = build_app(tmp_path)
    client = TestClient(app)
    with client:
        r = client.post(
            "/api/agency/intent", json={"text": "删库", "capability": "destructive"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "rejected"
        assert app.state.store.count(ACTION_COMPLETED) == 0


def test_cognitive_flags_applied_in_live_runtime(tmp_path, monkeypatch):
    from resident.main import build_app

    # active-v2: the live runtime must now apply the profile's cognitive flags
    monkeypatch.setenv("RESIDENT_PROFILE", "active-v2")
    app = build_app(tmp_path)
    assert app.state.memory.recall_pool_hygiene is True
    assert app.state.mind.thread_selection == "material"

    # sealed-baseline: flag-off defaults preserved (byte-identical cognition)
    monkeypatch.setenv("RESIDENT_PROFILE", "sealed-baseline")
    app2 = build_app(tmp_path / "sealed")
    assert app2.state.memory.recall_pool_hygiene is False
    assert app2.state.mind.thread_selection == "recent"


# --------------------------------------------------------------------- Phase B
# v1.0 (ADR-0018, Phase B): the Mind PROPOSES an intent → deterministic policy
# gates it → the Agency executes → objective events + memory anchor. The Mind
# never grants itself permission (principle 1); the circadian only ROUTES the
# proposal (as a background task that cannot block a life beat).


class _ProposingProvider:
    """A provider that persists a thought AND proposes a read-only intent."""

    model_id = "stub/proposing"

    def complete(self, *, system: str, prompt: str) -> str:
        return json.dumps(
            {
                "decision": "persist",
                "text": "我注意到工作区里的文件可以整理一下。",
                "links": {},
                "visibility": "private",
                "intent": {"text": "列出工作区里的文件并分析", "capability": "read_only"},
            },
            ensure_ascii=False,
        )


class _StubMind:
    async def wake_once(self, *a, **k):
        return {}


class _StubSleep:
    async def sleep_once(self, *a, **k):
        return {"result": "noop", "actions": []}


class _StubAgency:
    """Records intake calls; a stand-in for AgencyService in routing tests."""

    def __init__(self):
        self.calls: list[dict] = []

    async def intake(self, *, text, capability, source="user",
                     caused_by=None, thread_id=None):
        self.calls.append({
            "text": text, "capability": capability, "source": source,
            "caused_by": caused_by, "thread_id": thread_id,
        })
        return {"status": "completed", "action_id": "act_x"}


def _personal_store(tmp_path, name="e.sqlite3") -> EventStore:
    s = EventStore(tmp_path / name)
    s.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "今天读了一本书"},
    ))
    return s


def test_mind_emits_proposed_intent_when_flag_on(tmp_path):
    s = _personal_store(tmp_path)
    m = MindLoop(s, provider=_ProposingProvider(),
                 policy=RoutePolicy(exploration=0.0), rng=random.Random(0),
                 agency_propose=True)
    res = asyncio.run(m.wake_once("stub"))
    assert res["result"] == "thought"
    assert res["proposed_intent"] == {
        "text": "列出工作区里的文件并分析", "capability": "read_only",
    }


def test_mind_does_not_emit_intent_when_flag_off(tmp_path):
    s = _personal_store(tmp_path)
    # the provider still emits an intent, but with the flag OFF (default) it
    # must be ignored — the output contract carries no `intent` field, and the
    # extraction gate is off, so behaviour is byte-identical to pre-v1.0.
    m = MindLoop(s, provider=_ProposingProvider(),
                 policy=RoutePolicy(exploration=0.0), rng=random.Random(0))
    res = asyncio.run(m.wake_once("stub"))
    assert res["result"] == "thought"
    assert res["proposed_intent"] is None


async def test_circadian_routes_mind_intent_to_agency(tmp_path):
    from resident.circadian import CircadianOrchestrator

    store = _personal_store(tmp_path)
    agency = _StubAgency()
    orch = CircadianOrchestrator(store, _StubMind(), _StubSleep(), agency=agency, tz=UTC)
    res = {
        "route": "personal", "result": "thought", "event_id": "evt_thought",
        "thread_id": "th_1",
        "proposed_intent": {"text": "列出工作区文件", "capability": "read_only"},
    }
    orch._route_intent(res)
    # wait for the background task AND its done-callback (which discards it from
    # the live set) to run — yield until the set drains.
    for _ in range(10):
        if not orch._pending_actions:
            break
        await asyncio.sleep(0)
    assert len(agency.calls) == 1
    call = agency.calls[0]
    assert call["source"] == "mind"              # the Mind proposed it
    assert call["capability"] == "read_only"
    assert call["caused_by"] == ["evt_thought"]  # traces back to the thought
    assert call["thread_id"] == "th_1"
    assert orch._pending_actions == set()        # the task drained the set


def test_circadian_no_agency_is_noop(tmp_path):
    from resident.circadian import CircadianOrchestrator

    store = _personal_store(tmp_path)
    orch = CircadianOrchestrator(store, _StubMind(), _StubSleep(), tz=UTC)  # agency=None
    res = {
        "route": "personal", "result": "thought", "event_id": "evt_thought",
        "proposed_intent": {"text": "x", "capability": "read_only"},
    }
    orch._route_intent(res)  # silent no-op: no crash, no task scheduled
    assert orch._pending_actions == set()


async def test_live_end_to_end_mind_proposes_and_acting(tmp_path, monkeypatch):
    """Full Phase B loop through the live app: mind proposes → policy gates →
    Agency drives py-claw (fake binary) → objective deed + memory anchor,
    attributed to the resident (mind-proposed) and recallable through Memory."""
    from resident.main import build_app

    fake = _write_fake(tmp_path / "bin", "fake_pyclaw.sh", _OK_BODY)
    monkeypatch.setenv("RESIDENT_AGENCY", "1")
    monkeypatch.setenv("RESIDENT_PYCLAW_BIN", fake)
    monkeypatch.setenv("RESIDENT_AGENCY_TIMEOUT_S", "30")

    app = build_app(tmp_path)
    assert app.state.mind.agency_propose is True  # the live mind may propose
    store = app.state.store
    store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "今天读了一本书"},
    ))
    # make the live mind propose an intent on its next persist reflection
    app.state.mind.provider = _ProposingProvider()
    app.state.mind._force_route = "personal"
    res = await app.state.mind.wake_once("e2e")
    assert res["result"] == "thought"
    assert res["proposed_intent"] is not None

    # route the proposal exactly as a circadian beat would (background task)
    app.state.orchestrator._route_intent(res)
    while app.state.orchestrator._pending_actions:
        await asyncio.sleep(0)

    intents = store.list(10, type_prefix=INTENT_PROPOSED)
    assert len(intents) == 1
    assert intents[0].actor == "resident"  # mind-proposed, not a human
    assert store.count(ACTION_COMPLETED) == 1
    assert store.count("experience.created") == 1
    completed = store.list(10, type_prefix=ACTION_COMPLETED)[0]
    assert completed.links["caused_by"] == [intents[0].id]
    # the objective deed is recallable through Memory (action -> Memory -> wake)
    recalled = app.state.memory.recent(10, type_prefix="experience.created")
    assert any("行动" in e.text for e in recalled)


async def test_mind_proposed_destructive_is_rejected(tmp_path):
    """Principle 1, Phase B: a Mind-proposed intent cannot grant itself a
    capability the policy has not granted. A destructive proposal from the Mind
    lands as ``intent.rejected`` (actor=resident) and the executor is NEVER
    launched — even though the runner would have succeeded."""
    store = _personal_store(tmp_path)
    stub = _StubRunner(_ok_stub_result())  # would succeed IF ever called
    agency = AgencyService(store, stub, policy=ActionPolicy())  # read_only only
    # a real thought the (Mind-proposed) intent is causally rooted in
    thought = store.append(EventCreate(
        type="thought.created", visibility="private", content={"text": "想清理旧文件"},
    ))
    out = await agency.intake(
        text="删除工作区里的旧文件", capability="destructive", source="mind",
        caused_by=[thought.id],
    )
    assert out["status"] == "rejected"
    assert store.count(INTENT_PROPOSED) == 1
    assert store.count(INTENT_REJECTED) == 1
    assert store.count(ACTION_COMPLETED) == 0
    assert store.count(ACTION_FAILED) == 0
    assert stub.calls == []  # the executor was never launched
    # the proposal is attributed to the resident (the Mind proposed it)
    intent = store.list(10, type_prefix=INTENT_PROPOSED)[0]
    assert intent.actor == "resident"
    assert intent.links["caused_by"] == [thought.id]
    rej = store.list(10, type_prefix=INTENT_REJECTED)[0]
    assert rej.content["reason"] == "capability_not_granted:destructive"


# ------------------------------------------- Phase B, the final link (closed loop)
# ADR-0020's objective/subjective hard line, end to end: the Agency writes the
# OBJECTIVE deed (intent.* / action.* + the experience.created memory anchor);
# a LATER Mind wake recalls that anchor, and the wake's reflection persists a
# SUBJECTIVE thought.created that CITES the deed — a downstream event (higher
# seq, never a rewrite) whose link the store validates as non-dangling.


class _CitingProvider:
    """A fake model that reflects only on the material it was SHOWN: it
    persists a subjective thought that cites (``related_to``) exactly the
    deed-family events present in the recalled material. Grounded by
    construction — it never cites an id it was not shown, mirroring the
    output contract's "cite only event ids that appear above" rule."""

    model_id = "stub/citing"

    _DEED_TYPES = frozenset(
        {INTENT_PROPOSED, ACTION_STARTED, ACTION_COMPLETED, "experience.created"}
    )

    def __init__(self) -> None:
        self.last_output: dict | None = None

    def complete(self, *, system: str, prompt: str) -> str:
        ctx = json.loads(prompt)
        cited = [
            r["id"] for r in ctx.get("recent", [])
            if isinstance(r, dict) and r.get("type") in self._DEED_TYPES
        ]
        out = {
            "decision": "persist",
            "text": "回想起来，刚才那次文件分析确实完成了：工作区有 3 个文件。",
            "links": {"related_to": cited},
            "visibility": "private",
        }
        self.last_output = out
        return json.dumps(out, ensure_ascii=False)


async def test_reflection_cites_action_downstream(tmp_path):
    """The closed loop (ADR-0020): a completed deed is later RECALLED by a
    natural Mind wake, and the wake's persisted ``thought.created`` CITES the
    deed — strictly downstream in seq, resolvable (no dangling link), leaving
    the objective record untouched (append-only).

    Seams exercised (all real — the thought is never hand-appended):
    - **Agency**: ``AgencyService.intake`` against a *fake py-claw binary*
      writes ``intent.proposed -> action.started -> action.completed ->
      experience.created`` (the anchor) with a resolvable caused_by chain;
    - **recall**: a later ``MindLoop.wake_once`` on the SAME store — route
      pinned to ``personal`` (the established test-only seam; the route whose
      temporal/semantic paths reach back around the last user message, which
      the state-driven policy selects on its own for this state anyway). The
      wake's OWN multi-path recall surfaces the anchor and the completed deed
      (asserted via the thought's ``metadata["recalled"]`` — not hand-fed);
    - **provider**: the fake model's reflection output cites the deed ids it
      was shown in ``related_to`` (asserted on the provider's own output);
    - **persist**: the Mind's real reflect path stores the thought — the
      store's no-dangling-link invariant validates the cite on append.
    """
    fake = _write_fake(tmp_path / "bin", "fake_pyclaw.sh", _OK_BODY)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    agency = AgencyService(store, runner, policy=ActionPolicy())

    # 1) the OBJECTIVE deed: a user asks, the Agency completes a read-only action
    um = store.append(EventCreate(
        type="conversation.user_message", actor="user", visibility="user_visible",
        content={"text": "帮我分析工作区里的文件"},
    ))
    out = await agency.intake(
        text="分析工作区里的文件", capability="read_only", source="user",
        caused_by=[um.id],
    )
    assert out["status"] == "completed"
    completed = store.get(out["event_id"])
    anchor = store.get(out["experience_id"])
    assert completed is not None and completed.type == ACTION_COMPLETED
    assert anchor is not None and anchor.type == "experience.created"
    intent = store.get(out["intent_id"])
    assert intent is not None
    # the causal chain is resolvable (the store invariant already enforced it)
    assert completed.links["caused_by"] == [intent.id]
    assert anchor.links["caused_by"] == [completed.id]
    completed_seq = completed.seq

    # snapshot the whole objective record — it must come back byte-identical
    # after the wake (append-only: the subjective thought never rewrites it)
    deed_ids = [intent.id, out["started_event_id"], completed.id, anchor.id]
    snapshot = {eid: store.get(eid).model_dump() for eid in deed_ids}

    # 2) a LATER Mind wake on the same store (the deed is now part of its past)
    provider = _CitingProvider()
    mind = MindLoop(
        store, provider=provider,
        policy=RoutePolicy(exploration=0.0), rng=random.Random(0),
    )
    mind._force_route = "personal"
    res = await mind.wake_once("later")
    assert res["result"] == "thought"
    assert res["route"] == "personal"
    thought = store.get(res["event_id"])
    assert thought is not None and thought.type == "thought.created"

    # 3) the deed was NATURALLY recalled by this wake's own multi-path recall
    #    (grounded, not hand-fed): the anchor and the completed deed are in
    #    the recalled material of the wake that produced the thought.
    recalled_ids = thought.metadata["recalled"]
    assert anchor.id in recalled_ids
    assert completed.id in recalled_ids

    # 4) the provider's reflection output cited the deed by id — the
    #    subjective side references the objective deed (ADR-0020's hard line)
    assert provider.last_output is not None
    provider_cites = provider.last_output["links"]["related_to"]
    assert completed.id in provider_cites
    assert anchor.id in provider_cites

    # 5) the persisted thought carries the cite, and the store ACCEPTED the
    #    append — the no-dangling-link invariant resolved the cited ids to
    #    real, existing deed events (the captured ones, exactly).
    thought_cites = thought.links["related_to"]
    assert completed.id in thought_cites
    assert anchor.id in thought_cites
    assert store.get(completed.id) is not None
    assert store.get(anchor.id) is not None

    # 6) seq ordering: the subjective thought is strictly DOWNSTREAM of the
    #    objective deed — "higher seq, never a rewrite" (ADR-0020)
    assert thought.seq > completed_seq
    assert thought.seq > anchor.seq

    # 7) the objective record is unchanged — the thought cited the deed, it
    #    did not rewrite it (append-only)
    for eid, snap in snapshot.items():
        assert store.get(eid).model_dump() == snap


# --------------------------------------------------------------- action.started
# ADR-0020: an allowed deed records ``action.started`` (non-terminal) between
# intent.proposed and the terminal event, so a crash mid-run leaves a
# recoverable open deed. Objective facts only.


def test_service_allowed_writes_started_between_intent_and_completed(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="分析文件", capability="read_only", source="user")
    )
    assert out["status"] == "completed"
    assert "started_event_id" in out

    intent = store.list(10, type_prefix=INTENT_PROPOSED)[0]
    started = store.get(out["started_event_id"])
    assert started is not None
    assert started.type == ACTION_STARTED
    assert started.visibility == "private"
    assert started.links["caused_by"] == [intent.id]
    assert started.content["capability"] == "read_only"
    assert started.content["tool_surface"] == ["Read", "Glob", "Grep"]
    assert started.content["sandbox"] == f"/sandbox/{out['action_id']}"
    assert started.content["policy"] == agency.policy.version
    assert started.provenance["action_id"] == out["action_id"]
    assert started.provenance["policy"] == agency.policy.version
    # the causal chain stays resolvable: intent -> completed -> (started cites intent)
    completed = store.get(out["event_id"])
    assert completed is not None
    assert completed.links["caused_by"] == [intent.id]
    assert store.get(intent.id) is not None


def test_service_denied_writes_no_started_event(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="删掉日志", capability="destructive", source="user")
    )
    assert out["status"] == "rejected"
    assert store.count(ACTION_STARTED) == 0
    assert "started_event_id" not in out


def test_service_failed_carries_started_event_id(tmp_path):
    from resident.agency.runner import RunResult

    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(
        RunResult(ok=False, summary="boom", exit_code=1, duration_ms=3,
                  reason="error", detail="boom")
    )
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="x", capability="read_only", source="user")
    )
    assert out["status"] == "failed"
    started = store.get(out["started_event_id"])
    assert started is not None
    assert started.type == ACTION_STARTED
    intent = store.list(10, type_prefix=INTENT_PROPOSED)[0]
    assert started.links["caused_by"] == [intent.id]
    # the failed terminal still cites the intent (unchanged)
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.links["caused_by"] == [intent.id]


# ------------------------------------------------------------------- recovery
# ADR-0020 minimal-terminal-state property across a process restart: an open
# deed (intent.proposed, perhaps with action.started, but no terminal child) is
# closed at startup with action.failed(reason="process_restart") + the memory
# anchor. recover_open_deeds() is idempotent.


def _append_deed(store, *, text="分析工作区", capability="read_only", source="user",
                 started: bool, terminal: str | None = None, anchor: bool = True):
    """Append an intent (and optionally its started child / a terminal child)
    by hand — simulating what a crash mid-action leaves behind in the store.
    ``anchor`` controls the terminal's ``experience.created`` memory anchor,
    which the live path always writes (a rejected deed has no anchor by
    design); set ``anchor=False`` to simulate a crash between the terminal
    append and its anchor."""
    intent = store.append(EventCreate(
        type=INTENT_PROPOSED,
        actor="user" if source == "user" else "resident",
        visibility="private",
        content={"text": text, "capability": capability, "source": source},
        provenance={"source": "agency", "action_id": "act_manual"},
    ))
    started_evt = None
    if started:
        started_evt = store.append(EventCreate(
            type=ACTION_STARTED, visibility="private",
            content={"capability": capability, "tool_surface": ["Read"],
                     "sandbox": "/sb/act_manual", "policy": "agency-policy-v1"},
            links={"caused_by": [intent.id]},
            provenance={"source": "agency", "action_id": "act_manual",
                        "policy": "agency-policy-v1"},
        ))
    if terminal == "completed":
        completed = store.append(EventCreate(
            type=ACTION_COMPLETED, visibility="shareable",
            content={"summary": "完成了", "capability": capability},
            links={"caused_by": [intent.id]},
        ))
        if anchor:
            store.append(EventCreate(
                type="experience.created", visibility="private",
                content={"summary": f"行动：{text[:120]}"},
                links={"caused_by": [completed.id]},
                provenance={"source": "agency", "action_id": "act_manual"},
            ))
    elif terminal == "failed":
        # a live-path failure cites the started child (the recovery shape)
        failed = store.append(EventCreate(
            type=ACTION_FAILED, visibility="private",
            content={"reason": "timeout", "capability": capability},
            links={"caused_by": [started_evt.id if started_evt else intent.id]},
        ))
        if anchor:
            store.append(EventCreate(
                type="experience.created", visibility="private",
                content={"summary": f"行动（未完成，timeout）：{text[:120]}"},
                links={"caused_by": [failed.id]},
                provenance={"source": "agency", "action_id": "act_manual"},
            ))
    elif terminal == "rejected":
        store.append(EventCreate(
            type=INTENT_REJECTED, visibility="private",
            content={"reason": "capability_not_granted:x", "capability": capability},
            links={"caused_by": [intent.id]},
        ))
    return intent, started_evt


def test_recover_closes_deed_open_after_started(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    intent, started_evt = _append_deed(store, started=True)  # crash after started
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    recovered = agency.recover_open_deeds()
    assert len(recovered) == 1
    r = recovered[0]
    assert r["intent_id"] == intent.id
    assert r["action_id"] == "act_manual"
    assert r["reason"] == "process_restart"
    failed = store.get(r["event_id"])
    assert failed is not None
    assert failed.type == ACTION_FAILED
    assert failed.content["reason"] == "process_restart"
    assert failed.content["capability"] == "read_only"
    assert "restarted" in failed.content["detail"]
    assert failed.content["sandbox"] == "/sb/act_manual"
    assert failed.links["caused_by"] == [started_evt.id]  # resolves
    exp = store.get(r["experience_id"])
    assert exp is not None
    assert exp.type == "experience.created"
    assert exp.content["summary"].startswith("行动（未完成，process_restart）：")
    assert exp.links["caused_by"] == [failed.id]


def test_recover_closes_deed_open_before_started(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    intent, _ = _append_deed(store, started=False)  # crash before started
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    recovered = agency.recover_open_deeds()
    assert len(recovered) == 1
    failed = store.get(recovered[0]["event_id"])
    assert failed is not None
    assert failed.links["caused_by"] == [intent.id]
    assert "sandbox" not in failed.content  # no started child -> no sandbox fact


def test_recover_leaves_closed_deeds_alone(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    _append_deed(store, started=True, terminal="completed")   # terminal cites intent
    _append_deed(store, started=True, terminal="failed")     # terminal cites started
    _append_deed(store, started=False, terminal="rejected")  # the denial terminal
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    assert agency.recover_open_deeds() == []
    assert store.count(ACTION_FAILED) == 1  # only the one appended above


def test_recover_is_idempotent(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    _append_deed(store, started=True)
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    assert len(agency.recover_open_deeds()) == 1
    count_after_first = store.count()
    assert agency.recover_open_deeds() == []
    assert store.count() == count_after_first  # the second run appends nothing


def test_app_startup_recovers_open_deed(tmp_path, monkeypatch):
    from resident.main import build_app

    fake = _write_fake(tmp_path / "bin", "fake_pyclaw.sh", _OK_BODY)
    monkeypatch.setenv("RESIDENT_AGENCY", "1")
    monkeypatch.setenv("RESIDENT_PYCLAW_BIN", fake)
    # seed an open deed (crash after started) into the store the app will open
    seed = EventStore(tmp_path / "events.sqlite3")
    _intent, started_evt = _append_deed(seed, started=True)
    with TestClient(build_app(tmp_path)):  # the lifespan runs startup recovery
        pass
    store = EventStore(tmp_path / "events.sqlite3")
    failed = store.list(10, type_prefix=ACTION_FAILED)
    assert len(failed) == 1
    assert failed[0].content["reason"] == "process_restart"
    assert failed[0].links["caused_by"] == [started_evt.id]
    assert store.count("experience.created") == 1


def test_app_startup_no_recovery_when_agency_off(tmp_path):
    from resident.main import build_app

    seed = EventStore(tmp_path / "events.sqlite3")
    _append_deed(seed, started=True)
    with TestClient(build_app(tmp_path)) as client:  # flag off: default profile
        assert client.get("/api/agency/health").json()["enabled"] is False
    store = EventStore(tmp_path / "events.sqlite3")
    assert store.count(ACTION_FAILED) == 0      # recovery never ran
    assert store.count(INTENT_PROPOSED) == 1    # the open deed is untouched


# ------------------------------------------------------ idempotency / dedup
# An identical deed is never re-executed: an explicit idempotency_key finds the
# deed it names (any terminal, including a rejection); the natural identity
# (normalised text + capability) matches deeds within dedup_window_h — except a
# rejected deed (a denial is not an outcome that fulfils a new proposal).


def test_explicit_idempotency_key_returns_existing_deed(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    first = asyncio.run(agency.intake(
        text="分析文件", capability="read_only", source="user", idempotency_key="k1"))
    assert first["status"] == "completed"
    assert first.get("deduplicated") is not True
    # the key is recorded on the intent (stable lookup)
    intent = store.list(1, type_prefix=INTENT_PROPOSED)[0]
    assert intent.provenance["idempotency_key"] == "k1"
    second = asyncio.run(agency.intake(
        text="分析文件", capability="read_only", source="user", idempotency_key="k1"))
    assert second["status"] == "completed"
    assert second["deduplicated"] is True
    assert second["intent_id"] == first["intent_id"]
    assert second["event_id"] == first["event_id"]
    assert second["action_id"] == first["action_id"]
    assert store.count(INTENT_PROPOSED) == 1  # no new intent was appended
    assert len(stub.calls) == 1               # the executor ran exactly once


def test_explicit_key_on_open_deed_reports_in_flight(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    # a deed still in flight: keyed intent + started, no terminal yet
    intent = store.append(EventCreate(
        type=INTENT_PROPOSED, visibility="private",
        content={"text": "分析文件", "capability": "read_only", "source": "user"},
        provenance={"source": "agency", "action_id": "act_open",
                    "idempotency_key": "k2"},
    ))
    store.append(EventCreate(
        type=ACTION_STARTED, visibility="private",
        content={"capability": "read_only", "tool_surface": ["Read"],
                 "sandbox": "/sb", "policy": "agency-policy-v1"},
        links={"caused_by": [intent.id]},
        provenance={"source": "agency", "action_id": "act_open",
                    "policy": "agency-policy-v1"},
    ))
    out = asyncio.run(agency.intake(
        text="分析文件", capability="read_only", source="user", idempotency_key="k2"))
    assert out["status"] == "in_flight"
    assert out["in_flight"] is True
    assert out["deduplicated"] is True
    assert out["intent_id"] == intent.id
    assert out["action_id"] == "act_open"
    assert "event_id" not in out  # no terminal yet
    assert store.count(INTENT_PROPOSED) == 1
    assert stub.calls == []       # nothing was re-executed


def test_explicit_key_returns_rejected_outcome(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    denied = asyncio.run(agency.intake(
        text="删掉日志", capability="destructive", source="user", idempotency_key="k3"))
    assert denied["status"] == "rejected"
    again = asyncio.run(agency.intake(
        text="删掉日志", capability="destructive", source="user", idempotency_key="k3"))
    assert again["status"] == "rejected"
    assert again["deduplicated"] is True
    assert again["intent_id"] == denied["intent_id"]
    assert again["event_id"] == denied["rejected_event_id"]
    assert store.count(INTENT_PROPOSED) == 1
    assert store.count(INTENT_REJECTED) == 1


def test_natural_dedup_second_identical_intent_is_not_rerun(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(store, stub, policy=ActionPolicy())
    first = asyncio.run(agency.intake(
        text="列出工作区里的文件并分析", capability="read_only", source="mind"))
    assert first["status"] == "completed"
    # the Mind re-proposes the same opportunity (whitespace-normalised identity)
    second = asyncio.run(agency.intake(
        text="  列出工作区里的文件并分析 ", capability="read_only", source="mind"))
    assert second["status"] == "completed"
    assert second["deduplicated"] is True
    assert second["intent_id"] == first["intent_id"]
    assert store.count(INTENT_PROPOSED) == 1
    assert len(stub.calls) == 1


def test_natural_dedup_does_not_cross_capabilities(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    agency = AgencyService(
        store, stub, policy=ActionPolicy(allowed_capabilities={"read_only", "network"}))
    first = asyncio.run(agency.intake(text="检查站点", capability="read_only", source="user"))
    second = asyncio.run(agency.intake(text="检查站点", capability="network", source="user"))
    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second.get("deduplicated") is not True
    assert store.count(INTENT_PROPOSED) == 2
    assert len(stub.calls) == 2


def test_rejected_deed_is_not_a_natural_dedup_outcome(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    strict = AgencyService(store, stub, policy=ActionPolicy())  # read_only only
    denied = asyncio.run(strict.intake(
        text="抓取站点", capability="network", source="user"))
    assert denied["status"] == "rejected"
    # the owner then grants the capability: the identical intent must be gated
    # FRESH — a rejection is not an outcome that fulfils a new proposal
    lenient = AgencyService(
        store, stub, policy=ActionPolicy(allowed_capabilities={"read_only", "network"}))
    out = asyncio.run(lenient.intake(
        text="抓取站点", capability="network", source="user"))
    assert out["status"] == "completed"
    assert out.get("deduplicated") is not True
    assert store.count(INTENT_PROPOSED) == 2
    assert len(stub.calls) == 1


def test_natural_dedup_window_expiry_creates_fresh_deed(tmp_path):
    # a deed 25h old is outside the default 24h dedup window
    old = datetime.now(UTC) - timedelta(hours=25)
    store_old = EventStore(tmp_path / "e.sqlite3", now_fn=lambda: old)
    first = asyncio.run(AgencyService(
        store_old, _StubRunner(_ok_stub_result()), policy=ActionPolicy()).intake(
        text="分析文件", capability="read_only", source="user"))
    assert first["status"] == "completed"
    # the same store on the wall clock now: the old deed is out of the window
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _StubRunner(_ok_stub_result())
    second = asyncio.run(AgencyService(store, stub, policy=ActionPolicy()).intake(
        text="分析文件", capability="read_only", source="user"))
    assert second["status"] == "completed"
    assert second.get("deduplicated") is not True
    assert second["intent_id"] != first["intent_id"]
    assert store.count(INTENT_PROPOSED) == 2
    assert len(stub.calls) == 1


def test_dedup_window_h_is_configurable(tmp_path):
    # a deed 30h old: outside the default 24h window, inside a 48h window
    old = datetime.now(UTC) - timedelta(hours=30)
    store_old = EventStore(tmp_path / "e.sqlite3", now_fn=lambda: old)
    first = asyncio.run(AgencyService(
        store_old, _StubRunner(_ok_stub_result()), policy=ActionPolicy()).intake(
        text="分析文件", capability="read_only", source="user"))
    assert first["status"] == "completed"
    store = EventStore(tmp_path / "e.sqlite3")
    agency = AgencyService(
        store, _StubRunner(_ok_stub_result()), policy=ActionPolicy(), dedup_window_h=48)
    second = asyncio.run(agency.intake(
        text="分析文件", capability="read_only", source="user"))
    assert second["deduplicated"] is True
    assert second["intent_id"] == first["intent_id"]
    assert store.count(INTENT_PROPOSED) == 1


# ---------------------------------------------------------- malformed output
# An exit-0 run with no usable result is a FAILURE (reason=malformed_output),
# never a silent empty success — a completed deed must have a result to record.


_MALFORMED_GARBAGE_BODY = 'echo "not json at all"'
_MALFORMED_NO_RESULT_BODY = (
    'echo \'{"type":"result","subtype":"success","duration_ms":5,'
    '"duration_api_ms":5,"is_error":false,"num_turns":1,"stop_reason":"end_turn",'
    '"total_cost_usd":0.0,"usage":{},"modelUsage":{},'
    '"permission_denials":[],"uuid":"u3","session_id":"s3"}\'\n'
    "exit 0"
)


async def test_runner_exit0_without_payload_is_malformed(tmp_path):
    fake = _write_fake(tmp_path, "fake_garbage.sh", _MALFORMED_GARBAGE_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_m1",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "malformed_output"
    assert result.exit_code == 0
    assert "no parseable result message" in result.summary
    assert "not json at all" in (result.detail or "")


async def test_runner_exit0_payload_without_result_is_malformed(tmp_path):
    fake = _write_fake(tmp_path, "fake_nores.sh", _MALFORMED_NO_RESULT_BODY)
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(
        prompt="x", action_id="act_m2",
        allowed_tools=["Read"], timeout_s=30,
    )
    assert result.ok is False
    assert result.reason == "malformed_output"
    assert result.exit_code == 0
    assert "no result text" in result.summary


def test_service_malformed_output_is_failed_deed(tmp_path):
    fake = _write_fake(tmp_path / "bin", "fake_garbage.sh", _MALFORMED_GARBAGE_BODY)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    agency = AgencyService(store, runner, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="分析文件", capability="read_only", source="user")
    )
    assert out["status"] == "failed"
    assert out["reason"] == "malformed_output"
    assert store.count(ACTION_COMPLETED) == 0
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.content["reason"] == "malformed_output"
    # a failure is still an experience the life can recall
    assert store.count("experience.created") == 1
    exp = store.list(10, type_prefix="experience.created")[0]
    assert "malformed_output" in exp.content["summary"]


def test_service_exit0_without_result_text_is_failed_deed(tmp_path):
    fake = _write_fake(tmp_path / "bin", "fake_nores.sh", _MALFORMED_NO_RESULT_BODY)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    agency = AgencyService(store, runner, policy=ActionPolicy())
    out = asyncio.run(
        agency.intake(text="分析文件", capability="read_only", source="user")
    )
    assert out["status"] == "failed"
    assert out["reason"] == "malformed_output"
    assert store.count(ACTION_COMPLETED) == 0
    assert store.count(ACTION_FAILED) == 1


# ------------------------------------------------------------ store-scale hardening
# The deed index (recovery + dedup) scans a bounded window of the store. The
# window is NEWEST-FIRST: an open deed is by definition recent, and a deed's
# descendants (started/terminal/anchor) always have a HIGHER seq than the
# intent — so at any total store size, recent deeds are always covered.
# (The old oldest-first scan dropped the newest deeds once the store grew past
# the limit: recent open deeds went unrecovered, recent terminals were
# misreported in-flight, and in-window deeds whose terminal had aged out of the
# scan would have been re-closed.)


def _filler(store, n: int, prefix: str = "想法") -> None:
    for i in range(n):
        store.append(EventCreate(
            type="thought.created", visibility="private",
            content={"text": f"{prefix} {i}"},
        ))


def test_recover_finds_recent_open_deed_beyond_scan_window(tmp_path):
    """A recent open deed is recovered even when the store exceeds the scan
    limit, and an old completed deed inside the window is still correctly
    indexed as closed (not re-closed, not back-filled)."""
    store = EventStore(tmp_path / "e.sqlite3")
    _filler(store, 15, "旧想法")
    _append_deed(store, text="旧任务", started=True, terminal="completed")
    _filler(store, 3)
    open_intent, open_started = _append_deed(store, started=True)  # crash after started
    total = store.count()
    assert total > 10  # the store exceeds the shrunk window
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    agency.deed_scan_limit = 10  # shrink the newest-first window
    recovered = agency.recover_open_deeds()
    assert len(recovered) == 1
    r = recovered[0]
    assert r["intent_id"] == open_intent.id
    assert r["reason"] == "process_restart"
    failed = store.get(r["event_id"])
    assert failed is not None
    assert failed.type == ACTION_FAILED
    assert failed.links["caused_by"] == [open_started.id]
    # the old completed deed (inside the window) was indexed as closed: no
    # duplicate terminal, no spurious back-filled anchor
    assert store.count(ACTION_COMPLETED) == 1
    assert store.count(ACTION_FAILED) == 1
    assert store.count("experience.created") == 2  # the old anchor + the new one
    # idempotent
    assert agency.recover_open_deeds() == []


def test_explicit_key_completed_deed_resolves_beyond_scan_window(tmp_path):
    """A recent COMPLETED deed resolves to its terminal under explicit-key
    dedup at any total store size — not misreported in_flight forever."""
    store = EventStore(tmp_path / "e.sqlite3")
    _filler(store, 20, "旧")
    first = asyncio.run(AgencyService(
        store, _StubRunner(_ok_stub_result()), policy=ActionPolicy()).intake(
        text="分析文件", capability="read_only", source="user", idempotency_key="k7"))
    assert first["status"] == "completed"
    _filler(store, 5, "新")
    assert store.count() > 12
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    agency.deed_scan_limit = 12
    again = asyncio.run(agency.intake(
        text="分析文件", capability="read_only", source="user", idempotency_key="k7"))
    assert again["status"] == "completed"  # not "in_flight"
    assert again["deduplicated"] is True
    assert again["event_id"] == first["event_id"]
    assert store.count(INTENT_PROPOSED) == 1  # no new intent


def test_natural_dedup_completed_deed_resolves_beyond_scan_window(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    _filler(store, 20, "旧")
    first = asyncio.run(AgencyService(
        store, _StubRunner(_ok_stub_result()), policy=ActionPolicy()).intake(
        text="列出工作区里的文件并分析", capability="read_only", source="mind"))
    assert first["status"] == "completed"
    _filler(store, 5, "新")
    assert store.count() > 12
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    agency.deed_scan_limit = 12
    second = asyncio.run(agency.intake(
        text="列出工作区里的文件并分析", capability="read_only", source="mind"))
    assert second["status"] == "completed"
    assert second["deduplicated"] is True
    assert second["intent_id"] == first["intent_id"]
    assert store.count(INTENT_PROPOSED) == 1


# ------------------------------------------------- honest action.failed summaries
# timeout / sandbox_violation / binary-not-found must carry the honest one-liner
# in RunResult.summary (not only detail): intake records summary on the failed
# deed, so an empty summary would leave an empty action.failed claim.


async def test_runner_failure_paths_carry_nonempty_summary(tmp_path):
    fake = _write_fake(tmp_path, "fake_slow.sh", "sleep 30")
    # timeout
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    result = await runner.run(prompt="x", action_id="act_sum_to",
                              allowed_tools=["Read"], timeout_s=0.3)
    assert result.ok is False
    assert result.reason == "timeout"
    assert result.summary
    assert "budget" in result.summary
    # sandbox_violation
    outside = tmp_path / "real_ws"
    outside.mkdir()
    root_link = tmp_path / "ws_link"
    root_link.symlink_to(outside)
    runner2 = PyClawRunner(pyclaw_bin=fake, sandbox_root=root_link)
    result2 = await runner2.run(prompt="x", action_id="act_sum_sv",
                                allowed_tools=["Read"], timeout_s=5)
    assert result2.reason == "sandbox_violation"
    assert result2.summary
    assert "sandbox_root" in result2.summary
    # binary not found
    runner3 = PyClawRunner(pyclaw_bin=str(tmp_path / "nope.sh"),
                           sandbox_root=tmp_path / "ws")
    result3 = await runner3.run(prompt="x", action_id="act_sum_nf",
                                allowed_tools=["Read"], timeout_s=5)
    assert result3.reason == "error"
    assert result3.summary
    assert "not found" in result3.summary


def test_service_timeout_deed_has_nonempty_summary(tmp_path):
    fake = _write_fake(tmp_path / "bin", "fake_slow.sh", "sleep 30")
    store = EventStore(tmp_path / "e.sqlite3")
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / "ws")
    agency = AgencyService(store, runner, policy=ActionPolicy(timeout_s=0.3))
    out = asyncio.run(agency.intake(text="x", capability="read_only", source="user"))
    assert out["status"] == "failed"
    assert out["reason"] == "timeout"
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.content["reason"] == "timeout"
    assert failed.content["summary"]  # the honest one-liner, not empty
    assert "budget" in failed.content["summary"]
    assert store.count("experience.created") == 1


# ------------------------------------------------- granted-tier prompt correctness
# The prompt's "do not call" list is the COMPLEMENT of the granted surface, so
# a granted tool is never listed as forbidden. The v1 read_only wording is
# preserved byte-identically.


def _forbidden_clause(prompt: str) -> str:
    start = prompt.index("（例如 ") + len("（例如 ")
    end = prompt.index("等）", start)
    return prompt[start:end]


def test_prompt_for_read_only_prompt_unchanged():
    expected = (
        "你运行在一个隔离的沙箱工作目录中，只能使用被授予的工具。"
        "你被授权使用的工具是：Read、Glob、Grep。"
        "请只使用这些工具完成任务，不要调用其它任何工具（例如 Bash、Write、"
        "Edit、网络请求等）——它们不在授权范围内，会被拒绝。\n"
        "请完成下面的任务，并用中文给出简洁的结果说明。\n\n"
        "任务：整理文件"
    )
    assert _prompt_for("整理文件", ("Read", "Glob", "Grep")) == expected
    # the tools=None default keeps the generic granted line + the same list
    default = _prompt_for("整理文件")
    assert "策略授予你的工具" in default
    assert _forbidden_clause(default) == "Bash、Write、Edit、网络请求"


def test_prompt_for_forbids_only_ungranted_tools():
    surfaces = {
        "read_only": ("Read", "Glob", "Grep"),
        "file_write_local": ("Read", "Glob", "Grep", "Write", "Edit"),
        "network": ("Read", "Glob", "Grep", "WebFetch", "WebSearch"),
        "destructive": ("Read", "Glob", "Grep", "Write", "Edit", "Bash"),
    }
    expected_clause = {
        "read_only": "Bash、Write、Edit、网络请求",
        "file_write_local": "Bash、网络请求",
        "network": "Bash、Write、Edit",
        "destructive": "网络请求",
    }
    for cap, tools in surfaces.items():
        prompt = _prompt_for("任务", tools)
        clause = _forbidden_clause(prompt)
        assert clause == expected_clause[cap], cap
        # granted tools never appear in the forbidden list
        tokens = clause.split("、")
        for tool in tools:
            assert tool not in tokens, (cap, tool)
        # and the granted surface is named in the prompt
        assert f"你被授权使用的工具是：{'、'.join(tools)}。" in prompt


# ------------------------------------------------- terminal without memory anchor
# A crash between the terminal append and its experience.created append leaves
# a correctly-terminal deed with no memory anchor (never recalled). Recovery
# back-fills the anchor idempotently.


def test_recover_backfills_missing_memory_anchors(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    # a completed deed whose anchor was lost (the completed terminal cites the intent)
    a_intent, _ = _append_deed(store, text="整理笔记", started=True,
                               terminal="completed", anchor=False)
    # a failed deed whose anchor was lost (the live shape cites the started child)
    b_intent, _ = _append_deed(store, text="抓取数据", started=True,
                               terminal="failed", anchor=False)
    # a completed deed WITH its anchor: must not be duplicated
    _append_deed(store, text="读日志", started=True, terminal="completed", anchor=True)
    agency = AgencyService(store, _StubRunner(_ok_stub_result()), policy=ActionPolicy())
    recovered = agency.recover_open_deeds()
    assert len(recovered) == 2
    assert all(r["backfilled"] is True for r in recovered)
    assert all(r["reason"] == "missing_memory_anchor" for r in recovered)
    by_intent = {r["intent_id"]: r for r in recovered}
    assert set(by_intent) == {a_intent.id, b_intent.id}
    # the back-filled anchors cite the deeds' terminals with honest summaries
    a_completed = next(e for e in store.list(50, type_prefix=ACTION_COMPLETED)
                       if e.links["caused_by"] == [a_intent.id])
    a_anchor = store.get(by_intent[a_intent.id]["experience_id"])
    assert a_anchor is not None
    assert a_anchor.type == "experience.created"
    assert a_anchor.links["caused_by"] == [a_completed.id]
    assert a_anchor.content["summary"].startswith("行动：")
    assert "整理笔记" in a_anchor.content["summary"]
    b_failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    b_anchor = store.get(by_intent[b_intent.id]["experience_id"])
    assert b_anchor is not None
    assert b_anchor.links["caused_by"] == [b_failed.id]
    assert "抓取数据" in b_anchor.content["summary"]
    assert "未完成" in b_anchor.content["summary"]
    assert store.count("experience.created") == 3  # a + b back-filled, c untouched
    # idempotent: a second run appends nothing
    total_after = store.count()
    assert agency.recover_open_deeds() == []
    assert store.count() == total_after


# ------------------------------------------- post-started exception containment
# A post-``action.started`` exception (other than the runner's own
# FileNotFoundError) must not escape out of intake with the deed left open:
# the deed is already in flight, so it lands as action.failed + memory anchor.


class _ExplodingRunner(_StubRunner):
    """Dies after the deed has started (EMFILE/EACCES/...) — a post-start
    exception the old code let propagate with the deed still open."""

    async def run(self, *, prompt, action_id, allowed_tools, timeout_s):
        self.calls.append({
            "prompt": prompt, "action_id": action_id,
            "allowed_tools": list(allowed_tools), "timeout_s": timeout_s,
        })
        raise OSError(24, "EMFILE: too many open files")


def test_service_post_start_error_lands_as_failed_deed(tmp_path):
    store = EventStore(tmp_path / "e.sqlite3")
    stub = _ExplodingRunner(None)
    agency = AgencyService(store, stub, policy=ActionPolicy())
    out = asyncio.run(agency.intake(text="分析文件", capability="read_only", source="user"))
    assert out["status"] == "failed"
    assert out["reason"] == "error"
    assert store.count(ACTION_STARTED) == 1
    assert store.count(ACTION_FAILED) == 1
    assert store.count(ACTION_COMPLETED) == 0
    assert store.count("experience.created") == 1  # a failure is still an experience
    failed = store.list(10, type_prefix=ACTION_FAILED)[0]
    assert failed.content["reason"] == "error"
    assert failed.content["summary"]  # the honest one-liner, not empty
    assert "EMFILE" in failed.content["summary"]
    assert failed.links["caused_by"] == [out["intent_id"]]
    # the closed loop holds for dedup too: the deed is terminal, NOT in-flight
    again = asyncio.run(agency.intake(text="分析文件", capability="read_only", source="user"))
    assert again["status"] == "failed"
    assert again["deduplicated"] is True
    assert again["intent_id"] == out["intent_id"]
    assert len(stub.calls) == 1  # the executor was never re-run


# -------------------------------------------------------------------------- ADR-0024
# The ask → authorization channel. The fake py-claw speaks stream-json: it reads
# the host's user message, emits a ``can_use_tool`` control request (with a known
# request_id), reads the host's control_response, and emits the appropriate result
# (success if allow, error if deny/timeout). New events: action.permission_asked
# + action.permission_answered; new failure reason: permission_timeout.


from resident.agency.service import (  # noqa: E402
    ACTION_PERMISSION_ANSWERED,
    ACTION_PERMISSION_ASKED,
)


# A Python fake py-claw that speaks the stream-json ask protocol. It:
#   1. reads the host's user message (first stdin line);
#   2. emits a can_use_tool control_request (request_id="req-1");
#   3. reads the host's control_response (second stdin line);
#   4. parses behavior: allow → success result; deny/timeout → error result.
# The ``ASK_TOOL`` / ``ASK_ARG`` env vars let the test configure what the fake
# "model" asks for. The fake writes the request_id it emitted to a marker file
# so the test can assert the host echoed it back (wire-protocol conformance).
_FAKE_ASK_PY = r'''
import json, os, sys

tool = os.environ.get("ASK_TOOL", "Bash")
arg = os.environ.get("ASK_ARG", "ls -la")
request_id = "req-1"

def read_line():
    line = sys.stdin.readline()
    return line.strip() if line else ""

# 1. read the host's user message
user_line = read_line()

# 2. emit the can_use_tool control_request
print(json.dumps({
    "type": "control_request",
    "request_id": request_id,
    "request": {
        "subtype": "can_use_tool",
        "tool_name": tool,
        "input": {"command": arg} if tool == "Bash" else {"file_path": arg},
        "tool_use_id": "tu-1",
    },
}), flush=True)

# 3. read the host's control_response
resp_line = read_line()
# mark the request_id the fake emitted (the test asserts the host echoed it)
with open(os.environ["REQ_MARKER"], "w") as f:
    f.write(request_id)
try:
    resp = json.loads(resp_line)
except Exception:
    resp = {}

behavior = None
msg = ""
if resp:
    r = resp.get("response", {})
    if r.get("subtype") == "success":
        inner = r.get("response", {})
        behavior = inner.get("behavior")
        msg = inner.get("message", "")

# 4. emit the result
if behavior == "allow":
    print(json.dumps({
        "type": "result", "subtype": "success", "duration_ms": 5,
        "duration_api_ms": 5, "is_error": False, "num_turns": 1,
        "result": "任务完成", "stop_reason": "end_turn", "total_cost_usd": 0.0,
        "usage": {}, "modelUsage": {}, "permission_denials": [],
        "uuid": "u-allow", "session_id": "s-allow",
    }), flush=True)
else:
    reason = msg or f"{tool} requires permission"
    print(json.dumps({
        "type": "result", "subtype": "error_during_execution", "duration_ms": 5,
        "duration_api_ms": 5, "is_error": True, "num_turns": 1,
        "stop_reason": "error", "total_cost_usd": 0.0, "usage": {},
        "modelUsage": {}, "permission_denials": [{"tool_name": tool, "tool_use_id": "tu-1", "tool_input": {}}],
        "errors": [reason], "uuid": "u-deny", "session_id": "s-deny",
    }), flush=True)
    sys.exit(1)
'''


def _make_ask_fake(tmp_path: Path, name: str, marker: Path) -> str:
    """A stream-json fake py-claw that emits a can_use_tool ask."""
    script = tmp_path / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\n" + _FAKE_ASK_PY, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def _runner_with_ask(fake: str, tmp_path: Path, sandbox_sub: str = "ws") -> PyClawRunner:
    runner = PyClawRunner(pyclaw_bin=fake, sandbox_root=tmp_path / sandbox_sub)
    return runner


def test_ask_allow_round_trip(tmp_path):
    """ask → owner-allow: deed completes, permission_asked +
    permission_answered(answered_by=owner-webui) recorded, RunResult.ok."""
    from resident.agency.service import AgencyService

    marker = tmp_path / "req_marker.txt"
    fake = _make_ask_fake(tmp_path / "bin", "fake_ask_allow.sh", marker)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = _runner_with_ask(fake, tmp_path)

    # shorten the ask timeout so the test is fast (the owner answers quickly)
    runner.ask_timeout_s = 10.0

    agency = AgencyService(store, runner, policy=ActionPolicy())
    # the runner is driven directly (not through intake) so we can control
    # the answer timing; intake would work the same.
    import os
    os.environ["REQ_MARKER"] = str(marker)
    os.environ["ASK_TOOL"] = "Bash"
    os.environ["ASK_ARG"] = "ls -la"

    # the runner's host loop reads the ask and waits for the answer; we
    # answer from a background task after a short delay (the owner "sees" it).
    async def _run_and_answer():
        task = asyncio.create_task(runner.run(
            prompt="任务", action_id="act_ask",
            allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
        ))
        # wait for the ask to appear, then answer allow
        for _ in range(100):
            asks = runner.pending_permission_asks()
            if asks:
                break
            await asyncio.sleep(0.05)
        assert runner.pending_permission_asks(), "the ask never appeared"
        ask = runner.pending_permission_asks()[0]
        assert ask.tool == "Bash"
        assert "ls -la" in ask.argument_digest
        runner.submit_permission_answer(ask.ask_id, "allow")
        return await task

    result = asyncio.run(_run_and_answer())
    assert result.ok is True
    # the host echoed the ACTUAL request_id the fake emitted
    assert marker.read_text(encoding="utf-8") == "req-1"
    assert result.session_id == "s-allow"
    # the deed's objective record: the ask + the answer are in the store
    # (the observer was set by intake; here we drive the runner directly, so
    # the observer is None — the events are written by service.intake). We
    # assert the runner's own facts (ok, session, the echoed request_id).


def test_ask_deny_round_trip(tmp_path):
    """ask → owner-deny: the tool is denied, the deed fails, permission_answered
    (answer=deny, answered_by=owner-webui) is recorded."""
    marker = tmp_path / "req_marker.txt"
    fake = _make_ask_fake(tmp_path / "bin", "fake_ask_deny.sh", marker)
    runner = _runner_with_ask(fake, tmp_path)
    runner.ask_timeout_s = 10.0
    import os
    os.environ["REQ_MARKER"] = str(marker)
    os.environ["ASK_TOOL"] = "Bash"
    os.environ["ASK_ARG"] = "rm -rf /"

    async def _run_and_deny():
        task = asyncio.create_task(runner.run(
            prompt="任务", action_id="act_deny",
            allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
        ))
        for _ in range(100):
            if runner.pending_permission_asks():
                break
            await asyncio.sleep(0.05)
        ask = runner.pending_permission_asks()[0]
        assert "rm -rf" in ask.argument_digest
        runner.submit_permission_answer(ask.ask_id, "deny")
        return await task

    result = asyncio.run(_run_and_deny())
    assert result.ok is False
    assert result.reason == "error"
    assert "denied" in result.summary.lower() or "permission" in result.summary.lower()
    assert "rm -rf" not in result.summary  # the digest is redacted, the raw cmd is not in the summary


def test_ask_timeout_auto_deny(tmp_path):
    """ask → no answer before expiry: auto-deny, the deed closes with
    reason=permission_timeout, auto_deny_expired recorded."""
    marker = tmp_path / "req_marker.txt"
    fake = _make_ask_fake(tmp_path / "bin", "fake_ask_timeout.sh", marker)
    runner = _runner_with_ask(fake, tmp_path)
    # shorten the ask timeout to ~0.3s so the test is fast
    runner.ask_timeout_s = 0.3
    import os
    os.environ["REQ_MARKER"] = str(marker)
    os.environ["ASK_TOOL"] = "Bash"
    os.environ["ASK_ARG"] = "sleep 5"

    result = asyncio.run(runner.run(
        prompt="任务", action_id="act_to",
        allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
    ))
    assert result.ok is False
    # the ask expired → the host auto-denied → the tool was denied → error
    assert result.reason == "error"
    # the deny message mentions the timeout (the host's honest one-liner)
    assert "timed out" in result.summary.lower()


def test_no_ask_unchanged_happy_path(tmp_path):
    """No ask at all (everything pre-allowed): behavior identical to today's
    happy path — the fake emits a success result without any control request."""
    # a fake that just emits a success result (no ask) — the existing _OK_BODY
    fake = _write_fake(tmp_path / "bin", "fake_ok.sh", _OK_BODY)
    runner = _runner_with_ask(fake, tmp_path)
    result = asyncio.run(runner.run(
        prompt="任务", action_id="act_ok",
        allowed_tools=["Read", "Glob", "Grep"], timeout_s=30,
    ))
    assert result.ok is True
    assert result.session_id == "s1"
    assert "文件分析" in result.summary
    # no asks were ever live
    assert runner.pending_permission_asks() == []


def test_intake_records_permission_events(tmp_path):
    """Full intake path with an ask: the deed records action.permission_asked
    + action.permission_answered (the ADR-0024 family extension), and the
    terminal state invariant holds (one completed/failed per started)."""
    from resident.agency.service import AgencyService, ACTION_PERMISSION_ASKED, ACTION_PERMISSION_ANSWERED

    marker = tmp_path / "req_marker.txt"
    fake = _make_ask_fake(tmp_path / "bin", "fake_ask_intake.sh", marker)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = _runner_with_ask(fake, tmp_path)
    runner.ask_timeout_s = 10.0
    import os
    os.environ["REQ_MARKER"] = str(marker)
    os.environ["ASK_TOOL"] = "Bash"
    os.environ["ASK_ARG"] = "ls -la"

    # the policy must grant the capability so the intent is NOT rejected
    agency = AgencyService(store, runner, policy=ActionPolicy(allowed_capabilities={"destructive"}))

    async def _intake_and_answer():
        task = asyncio.create_task(agency.intake(
            text="列出文件", capability="destructive", source="user",
        ))
        # wait for the ask to appear (the policy grants destructive → Bash is
        # in the rule set, so NO ask should fire... but the fake always asks,
        # simulating a tool not in the ruleset).
        for _ in range(200):
            asks = runner.pending_permission_asks()
            if asks:
                break
            await asyncio.sleep(0.05)
        assert runner.pending_permission_asks(), "the ask never appeared"
        ask = runner.pending_permission_asks()[0]
        runner.submit_permission_answer(ask.ask_id, "allow")
        return await task

    out = asyncio.run(_intake_and_answer())
    assert out["status"] == "completed"
    # the ADR-0024 events are in the store
    asked = store.list(10, type_prefix=ACTION_PERMISSION_ASKED)
    answered = store.list(10, type_prefix=ACTION_PERMISSION_ANSWERED)
    assert len(asked) == 1
    assert len(answered) == 1
    assert asked[0].content["tool"] == "Bash"
    assert "ls -la" in asked[0].content["argument_digest"]
    assert asked[0].provenance["ask_id"] == "req-1"  # the actual request_id
    assert answered[0].content["answer"] == "allow"
    assert answered[0].content["answered_by"] == "owner-webui"
    assert answered[0].content["rule_delta"] is None  # v1: always null
    # terminal state invariant: one started, one completed
    assert store.count(ACTION_STARTED) == 1
    assert store.count(ACTION_COMPLETED) == 1


def test_permission_ask_api_routes(tmp_path, monkeypatch):
    """The WebUI API: the agency exposes pending_permission_asks() and
    answer_permission_ask() (the in-process channel the WebUI POSTs to)."""
    from resident.agency.service import AgencyService

    marker = tmp_path / "req_marker.txt"
    fake = _make_ask_fake(tmp_path / "bin", "fake_ask_api.sh", marker)
    store = EventStore(tmp_path / "e.sqlite3")
    runner = _runner_with_ask(fake, tmp_path)
    runner.ask_timeout_s = 10.0
    import os
    os.environ["REQ_MARKER"] = str(marker)
    os.environ["ASK_TOOL"] = "Bash"
    os.environ["ASK_ARG"] = "ls -la"

    agency = AgencyService(store, runner, policy=ActionPolicy(allowed_capabilities={"destructive"}))

    async def _intake_and_answer_via_api():
        task = asyncio.create_task(agency.intake(
            text="列出文件", capability="destructive", source="user",
        ))
        # the WebUI GET /api/agency/permission-asks → agency.pending_permission_asks()
        for _ in range(200):
            pending = agency.pending_permission_asks()
            if pending:
                break
            await asyncio.sleep(0.05)
        assert agency.pending_permission_asks(), "the ask never appeared"
        pending = agency.pending_permission_asks()[0]
        assert pending["tool"] == "Bash"
        assert pending["ask_id"] == "req-1"
        # the WebUI POST /api/agency/permission-asks/answer → agency.answer_permission_ask()
        result = agency.answer_permission_ask(pending["ask_id"], "allow")
        assert result == {"ask_id": "req-1", "answer": "allow"}
        # a stale/unknown ask is rejected
        import pytest
        with pytest.raises(ValueError):
            agency.answer_permission_ask("nonexistent", "allow")
        # a bad answer value is rejected
        with pytest.raises(ValueError):
            agency.answer_permission_ask(pending["ask_id"], "maybe")
        return await task

    out = asyncio.run(_intake_and_answer_via_api())
    assert out["status"] == "completed"
