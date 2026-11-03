"""Regression tests for bugs found while reviewing the telemetry/versioning/contracts change.

Each test names the failure it guards against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp import skill_contracts as contracts
from skill_orchestrator_mcp.models import CreateSkillInput, InvokeSkillInput, ReturnMode
from skill_orchestrator_mcp.telemetry import ExecutionTelemetry

from .conftest import Storage

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _create(name: str, code: str, **kw: Any) -> str:
    msg = await server.create_skill(CreateSkillInput(name=name, description="regression test", code=code, **kw))
    assert "created successfully" in msg, msg
    return msg


async def _invoke(name: str, params: dict[str, Any] | None = None, **kw: Any) -> str:
    return await server.invoke_skill(
        InvokeSkillInput(skill_name=name, parameters=params, return_mode=ReturnMode.FULL, **kw)
    )


def _state(storage: Storage, ns: str = "default") -> dict[str, Any]:
    path = storage.state_dir / f"{ns}.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _write_skill_file(storage: Storage, skill_name: str, **fields: Any) -> None:
    data = {"id": "x", "name": skill_name, "description": "hand-written skill file", "code": "__result__ = 'ran'"}
    data.update(fields)
    (storage.skills_dir / f"{skill_name}.json").write_text(json.dumps(data))
    server.SKILL_INDEX.rebuild()


# ----------------------------------------------------------------------------
# Execution core
# ----------------------------------------------------------------------------


async def test_exception_without_message_is_an_error(storage: Storage) -> None:
    # A bare `assert` has an empty str(e); it used to be recorded as success.
    await _create("bare_assert", "set_state('k', 1)\nassert 1 == 2")

    out = await _invoke("bare_assert")

    assert "**Error:** AssertionError" in out
    assert "1 pending write(s) discarded" in out
    (rec,) = server.TELEMETRY.records(skill_name="bare_assert")
    assert rec["status"] == "error" and rec["error"].startswith("AssertionError")
    assert _state(storage) == {}


async def test_sys_exit_zero_completes_and_commits(storage: Storage) -> None:
    await _create("exits_cleanly", "set_state('k', 1)\n__result__ = 'bye'\nimport sys\nsys.exit(0)")

    out = await _invoke("exits_cleanly")

    assert "bye" in out and "committed 1 write(s)" in out
    assert _state(storage) == {"k": 1}
    (rec,) = server.TELEMETRY.records(skill_name="exits_cleanly")
    assert rec["status"] == "ok"


async def test_sys_exit_nonzero_discards_writes(storage: Storage) -> None:
    await _create("exits_badly", "set_state('k', 1)\nimport sys\nsys.exit(4)")

    out = await _invoke("exits_badly")

    assert "exit code 4" in out and "discarded" in out
    assert _state(storage) == {}


async def test_hard_exit_zero_is_not_success(storage: Storage) -> None:
    await _create("vanishes", "import os\nos._exit(0)")

    out = await _invoke("vanishes")

    assert "exited without reporting a result" in out
    (rec,) = server.TELEMETRY.records(skill_name="vanishes")
    assert rec["status"] == "error"


async def test_unserializable_result_keeps_state_and_shows_repr(storage: Storage) -> None:
    await _create("tuple_keys", "set_state('k', 1)\n__result__ = {(1, 2): 'x'}")

    out = await _invoke("tuple_keys")

    assert "{(1, 2): 'x'}" in out and "not JSON-serializable" in out
    assert _state(storage) == {"k": 1}


async def test_nested_non_dict_params_pass_through(storage: Storage) -> None:
    await _create("takes_list", "__result__ = len(params)")
    await _create("passes_list", "__result__ = invoke_skill('takes_list', [1, 2, 3])")

    assert "**Result:**\n```\n3\n```" in await _invoke("passes_list")


async def test_nested_params_are_the_callers_object(storage: Storage) -> None:
    await _create("fills", "params['out'] = 42")
    await _create("reads_back", "p = {'seed': 1}\ninvoke_skill('fills', p)\n__result__ = p.get('out')")

    assert "42" in await _invoke("reads_back")


async def test_parent_helper_override_does_not_hijack_child(storage: Storage) -> None:
    await _create("real_writer", "set_state('from_child', True)")
    await _create(
        "shadowing_parent",
        "_mem = {}\ndef set_state(k, v, namespace='default'):\n    _mem[k] = v\n"
        "invoke_skill('real_writer')\n__result__ = sorted(_mem)",
    )

    await _invoke("shadowing_parent")

    assert _state(storage) == {"from_child": True}


async def test_nan_state_value_rejected(storage: Storage) -> None:
    await _create("nan_writer", "set_state('k', float('nan'))")

    out = await _invoke("nan_writer")

    assert "not JSON-serializable" in out
    assert _state(storage) == {}


async def test_partial_commit_reports_what_was_written(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = server._replace_file
    calls = {"n": 0}

    def flaky_replace(tmp: Path, path: Path) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            tmp.unlink(missing_ok=True)
            raise PermissionError("locked by another process")
        real_replace(tmp, path)

    await _create("two_namespaces", "set_state('a', 1, 'ns1')\nset_state('b', 2, 'ns2')")
    monkeypatch.setattr(server, "_replace_file", flaky_replace)

    out = await _invoke("two_namespaces")

    assert "partially failed: wrote 'ns1'; not written: 'ns2'" in out
    assert not list(storage.state_dir.glob("*.tmp"))


async def test_bad_op_writes_nothing(storage: Storage) -> None:
    with pytest.raises(server.StateCommitError, match="no state was written"):
        server._commit_state_ops(
            [
                {"op": "set", "namespace": "ok", "key": "a", "value": 1},
                {"op": "set", "namespace": "../bad", "key": "a", "value": 1},
            ]
        )
    assert not (storage.state_dir / "ok.json").exists()


def _run_workers(script: str, home: Path, n: int = 2) -> list[str]:
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "PYTHONPATH": str(REPO_ROOT)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(script), str(i)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for i in range(n)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
        outs.append(out)
    return outs


def test_state_commits_from_separate_processes_are_not_lost(tmp_path: Path) -> None:
    script = """
        import sys
        from skill_orchestrator_mcp import server
        w = sys.argv[1]
        for i in range(150):
            server._commit_state_ops([{"op": "set", "namespace": "shared", "key": f"w{w}_{i}", "value": i}])
    """
    _run_workers(script, tmp_path)

    data = json.loads((tmp_path / ".skill_orchestrator" / "state" / "shared.json").read_text())
    assert len(data) == 300


def test_expected_version_holds_across_processes(tmp_path: Path) -> None:
    # Both processes race to move v{N} -> v{N+1}; exactly one may win each round.
    setup = """
        import asyncio
        from skill_orchestrator_mcp import server
        from skill_orchestrator_mcp.models import CreateSkillInput
        asyncio.run(server.create_skill(CreateSkillInput(
            name="contended", description="cross-process test", code="__result__ = 0")))
    """
    _run_workers(setup, tmp_path, n=1)
    script = """
        import asyncio, sys
        from skill_orchestrator_mcp import server
        w = sys.argv[1]
        async def main():
            wins = 0
            for rnd in range(1, 16):
                msg = await server.update_skill(
                    "contended", code=f"__result__ = '{w}-{rnd}'", expected_version=rnd)
                wins += msg.startswith("✅")
                # Wait until someone has produced v{rnd+1} before the next round.
                while server._skill_version(server._load_skill("contended") or {}) < rnd + 1:
                    await asyncio.sleep(0.001)
            print(wins)
        asyncio.run(main())
    """
    outs = _run_workers(script, tmp_path)

    skill = json.loads((tmp_path / ".skill_orchestrator" / "skills" / "contended.json").read_text())
    assert skill["version"] == 16
    assert sum(int(o.strip()) for o in outs) == 15


# ----------------------------------------------------------------------------
# Contracts
# ----------------------------------------------------------------------------


async def test_malformed_dependencies_in_one_file_do_not_break_others(storage: Storage) -> None:
    _write_skill_file(storage, "broken_deps", dependencies=[None, 1])
    await _create("healthy", "__result__ = 'still works'")

    assert "still works" in await _invoke("healthy")
    out = await _invoke("broken_deps")
    assert "ran" in out and "Dependency ignored" in out


async def test_string_dependencies_are_one_requirement(storage: Storage) -> None:
    _write_skill_file(storage, "str_deps", dependencies="json")
    assert "ran" in await _invoke("str_deps")


async def test_legacy_free_form_dependencies_warn_instead_of_refusing(storage: Storage) -> None:
    _write_skill_file(storage, "free_form", dependencies=["json (stdlib)", "python>=3.0"])

    out = await _invoke("free_form")

    assert "ran" in out
    assert "Dependency ignored: json (stdlib)" in out


def test_python_requirement_is_checked_against_the_interpreter() -> None:
    assert contracts.missing_dependencies(["python>=3.0"]) == []
    assert contracts.missing_dependencies(["python<3"])[0].startswith("python<3 (running Python")


@pytest.mark.parametrize("mode,expect", [("warn", "Unmet dependency"), ("off", "ran")])
async def test_dependency_enforcement_modes(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, mode: str, expect: str
) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT", mode)
    _write_skill_file(storage, "needy", dependencies=["surely-not-installed-pkg-xyz"])

    out = await _invoke("needy")

    assert "ran" in out and expect in out


def test_dependency_check_never_imports_packages(capsys: pytest.CaptureFixture[str]) -> None:
    sys.modules.pop("this", None)
    contracts._dep_cache.clear()

    contracts.missing_dependencies(["this.zen"])

    assert "this" not in sys.modules
    assert capsys.readouterr().out == ""


async def test_unresolvable_ref_rejected_at_create(storage: Storage) -> None:
    schema = {"type": "object", "properties": {"a": {"$ref": "#/$defs/missing"}}}
    msg = await server.create_skill(
        CreateSkillInput(
            name="bad_ref", description="regression test", code="__result__ = 1", parameters_schema=schema
        )
    )
    assert "does not resolve" in msg
    remote = {"type": "object", "properties": {"a": {"$ref": "https://example.com/s.json"}}}
    assert "not supported" in (contracts.check_schema(remote) or "")


async def test_legacy_unresolvable_ref_runs_with_warning(storage: Storage) -> None:
    _write_skill_file(
        storage,
        "legacy_ref",
        parameters_schema={"type": "object", "properties": {"a": {"$ref": "#/$defs/nope"}}},
    )
    out = await _invoke("legacy_ref", {"a": 1})
    assert "ran" in out and "Parameters not validated" in out


def test_older_drafts_and_nullable_object_schemas_are_accepted() -> None:
    draft4 = {
        "$schema": "http://json-schema.org/draft-04/schema#",
        "type": "object",
        "properties": {"n": {"type": "number", "maximum": 5, "exclusiveMaximum": True}},
    }
    assert contracts.check_schema(draft4) is None
    assert contracts.validate_params(draft4, {"n": 5})  # 5 is excluded under draft-04
    assert contracts.check_schema({"type": ["object", "null"]}) is None
    local = {"type": "object", "$defs": {"n": {"type": "integer"}}, "properties": {"n": {"$ref": "#/$defs/n"}}}
    assert contracts.check_schema(local) is None
    assert contracts.validate_params(local, {"n": "x"})


# ----------------------------------------------------------------------------
# Versioning
# ----------------------------------------------------------------------------


async def test_string_versions_are_tolerated(storage: Storage) -> None:
    _write_skill_file(storage, "str_version", version="1.0")
    assert "ran" in await _invoke("str_version")
    assert "version 2" in await server.update_skill("str_version", code="__result__ = 'v2'")

    _write_skill_file(storage, "str_current", version="2")
    assert "historical" not in await server.get_skill("str_current", version=2)


async def test_recreated_skill_does_not_inherit_old_history(storage: Storage) -> None:
    await _create("phoenix", "__result__ = 'old'")
    await server.update_skill("phoenix", code="__result__ = 'old v2'")
    await _invoke("phoenix")
    (storage.skills_dir / "phoenix.json").unlink()  # removed outside the tools
    server.SKILL_INDEX.rebuild()

    await _create("phoenix", "__result__ = 'new'")

    assert server.SKILL_VERSIONS.versions("phoenix") == [1]
    assert server.TELEMETRY.records(skill_name="phoenix") == []


async def test_name_field_cannot_redirect_snapshot_paths(storage: Storage, tmp_path: Path) -> None:
    _write_skill_file(storage, "evil", name="../../../ESCAPED_" + tmp_path.name)

    msg = await server.update_skill("evil", code="__result__ = 2")

    assert "version 2" in msg
    assert not list(storage.root.parent.rglob("ESCAPED_*"))
    assert json.loads((storage.skills_dir / "evil.json").read_text())["name"] == "evil"
    assert server.SKILL_VERSIONS.versions("evil") == [1, 2]


async def test_undo_hint_omitted_when_old_version_was_pruned(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skill_orchestrator_mcp.skill_versions import SkillVersionStore

    monkeypatch.setattr(server, "SKILL_VERSIONS", SkillVersionStore(storage.skills_dir, max_versions=1))
    await _create("short_memory", "__result__ = 1")

    msg = await server.update_skill("short_memory", code="__result__ = 2")

    assert "version 2" in msg and "rollback_skill" not in msg


# ----------------------------------------------------------------------------
# Telemetry
# ----------------------------------------------------------------------------


def test_last_run_uses_timestamps_not_file_order(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    t.record(kind="skill", status="ok", duration_ms=1, skill_name="c", ts="2026-01-01T00:00:02+00:00")
    # A nested failure from an older, slower parent run is written later.
    t.record(kind="nested_skill", status="error", duration_ms=1, skill_name="c",
             error="old failure", ts="2026-01-01T00:00:01+00:00")

    s = t.skill_stats("c")

    assert s["last_status"] == "ok" and s["last_run_at"].endswith("00:00:02+00:00")
    assert [r["status"] for r in t.recent(skill_name="c")] == ["ok", "error"]


def test_record_never_raises_on_bad_input(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    assert t.record(kind="skill", status="ok", duration_ms="n/a") is None  # type: ignore[arg-type]


async def test_pipe_in_error_does_not_break_stats_table(storage: Storage) -> None:
    await _create("pipe_error", "x = 1 | 'a'")
    await _invoke("pipe_error")

    detail = await server.get_skill_stats("pipe_error")

    row = next(line for line in detail.splitlines() if line.startswith("| v1 "))
    assert row.count("|") - row.count("\\|") == 7
