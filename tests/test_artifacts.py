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




# ----------------------------------------------------------------------------
# Edge cases
# ----------------------------------------------------------------------------


def test_chunked_reads_cover_whole_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    content = "".join(str(i % 10) for i in range(2500))
    ref = store.put_text(content)

    pieces = []
    start: int | None = 0
    while start is not None:
        chunk = store.get_text_chunk(ref.artifact_id, start=start, limit=1000)
        assert chunk is not None
        pieces.append(chunk["chunk"])
        start = chunk["next_start"]

    assert "".join(pieces) == content
    assert len(pieces) == 3


def test_start_past_end_returns_empty_done_chunk(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("abc")

    chunk = store.get_text_chunk(ref.artifact_id, start=100)

    assert chunk is not None
    assert chunk["chunk"] == ""
    assert chunk["done"] is True and chunk["next_start"] is None


def test_negative_start_and_zero_limit_are_clamped(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("abcdef")

    chunk = store.get_text_chunk(ref.artifact_id, start=-5, limit=0)

    assert chunk is not None
    assert chunk["start"] == 0 and chunk["limit"] == 1
    assert chunk["chunk"] == "a"


def test_oversized_content_is_truncated_with_note(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path, max_artifact_bytes=100)
    ref = store.put_text("x" * 250)

    chunk = store.get_text_chunk(ref.artifact_id, limit=10_000)

    assert chunk is not None
    assert chunk["chunk"].startswith("x" * 100)
    assert "150 bytes omitted" in chunk["chunk"]
    assert "x" * 101 not in chunk["chunk"]


def test_unicode_content_round_trips(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    content = "héllo wörld ✓ 日本語"
    ref = store.put_text(content)

    chunk = store.get_text_chunk(ref.artifact_id, limit=10_000)

    assert chunk is not None and chunk["chunk"] == content
    assert chunk["meta"]["size_bytes"] == len(content.encode("utf-8"))


def test_unknown_and_malicious_ids_return_none(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    outside = tmp_path / "outside.meta.json"
    outside.write_text(json.dumps({"id": "outside"}))

    assert store.get_meta("doesnotexist") is None
    assert store.get_text_chunk("doesnotexist") is None
    # IDs are sanitised to alphanumerics, so traversal cannot reach outside files.
    assert store.get_meta("../outside") is None
    assert store.delete("../outside") is False
    assert outside.exists()


def test_corrupt_meta_returns_none(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("data")
    ref.meta_path.write_text("{not json")

    assert store.get_meta(ref.artifact_id) is None


def test_non_dict_meta_returns_none(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("data")
    ref.meta_path.write_text("[1, 2, 3]")

    assert store.get_meta(ref.artifact_id) is None


def test_missing_data_file_returns_none(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("data")
    ref.data_path.unlink()

    assert store.get_meta(ref.artifact_id) is not None
    assert store.get_text_chunk(ref.artifact_id) is None


def test_expired_artifact_files_are_removed(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("bye", ttl_seconds=60)
    meta = json.loads(ref.meta_path.read_text())
    meta["expires_at"] = "2000-01-01T00:00:00+00:00"
    ref.meta_path.write_text(json.dumps(meta))

    assert store.get_text_chunk(ref.artifact_id) is None
    assert not ref.meta_path.exists() and not ref.data_path.exists()


def test_naive_or_garbage_expiry_does_not_crash(tmp_path: Path) -> None:
    # Bug: a hand-edited/legacy timezone-naive expires_at raised TypeError
    # (can't compare offset-naive and offset-aware datetimes).
    store = ArtifactStore(tmp_path)
    naive = store.put_text("naive", ttl_seconds=60)
    garbage = store.put_text("garbage", ttl_seconds=60)
    for ref, value in ((naive, "2000-01-01T00:00:00"), (garbage, "not-a-date")):
        meta = json.loads(ref.meta_path.read_text())
        meta["expires_at"] = value
        ref.meta_path.write_text(json.dumps(meta))

    # Naive timestamps are treated as UTC (so this one is expired).
    assert store.get_meta(naive.artifact_id) is None
    # Unparseable timestamps never expire (existing behaviour).
    assert store.get_meta(garbage.artifact_id) is not None


def test_delete_twice_is_idempotent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_text("x")

    assert store.delete(ref.artifact_id) is True
    assert store.delete(ref.artifact_id) is False
