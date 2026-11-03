"""Operator trust tiers, process-tree kill and resource limits."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from skill_orchestrator_mcp import sandbox_env, server, trust_tiers
from skill_orchestrator_mcp.models import CreateSkillInput, InvokeSkillInput, SkillTestCase

from .conftest import Storage

IS_WINDOWS = sys.platform == "win32"
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

# A grandchild that bumps a counter in a file until it is killed.
HEARTBEAT = (
    "import pathlib, sys, time\n"
    "p = pathlib.Path(sys.argv[1])\n"
    "i = 0\n"
    "while True:\n"
    "    i += 1\n"
    "    p.write_text(str(i))\n"
    "    time.sleep(0.05)\n"
)

START_HEARTBEAT = (
    "import subprocess\n"
    "subprocess.Popen([sys.executable, '-c', heartbeat, hb])\n"
    "deadline = time.time() + 10\n"
    "while not Path(hb).exists() and time.time() < deadline:\n"
    "    time.sleep(0.02)\n"
)


def _read(path: Path) -> str:
    for _ in range(20):
        try:
            text = path.read_text()
        except OSError:
            text = ""
        if text:
            return text
        time.sleep(0.02)
    return ""


async def _assert_stopped(hb: Path) -> None:
    assert hb.exists(), "grandchild never started"
    await asyncio.sleep(0.4)
    first = _read(hb)
    await asyncio.sleep(0.6)
    assert _read(hb) == first, "grandchild is still running after the run ended"


# ----------------------------------------------------------------------------
# Policy parsing
# ----------------------------------------------------------------------------


def test_default_tier_is_standard_with_limits() -> None:
    policy = trust_tiers.current_policy({})
    assert policy.tier == "standard"
    assert policy.limits == trust_tiers.TIER_LIMITS["standard"]
    assert policy.limits.memory_mb and policy.limits.allow_subprocesses
    assert policy.allows_tool("execute_code") and not policy.limits_required


def test_trusted_tier_has_no_limits() -> None:
    policy = trust_tiers.current_policy({"SKILL_ORCHESTRATOR_TRUST_TIER": " Trusted "})
    assert policy.tier == "trusted"
    assert not policy.limits.any


def test_restricted_tier_policy() -> None:
    policy = trust_tiers.current_policy({"SKILL_ORCHESTRATOR_TRUST_TIER": "restricted"})
    assert policy.restricted and policy.limits_required and policy.verified_skills_only
    assert not policy.allow_env_passthrough and not policy.allow_inprocess
    assert not policy.limits.allow_subprocesses
    for tool in trust_tiers.RESTRICTED_TOOLS:
        assert not policy.allows_tool(tool)
    assert policy.allows_tool("invoke_skill") and policy.allows_tool("verify_skill")


def test_unknown_tier_fails_closed() -> None:
    policy = trust_tiers.current_policy({"SKILL_ORCHESTRATOR_TRUST_TIER": "standrad"})
    assert policy.tier == "restricted"
    assert policy.invalid_tier == "standrad"
    assert "'standrad' is not one of" in policy.refusal("execute_code")


def test_limit_overrides_and_invalid_values() -> None:
    policy = trust_tiers.current_policy(
        {
            "SKILL_ORCHESTRATOR_TRUST_TIER": "restricted",
            "SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB": "off",
            "SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB": "12",
            "SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES": "on",
        }
    )
    assert policy.limits == trust_tiers.ResourceLimits(None, 12, True)
    # Overrides change limits only, never the tool policy.
    assert not policy.allows_tool("execute_code")

    bad = trust_tiers.current_policy(
        {"SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB": "-5", "SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES": "x"}
    )
    assert bad.limits == trust_tiers.TIER_LIMITS["standard"]
    assert {n for n, _ in bad.invalid_overrides} == {
        "SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB",
        "SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES",
    }


def test_passthrough_can_be_disabled() -> None:
    env = {"PATH": "/bin", "MY_TOKEN": "x", "SKILL_ORCHESTRATOR_ENV_PASSTHROUGH": "MY_TOKEN"}
    assert "MY_TOKEN" in sandbox_env.build_child_env(env, is_windows=False)
    child = sandbox_env.build_child_env(env, is_windows=False, allow_passthrough=False)
    assert "MY_TOKEN" not in child and child["PATH"] == "/bin"


# ----------------------------------------------------------------------------
# Process-tree kill
# ----------------------------------------------------------------------------


async def test_timeout_kills_grandchildren(storage: Storage, tmp_path: Path) -> None:
    hb = tmp_path / "hb.txt"
    outcome = await server._run_code(
        START_HEARTBEAT + "time.sleep(60)\n",
        context={"hb": str(hb), "heartbeat": HEARTBEAT},
        timeout=3,
    )
    assert outcome.status == server.STATUS_TIMEOUT
    await _assert_stopped(hb)


async def test_background_process_killed_after_normal_exit(
    storage: Storage, tmp_path: Path
) -> None:
    # The grandchild inherits the output pipes; the run must not wait for it.
    hb = tmp_path / "hb.txt"
    started = time.monotonic()
    outcome = await server._run_code(
        START_HEARTBEAT + "print('done')\n__result__ = 1\n",
        context={"hb": str(hb), "heartbeat": HEARTBEAT},
        timeout=30,
    )
    assert outcome.status == server.STATUS_OK, outcome.text
    assert "done" in outcome.text
    assert time.monotonic() - started < 20
    await _assert_stopped(hb)


# ----------------------------------------------------------------------------
# Resource limits
# ----------------------------------------------------------------------------


async def test_memory_limit(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB", "256")
    outcome = await server._run_code("buf = bytearray(600 * 1024 * 1024)\n", timeout=30)
    assert outcome.status == server.STATUS_ERROR
    assert "MemoryError" in (outcome.error or ""), outcome.text

    ok = await server._run_code("buf = bytearray(16 * 1024 * 1024)\n__result__ = len(buf)\n")
    assert ok.status == server.STATUS_OK, ok.text


async def test_trusted_tier_has_no_memory_limit(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "trusted")
    monkeypatch.setenv("SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB", "256")  # override still applies
    limited = await server._run_code("buf = bytearray(600 * 1024 * 1024)\n")
    assert "MemoryError" in (limited.error or "")

    monkeypatch.delenv("SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB")
    free = await server._run_code("buf = bytearray(300 * 1024 * 1024)\n")
    assert free.status == server.STATUS_OK, free.text


@pytest.mark.skipif(IS_WINDOWS, reason="RLIMIT_FSIZE is POSIX only")
async def test_file_size_limit(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB", "1")
    outcome = await server._run_code(
        "Path(target).write_bytes(b'x' * (2 * 1024 * 1024))\n",
        context={"target": str(tmp_path / "big.bin")},
    )
    assert outcome.status == server.STATUS_ERROR
    assert "File too large" in outcome.text or "EFBIG" in outcome.text, outcome.text


SPAWN = (
    "import subprocess\n"
    "__result__ = subprocess.run([sys.executable, '-c', 'print(1)'], "
    "capture_output=True).returncode\n"
)


async def test_subprocesses_blocked(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES", "off")
    outcome = await server._run_code(SPAWN)
    if IS_ROOT:
        # Standard tier: can't be enforced for root, so the run warns.
        assert "Sandbox warning" in outcome.text and "root" in outcome.text
    else:
        assert outcome.status == server.STATUS_ERROR, outcome.text
        assert "before reporting a result" not in outcome.text  # the run itself started

    monkeypatch.delenv("SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES")
    allowed = await server._run_code(SPAWN)
    assert allowed.status == server.STATUS_OK and allowed.result == 0, allowed.text


# ----------------------------------------------------------------------------
# Restricted tier
# ----------------------------------------------------------------------------


@pytest.fixture
def restricted(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "restricted")
    if IS_ROOT:
        # No-subprocess limit can't be enforced for root; acknowledge it.
        monkeypatch.setenv("SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES", "on")
    return monkeypatch


async def _create_skill(name: str, *, verified: bool) -> None:
    tests = [SkillTestCase(parameters={"x": 2}, expected_result=4)] if verified else []
    msg = await server.create_skill(
        CreateSkillInput(
            name=name,
            description="Doubles x for trust-tier tests",
            code="__result__ = params['x'] * 2",
            tests=tests,
        )
    )
    assert "created successfully" in msg, msg


async def test_restricted_refuses_code_changing_tools(
    storage: Storage, restricted: pytest.MonkeyPatch
) -> None:
    out = await server.execute_code(server.ExecuteCodeInput(code="print(1)"))
    assert "execute_code is disabled" in out and "restricted" in out
    out = await server.create_skill(
        CreateSkillInput(name="nope", description="should be refused", code="__result__ = 1")
    )
    assert "create_skill is disabled" in out
    assert not (storage.skills_dir / "nope.json").exists()
    out = await server.delete_skill("anything", confirm=True)
    assert "delete_skill is disabled" in out


async def test_restricted_runs_only_verified_skills(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_skill("checked", verified=True)
    await _create_skill("unchecked", verified=False)
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "restricted")
    if IS_ROOT:
        monkeypatch.setenv("SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES", "on")

    ok = await server.invoke_skill(InvokeSkillInput(skill_name="checked", parameters={"x": 5}))
    assert "Error" not in ok and "10" in ok, ok

    refused = await server.invoke_skill(
        InvokeSkillInput(skill_name="unchecked", parameters={"x": 5})
    )
    assert refused.startswith("Error:") and "only runs verified skills" in refused

    # Nested calls can only reach verified skills.
    registry = server._build_skill_registry(verified_only=True)
    assert set(registry) == {"checked"}


async def test_restricted_ignores_passthrough_and_inprocess(
    storage: Storage, restricted: pytest.MonkeyPatch
) -> None:
    restricted.setenv("MY_SECRET_FOR_TEST", "s3cret")
    restricted.setenv("SKILL_ORCHESTRATOR_ENV_PASSTHROUGH", "MY_SECRET_FOR_TEST")
    restricted.setenv("SKILL_ORCHESTRATOR_EXEC_MODE", "inprocess")
    outcome = await server._run_code(
        "__result__ = [os.environ.get('MY_SECRET_FOR_TEST'), os.getpid()]"
    )
    assert outcome.status == server.STATUS_OK, outcome.text
    secret, pid = outcome.result
    assert secret is None
    assert pid != os.getpid()  # ran in a subprocess, not in the server


@pytest.mark.skipif(IS_WINDOWS or not IS_ROOT, reason="root-only fail-closed path")
async def test_restricted_fails_closed_when_limit_unenforceable(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "restricted")
    outcome = await server._run_code("__result__ = 1")
    assert outcome.status == server.STATUS_ERROR
    assert "SandboxLimitError" in (outcome.error or "")
    assert outcome.result is None


@pytest.mark.skipif(IS_ROOT, reason="RLIMIT_NPROC is not enforced for root")
async def test_restricted_blocks_subprocesses(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "restricted")
    # The run itself starts (on Windows that includes a venv launcher)...
    plain = await server._run_code("__result__ = 1")
    assert plain.status == server.STATUS_OK and plain.result == 1, plain.text
    # ...but the code cannot start another process.
    outcome = await server._run_code(SPAWN)
    assert outcome.status == server.STATUS_ERROR, outcome.text
    assert "before reporting a result" not in outcome.text


async def test_status_reports_trust_tier(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_TRUST_TIER", "bogus")
    status = await server.get_orchestrator_status()
    assert "## Trust Tier" in status
    assert "'bogus' is not one of" in status
    assert "execute_code" in status  # lists refused tools


async def test_cancelled_run_kills_tree(storage: Storage, tmp_path: Path) -> None:
    # A cancelled MCP request must not leave the run's processes behind.
    hb = tmp_path / "hb.txt"
    task = asyncio.ensure_future(
        server._run_code(
            START_HEARTBEAT + "time.sleep(60)\n",
            context={"hb": str(hb), "heartbeat": HEARTBEAT},
            timeout=60,
        )
    )
    for _ in range(200):
        if hb.exists():
            break
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_stopped(hb)
