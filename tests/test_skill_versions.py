"""Tests for skill version history, diff and rollback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    InvokeSkillInput,
    ResponseFormat,
    ReturnMode,
)
from skill_orchestrator_mcp.skill_versions import SkillVersionStore

from .conftest import Storage


async def _create(name: str = "vskill", code: str = "__result__ = 'v1'", **kw: Any) -> None:
    msg = await server.create_skill(
        CreateSkillInput(name=name, description="versioning test skill", code=code, **kw)
    )
    assert "created successfully" in msg, msg


def _on_disk(storage: Storage, name: str = "vskill") -> dict[str, Any]:
    return json.loads((storage.skills_dir / f"{name}.json").read_text())


async def test_create_and_update_snapshot_every_version(storage: Storage) -> None:
    await _create()
    await server.update_skill("vskill", code="__result__ = 'v2'", change_note="second")

    assert server.SKILL_VERSIONS.versions("vskill") == [1, 2]
    snap = server.SKILL_VERSIONS.get("vskill", 2)
    assert snap is not None and snap["change_note"] == "second"
    # Snapshots live in a dotdir, so the index never mistakes them for skills.
    server.SKILL_INDEX.rebuild()
    assert list(server.SKILL_INDEX.all()) == ["vskill"]


async def test_rollback_restores_content_as_new_version(storage: Storage) -> None:
    await _create(tags=["a"])
    await server.update_skill("vskill", code="raise ValueError('broken edit')", tags=["b"])

    msg = await server.rollback_skill("vskill", version=1)
    out = await server.invoke_skill(InvokeSkillInput(skill_name="vskill", return_mode=ReturnMode.FULL))

    assert "saved as version 3" in msg
    disk = _on_disk(storage)
    assert disk["version"] == 3 and disk["rolled_back_from"] == 1
    assert disk["tags"] == ["a"]
    assert "'v1'" in disk["code"] and "v1" in out
    assert server.SKILL_VERSIONS.versions("vskill") == [1, 2, 3]


async def test_rollback_can_itself_be_undone(storage: Storage) -> None:
    await _create()
    await server.update_skill("vskill", code="__result__ = 'v2'")
    await server.rollback_skill("vskill", version=1)

    await server.rollback_skill("vskill", version=2)

    assert "'v2'" in _on_disk(storage)["code"]


async def test_rollback_errors(storage: Storage) -> None:
    await _create()

    assert "already at version 1" in await server.rollback_skill("vskill", version=1)
    assert "no stored version 9" in await server.rollback_skill("vskill", version=9)
    assert (await server.rollback_skill("ghost", version=1)).startswith("Error")


async def test_rollback_restores_removed_schema(storage: Storage) -> None:
    await _create()
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    await server.update_skill("vskill", parameters_schema=schema)

    await server.rollback_skill("vskill", version=1)

    assert _on_disk(storage).get("parameters_schema") is None


async def test_expected_version_guards_concurrent_edits(storage: Storage) -> None:
    await _create()
    await server.update_skill("vskill", code="__result__ = 'agent A'")

    msg = await server.update_skill("vskill", code="__result__ = 'agent B'", expected_version=1)

    assert "Version conflict" in msg
    assert "agent A" in _on_disk(storage)["code"]
    ok = await server.update_skill("vskill", code="__result__ = 'agent B'", expected_version=2)
    assert "version 3" in ok


async def test_no_op_update_does_not_bump_version(storage: Storage) -> None:
    await _create()

    msg = await server.update_skill("vskill", code="__result__ = 'v1'")

    assert msg.startswith("No changes")
    assert _on_disk(storage)["version"] == 1


async def test_update_supports_schema_category_examples(storage: Storage) -> None:
    await _create()

    msg = await server.update_skill(
        "vskill",
        parameters_schema={"type": "object", "required": ["q"]},
        category="utility",
        examples=["run it"],
    )

    disk = _on_disk(storage)
    assert "version 2" in msg
    assert disk["category"] == "utility" and disk["examples"] == ["run it"]
    assert disk["parameters_schema"] == {"type": "object", "required": ["q"]}
    assert "Unknown category" in await server.update_skill("vskill", category="nope")
    assert "Invalid parameters_schema" in await server.update_skill(
        "vskill", parameters_schema={"type": "object", "required": "q"}
    )


async def test_legacy_skill_gets_snapshot_before_first_edit(storage: Storage) -> None:
    legacy = {
        "id": "abc",
        "name": "legacy",
        "description": "made before versioning",
        "code": "__result__ = 'old'",
        "version": 4,
    }
    (storage.skills_dir / "legacy.json").write_text(json.dumps(legacy))
    server.SKILL_INDEX.rebuild()

    await server.update_skill("legacy", code="__result__ = 'new'")
    await server.rollback_skill("legacy", version=4)

    assert server.SKILL_VERSIONS.versions("legacy") == [4, 5, 6]
    assert "'old'" in _on_disk(storage, "legacy")["code"]


async def test_diff_shows_code_and_metadata_changes(storage: Storage) -> None:
    await _create(tags=["a"])
    await server.update_skill("vskill", code="__result__ = 'v2'", tags=["a", "b"])

    diff = await server.diff_skill_versions("vskill", from_version=1)

    assert "v1 → v2" in diff
    assert "-__result__ = 'v1'" in diff and "+__result__ = 'v2'" in diff
    assert '**tags:** `["a"]` → `["a", "b"]`' in diff
    assert "no stored version 7" in await server.diff_skill_versions("vskill", from_version=7)


async def test_list_versions_includes_per_version_stats(storage: Storage) -> None:
    await _create()
    await server.invoke_skill(InvokeSkillInput(skill_name="vskill"))
    await server.update_skill("vskill", code="raise ValueError('regressed')", change_note="try faster path")
    await server.invoke_skill(InvokeSkillInput(skill_name="vskill"))

    md = await server.list_skill_versions("vskill")
    data = json.loads(await server.list_skill_versions("vskill", response_format=ResponseFormat.JSON))

    assert "v2 (current)" in md and "try faster path" in md
    v1, v2 = data["versions"]
    assert v1["stats"]["failure_rate"] == 0.0
    assert v2["stats"]["failure_rate"] == 1.0 and "regressed" in v2["stats"]["last_error"]


async def test_get_skill_can_show_past_version(storage: Storage) -> None:
    await _create()
    await server.update_skill("vskill", code="__result__ = 'v2'")

    old = await server.get_skill("vskill", version=1)
    cur = await server.get_skill("vskill")

    assert "historical v1" in old and "'v1'" in old and "current is v2" in old
    assert "'v2'" in cur and "Stored versions:** v1, v2" in cur
    assert "no stored version 5" in await server.get_skill("vskill", version=5)


async def test_delete_removes_history_and_telemetry(storage: Storage) -> None:
    await _create()
    await server.invoke_skill(InvokeSkillInput(skill_name="vskill"))

    await server.delete_skill("vskill", confirm=True)

    assert server.SKILL_VERSIONS.versions("vskill") == []
    assert server.TELEMETRY.records(skill_name="vskill") == []


def test_store_prunes_to_max_versions(tmp_path: Path) -> None:
    store = SkillVersionStore(tmp_path, max_versions=3)
    for v in range(1, 7):
        store.save("p", {"name": "p", "version": v, "code": f"x = {v}"})

    assert store.versions("p") == [4, 5, 6]
