"""Tests for skill CRUD, storage and search tools in server.py."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ResponseFormat,
    SearchSkillsInput,
    SkillCategory,
)

from .conftest import Storage


def _skill_input(name: str = "sample_skill", **overrides: Any) -> CreateSkillInput:
    fields: dict[str, Any] = {
        "name": name,
        "description": "A sample skill used in unit tests",
        "code": "__result__ = {'ok': True}",
        "category": SkillCategory.UTILITY,
        "tags": ["unit", "sample"],
        "examples": ["run the sample"],
    }
    fields.update(overrides)
    return CreateSkillInput(**fields)


# ----------------------------------------------------------------------------
# create_skill
# ----------------------------------------------------------------------------


async def test_create_skill_persists_file_and_index(storage: Storage) -> None:
    # Act
    msg = await server.create_skill(_skill_input())

    # Assert
    assert "Skill created successfully" in msg
    on_disk = json.loads((storage.skills_dir / "sample_skill.json").read_text())
    assert on_disk["name"] == "sample_skill"
    assert on_disk["version"] == 1
    assert on_disk["category"] == "utility"
    meta = server.SKILL_INDEX.get("sample_skill")
    assert meta is not None and "code" not in meta


async def test_create_skill_rejects_duplicate_name(storage: Storage) -> None:
    await server.create_skill(_skill_input())

    msg = await server.create_skill(_skill_input(description="Another description here"))

    assert msg.startswith("Error: Skill 'sample_skill' already exists")


async def test_create_skill_rejects_syntax_error_and_writes_nothing(storage: Storage) -> None:
    msg = await server.create_skill(_skill_input(code="def broken(:\n    pass"))

    assert msg.startswith("Error: Invalid Python syntax")
    assert not (storage.skills_dir / "sample_skill.json").exists()
    assert server.SKILL_INDEX.get("sample_skill") is None


@pytest.mark.parametrize("bad_name", ["../evil", "Upper", "1starts_with_digit", "has-dash", ""])
async def test_create_skill_input_rejects_unsafe_names(bad_name: str) -> None:
    with pytest.raises(ValidationError):
        _skill_input(name=bad_name)


# ----------------------------------------------------------------------------
# get_skill / update_skill / delete_skill
# ----------------------------------------------------------------------------


async def test_get_skill_returns_code_and_usage(storage: Storage) -> None:
    await server.create_skill(_skill_input(parameters_schema={"type": "object"}))
    server.USAGE_TRACKER.increment("sample_skill", by=3)

    text = await server.get_skill("sample_skill")

    assert "# Skill: sample_skill" in text
    assert "__result__ = {'ok': True}" in text
    assert "**Invocations:** 3" in text
    assert "## Parameters Schema" in text


async def test_get_skill_missing_returns_error(storage: Storage) -> None:
    assert (await server.get_skill("nope")).startswith("Error: Skill 'nope' not found")


async def test_update_skill_bumps_version_and_keeps_unset_fields(storage: Storage) -> None:
    await server.create_skill(_skill_input())

    msg = await server.update_skill("sample_skill", description="Updated description text")

    assert "updated to version 2" in msg
    on_disk = json.loads((storage.skills_dir / "sample_skill.json").read_text())
    assert on_disk["description"] == "Updated description text"
    assert on_disk["code"] == "__result__ = {'ok': True}"
    assert on_disk["tags"] == ["unit", "sample"]
    meta = server.SKILL_INDEX.get("sample_skill")
    assert meta is not None and meta["description"] == "Updated description text"


async def test_update_skill_rejects_bad_syntax_without_saving(storage: Storage) -> None:
    await server.create_skill(_skill_input())

    msg = await server.update_skill("sample_skill", code="if True print('x')")

    assert msg.startswith("Error: Invalid Python syntax")
    on_disk = json.loads((storage.skills_dir / "sample_skill.json").read_text())
    assert on_disk["version"] == 1


async def test_update_skill_missing_returns_error(storage: Storage) -> None:
    assert (await server.update_skill("ghost", description="x" * 20)).startswith("Error")


async def test_delete_skill_requires_confirm(storage: Storage) -> None:
    await server.create_skill(_skill_input())

    msg = await server.delete_skill("sample_skill")

    assert "confirm=True" in msg
    assert (storage.skills_dir / "sample_skill.json").exists()


async def test_delete_skill_removes_file_index_and_usage(storage: Storage) -> None:
    await server.create_skill(_skill_input())
    server.USAGE_TRACKER.increment("sample_skill")

    msg = await server.delete_skill("sample_skill", confirm=True)

    assert "permanently deleted" in msg
    assert not (storage.skills_dir / "sample_skill.json").exists()
    assert server.SKILL_INDEX.get("sample_skill") is None
    assert server.USAGE_TRACKER.get("sample_skill") == 0


async def test_delete_skill_missing_returns_error(storage: Storage) -> None:
    assert (await server.delete_skill("ghost", confirm=True)).startswith("Error")


# ----------------------------------------------------------------------------
# Path traversal (bug: get/update/delete took raw names and built file paths)
# ----------------------------------------------------------------------------


async def test_delete_skill_cannot_escape_skills_dir(storage: Storage) -> None:
    # A state namespace file that lives *next to* the skills dir.
    victim = storage.state_dir / "default.json"
    victim.write_text("{}")

    msg = await server.delete_skill("../state/default", confirm=True)

    assert msg.startswith("Error")
    assert victim.exists()


async def test_delete_skill_cannot_remove_internal_dotfiles(storage: Storage) -> None:
    await server.create_skill(_skill_input())
    index_file = server.SKILL_INDEX.index_path
    assert index_file.exists()

    msg = await server.delete_skill(".skill_index", confirm=True)

    assert msg.startswith("Error")
    assert index_file.exists()


async def test_get_skill_cannot_read_outside_skills_dir(storage: Storage) -> None:
    secret = storage.state_dir / "secret.json"
    secret.write_text(json.dumps({"name": "x", "description": "d", "code": "TOP SECRET"}))

    text = await server.get_skill("../state/secret")

    assert text.startswith("Error")
    assert "TOP SECRET" not in text


async def test_update_skill_cannot_write_outside_skills_dir(storage: Storage) -> None:
    target = storage.state_dir / "other.json"
    target.write_text(json.dumps({"name": "../state/other", "description": "d", "code": "x = 1"}))

    msg = await server.update_skill("../state/other", code="x = 2")

    assert msg.startswith("Error")
    assert json.loads(target.read_text())["code"] == "x = 1"


async def test_skill_resource_rejects_traversal(storage: Storage) -> None:
    (storage.state_dir / "secret.json").write_text(json.dumps({"code": "TOP SECRET"}))

    payload = json.loads(server.get_skill_resource("../state/secret"))

    assert "error" in payload


# ----------------------------------------------------------------------------
# search_skills
# ----------------------------------------------------------------------------


async def _create_many(n: int) -> None:
    for i in range(n):
        await server.create_skill(
            _skill_input(name=f"skill_{i:02d}", tags=[f"t{i % 2}"], examples=[])
        )


async def test_search_skills_empty_library(storage: Storage) -> None:
    text = await server.search_skills(SearchSkillsInput())
    assert text.startswith("No skills found")


async def test_search_skills_filters_by_query_category_and_tags(storage: Storage) -> None:
    await server.create_skill(_skill_input(name="csv_parser", description="Parses CSV files quickly", tags=["csv"]))
    await server.create_skill(
        _skill_input(
            name="chart_maker",
            description="Draws charts from data",
            category=SkillCategory.VISUALIZATION,
            tags=["charts"],
        )
    )

    by_query = json.loads(await server.search_skills(SearchSkillsInput(query="CSV", response_format=ResponseFormat.JSON)))
    by_cat = json.loads(await server.search_skills(SearchSkillsInput(category=SkillCategory.VISUALIZATION, response_format=ResponseFormat.JSON)))
    by_tag = json.loads(await server.search_skills(SearchSkillsInput(tags=["charts", "nomatch"], response_format=ResponseFormat.JSON)))

    assert [s["name"] for s in by_query["items"]] == ["csv_parser"]
    assert [s["name"] for s in by_cat["items"]] == ["chart_maker"]
    assert [s["name"] for s in by_tag["items"]] == ["chart_maker"]


async def test_search_skills_pagination_and_cursor_past_end(storage: Storage) -> None:
    await _create_many(5)

    page1 = json.loads(await server.search_skills(SearchSkillsInput(limit=2, response_format=ResponseFormat.JSON)))
    past_end = json.loads(await server.search_skills(SearchSkillsInput(cursor=99, response_format=ResponseFormat.JSON)))

    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page1["next_cursor"] == 2
    assert past_end["items"] == []
    assert past_end["next_cursor"] is None


async def test_search_skills_sorts_by_usage_and_projects_fields(storage: Storage) -> None:
    await _create_many(3)
    server.USAGE_TRACKER.increment("skill_02", by=5)

    res = json.loads(
        await server.search_skills(
            SearchSkillsInput(fields=["category"], include_code=True, response_format=ResponseFormat.JSON)
        )
    )

    assert res["items"][0]["name"] == "skill_02"
    # `name` is always included; nothing outside the projection leaks through.
    assert set(res["items"][0]) == {"name", "category"}


async def test_search_skills_include_code_markdown(storage: Storage) -> None:
    await server.create_skill(_skill_input())
    text = await server.search_skills(SearchSkillsInput(include_code=True))
    assert "```python\n__result__ = {'ok': True}\n```" in text


async def test_search_skills_tolerates_out_of_band_skill_without_description(storage: Storage) -> None:
    # Skill files dropped in by hand may miss fields; search must not crash.
    (storage.skills_dir / "handmade.json").write_text(json.dumps({"name": "handmade", "code": "x = 1"}))

    text = await server.search_skills(SearchSkillsInput(query="hand"))

    assert "handmade" in text


async def test_search_skills_ignores_corrupt_skill_file(storage: Storage) -> None:
    await server.create_skill(_skill_input())
    (storage.skills_dir / "broken.json").write_text("{not json")

    text = await server.search_skills(SearchSkillsInput())

    assert "sample_skill" in text
    assert "broken" not in text


# ----------------------------------------------------------------------------
# status / resources
# ----------------------------------------------------------------------------


async def test_orchestrator_status_counts(storage: Storage) -> None:
    await _create_many(2)
    (storage.state_dir / "ns.json").write_text("{}")
    server.USAGE_TRACKER.increment("skill_00", by=4)

    text = await server.get_orchestrator_status()

    assert "**Total Skills:** 2" in text
    assert "**Total Invocations:** 4" in text
    assert "**State Namespaces:** 1" in text
    assert str(storage.skills_dir) in text


async def test_skills_list_resource(storage: Storage) -> None:
    await _create_many(2)
    payload = json.loads(server.list_all_skills())
    assert payload["total"] == 2
    assert {i["name"] for i in payload["items"]} == {"skill_00", "skill_01"}


async def test_list_categories_covers_every_enum_value() -> None:
    text = await server.list_categories()
    for cat in SkillCategory:
        assert f"**{cat.value}**" in text
