"""Tests for UsageTracker (invocation counters persisted in a dotfile)."""

from __future__ import annotations

import json
from pathlib import Path

from skill_orchestrator_mcp.usage_stats import UsageTracker


def _tracker(tmp_path: Path, **kwargs: float) -> UsageTracker:
    return UsageTracker(tmp_path, flush_interval_seconds=kwargs.get("interval", 3600), max_updates_before_flush=int(kwargs.get("max_updates", 1000)))


def test_increment_get_and_snapshot(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.increment("a")
    t.increment("a", by=2)
    t.increment("b")

    snap = t.snapshot()

    assert t.get("a") == 3
    assert t.get("unknown") == 0
    assert snap.total == 4 and snap.by_skill == {"a": 3, "b": 1}


def test_writes_are_batched_until_threshold(tmp_path: Path) -> None:
    t = _tracker(tmp_path, max_updates=3)
    t.increment("a")
    t.increment("a")
    assert not t.path.exists()

    t.increment("a")

    assert json.loads(t.path.read_text()) == {"a": 3}


def test_flush_persists_and_reloads(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.increment("a", by=5)
    t.flush()

    assert UsageTracker(tmp_path).get("a") == 5


def test_ensure_minimums_only_seeds_missing(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.increment("a", by=2)

    t.ensure_minimums({"a": 10, "b": 7})

    assert t.get("a") == 2
    assert t.get("b") == 7
    assert json.loads(t.path.read_text()) == {"a": 2, "b": 7}  # forced flush


def test_remove_unknown_is_noop(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    t.remove("ghost")
    t.flush()
    assert not t.path.exists()


def test_corrupt_or_wrong_type_usage_file_is_ignored(tmp_path: Path) -> None:
    (tmp_path / ".skill_usage.json").write_text("{nope")
    assert UsageTracker(tmp_path).snapshot().total == 0

    (tmp_path / ".skill_usage.json").write_text("[1, 2]")
    assert UsageTracker(tmp_path).snapshot().total == 0

    (tmp_path / ".skill_usage.json").write_text(json.dumps({"a": "not-a-number"}))
    assert UsageTracker(tmp_path).snapshot().total == 0


def test_usage_file_is_a_dotfile(tmp_path: Path) -> None:
    # Must not be picked up by the skill index's "*.json" scan.
    assert _tracker(tmp_path).path.name.startswith(".")
