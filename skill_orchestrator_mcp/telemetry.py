"""
Execution Telemetry

Records one line per execution (top-level `invoke_skill`, nested composition
calls, and `execute_code`) to an append-only JSONL log, and computes
aggregates (success rate, latency percentiles, last error) on demand.

Design notes:
- Local only. Nothing leaves the machine.
- The log is capped: once it grows past ``max_records * 1.25`` lines it is
  compacted to the newest ``max_records``. Appends stay O(1).
- Every record carries the skill version that ran, so stats can be split by
  version. That is what makes self-edits observable: after an
  `update_skill`, compare the new version's error rate with the old one and
  `rollback_skill` if it got worse.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

DEFAULT_TELEMETRY_FILENAME = "executions.jsonl"
DEFAULT_MAX_RECORDS = 10_000
MAX_ERROR_CHARS = 500

# Execution statuses.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_TIMEOUT = "timeout"
# Refused before running: missing dependencies or parameters that fail the schema.
STATUS_REJECTED = "rejected"

KIND_SKILL = "skill"
KIND_NESTED = "nested_skill"
KIND_CODE = "execute_code"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate(text: Optional[str], limit: int = MAX_ERROR_CHARS) -> Optional[str]:
    if text is None:
        return None
    # One line, so errors render cleanly in markdown tables and log greps.
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _percentile(sorted_values: List[float], pct: float) -> Optional[float]:
    """Nearest-rank percentile on an already sorted list."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


@dataclass
class _Bucket:
    runs: int = 0
    ok: int = 0
    error: int = 0
    timeout: int = 0
    rejected: int = 0
    durations: List[float] = field(default_factory=list)
    last_run_at: Optional[str] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None

    def add(self, rec: Dict[str, Any]) -> None:
        self.runs += 1
        status = rec.get("status")
        if status == STATUS_OK:
            self.ok += 1
        elif status == STATUS_TIMEOUT:
            self.timeout += 1
        elif status == STATUS_REJECTED:
            self.rejected += 1
        else:
            self.error += 1
        duration = rec.get("duration_ms")
        if isinstance(duration, (int, float)) and status != STATUS_REJECTED:
            self.durations.append(float(duration))
        ts = rec.get("ts")
        if isinstance(ts, str):
            self.last_run_at = ts
            if status != STATUS_OK:
                self.last_error = rec.get("error") or status
                self.last_error_at = ts

    def summary(self) -> Dict[str, Any]:
        durations = sorted(self.durations)
        failed = self.error + self.timeout
        executed = self.runs - self.rejected
        return {
            "runs": self.runs,
            "ok": self.ok,
            "error": self.error,
            "timeout": self.timeout,
            "rejected": self.rejected,
            "success_rate": round(self.ok / executed, 4) if executed else None,
            "failure_rate": round(failed / executed, 4) if executed else None,
            "avg_ms": round(sum(durations) / len(durations), 1) if durations else None,
            "p50_ms": _percentile(durations, 50),
            "p95_ms": _percentile(durations, 95),
            "max_ms": durations[-1] if durations else None,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
        }


class ExecutionTelemetry:
    """Thread-safe append-only execution log with on-demand aggregates."""

    def __init__(
        self,
        telemetry_dir: Path,
        *,
        filename: str = DEFAULT_TELEMETRY_FILENAME,
        max_records: int = DEFAULT_MAX_RECORDS,
        enabled: bool = True,
    ) -> None:
        self._path = telemetry_dir / filename
        self._lock = threading.Lock()
        self._max_records = max(1, int(max_records))
        self._enabled = enabled
        self._line_count: Optional[int] = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------ write

    def record(
        self,
        *,
        kind: str,
        status: str,
        duration_ms: float,
        skill_name: Optional[str] = None,
        skill_version: Optional[int] = None,
        error: Optional[str] = None,
        parent: Optional[str] = None,
        depth: Optional[int] = None,
        exec_ms: Optional[float] = None,
        state_writes: Optional[int] = None,
        ts: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Append one execution record. Never raises: telemetry must not break runs."""
        if not self._enabled:
            return None
        rec: Dict[str, Any] = {
            "ts": ts or utc_now_iso(),
            "kind": kind,
            "skill": skill_name,
            "version": skill_version,
            "status": status,
            "duration_ms": round(float(duration_ms), 1),
        }
        if exec_ms is not None:
            rec["exec_ms"] = round(float(exec_ms), 1)
        if error:
            rec["error"] = _truncate(error)
        if parent is not None:
            rec["parent"] = parent
        if depth is not None:
            rec["depth"] = depth
        if state_writes:
            rec["state_writes"] = state_writes
        try:
            self._append([rec])
        except Exception:
            return None
        return rec

    def record_many(self, records: Iterable[Dict[str, Any]]) -> None:
        if not self._enabled:
            return
        recs = list(records)
        if not recs:
            return
        try:
            self._append(recs)
        except Exception:
            pass

    def clear(self, skill_name: Optional[str] = None) -> int:
        """Drop records (all, or for one skill). Returns the number removed."""
        with self._lock:
            records = self._read_locked()
            if skill_name is None:
                kept: List[Dict[str, Any]] = []
            else:
                kept = [r for r in records if r.get("skill") != skill_name]
            self._rewrite_locked(kept)
            return len(records) - len(kept)

    # ------------------------------------------------------------------- read

    def records(
        self,
        *,
        skill_name: Optional[str] = None,
        kind: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return matching records, oldest first."""
        with self._lock:
            records = self._read_locked()
        out = []
        for r in records:
            if skill_name is not None and r.get("skill") != skill_name:
                continue
            if kind is not None and r.get("kind") != kind:
                continue
            if status is not None and r.get("status") != status:
                continue
            out.append(r)
        return out

    def recent(self, *, skill_name: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """Newest-first slice of records."""
        recs = self.records(skill_name=skill_name)
        return list(reversed(recs[-limit:])) if limit > 0 else []

    def skill_stats(self, skill_name: str) -> Dict[str, Any]:
        """Aggregate stats for one skill, overall and per version."""
        recs = self.records(skill_name=skill_name)
        overall = _Bucket()
        by_version: Dict[str, _Bucket] = {}
        top_level = 0
        nested = 0
        for r in recs:
            overall.add(r)
            key = str(r.get("version")) if r.get("version") is not None else "unknown"
            by_version.setdefault(key, _Bucket()).add(r)
            if r.get("kind") == KIND_NESTED:
                nested += 1
            else:
                top_level += 1
        return {
            "skill": skill_name,
            **overall.summary(),
            "top_level_runs": top_level,
            "nested_runs": nested,
            "by_version": {
                v: b.summary()
                for v, b in sorted(by_version.items(), key=lambda kv: _version_sort_key(kv[0]))
            },
        }

    def all_stats(self) -> Dict[str, Dict[str, Any]]:
        """Overall summary per skill (``execute_code`` runs under '(execute_code)')."""
        recs = self.records()
        buckets: Dict[str, _Bucket] = {}
        for r in recs:
            name = r.get("skill") or f"({r.get('kind', 'unknown')})"
            buckets.setdefault(str(name), _Bucket()).add(r)
        return {name: b.summary() for name, b in buckets.items()}

    # --------------------------------------------------------------- internal

    def _read_locked(self) -> List[Dict[str, Any]]:
        if not self._path.exists():
            self._line_count = 0
            return []
        out: List[Dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue  # skip a torn/corrupt line instead of failing the read
                if isinstance(rec, dict):
                    out.append(rec)
        self._line_count = len(out)
        return out

    def _append(self, recs: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._line_count is None:
                self._read_locked()
            with self._path.open("a", encoding="utf-8") as f:
                for rec in recs:
                    f.write(json.dumps(rec, default=str) + "\n")
            self._line_count = (self._line_count or 0) + len(recs)
            if self._line_count > int(self._max_records * 1.25):
                self._rewrite_locked(self._read_locked()[-self._max_records :])

    def _rewrite_locked(self, records: List[Dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            delete=False,
            dir=str(self._path.parent),
            prefix=self._path.name + ".",
            suffix=".tmp",
        ) as tmp:
            for rec in records:
                tmp.write(json.dumps(rec, default=str) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self._path)
        self._line_count = len(records)


def _version_sort_key(v: str) -> tuple[int, int]:
    try:
        return (0, int(v))
    except ValueError:
        return (1, 0)
