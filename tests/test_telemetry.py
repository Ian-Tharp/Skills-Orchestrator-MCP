"""Tests for execution telemetry: the log itself and what the server records."""

from __future__ import annotations

import json
from pathlib import Path

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ExecuteCodeInput,
    InvokeSkillInput,
    ResponseFormat,
    ReturnMode,
)
from skill_orchestrator_mcp.telemetry import ExecutionTelemetry

from .conftest import Storage

# ----------------------------------------------------------------------------
# ExecutionTelemetry unit tests
# ----------------------------------------------------------------------------


def _rec(t: ExecutionTelemetry, status: str, ms: float, version: int = 1, skill: str = "s") -> None:
    t.record(kind="skill", status=status, duration_ms=ms, skill_name=skill, skill_version=version)


def test_stats_aggregate_status_counts_and_latency(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    for ms in (10, 20, 30, 40):
        _rec(t, "ok", ms)
    _rec(t, "error", 50)
    _rec(t, "timeout", 1000)
    t.record(kind="skill", status="rejected", duration_ms=0, skill_name="s", skill_version=1, error="bad")

    s = t.skill_stats("s")

    assert (s["runs"], s["ok"], s["error"], s["timeout"], s["rejected"]) == (7, 4, 1, 1, 1)
    # Rejected runs never executed, so they don't count toward the success rate.
    assert s["success_rate"] == round(4 / 6, 4)
    assert s["failure_rate"] == round(2 / 6, 4)
    assert s["p50_ms"] == 30.0
    assert s["max_ms"] == 1000.0
    assert s["last_error"] == "bad"


def test_stats_split_by_version(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    _rec(t, "ok", 5, version=1)
    _rec(t, "error", 5, version=2)
    _rec(t, "error", 5, version=2)

    by_version = t.skill_stats("s")["by_version"]

    assert list(by_version) == ["1", "2"]
    assert by_version["1"]["success_rate"] == 1.0
    assert by_version["2"]["success_rate"] == 0.0


def test_log_is_compacted_to_max_records(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path, max_records=10)
    for i in range(40):
        _rec(t, "ok", i)

    lines = t.path.read_text().splitlines()

    assert len(lines) <= 13  # compaction triggers past max_records * 1.25
    assert json.loads(lines[-1])["duration_ms"] == 39.0


def test_corrupt_line_is_skipped(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    _rec(t, "ok", 1)
    with t.path.open("a") as f:
        f.write("{not json\n")
    _rec(t, "ok", 2)

    assert t.skill_stats("s")["runs"] == 2


def test_errors_are_stored_on_one_line(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    t.record(kind="skill", status="error", duration_ms=1, skill_name="s", error="line one\n- line two")
    assert t.records()[0]["error"] == "line one - line two"


def test_disabled_telemetry_writes_nothing(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path, enabled=False)
    _rec(t, "ok", 1)
    assert not t.path.exists()


def test_clear_one_skill(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    _rec(t, "ok", 1, skill="a")
    _rec(t, "ok", 1, skill="b")

    assert t.clear("a") == 1
    assert t.skill_stats("a")["runs"] == 0
    assert t.skill_stats("b")["runs"] == 1


def test_recent_is_newest_first(tmp_path: Path) -> None:
    t = ExecutionTelemetry(tmp_path)
    for ms in (1, 2, 3):
        _rec(t, "ok", ms)
    assert [r["duration_ms"] for r in t.recent(skill_name="s", limit=2)] == [3.0, 2.0]


def test_recent_breaks_timestamp_ties_by_file_order(tmp_path: Path) -> None:
    # Windows clocks are coarse enough that consecutive runs share a timestamp.
    t = ExecutionTelemetry(tmp_path)
    for ms in (1, 2, 3):
        t.record(kind="skill", status="ok", duration_ms=ms, skill_name="s", ts="2026-01-01T00:00:00+00:00")
    assert [r["duration_ms"] for r in t.recent(skill_name="s", limit=2)] == [3.0, 2.0]


# ----------------------------------------------------------------------------
# What the server records
# ----------------------------------------------------------------------------


async def _create(name: str, code: str) -> None:
    msg = await server.create_skill(
        CreateSkillInput(name=name, description="telemetry test skill", code=code)
    )
    assert "created successfully" in msg, msg


async def _invoke(name: str, **kw: object) -> str:
    return await server.invoke_skill(
        InvokeSkillInput(skill_name=name, return_mode=ReturnMode.FULL, **kw)  # type: ignore[arg-type]
    )


async def test_invoke_records_ok_and_error(storage: Storage) -> None:
    await _create("flaky", "if params.get('fail'):\n    raise ValueError('boom')\n__result__ = 1")

    await _invoke("flaky")
    await _invoke("flaky", parameters={"fail": True})

    recs = server.TELEMETRY.records(skill_name="flaky")
    assert [r["status"] for r in recs] == ["ok", "error"]
    assert recs[1]["error"] == "ValueError: boom"
    assert recs[0]["version"] == 1
    assert recs[0]["duration_ms"] > 0
    assert "exec_ms" in recs[0]


async def test_invoke_records_timeout(storage: Storage) -> None:
    await _create("sleepy", "import time\ntime.sleep(10)")

    await _invoke("sleepy", timeout=1)

    (rec,) = server.TELEMETRY.records(skill_name="sleepy")
    assert rec["status"] == "timeout"


async def test_nested_calls_are_recorded_with_parent(storage: Storage) -> None:
    await _create("leaf", "__result__ = params['x'] * 2")
    await _create("root", "__result__ = invoke_skill('leaf', {'x': 2}) + invoke_skill('leaf', {'x': 3})")

    out = await _invoke("root")

    assert "10" in out
    leaf = server.TELEMETRY.records(skill_name="leaf")
    assert len(leaf) == 2
    assert all(r["kind"] == "nested_skill" and r["parent"] == "root" and r["depth"] == 2 for r in leaf)
    stats = server.TELEMETRY.skill_stats("leaf")
    assert stats["nested_runs"] == 2 and stats["top_level_runs"] == 0


async def test_nested_failure_caught_by_parent_is_recorded(storage: Storage) -> None:
    await _create("bad_leaf", "raise RuntimeError('leaf broke')")
    await _create(
        "tolerant_root",
        "try:\n    invoke_skill('bad_leaf')\nexcept RuntimeError:\n    pass\n__result__ = 'ok'",
    )

    await _invoke("tolerant_root")

    (leaf,) = server.TELEMETRY.records(skill_name="bad_leaf")
    assert leaf["status"] == "error"
    assert "leaf broke" in leaf["error"]
    (root,) = server.TELEMETRY.records(skill_name="tolerant_root")
    assert root["status"] == "ok"


async def test_execute_code_is_recorded(storage: Storage) -> None:
    await server.execute_code(ExecuteCodeInput(code="__result__ = 1"))

    (rec,) = server.TELEMETRY.records(kind="execute_code")
    assert rec["status"] == "ok" and rec["skill"] is None


async def test_get_skill_stats_reports(storage: Storage) -> None:
    await _create("measured", "__result__ = 1")
    await _invoke("measured")
    await server.execute_code(ExecuteCodeInput(code="raise KeyError('k')"))

    overview = await server.get_skill_stats()
    detail = await server.get_skill_stats("measured")
    as_json = json.loads(await server.get_skill_stats("measured", response_format=ResponseFormat.JSON))

    assert "measured" in overview and "(execute_code)" in overview
    assert "KeyError" in overview
    assert "### By version" in detail and "| v1 | 1 |" in detail
    assert as_json["runs"] == 1 and len(as_json["recent"]) == 1


async def test_get_skill_stats_unknown_skill(storage: Storage) -> None:
    assert "No executions recorded" in await server.get_skill_stats("nobody")


async def test_status_shows_telemetry_and_real_version(storage: Storage) -> None:
    await _create("will_fail", "raise ValueError('x')")
    await _invoke("will_fail")

    status = await server.get_orchestrator_status()

    assert f"v{server.SERVER_VERSION}" in status
    assert "Recorded Executions:** 1" in status
    assert "Skills Whose Last Run Failed:** will_fail" in status
