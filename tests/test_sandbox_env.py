"""The skill / execute_code child gets an allowlisted environment, never secrets."""

from __future__ import annotations

import json
import os

import pytest

from skill_orchestrator_mcp import sandbox_env, server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ExecuteCodeInput,
    InvokeSkillInput,
    ReturnMode,
)

from .conftest import Storage

SECRET = "sk-test-0123456789-do-not-leak"

_DUMP_ENV = "__result__ = {'env': dict(os.environ)}"


def _result(text: str) -> dict:
    """Pull the JSON `__result__` block out of a FULL-mode tool response."""
    block = text.split("**Result:**\n```\n", 1)[1].split("\n```", 1)[0]
    return json.loads(block)


async def _child_env_via_execute_code() -> dict:
    out = await server.execute_code(
        ExecuteCodeInput(code=_DUMP_ENV, return_mode=ReturnMode.FULL)
    )
    return _result(out)["env"]


# ----------------------------------------------------------------------------
# Pure function
# ----------------------------------------------------------------------------


def test_build_child_env_posix_keeps_only_allowlist() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "TMPDIR": "/tmp/x",
        "FAKE_API_KEY": SECRET,
        "ANTHROPIC_API_KEY": SECRET,
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "HTTPS_PROXY": "http://user:pass@proxy:8080",
        "PYTHONPATH": "/evil",
        "SKILL_ORCHESTRATOR_TELEMETRY": "on",
    }

    env = sandbox_env.build_child_env(parent, is_windows=False)

    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "TMPDIR": "/tmp/x",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def test_build_child_env_windows_case_insensitive_and_needed_vars() -> None:
    parent = {
        "Path": r"C:\Windows",
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\t",
        "TMP": r"C:\t",
        "USERPROFILE": r"C:\Users\u",
        "FAKE_API_KEY": SECRET,
    }

    env = sandbox_env.build_child_env(parent, is_windows=True)

    assert env["PATH"] == r"C:\Windows"
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["TEMP"] == env["TMP"] == r"C:\t"
    assert env["USERPROFILE"] == r"C:\Users\u"
    assert SECRET not in env.values()
    assert "HOME" not in env  # POSIX-only name is not copied on Windows


def test_passthrough_copies_only_named_vars() -> None:
    parent = {
        "PATH": "/bin",
        "MY_SERVICE_URL": "https://example.invalid",
        "FAKE_API_KEY": SECRET,
        sandbox_env.PASSTHROUGH_ENV_VAR: " MY_SERVICE_URL , bad-name, ,MISSING_VAR",
    }

    env = sandbox_env.build_child_env(parent, is_windows=False)

    assert env["MY_SERVICE_URL"] == "https://example.invalid"
    assert "FAKE_API_KEY" not in env
    assert "MISSING_VAR" not in env
    assert "bad-name" not in env
    # The passthrough setting itself is not handed to the child.
    assert sandbox_env.PASSTHROUGH_ENV_VAR not in env


def test_fixed_values_override_passthrough() -> None:
    parent = {
        "PYTHONIOENCODING": "latin-1",
        sandbox_env.PASSTHROUGH_ENV_VAR: "PYTHONIOENCODING",
    }

    env = sandbox_env.build_child_env(parent, is_windows=False)

    assert env["PYTHONIOENCODING"] == "utf-8"


def test_parse_passthrough_reports_rejected_entries() -> None:
    names, rejected = sandbox_env.parse_passthrough("A,B_2, A ,1BAD,x=y")
    assert names == ["A", "B_2"]
    assert rejected == ["1BAD", "x=y"]


# ----------------------------------------------------------------------------
# End to end through the real subprocess
# ----------------------------------------------------------------------------


async def test_secret_invisible_to_execute_code(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_API_KEY", SECRET)
    monkeypatch.setenv("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT", "strict")

    env = await _child_env_via_execute_code()

    assert "FAKE_API_KEY" not in env
    assert SECRET not in json.dumps(env)
    assert not any(k.startswith("SKILL_ORCHESTRATOR_") for k in env)
    assert env["PYTHONIOENCODING"] == "utf-8"


async def test_secret_invisible_to_skills_and_nested_skills(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_API_KEY", SECRET)
    await server.create_skill(
        CreateSkillInput(
            name="env_leaf",
            description="Returns whether a secret is visible",
            code="import os\n__result__ = {'seen': os.environ.get('FAKE_API_KEY')}",
        )
    )
    await server.create_skill(
        CreateSkillInput(
            name="env_parent",
            description="Calls env_leaf and reports both views",
            code=(
                "child = invoke_skill('env_leaf')\n"
                "__result__ = {'parent': os.environ.get('FAKE_API_KEY'), 'child': child['seen']}"
            ),
        )
    )

    out = await server.invoke_skill(
        InvokeSkillInput(skill_name="env_parent", return_mode=ReturnMode.FULL)
    )

    assert _result(out) == {"parent": None, "child": None}
    assert SECRET not in out


async def test_secret_not_reachable_via_subprocess_from_skill(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grandchild process inherits the scrubbed env, not the server's."""
    monkeypatch.setenv("FAKE_API_KEY", SECRET)
    code = (
        "import subprocess\n"
        "out = subprocess.run([sys.executable, '-c', "
        "'import os; print(os.environ.get(\"FAKE_API_KEY\"))'], "
        "capture_output=True, text=True, check=True)\n"
        "__result__ = {'grandchild': out.stdout.strip()}"
    )

    out = await server.execute_code(ExecuteCodeInput(code=code, return_mode=ReturnMode.FULL))

    assert _result(out) == {"grandchild": "None"}


async def test_passthrough_reaches_child_only_when_allowlisted(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_API_KEY", SECRET)
    monkeypatch.setenv("SKILL_TEST_ENDPOINT", "https://example.invalid")

    env_before = await _child_env_via_execute_code()
    monkeypatch.setenv(sandbox_env.PASSTHROUGH_ENV_VAR, "SKILL_TEST_ENDPOINT")
    env_after = await _child_env_via_execute_code()

    assert "SKILL_TEST_ENDPOINT" not in env_before
    assert env_after["SKILL_TEST_ENDPOINT"] == "https://example.invalid"
    assert "FAKE_API_KEY" not in env_after


async def test_context_cannot_inject_environment(storage: Storage) -> None:
    """Client-supplied context becomes variables, never environment entries."""
    out = await server.execute_code(
        ExecuteCodeInput(
            code=_DUMP_ENV,
            context={sandbox_env.PASSTHROUGH_ENV_VAR: "PATH", "FAKE_API_KEY": "x"},
            return_mode=ReturnMode.FULL,
        )
    )

    env = _result(out)["env"]
    assert "FAKE_API_KEY" not in env
    assert sandbox_env.PASSTHROUGH_ENV_VAR not in env


async def test_child_still_works_with_scrubbed_env(storage: Storage) -> None:
    """Temp files, home lookup, hashing, sockets and UTF-8 output all work."""
    code = (
        "import tempfile, hashlib, socket, random, uuid\n"
        "with tempfile.NamedTemporaryFile('w', delete=False) as f:\n"
        "    f.write('x')\n"
        "os.unlink(f.name)\n"
        "s = socket.socket(); s.close()\n"
        "print('héllo ✓')\n"
        "__result__ = {'home': str(Path.home()), 'h': hashlib.sha256(b'a').hexdigest()[:8],"
        " 'r': random.random() < 2, 'u': len(str(uuid.uuid4()))}"
    )

    out = await server.execute_code(ExecuteCodeInput(code=code, return_mode=ReturnMode.FULL))

    assert "**Error:**" not in out, out
    assert "héllo ✓" in out
    res = _result(out)
    assert res["h"] == "ca978112" and res["r"] is True and res["u"] == 36
    assert res["home"]


def test_server_env_untouched_by_build() -> None:
    before = dict(os.environ)
    sandbox_env.build_child_env()
    assert dict(os.environ) == before
