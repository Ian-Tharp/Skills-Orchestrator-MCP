"""Shared test fixtures.

`skill_orchestrator_mcp.server` and `builtin_skills` create their storage
directories under ``Path.home()`` at import time. To make sure the test suite
never touches the real user data dir, HOME is redirected to a throwaway
directory *before* any test module imports the package (conftest.py is loaded
first by pytest).

Tests that exercise the MCP tools should use the ``storage`` fixture, which
points every storage global in ``server`` at a fresh ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import pytest

_TEST_HOME = Path(tempfile.mkdtemp(prefix="skill_orchestrator_test_home_"))
os.environ["HOME"] = str(_TEST_HOME)  # POSIX Path.home()
os.environ["USERPROFILE"] = str(_TEST_HOME)  # Windows Path.home()
# Always test the real sandbox path unless a test opts in to something else.
os.environ.pop("SKILL_ORCHESTRATOR_EXEC_MODE", None)

from skill_orchestrator_mcp import server  # noqa: E402  (must follow HOME redirect)
from skill_orchestrator_mcp.artifacts import ArtifactStore  # noqa: E402
from skill_orchestrator_mcp.skill_index import SkillIndex  # noqa: E402
from skill_orchestrator_mcp.usage_stats import UsageTracker  # noqa: E402


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    shutil.rmtree(_TEST_HOME, ignore_errors=True)


@dataclass
class Storage:
    root: Path
    skills_dir: Path
    state_dir: Path
    sessions_dir: Path
    artifacts_dir: Path


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Storage]:
    """Point all server storage at an isolated tmp dir for one test."""
    root = tmp_path / "orchestrator"
    s = Storage(
        root=root,
        skills_dir=root / "skills",
        state_dir=root / "state",
        sessions_dir=root / "sessions",
        artifacts_dir=root / "artifacts",
    )
    for d in (s.skills_dir, s.state_dir, s.sessions_dir, s.artifacts_dir):
        d.mkdir(parents=True)

    monkeypatch.setattr(server, "DEFAULT_SKILLS_DIR", s.skills_dir)
    monkeypatch.setattr(server, "DEFAULT_STATE_DIR", s.state_dir)
    monkeypatch.setattr(server, "DEFAULT_SESSIONS_DIR", s.sessions_dir)
    monkeypatch.setattr(server, "DEFAULT_ARTIFACTS_DIR", s.artifacts_dir)
    monkeypatch.setattr(server, "SKILL_INDEX", SkillIndex(s.skills_dir))
    monkeypatch.setattr(server, "USAGE_TRACKER", UsageTracker(s.skills_dir))
    monkeypatch.setattr(server, "ARTIFACT_STORE", ArtifactStore(s.artifacts_dir))
    # Force search_skills to re-check the directory on its next call.
    monkeypatch.setattr(server, "_last_index_refresh_check_monotonic", 0.0)
    yield s
