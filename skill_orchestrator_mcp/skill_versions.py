"""
Skill Version History

Every saved version of a skill (create, update, rollback) is kept as a full
snapshot under ``skills/.versions/<name>/v<N>.json``. The directory is a
dotdir, so the non-recursive ``*.json`` glob that the skill index uses never
sees it.

Rollback never rewrites history: rolling back to v2 from v5 saves the v2
content as v6. Old versions are pruned to the newest ``max_versions``.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import fileio

DEFAULT_VERSIONS_DIRNAME = ".versions"
DEFAULT_MAX_VERSIONS = 50

# Fields that define a skill's behavior/content (what rollback restores and diff compares).
CONTENT_FIELDS = (
    "code",
    "description",
    "category",
    "tags",
    "dependencies",
    "parameters_schema",
    "examples",
    "tests",
)

_VERSION_FILE_RE = re.compile(r"^v(\d+)\.json$")


def _is_safe_name(name: str) -> bool:
    return (
        bool(name)
        and not name.startswith(".")
        and not any(c in name for c in ("/", "\\", ":", "\x00"))
    )


def _as_version(raw: Any) -> int:
    try:
        v = int(float(raw))
    except (TypeError, ValueError, OverflowError):
        return 1
    return v if v >= 1 else 1


def code_digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]


def _atomic_write_json(path: Path, data: Any) -> None:
    fileio.atomic_write_json(path, data)


class SkillVersionStore:
    def __init__(
        self,
        skills_dir: Path,
        *,
        dirname: str = DEFAULT_VERSIONS_DIRNAME,
        max_versions: int = DEFAULT_MAX_VERSIONS,
    ) -> None:
        self._root = skills_dir / dirname
        self._max_versions = max(1, int(max_versions))
        self._lock = threading.Lock()

    @property
    def root(self) -> Path:
        return self._root

    def _dir(self, name: str) -> Path:
        # `name` becomes a directory name: never let it escape the versions root.
        if not _is_safe_name(name):
            raise ValueError(f"Invalid skill name: {name!r}")
        return self._root / name

    def save(self, name: str, skill: Mapping[str, Any]) -> None:
        """Snapshot a skill at its current ``version``, then prune old snapshots.

        `name` is the validated skill name the caller looked up, not the
        (possibly hand-edited) ``name`` field inside the file.
        """
        version = _as_version(skill.get("version", 1))
        with self._lock:
            _atomic_write_json(self._dir(name) / f"v{version}.json", {**skill, "name": name})
            self._prune_locked(name)

    def ensure_snapshot(self, name: str, skill: Mapping[str, Any]) -> None:
        """Snapshot a skill only if its current version has no snapshot yet.

        Skills created before version history existed have none; this captures
        their current content before the first edit so it can be rolled back to.
        """
        version = _as_version(skill.get("version", 1))
        if not (self._dir(name) / f"v{version}.json").exists():
            self.save(name, skill)

    def versions(self, name: str) -> List[int]:
        if not _is_safe_name(name):
            return []
        d = self._dir(name)
        if not d.is_dir():
            return []
        out = []
        for p in d.iterdir():
            m = _VERSION_FILE_RE.match(p.name)
            if m:
                out.append(int(m.group(1)))
        return sorted(out)

    def get(self, name: str, version: int) -> Optional[Dict[str, Any]]:
        if not _is_safe_name(name):
            return None
        path = self._dir(name) / f"v{int(version)}.json"
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def history(self, name: str) -> List[Dict[str, Any]]:
        """Metadata for each stored version, oldest first (no code)."""
        out = []
        for v in self.versions(name):
            snap = self.get(name, v)
            if snap is None:
                continue
            out.append(
                {
                    "version": v,
                    "updated_at": snap.get("updated_at"),
                    "code_sha": code_digest(str(snap.get("code", ""))),
                    "code_lines": len(str(snap.get("code", "")).splitlines()),
                    "change_note": snap.get("change_note"),
                    "rolled_back_from": snap.get("rolled_back_from"),
                }
            )
        return out

    def remove_all(self, name: str) -> None:
        if not _is_safe_name(name):
            return
        with self._lock:
            shutil.rmtree(self._dir(name), ignore_errors=True)

    def _prune_locked(self, name: str) -> None:
        versions = self.versions(name)
        for v in versions[: max(0, len(versions) - self._max_versions)]:
            try:
                (self._dir(name) / f"v{v}.json").unlink()
            except OSError:
                pass


def diff_skills(old: Mapping[str, Any], new: Mapping[str, Any], *, old_label: str, new_label: str) -> str:
    """Human-readable diff of two skill snapshots: metadata changes + unified code diff."""
    lines: List[str] = []
    for f in CONTENT_FIELDS:
        if f == "code":
            continue
        a, b = old.get(f), new.get(f)
        if a != b:
            lines.append(
                f"- **{f}:** `{json.dumps(a, default=str)}` → `{json.dumps(b, default=str)}`"
            )
    code_diff = list(
        difflib.unified_diff(
            str(old.get("code", "")).splitlines(),
            str(new.get("code", "")).splitlines(),
            fromfile=old_label,
            tofile=new_label,
            lineterm="",
        )
    )
    if code_diff:
        lines.append("\n```diff\n" + "\n".join(code_diff) + "\n```")
    if not lines:
        return "*No differences.*"
    return "\n".join(lines)
