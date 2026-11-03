"""Tests for the code execution path (subprocess sandbox) and the
execute_code / invoke_skill tools that wrap it.

All code executed here is test-authored and runs through the existing
subprocess runner in `_execute_code_internal`.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ExecuteCodeInput,
    InvokeSkillInput,
    ReturnMode,
)

from .conftest import Storage


async def _run(code: str, **kwargs: object) -> str:
    return await server._execute_code_internal(code=code, timeout=10, **kwargs)  # type: ignore[arg-type]  # kwargs are forwarded verbatim


# ----------------------------------------------------------------------------
# _execute_code_internal (subprocess path)
# ----------------------------------------------------------------------------


async def test_result_dict_is_rendered_as_json() -> None:
    out = await _run("__result__ = {'answer': 42, 'items': [1, 2]}")
    assert '"answer": 42' in out
    assert "**Result:**" in out


async def test_context_variables_are_injected() -> None:
    out = await _run("__result__ = x * 2", context={"x": 21})
    assert "42" in out


async def test_common_imports_and_helpers_are_available() -> None:
    out = await _run("pretty_print({'k': math.floor(2.7)})\n__result__ = json.dumps([1])")
    assert '"k": 2' in out
    assert "[1]" in out


async def test_exception_is_reported_with_traceback_and_context_hint() -> None:
    out = await _run("raise ValueError('boom')", context={"visible_var": 1})

    assert "**Error:** boom" in out
    assert "ValueError" in out
    assert "visible_var" in out


async def test_stderr_is_captured() -> None:
    out = await _run("import sys\nprint('warned', file=sys.stderr)")
    assert "**Stderr:**" in out and "warned" in out


async def test_no_output_message() -> None:
    assert "no output" in await _run("x = 1")


async def test_capture_output_false_discards_stdout() -> None:
    out = await _run("print('hidden')\n__result__ = 'shown'", capture_output=False)
    assert "hidden" not in out
    assert "shown" in out


async def test_non_json_result_falls_back_to_str() -> None:
    out = await _run("__result__ = {'s': {1, 2}}")
    assert "{1, 2}" in out


async def test_large_stdout_is_truncated() -> None:
    out = await _run(f"print('a' * {server.MAX_OUTPUT_SIZE + 500})")
    assert "[truncated, 501 characters omitted]" in out


async def test_stdin_is_closed_so_input_does_not_hang() -> None:
    out = await _run("input()")
    assert "EOFError" in out


async def test_runs_in_temp_cwd_not_server_cwd() -> None:
    out = await _run("import os\n__result__ = os.path.basename(os.getcwd())")
    assert "skill_orchestrator_exec_" in out


async def test_sys_exit_nonzero_is_reported_as_error() -> None:
    # Bug: a child that exited without writing a result was reported as
    # "Code executed successfully with no output."
    out = await _run("import sys\nsys.exit(3)")

    assert "successfully" not in out
    assert "exit code 3" in out


async def test_hard_process_crash_is_reported_as_error() -> None:
    out = await _run("import os\nos._exit(7)")

    assert "successfully" not in out
    assert "exit code 7" in out


async def test_composition_disabled_for_plain_execution() -> None:
    out = await _run("invoke_skill('anything')")
    assert "Skill composition not available" in out


# ----------------------------------------------------------------------------
# execute_code tool (return modes + artifact storage)
# ----------------------------------------------------------------------------


async def test_execute_code_handle_mode_stores_full_output(storage: Storage) -> None:
    ref = json.loads(await server.execute_code(ExecuteCodeInput(code="print('hi')", return_mode=ReturnMode.HANDLE)))

    chunk = server.ARTIFACT_STORE.get_text_chunk(ref["artifact_id"])
    assert ref["resource"] == f"artifact://{ref['artifact_id']}"
    assert chunk is not None and "hi" in chunk["chunk"]


async def test_execute_code_summary_mode_truncates_preview(storage: Storage) -> None:
    text = await server.execute_code(ExecuteCodeInput(code="print('z' * 5000)"))

    assert text.startswith("## Code Execution (summary)")
    assert "(truncated)" in text
    assert "artifact://" in text


async def test_execute_code_full_mode_and_ttl(storage: Storage) -> None:
    text = await server.execute_code(
        ExecuteCodeInput(code="__result__ = 1", return_mode=ReturnMode.FULL, artifact_ttl_seconds=60)
    )
    artifact_id = text.rsplit("artifact://", 1)[1].rstrip("`")

    meta = server.ARTIFACT_STORE.get_meta(artifact_id)
    assert "**Result:**" in text
    assert meta is not None and meta["expires_at"] is not None


def test_execute_code_input_bounds() -> None:
    with pytest.raises(ValidationError):
        ExecuteCodeInput(code="")
    with pytest.raises(ValidationError):
        ExecuteCodeInput(code="x=1", timeout=181)
    with pytest.raises(ValidationError):
        ExecuteCodeInput(code="x=1", unknown_field=True)  # type: ignore[call-arg]  # extra='forbid' check


# ----------------------------------------------------------------------------
# invoke_skill tool
# ----------------------------------------------------------------------------


async def _create(name: str, code: str) -> None:
    await server.create_skill(
        CreateSkillInput(name=name, description="Skill created by execution tests", code=code)
    )


async def test_invoke_skill_passes_params_and_counts_usage(storage: Storage) -> None:
    await _create("doubler", "__result__ = {'out': params['n'] * 2, 'me': __skill_name__}")

    text = await server.invoke_skill(
        InvokeSkillInput(skill_name="doubler", parameters={"n": 21}, return_mode=ReturnMode.FULL)
    )

    assert '"out": 42' in text
    assert '"me": "doubler"' in text
    assert server.USAGE_TRACKER.get("doubler") == 1


async def test_invoke_skill_missing_returns_error_without_counting(storage: Storage) -> None:
    text = await server.invoke_skill(InvokeSkillInput(skill_name="ghost"))

    assert text.startswith("Error: Skill 'ghost' not found")
    assert server.USAGE_TRACKER.get("ghost") == 0


async def test_invoke_skill_composes_with_stored_child(storage: Storage) -> None:
    await _create("child_skill", "__result__ = params.get('v', 0) + 1")
    await _create("parent_skill", "__result__ = {'child': invoke_skill('child_skill', {'v': 1})}")

    text = await server.invoke_skill(InvokeSkillInput(skill_name="parent_skill", return_mode=ReturnMode.FULL))

    assert '"child": 2' in text


async def test_invoke_skill_reports_runtime_error(storage: Storage) -> None:
    await _create("broken_skill", "raise RuntimeError('skill exploded')")

    text = await server.invoke_skill(InvokeSkillInput(skill_name="broken_skill", return_mode=ReturnMode.FULL))

    assert "skill exploded" in text
    assert server.USAGE_TRACKER.get("broken_skill") == 1


async def test_invoke_skill_timeout(storage: Storage) -> None:
    await _create("sleepy_skill", "import time\ntime.sleep(5)")

    text = await server.invoke_skill(
        InvokeSkillInput(skill_name="sleepy_skill", timeout=1, return_mode=ReturnMode.FULL)
    )

    assert "timed out after 1s" in text


async def test_invoke_skill_rejects_traversal_name(storage: Storage) -> None:
    (storage.state_dir / "fake.json").write_text(
        json.dumps({"id": "x", "name": "fake", "code": "__result__ = 'escaped'"})
    )

    text = await server.invoke_skill(InvokeSkillInput(skill_name="../state/fake"))

    assert text.startswith("Error")
    assert "escaped" not in text


# ----------------------------------------------------------------------------
# Legacy in-process mode (opt-in via env var)
# ----------------------------------------------------------------------------


async def test_inprocess_mode_basic_result_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_EXEC_MODE", "inprocess")

    ok = await _run("print('p')\n__result__ = {'v': 1}")
    err = await _run("raise KeyError('missing')")

    assert '"v": 1' in ok and "p" in ok
    assert "**Error:**" in err and "missing" in err
