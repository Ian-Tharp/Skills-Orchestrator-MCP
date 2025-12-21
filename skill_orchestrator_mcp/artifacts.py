"""
Artifact Store

Stores large tool outputs out-of-band (on disk) and returns lightweight handles.
This supports "progressive disclosure" and reduces context pressure for MCP clients.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional


DEFAULT_MAX_ARTIFACT_BYTES = 5 * 1024 * 1024  # 5 MiB
DEFAULT_CHUNK_BYTES = 10_000  # for tool responses


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _atomic_write_json(path: Path, data: Any) -> None:
    _atomic_write_bytes(path, json.dumps(data, indent=2, default=str).encode("utf-8"))


def _generate_artifact_id() -> str:
    # Keep IDs short to avoid noisy outputs.
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    meta_path: Path
    data_path: Path

    @property
    def resource_uri(self) -> str:
        return f"artifact://{self.artifact_id}"


class ArtifactStore:
    def __init__(
        self,
        artifacts_dir: Path,
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    ) -> None:
        self._dir = artifacts_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_artifact_bytes
        self._lock = threading.Lock()

    def put_text(
        self,
        content: str,
        *,
        mime_type: str = "text/plain",
        name: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
    ) -> ArtifactRef:
        data = content.encode("utf-8", errors="replace")
        if len(data) > self._max_bytes:
            truncated = data[: self._max_bytes]
            note = (
                f"\n\n... [artifact truncated to {self._max_bytes} bytes; "
                f"{len(data) - self._max_bytes} bytes omitted]"
            ).encode("utf-8")
            data = truncated + note

        artifact_id = _generate_artifact_id()
        ref = self._ref(artifact_id)

        expires_at: Optional[str] = None
        if ttl_seconds is not None:
            expires_at = (_utc_now() + timedelta(seconds=int(ttl_seconds))).isoformat()

        meta = {
            "id": artifact_id,
            "created_at": _utc_now_iso(),
            "expires_at": expires_at,
            "mime_type": mime_type,
            "name": name,
            "size_bytes": len(data),
        }

        with self._lock:
            _atomic_write_bytes(ref.data_path, data)
            _atomic_write_json(ref.meta_path, meta)

        return ref

    def get_meta(self, artifact_id: str) -> Optional[dict]:
        ref = self._ref(artifact_id)
        if not ref.meta_path.exists():
            return None
        try:
            with ref.meta_path.open("r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return None

        if self._is_expired(meta):
            self.delete(artifact_id)
            return None
        return meta

    def get_text_chunk(
        self,
        artifact_id: str,
        *,
        start: int = 0,
        limit: int = DEFAULT_CHUNK_BYTES,
    ) -> Optional[dict]:
        meta = self.get_meta(artifact_id)
        if meta is None:
            return None

        ref = self._ref(artifact_id)
        if not ref.data_path.exists():
            return None

        start = max(0, int(start))
        limit = max(1, int(limit))

        with ref.data_path.open("rb") as f:
            f.seek(start)
            chunk = f.read(limit)
            next_start = start + len(chunk)

        try:
            text = chunk.decode("utf-8", errors="replace")
        except Exception:
            text = repr(chunk)

        size = int(meta.get("size_bytes", 0) or 0)
        done = next_start >= size

        return {
            "meta": meta,
            "start": start,
            "limit": limit,
            "next_start": None if done else next_start,
            "done": done,
            "chunk": text,
        }

    def delete(self, artifact_id: str) -> bool:
        ref = self._ref(artifact_id)
        removed_any = False
        with self._lock:
            for p in (ref.data_path, ref.meta_path):
                try:
                    if p.exists():
                        p.unlink()
                        removed_any = True
                except Exception:
                    continue
        return removed_any

    # ------------------------- internal -------------------------

    def _ref(self, artifact_id: str) -> ArtifactRef:
        safe_id = "".join(c for c in artifact_id if c.isalnum())[:64]
        return ArtifactRef(
            artifact_id=safe_id,
            meta_path=self._dir / f"{safe_id}.meta.json",
            data_path=self._dir / f"{safe_id}.data",
        )

    @staticmethod
    def _is_expired(meta: dict) -> bool:
        expires_at = meta.get("expires_at")
        if not expires_at:
            return False
        try:
            exp = datetime.fromisoformat(str(expires_at))
        except Exception:
            return False
        return _utc_now() >= exp


