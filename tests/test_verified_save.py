"""Verified-save gate (create/update/verify_skill) and telemetry-driven lifecycle."""

from __future__ import annotations

import json
from typing import Any

import pytest

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    InvokeSkillInput,
    ResponseFormat,
    ReturnMode,
    SearchSkillsInput,
    SkillTestCase,
)

from .conftest import Storage

DOUBLE = "__result__ = {'y': params['x'] * 2}"


def _create(name: str = "doubler", **overrides: Any) -> CreateSkillInput:
    fields: dict[str, Any] = {
        "name": name,
        "description": "Doubles params['x'] for tests",
        "code": DOUBLE,
    }
    fields.update(overrides)
    return CreateSkillInput(**fields)


def _disk(storage: Storage, name: str) -> dict:
    return json.loads((storage.skills_dir / f"{name}.json").read_text())


async def _search_item(name: str) -> dict:
    out = await server.search_skills(
        SearchSkillsInput(query=name, response_format=ResponseFormat.JSON)
    )
    items = {i["name"]: i for i in json.loads(out)["items"]}
    return items[name]


# ----------------------------------------------------------------------------
# create_skill
# ----------------------------------------------------------------------------


async def test_passing_tests_save_as_verified(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 2}, expected_result={"y": 4})])
    )

    assert "Skill created successfully" in msg
    assert "verified (1/1 test case(s) passed)" in msg
    disk = _disk(storage, "doubler")
    assert disk["status"] == "verified"
    assert disk["verification"]["passed"] == 1
    assert disk["tests"] == [{"parameters": {"x": 2}, "expected_result": {"y": 4}}]
    assert (await _search_item("doubler"))["status"] == "verified"


async def test_failing_test_rejects_save_and_shows_failure(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(tests=[SkillTestCase(name="missing x", parameters={})])
    )

    assert msg.startswith("Error: Skill 'doubler' was not saved")
    assert "missing x" in msg and "KeyError" in msg
    assert not (storage.skills_dir / "doubler.json").exists()
    assert server.SKILL_INDEX.get("doubler") is None
    assert server.SKILL_VERSIONS.versions("doubler") == []


async def test_wrong_expected_result_rejects_save(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 2}, expected_result={"y": 5})])
    )

    assert "was not saved" in msg
    assert 'expected result {"y": 5}, got {"y": 4}' in msg


async def test_expected_none_is_checked_when_given(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 1}, expected_result=None)])
    )

    assert "was not saved" in msg


async def test_save_as_draft_keeps_failing_skill(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(tests=[SkillTestCase(parameters={})], save_as_draft=True)
    )

    assert "Skill created successfully" in msg
    assert "Saved as a **draft**" in msg and "KeyError" in msg
    disk = _disk(storage, "doubler")
    assert disk["status"] == "draft"
    assert disk["verification"]["failures"][0]["case"] == "test 1"

    shown = await server.get_skill("doubler")
    assert "**Status:** draft (0/1 test case(s) passed" in shown

    run = await server.invoke_skill(
        InvokeSkillInput(skill_name="doubler", parameters={"x": 3}, return_mode=ReturnMode.FULL)
    )
    assert "This skill is a draft" in run and '"y": 6' in run


async def test_json_examples_are_run(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(
            code="__result__ = 10 / params['x']",
            examples=["Divide ten by x", '{"x": 0}'],
        )
    )

    assert "was not saved" in msg and "ZeroDivisionError" in msg
    assert "example 2" in msg


async def test_no_runnable_cases_saves_unverified(storage: Storage) -> None:
    msg = await server.create_skill(_create(examples=["double a number"]))

    assert "Skill created successfully" in msg
    assert "unverified (no runnable test cases" in msg
    assert _disk(storage, "doubler")["status"] == "unverified"


async def test_test_parameters_must_match_schema(storage: Storage) -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": ["x"],
    }
    msg = await server.create_skill(
        _create(parameters_schema=schema, tests=[SkillTestCase(parameters={"x": "two"})])
    )

    assert "was not saved" in msg and "invalid parameters" in msg


async def test_schema_defaults_apply_to_tests(storage: Storage) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer", "default": 5}}}

    msg = await server.create_skill(
        _create(parameters_schema=schema, tests=[SkillTestCase(expected_result={"y": 10})])
    )

    assert "verified (1/1" in msg


async def test_test_timeout_fails_case(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(code="time.sleep(5)\n__result__ = 1", tests=[SkillTestCase(timeout=1)])
    )

    assert "was not saved" in msg and "Timed out after 1s" in msg


async def test_verification_does_not_commit_state_or_record_telemetry(storage: Storage) -> None:
    code = "set_state('touched', True, namespace='verify_ns')\n" + DOUBLE

    msg = await server.create_skill(
        _create(code=code, tests=[SkillTestCase(parameters={"x": 1})])
    )

    assert "verified" in msg
    assert not (storage.state_dir / "verify_ns.json").exists()
    assert server.TELEMETRY.records() == []


async def test_unmet_dependency_skips_tests_as_unverified(storage: Storage) -> None:
    msg = await server.create_skill(
        _create(
            dependencies=["surely-not-installed-pkg-xyz"],
            tests=[SkillTestCase(parameters={"x": 1})],
        )
    )

    assert "Skill created successfully" in msg
    assert "tests not run, unmet dependencies" in msg
    assert _disk(storage, "doubler")["status"] == "unverified"


async def test_candidate_can_compose_with_saved_skills(storage: Storage) -> None:
    await server.create_skill(_create())
    msg = await server.create_skill(
        _create(
            name="quadrupler",
            code="__result__ = {'z': invoke_skill('doubler', {'x': params['x']})['y'] * 2}",
            tests=[SkillTestCase(parameters={"x": 3}, expected_result={"z": 12})],
        )
    )

    assert "verified (1/1" in msg


async def test_require_verified_rejects_untested_skills(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "REQUIRE_VERIFIED_SKILLS", True)

    rejected = await server.create_skill(_create())
    drafted = await server.create_skill(_create(save_as_draft=True))

    assert "only accepts verified skills" in rejected
    assert "Skill created successfully" in drafted
    assert _disk(storage, "doubler")["status"] == "draft"


# ----------------------------------------------------------------------------
# update_skill / verify_skill / rollback
# ----------------------------------------------------------------------------


async def test_update_that_breaks_tests_is_rejected(storage: Storage) -> None:
    await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 2}, expected_result={"y": 4})])
    )

    msg = await server.update_skill("doubler", code="__result__ = {'y': params['x'] * 3}")

    assert msg.startswith("Error: Skill 'doubler' was not updated")
    assert _disk(storage, "doubler")["version"] == 1
    assert server.SKILL_VERSIONS.versions("doubler") == [1]


async def test_update_with_passing_tests_saves_new_version(storage: Storage) -> None:
    await server.create_skill(_create())

    msg = await server.update_skill(
        "doubler",
        code="__result__ = {'y': params['x'] + params['x']}",
        tests=[SkillTestCase(parameters={"x": 4}, expected_result={"y": 8})],
    )

    assert "updated to version 2" in msg and "verified (1/1" in msg
    disk = _disk(storage, "doubler")
    assert disk["status"] == "verified" and disk["tests"][0]["parameters"] == {"x": 4}


async def test_update_accepts_plain_dict_tests(storage: Storage) -> None:
    """Tests arrive as dicts when called through MCP; they're validated the same way."""
    await server.create_skill(_create())

    msg = await server.update_skill(
        "doubler", tests=[{"parameters": {"x": 1}, "expected_result": {"y": 2}}]  # type: ignore[list-item]
    )
    bad = await server.update_skill("doubler", tests=[{"bogus": 1}])  # type: ignore[list-item]

    assert "verified (1/1" in msg
    assert bad.startswith("Error: Invalid test case")


async def test_metadata_only_update_skips_verification(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 1}, expected_result={"y": 2})])
    )
    calls = 0
    real = server._verify_skill

    async def counting(skill: dict) -> Any:
        nonlocal calls
        calls += 1
        return await real(skill)

    monkeypatch.setattr(server, "_verify_skill", counting)

    msg = await server.update_skill("doubler", description="Doubles x, now with docs")

    assert "updated to version 2" in msg and calls == 0
    assert _disk(storage, "doubler")["status"] == "verified"


async def test_update_save_as_draft(storage: Storage) -> None:
    await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 2}, expected_result={"y": 4})])
    )

    msg = await server.update_skill("doubler", code="raise RuntimeError('wip')", save_as_draft=True)

    assert "updated to version 2" in msg and "RuntimeError: wip" in msg
    assert _disk(storage, "doubler")["status"] == "draft"


async def test_verify_skill_promotes_draft_once_fixed(storage: Storage) -> None:
    await server.create_skill(
        _create(
            name="caller",
            code="__result__ = invoke_skill('doubler', {'x': 1})",
            tests=[SkillTestCase(expected_result={"y": 2})],
            save_as_draft=True,
        )
    )
    assert _disk(storage, "caller")["status"] == "draft"
    await server.create_skill(_create())

    out = await server.verify_skill("caller")

    assert "verified (1/1" in out and "| pass |" in out
    disk = _disk(storage, "caller")
    assert disk["status"] == "verified" and disk["version"] == 1
    assert server.SKILL_VERSIONS.get("caller", 1)["status"] == "verified"  # type: ignore[index]


async def test_rollback_restores_status_of_target_version(storage: Storage) -> None:
    await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 2}, expected_result={"y": 4})])
    )
    await server.update_skill("doubler", code="raise RuntimeError('wip')", save_as_draft=True)

    await server.rollback_skill("doubler", version=1)

    disk = _disk(storage, "doubler")
    assert disk["version"] == 3 and disk["status"] == "verified"


# ----------------------------------------------------------------------------
# Telemetry-driven lifecycle: degraded
# ----------------------------------------------------------------------------


async def _invoke(name: str, **parameters: Any) -> str:
    return await server.invoke_skill(
        InvokeSkillInput(skill_name=name, parameters=parameters, return_mode=ReturnMode.FULL)
    )


async def test_recent_failures_mark_skill_degraded(storage: Storage) -> None:
    await server.create_skill(_create())
    await _invoke("doubler", x=1)
    for _ in range(4):
        await _invoke("doubler")  # KeyError

    item = await _search_item("doubler")
    shown = await server.get_skill("doubler")
    status = await server.get_orchestrator_status()
    markdown = await server.search_skills(SearchSkillsInput(query="doubler"))

    assert item["degraded"] is True
    assert item["recent_runs"] == 5 and item["recent_failures"] == 4
    assert "degraded: 4 of the last 5 runs of v1 failed" in shown
    assert "**Degraded Skills:** doubler" in status
    assert "**Health:** ⚠️ degraded" in markdown
    # Never deleted automatically.
    assert (storage.skills_dir / "doubler.json").exists()


async def test_too_few_runs_not_degraded(storage: Storage) -> None:
    await server.create_skill(_create())
    for _ in range(4):
        await _invoke("doubler")

    item = await _search_item("doubler")

    assert item["degraded"] is False and item["recent_failures"] == 4


async def test_new_version_starts_healthy(storage: Storage) -> None:
    await server.create_skill(_create())
    for _ in range(5):
        await _invoke("doubler")
    assert (await _search_item("doubler"))["degraded"] is True

    await server.update_skill("doubler", code="__result__ = {'y': params.get('x', 0) * 2}")

    item = await _search_item("doubler")
    assert item["degraded"] is False and item["recent_runs"] == 0


async def test_degraded_threshold_is_configurable(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "DEGRADED_FAILURE_RATE", 0.9)
    monkeypatch.setattr(server, "DEGRADED_MIN_RUNS", 2)
    await server.create_skill(_create())
    await _invoke("doubler", x=1)
    for _ in range(4):
        await _invoke("doubler")

    assert (await _search_item("doubler"))["degraded"] is False  # 80% <= 90%


async def test_status_reports_sandbox_and_lifecycle(storage: Storage) -> None:
    await server.create_skill(
        _create(tests=[SkillTestCase(parameters={"x": 1})])
    )

    status = await server.get_orchestrator_status()

    assert "**Verified Skills:** 1 of 1" in status
    assert "**Child environment:** allowlist only (PATH" in status
