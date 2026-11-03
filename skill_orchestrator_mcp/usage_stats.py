"""
Usage Stats

Tracks skill invocation counts without rewriting skill JSON files on every call.
This reduces write amplification and keeps skill metadata stable for indexing.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

from . import fileio

DEFAULT_FLUSH_INTERVAL_SECONDS = 10.0
DEFAULT_MAX_UPDATES_BEFORE_FLUSH = 25
DEFAULT_USAGE_FILENAME = ".skill_usage.json"  # dotfile so it won't match "*.json" globs


def _atomic_write_json(path: Path, data: Dict[str, int]) -> None:
    fileio.atomic_write_json(path, data)


@dataclass
class UsageSnapshot:
    total: int
    by_skill: Dict[str, int]


class UsageTracker:
    def __init__(
        self,
        skills_dir: Path,
        *,
        filename: str = DEFAULT_USAGE_FILENAME,
        flush_interval_seconds: float = DEFAULT_FLUSH_INTERVAL_SECONDS,
        max_updates_before_flush: int = DEFAULT_MAX_UPDATES_BEFORE_FLUSH,
    ) -> None:
        self._path = skills_dir / filename
        self._lock = threading.Lock()
        self._counts: Dict[str, int] = {}
        self._dirty = False
        self._last_flush_monotonic = time.monotonic()
        self._pending_updates = 0
        self._flush_interval_seconds = float(flush_interval_seconds)
        self._max_updates_before_flush = int(max_updates_before_flush)

        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def get(self, skill_name: str) -> int:
        with self._lock:
            return int(self._counts.get(skill_name, 0) or 0)

    def increment(self, skill_name: str, *, by: int = 1) -> int:
        with self._lock:
            self._counts[skill_name] = int(self._counts.get(skill_name, 0) or 0) + int(by)
            self._dirty = True
            self._pending_updates += 1

            self._maybe_flush_locked()
            return int(self._counts[skill_name])

    def remove(self, skill_name: str) -> None:
        with self._lock:
            if skill_name in self._counts:
                self._counts.pop(skill_name, None)
                self._dirty = True
                self._pending_updates += 1
                self._maybe_flush_locked()

    def snapshot(self) -> UsageSnapshot:
        with self._lock:
            by_skill = dict(self._counts)
        total = sum(int(v or 0) for v in by_skill.values())
        return UsageSnapshot(total=total, by_skill=by_skill)

    def ensure_minimums(self, minimums: Dict[str, int]) -> None:
        """Seed any missing counts from a baseline mapping (one-time migration helper)."""

        with self._lock:
            changed = False
            for name, count in minimums.items():
                if name not in self._counts:
                    self._counts[name] = int(count or 0)
                    changed = True

            if changed:
                self._dirty = True
                self._pending_updates += 1
                self._maybe_flush_locked(force=True)

    def flush(self) -> None:
        with self._lock:
            self._maybe_flush_locked(force=True)

    # ------------------------- internal -------------------------

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                self._counts = {str(k): int(v or 0) for k, v in raw.items()}
        except Exception:
            # Corrupt usage file -> ignore; caller can rebuild usage.
            self._counts = {}

    def _maybe_flush_locked(self, *, force: bool = False) -> None:
        if not self._dirty:
            return

        now = time.monotonic()
        interval_elapsed = now - self._last_flush_monotonic >= self._flush_interval_seconds
        updates_exceeded = self._pending_updates >= self._max_updates_before_flush

        if not force and not interval_elapsed and not updates_exceeded:
            return

        _atomic_write_json(self._path, dict(self._counts))
        self._dirty = False
        self._pending_updates = 0
        self._last_flush_monotonic = now


