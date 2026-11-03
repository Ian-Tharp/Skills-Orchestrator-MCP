"""
Pydantic models for skill management operations.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from .common import ResponseFormat, SkillCategory


class SkillTestCase(BaseModel):
    """One sandboxed check run before a skill is saved (the verified-save gate)."""
    model_config = ConfigDict(extra='forbid')

    name: Optional[str] = Field(
        default=None,
        description="Optional label shown in verification results",
        max_length=100,
    )
    parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Parameters passed to the skill (validated against parameters_schema)",
    )
    context: Dict[str, Any] = Field(
        default_factory=dict,
        description="Context variables passed to the skill",
    )
    expected_result: Any = Field(
        default=None,
        description=(
            "If set, the skill's __result__ must equal this (compared as JSON). "
            "If omitted, the case passes when the run finishes without error."
        ),
    )
    timeout: Optional[int] = Field(
        default=None,
        description="Seconds allowed for this case (default: the server's default timeout)",
        ge=1,
    )


class CreateSkillInput(BaseModel):
    """Input model for creating a new skill."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra='forbid'
    )

    name: str = Field(
        ...,
        description="Unique identifier for the skill (snake_case, e.g., 'process_csv_data')",
        min_length=1,
        max_length=100,
        pattern=r'^[a-z][a-z0-9_]*$'
    )
    description: str = Field(
        ...,
        description="Clear description of what the skill does and when to use it",
        min_length=10,
        max_length=1000
    )
    code: str = Field(
        ...,
        description="Python code implementing the skill. Should define a main() function or be directly executable.",
        min_length=10,
        max_length=50000
    )
    category: SkillCategory = Field(
        default=SkillCategory.CUSTOM,
        description="Category for organizing and discovering the skill"
    )
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for searchability (e.g., ['csv', 'pandas', 'analysis'])",
        max_length=20
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="Python package dependencies (e.g., ['pandas', 'requests'])",
        max_length=20
    )
    parameters_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON Schema for skill parameters (optional, for parameterized skills)"
    )
    examples: List[str] = Field(
        default_factory=list,
        description=(
            "Example usage strings to help discover when this skill is useful. An example "
            "that is a JSON object (e.g. '{\"x\": 2}') is also run as test parameters."
        ),
        max_length=5
    )
    tests: List[SkillTestCase] = Field(
        default_factory=list,
        description=(
            "Test cases run in the sandbox before the skill is saved. If any fails, "
            "the skill is not saved (unless save_as_draft=True). State writes made "
            "during tests are never committed."
        ),
        max_length=10
    )
    save_as_draft: bool = Field(
        default=False,
        description=(
            "Save even if a test fails; the skill is marked status 'draft' and the "
            "failures are reported. Fix it with update_skill or re-check with verify_skill."
        ),
    )


class SearchSkillsInput(BaseModel):
    """Input model for searching skills."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        extra='forbid'
    )

    query: Optional[str] = Field(
        default=None,
        description="Text query to search skill names, descriptions, and tags"
    )
    category: Optional[SkillCategory] = Field(
        default=None,
        description="Filter by category"
    )
    tags: Optional[List[str]] = Field(
        default=None,
        description="Filter by tags (skills matching ANY of these tags)"
    )
    limit: int = Field(
        default=20,
        description="Maximum number of results to return",
        ge=1,
        le=200
    )
    cursor: int = Field(
        default=0,
        description="Pagination cursor (0-based offset into the sorted result set)",
        ge=0
    )
    fields: Optional[List[str]] = Field(
        default=None,
        description="For JSON responses: optional list of fields to include per item (e.g. ['name','description','category'])"
    )
    include_code: bool = Field(
        default=False,
        description="Whether to include full code in results (WARNING: increases response size significantly)"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable, 'json' for structured"
    )
