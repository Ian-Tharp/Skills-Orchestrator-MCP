"""Edge cases for skill composition, complementing test_skill_composition.py."""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch

import pytest

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import CreateSkillInput, InvokeSkillInput, ReturnMode

from .conftest import Storage


def _registry(**codes: str) -> Dict[str, Dict[str, Any]]:
    return {n: {"name": n, "code": c, "description": f"{n} skill"} for n, c in codes.items()}


def _result_json(output: str) -> Any:
    """Parse the JSON body of the **Result:** block in execution output."""
    body = output.split("**Result:**\n```\n", 1)[1].split("\n```", 1)[0]
    return json.loads(body)


async def _compose(registry: Dict[str, Dict[str, Any]], top: str) -> str:
    with patch("skill_orchestrator_mcp.server._build_skill_registry", return_value=registry):
        return await server._execute_code_internal(
            code=registry[top]["code"],
            timeout=10,
            enable_composition=True,
            current_skill_name=top,
        )


async def test_child_error_propagates_and_call_stack_is_restored() -> None:
    reg = _registry(
        parent=(
            "try:\n"
            "    invoke_skill('bad')\n"
            "except RuntimeError as e:\n"
            "    caught = str(e)\n"
            "__result__ = {'caught': caught, 'stack': list(_skill_call_stack), 'after': invoke_skill('good')}"
        ),
        bad="raise RuntimeError('child failed')",
        good="__result__ = __call_depth__",
    )

    result = _result_json(await _compose(reg, "parent"))

    assert result == {"caught": "child failed", "stack": ["parent"], "after": 2}


async def test_same_child_can_be_called_repeatedly() -> None:
    reg = _registry(
        parent="__result__ = [invoke_skill('inc', {'n': i}) for i in range(3)]",
        inc="__result__ = params['n'] + 1",
    )
    assert _result_json(await _compose(reg, "parent")) == [1, 2, 3]


async def test_child_without_result_returns_none() -> None:
    reg = _registry(parent="__result__ = {'child': invoke_skill('quiet')}", quiet="x = 1")
    assert '"child": null' in await _compose(reg, "parent")


async def test_child_with_empty_code_raises() -> None:
    reg = _registry(parent="invoke_skill('empty')", empty="")
    assert "has no code defined" in await _compose(reg, "parent")


async def test_missing_skill_error_truncates_available_list() -> None:
    reg = _registry(parent="invoke_skill('nope')", **{f"s{i:02d}": "x = 1" for i in range(12)})
    out = await _compose(reg, "parent")
    assert "Skill 'nope' not found" in out
    assert "..." in out


async def test_max_depth_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "MAX_SKILL_CALL_DEPTH", 2)
    reg = _registry(a="invoke_skill('b')", b="invoke_skill('c')", c="__result__ = 1")
    assert "Maximum skill call depth (2) exceeded" in await _compose(reg, "a")


async def test_child_does_not_see_parent_locals() -> None:
    reg = _registry(
        parent="secret = 'p'\n__result__ = invoke_skill('child')",
        child="__result__ = 'secret' in globals()",
    )
    out = await _compose(reg, "parent")
    assert "False" in out


# ----------------------------------------------------------------------------
# Registry built from real storage
# ----------------------------------------------------------------------------


async def _create(name: str, code: str) -> None:
    await server.create_skill(CreateSkillInput(name=name, description="composition storage test", code=code))


async def test_registry_reflects_updates_and_deletes(storage: Storage) -> None:
    await _create("leaf_skill", "__result__ = 'v1'")
    await _create("root_skill", "__result__ = invoke_skill('leaf_skill')")

    await server.update_skill("leaf_skill", code="__result__ = 'v2'")
    updated = await server.invoke_skill(InvokeSkillInput(skill_name="root_skill", return_mode=ReturnMode.FULL))
    await server.delete_skill("leaf_skill", confirm=True)
    deleted = await server.invoke_skill(InvokeSkillInput(skill_name="root_skill", return_mode=ReturnMode.FULL))

    assert "v2" in updated
    assert "Skill 'leaf_skill' not found" in deleted


async def test_one_malformed_skill_file_does_not_break_composition(storage: Storage) -> None:
    # Bug: _build_skill_registry indexed skill['code'] directly, so a single
    # hand-written skill file without `code` made every invocation fail.
    await _create("healthy_skill", "__result__ = 'still works'")
    (storage.skills_dir / "no_code.json").write_text(json.dumps({"name": "no_code", "description": "d"}))
    server.SKILL_INDEX.rebuild()

    out = await server.invoke_skill(InvokeSkillInput(skill_name="healthy_skill", return_mode=ReturnMode.FULL))

    assert "still works" in out
    assert "Execution Error" not in out
