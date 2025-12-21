"""
Skill Index

Provides a metadata-only index over persisted skills to enable:
- Fast search/listing without reading every skill JSON file
- Progressive disclosure (load code only when needed)

The index is stored alongside skills as a dotfile so it is excluded from
`Path.glob("*.json")` patterns, avoiding accidental treatment as a skill.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


INDEX_SCHEMA_VERSION = 1
DEFAULT_INDEX_FILENAME = ".skill_index.json"


@dataclass(frozen=True)
class IndexSignature:
    """A lightweight signature to detect out-of-band directory changes."""

    skill_files_count: int
    skill_names: tuple[str, ...]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically to avoid partial/corrupt files."""

    path.parent.mkdir(parents=True, exist_ok=True)

    # Use same directory for atomic replace on Windows.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        json.dump(data, tmp, indent=2, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)

    os.replace(tmp_path, path)


def _is_skill_filename(path: Path) -> bool:
    """Return True if a filename looks like a skill file (`name.json`)."""

    # Exclude dotfiles like `.skill_index.json` and `.usage.json`
    if path.name.startswith("."):
        return False
    if path.suffix.lower() != ".json":
        return False
    return True


def _extract_skill_metadata(skill: Mapping[str, Any]) -> Dict[str, Any]:
    """Strip code and keep metadata fields only."""

    # Keep backwards/forwards compatible: only remove `code`, keep everything else.
    return {k: v for k, v in skill.items() if k != "code"}


class SkillIndex:
    """In-memory + on-disk metadata index for skills."""

    def __init__(
        self,
        skills_dir: Path,
        *,
        index_filename: str = DEFAULT_INDEX_FILENAME,
    ) -> None:
        self._skills_dir = skills_dir
        self._index_path = skills_dir / index_filename
        self._lock = threading.Lock()
        self._loaded = False
        self._skills_by_name: Dict[str, Dict[str, Any]] = {}
        self._signature: Optional[IndexSignature] = None

    @property
    def index_path(self) -> Path:
        return self._index_path

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._load_or_rebuild_locked()
            self._loaded = True

    def rebuild(self) -> None:
        """Force rebuild the index from skill JSON files."""

        with self._lock:
            self._skills_by_name = {}
            self._rebuild_from_disk_locked()
            self._persist_locked()
            self._loaded = True

    def upsert_from_skill(self, skill: Mapping[str, Any]) -> None:
        """Insert/update a skill entry in the index from full skill data."""

        name = str(skill.get("name", "")).strip()
        if not name:
            return

        with self._lock:
            self._skills_by_name[name] = _extract_skill_metadata(skill)
            self._persist_locked()
            self._loaded = True

    def remove(self, name: str) -> None:
        """Remove a skill entry from the index (if present)."""

        with self._lock:
            self._skills_by_name.pop(name, None)
            self._persist_locked()
            self._loaded = True

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            entry = self._skills_by_name.get(name)
            return dict(entry) if entry else None

    def all(self) -> Dict[str, Dict[str, Any]]:
        self.ensure_loaded()
        with self._lock:
            return {k: dict(v) for k, v in self._skills_by_name.items()}

    def maybe_refresh_from_disk(self) -> bool:
        """Refresh index if skill filenames have changed out-of-band.\n\n        Returns True if refreshed.\n        """

        self.ensure_loaded()
        new_sig = self._compute_signature()
        with self._lock:
            if self._signature == new_sig:
                return False
            # Rebuild only when filenames changed (create/delete/rename outside tools).
            self._skills_by_name = {}
            self._rebuild_from_disk_locked()
            self._persist_locked()
            return True

    # ------------------------- internal -------------------------

    def _load_or_rebuild_locked(self) -> None:
        self._skills_dir.mkdir(parents=True, exist_ok=True)

        if self._index_path.exists():
            try:
                with self._index_path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                if raw.get("schema_version") == INDEX_SCHEMA_VERSION and isinstance(
                    raw.get("skills_by_name"), dict
                ):
                    self._skills_by_name = {
                        str(k): dict(v) for k, v in raw["skills_by_name"].items()
                    }
                    self._signature = self._compute_signature()
                    return
            except Exception:
                # Corrupt/invalid index -> rebuild.
                pass

        self._rebuild_from_disk_locked()
        self._persist_locked()

    def _rebuild_from_disk_locked(self) -> None:
        skills_by_name: Dict[str, Dict[str, Any]] = {}

        for path in self._skills_dir.glob("*.json"):
            if not _is_skill_filename(path):
                continue

            try:
                with path.open("r", encoding="utf-8") as f:
                    skill = json.load(f)
                name = str(skill.get("name", "")).strip()
                if not name:
                    continue
                skills_by_name[name] = _extract_skill_metadata(skill)
            except Exception:
                # Ignore unreadable/bad skill files; keep index usable.
                continue

        self._skills_by_name = skills_by_name
        self._signature = self._compute_signature()

    def _persist_locked(self) -> None:
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "skills_by_name": self._skills_by_name,
        }
        _atomic_write_json(self._index_path, payload)

    def _compute_signature(self) -> IndexSignature:
        names: list[str] = []
        for path in self._skills_dir.glob("*.json"):
            if not _is_skill_filename(path):
                continue
            names.append(path.stem)
        names_sorted = tuple(sorted(names))
        return IndexSignature(skill_files_count=len(names_sorted), skill_names=names_sorted)


