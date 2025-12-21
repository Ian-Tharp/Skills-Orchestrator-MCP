# Skill Orchestrator Models

This directory contains all Pydantic input validation models for the Skill Orchestrator MCP server, organized by functional domain for clarity and maintainability.

## Structure

```
models/
├── __init__.py              # Package exports
├── common.py               # Shared enums and types
├── skill_models.py         # Skill management
├── execution_models.py     # Code execution
├── state_models.py         # State persistence
└── artifact_models.py      # Artifact storage
```

## Files

### `common.py` - Shared Types
Common enums used across multiple model domains.

**Exports:**
- `SkillCategory` - Categories for organizing skills (data_processing, workflow, etc.)
- `ResponseFormat` - Output format options (markdown, json)
- `ReturnMode` - Response verbosity control (summary, handle, full)

### `skill_models.py` - Skill Management
Models for creating and searching skills.

**Exports:**
- `CreateSkillInput` - Create a new reusable skill
  - name, description, code, category, tags, dependencies, parameters_schema, examples
- `SearchSkillsInput` - Search and filter skills
  - query, category, tags, limit, cursor, include_code, response_format

### `execution_models.py` - Code Execution
Models for executing Python code and invoking skills.

**Exports:**
- `ExecuteCodeInput` - Execute arbitrary Python code
  - code, context, timeout, capture_output, return_mode, artifact_ttl_seconds
- `InvokeSkillInput` - Execute a stored skill by name
  - skill_name, parameters, context, timeout, return_mode, artifact_ttl_seconds

### `state_models.py` - State Persistence
Models for managing persistent state across conversations.

**Exports:**
- `StateInput` - Set/get individual state values
  - key, value, namespace
- `ListStateInput` - List state keys with pagination
  - namespace, response_format, limit, cursor, return_mode, include_values, value_preview_length

### `artifact_models.py` - Artifact Storage
Models for out-of-band storage of large outputs.

**Exports:**
- `StoreArtifactInput` - Explicitly store large content
  - content, mime_type, name, ttl_seconds
- `GetArtifactInput` - Retrieve artifact chunks
  - artifact_id, start, limit

## Usage

### Import all models
```python
from skill_orchestrator_mcp.models import (
    CreateSkillInput,
    ExecuteCodeInput,
    SearchSkillsInput,
    ListStateInput,
    ReturnMode,
    # ... etc
)
```

### Import specific domain
```python
from skill_orchestrator_mcp.models.skill_models import CreateSkillInput, SearchSkillsInput
from skill_orchestrator_mcp.models.common import ReturnMode
```

### Import from submodules
```python
from skill_orchestrator_mcp.models import skill_models
from skill_orchestrator_mcp.models import execution_models
```

## Design Principles

1. **Domain Separation** - Models grouped by functional area
2. **Shared Types** - Common enums in `common.py` to avoid duplication
3. **Validation** - Pydantic handles all input validation and coercion
4. **Documentation** - Rich Field descriptions for auto-generated docs
5. **Type Safety** - Full typing for IDE support and runtime checking

## Model Configuration

All models use these common Pydantic configs:

```python
model_config = ConfigDict(
    str_strip_whitespace=True,  # Auto-trim strings
    extra='forbid'              # Reject unknown fields
)
```

Some models also use:
- `validate_assignment=True` - Validate on attribute assignment (CreateSkillInput)
- Field validators for complex validation logic

## Return Modes

Many tools support three return modes for context efficiency:

### SUMMARY (Default)
- Returns preview (first 1-2K chars) + artifact reference
- Best for quick inspection
- Example: Search results, state listings

### HANDLE
- Returns only artifact reference
- Most context-efficient
- Use when you just need to pass reference around

### FULL
- Returns complete data + artifact reference
- Use sparingly for detailed inspection
- May be large (stored as artifact regardless)

## Pagination Pattern

Models supporting pagination follow this pattern:

```python
limit: int = Field(default=20, ge=1, le=200)  # Max items per page
cursor: int = Field(default=0, ge=0)           # Offset into results
next_cursor: Optional[int]                     # Returned in response
```

Example:
```python
# Page 1
search_skills(params={'query': 'data', 'limit': 10, 'cursor': 0})

# Page 2
search_skills(params={'query': 'data', 'limit': 10, 'cursor': 10})
```

## Validation Examples

### Field Constraints
```python
# String length
name: str = Field(min_length=1, max_length=100)

# Numeric range
timeout: int = Field(ge=1, le=180)

# Pattern matching
name: str = Field(pattern=r'^[a-z][a-z0-9_]*$')

# List size
tags: List[str] = Field(max_length=20)
```

### Default Values
```python
# Static default
category: SkillCategory = Field(default=SkillCategory.CUSTOM)

# Factory default (for mutable types)
tags: List[str] = Field(default_factory=list)

# Optional field
value: Optional[Any] = Field(default=None)
```

## Testing

Models can be tested independently:

```python
from skill_orchestrator_mcp.models import ListStateInput, ReturnMode

# Create instance
params = ListStateInput(
    namespace="test",
    limit=10,
    return_mode=ReturnMode.SUMMARY
)

# Access fields
assert params.namespace == "test"
assert params.limit == 10
assert params.return_mode == ReturnMode.SUMMARY

# Validation error
try:
    bad_params = ListStateInput(limit=1000)  # Exceeds max
except ValidationError as e:
    print(e)
```

## Migration from server.py

These models were extracted from `server.py` in the 2025-12-21 refactoring:

- Reduced server.py by ~260 lines
- Improved code organization
- Enabled independent model testing
- No breaking changes to API

## Future Enhancements

Potential additions:
- [ ] UpdateSkillInput for partial skill updates
- [ ] BatchOperationInput for multi-item operations
- [ ] FilterStateInput for advanced state queries
- [ ] SkillExecutionConfig for execution preferences

---

**Last Updated:** 2025-12-21
**Models Version:** 1.0
**Maintainer:** Skill Orchestrator Team
