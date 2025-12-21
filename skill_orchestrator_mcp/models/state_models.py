"""
Pydantic models for state management operations.
"""

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import ResponseFormat, ReturnMode


class StateInput(BaseModel):
    """Input model for state operations."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

    key: str = Field(
        ...,
        description="State key (dot notation supported, e.g., 'session.user_prefs')",
        min_length=1,
        max_length=200
    )
    value: Optional[Any] = Field(
        default=None,
        description="Value to store (JSON-serializable)"
    )
    namespace: str = Field(
        default="default",
        description="Namespace for organizing state",
        min_length=1,
        max_length=50
    )


class ListStateInput(BaseModel):
    """Input model for listing state keys.

    Supports pagination and multiple response modes to manage context efficiently.
    """
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

    namespace: str = Field(
        default="default",
        description="State namespace to list",
        min_length=1,
        max_length=50
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable, 'json' for structured"
    )
    limit: int = Field(
        default=50,
        description="Maximum number of keys to return",
        ge=1,
        le=500
    )
    cursor: int = Field(
        default=0,
        description="Pagination cursor (0-based offset into the sorted result set)",
        ge=0
    )
    return_mode: ReturnMode = Field(
        default=ReturnMode.SUMMARY,
        description="Return mode: 'summary' (keys only), 'handle' (artifact ref), or 'full' (keys + values)"
    )
    include_values: bool = Field(
        default=False,
        description="Whether to include full values in the response (only applies to FULL mode)"
    )
    value_preview_length: int = Field(
        default=50,
        description="Maximum characters to show in value previews (SUMMARY mode)",
        ge=10,
        le=500
    )
