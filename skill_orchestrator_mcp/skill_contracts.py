"""
Skill Contracts: dependency and parameter-schema enforcement.

A skill declares two contracts:

- ``dependencies``: Python requirement strings (``"pandas"``, ``"requests>=2.31"``).
  Before a skill runs, each one is checked against the interpreter that will
  execute it (the subprocess uses the server's ``sys.executable``). A skill
  with unmet dependencies is refused with an install hint instead of failing
  halfway through with an ``ImportError``.
- ``parameters_schema``: a JSON Schema for ``params``. It is checked for
  validity when the skill is created or updated, and the parameters of every
  invocation (top-level and nested) are validated against it. Top-level
  ``default`` values from the schema are filled in for missing properties.

This module also finds the skills a skill calls (``invoke_skill('name', ...)``
with a literal name), so deleting a skill other skills depend on can be
flagged.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.requirements import InvalidRequirement, Requirement

# Enforcement modes for parameter schemas (env SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT).
ENFORCE_STRICT = "strict"  # reject invalid parameters (default)
ENFORCE_WARN = "warn"  # run anyway, report violations in the output
ENFORCE_OFF = "off"  # skip validation

_MAX_SCHEMA_ERRORS = 10


def schema_enforcement_mode() -> str:
    raw = os.environ.get("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT", ENFORCE_STRICT)
    mode = raw.strip().lower()
    return mode if mode in (ENFORCE_STRICT, ENFORCE_WARN, ENFORCE_OFF) else ENFORCE_STRICT


# ----------------------------------------------------------------------------
# Python dependencies
# ----------------------------------------------------------------------------


def parse_dependency(spec: str) -> Tuple[str, str]:
    """Split a requirement string into (name, specifier). Raises ValueError if invalid."""
    try:
        req = Requirement(spec.strip())
    except InvalidRequirement as e:
        raise ValueError(f"Invalid dependency {spec!r}: {e}") from e
    return req.name, str(req.specifier)


def invalid_dependencies(deps: List[str]) -> List[str]:
    """Return an error message for each requirement string that cannot be parsed."""
    errors = []
    for d in deps:
        try:
            parse_dependency(d)
        except ValueError as e:
            errors.append(str(e))
    return errors


def missing_dependencies(deps: Optional[List[str]]) -> List[str]:
    """Return a description of each unmet dependency (empty list = all satisfied).

    A requirement is met when an installed distribution of that name satisfies
    the specifier. Names without a version specifier also count as met when
    they are importable modules (stdlib, or import names like ``bs4``).
    """
    missing: List[str] = []
    for spec in deps or []:
        try:
            req = Requirement(spec.strip())
        except InvalidRequirement:
            missing.append(f"{spec} (invalid requirement string)")
            continue

        if req.marker is not None and not req.marker.evaluate():
            continue  # not needed on this platform

        try:
            installed: Optional[str] = importlib.metadata.version(req.name)
        except importlib.metadata.PackageNotFoundError:
            installed = None

        if installed is None:
            if not req.specifier and _is_importable(req.name):
                continue
            missing.append(f"{spec} (not installed)")
            continue

        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            missing.append(f"{spec} (installed: {installed})")
    return missing


def _is_importable(module: str) -> bool:
    candidate = module.replace("-", "_")
    try:
        return importlib.util.find_spec(candidate) is not None
    except (ImportError, ValueError):
        return False


def install_hint(missing: List[str]) -> str:
    specs = " ".join(f'"{m.split(" (")[0]}"' for m in missing)
    return f"Install into the server's environment, e.g. `uv pip install {specs}`."


# ----------------------------------------------------------------------------
# Parameter schemas
# ----------------------------------------------------------------------------


def check_schema(schema: Any) -> Optional[str]:
    """Return an error message if `schema` is not a valid JSON Schema, else None."""
    if schema is None:
        return None
    if not isinstance(schema, (dict, bool)):
        return "parameters_schema must be a JSON object"
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        path = "/".join(str(p) for p in e.path)
        return f"Invalid parameters_schema: {e.message}" + (f" (at '{path}')" if path else "")
    if isinstance(schema, dict) and schema.get("type") not in (None, "object"):
        return "parameters_schema must describe an object (params is always a dict)"
    return None


def validate_params(schema: Any, params: Mapping[str, Any]) -> List[str]:
    """Validate params against a schema. Returns readable error strings (empty = valid)."""
    if not schema:
        return []
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(dict(params)), key=lambda e: list(e.path))
    out = []
    for e in errors[:_MAX_SCHEMA_ERRORS]:
        where = ".".join(str(p) for p in e.path)
        out.append(f"{where}: {e.message}" if where else e.message)
    if len(errors) > _MAX_SCHEMA_ERRORS:
        out.append(f"... and {len(errors) - _MAX_SCHEMA_ERRORS} more")
    return out


def apply_defaults(schema: Any, params: Mapping[str, Any]) -> Dict[str, Any]:
    """Fill missing top-level properties that declare a `default` in the schema."""
    out = dict(params)
    if isinstance(schema, dict):
        props = schema.get("properties")
        if isinstance(props, dict):
            for key, prop in props.items():
                if key not in out and isinstance(prop, dict) and "default" in prop:
                    out[key] = prop["default"]
    return out


# ----------------------------------------------------------------------------
# Skill-to-skill references
# ----------------------------------------------------------------------------

_SKILL_REF_FALLBACK_RE = re.compile(r"""invoke_skill\(\s*['"]([a-z][a-z0-9_]*)['"]""")


def find_skill_refs(code: str) -> List[str]:
    """Names of skills a skill invokes via ``invoke_skill('<literal>', ...)``.

    Dynamic names (variables, f-strings) can't be resolved statically and are
    ignored.
    """
    refs: List[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        refs = _SKILL_REF_FALLBACK_RE.findall(code)
    else:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "invoke_skill"
            ):
                first: Optional[ast.expr] = node.args[0] if node.args else None
                if first is None:
                    for kw in node.keywords:
                        if kw.arg == "skill_name":
                            first = kw.value
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    refs.append(first.value)
    seen: Dict[str, None] = {}
    for r in refs:
        seen.setdefault(r, None)
    return list(seen)
