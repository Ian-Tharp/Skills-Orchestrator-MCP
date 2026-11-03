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
    # Reap every worker before asserting, so a failure can't leak a running process.
    results = [p.communicate(timeout=120) for p in procs]
    for p, (_, err) in zip(procs, results):
        assert p.returncode == 0, err
    return [out for out, _ in results]


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
    assert "remote refs are not fetched" in (contracts.check_schema(remote) or "")


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


# ----------------------------------------------------------------------------
# Second review round (bugs introduced by the first round of fixes)
# ----------------------------------------------------------------------------


def test_dotted_dependencies_check_the_submodule_without_importing() -> None:
    contracts._dep_cache.clear()
    sys.modules.pop("this", None)
    assert contracts.missing_dependencies(["json.decoder"]) == []
    assert contracts.missing_dependencies(["json.nonexistent_submodule"])
    assert contracts.missing_dependencies(["this.zen"])  # `this` is a module, not a package
    assert "this" not in sys.modules


@pytest.mark.parametrize(
    "schema",
    [
        # "$ref" inside instance data is not a reference.
        {"type": "object", "properties": {"a": {"default": {"$ref": "x"}, "examples": [{"$ref": "y"}]}}},
        # A property literally named "$ref".
        {"type": "object", "properties": {"$ref": True}},
        # Percent-encoded JSON pointer.
        {"type": "object", "$defs": {"a b": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/a%20b"}}},
        # $anchor and $id-relative references.
        {"type": "object", "$defs": {"n": {"$anchor": "num", "type": "integer"}}, "properties": {"a": {"$ref": "#num"}}},
        {
            "$id": "https://example.com/root",
            "type": "object",
            "$defs": {"n": {"$id": "n.json", "type": "integer"}},
            "properties": {"a": {"$ref": "https://example.com/n.json"}},
        },
    ],
)
def test_valid_ref_usages_are_accepted(schema: dict[str, Any]) -> None:
    assert contracts.check_schema(schema) is None


@pytest.mark.parametrize("bad", [["x"], {"a": 1}, 7])
async def test_non_string_dollar_schema_is_rejected_cleanly(storage: Storage, bad: Any) -> None:
    schema = {"$schema": bad, "type": "object"}
    assert "$schema must be a string" in (contracts.check_schema(schema) or "")
    msg = await server.create_skill(
        CreateSkillInput(name="odd_schema", description="regression test", code="__result__ = 1",
                         parameters_schema=schema)
    )
    assert msg.startswith("Error: Invalid parameters_schema")
    _write_skill_file(storage, "odd_legacy", parameters_schema=schema)
    out = await _invoke("odd_legacy")
    assert "ran" in out and "Parameters not validated" in out


async def test_string_dependency_is_not_split_into_characters(storage: Storage) -> None:
    _write_skill_file(storage, "str_dep2", dependencies="surely-not-installed-pkg-xyz")

    msg = await server.update_skill("str_dep2", description="updated description text")
    shown = await server.get_skill("str_dep2")

    assert "surely-not-installed-pkg-xyz" in msg and '"s" "u"' not in msg
    assert "Dependencies:** surely-not-installed-pkg-xyz" in shown


async def test_infinite_version_is_tolerated(storage: Storage) -> None:
    (storage.skills_dir / "inf_version.json").write_text(
        '{"id": "x", "name": "inf_version", "description": "d", "code": "__result__ = 1", "version": Infinity}'
    )
    server.SKILL_INDEX.rebuild()
    assert "Result" in await _invoke("inf_version")
    assert "Version:** 1" in await server.get_skill("inf_version")


async def test_create_refuses_to_overwrite_empty_skill_file(storage: Storage) -> None:
    (storage.skills_dir / "blank.json").write_text("{}")
    msg = await server.create_skill(
        CreateSkillInput(name="blank", description="regression test", code="__result__ = 1")
    )
    assert "already exists but is empty or unreadable" in msg
    assert (storage.skills_dir / "blank.json").read_text() == "{}"


async def test_nested_sys_exit_is_an_error_for_the_caller(storage: Storage) -> None:
    await _create("child_exit", "set_state('c', 1)\nimport sys\nsys.exit(0)")
    # A caller can still catch the child's exit, as before contracts existed...
    await _create(
        "parent_of_exit",
        "set_state('p', 1)\n"
        "try:\n    invoke_skill('child_exit')\nexcept SystemExit as e:\n    set_state('caught', e.code)\n"
        "set_state('after', 1)",
    )
    # ...but if nobody catches it, the cut-off parent must not commit as "ok".
    await _create("parent_uncaught", "set_state('p2', 1)\ninvoke_skill('child_exit')\nset_state('after2', 1)")

    caught = await _invoke("parent_of_exit")
    uncaught = await _invoke("parent_uncaught")

    assert _state(storage) == {"p": 1, "caught": 0, "after": 1}
    assert "committed" in caught
    assert "Skill 'child_exit' called sys.exit(0)" in uncaught and "discarded" in uncaught
    (rec,) = server.TELEMETRY.records(skill_name="parent_uncaught")
    assert rec["status"] == "error"


async def test_parent_invoke_skill_wrapper_reaches_grandchildren(storage: Storage) -> None:
    await _create("leaf2", "__result__ = 'leaf'")
    await _create("mid2", "__result__ = invoke_skill('leaf2')")
    await _create(
        "tracer",
        "calls = []\n_orig = invoke_skill\n"
        "def invoke_skill(name, *a, **k):\n    calls.append(name)\n    return _orig(name, *a, **k)\n"
        "invoke_skill('mid2')\n__result__ = calls",
    )

    out = await _invoke("tracer")

    assert '"mid2",\n  "leaf2"' in out


async def test_warn_mode_passes_non_dict_nested_params(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT", "warn")
    await _create("typed_list", "__result__ = len(params)", parameters_schema={"type": "object"})
    await _create("sends_list", "__result__ = invoke_skill('typed_list', [1, 2])")

    out = await _invoke("sends_list")

    assert "**Result:**\n```\n2\n```" in out and "expected a dict" in out


async def test_child_rolls_back_even_if_its_exception_cannot_be_printed(storage: Storage) -> None:
    await _create(
        "unprintable",
        "class Weird(Exception):\n    def __str__(self):\n        raise RuntimeError('no str')\n"
        "set_state('child', 1)\nraise Weird()",
    )
    await _create("catches_weird", "try:\n    invoke_skill('unprintable')\nexcept Exception:\n    pass")

    await _invoke("catches_weird")

    assert _state(storage) == {}


async def test_sys_exit_with_message_reads_cleanly(storage: Storage) -> None:
    await _create("exit_msg", "import sys\nsys.exit('fatal: bad input')")
    out = await _invoke("exit_msg")
    assert "sys.exit('fatal: bad input')" in out


async def test_lock_wait_does_not_block_the_event_loop(storage: Storage, tmp_path: Path) -> None:
    import asyncio

    from skill_orchestrator_mcp.models import StateInput

    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(REPO_ROOT)!r})
            from pathlib import Path
            from skill_orchestrator_mcp.locking import file_lock
            with file_lock(Path({str(storage.state_dir / ".state.lock")!r})):
                print("held", flush=True)
                time.sleep(1.5)
        """)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    t = asyncio.create_task(ticker())
    msg = await server.set_state(StateInput(key="k", value=1))
    t.cancel()
    holder.communicate(timeout=10)

    assert msg.startswith("✅")
    assert ticks >= 10  # the loop kept running while set_state waited ~1.5 s for the lock


def test_lock_falls_back_when_locking_is_unsupported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    from skill_orchestrator_mcp import locking

    def unsupported(fd: int) -> bool:
        return locking._classify(OSError(errno.ENOLCK, "No locks available"))

    monkeypatch.setattr(locking, "_try_lock", unsupported)
    with locking.file_lock(tmp_path / "x.lock", timeout=0.5):
        pass  # no 30 s wait, no exception


def test_aliased_stdlib_submodules_count_as_installed() -> None:
    contracts._dep_cache.clear()
    assert contracts.missing_dependencies(["os.path", "collections.abc"]) == []


def test_unresolvable_dynamic_ref_rejected_at_create() -> None:
    bad = {"type": "object", "properties": {"k": {"$dynamicRef": "#nope"}}}
    good = {
        "type": "object",
        "$defs": {"n": {"$dynamicAnchor": "node", "type": "integer"}},
        "properties": {"k": {"$dynamicRef": "#node"}},
    }
    assert "does not resolve" in (contracts.check_schema(bad) or "")
    assert contracts.check_schema(good) is None


def test_servers_starting_at_the_same_time_do_not_crash(tmp_path: Path) -> None:
    # Each MCP client starts its own server; on Windows, simultaneous startups
    # rewriting the skill index used to fail os.replace with "Access is denied".
    script = """
        import asyncio, sys
        from skill_orchestrator_mcp import server
        for _ in range(30):
            server.SKILL_INDEX.rebuild()
            server.USAGE_TRACKER.increment("x")
            server.USAGE_TRACKER.flush()
    """
    _run_workers(script, tmp_path, n=4)
