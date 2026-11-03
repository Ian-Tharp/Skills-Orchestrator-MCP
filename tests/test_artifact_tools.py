"""Tests for the store_artifact / get_artifact MCP tools and artifact:// resource."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from skill_orchestrator_mcp import server
from skill_orchestrator_mcp.models import GetArtifactInput, StoreArtifactInput

from .conftest import Storage


async def test_store_then_get_artifact_chunked(storage: Storage) -> None:
    ref = json.loads(await server.store_artifact(StoreArtifactInput(content="0123456789", name="digits")))

    first = json.loads(await server.get_artifact(GetArtifactInput(artifact_id=ref["artifact_id"], limit=4)))
    rest = json.loads(
        await server.get_artifact(GetArtifactInput(artifact_id=ref["artifact_id"], start=first["next_start"], limit=100))
    )

    assert first["chunk"] == "0123" and first["done"] is False
    assert rest["chunk"] == "456789" and rest["done"] is True
    assert first["meta"]["name"] == "digits"
    assert list(storage.artifacts_dir.glob(f"{ref['artifact_id']}.*"))


async def test_get_artifact_unknown_id(storage: Storage) -> None:
    payload = json.loads(await server.get_artifact(GetArtifactInput(artifact_id="missing")))
    assert "not found" in payload["error"]


async def test_artifact_resource_preview(storage: Storage) -> None:
    ref = json.loads(await server.store_artifact(StoreArtifactInput(content="y" * 12_000)))

    payload = json.loads(server.get_artifact_resource(ref["artifact_id"]))
    missing = json.loads(server.get_artifact_resource("nope"))

    assert len(payload["preview"]) == 10_000
    assert payload["preview_truncated"] is True
    assert "error" in missing


def test_artifact_input_validation() -> None:
    with pytest.raises(ValidationError):
        StoreArtifactInput(content="")
    with pytest.raises(ValidationError):
        StoreArtifactInput(content="x", ttl_seconds=0)
    with pytest.raises(ValidationError):
        GetArtifactInput(artifact_id="a", limit=200_001)
