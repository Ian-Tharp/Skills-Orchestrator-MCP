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
import copy
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import sandbox_env, skill_md
from . import skill_contracts as contracts
from . import skill_verification as verification
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
    SkillTestCase,
    StateInput,
    StoreArtifactInput,
)
from .skill_index import SkillIndex
from .skill_versions import CONTENT_FIELDS, SkillVersionStore, code_digest, diff_skills
from .telemetry import (
    KIND_CODE,
    KIND_NESTED,
    KIND_SKILL,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_REJECTED,
    STATUS_TIMEOUT,
    ExecutionTelemetry,
)
from .usage_stats import UsageTracker

try:
    SERVER_VERSION = importlib.metadata.version("skill-orchestrator-mcp")
except importlib.metadata.PackageNotFoundError:  # running from a source checkout
    SERVER_VERSION = "0.0.0+unknown"

# =============================================================================
# Configuration
# =============================================================================

# Default storage paths
DEFAULT_SKILLS_DIR = Path.home() / ".skill_orchestrator" / "skills"
DEFAULT_STATE_DIR = Path.home() / ".skill_orchestrator" / "state"
DEFAULT_SESSIONS_DIR = Path.home() / ".skill_orchestrator" / "sessions"
DEFAULT_ARTIFACTS_DIR = Path.home() / ".skill_orchestrator" / "artifacts"
DEFAULT_TELEMETRY_DIR = Path.home() / ".skill_orchestrator" / "telemetry"

# Ensure directories exist
for dir_path in [
    DEFAULT_SKILLS_DIR,
    DEFAULT_STATE_DIR,
    DEFAULT_SESSIONS_DIR,
    DEFAULT_ARTIFACTS_DIR,
    DEFAULT_TELEMETRY_DIR,
]:
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

# Execution telemetry (local JSONL log; set SKILL_ORCHESTRATOR_TELEMETRY=off to disable)
TELEMETRY = ExecutionTelemetry(
    DEFAULT_TELEMETRY_DIR,
    max_records=max(1, _get_int_env("SKILL_ORCHESTRATOR_TELEMETRY_MAX_RECORDS", 10_000)),
    enabled=os.environ.get("SKILL_ORCHESTRATOR_TELEMETRY", "on").strip().lower()
    not in ("0", "off", "false", "no"),
)

# Skill version history (snapshots under skills/.versions/)
SKILL_VERSIONS = SkillVersionStore(
    DEFAULT_SKILLS_DIR,
    max_versions=max(1, _get_int_env("SKILL_ORCHESTRATOR_MAX_SKILL_VERSIONS", 50)),
)

# Verified-save gate and lifecycle
# Reject saves that have no runnable test cases (unless saved as a draft).
REQUIRE_VERIFIED_SKILLS = os.environ.get(
    "SKILL_ORCHESTRATOR_REQUIRE_VERIFIED_SKILLS", "off"
).strip().lower() in ("1", "on", "true", "yes")
# A skill is shown as degraded when more than this share of the current
# version's last DEGRADED_WINDOW runs failed (and at least DEGRADED_MIN_RUNS ran).
DEGRADED_FAILURE_RATE = min(
    1.0, max(0.0, _get_float_env("SKILL_ORCHESTRATOR_DEGRADED_FAILURE_RATE", 0.5))
)
DEGRADED_WINDOW = max(1, _get_int_env("SKILL_ORCHESTRATOR_DEGRADED_WINDOW", 20))
DEGRADED_MIN_RUNS = max(1, _get_int_env("SKILL_ORCHESTRATOR_DEGRADED_MIN_RUNS", 5))

# Serializes read-modify-write of state files (MCP tools + state bridge commits).
_STATE_LOCK = threading.Lock()

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

_UNSAFE_NAME_CHARS = frozenset("/\\:\x00")


def _is_safe_storage_name(name: str) -> bool:
    """Return True if `name` can be used as a single file name in a storage dir.

    Skill names and state namespaces become file names. Reject anything that
    could escape the storage directory (path separators, `..`, drive markers,
    NUL) or clobber internal dotfiles such as `.skill_index.json`.
    """
    return bool(name) and not name.startswith(".") and not (_UNSAFE_NAME_CHARS & set(name))


def _get_skill_path(name: str) -> Path:
    """Get the file path for a skill."""
    if not _is_safe_storage_name(name):
        raise ValueError(f"Invalid skill name: {name!r}")
    return DEFAULT_SKILLS_DIR / f"{name}.json"


def _get_state_path(namespace: str) -> Path:
    """Get the file path for a state namespace."""
    if not _is_safe_storage_name(namespace):
        raise ValueError(f"Invalid state namespace: {namespace!r}")
    return DEFAULT_STATE_DIR / f"{namespace}.json"


def _invalid_namespace_message(namespace: str) -> str:
    return (
        f"Error: Invalid namespace '{namespace}'. Namespaces may not contain path "
        "separators, ':' or start with '.'."
    )


def _load_skill(name: str) -> Optional[Dict[str, Any]]:
    """Load a skill from disk. Unsafe names are treated as not found."""
    if not _is_safe_storage_name(name):
        return None
    path = _get_skill_path(name)
    if path.exists():
        with open(path, 'r') as f:
            return cast(Dict[str, Any], json.load(f))
    return None


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via temp file + rename so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
    ) as tmp:
        json.dump(data, tmp, indent=2, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _save_skill(skill_data: Dict[str, Any]) -> None:
    """Save a skill to disk."""
    _atomic_write_json(_get_skill_path(skill_data['name']), skill_data)


def _load_state(namespace: str) -> Dict[str, Any]:
    """Load state from disk."""
    path = _get_state_path(namespace)
    if path.exists():
        with open(path, 'r', encoding='utf-8') as f:
            return cast(Dict[str, Any], json.load(f))
    return {}


def _save_state(namespace: str, state: Dict[str, Any]) -> None:
    """Save state to disk."""
    _atomic_write_json(_get_state_path(namespace), state)


def _delete_nested_value(data: Dict[str, Any], key: str) -> bool:
    """Delete a nested value using dot notation. Returns True if it existed."""
    keys = key.split('.')
    node: Any = data
    for k in keys[:-1]:
        node = node.get(k) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return False
    if isinstance(node, dict) and keys[-1] in node:
        del node[keys[-1]]
        return True
    return False


def _commit_state_ops(ops: List[Dict[str, Any]]) -> Dict[str, int]:
    """Apply state writes buffered by a skill run (the state bridge).

    Ops are replayed onto the *current* file contents, so concurrent writes to
    other keys are preserved (last writer wins per key). All ops are applied
    in memory first; if any is invalid nothing is written.

    Returns the number of ops applied per namespace. Raises ValueError on an
    invalid op.
    """
    if not ops:
        return {}
    with _STATE_LOCK:
        loaded: Dict[str, Dict[str, Any]] = {}
        counts: Dict[str, int] = {}
        for op in ops:
            if not isinstance(op, dict):
                raise ValueError("malformed state op")
            ns = op.get("namespace")
            key = op.get("key")
            if not isinstance(ns, str) or not _is_safe_storage_name(ns):
                raise ValueError(f"invalid state namespace {ns!r}")
            if not isinstance(key, str) or not key:
                raise ValueError(f"invalid state key {key!r}")
            if ns not in loaded:
                loaded[ns] = _load_state(ns)
            if op.get("op") == "set":
                _set_nested_value(loaded[ns], key, op.get("value"))
            elif op.get("op") == "delete":
                _delete_nested_value(loaded[ns], key)
            else:
                raise ValueError(f"unknown state op {op.get('op')!r}")
            counts[ns] = counts.get(ns, 0) + 1
        for ns, data in loaded.items():
            _save_state(ns, data)
        return counts


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
    for i, k in enumerate(keys[:-1]):
        child = data.setdefault(k, {})
        if not isinstance(child, dict):
            raise ValueError(
                f"Cannot set '{key}': '{'.'.join(keys[: i + 1])}' holds a "
                f"{type(child).__name__}, not an object"
            )
        data = child
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


def _registry_entry(skill: Dict[str, Any]) -> Dict[str, Any]:
    """What the child needs to know about one skill to invoke it."""
    schema = skill.get('parameters_schema')
    return {
        'name': skill['name'],
        'code': skill['code'],
        'description': skill.get('description', ''),
        'version': skill.get('version', 1),
        'dependencies': skill.get('dependencies') or [],
        # Checked here (same interpreter as the child) so the child can refuse cheaply.
        'missing_dependencies': contracts.missing_dependencies(skill.get('dependencies')),
        'parameters_schema': schema,
        # Legacy skills may carry a broken schema; the child skips enforcing those.
        'schema_valid': contracts.check_schema(schema) is None,
    }


def _build_skill_registry() -> Dict[str, Dict[str, Any]]:
    """Build a registry of all skills for subprocess injection.

    Returns a dict mapping skill names to their full definitions (including code).
    This enables skill composition - skills can invoke other skills during execution.
    """
    registry: Dict[str, Dict[str, Any]] = {}
    for name in SKILL_INDEX.all():
        skill = _load_skill(name)
        # Skip malformed skill files so one bad file can't break composition.
        if skill and isinstance(skill.get('code'), str):
            registry[name] = _registry_entry(skill)
    return registry


def _skill_dependents(name: str) -> List[str]:
    """Skills whose code calls `invoke_skill('<name>', ...)` with a literal name."""
    dependents = []
    for other in SKILL_INDEX.all():
        if other == name:
            continue
        skill = _load_skill(other)
        code = skill.get('code') if skill else None
        if isinstance(code, str) and name in contracts.find_skill_refs(code):
            dependents.append(other)
    return sorted(dependents)


def _contract_warnings(name: str, code: str, dependencies: List[str]) -> List[str]:
    """Non-fatal problems worth telling the author about at create/update time."""
    warnings = []
    missing = contracts.missing_dependencies(dependencies)
    if missing:
        warnings.append(
            f"Unmet dependencies: {', '.join(missing)}. Invocations will be refused until "
            f"they are installed. {contracts.install_hint(missing)}"
        )
    known = set(SKILL_INDEX.all())
    for ref in contracts.find_skill_refs(code):
        if ref == name:
            warnings.append(f"Skill calls itself via invoke_skill('{ref}'); recursion is blocked at runtime.")
        elif ref not in known:
            warnings.append(f"Calls unknown skill '{ref}' (create it before invoking this one).")
    return warnings


def _format_warnings(warnings: List[str]) -> str:
    if not warnings:
        return ""
    return "\n\n**Warnings:**\n" + "\n".join(f"- {w}" for w in warnings)


def _case_timeout(raw: Any) -> int:
    try:
        value = int(raw) if raw is not None else MAX_EXECUTION_TIME
    except (TypeError, ValueError):
        value = MAX_EXECUTION_TIME
    return max(1, min(value, MAX_EXECUTION_TIME_LIMIT))


async def _verify_skill(skill: Dict[str, Any]) -> verification.VerificationReport:
    """Run a (possibly unsaved) skill's test cases in the sandbox.

    The candidate replaces any saved skill of the same name in the composition
    registry, so it can call itself and other skills as it will after saving.
    State writes are never committed and runs are not recorded in telemetry.
    """
    now = datetime.now(timezone.utc).isoformat()
    cases = verification.collect_cases(skill)
    if not cases:
        return verification.VerificationReport(
            status=verification.STATUS_UNVERIFIED,
            reason=(
                "no runnable test cases: add `tests`, or an example that is a JSON object "
                "of parameters"
            ),
            checked_at=now,
        )
    missing = contracts.missing_dependencies(skill.get('dependencies'))
    if missing:
        return verification.VerificationReport(
            status=verification.STATUS_UNVERIFIED,
            reason=f"tests not run, unmet dependencies: {', '.join(missing)}",
            checked_at=now,
        )

    name = str(skill['name'])
    schema = skill.get('parameters_schema')
    schema_ok = bool(schema) and contracts.check_schema(schema) is None
    overrides = {name: _registry_entry(skill)}
    results: List[verification.CaseResult] = []
    for case in cases:
        label = str(case.get('name') or f"test {len(results) + 1}")
        parameters = case.get('parameters') or {}
        context = case.get('context') or {}
        if not isinstance(parameters, dict) or not isinstance(context, dict):
            results.append(
                verification.CaseResult(label, False, "parameters and context must be objects")
            )
            continue
        if schema_ok:
            assert isinstance(schema, dict)
            parameters = contracts.apply_defaults(schema, parameters)
            errors = contracts.validate_params(schema, parameters)
            if errors:
                results.append(
                    verification.CaseResult(label, False, "invalid parameters: " + "; ".join(errors))
                )
                continue
        outcome = await _run_code(
            code=str(skill['code']),
            context={
                'params': parameters,
                'context': context,
                '__skill_name__': name,
                '__skill_id__': skill.get('id'),
                '__skill_version__': skill.get('version', 1),
            },
            timeout=_case_timeout(case.get('timeout')),
            capture_output=True,
            enable_composition=True,
            current_skill_name=name,
            registry_overrides=overrides,
            commit_state=False,
        )
        if outcome.status != STATUS_OK:
            results.append(
                verification.CaseResult(
                    label, False, outcome.error or outcome.status, outcome.duration_ms
                )
            )
        elif 'expected_result' in case and not verification.results_match(
            case['expected_result'], outcome.result
        ):
            expected = json.dumps(case['expected_result'], default=str)[:300]
            got = json.dumps(outcome.result, default=str)[:300]
            results.append(
                verification.CaseResult(
                    label, False, f"expected result {expected}, got {got}", outcome.duration_ms
                )
            )
        else:
            results.append(verification.CaseResult(label, True, None, outcome.duration_ms))

    failed = any(not r.passed for r in results)
    return verification.VerificationReport(
        status=verification.STATUS_DRAFT if failed else verification.STATUS_VERIFIED,
        cases=results,
        checked_at=now,
    )


def _apply_gate(
    skill_data: Dict[str, Any],
    report: verification.VerificationReport,
    save_as_draft: bool,
) -> Optional[str]:
    """Decide whether a verified candidate may be saved.

    Returns an error message (nothing is saved), or None after setting the
    skill's `status` and `verification` fields.
    """
    name = skill_data['name']
    if report.failed:
        if not save_as_draft:
            failed = len(report.cases) - report.passed_count
            return (
                f"Error: Skill '{name}' was not saved: {failed} of {len(report.cases)} test "
                f"case(s) failed in the sandbox.\n\n{report.failure_block()}\n\n"
                "Fix the code or the tests, or pass save_as_draft=True to keep it as a draft."
            )
        report.status = verification.STATUS_DRAFT
    elif report.status == verification.STATUS_UNVERIFIED and REQUIRE_VERIFIED_SKILLS:
        if not save_as_draft:
            return (
                f"Error: Skill '{name}' was not saved: this server only accepts verified skills "
                f"(SKILL_ORCHESTRATOR_REQUIRE_VERIFIED_SKILLS) and it could not be verified "
                f"({report.reason}). Add test cases, or pass save_as_draft=True."
            )
        report.status = verification.STATUS_DRAFT
    skill_data['status'] = report.status
    skill_data['verification'] = report.to_record()
    return None


def _verification_text(report: verification.VerificationReport) -> str:
    text = f"**Verification:** {report.summary_line()}"
    if report.failed:
        text += "\n" + report.failure_block()
    if report.status == verification.STATUS_DRAFT:
        text += (
            "\nSaved as a **draft**. Fix it with update_skill, or re-check with verify_skill."
        )
    elif report.status == verification.STATUS_UNVERIFIED:
        text += (
            "\nTip: add `tests` (parameters + expected_result) so changes are checked "
            "before they are saved."
        )
    return text


def _coerce_tests(tests: List[Any]) -> List[Dict[str, Any]]:
    """Store test cases as plain dicts, keeping `expected_result` only if it was given."""
    out = []
    for t in tests:
        model = t if isinstance(t, SkillTestCase) else SkillTestCase.model_validate(t)
        out.append(model.model_dump(exclude_unset=True))
    return out


def _health_map(skills: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Recent failure counts for each skill's current version, with a degraded flag."""
    if not TELEMETRY.enabled or not skills:
        return {}
    versions = {
        str(s['name']): int(s.get('version', 1) or 1) for s in skills if s.get('name')
    }
    counts = TELEMETRY.recent_failures(versions, window=DEGRADED_WINDOW)
    return {
        name: {
            'recent_runs': executed,
            'recent_failures': failed,
            'degraded': verification.is_degraded(
                failed, executed, threshold=DEGRADED_FAILURE_RATE, min_runs=DEGRADED_MIN_RUNS
            ),
        }
        for name, (failed, executed) in counts.items()
    }


def _health_text(health: Optional[Dict[str, Any]], version: Any) -> Optional[str]:
    if not health or not health.get('degraded'):
        return None
    return (
        f"⚠️ degraded: {health['recent_failures']} of the last {health['recent_runs']} runs "
        f"of v{version} failed (see get_skill_stats; rollback_skill can restore a "
        "working version)"
    )


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

    Verified-save gate: the skill's test cases (`tests`, plus any example
    that is a JSON object of parameters) are run in the sandbox first. If one
    fails, the skill is not saved, unless `save_as_draft=True`, in which case
    it is saved with status 'draft'. With no runnable cases it is saved as
    'unverified'. State writes made by test runs are never committed.

    Best practices:
    - Include a main() function for entry point
    - Document parameters in the description
    - Add relevant tags for discoverability
    - Keep skills focused on a single purpose
    - Add `tests` with an expected_result so later edits are checked too

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
            - tests: Test cases run before saving
            - save_as_draft: Save as a draft even if a test fails

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
        "tests": _coerce_tests(params.tests),
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

    # Validate contracts (bad schemas / requirement strings are author errors, reject them).
    schema_error = contracts.check_schema(params.parameters_schema)
    if schema_error:
        return f"Error: {schema_error}"
    dep_errors = contracts.invalid_dependencies(params.dependencies)
    if dep_errors:
        return "Error: " + "; ".join(dep_errors)
    warnings = _contract_warnings(params.name, params.code, params.dependencies)

    # Verified-save gate: run the test cases in the sandbox before saving.
    report = await _verify_skill(skill_data)
    gate_error = _apply_gate(skill_data, report, params.save_as_draft)
    if gate_error:
        return gate_error
    if _load_skill(params.name):
        return f"Error: Skill '{params.name}' was created by someone else while it was being verified."

    # Save skill
    _save_skill(skill_data)
    SKILL_INDEX.upsert_from_skill(skill_data)
    SKILL_VERSIONS.save(skill_data)
    USAGE_TRACKER.ensure_minimums({params.name: 0})

    return f"""✅ Skill created successfully!

**Name:** {params.name}
**ID:** {skill_data['id']}
**Category:** {params.category.value}
**Tags:** {', '.join(params.tags) if params.tags else 'None'}
**Dependencies:** {', '.join(params.dependencies) if params.dependencies else 'None'}

{_verification_text(report)}

The skill is now available for invocation via `invoke_skill`.{_format_warnings(warnings)}"""


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

    version = int(skill.get('version', 1) or 1)
    warnings: List[str] = []
    if verification.skill_status(skill) == verification.STATUS_DRAFT:
        warnings.append(
            "This skill is a draft: its test cases failed when it was saved "
            "(see get_skill)."
        )

    def _reject(reason: str) -> str:
        TELEMETRY.record(
            kind=KIND_SKILL,
            status=STATUS_REJECTED,
            duration_ms=0.0,
            skill_name=params.skill_name,
            skill_version=version,
            error=reason,
        )
        return f"Error: {reason}"

    # Contract 1: Python dependencies must be importable by the runner interpreter.
    missing = contracts.missing_dependencies(skill.get('dependencies'))
    if missing:
        return _reject(
            f"Skill '{params.skill_name}' has unmet dependencies: {', '.join(missing)}. "
            f"{contracts.install_hint(missing)}"
        )

    # Contract 2: parameters must satisfy the skill's parameters_schema.
    parameters: Dict[str, Any] = dict(params.parameters or {})
    schema = skill.get('parameters_schema')
    mode = contracts.schema_enforcement_mode()
    if schema and mode != contracts.ENFORCE_OFF:
        schema_error = contracts.check_schema(schema)
        if schema_error:
            warnings.append(f"Parameters not validated: {schema_error}. Fix it with update_skill.")
        else:
            parameters = contracts.apply_defaults(schema, parameters)
            errors = contracts.validate_params(schema, parameters)
            if errors and mode == contracts.ENFORCE_STRICT:
                return _reject(
                    f"Invalid parameters for skill '{params.skill_name}':\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nUse get_skill to see its parameters_schema."
                )
            warnings.extend(f"Parameter schema violation: {e}" for e in errors)

    # Prepare execution namespace
    namespace = {
        'params': parameters,
        'context': params.context or {},
        '__skill_name__': params.skill_name,
        '__skill_id__': skill['id'],
        '__skill_version__': version,
    }

    # Execute skill code with composition enabled
    outcome = await _run_code(
        code=skill['code'],
        context=namespace,
        timeout=params.timeout,
        capture_output=params.capture_output,
        enable_composition=True,
        current_skill_name=params.skill_name
    )
    _record_outcome(outcome, kind=KIND_SKILL, skill_name=params.skill_name, skill_version=version)

    # Update invocation count without rewriting the skill JSON (write amplification).
    USAGE_TRACKER.increment(params.skill_name)

    warning_text = _format_warnings(warnings).lstrip("\n")
    full_text = f"""## Skill Execution: {params.skill_name} (v{version})

{warning_text + chr(10) + chr(10) if warning_text else ''}{outcome.text}"""

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

    Security note: Code runs in a separate Python process with a timeout and
    an allowlisted environment (no API keys or other server env vars). It is
    NOT a sandbox: it runs as the server's user, with the same filesystem and
    network access.

    Args:
        params: ExecuteCodeInput containing:
            - code: Python code to execute
            - context: Variables to inject into namespace
            - timeout: Max execution time (default 30s)
            - capture_output: Whether to capture stdout/stderr

    Returns:
        str: Execution output and results
    """
    outcome = await _run_code(
        code=params.code,
        context=params.context,
        timeout=params.timeout,
        capture_output=params.capture_output
    )
    _record_outcome(outcome, kind=KIND_CODE)
    output = outcome.text

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


@dataclass
class ExecutionOutcome:
    """Structured result of one code/skill execution (text + telemetry fields)."""

    text: str
    status: str
    duration_ms: float
    exec_ms: Optional[float] = None
    error: Optional[str] = None
    # Nested invoke_skill calls made inside the run (from the child's trace).
    trace: List[Dict[str, Any]] = field(default_factory=list)
    # Number of state-bridge writes committed (0 when the run failed).
    state_writes: int = 0
    # The run's __result__ (JSON-decoded), used by the verified-save gate.
    result: Any = None


def _record_outcome(
    outcome: ExecutionOutcome,
    *,
    kind: str,
    skill_name: Optional[str] = None,
    skill_version: Optional[int] = None,
) -> None:
    """Write telemetry for a run and every nested skill call it made."""
    TELEMETRY.record(
        kind=kind,
        status=outcome.status,
        duration_ms=outcome.duration_ms,
        skill_name=skill_name,
        skill_version=skill_version,
        error=outcome.error,
        exec_ms=outcome.exec_ms,
        state_writes=outcome.state_writes,
    )
    nested = []
    for t in outcome.trace:
        if not isinstance(t, dict) or not isinstance(t.get("skill"), str):
            continue
        nested.append(
            {
                "ts": t.get("ts"),
                "kind": KIND_NESTED,
                "skill": t["skill"],
                "version": t.get("version"),
                "status": t.get("status", STATUS_ERROR),
                "duration_ms": round(float(t.get("duration_ms") or 0.0), 1),
                **({"error": " ".join(str(t["error"]).split())[:500]} if t.get("error") else {}),
                **({"parent": t["parent"]} if t.get("parent") else {}),
                "depth": t.get("depth"),
            }
        )
    TELEMETRY.record_many(nested)


async def _execute_code_internal(
    code: str,
    context: Optional[Dict[str, Any]] = None,
    timeout: int = MAX_EXECUTION_TIME,
    capture_output: bool = True,
    enable_composition: bool = False,
    current_skill_name: Optional[str] = None
) -> str:
    """Run code and return only the formatted text (see `_run_code`)."""
    outcome = await _run_code(
        code=code,
        context=context,
        timeout=timeout,
        capture_output=capture_output,
        enable_composition=enable_composition,
        current_skill_name=current_skill_name,
    )
    return outcome.text


def _format_error_block(error: str, tb: str, context: Optional[Dict[str, Any]]) -> str:
    error_msg = f"**Error:** {error}\n```\n{tb}\n```"
    # Add helpful debugging hint
    if context:
        available_vars = [k for k in context.keys() if not k.startswith('__')]
        if available_vars:
            error_msg += f"\n**Available context variables:** {', '.join(available_vars)}"
    error_msg += "\n**Tip:** Use `debug_context(**locals())` in your skill code to inspect available variables."
    return error_msg


def _format_result_block(result: Any) -> str:
    if isinstance(result, (dict, list)):
        result_str = json.dumps(result, indent=2, default=str)
    else:
        result_str = str(result)
    return f"**Result:**\n```\n{_sanitize_code_output(result_str)}\n```"


def _run_inprocess(
    code: str, context: Optional[Dict[str, Any]], capture_output: bool
) -> ExecutionOutcome:
    """Legacy in-process exec path.

    NOTE: This does NOT enforce timeouts and has no composition or state
    bridge; kept only as a compatibility fallback.
    """
    started = time.perf_counter()
    exec_namespace: Dict[str, Any] = {
        "__builtins__": __builtins__,
        "__name__": "__main__",
        "__doc__": None,
    }
    if context:
        exec_namespace.update(context)

    imports = """
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

    helpers = """
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
{imports}
{helpers}

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
    except Exception:
        return ExecutionOutcome(
            text=f"**Execution Error:**\n```\n{traceback.format_exc()}\n```",
            status=STATUS_ERROR,
            duration_ms=(time.perf_counter() - started) * 1000,
            error=traceback.format_exc(limit=1).strip().splitlines()[-1],
        )

    stdout_content = stdout_capture.getvalue()
    stderr_content = stderr_capture.getvalue()
    error = exec_namespace.get("__error__")
    tb = exec_namespace.get("__traceback__", "")
    result = exec_namespace.get("__result__")

    output_parts: List[str] = []
    if error:
        output_parts.append(_format_error_block(str(error), str(tb), context))
    if stdout_content:
        output_parts.append(f"**Output:**\n```\n{_sanitize_code_output(stdout_content)}\n```")
    if stderr_content:
        output_parts.append(f"**Stderr:**\n```\n{_sanitize_code_output(stderr_content)}\n```")
    if result is not None:
        output_parts.append(_format_result_block(result))
    if not output_parts:
        output_parts.append("*Code executed successfully with no output.*")

    return ExecutionOutcome(
        text="\n\n".join(output_parts),
        status=STATUS_ERROR if error else STATUS_OK,
        duration_ms=(time.perf_counter() - started) * 1000,
        error=str(error) if error else None,
        result=result,
    )


# -----------------------------------------------------------------------------
# Subprocess runner sources
# -----------------------------------------------------------------------------

_COMMON_IMPORTS = (
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
_HELPER_FUNCTIONS = '''
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
# State Bridge
# =============================================================================
# Skills can read and write the same persistent state as the get_state /
# set_state MCP tools. Reads come straight from the state files. Writes are
# buffered in _state_ops and committed by the server only if the whole run
# succeeds, so a failing skill never leaves half-written state behind. A
# nested invoke_skill that raises has its own writes rolled back too.
# _state_dir, _state_ops and _state_cache are injected by the runner.

def _state_check(key, namespace):
    bad_chars = ('/', chr(92), ':', chr(0))
    if (not isinstance(namespace, str) or not namespace or namespace.startswith('.')
            or len(namespace) > 50 or any(c in namespace for c in bad_chars)):
        raise ValueError(f"Invalid state namespace: {namespace!r}")
    if key is not None and (not isinstance(key, str) or not key or len(key) > 200):
        raise ValueError(f"Invalid state key: {key!r}")


def _state_nested_set(data, key, value):
    parts = key.split('.')
    for i, k in enumerate(parts[:-1]):
        child = data.setdefault(k, {})
        if not isinstance(child, dict):
            where = '.'.join(parts[:i + 1])
            raise ValueError(
                f"Cannot set '{key}': '{where}' holds a {type(child).__name__}, not an object"
            )
        data = child
    data[parts[-1]] = value


def _state_nested_delete(data, key):
    parts = key.split('.')
    for k in parts[:-1]:
        data = data.get(k) if isinstance(data, dict) else None
        if not isinstance(data, dict):
            return False
    if isinstance(data, dict) and parts[-1] in data:
        del data[parts[-1]]
        return True
    return False


def _state_apply(data, op):
    import copy as _copy
    if op['op'] == 'set':
        _state_nested_set(data, op['key'], _copy.deepcopy(op['value']))
        return True
    return _state_nested_delete(data, op['key'])


def _state_namespace_data(namespace):
    import json as _json
    import os as _os
    g = globals()
    cache = g['_state_cache']
    if namespace not in cache:
        data = {}
        state_dir = g.get('_state_dir')
        if state_dir:
            path = _os.path.join(state_dir, namespace + '.json')
            if _os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    loaded = _json.load(f)
                if isinstance(loaded, dict):
                    data = loaded
        # Read-your-writes: replay this run's pending ops on top of disk state.
        for op in g['_state_ops']:
            if op['namespace'] == namespace:
                _state_apply(data, op)
        cache[namespace] = data
    return cache[namespace]


def get_state(key, namespace='default', default=None):
    """Read persistent state (same store as the get_state MCP tool).

    Returns `default` when the key is missing. Dot notation works: 'a.b.c'.
    """
    import copy as _copy
    _state_check(key, namespace)
    node = _state_namespace_data(namespace)
    for k in key.split('.'):
        if isinstance(node, dict) and k in node:
            node = node[k]
        else:
            return default
    return _copy.deepcopy(node)


def set_state(key, value, namespace='default'):
    """Write persistent state. Committed only if the whole run succeeds."""
    import json as _json
    _state_check(key, namespace)
    try:
        value = _json.loads(_json.dumps(value))
    except (TypeError, ValueError) as e:
        raise ValueError(f"State value for '{key}' is not JSON-serializable: {e}") from None
    op = {'op': 'set', 'namespace': namespace, 'key': key, 'value': value}
    _state_apply(_state_namespace_data(namespace), op)  # validates the path first
    globals()['_state_ops'].append(op)


def delete_state(key, namespace='default'):
    """Delete a state key. Returns True if it existed. Committed only on success."""
    _state_check(key, namespace)
    op = {'op': 'delete', 'namespace': namespace, 'key': key}
    existed = _state_apply(_state_namespace_data(namespace), op)
    if existed:
        globals()['_state_ops'].append(op)
    return existed


def list_state_keys(namespace='default'):
    """Flattened dot-notation keys of every leaf value in a namespace."""
    _state_check(None, namespace)
    out = []

    def walk(d, prefix):
        for k, v in d.items():
            full = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict) and v:
                walk(v, full)
            else:
                out.append(full)

    walk(_state_namespace_data(namespace), '')
    return sorted(out)


# =============================================================================
# Skill Composition System
# =============================================================================
# Note: _skill_call_stack, _skill_registry, _max_skill_call_depth, _skill_trace
# and _schema_enforcement are injected by the runner before these helpers are
# exec'd. Do NOT initialize them here as that would overwrite the runner's values.

def _trace_call(skill_name, skill, status, started, error=None, pushed=True):
    """Record one nested invoke_skill call for telemetry.

    `pushed` says whether skill_name is already on the call stack (False for
    calls refused before they started).
    """
    import datetime as _dt
    import time as _time
    g = globals()
    stack = g.get('_skill_call_stack', [])
    callers = stack[:-1] if pushed else stack
    g.setdefault('_skill_trace', []).append({
        'ts': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'skill': skill_name,
        'version': (skill or {}).get('version'),
        'parent': callers[-1] if callers else None,
        'depth': len(callers) + 1,
        'status': status,
        'duration_ms': (_time.perf_counter() - started) * 1000.0,
        'error': error,
    })


def _check_skill_params(skill_name, skill, parameters):
    """Fill schema defaults and enforce parameters_schema for a nested call."""
    schema = skill.get('parameters_schema')
    mode = globals().get('_schema_enforcement', 'strict')
    params = dict(parameters or {})
    if not schema or mode == 'off' or not skill.get('schema_valid', True):
        return params
    for key, prop in (schema.get('properties') or {}).items():
        if key not in params and isinstance(prop, dict) and 'default' in prop:
            params[key] = prop['default']
    import jsonschema as _jsonschema
    errors = sorted(
        _jsonschema.Draft202012Validator(schema).iter_errors(params),
        key=lambda e: list(e.path),
    )
    if errors:
        msgs = []
        for e in errors[:10]:
            where = '.'.join(str(p) for p in e.path)
            msgs.append(f"{where}: {e.message}" if where else e.message)
        if mode == 'strict':
            raise ValueError(f"Invalid parameters for skill '{skill_name}': " + '; '.join(msgs))
        import sys as _sys
        print(f"[warning] parameters for '{skill_name}' violate its schema: " + '; '.join(msgs),
              file=_sys.stderr)
    return params


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
        RuntimeError: If skill not found, max depth exceeded, circular call
            detected, or the skill's dependencies are not installed
        ValueError: If parameters fail the skill's parameters_schema

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
    import time as _time
    started = _time.perf_counter()

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

    # Look up skill in registry
    if skill_name not in skill_registry:
        available = list(skill_registry.keys())[:10]
        raise RuntimeError(
            f"Skill '{skill_name}' not found. "
            f"Available skills: {', '.join(available)}{'...' if len(skill_registry) > 10 else ''}"
        )
    skill = skill_registry[skill_name]

    # Check call depth
    if len(skill_call_stack) >= max_call_depth:
        msg = (
            f"Maximum skill call depth ({max_call_depth}) exceeded. "
            f"Call stack: {' -> '.join(skill_call_stack)}"
        )
        _trace_call(skill_name, skill, 'rejected', started, msg, pushed=False)
        raise RuntimeError(msg)

    # Check for circular calls
    if skill_name in skill_call_stack:
        msg = (
            f"Circular skill invocation detected: {skill_name}. "
            f"Call stack: {' -> '.join(skill_call_stack)} -> {skill_name}"
        )
        _trace_call(skill_name, skill, 'rejected', started, msg, pushed=False)
        raise RuntimeError(msg)

    # Enforce the skill's contracts before running it.
    missing = skill.get('missing_dependencies') or []
    if missing:
        msg = f"Skill '{skill_name}' has unmet dependencies: {', '.join(missing)}"
        _trace_call(skill_name, skill, 'rejected', started, msg, pushed=False)
        raise RuntimeError(msg)
    try:
        parameters = _check_skill_params(skill_name, skill, parameters)
    except ValueError as e:
        _trace_call(skill_name, skill, 'rejected', started, str(e), pushed=False)
        raise

    skill_code = skill.get('code', '')

    if not skill_code:
        raise RuntimeError(f"Skill '{skill_name}' has no code defined.")

    # Push onto call stack
    skill_call_stack.append(skill_name)
    state_mark = len(g['_state_ops'])
    status = 'ok'
    error = None

    try:
        # Create execution namespace for the child skill
        child_namespace = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
            '__doc__': None,
            '__skill_name__': skill_name,
            '__skill_version__': skill.get('version'),
            '__parent_skill__': skill_call_stack[-2] if len(skill_call_stack) > 1 else None,
            '__call_depth__': len(skill_call_stack),
            'params': parameters,
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
            # State bridge
            'get_state': get_state,
            'set_state': set_state,
            'delete_state': delete_state,
            'list_state_keys': list_state_keys,
        }

        # Execute the skill code
        exec(skill_code, child_namespace)

        # Return the result
        return child_namespace.get('__result__')

    except BaseException as e:
        status = 'error'
        error = f"{type(e).__name__}: {e}"
        # Roll back this call's state writes so a caller that catches the
        # exception doesn't commit a half-finished child's changes.
        del g['_state_ops'][state_mark:]
        g['_state_cache'].clear()
        raise

    finally:
        _trace_call(skill_name, skill, status, started, error)
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
            'version': skill.get('version'),
            'parameters_schema': skill.get('parameters_schema'),
            'dependencies': skill.get('dependencies', []),
            'missing_dependencies': skill.get('missing_dependencies', []),
        }
    return None
'''

_RUNNER_SOURCE = """
import json
import sys
import time
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

    # Keep our own references: skill code may rebind these names.
    state_ops = []
    skill_trace = []

    error = None
    error_type = None
    tb = None
    result = None
    exec_ms = None

    try:
        # Execute common imports first
        exec(common_imports, namespace)

        # Initialize skill composition system BEFORE helpers (so invoke_skill can access them)
        namespace["_skill_registry"] = skill_registry
        namespace["_max_skill_call_depth"] = max_call_depth
        namespace["_skill_call_stack"] = [current_skill_name] if current_skill_name else []
        namespace["_skill_trace"] = skill_trace
        namespace["_schema_enforcement"] = payload.get("schema_enforcement", "strict")

        # State bridge
        namespace["_state_dir"] = payload.get("state_dir")
        namespace["_state_ops"] = state_ops
        namespace["_state_cache"] = {}

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
        started = time.perf_counter()
        try:
            exec(code, namespace)
        finally:
            exec_ms = (time.perf_counter() - started) * 1000.0
        result = namespace.get("__result__")
    except Exception as e:
        error = str(e)
        error_type = type(e).__name__
        tb = traceback.format_exc()

    out = {
        "error": error,
        "error_type": error_type,
        "traceback": tb,
        "result": result,
        "exec_ms": exec_ms,
        # A failed run commits nothing, so don't ship its writes back.
        "state_ops": state_ops if error is None else [],
        "state_ops_discarded": len(state_ops) if error is not None else 0,
        "trace": skill_trace,
    }
    if result_path:
        try:
            with open(result_path, "w", encoding="utf-8") as rf:
                json.dump(out, rf, indent=2, default=str)
        except Exception:
            pass

if __name__ == "__main__":
    main()
""".lstrip()


async def _run_code(
    code: str,
    context: Optional[Dict[str, Any]] = None,
    timeout: int = MAX_EXECUTION_TIME,
    capture_output: bool = True,
    enable_composition: bool = False,
    current_skill_name: Optional[str] = None,
    registry_overrides: Optional[Dict[str, Dict[str, Any]]] = None,
    commit_state: bool = True,
) -> ExecutionOutcome:
    """Internal code execution implementation.

    Default behavior executes code in a subprocess boundary so timeouts are real.
    Set env var SKILL_ORCHESTRATOR_EXEC_MODE=inprocess to use the legacy in-process
    exec() path (not recommended for untrusted code).

    `registry_overrides` replaces registry entries for this run (used to test
    a skill that is not saved yet). With `commit_state=False`, state-bridge
    writes are dropped even when the run succeeds (verification runs).
    """

    exec_mode = os.environ.get("SKILL_ORCHESTRATOR_EXEC_MODE", "subprocess").strip().lower()
    if exec_mode == "inprocess":
        return _run_inprocess(code, context, capture_output)

    # -------------------- Subprocess execution path (default) --------------------
    started = time.perf_counter()

    def _elapsed_ms() -> float:
        return (time.perf_counter() - started) * 1000.0

    try:
        with tempfile.TemporaryDirectory(
            prefix="skill_orchestrator_exec_",
            ignore_cleanup_errors=True,
        ) as tmp:
            tmp_dir = Path(tmp)
            runner_path = tmp_dir / "runner.py"
            payload_path = tmp_dir / "payload.json"
            result_path = tmp_dir / "result.json"

            runner_path.write_text(_RUNNER_SOURCE, encoding="utf-8")

            payload = {
                "code": code,
                "context": context or {},
                "result_path": str(result_path),
                "common_imports": _COMMON_IMPORTS,
                "helpers": _HELPER_FUNCTIONS,
                "skill_registry": (
                    {**_build_skill_registry(), **(registry_overrides or {})}
                    if enable_composition
                    else {}
                ),
                "max_call_depth": MAX_SKILL_CALL_DEPTH,
                "current_skill_name": current_skill_name,
                "state_dir": str(DEFAULT_STATE_DIR),
                "schema_enforcement": contracts.schema_enforcement_mode(),
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
                # Never inherit the server's environment: it may hold the
                # client's API keys. Only allowlisted names reach the child.
                env=sandbox_env.build_child_env(),
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
                # Normalize Windows CRLF so output (and truncation counts) match across platforms.
                stdout_content = (
                    (stdout_b or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
                )
                stderr_content = (
                    (stderr_b or b"").decode("utf-8", errors="replace").replace("\r\n", "\n")
                )

            error = None
            error_type: Optional[str] = None
            tb = ""
            result_obj: Any = None
            exec_ms: Optional[float] = None
            state_ops: List[Dict[str, Any]] = []
            discarded = 0
            trace: List[Dict[str, Any]] = []
            if result_path.exists():
                try:
                    result_payload = json.loads(result_path.read_text(encoding="utf-8"))
                    error = result_payload.get("error")
                    error_type = result_payload.get("error_type")
                    tb = result_payload.get("traceback") or ""
                    result_obj = result_payload.get("result")
                    exec_ms = result_payload.get("exec_ms")
                    state_ops = list(result_payload.get("state_ops") or [])
                    discarded = int(result_payload.get("state_ops_discarded") or 0)
                    trace = list(result_payload.get("trace") or [])
                except Exception:
                    error = "Failed to read execution result payload"

            # The runner only catches Exception, so sys.exit() or a hard crash
            # (os._exit, segfault, OOM kill) ends the child without a result.
            if not timed_out and error is None and proc.returncode not in (0, None):
                error = (
                    f"Process terminated with exit code {proc.returncode} "
                    "before reporting a result"
                )

            # Commit buffered state writes only for a clean, completed run.
            state_note = ""
            state_writes = 0
            if timed_out or error is not None:
                discarded += len(state_ops)
                if discarded:
                    state_note = (
                        f"**State:** {discarded} pending write(s) discarded because the run failed."
                    )
            elif state_ops and not commit_state:
                state_note = (
                    f"**State:** {len(state_ops)} write(s) not committed (verification run)."
                )
            elif state_ops:
                try:
                    counts = _commit_state_ops(state_ops)
                except Exception as e:
                    error = f"State commit failed, no state was written: {e}"
                    error_type = "StateCommitError"
                else:
                    state_writes = sum(counts.values())
                    state_note = f"**State:** committed {state_writes} write(s) to " + ", ".join(
                        f"'{ns}' ({n})" for ns, n in sorted(counts.items())
                    )

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
                output_parts.append(_format_error_block(str(error), tb, context))

            if stdout_content:
                output_parts.append(f"**Output:**\n```\n{_sanitize_code_output(stdout_content)}\n```")

            if stderr_content:
                output_parts.append(f"**Stderr:**\n```\n{_sanitize_code_output(stderr_content)}\n```")

            if result_obj is not None:
                output_parts.append(_format_result_block(result_obj))

            if state_note:
                output_parts.append(state_note)

            if not output_parts:
                output_parts.append("*Code executed successfully with no output.*")

            if timed_out:
                status = STATUS_TIMEOUT
                error_summary: Optional[str] = f"Timed out after {timeout}s"
            elif error:
                status = STATUS_ERROR
                error_summary = f"{error_type}: {error}" if error_type else str(error)
            else:
                status = STATUS_OK
                error_summary = None

            return ExecutionOutcome(
                text="\n\n".join(output_parts),
                status=status,
                duration_ms=_elapsed_ms(),
                exec_ms=exec_ms,
                error=error_summary,
                trace=trace,
                state_writes=state_writes,
                result=result_obj,
            )
    except Exception:
        return ExecutionOutcome(
            text=f"**Execution Error:**\n```\n{traceback.format_exc()}\n```",
            status=STATUS_ERROR,
            duration_ms=_elapsed_ms(),
            error=traceback.format_exc(limit=1).strip().splitlines()[-1],
        )


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
            or query_lower in str(s.get('description') or '').lower()
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
    health = _health_map(page)
    page_with_usage: List[Dict[str, Any]] = []
    for meta in page:
        name = str(meta.get("name", ""))
        h = health.get(name) or {}
        page_with_usage.append(
            {
                **meta,
                "invocation_count": USAGE_TRACKER.get(name),
                "status": verification.skill_status(meta),
                "degraded": bool(h.get("degraded")),
                "recent_runs": h.get("recent_runs", 0),
                "recent_failures": h.get("recent_failures", 0),
            }
        )
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
        lines.append(f"*{skill.get('description') or 'No description'}*\n")
        lines.append(f"- **Status:** {skill['status']}")
        health_line = _health_text(health.get(str(skill['name'])), skill.get('version', 1))
        if health_line:
            lines.append(f"- **Health:** {health_line}")
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
async def get_skill(name: str, version: Optional[int] = None) -> str:
    """Get full details of a specific skill including code.

    Use this to load a skill's complete definition when you need to:
    - Review the implementation
    - Understand parameters
    - Modify or extend the skill

    Args:
        name: The name of the skill to retrieve
        version: Optional past version to show instead of the current one
            (see list_skill_versions)

    Returns:
        str: Full skill details in markdown format
    """
    current = _load_skill(name)
    if not current:
        return f"Error: Skill '{name}' not found. Use search_skills to find available skills."

    skill = current
    if version is not None and version != current.get('version', 1):
        snapshot = SKILL_VERSIONS.get(name, version)
        if snapshot is None:
            available = ", ".join(f"v{v}" for v in SKILL_VERSIONS.versions(name)) or "none"
            return f"Error: Skill '{name}' has no stored version {version}. Available: {available}."
        skill = snapshot

    # Overlay authoritative usage count.
    skill["invocation_count"] = USAGE_TRACKER.get(name)
    shown_version = skill.get('version', 1)
    is_current = shown_version == current.get('version', 1)

    lines = [
        f"# Skill: {skill['name']}" + ("" if is_current else f" (historical v{shown_version})"),
        f"\n{skill['description']}\n",
        "## Metadata",
        f"- **ID:** {skill.get('id', 'unknown')}",
        f"- **Category:** {skill.get('category', 'custom')}",
        f"- **Version:** {shown_version}"
        + ("" if is_current else f" (current is v{current.get('version', 1)})"),
        f"- **Tags:** {', '.join(skill.get('tags', [])) or 'None'}",
        f"- **Dependencies:** {', '.join(skill.get('dependencies', [])) or 'None'}",
        f"- **Created:** {skill.get('created_at', 'unknown')}",
        f"- **Updated:** {skill.get('updated_at', 'unknown')}",
        f"- **Invocations:** {skill.get('invocation_count', 0)}",
    ]

    status_line = f"- **Status:** {verification.skill_status(skill)}"
    record = skill.get('verification')
    if isinstance(record, dict) and record.get('cases'):
        status_line += (
            f" ({record.get('passed', 0)}/{record['cases']} test case(s) passed, "
            f"checked {str(record.get('checked_at') or '')[:19]})"
        )
    elif isinstance(record, dict) and record.get('reason'):
        status_line += f" ({record['reason']})"
    lines.append(status_line)
    if isinstance(record, dict):
        for failure in record.get('failures') or []:
            lines.append(f"  - failed: {failure.get('case')}: {failure.get('error')}")
    if is_current:
        health_line = _health_text(_health_map([current]).get(name), shown_version)
        if health_line:
            lines.append(f"- **Health:** {health_line}")

    if skill.get('change_note'):
        lines.append(f"- **Change note:** {skill['change_note']}")

    missing = contracts.missing_dependencies(skill.get('dependencies'))
    if missing:
        lines.append(f"- **Unmet dependencies:** {', '.join(missing)}")

    refs = contracts.find_skill_refs(str(skill.get('code', '')))
    if refs:
        lines.append(f"- **Calls skills:** {', '.join(refs)}")

    history = SKILL_VERSIONS.versions(name)
    if history:
        lines.append(f"- **Stored versions:** {', '.join(f'v{v}' for v in history)}")

    stats = TELEMETRY.skill_stats(name) if TELEMETRY.enabled else None
    if stats and stats["runs"]:
        lines.append(f"- **Telemetry:** {_stats_one_liner(stats)}")

    if skill.get('parameters_schema'):
        lines.append(f"\n## Parameters Schema\n```json\n{json.dumps(skill['parameters_schema'], indent=2)}\n```")

    if skill.get('examples'):
        lines.append("\n## Examples")
        for ex in skill['examples']:
            lines.append(f"- {ex}")

    if skill.get('tests'):
        lines.append(
            f"\n## Tests\n```json\n{json.dumps(skill['tests'], indent=2, default=str)}\n```"
        )

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
    dependencies: Optional[List[str]] = None,
    parameters_schema: Optional[Dict[str, Any]] = None,
    category: Optional[str] = None,
    examples: Optional[List[str]] = None,
    expected_version: Optional[int] = None,
    change_note: Optional[str] = None,
    tests: Optional[List[SkillTestCase]] = None,
    save_as_draft: bool = False,
) -> str:
    """Update an existing skill's properties.

    Only provided fields will be updated; others remain unchanged. Each
    update saves a new version and keeps the previous ones, so a bad edit can
    be undone with `rollback_skill`. Compare versions with
    `diff_skill_versions` and check error rates per version with
    `get_skill_stats`.

    Verified-save gate: if the code, schema, dependencies, tests or examples
    change, the skill's test cases are run in the sandbox first. If one fails
    the update is not saved, unless `save_as_draft=True`.

    Args:
        name: Name of the skill to update
        code: New Python code (optional)
        description: New description (optional)
        tags: New tags list (optional)
        dependencies: New Python requirement strings, e.g. ["pandas>=2"] (optional)
        parameters_schema: New JSON Schema for params (optional; pass {} to remove it)
        category: New category (optional, see list_categories)
        examples: New example usage strings (optional)
        expected_version: If set, the update is refused unless the skill is
            currently at this version (guards against overwriting a
            concurrent edit by another agent)
        change_note: Short note on why the skill changed, kept with the version
        tests: New test cases (optional; replaces the stored ones, [] removes them)
        save_as_draft: Save even if a test fails, marking the skill 'draft'

    Returns:
        str: Confirmation of update
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."

    current_version = int(skill.get('version', 1) or 1)
    if expected_version is not None and expected_version != current_version:
        return (
            f"Error: Version conflict for '{name}': expected v{expected_version} but it is at "
            f"v{current_version}. Reload it with get_skill and reapply your change."
        )

    updated = dict(skill)

    # Validate and apply fields
    if code is not None:
        try:
            compile(code, f"skill_{name}", 'exec')
        except SyntaxError as e:
            return f"Error: Invalid Python syntax: {e}"
        updated['code'] = code

    if description is not None:
        updated['description'] = description

    if tags is not None:
        updated['tags'] = tags

    if dependencies is not None:
        dep_errors = contracts.invalid_dependencies(dependencies)
        if dep_errors:
            return "Error: " + "; ".join(dep_errors)
        updated['dependencies'] = dependencies

    if parameters_schema is not None:
        schema_error = contracts.check_schema(parameters_schema)
        if schema_error:
            return f"Error: {schema_error}"
        updated['parameters_schema'] = parameters_schema or None

    if category is not None:
        try:
            updated['category'] = SkillCategory(category).value
        except ValueError:
            valid = ", ".join(c.value for c in SkillCategory)
            return f"Error: Unknown category '{category}'. Valid categories: {valid}."

    if examples is not None:
        updated['examples'] = examples

    if tests is not None:
        if len(tests) > verification.MAX_TEST_CASES:
            return f"Error: At most {verification.MAX_TEST_CASES} test cases are allowed."
        try:
            updated['tests'] = _coerce_tests(list(tests))
        except ValueError as e:
            return f"Error: Invalid test case: {e}"

    if all(updated.get(f) == skill.get(f) for f in CONTENT_FIELDS):
        return f"No changes: skill '{name}' is unchanged at version {current_version}."

    report: Optional[verification.VerificationReport] = None
    if any(updated.get(f) != skill.get(f) for f in verification.BEHAVIOR_FIELDS):
        report = await _verify_skill({**updated, 'version': current_version + 1})
        gate_error = _apply_gate(updated, report, save_as_draft)
        if gate_error:
            return gate_error.replace("was not saved", "was not updated", 1)
        latest = _load_skill(name)
        if not latest or int(latest.get('version', 1) or 1) != current_version:
            return (
                f"Error: Skill '{name}' changed while the update was being verified. "
                "Reload it with get_skill and reapply your change."
            )

    # Keep the pre-edit content restorable even for skills created before
    # version history existed.
    SKILL_VERSIONS.ensure_snapshot(skill)

    # Update metadata
    updated['updated_at'] = datetime.now(timezone.utc).isoformat()
    updated['version'] = current_version + 1
    updated.pop('rolled_back_from', None)
    if change_note:
        updated['change_note'] = change_note
    else:
        updated.pop('change_note', None)

    _save_skill(updated)
    SKILL_INDEX.upsert_from_skill(updated)
    SKILL_VERSIONS.save(updated)

    warnings = _contract_warnings(
        name, str(updated.get('code', '')), list(updated.get('dependencies') or [])
    )
    verified_text = f"\n\n{_verification_text(report)}" if report else ""
    return (
        f"✅ Skill '{name}' updated to version {updated['version']}. "
        f"Undo with rollback_skill(name='{name}', version={current_version})."
        f"{verified_text}{_format_warnings(warnings)}"
    )


@mcp.tool(
    name="verify_skill",
    annotations=ToolAnnotations(
        title="Re-run a Skill's Test Cases",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True
    )
)
async def verify_skill(name: str) -> str:
    """Run a saved skill's test cases in the sandbox and update its status.

    Use after installing a missing dependency, or to promote a draft once
    the cause of its failure is fixed outside the skill. Does not create a
    new version. State writes made by the tests are not committed.

    Args:
        name: Skill name

    Returns:
        str: Per-case results and the new status (verified, draft or unverified)
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."
    version = int(skill.get('version', 1) or 1)
    report = await _verify_skill(skill)
    if report.failed:
        report.status = verification.STATUS_DRAFT

    latest = _load_skill(name)
    if not latest or int(latest.get('version', 1) or 1) != version:
        return f"Error: Skill '{name}' changed during verification; run verify_skill again."
    latest['status'] = report.status
    latest['verification'] = report.to_record()
    _save_skill(latest)
    SKILL_INDEX.upsert_from_skill(latest)
    SKILL_VERSIONS.save(latest)

    lines = [f"## Verification: {name} (v{version})", "", _verification_text(report)]
    if report.cases:
        lines += ["", "| Case | Result | ms |", "|---|---|---|"]
        for c in report.cases:
            lines.append(
                f"| {c.name} | {'pass' if c.passed else 'FAIL'} | {_fmt_ms(c.duration_ms)} |"
            )
    return "\n".join(lines)


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
async def delete_skill(name: str, confirm: bool = False, force: bool = False) -> str:
    """Delete a skill permanently, including its version history and telemetry.

    WARNING: This action cannot be undone. Set confirm=True to proceed. If
    other skills call this one via invoke_skill, deletion is refused unless
    force=True (those skills would start failing).

    Args:
        name: Name of the skill to delete
        confirm: Must be True to confirm deletion
        force: Delete even if other skills depend on this one

    Returns:
        str: Confirmation or error message
    """
    if not confirm:
        return f"⚠️ To delete skill '{name}', call delete_skill with confirm=True"

    if not _is_safe_storage_name(name):
        return f"Error: Invalid skill name '{name}'."

    path = _get_skill_path(name)
    if not path.exists():
        return f"Error: Skill '{name}' not found."

    dependents = _skill_dependents(name)
    if dependents and not force:
        return (
            f"Error: Skill '{name}' is called by: {', '.join(dependents)}. Deleting it would "
            "break them. Update those skills first, or pass force=True to delete anyway."
        )

    path.unlink()
    SKILL_INDEX.remove(name)
    USAGE_TRACKER.remove(name)
    SKILL_VERSIONS.remove_all(name)
    TELEMETRY.clear(name)
    msg = f"✅ Skill '{name}' has been permanently deleted."
    if dependents:
        msg += f"\n\n**Warning:** these skills still call it and will fail: {', '.join(dependents)}"
    return msg


# =============================================================================
# Portability: SKILL.md (agentskills.io) export / import
# =============================================================================

def _portable_path(raw: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve a client-supplied folder path; enforce SKILL_ORCHESTRATOR_PORTABLE_ROOT if set."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return None, f"Error: Path must be absolute, got '{raw}'."
    path = path.resolve()
    root_raw = os.environ.get("SKILL_ORCHESTRATOR_PORTABLE_ROOT", "").strip()
    if root_raw:
        root = Path(root_raw).expanduser().resolve()
        if path != root and root not in path.parents:
            return None, (
                f"Error: '{path}' is outside the allowed folder '{root}' "
                "(SKILL_ORCHESTRATOR_PORTABLE_ROOT)."
            )
    return path, None


@mcp.tool(
    name="export_skill",
    annotations=ToolAnnotations(
        title="Export a Skill as a SKILL.md Folder",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def export_skill(name: str, directory: str, overwrite: bool = False) -> str:
    """Write a skill as an agentskills.io folder (SKILL.md + scripts/) other agents can use.

    Creates `<directory>/<skill-name>/` with SKILL.md (frontmatter name and
    description, usage instructions), scripts/<name>.py (the code),
    scripts/run.py (runs it standalone), examples/examples.json (examples
    and test cases) and references/parameters.schema.json (if any).

    Args:
        name: Skill to export
        directory: Absolute path of the parent folder to write into
        overwrite: Replace those files if the skill folder already exists
            (other files in it are left alone)

    Returns:
        str: The folder and files written
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."
    target, error = _portable_path(directory)
    if error or target is None:
        return error or "Error: invalid directory."
    try:
        root, written = skill_md.export_skill_dir(
            skill, target, common_imports=_COMMON_IMPORTS, overwrite=overwrite
        )
    except (skill_md.SkillMdError, OSError) as e:
        return f"Error: Could not export '{name}': {e}"
    files = "\n".join(f"- `{p.relative_to(root).as_posix()}`" for p in written)
    return (
        f"✅ Exported '{name}' (v{skill.get('version', 1)}) to `{root}`\n\n{files}\n\n"
        f"Import it elsewhere with import_skill(path='{root}')."
    )


@mcp.tool(
    name="import_skill",
    annotations=ToolAnnotations(
        title="Import a Skill from a SKILL.md Folder",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False
    )
)
async def import_skill(
    path: str, name: Optional[str] = None, save_as_draft: bool = False
) -> str:
    """Create a skill from an agentskills.io folder (SKILL.md + scripts/*.py).

    Reads frontmatter name/description, the code (metadata.entrypoint, or the
    single script in scripts/), and, when present, examples/examples.json and
    references/parameters.schema.json. The skill goes through create_skill,
    so its test cases run in the sandbox before it is saved.

    Args:
        path: Absolute path of the skill folder (or its SKILL.md)
        name: Save under this name instead of the one in SKILL.md
        save_as_draft: Save even if a test case fails (status 'draft')

    Returns:
        str: create_skill's result, or why the folder could not be read
    """
    source, error = _portable_path(path)
    if error or source is None:
        return error or "Error: invalid path."
    try:
        fields = skill_md.read_skill_dir(source)
    except (skill_md.SkillMdError, OSError, UnicodeDecodeError) as e:
        return f"Error: Could not read skill folder '{source}': {e}"
    if name:
        fields["name"] = name
    try:
        fields["category"] = SkillCategory(fields["category"])
    except ValueError:
        fields["category"] = SkillCategory.CUSTOM
    if len(fields["description"]) > 1000:
        fields["description"] = fields["description"][:999] + "…"
    try:
        params = CreateSkillInput(**fields, save_as_draft=save_as_draft)
    except ValueError as e:
        return f"Error: '{source}' is not a valid skill: {e}"
    return str(await create_skill(params))


# =============================================================================
# Versioning Tools
# =============================================================================

@mcp.tool(
    name="list_skill_versions",
    annotations=ToolAnnotations(
        title="List Skill Version History",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def list_skill_versions(
    name: str, response_format: ResponseFormat = ResponseFormat.MARKDOWN
) -> str:
    """List a skill's stored versions with per-version run stats.

    Use this after editing a skill to see whether the new version fails more
    often than the old one, then `rollback_skill` if it does.

    Args:
        name: Skill name
        response_format: 'markdown' or 'json'

    Returns:
        str: Versions (oldest first) with timestamp, code hash, change note
        and telemetry (runs, failure rate, p50 latency)
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."

    current_version = int(skill.get('version', 1) or 1)
    history = SKILL_VERSIONS.history(name)
    if not any(h["version"] == current_version for h in history):
        # Legacy skill without a snapshot of its current version.
        history.append(
            {
                "version": current_version,
                "updated_at": skill.get("updated_at"),
                "code_sha": code_digest(str(skill.get("code", ""))),
                "code_lines": len(str(skill.get("code", "")).splitlines()),
                "change_note": skill.get("change_note"),
                "rolled_back_from": skill.get("rolled_back_from"),
            }
        )
    by_version = TELEMETRY.skill_stats(name)["by_version"] if TELEMETRY.enabled else {}
    for h in history:
        h["current"] = h["version"] == current_version
        s = by_version.get(str(h["version"]))
        h["stats"] = (
            {k: s[k] for k in ("runs", "failure_rate", "p50_ms", "last_error")} if s else None
        )

    if response_format == ResponseFormat.JSON:
        return json.dumps(
            {"skill": name, "current_version": current_version, "versions": history},
            indent=2,
            default=str,
        )

    lines = [
        f"## Versions of '{name}' (current: v{current_version})",
        "",
        "| Version | Updated | Code | Runs | Failure rate | p50 ms | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for h in history:
        s = h["stats"] or {}
        note = h.get("change_note") or (
            f"rollback to v{h['rolled_back_from']}" if h.get("rolled_back_from") else ""
        )
        lines.append(
            f"| v{h['version']}{' (current)' if h['current'] else ''} "
            f"| {str(h.get('updated_at') or '')[:19]} "
            f"| `{h['code_sha']}` ({h['code_lines']} lines) "
            f"| {s.get('runs', 0)} "
            f"| {_fmt_rate(s.get('failure_rate'))} "
            f"| {_fmt_ms(s.get('p50_ms'))} "
            f"| {note} |"
        )
    lines.append("\nUse diff_skill_versions to compare and rollback_skill to restore a version.")
    return "\n".join(lines)


@mcp.tool(
    name="diff_skill_versions",
    annotations=ToolAnnotations(
        title="Diff Two Skill Versions",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def diff_skill_versions(
    name: str, from_version: int, to_version: Optional[int] = None
) -> str:
    """Show what changed between two versions of a skill.

    Args:
        name: Skill name
        from_version: Older version number
        to_version: Newer version number (default: current version)

    Returns:
        str: Metadata changes plus a unified diff of the code
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."
    current_version = int(skill.get('version', 1) or 1)
    target = to_version if to_version is not None else current_version

    def _resolve(v: int) -> Optional[Dict[str, Any]]:
        if v == current_version:
            return skill
        return SKILL_VERSIONS.get(name, v)

    old, new = _resolve(from_version), _resolve(target)
    for v, snap in ((from_version, old), (target, new)):
        if snap is None:
            available = ", ".join(f"v{x}" for x in SKILL_VERSIONS.versions(name)) or "none"
            return f"Error: Skill '{name}' has no stored version {v}. Available: {available}."
    assert old is not None and new is not None
    body = diff_skills(old, new, old_label=f"{name}@v{from_version}", new_label=f"{name}@v{target}")
    return f"## {name}: v{from_version} → v{target}\n\n{body}"


@mcp.tool(
    name="rollback_skill",
    annotations=ToolAnnotations(
        title="Roll a Skill Back to an Earlier Version",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False
    )
)
async def rollback_skill(name: str, version: int, expected_version: Optional[int] = None) -> str:
    """Restore a skill's content from an earlier version.

    Non-destructive: the restored content is saved as a new version
    (rolling back from v5 to v2 creates v6 = v2's content), so the rollback
    itself can be undone.

    Args:
        name: Skill name
        version: Version to restore (see list_skill_versions)
        expected_version: If set, refuse unless the skill is currently at this version

    Returns:
        str: Confirmation with the new version number
    """
    skill = _load_skill(name)
    if not skill:
        return f"Error: Skill '{name}' not found."
    current_version = int(skill.get('version', 1) or 1)
    if expected_version is not None and expected_version != current_version:
        return (
            f"Error: Version conflict for '{name}': expected v{expected_version} but it is at "
            f"v{current_version}."
        )
    if version == current_version:
        return f"No changes: skill '{name}' is already at version {version}."

    target = SKILL_VERSIONS.get(name, version)
    if target is None:
        available = ", ".join(f"v{v}" for v in SKILL_VERSIONS.versions(name)) or "none"
        return f"Error: Skill '{name}' has no stored version {version}. Available: {available}."

    SKILL_VERSIONS.ensure_snapshot(skill)

    restored = dict(skill)
    for f in CONTENT_FIELDS:
        if f in target:
            restored[f] = copy.deepcopy(target[f])
        else:
            restored.pop(f, None)
    # The restored content keeps the verification result it had (none for
    # versions saved before the gate existed).
    for f in ('status', 'verification'):
        if f in target:
            restored[f] = copy.deepcopy(target[f])
        else:
            restored.pop(f, None)
    restored['version'] = current_version + 1
    restored['updated_at'] = datetime.now(timezone.utc).isoformat()
    restored['rolled_back_from'] = version
    restored['change_note'] = f"Rollback to v{version}"

    _save_skill(restored)
    SKILL_INDEX.upsert_from_skill(restored)
    SKILL_VERSIONS.save(restored)
    return (
        f"✅ Skill '{name}' rolled back to the content of v{version}, saved as "
        f"version {restored['version']}."
    )


# =============================================================================
# Telemetry Tools
# =============================================================================

def _fmt_rate(rate: Any) -> str:
    return f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "–"


def _fmt_ms(ms: Any) -> str:
    return f"{ms:.0f}" if isinstance(ms, (int, float)) else "–"


def _stats_one_liner(s: Dict[str, Any]) -> str:
    parts = [
        f"{s['runs']} runs",
        f"{_fmt_rate(s.get('success_rate'))} success",
        f"p50 {_fmt_ms(s.get('p50_ms'))} ms",
        f"p95 {_fmt_ms(s.get('p95_ms'))} ms",
    ]
    if s.get("rejected"):
        parts.append(f"{s['rejected']} rejected")
    return ", ".join(parts)


@mcp.tool(
    name="get_skill_stats",
    annotations=ToolAnnotations(
        title="Get Execution Telemetry",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def get_skill_stats(
    skill_name: Optional[str] = None,
    recent: int = 5,
    response_format: ResponseFormat = ResponseFormat.MARKDOWN,
) -> str:
    """Execution telemetry: success rate, latency and recent errors.

    Without `skill_name`, returns a per-skill overview (execute_code runs are
    listed as '(execute_code)'). With `skill_name`, returns that skill's
    totals, a per-version breakdown (to judge whether an edit helped), and its
    most recent runs, nested composition calls included.

    Statuses: ok, error, timeout, rejected (refused before running because
    of unmet dependencies or invalid parameters).

    Args:
        skill_name: Optional skill to drill into
        recent: Number of most recent runs to include (0-50, single-skill view)
        response_format: 'markdown' or 'json'

    Returns:
        str: Telemetry report
    """
    if not TELEMETRY.enabled:
        return "Telemetry is disabled (SKILL_ORCHESTRATOR_TELEMETRY=off)."
    recent = max(0, min(int(recent), 50))

    if skill_name is None:
        overview = TELEMETRY.all_stats()
        if response_format == ResponseFormat.JSON:
            return json.dumps({"skills": overview}, indent=2, default=str)
        if not overview:
            return "No executions recorded yet."
        rows = sorted(overview.items(), key=lambda kv: kv[1]["runs"], reverse=True)
        lines = [
            "## Execution telemetry",
            "",
            "| Skill | Runs | Success | Errors | Timeouts | Rejected | p50 ms | p95 ms | Last run |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for name, s in rows:
            lines.append(
                f"| {name} | {s['runs']} | {_fmt_rate(s['success_rate'])} | {s['error']} "
                f"| {s['timeout']} | {s['rejected']} | {_fmt_ms(s['p50_ms'])} "
                f"| {_fmt_ms(s['p95_ms'])} | {str(s['last_run_at'] or '')[:19]} |"
            )
        failing = [(n, s) for n, s in rows if s.get("last_error")]
        if failing:
            lines.append("\n**Last errors:**")
            for n, s in failing[:10]:
                lines.append(f"- **{n}** ({str(s['last_error_at'])[:19]}): {s['last_error']}")
        return "\n".join(lines)

    stats = TELEMETRY.skill_stats(skill_name)
    runs = TELEMETRY.recent(skill_name=skill_name, limit=recent)
    if response_format == ResponseFormat.JSON:
        return json.dumps({**stats, "recent": runs}, indent=2, default=str)
    if not stats["runs"]:
        return f"No executions recorded for '{skill_name}'."

    lines = [
        f"## Telemetry: {skill_name}",
        "",
        f"- **Totals:** {_stats_one_liner(stats)}",
        f"- **Top-level / nested runs:** {stats['top_level_runs']} / {stats['nested_runs']}",
        f"- **Errors / timeouts:** {stats['error']} / {stats['timeout']}",
    ]
    if stats.get("last_error"):
        lines.append(f"- **Last error** ({str(stats['last_error_at'])[:19]}): {stats['last_error']}")
    lines += [
        "",
        "### By version",
        "| Version | Runs | Success | p50 ms | p95 ms | Last error |",
        "|---|---|---|---|---|---|",
    ]
    for v, s in stats["by_version"].items():
        lines.append(
            f"| v{v} | {s['runs']} | {_fmt_rate(s['success_rate'])} | {_fmt_ms(s['p50_ms'])} "
            f"| {_fmt_ms(s['p95_ms'])} | {s['last_error'] or ''} |"
        )
    if runs:
        lines += ["", f"### Last {len(runs)} run(s)"]
        for r in runs:
            via = f" via {r['parent']}" if r.get("parent") else ""
            err = f" — {r['error']}" if r.get("error") else ""
            lines.append(
                f"- {str(r.get('ts'))[:19]} v{r.get('version')} **{r.get('status')}** "
                f"{_fmt_ms(r.get('duration_ms'))} ms{via}{err}"
            )
    return "\n".join(lines)


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
    if not _is_safe_storage_name(params.namespace):
        return _invalid_namespace_message(params.namespace)

    with _STATE_LOCK:
        state = _load_state(params.namespace)
        try:
            _set_nested_value(state, params.key, params.value)
        except ValueError as e:
            return f"Error: {e}"
        _save_state(params.namespace, state)

    return f"✅ State '{params.key}' set in namespace '{params.namespace}'."


@mcp.tool(
    name="delete_state",
    annotations=ToolAnnotations(
        title="Delete Persistent State",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False
    )
)
async def delete_state(key: str, namespace: str = "default") -> str:
    """Delete a key (and everything nested under it) from persistent state.

    Args:
        key: State key to delete (dot notation supported)
        namespace: State namespace (default: 'default')

    Returns:
        str: Confirmation, or a note that the key did not exist
    """
    if not _is_safe_storage_name(namespace):
        return _invalid_namespace_message(namespace)

    with _STATE_LOCK:
        state = _load_state(namespace)
        existed = _delete_nested_value(state, key)
        if existed:
            _save_state(namespace, state)

    if not existed:
        return f"State '{key}' not found in namespace '{namespace}'."
    return f"✅ State '{key}' deleted from namespace '{namespace}'."


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
    if not _is_safe_storage_name(namespace):
        return _invalid_namespace_message(namespace)

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
    if not _is_safe_storage_name(params.namespace):
        return _invalid_namespace_message(params.namespace)

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

    drafts = sorted(
        str(s.get("name")) for s in skills
        if verification.skill_status(s) == verification.STATUS_DRAFT
    )
    verified_count = sum(
        1 for s in skills if verification.skill_status(s) == verification.STATUS_VERIFIED
    )
    degraded = sorted(n for n, h in _health_map(skills).items() if h["degraded"])
    env_info = sandbox_env.describe()
    lifecycle_lines = (
        f"- **Verified Skills:** {verified_count} of {skill_count}\n"
        f"- **Draft Skills:** {', '.join(drafts) if drafts else 'None'}\n"
        f"- **Degraded Skills:** {', '.join(degraded) if degraded else 'None'} "
        f"(>{DEGRADED_FAILURE_RATE:.0%} of the last {DEGRADED_WINDOW} runs failed)"
    )
    sandbox_lines = (
        f"- **Child environment:** allowlist only ({', '.join(env_info['default'])})\n"
        f"- **Operator passthrough:** "
        f"{', '.join(env_info['passthrough']) if env_info['passthrough'] else 'None'}"
        + (
            f" (ignored invalid names: {', '.join(env_info['rejected'])})"
            if env_info['rejected']
            else ""
        )
    )

    if TELEMETRY.enabled:
        overview = TELEMETRY.all_stats()
        recorded = sum(s["runs"] for s in overview.values())
        failing = sorted(
            n for n, s in overview.items()
            if s.get("last_error_at") and s.get("last_error_at") == s.get("last_run_at")
        )
        telemetry_lines = (
            f"- **Recorded Executions:** {recorded}\n"
            f"- **Skills Whose Last Run Failed:** {', '.join(failing) if failing else 'None'}\n"
            f"- **Telemetry Log:** `{TELEMETRY.path}`"
        )
    else:
        telemetry_lines = "- Telemetry disabled (SKILL_ORCHESTRATOR_TELEMETRY=off)"

    return f"""# Skill Orchestrator Status

**Version:** skill_orchestrator_mcp v{SERVER_VERSION}

## Storage
- **Skills Directory:** `{DEFAULT_SKILLS_DIR}`
- **State Directory:** `{DEFAULT_STATE_DIR}`
- **Sessions Directory:** `{DEFAULT_SESSIONS_DIR}`
- **Artifacts Directory:** `{DEFAULT_ARTIFACTS_DIR}`

## Statistics
- **Total Skills:** {skill_count}
- **Total Invocations:** {total_invocations}
- **State Namespaces:** {state_namespaces}

## Telemetry
{telemetry_lines}

## Skill Lifecycle
{lifecycle_lines}

## Sandbox
{sandbox_lines}

## Skills by Category
{chr(10).join(f'- **{cat}:** {count}' for cat, count in sorted(skills_by_category.items()))}

## Capabilities
- ✅ Create and manage reusable skills
- ✅ Execute Python code in isolated environments
- ✅ Persist state across conversations
- ✅ Progressive tool discovery
- ✅ Skill version history with rollback
- ✅ Dependency and parameter-schema enforcement
- ✅ Execution telemetry (success rate, latency, errors)
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
    if not _is_safe_storage_name(namespace):
        return json.dumps({"error": f"Invalid namespace '{namespace}'"})
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
