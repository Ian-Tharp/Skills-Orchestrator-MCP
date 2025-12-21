"""
Pydantic models for artifact storage and retrieval operations.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class StoreArtifactInput(BaseModel):
    """Input model for storing an artifact explicitly."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    content: str = Field(..., description="Artifact content (stored as UTF-8 text)", min_length=1)
    mime_type: str = Field(
        default="text/plain",
        description="MIME type hint (e.g. text/plain, text/markdown, application/json)",
        min_length=3,
        max_length=100,
    )
    name: Optional[str] = Field(default=None, description="Optional human-friendly name")
    ttl_seconds: Optional[int] = Field(
        default=None,
        description="Optional TTL in seconds",
        ge=1,
        le=7 * 24 * 60 * 60,
    )


class GetArtifactInput(BaseModel):
    """Input model for retrieving an artifact chunk."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    artifact_id: str = Field(..., description="Artifact ID", min_length=1, max_length=64)
    start: int = Field(default=0, description="Start offset (bytes)", ge=0)
    limit: int = Field(default=10_000, description="Max bytes to return", ge=1, le=200_000)
