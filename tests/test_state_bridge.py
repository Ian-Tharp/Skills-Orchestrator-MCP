"""Tests for the state bridge: get_state/set_state/delete_state inside skills."""

from __future__ import annotations

import json

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ExecuteCodeInput,
    InvokeSkillInput,
    ReturnMode,
    StateInput,
)

from .conftest import Storage


async def _create(name: str, code: str) -> None:
    msg = await server.create_skill(CreateSkillInput(name=name, description="state bridge test", code=code))
    assert "created successfully" in msg, msg


async def _invoke(name: str, **kw: object) -> str:
    return await server.invoke_skill(
        InvokeSkillInput(skill_name=name, return_mode=ReturnMode.FULL, **kw)  # type: ignore[arg-type]
    )


def _state(storage: Storage, ns: str = "default") -> dict[str, object]:
    path = storage.state_dir / f"{ns}.json"
    return json.loads(path.read_text()) if path.exists() else {}


async def test_skill_writes_are_visible_to_mcp_tools(storage: Storage) -> None:
    await _create("writer", "set_state('session.last', {'step': 3}, namespace='proj')\n__result__ = 'done'")

    out = await _invoke("writer")

    assert "committed 1 write(s) to 'proj' (1)" in out
    assert json.loads(await server.get_state("session.last", "proj")) == {"step": 3}


async def test_skill_reads_state_written_by_tools(storage: Storage) -> None:
    await server.set_state(StateInput(key="prefs.theme", value="dark", namespace="ui"))
    await _create("reader", "__result__ = {'theme': get_state('prefs.theme', 'ui'), 'x': get_state('nope', 'ui', 7)}")

    out = await _invoke("reader")

    assert '"theme": "dark"' in out and '"x": 7' in out


async def test_read_your_writes_within_a_run(storage: Storage) -> None:
    code = (
        "set_state('counter', get_state('counter', default=0) + 1)\n"
        "set_state('counter', get_state('counter') + 1)\n"
        "__result__ = get_state('counter')"
    )
    await _create("counter", code)

    await _invoke("counter")
    out = await _invoke("counter")

    assert "**Result:**\n```\n4\n```" in out
    assert _state(storage) == {"counter": 4}


async def test_failed_run_discards_writes(storage: Storage) -> None:
    await server.set_state(StateInput(key="balance", value=100))
    await _create("half_done", "set_state('balance', 0)\nraise RuntimeError('crash mid-transfer')")

    out = await _invoke("half_done")

    assert "1 pending write(s) discarded" in out
    assert _state(storage) == {"balance": 100}
    (rec,) = server.TELEMETRY.records(skill_name="half_done")
    assert rec["status"] == "error" and "state_writes" not in rec


async def test_timed_out_run_discards_writes(storage: Storage) -> None:
    await _create("slow_writer", "set_state('k', 1)\nimport time\ntime.sleep(10)")

    await _invoke("slow_writer", timeout=1)

    assert _state(storage) == {}


async def test_caught_nested_failure_rolls_back_only_the_child(storage: Storage) -> None:
    await _create("child_writer", "set_state('child', 1)\nraise ValueError('child failed')")
    await _create(
        "parent_writer",
        "set_state('parent', 1)\n"
        "try:\n    invoke_skill('child_writer')\nexcept ValueError:\n    pass\n"
        "__result__ = get_state('child', default='rolled back')",
    )

    out = await _invoke("parent_writer")

    assert "rolled back" in out
    assert _state(storage) == {"parent": 1}


async def test_nested_skills_share_state_within_a_run(storage: Storage) -> None:
    await _create("child_inc", "set_state('n', get_state('n', default=0) + 1)")
    await _create("parent_inc", "invoke_skill('child_inc')\ninvoke_skill('child_inc')\n__result__ = get_state('n')")

    out = await _invoke("parent_inc")

    assert "**Result:**\n```\n2\n```" in out
    assert _state(storage) == {"n": 2}


async def test_delete_state_and_list_keys(storage: Storage) -> None:
    await server.set_state(StateInput(key="a.b", value=1, namespace="ns"))
    await server.set_state(StateInput(key="a.c", value=2, namespace="ns"))
    await _create(
        "pruner",
        "before = list_state_keys('ns')\n"
        "removed = delete_state('a.b', 'ns')\n"
        "missing = delete_state('zzz', 'ns')\n"
        "__result__ = {'before': before, 'after': list_state_keys('ns'), 'removed': removed, 'missing': missing}",
    )

    out = await _invoke("pruner")

    assert '"before": [\n    "a.b",\n    "a.c"\n  ]' in out
    assert '"removed": true' in out and '"missing": false' in out
    assert _state(storage, "ns") == {"a": {"c": 2}}


async def test_commit_merges_with_other_keys(storage: Storage) -> None:
    # The skill only replays its own ops onto the file, so keys it didn't
    # touch (e.g. written by another agent meanwhile) survive.
    await _create("touch_one", "set_state('mine', 1)")
    await server.set_state(StateInput(key="theirs", value="keep"))

    await _invoke("touch_one")

    assert _state(storage) == {"theirs": "keep", "mine": 1}


async def test_invalid_namespace_raises_in_skill(storage: Storage) -> None:
    await _create("escaper", "set_state('k', 1, namespace='../skills/evil')")

    out = await _invoke("escaper")

    assert "Invalid state namespace" in out
    assert not (storage.skills_dir / "evil.json").exists()


async def test_non_json_value_rejected_early(storage: Storage) -> None:
    await _create("bad_value", "set_state('k', object())")

    out = await _invoke("bad_value")

    assert "not JSON-serializable" in out
    assert _state(storage) == {}


async def test_set_through_non_object_reports_error(storage: Storage) -> None:
    await server.set_state(StateInput(key="leaf", value=5))
    await _create("bad_path", "set_state('leaf.child', 1)")

    out = await _invoke("bad_path")

    assert "holds a int, not an object" in out


async def test_execute_code_has_state_bridge(storage: Storage) -> None:
    out = await server.execute_code(
        ExecuteCodeInput(code="set_state('from_code', True)\n__result__ = get_state('from_code')", return_mode=ReturnMode.FULL)
    )

    assert "True" in out
    assert _state(storage) == {"from_code": True}


async def test_delete_state_tool(storage: Storage) -> None:
    await server.set_state(StateInput(key="x.y", value=1))

    assert "deleted" in await server.delete_state("x.y")
    assert "not found" in await server.delete_state("x.y")
    assert _state(storage) == {"x": {}}
    assert (await server.delete_state("k", "../bad")).startswith("Error")
