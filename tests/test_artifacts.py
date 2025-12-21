from __future__ import annotations

import json
from pathlib import Path

from skill_orchestrator_mcp.artifacts import ArtifactStore


def test_artifact_store_put_and_get_chunk(tmp_path: Path) -> None:
    # Arrange
    artifacts_dir = tmp_path / "artifacts"
    store = ArtifactStore(artifacts_dir)
    content = "hello\n" * 1000

    # Act
    ref = store.put_text(content, mime_type="text/plain", name="unit-test")
    chunk = store.get_text_chunk(ref.artifact_id, start=0, limit=100)

    # Assert
    assert chunk is not None
    assert chunk["meta"]["id"] == ref.artifact_id
    assert chunk["meta"]["mime_type"] == "text/plain"
    assert isinstance(chunk["chunk"], str)
    assert len(chunk["chunk"]) > 0


def test_artifact_store_delete(tmp_path: Path) -> None:
    # Arrange
    artifacts_dir = tmp_path / "artifacts"
    store = ArtifactStore(artifacts_dir)
    ref = store.put_text("data", mime_type="text/plain")

    # Act
    removed = store.delete(ref.artifact_id)
    missing = store.get_meta(ref.artifact_id)

    # Assert
    assert removed is True
    assert missing is None


def test_artifact_store_ttl_expiration_is_enforced(tmp_path: Path) -> None:
    # Arrange
    artifacts_dir = tmp_path / "artifacts"
    store = ArtifactStore(artifacts_dir)
    ref = store.put_text("secret", ttl_seconds=60)

    # Force expiry by editing meta (avoid sleeping in unit tests)
    meta_path = artifacts_dir / f"{ref.artifact_id}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["expires_at"] = "1970-01-01T00:00:00+00:00"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    # Act
    expired_meta = store.get_meta(ref.artifact_id)

    # Assert
    assert expired_meta is None


