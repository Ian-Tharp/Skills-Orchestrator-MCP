"""Tests for dependency and parameter-schema enforcement."""

from __future__ import annotations

import json
from typing import Any

import pytest

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp import skill_contracts as contracts
from skill_orchestrator_mcp.models import CreateSkillInput, InvokeSkillInput, ReturnMode

from .conftest import Storage

NUM_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 0},
        "label": {"type": "string", "default": "none"},
    },
    "required": ["n"],
    "additionalProperties": False,
}


async def _create(name: str, code: str, **kw: Any) -> str:
    return await server.create_skill(
        CreateSkillInput(name=name, description="contract test skill", code=code, **kw)
    )


async def _invoke(name: str, params: dict[str, Any] | None = None) -> str:
    return await server.invoke_skill(
        InvokeSkillInput(skill_name=name, parameters=params, return_mode=ReturnMode.FULL)
    )


# ----------------------------------------------------------------------------
# Unit: skill_contracts
# ----------------------------------------------------------------------------


def test_missing_dependencies() -> None:
    assert contracts.missing_dependencies(["pydantic", "json", "jsonschema>=4"]) == []
    missing = contracts.missing_dependencies(["surely-not-installed-pkg-xyz", "pydantic<1"])
    assert missing[0] == "surely-not-installed-pkg-xyz (not installed)"
    assert missing[1].startswith("pydantic<1 (installed: ")
    # Markers for another platform are skipped.
    assert contracts.missing_dependencies(['nopkg-xyz; sys_platform == "nonexistent"']) == []


def test_invalid_dependency_strings() -> None:
    assert contracts.invalid_dependencies(["pandas>=2", "requests"]) == []
    assert contracts.invalid_dependencies(["not a req!!"])


def test_check_schema() -> None:
    assert contracts.check_schema(None) is None
    assert contracts.check_schema(NUM_SCHEMA) is None
    assert "Invalid parameters_schema" in (contracts.check_schema({"type": "nope"}) or "")
    assert "must describe an object" in (contracts.check_schema({"type": "string"}) or "")


def test_validate_and_defaults() -> None:
    assert contracts.validate_params(NUM_SCHEMA, {"n": 1}) == []
    errors = contracts.validate_params(NUM_SCHEMA, {"n": -1, "extra": True})
    assert any("n:" in e and "minimum" in e for e in errors)
    assert any("extra" in e for e in errors)
    assert contracts.apply_defaults(NUM_SCHEMA, {"n": 1}) == {"n": 1, "label": "none"}


def test_find_skill_refs() -> None:
    code = (
        "a = invoke_skill('alpha', {})\n"
        "b = invoke_skill(skill_name=\"beta\")\n"
        "c = invoke_skill(name_var)\n"
        "d = invoke_skill('alpha')\n"
    )
    assert contracts.find_skill_refs(code) == ["alpha", "beta"]
    assert contracts.find_skill_refs("invoke_skill('gamma'") == ["gamma"]  # syntax error fallback


# ----------------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------------


async def test_invoke_refuses_skill_with_missing_dependency(storage: Storage) -> None:
    msg = await _create("needs_pkg", "__result__ = 1", dependencies=["surely-not-installed-pkg-xyz"])
    assert "Unmet dependencies" in msg  # created, with a warning

    out = await _invoke("needs_pkg")

    assert out.startswith("Error: Skill 'needs_pkg' has unmet dependencies")
    assert "uv pip install" in out
    (rec,) = server.TELEMETRY.records(skill_name="needs_pkg")
    assert rec["status"] == "rejected"
    assert server.USAGE_TRACKER.get("needs_pkg") == 0


async def test_satisfied_dependencies_run(storage: Storage) -> None:
    await _create("uses_pkg", "import jsonschema\n__result__ = 'ok'", dependencies=["jsonschema>=4"])
    assert "ok" in await _invoke("uses_pkg")


async def test_create_rejects_unparseable_dependency(storage: Storage) -> None:
    msg = await _create("bad_dep", "__result__ = 1", dependencies=["not a req!!"])
    assert msg.startswith("Error: Invalid dependency")
    assert not (storage.skills_dir / "bad_dep.json").exists()


async def test_nested_call_to_skill_with_missing_dependency(storage: Storage) -> None:
    await _create("needs_pkg2", "__result__ = 1", dependencies=["surely-not-installed-pkg-xyz"])
    await _create("calls_needy", "__result__ = invoke_skill('needs_pkg2')")

    out = await _invoke("calls_needy")

    assert "has unmet dependencies" in out
    (rec,) = server.TELEMETRY.records(skill_name="needs_pkg2")
    assert rec["status"] == "rejected" and rec["parent"] == "calls_needy"


async def test_get_skill_info_exposes_contracts(storage: Storage) -> None:
    await _create("described", "__result__ = 1", parameters_schema=NUM_SCHEMA, dependencies=["pydantic"])
    await _create("inspector", "__result__ = get_skill_info('described')")

    out = await _invoke("inspector")

    assert '"dependencies": [\n    "pydantic"\n  ]' in out
    assert '"missing_dependencies": []' in out
    assert '"version": 1' in out


# ----------------------------------------------------------------------------
# Parameter schemas
# ----------------------------------------------------------------------------


async def test_create_rejects_invalid_schema(storage: Storage) -> None:
    msg = await _create("bad_schema", "__result__ = 1", parameters_schema={"type": "object", "required": "n"})
    assert msg.startswith("Error: Invalid parameters_schema")


async def test_invalid_params_rejected_before_running(storage: Storage) -> None:
    await _create("typed", "set_state('ran', True)\n__result__ = params", parameters_schema=NUM_SCHEMA)

    out = await _invoke("typed", {"n": "three"})

    assert out.startswith("Error: Invalid parameters for skill 'typed'")
    assert "n: 'three' is not of type 'integer'" in out
    assert not (storage.state_dir / "default.json").exists()  # never ran
    (rec,) = server.TELEMETRY.records(skill_name="typed")
    assert rec["status"] == "rejected"


async def test_schema_defaults_are_applied(storage: Storage) -> None:
    await _create("typed2", "__result__ = params", parameters_schema=NUM_SCHEMA)

    out = await _invoke("typed2", {"n": 2})

    assert '"label": "none"' in out


async def test_warn_mode_runs_and_reports(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT", "warn")
    await _create("typed3", "__result__ = 'ran'", parameters_schema=NUM_SCHEMA)

    out = await _invoke("typed3", {})

    assert "Parameter schema violation: 'n' is a required property" in out
    assert "ran" in out


async def test_off_mode_skips_validation(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT", "off")
    await _create("typed4", "__result__ = params", parameters_schema=NUM_SCHEMA)

    out = await _invoke("typed4", {"n": "x"})

    assert "Warnings" not in out and '"n": "x"' in out


async def test_nested_params_are_validated(storage: Storage) -> None:
    await _create("typed_child", "__result__ = params['n'] + 1", parameters_schema=NUM_SCHEMA)
    await _create(
        "typed_parent",
        "good = invoke_skill('typed_child', {'n': 1})\n"
        "try:\n    invoke_skill('typed_child', {'n': -5})\n    bad = 'accepted'\n"
        "except ValueError as e:\n    bad = str(e)\n"
        "__result__ = {'good': good, 'bad': bad}",
    )

    out = await _invoke("typed_parent")

    assert '"good": 2' in out
    assert "Invalid parameters for skill 'typed_child'" in out
    statuses = [r["status"] for r in server.TELEMETRY.records(skill_name="typed_child")]
    assert statuses == ["ok", "rejected"]


async def test_legacy_invalid_schema_is_not_enforced(storage: Storage) -> None:
    legacy = {
        "id": "x",
        "name": "legacy_schema",
        "description": "hand-written skill with a broken schema",
        "code": "__result__ = 'ran'",
        "parameters_schema": {"type": "object", "required": "oops"},
    }
    (storage.skills_dir / "legacy_schema.json").write_text(json.dumps(legacy))
    server.SKILL_INDEX.rebuild()

    out = await _invoke("legacy_schema")

    assert "Parameters not validated" in out and "ran" in out


# ----------------------------------------------------------------------------
# Skill-to-skill dependencies
# ----------------------------------------------------------------------------


async def test_create_warns_about_unknown_skill_refs(storage: Storage) -> None:
    msg = await _create("orphan_caller", "__result__ = invoke_skill('does_not_exist')")
    assert "Calls unknown skill 'does_not_exist'" in msg


async def test_delete_refuses_when_other_skills_depend(storage: Storage) -> None:
    await _create("base_skill", "__result__ = 1")
    await _create("uses_base", "__result__ = invoke_skill('base_skill')")

    refused = await server.delete_skill("base_skill", confirm=True)
    forced = await server.delete_skill("base_skill", confirm=True, force=True)

    assert refused.startswith("Error: Skill 'base_skill' is called by: uses_base")
    assert "permanently deleted" in forced and "still call it" in forced
    assert not (storage.skills_dir / "base_skill.json").exists()


async def test_get_skill_lists_called_skills(storage: Storage) -> None:
    await _create("callee", "__result__ = 1")
    await _create("caller", "__result__ = invoke_skill('callee')")

    assert "Calls skills:** callee" in await server.get_skill("caller")
