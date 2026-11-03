"""Tests for the bundled built-in skills."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from skill_orchestrator_mcp import builtin_skills
from skill_orchestrator_mcp import skill_contracts as contracts
from skill_orchestrator_mcp.models import CreateSkillInput


@pytest.mark.parametrize("skill", builtin_skills.BUILTIN_SKILLS, ids=lambda s: s["name"])
def test_builtin_skill_is_valid(skill: dict[str, object]) -> None:
    # Same validation create_skill applies to user skills.
    fields = ("name", "description", "code", "category", "tags", "examples")
    CreateSkillInput.model_validate({k: skill[k] for k in fields if k in skill})
    compile(str(skill["code"]), f"builtin_{skill['name']}", "exec")


@pytest.mark.parametrize("skill", builtin_skills.BUILTIN_SKILLS, ids=lambda s: s["name"])
def test_builtin_skill_contracts_are_valid(skill: dict[str, Any]) -> None:
    # invoke_skill enforces these, so a bad built-in schema would block the skill.
    assert contracts.check_schema(skill.get("parameters_schema")) is None
    assert contracts.invalid_dependencies(list(skill.get("dependencies") or [])) == []


def test_builtin_names_are_unique() -> None:
    names = [s["name"] for s in builtin_skills.BUILTIN_SKILLS]
    assert len(names) == len(set(names))


def test_install_builtin_skills_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin_skills, "SKILLS_DIR", tmp_path)

    first = builtin_skills.install_builtin_skills()
    second = builtin_skills.install_builtin_skills()

    assert len(first["installed"]) == first["total"] and first["skipped"] == []
    assert second["installed"] == [] and len(second["skipped"]) == first["total"]
    for name in first["installed"]:
        data = json.loads((tmp_path / f"{name}.json").read_text())
        assert data["builtin"] is True and data["version"] == 1


def test_install_does_not_overwrite_user_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(builtin_skills, "SKILLS_DIR", tmp_path)
    name = builtin_skills.BUILTIN_SKILLS[0]["name"]
    (tmp_path / f"{name}.json").write_text(json.dumps({"name": name, "code": "mine"}))

    result = builtin_skills.install_builtin_skills()

    assert name in result["skipped"]
    assert json.loads((tmp_path / f"{name}.json").read_text())["code"] == "mine"
