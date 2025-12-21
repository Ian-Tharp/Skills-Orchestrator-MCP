from __future__ import annotations

import json
from pathlib import Path

from skill_orchestrator_mcp.skill_index import SkillIndex


def _write_skill(path: Path, *, name: str, invocation_count: int = 0) -> None:
    payload = {
        "id": "abc123",
        "name": name,
        "description": "A test skill used for indexing",
        "code": "print('hello')",
        "category": "utility",
        "tags": ["test"],
        "dependencies": [],
        "examples": ["Example usage"],
        "invocation_count": invocation_count,
        "version": 1,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_skill_index_rebuild_excludes_code(tmp_path: Path) -> None:
    # Arrange
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    _write_skill(skills_dir / "alpha.json", name="alpha", invocation_count=3)

    idx = SkillIndex(skills_dir)

    # Act
    idx.rebuild()
    alpha = idx.get("alpha")

    # Assert
    assert alpha is not None
    assert alpha["name"] == "alpha"
    assert "code" not in alpha
    assert alpha["invocation_count"] == 3


def test_skill_index_upsert_and_remove_persist(tmp_path: Path) -> None:
    # Arrange
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    idx = SkillIndex(skills_dir)
    idx.rebuild()

    skill = {
        "id": "def456",
        "name": "beta",
        "description": "Another test skill",
        "code": "print('beta')",
        "category": "utility",
    }

    # Act
    idx.upsert_from_skill(skill)
    after_upsert = idx.get("beta")
    idx.remove("beta")
    after_remove = idx.get("beta")

    # Assert
    assert after_upsert is not None
    assert after_upsert["name"] == "beta"
    assert "code" not in after_upsert
    assert after_remove is None


def test_skill_index_maybe_refresh_detects_new_skill(tmp_path: Path) -> None:
    # Arrange
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    _write_skill(skills_dir / "alpha.json", name="alpha")
    idx = SkillIndex(skills_dir)
    idx.rebuild()

    # Act
    _write_skill(skills_dir / "gamma.json", name="gamma")
    refreshed = idx.maybe_refresh_from_disk()

    # Assert
    assert refreshed is True
    assert idx.get("gamma") is not None


