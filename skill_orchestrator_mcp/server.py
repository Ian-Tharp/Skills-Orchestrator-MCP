"""
Skill Orchestrator MCP Server - Core Server Module

This MCP server implements the "code execution with MCP" pattern for efficient
agent-tool interaction. It provides:

1. Skill Registry - Store and retrieve reusable code patterns/workflows
2. Code Execution - Run Python code in isolated environments
3. State Persistence - Maintain context across interactions
4. Dynamic Tool Generation - Create new capabilities on the fly
5. Progressive Discovery - Search and load tools on demand

Architecture follows Docker MCP Gateway patterns and Anthropic's recommendations
for context-efficient tool usage.
"""

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from .artifacts import ArtifactStore
from .models import (
    CreateSkillInput,
    ExecuteCodeInput,
    GetArtifactInput,
    InvokeSkillInput,
    ListStateInput,
    ResponseFormat,
    ReturnMode,
    SearchSkillsInput,
    SkillCategory,
    StateInput,
    StoreArtifactInput,
)
from .skill_index import SkillIndex
from .usage_stats import UsageTracker

# =============================================================================
# Configuration
# =============================================================================

# Default storage paths
DEFAULT_SKILLS_DIR = Path.home() / ".skill_orchestrator" / "skills"
DEFAULT_STATE_DIR = Path.home() / ".skill_orchestrator" / "state"
DEFAULT_SESSIONS_DIR = Path.home() / ".skill_orchestrator" / "sessions"
DEFAULT_ARTIFACTS_DIR = Path.home() / ".skill_orchestrator" / "artifacts"

# Ensure directories exist
for dir_path in [DEFAULT_SKILLS_DIR, DEFAULT_STATE_DIR, DEFAULT_SESSIONS_DIR, DEFAULT_ARTIFACTS_DIR]:
    dir_path.mkdir(parents=True, exist_ok=True)

# Skill metadata index (metadata-only, progressive disclosure)
SKILL_INDEX = SkillIndex(DEFAULT_SKILLS_DIR)
SKILL_INDEX.ensure_loaded()

# Usage stats (avoid rewriting skill JSON on every invocation)
USAGE_TRACKER = UsageTracker(DEFAULT_SKILLS_DIR)
# Seed usage counts from any legacy per-skill invocation_count values in the index.
try:
    USAGE_TRACKER.ensure_minimums(
        {
            name: int(meta.get("invocation_count", 0) or 0)
            for name, meta in SKILL_INDEX.all().items()
        }
    )
except Exception:
    # Best-effort migration only; keep server functional even if this fails.
    pass

# Artifact store (large outputs out-of-band)
ARTIFACT_STORE = ArtifactStore(DEFAULT_ARTIFACTS_DIR)

# -----------------------------------------------------------------------------
# Execution configuration (env-driven, with safe parsing)
# -----------------------------------------------------------------------------

def _get_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _get_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


# Execution limits
MAX_EXECUTION_TIME_LIMIT = max(
    1,
    _get_int_env("SKILL_ORCHESTRATOR_MAX_EXECUTION_TIME_SECONDS", 180),
)  # seconds
MAX_EXECUTION_TIME = max(
    1,
    min(
        _get_int_env("SKILL_ORCHESTRATOR_DEFAULT_EXECUTION_TIME_SECONDS", 30),
        MAX_EXECUTION_TIME_LIMIT,
    ),
)  # seconds
MAX_OUTPUT_SIZE = 100_000  # characters

# Subprocess termination grace period (avoid hangs after kill on Windows)
SUBPROCESS_TERMINATION_GRACE_SECONDS = max(
    0.5,
    _get_float_env("SKILL_ORCHESTRATOR_TERMINATION_GRACE_SECONDS", 5.0),
)

# Skill composition limits
MAX_SKILL_CALL_DEPTH = _get_int_env("SKILL_ORCHESTRATOR_MAX_CALL_DEPTH", 10)

# Index refresh debounce (to detect out-of-band skill file changes without rescanning constantly)
_INDEX_REFRESH_CHECK_INTERVAL_SECONDS = 2.0
_last_index_refresh_check_monotonic = 0.0

# =============================================================================
# Server Initialization
# =============================================================================

mcp = FastMCP("skill_orchestrator_mcp")

# =============================================================================
# Helper Functions
# =============================================================================

def _get_skill_path(name: str) -> Path:
    """Get the file path for a skill."""
    return DEFAULT_SKILLS_DIR / f"{name}.json"


def _get_state_path(namespace: str) -> Path:
    """Get the file path for a state namespace."""
    return DEFAULT_STATE_DIR / f"{namespace}.json"


def _load_skill(name: str) -> Optional[Dict[str, Any]]:
    """Load a skill from disk."""
    path = _get_skill_path(name)
    if path.exists():
        with open(path, 'r') as f:
            return cast(Dict[str, Any], json.load(f))
    return None


def _save_skill(skill_data: Dict[str, Any]) -> None:
    """Save a skill to disk."""
    path = _get_skill_path(skill_data['name'])
    with open(path, 'w') as f:
        json.dump(skill_data, f, indent=2, default=str)


def _load_state(namespace: str) -> Dict[str, Any]:
    """Load state from disk."""
    path = _get_state_path(namespace)
    if path.exists():
        with open(path, 'r') as f:
            return cast(Dict[str, Any], json.load(f))
    return {}


def _save_state(namespace: str, state: Dict[str, Any]) -> None:
    """Save state to disk."""
    path = _get_state_path(namespace)
    with open(path, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def _get_nested_value(data: Any, key: str) -> Any:
    """Get a nested value using dot notation."""
    keys = key.split('.')
    for k in keys:
        if isinstance(data, dict) and k in data:
            data = data[k]
        else:
            return None
    return data


def _set_nested_value(data: Dict[str, Any], key: str, value: Any) -> None:
    """Set a nested value using dot notation."""
    keys = key.split('.')
    for k in keys[:-1]:
        data = data.setdefault(k, {})
    data[keys[-1]] = value


def _generate_skill_id(name: str) -> str:
    """Generate a unique ID for a skill."""
    timestamp = datetime.now(timezone.utc).isoformat()
    return hashlib.sha256(f"{name}:{timestamp}".encode()).hexdigest()[:12]


def _sanitize_code_output(output: str) -> str:
    """Truncate and sanitize code output."""
    if len(output) > MAX_OUTPUT_SIZE:
        return output[:MAX_OUTPUT_SIZE] + f"\n\n... [truncated, {len(output) - MAX_OUTPUT_SIZE} characters omitted]"
    return output


def _build_skill_registry() -> Dict[str, Dict[str, Any]]:
    """Build a registry of all skills for subprocess injection.

    Returns a dict mapping skill names to their full definitions (including code).
    This enables skill composition - skills can invoke other skills during execution.
    """
    registry = {}
    for name, meta in SKILL_INDEX.all().items():
        skill = _load_skill(name)
        if skill:
            registry[name] = {
                'name': skill['name'],
                'code': skill['code'],
                'description': skill.get('description', ''),
                'parameters_schema': skill.get('parameters_schema'),
            }
    return registry


# =============================================================================
# Core Tools
# =============================================================================

@mcp.tool(
    name="create_skill",
    annotations=ToolAnnotations(
        title="Create a New Skill",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False
    )
)
async def create_skill(params: CreateSkillInput) -> str:
    """Create and store a new reusable skill.

    Skills are stored code patterns that can be invoked later. They enable
    progressive capability building - create a skill once, use it many times.

    Best practices:
    - Include a main() function for entry point
    - Document parameters in the description
    - Add relevant tags for discoverability
    - Keep skills focused on a single purpose

    Args:
        params: CreateSkillInput containing:
            - name: Unique snake_case identifier
            - description: What the skill does
            - code: Python implementation
            - category: Organization category
            - tags: Search tags
            - dependencies: Required packages
            - parameters_schema: JSON Schema for parameters
            - examples: Usage examples

    Returns:
        str: Confirmation message with skill ID
    """
    # Check if skill already exists
    existing = _load_skill(params.name)
    if existing:
        return f"Error: Skill '{params.name}' already exists (ID: {existing['id']}). Use update_skill to modify."

    # Create skill metadata
    skill_data = {
        "id": _generate_skill_id(params.name),
        "name": params.name,
        "description": params.description,
        "code": params.code,
        "category": params.category.value,
        "tags": params.tags,
        "dependencies": params.dependencies,
        "parameters_schema": params.parameters_schema,
        "examples": params.examples,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "version": 1,
        "invocation_count": 0
    }

    # Validate code syntax
    try:
        compile(params.code, f"skill_{params.name}", 'exec')
    except SyntaxError as e:
        return f"Error: Invalid Python syntax in skill code: {e}"

    # Save skill
    _save_skill(skill_data)
    SKILL_INDEX.upsert_from_skill(skill_data)
    USAGE_TRACKER.ensure_minimums({params.name: 0})

    return f"""✅ Skill created successfully!

**Name:** {params.name}
**ID:** {skill_data['id']}
**Category:** {params.category.value}
**Tags:** {', '.join(params.tags) if params.tags else 'None'}
**Dependencies:** {', '.join(params.dependencies) if params.dependencies else 'None'}

The skill is now available for invocation via `invoke_skill`."""


@mcp.tool(
    name="invoke_skill",
    annotations=ToolAnnotations(
        title="Invoke a Stored Skill",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True
    )
)
async def invoke_skill(params: InvokeSkillInput) -> str:
    """Execute a stored skill by name.

    Invokes a previously created skill, passing any provided parameters
    and context. The skill code is executed in an isolated namespace.

    Args:
        params: InvokeSkillInput containing:
            - skill_name: Name of the skill to invoke
            - parameters: Parameters to pass to the skill
            - context: Additional context variables
            - timeout: Max execution time (default 30s)
            - capture_output: Whether to capture stdout/stderr (default True)
            - return_mode: SUMMARY, HANDLE, or FULL
            - artifact_ttl_seconds: Optional TTL for outputs

    Returns:
        str: Execution result or error message
    """
    # Load skill
    skill = _load_skill(params.skill_name)
    if not skill:
        return f"Error: Skill '{params.skill_name}' not found. Use search_skills to find available skills."

    # Prepare execution namespace
    namespace = {
        'params': params.parameters or {},
        'context': params.context or {},
        '__skill_name__': params.skill_name,
        '__skill_id__': skill['id']
    }

    # Execute skill code with composition enabled
    result = await _execute_code_internal(
        code=skill['code'],
        context=namespace,
        timeout=params.timeout,
        capture_output=params.capture_output,
        enable_composition=True,
        current_skill_name=params.skill_name
    )

    # Update invocation count without rewriting the skill JSON (write amplification).
    USAGE_TRACKER.increment(params.skill_name)

    full_text = f"""## Skill Execution: {params.skill_name}

{result}"""

    ref = ARTIFACT_STORE.put_text(
        full_text,
        mime_type="text/markdown",
        name=f"invoke_skill:{params.skill_name}",
        ttl_seconds=params.artifact_ttl_seconds,
    )

    if params.return_mode == ReturnMode.HANDLE:
        return json.dumps({"artifact_id": ref.artifact_id, "resource": ref.resource_uri}, indent=2)

    if params.return_mode == ReturnMode.SUMMARY:
        preview = _sanitize_code_output(full_text[:2000])
        truncated = " (truncated)" if len(full_text) > 2000 else ""
        return (
            f"## Skill Execution (summary): {params.skill_name}\n\n"
            f"- **Artifact:** `{ref.resource_uri}`\n"
            f"- **Artifact ID:** `{ref.artifact_id}`\n\n"
            f"**Preview{truncated}:**\n```\n{preview}\n```"
        )

    # FULL
    return f"{full_text}\n\n---\nFull output stored as `{ref.resource_uri}`"


@mcp.tool(
    name="execute_code",
    annotations=ToolAnnotations(
        title="Execute Python Code",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True
    )
)
async def execute_code(params: ExecuteCodeInput) -> str:
    """Execute Python code in an isolated environment.

    Implements the "code execution with MCP" pattern for context-efficient
    operations. Use this to:
    - Process data without passing it through context
    - Chain multiple operations efficiently
    - Perform computations locally
    - Test skill code before saving

    Security note: Code runs in an isolated namespace but has access to
    standard library. External network calls and file operations are allowed
    but should be used carefully.

    Args:
        params: ExecuteCodeInput containing:
            - code: Python code to execute
            - context: Variables to inject into namespace
            - timeout: Max execution time (default 30s)
            - capture_output: Whether to capture stdout/stderr

    Returns:
        str: Execution output and results
    """
    output = await _execute_code_internal(
        code=params.code,
        context=params.context,
        timeout=params.timeout,
        capture_output=params.capture_output
    )

    ref = ARTIFACT_STORE.put_text(
        output,
        mime_type="text/markdown",
        name="execute_code",
        ttl_seconds=params.artifact_ttl_seconds,
    )

    if params.return_mode == ReturnMode.HANDLE:
        return json.dumps({"artifact_id": ref.artifact_id, "resource": ref.resource_uri}, indent=2)

    if params.return_mode == ReturnMode.SUMMARY:
        preview = _sanitize_code_output(output[:2000])
        truncated = " (truncated)" if len(output) > 2000 else ""
        return (
            "## Code Execution (summary)\n\n"
            f"- **Artifact:** `{ref.resource_uri}`\n"
            f"- **Artifact ID:** `{ref.artifact_id}`\n\n"
            f"**Preview{truncated}:**\n```\n{preview}\n```"
        )

    # FULL
    return f"{output}\n\n---\nFull output stored as `{ref.resource_uri}`"


@mcp.tool(
    name="store_artifact",
    annotations=ToolAnnotations(
        title="Store an Artifact (Large Output Handle)",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
async def store_artifact(params: StoreArtifactInput) -> str:
    """Store large content out-of-band and return an artifact handle."""

    ref = ARTIFACT_STORE.put_text(
        params.content,
        mime_type=params.mime_type,
        name=params.name,
        ttl_seconds=params.ttl_seconds,
    )
    return json.dumps(
        {
            "artifact_id": ref.artifact_id,
            "resource": ref.resource_uri,
        },
        indent=2,
    )


@mcp.tool(
    name="get_artifact",
    annotations=ToolAnnotations(
        title="Get Artifact Content (Chunked)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
async def get_artifact(params: GetArtifactInput) -> str:
    """Retrieve artifact content in chunks to avoid flooding context."""

    chunk = ARTIFACT_STORE.get_text_chunk(
        params.artifact_id,
        start=params.start,
        limit=params.limit,
    )
    if chunk is None:
        return json.dumps({"error": f"Artifact '{params.artifact_id}' not found"}, indent=2)
    return json.dumps(chunk, indent=2, default=str)


async def _execute_code_internal(
    code: str,
    context: Optional[Dict[str, Any]] = None,
    timeout: int = MAX_EXECUTION_TIME,
    capture_output: bool = True,
    enable_composition: bool = False,
    current_skill_name: Optional[str] = None
) -> str:
    """Internal code execution implementation.

    Default behavior executes code in a subprocess boundary so timeouts are real.
    Set env var SKILL_ORCHESTRATOR_EXEC_MODE=inprocess to use the legacy in-process
    exec() path (not recommended for untrusted code).
    """

    exec_mode = os.environ.get("SKILL_ORCHESTRATOR_EXEC_MODE", "subprocess").strip().lower()
    if exec_mode == "inprocess":
        # -------------------- Legacy in-process exec path --------------------
        # NOTE: This does NOT enforce timeouts; kept only as a compatibility fallback.
        safe_builtins = {
            "__builtins__": __builtins__,
            "__name__": "__main__",
            "__doc__": None,
        }

        exec_namespace = {**safe_builtins}
        if context:
            exec_namespace.update(context)

        exec_namespace["__imports__"] = """
import json
import re
import math
import datetime
import collections
import itertools
import functools
import os
import sys
from pathlib import Path
"""

        exec_namespace["__helpers__"] = """
def debug_context(**namespace_vars):
    \"\"\"Debug helper: Print all available variables in the execution context.\"\"\"
    import json
    print("=== Execution Context Debug ===")
    for key, value in sorted(namespace_vars.items()):
        if not key.startswith('__'):
            value_type = type(value).__name__
            try:
                value_preview = str(value)[:100]
                if len(str(value)) > 100:
                    value_preview += "..."
            except:
                value_preview = f"<{value_type}>"
            print(f"  {key}: {value_type} = {value_preview}")
    print("=" * 31)

def pretty_print(obj, indent=2):
    \"\"\"Pretty print JSON-serializable objects.\"\"\"
    import json
    try:
        print(json.dumps(obj, indent=indent, default=str))
    except Exception as e:
        print(f"Error pretty printing: {e}")
        print(obj)
"""

        stdout_capture = StringIO()
        stderr_capture = StringIO()

        wrapped_code = f"""
{exec_namespace['__imports__']}
{exec_namespace['__helpers__']}

__result__ = None
__error__ = None

try:
{chr(10).join('    ' + line for line in code.split(chr(10)))}
except Exception as e:
    __error__ = str(e)
    import traceback
    __traceback__ = traceback.format_exc()
"""

        try:
            if capture_output:
                with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
                    exec(wrapped_code, exec_namespace)
            else:
                exec(wrapped_code, exec_namespace)

            stdout_content = stdout_capture.getvalue()
            stderr_content = stderr_capture.getvalue()
            error = exec_namespace.get("__error__")
            tb = exec_namespace.get("__traceback__", "")
            result = exec_namespace.get("__result__")

            output_parts: List[str] = []

            if error:
                error_msg = f"**Error:** {error}\n```\n{tb}\n```"
                # Add helpful debugging hint
                if context:
                    available_vars = [k for k in context.keys() if not k.startswith('__')]
                    if available_vars:
                        error_msg += f"\n**Available context variables:** {', '.join(available_vars)}"
                error_msg += "\n**Tip:** Use `debug_context(**locals())` in your skill code to inspect available variables."
                output_parts.append(error_msg)
            if stdout_content:
                output_parts.append(f"**Output:**\n```\n{_sanitize_code_output(stdout_content)}\n```")
            if stderr_content:
                output_parts.append(f"**Stderr:**\n```\n{_sanitize_code_output(stderr_content)}\n```")
            if result is not None:
                if isinstance(result, (dict, list)):
                    result_str = json.dumps(result, indent=2, default=str)
                else:
                    result_str = str(result)
                output_parts.append(f"**Result:**\n```\n{_sanitize_code_output(result_str)}\n```")

            if not output_parts:
                output_parts.append("*Code executed successfully with no output.*")

            return "\n\n".join(output_parts)
        except Exception:
            return f"**Execution Error:**\n```\n{traceback.format_exc()}\n```"

    # -------------------- Subprocess execution path (default) --------------------

    common_imports = (
        "import json\n"
        "import re\n"
        "import math\n"
        "import datetime\n"
        "import collections\n"
        "import itertools\n"
        "import functools\n"
        "import time\n"
        "import os\n"
        "import sys\n"
        "from pathlib import Path\n"
    )

    # Helper utilities available to all skills
    helper_functions = '''
def debug_context(**namespace_vars):
    """Debug helper: Print all available variables in the execution context.

    Usage in skills:
        debug_context(**locals())
    """
    import json
    print("=== Execution Context Debug ===")
    for key, value in sorted(namespace_vars.items()):
        if not key.startswith('__'):
            value_type = type(value).__name__
            try:
                value_preview = str(value)[:100]
                if len(str(value)) > 100:
                    value_preview += "..."
            except:
                value_preview = f"<{value_type}>"
            print(f"  {key}: {value_type} = {value_preview}")
    print("=" * 31)

def pretty_print(obj, indent=2):
    """Pretty print JSON-serializable objects.

    Usage:
        pretty_print({"key": "value", "nested": {"data": [1, 2, 3]}})
    """
    import json
    try:
        print(json.dumps(obj, indent=indent, default=str))
    except Exception as e:
        print(f"Error pretty printing: {e}")
        print(obj)


# =============================================================================
# Skill Composition System
# =============================================================================
# Note: _skill_call_stack, _skill_registry, and _max_skill_call_depth are
# injected by the runner before these helpers are exec'd. Do NOT initialize
# them here as that would overwrite the runner's values.

def invoke_skill(skill_name, parameters=None, context=None):
    """Invoke another skill from within a skill.

    Enables skill composition - skills can call other skills to build
    complex workflows from simple primitives.

    Args:
        skill_name: Name of the skill to invoke
        parameters: Dict of parameters to pass (accessible as `params` in skill)
        context: Additional context variables (accessible as `context` in skill)

    Returns:
        The value of __result__ from the invoked skill, or None

    Raises:
        RuntimeError: If skill not found, max depth exceeded, or circular call detected

    Example:
        # From within a skill:
        domain_info = invoke_skill('domain_context_switcher', {
            'action': 'detect',
            'message': user_input
        })
        if domain_info.get('domains'):
            session_data = invoke_skill('persistent_session_context', {
                'action': 'recall',
                'domain': domain_info['domains'][0]
            })
    """
    # Access namespace variables through globals()
    g = globals()
    skill_registry = g.get('_skill_registry', {})
    skill_call_stack = g.get('_skill_call_stack', [])
    max_call_depth = g.get('_max_skill_call_depth', 10)

    # Check if skill registry is available
    if not skill_registry:
        raise RuntimeError(
            f"Skill composition not available: skill registry not loaded. "
            f"This may happen in direct execute_code calls."
        )

    # Check call depth
    if len(skill_call_stack) >= max_call_depth:
        raise RuntimeError(
            f"Maximum skill call depth ({max_call_depth}) exceeded. "
            f"Call stack: {' -> '.join(skill_call_stack)}"
        )

    # Check for circular calls
    if skill_name in skill_call_stack:
        raise RuntimeError(
            f"Circular skill invocation detected: {skill_name}. "
            f"Call stack: {' -> '.join(skill_call_stack)} -> {skill_name}"
        )

    # Look up skill in registry
    if skill_name not in skill_registry:
        available = list(skill_registry.keys())[:10]
        raise RuntimeError(
            f"Skill '{skill_name}' not found. "
            f"Available skills: {', '.join(available)}{'...' if len(skill_registry) > 10 else ''}"
        )

    skill = skill_registry[skill_name]
    skill_code = skill.get('code', '')

    if not skill_code:
        raise RuntimeError(f"Skill '{skill_name}' has no code defined.")

    # Push onto call stack
    skill_call_stack.append(skill_name)

    try:
        # Create execution namespace for the child skill
        child_namespace = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
            '__doc__': None,
            '__skill_name__': skill_name,
            '__parent_skill__': skill_call_stack[-2] if len(skill_call_stack) > 1 else None,
            '__call_depth__': len(skill_call_stack),
            'params': parameters or {},
            'context': context or {},
            # Inject composition helpers into child
            '_skill_registry': skill_registry,
            '_skill_call_stack': skill_call_stack,
            '_max_skill_call_depth': max_call_depth,
            'invoke_skill': invoke_skill,
            'list_available_skills': list_available_skills,
            'get_skill_info': get_skill_info,
            'debug_context': debug_context,
            'pretty_print': pretty_print,
        }

        # Execute the skill code
        exec(skill_code, child_namespace)

        # Return the result
        return child_namespace.get('__result__')

    finally:
        # Pop from call stack
        skill_call_stack.pop()


def list_available_skills():
    """List all skills available for composition.

    Returns:
        Dict mapping skill names to their descriptions.

    Example:
        skills = list_available_skills()
        for name, desc in skills.items():
            print(f"{name}: {desc}")
    """
    skill_registry = globals().get('_skill_registry', {})
    if not skill_registry:
        return {}
    return {
        name: skill.get('description', 'No description')
        for name, skill in skill_registry.items()
    }


def get_skill_info(skill_name):
    """Get information about a skill without invoking it.

    Args:
        skill_name: Name of the skill

    Returns:
        Dict with skill metadata, or None if not found
    """
    skill_registry = globals().get('_skill_registry', {})
    if not skill_registry:
        return None
    skill = skill_registry.get(skill_name)
    if skill:
        return {
            'name': skill.get('name'),
            'description': skill.get('description'),
            'parameters_schema': skill.get('parameters_schema'),
        }
    return None
'''

    runner_source = """
import json
import sys
import traceback

def main():
    payload_path = sys.argv[1]
    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    code = payload.get("code", "")
    context = payload.get("context") or {}
    result_path = payload.get("result_path")
    common_imports = payload.get("common_imports", "")
    skill_registry = payload.get("skill_registry") or {}
    max_call_depth = payload.get("max_call_depth", 10)
    current_skill_name = payload.get("current_skill_name")

    namespace = {"__builtins__": __builtins__, "__name__": "__main__", "__doc__": None}
    if isinstance(context, dict):
        namespace.update(context)

    error = None
    tb = None
    result = None

    try:
        # Execute common imports first
        exec(common_imports, namespace)

        # Initialize skill composition system BEFORE helpers (so invoke_skill can access them)
        namespace["_skill_registry"] = skill_registry
        namespace["_max_skill_call_depth"] = max_call_depth
        namespace["_skill_call_stack"] = [current_skill_name] if current_skill_name else []

        # Set user-facing context variables for the top-level skill
        # __call_depth__ = number of skills in the call chain (1 for top-level, 2 for first child, etc.)
        # This MUST equal len(_skill_call_stack) for consistency
        namespace["__call_depth__"] = len(namespace["_skill_call_stack"])
        namespace["__skill_name__"] = current_skill_name
        namespace["__parent_skill__"] = None

        # Now execute helper functions - they'll have access to the composition system
        helpers = payload.get("helpers", "")
        exec(helpers, namespace)

        # Execute the actual code
        exec(code, namespace)
        result = namespace.get("__result__")
    except Exception as e:
        error = str(e)
        tb = traceback.format_exc()

    out = {"error": error, "traceback": tb, "result": result}
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as rf:
                json.dump(out, rf, indent=2, default=str)
        except Exception:
            pass

if __name__ == "__main__":
    main()
""".lstrip()

    try:
        with tempfile.TemporaryDirectory(
            prefix="skill_orchestrator_exec_",
            ignore_cleanup_errors=True,
        ) as tmp:
            tmp_dir = Path(tmp)
            runner_path = tmp_dir / "runner.py"
            payload_path = tmp_dir / "payload.json"
            result_path = tmp_dir / "result.json"

            runner_path.write_text(runner_source, encoding="utf-8")

            payload = {
                "code": code,
                "context": context or {},
                "result_path": str(result_path),
                "common_imports": common_imports,
                "helpers": helper_functions,
                "skill_registry": _build_skill_registry() if enable_composition else {},
                "max_call_depth": MAX_SKILL_CALL_DEPTH,
                "current_skill_name": current_skill_name,
            }
            payload_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

            if capture_output:
                stdout_opt = asyncio.subprocess.PIPE
                stderr_opt = asyncio.subprocess.PIPE
            else:
                stdout_opt = asyncio.subprocess.DEVNULL
                stderr_opt = asyncio.subprocess.DEVNULL

            # Windows hardening:
            # - stdin=DEVNULL prevents child code from blocking on input()
            # - creationflags prevent console window creation + helps termination semantics
            # - close_fds reduces inherited handle surprises with stdio-based MCP servers
            creationflags = 0
            if os.name == "nt":
                if hasattr(subprocess, "CREATE_NO_WINDOW"):
                    creationflags |= subprocess.CREATE_NO_WINDOW
                if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                    creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(runner_path),
                str(payload_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=stdout_opt,
                stderr=stderr_opt,
                cwd=str(tmp_dir),
                close_fds=True,
                creationflags=creationflags,
            )

            timed_out = False
            kill_failed = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(), timeout=SUBPROCESS_TERMINATION_GRACE_SECONDS
                    )
                except asyncio.TimeoutError:
                    kill_failed = True
                    stdout_b, stderr_b = b"", b""

            stdout_content = ""
            stderr_content = ""
            if capture_output:
                stdout_content = (stdout_b or b"").decode("utf-8", errors="replace")
                stderr_content = (stderr_b or b"").decode("utf-8", errors="replace")

            error = None
            tb = ""
            result_obj: Any = None
            if result_path.exists():
                try:
                    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
                    error = result_payload.get("error")
                    tb = result_payload.get("traceback") or ""
                    result_obj = result_payload.get("result")
                except Exception:
                    error = "Failed to read execution result payload"

            output_parts = []

            if timed_out:
                output_parts.append(
                    f"**Error:** Execution timed out after {timeout}s (process kill attempted)."
                )
                if kill_failed:
                    output_parts.append(
                        f"**Warning:** Process did not terminate within {SUBPROCESS_TERMINATION_GRACE_SECONDS}s "
                        f"after kill. PID may still be running."
                    )

            if error:
                error_msg = f"**Error:** {error}\n```\n{tb}\n```"
                # Add helpful debugging hint
                if context:
                    available_vars = [k for k in context.keys() if not k.startswith('__')]
                    if available_vars:
                        error_msg += f"\n**Available context variables:** {', '.join(available_vars)}"
                error_msg += "\n**Tip:** Use `debug_context(**locals())` in your skill code to inspect available variables."
                output_parts.append(error_msg)

            if stdout_content:
                output_parts.append(f"**Output:**\n```\n{_sanitize_code_output(stdout_content)}\n```")

            if stderr_content:
                output_parts.append(f"**Stderr:**\n```\n{_sanitize_code_output(stderr_content)}\n```")

            if result_obj is not None:
                if isinstance(result_obj, (dict, list)):
                    result_str = json.dumps(result_obj, indent=2, default=str)
                else:
                    result_str = str(result_obj)
                output_parts.append(f"**Result:**\n```\n{_sanitize_code_output(result_str)}\n```")

            if not output_parts:
                output_parts.append("*Code executed successfully with no output.*")

            return "\n\n".join(output_parts)
    except Exception:
        return f"**Execution Error:**\n```\n{traceback.format_exc()}\n```"


@mcp.tool(
    name="search_skills",
    annotations=ToolAnnotations(
        title="Search Available Skills",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def search_skills(params: SearchSkillsInput) -> str:
    """Search and discover available skills.

    Progressive disclosure: Use this to find relevant skills before loading
    their full definitions. This keeps context efficient by only loading
    what's needed.

    Args:
        params: SearchSkillsInput containing:
            - query: Text search across names, descriptions, tags
            - category: Filter by skill category
            - tags: Filter by tags
            - include_code: Whether to include full code
            - response_format: markdown or json

    Returns:
        str: List of matching skills with metadata
    """
    global _last_index_refresh_check_monotonic

    # Refresh index if skill filenames changed outside of the orchestrator tools.
    now = time.monotonic()
    if now - _last_index_refresh_check_monotonic >= _INDEX_REFRESH_CHECK_INTERVAL_SECONDS:
        SKILL_INDEX.maybe_refresh_from_disk()
        _last_index_refresh_check_monotonic = now

    skills = list(SKILL_INDEX.all().values())

    # Apply filters
    if params.query:
        query_lower = params.query.lower()
        skills = [
            s for s in skills
            if query_lower in s['name'].lower()
            or query_lower in s['description'].lower()
            or any(query_lower in tag.lower() for tag in s.get('tags', []))
            or any(query_lower in ex.lower() for ex in s.get('examples', []))
        ]

    if params.category:
        skills = [s for s in skills if s.get('category') == params.category.value]

    if params.tags:
        skills = [
            s for s in skills
            if any(tag in s.get('tags', []) for tag in params.tags)
        ]

    # Sort by invocation count (most used first) using usage tracker (fast, write-light).
    skills.sort(key=lambda s: USAGE_TRACKER.get(str(s.get("name", ""))), reverse=True)

    total = len(skills)
    if total == 0:
        return "No skills found matching your criteria. Create a new skill with `create_skill`."

    start = min(params.cursor, total)
    end = min(start + params.limit, total)
    page = skills[start:end]
    next_cursor: Optional[int] = end if end < total else None

    # Optionally hydrate code only for the returned page.
    # Attach usage counts (index may contain legacy invocation_count, but usage is authoritative).
    page_with_usage: List[Dict[str, Any]] = []
    for meta in page:
        name = str(meta.get("name", ""))
        page_with_usage.append({**meta, "invocation_count": USAGE_TRACKER.get(name)})
    page = page_with_usage

    if params.include_code:
        hydrated: List[Dict[str, Any]] = []
        for meta in page:
            skill_name = meta.get("name")
            if isinstance(skill_name, str):
                full = _load_skill(skill_name)
                if full and "code" in full:
                    hydrated.append({**meta, "code": full.get("code")})
                else:
                    hydrated.append(dict(meta))
            else:
                hydrated.append(dict(meta))
        page = hydrated

    # Format response
    if params.response_format == ResponseFormat.JSON:
        items = page

        if params.fields:
            allowed = set(params.fields)
            # Always include `name` if present.
            allowed.add("name")
            items = [{k: v for k, v in s.items() if k in allowed} for s in items]

        payload = {
            "total": total,
            "cursor": params.cursor,
            "next_cursor": next_cursor,
            "items": items,
        }
        return json.dumps(payload, indent=2, default=str)

    # Markdown format (paged)
    lines = [
        f"## Found {total} skill(s)",
        f"Showing {len(page)} result(s) (cursor={params.cursor}, limit={params.limit}).\n",
    ]

    for skill in page:
        lines.append(f"### {skill['name']}")
        lines.append(f"*{skill['description']}*\n")
        lines.append(f"- **Category:** {skill.get('category', 'custom')}")
        lines.append(f"- **Tags:** {', '.join(skill.get('tags', [])) or 'None'}")
        lines.append(f"- **Dependencies:** {', '.join(skill.get('dependencies', [])) or 'None'}")
        lines.append(f"- **Used:** {skill.get('invocation_count', 0)} times")
        lines.append(f"- **ID:** {skill.get('id', 'unknown')}")

        if skill.get('examples'):
            lines.append(f"- **Examples:** {', '.join(skill['examples'])}")

        if params.include_code and skill.get("code"):
            lines.append(f"\n```python\n{skill['code']}\n```")

        lines.append("")

    if next_cursor is not None:
        lines.append(f"**Next cursor:** {next_cursor}")

    return "\n".join(lines)


@mcp.tool(
    name="get_skill",
    annotations=ToolAnnotations(
        title="Get Skill Details",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def get_skill(name: str) -> str:
    """Get full details of a specific skill including code.

    Use this to load a skill's complete definition when you need to:
    - Review the implementation
    - Understand parameters
    - Modify or extend the skill

    Args:
        name: The name of the skill to retrieve

    Returns:
        str: Full skill details in markdown format
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found. Use search_skills to find available skills."

    # Overlay authoritative usage count.
    skill["invocation_count"] = USAGE_TRACKER.get(name)

    lines = [
        f"# Skill: {skill['name']}",
        f"\n{skill['description']}\n",
        "## Metadata",
        f"- **ID:** {skill.get('id', 'unknown')}",
        f"- **Category:** {skill.get('category', 'custom')}",
        f"- **Version:** {skill.get('version', 1)}",
        f"- **Tags:** {', '.join(skill.get('tags', [])) or 'None'}",
        f"- **Dependencies:** {', '.join(skill.get('dependencies', [])) or 'None'}",
        f"- **Created:** {skill.get('created_at', 'unknown')}",
        f"- **Updated:** {skill.get('updated_at', 'unknown')}",
        f"- **Invocations:** {skill.get('invocation_count', 0)}",
    ]

    if skill.get('parameters_schema'):
        lines.append(f"\n## Parameters Schema\n```json\n{json.dumps(skill['parameters_schema'], indent=2)}\n```")

    if skill.get('examples'):
        lines.append("\n## Examples")
        for ex in skill['examples']:
            lines.append(f"- {ex}")

    lines.append(f"\n## Code\n```python\n{skill['code']}\n```")

    return "\n".join(lines)


@mcp.tool(
    name="update_skill",
    annotations=ToolAnnotations(
        title="Update an Existing Skill",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False
    )
)
async def update_skill(
    name: str,
    code: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[List[str]] = None,
    dependencies: Optional[List[str]] = None
) -> str:
    """Update an existing skill's properties.

    Only provided fields will be updated; others remain unchanged.
    The skill version is automatically incremented.

    Args:
        name: Name of the skill to update
        code: New Python code (optional)
        description: New description (optional)
        tags: New tags list (optional)
        dependencies: New dependencies list (optional)

    Returns:
        str: Confirmation of update
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."

    # Update fields
    if code is not None:
        try:
            compile(code, f"skill_{name}", 'exec')
        except SyntaxError as e:
            return f"Error: Invalid Python syntax: {e}"
        skill['code'] = code

    if description is not None:
        skill['description'] = description

    if tags is not None:
        skill['tags'] = tags

    if dependencies is not None:
        skill['dependencies'] = dependencies

    # Update metadata
    skill['updated_at'] = datetime.now(timezone.utc).isoformat()
    skill['version'] = skill.get('version', 1) + 1

    _save_skill(skill)
    SKILL_INDEX.upsert_from_skill(skill)

    return f"✅ Skill '{name}' updated to version {skill['version']}."


@mcp.tool(
    name="delete_skill",
    annotations=ToolAnnotations(
        title="Delete a Skill",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def delete_skill(name: str, confirm: bool = False) -> str:
    """Delete a skill permanently.

    WARNING: This action cannot be undone. Set confirm=True to proceed.

    Args:
        name: Name of the skill to delete
        confirm: Must be True to confirm deletion

    Returns:
        str: Confirmation or error message
    """
    if not confirm:
        return f"⚠️ To delete skill '{name}', call delete_skill with confirm=True"

    path = _get_skill_path(name)
    if not path.exists():
        return f"Error: Skill '{name}' not found."

    path.unlink()
    SKILL_INDEX.remove(name)
    USAGE_TRACKER.remove(name)
    return f"✅ Skill '{name}' has been permanently deleted."


# =============================================================================
# State Management Tools
# =============================================================================

@mcp.tool(
    name="set_state",
    annotations=ToolAnnotations(
        title="Store Persistent State",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def set_state(params: StateInput) -> str:
    """Store a value in persistent state.

    State persists across conversations and can be used to:
    - Remember preferences
    - Store intermediate results
    - Track workflow progress
    - Cache expensive computations

    Uses dot notation for nested keys: 'user.preferences.theme'

    Args:
        params: StateInput containing:
            - key: State key (dot notation for nesting)
            - value: JSON-serializable value to store
            - namespace: Organization namespace (default: 'default')

    Returns:
        str: Confirmation message
    """
    state = _load_state(params.namespace)
    _set_nested_value(state, params.key, params.value)
    _save_state(params.namespace, state)

    return f"✅ State '{params.key}' set in namespace '{params.namespace}'."


@mcp.tool(
    name="get_state",
    annotations=ToolAnnotations(
        title="Retrieve Persistent State",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def get_state(key: str, namespace: str = "default") -> str:
    """Retrieve a value from persistent state.

    Args:
        key: State key to retrieve (dot notation supported)
        namespace: State namespace (default: 'default')

    Returns:
        str: The stored value or None if not found
    """
    state = _load_state(namespace)
    value = _get_nested_value(state, key)

    if value is None:
        return f"State '{key}' not found in namespace '{namespace}'."

    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, default=str)
    return str(value)


@mcp.tool(
    name="list_state",
    annotations=ToolAnnotations(
        title="List State Keys (Paginated)",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def list_state(params: ListStateInput) -> str:
    """List keys in a state namespace with pagination and context-efficient response modes.

    Supports three return modes:
    - SUMMARY (default): Keys with type info and short value previews, paginated
    - HANDLE: Returns artifact reference for full state data
    - FULL: Complete paginated list with full values (use sparingly)

    Args:
        params: ListStateInput containing:
            - namespace: State namespace to list (default: "default")
            - response_format: 'markdown' or 'json'
            - limit: Maximum keys to return (default: 50, max: 500)
            - cursor: Pagination offset (default: 0)
            - return_mode: SUMMARY, HANDLE, or FULL
            - include_values: Whether to include full values (FULL mode only)
            - value_preview_length: Max chars for value previews (default: 50)

    Returns:
        str: Paginated list of state keys with metadata
    """
    state = _load_state(params.namespace)

    if not state:
        return f"No state found in namespace '{params.namespace}'."

    def _flatten_keys(d: Dict[str, Any], prefix: str = "") -> List[Tuple[str, str, Any]]:
        """Flatten nested state into (key, type, value) tuples."""
        items: List[Tuple[str, str, Any]] = []
        for k, v in d.items():
            key = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                items.extend(_flatten_keys(v, key))
            else:
                items.append((key, type(v).__name__, v))
        return items

    flat = _flatten_keys(state)
    total = len(flat)

    # Apply pagination
    start = min(params.cursor, total)
    end = min(start + params.limit, total)
    page = flat[start:end]
    next_cursor: Optional[int] = end if end < total else None

    # Build full output (may be stored as artifact)
    if params.response_format == ResponseFormat.JSON:
        items = []
        for key, type_name, value in page:
            item = {"key": key, "type": type_name}
            if params.return_mode == ReturnMode.FULL and params.include_values:
                item["value"] = value
            elif params.return_mode != ReturnMode.HANDLE:
                # Summary mode: include preview
                val_str = str(value)
                if len(val_str) > params.value_preview_length:
                    item["value_preview"] = val_str[:params.value_preview_length] + "..."
                else:
                    item["value_preview"] = val_str
            items.append(item)

        payload = {
            "namespace": params.namespace,
            "total_keys": total,
            "cursor": params.cursor,
            "next_cursor": next_cursor,
            "items": items,
        }
        full_text = json.dumps(payload, indent=2, default=str)
    else:
        # Markdown format
        lines = [
            f"## State in namespace '{params.namespace}'",
            f"Total keys: {total} | Showing: {len(page)} (cursor={params.cursor}, limit={params.limit})\n"
        ]

        for key, type_name, value in page:
            if params.return_mode == ReturnMode.FULL and params.include_values:
                # Full value (potentially large)
                val_str = str(value)
                if len(val_str) > 200:
                    lines.append(f"### {key} ({type_name})")
                    lines.append(f"```\n{val_str}\n```\n")
                else:
                    lines.append(f"- **{key}** ({type_name}): `{val_str}`")
            elif params.return_mode != ReturnMode.HANDLE:
                # Summary: short preview
                val_str = str(value)
                if len(val_str) > params.value_preview_length:
                    val_preview = val_str[:params.value_preview_length] + "..."
                else:
                    val_preview = val_str
                lines.append(f"- **{key}** ({type_name}): `{val_preview}`")
            else:
                # Handle mode: keys and types only
                lines.append(f"- **{key}** ({type_name})")

        if next_cursor is not None:
            lines.append(f"\n**Next cursor:** {next_cursor} (use to fetch more keys)")

        full_text = "\n".join(lines)

    # Handle different return modes with artifact system
    ref = ARTIFACT_STORE.put_text(
        full_text,
        mime_type="application/json" if params.response_format == ResponseFormat.JSON else "text/markdown",
        name=f"list_state:{params.namespace}",
        ttl_seconds=300,  # 5 min TTL for state listings
    )

    if params.return_mode == ReturnMode.HANDLE:
        return json.dumps({"artifact_id": ref.artifact_id, "resource": ref.resource_uri}, indent=2)

    if params.return_mode == ReturnMode.SUMMARY:
        # Return summary + artifact reference
        preview = full_text[:1000]
        truncated = " (truncated)" if len(full_text) > 1000 else ""
        return (
            f"## State Listing (summary): {params.namespace}\n\n"
            f"- **Artifact:** `{ref.resource_uri}`\n"
            f"- **Artifact ID:** `{ref.artifact_id}`\n"
            f"- **Total keys:** {total}\n"
            f"- **Showing:** {len(page)} keys\n\n"
            f"**Preview{truncated}:**\n```\n{preview}\n```"
        )

    # FULL mode: return complete output (also stored as artifact)
    return f"{full_text}\n\n---\nFull output stored as `{ref.resource_uri}`"


# =============================================================================
# Utility Tools
# =============================================================================

@mcp.tool(
    name="list_categories",
    annotations=ToolAnnotations(
        title="List Skill Categories",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def list_categories() -> str:
    """List all available skill categories.

    Returns:
        str: List of categories with descriptions
    """
    categories = {
        SkillCategory.DATA_PROCESSING: "Processing and transforming data (CSV, JSON, etc.)",
        SkillCategory.FILE_OPERATIONS: "Reading, writing, and managing files",
        SkillCategory.API_INTEGRATION: "Connecting to external APIs and services",
        SkillCategory.TEXT_ANALYSIS: "Text processing, NLP, and analysis",
        SkillCategory.AUTOMATION: "Workflow automation and scripting",
        SkillCategory.VISUALIZATION: "Charts, graphs, and visual outputs",
        SkillCategory.COMPUTATION: "Mathematical and scientific computations",
        SkillCategory.WORKFLOW: "Multi-step workflows and pipelines",
        SkillCategory.UTILITY: "General utility functions",
        SkillCategory.CUSTOM: "Custom/uncategorized skills"
    }

    lines = ["## Skill Categories\n"]
    for cat, desc in categories.items():
        lines.append(f"- **{cat.value}**: {desc}")

    return "\n".join(lines)


@mcp.tool(
    name="get_orchestrator_status",
    annotations=ToolAnnotations(
        title="Get Orchestrator Status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def get_orchestrator_status() -> str:
    """Get the current status of the Skill Orchestrator.

    Returns information about:
    - Number of stored skills
    - State namespaces
    - Storage locations
    - Server version

    Returns:
        str: Status information in markdown format
    """
    global _last_index_refresh_check_monotonic

    now = time.monotonic()
    if now - _last_index_refresh_check_monotonic >= _INDEX_REFRESH_CHECK_INTERVAL_SECONDS:
        SKILL_INDEX.maybe_refresh_from_disk()
        _last_index_refresh_check_monotonic = now

    skills = list(SKILL_INDEX.all().values())
    skill_count = len(skills)

    # Count state namespaces
    state_namespaces = len(list(DEFAULT_STATE_DIR.glob("*.json")))

    # Get total skill invocations from usage stats (authoritative)
    total_invocations = USAGE_TRACKER.snapshot().total
    skills_by_category: Dict[str, int] = {}
    for skill in skills:
        cat = str(skill.get("category", "custom"))
        skills_by_category[cat] = skills_by_category.get(cat, 0) + 1

    return f"""# Skill Orchestrator Status

**Version:** skill_orchestrator_mcp v0.1.0

## Storage
- **Skills Directory:** `{DEFAULT_SKILLS_DIR}`
- **State Directory:** `{DEFAULT_STATE_DIR}`
- **Sessions Directory:** `{DEFAULT_SESSIONS_DIR}`
- **Artifacts Directory:** `{DEFAULT_ARTIFACTS_DIR}`

## Statistics
- **Total Skills:** {skill_count}
- **Total Invocations:** {total_invocations}
- **State Namespaces:** {state_namespaces}

## Skills by Category
{chr(10).join(f'- **{cat}:** {count}' for cat, count in sorted(skills_by_category.items()))}

## Capabilities
- ✅ Create and manage reusable skills
- ✅ Execute Python code in isolated environments
- ✅ Persist state across conversations
- ✅ Progressive tool discovery
- ✅ Dynamic capability evolution
"""


# =============================================================================
# Resources
# =============================================================================

@mcp.resource("skills://list")
def list_all_skills() -> str:
    """List all available skills as a resource."""
    # Keep resources context-efficient: return top-N by usage, metadata-only.
    skills = list(SKILL_INDEX.all().values())
    skills.sort(key=lambda s: USAGE_TRACKER.get(str(s.get("name", ""))), reverse=True)

    max_items = 200
    items = [
        {
            "name": s.get("name"),
            "description": s.get("description"),
            "category": s.get("category", "custom"),
        }
        for s in skills[:max_items]
    ]
    payload = {"total": len(skills), "returned": len(items), "items": items}
    return json.dumps(payload, indent=2, default=str)


@mcp.resource("skills://{name}")
def get_skill_resource(name: str) -> str:
    """Get a specific skill as a resource."""
    skill = _load_skill(name)
    if skill:
        return json.dumps(skill, indent=2, default=str)
    return json.dumps({"error": f"Skill '{name}' not found"})


@mcp.resource("state://{namespace}")
def get_state_resource(namespace: str) -> str:
    """Get all state in a namespace as a resource."""
    state = _load_state(namespace)
    return json.dumps(state, indent=2, default=str)


@mcp.resource("artifact://{artifact_id}")
def get_artifact_resource(artifact_id: str) -> str:
    """Get an artifact with a small preview (use get_artifact for chunking)."""

    meta = ARTIFACT_STORE.get_meta(artifact_id)
    if meta is None:
        return json.dumps({"error": f"Artifact '{artifact_id}' not found"}, indent=2)

    preview_chunk = ARTIFACT_STORE.get_text_chunk(artifact_id, start=0, limit=10_000)
    preview = preview_chunk.get("chunk") if preview_chunk else ""
    truncated = bool(preview_chunk and preview_chunk.get("next_start") is not None)

    return json.dumps(
        {
            "meta": meta,
            "preview": preview,
            "preview_truncated": truncated,
            "hint": "Use the get_artifact tool for chunked retrieval.",
        },
        indent=2,
        default=str,
    )


# =============================================================================
# Entry Point
# =============================================================================

def run_server() -> None:
    """Run the MCP server."""
    mcp.run()


if __name__ == "__main__":
    run_server()
