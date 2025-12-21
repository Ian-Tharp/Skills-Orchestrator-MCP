"""
Pydantic models for code execution operations.
"""

from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import ReturnMode


class ExecuteCodeInput(BaseModel):
    """Input model for code execution."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

    code: str = Field(
        ...,
        description="Python code to execute",
        min_length=1,
        max_length=100000
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Variables to inject into the execution namespace"
    )
    timeout: int = Field(
        default=30,
        description="Maximum execution time in seconds (server default: 30s, max: 180s)",
        ge=1,
        le=180
    )
    capture_output: bool = Field(
        default=True,
        description="Whether to capture and return stdout/stderr"
    )
    return_mode: ReturnMode = Field(
        default=ReturnMode.SUMMARY,
        description="Return mode: 'summary' (default), 'handle', or 'full'"
    )
    artifact_ttl_seconds: Optional[int] = Field(
        default=None,
        description="Optional TTL for stored artifact outputs (seconds)",
        ge=1,
        le=7 * 24 * 60 * 60
    )


class InvokeSkillInput(BaseModel):
    """Input model for invoking a stored skill."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

    skill_name: str = Field(
        ...,
        description="Name of the skill to invoke",
        min_length=1,
        max_length=100
    )
    parameters: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Parameters to pass to the skill"
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional context variables"
    )
    timeout: int = Field(
        default=30,
        description="Maximum execution time in seconds (server default: 30s, max: 180s)",
        ge=1,
        le=180
    )
    return_mode: ReturnMode = Field(
        default=ReturnMode.SUMMARY,
        description="Return mode: 'summary' (default), 'handle', or 'full'"
    )
    artifact_ttl_seconds: Optional[int] = Field(
        default=None,
        description="Optional TTL for stored artifact outputs (seconds)",
        ge=1,
        le=7 * 24 * 60 * 60
    )
