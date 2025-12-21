"""
Common enums and types used across multiple model domains.
"""

from enum import Enum


class SkillCategory(str, Enum):
    """Categories for organizing skills."""
    DATA_PROCESSING = "data_processing"
    FILE_OPERATIONS = "file_operations"
    API_INTEGRATION = "api_integration"
    TEXT_ANALYSIS = "text_analysis"
    AUTOMATION = "automation"
    VISUALIZATION = "visualization"
    COMPUTATION = "computation"
    WORKFLOW = "workflow"
    UTILITY = "utility"
    CUSTOM = "custom"


class ResponseFormat(str, Enum):
    """Output format options."""
    MARKDOWN = "markdown"
    JSON = "json"


class ReturnMode(str, Enum):
    """How tools should return results to the agent."""

    SUMMARY = "summary"  # short excerpt + artifact handle (default)
    HANDLE = "handle"  # artifact handle only
    FULL = "full"  # full text response (may be large; still stored as artifact)
