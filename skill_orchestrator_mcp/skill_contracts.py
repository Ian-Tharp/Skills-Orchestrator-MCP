"""
Skill Contracts: dependency and parameter-schema enforcement.

A skill declares two contracts:

- ``dependencies``: Python requirement strings (``"pandas"``, ``"requests>=2.31"``,
  ``"python>=3.11"``). Before a skill runs, each one is checked against the
  interpreter that will execute it (the subprocess uses the server's
  ``sys.executable``). A skill with unmet dependencies is refused with an
  install hint instead of failing halfway through with an ``ImportError``.
  Strings that can't be parsed (free-form notes in older skills) are reported
  as warnings, never as a reason to refuse.
- ``parameters_schema``: a JSON Schema for ``params``. It is checked for
  validity when the skill is created or updated, and the parameters of every
  invocation (top-level and nested) are validated against it. Top-level
  ``default`` values from the schema are filled in for missing properties.

This module also finds the skills a skill calls (``invoke_skill('name', ...)``
with a literal name), so deleting a skill other skills depend on can be
flagged.

Checks are cached briefly: the registry built for every invocation runs them
for every skill.
"""

from __future__ import annotations

import ast
import functools
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for
from packaging.requirements import InvalidRequirement, Requirement
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012, specification_with

# Enforcement modes (env SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT and
# SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT).
ENFORCE_STRICT = "strict"  # refuse to run (default)
ENFORCE_WARN = "warn"  # run anyway, report the problem in the output
ENFORCE_OFF = "off"  # skip the check

_MODES = (ENFORCE_STRICT, ENFORCE_WARN, ENFORCE_OFF)
_MAX_SCHEMA_ERRORS = 10
_DEP_CACHE_TTL_SECONDS = 30.0


def _mode_from_env(var: str) -> str:
    mode = os.environ.get(var, ENFORCE_STRICT).strip().lower()
    return mode if mode in _MODES else ENFORCE_STRICT


def schema_enforcement_mode() -> str:
    return _mode_from_env("SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT")


def dependency_enforcement_mode() -> str:
    return _mode_from_env("SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT")


# ----------------------------------------------------------------------------
# Python dependencies
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyReport:
    # Parseable requirements that are not satisfied (reason in parentheses).
    missing: List[str] = field(default_factory=list)
    # Entries that aren't requirement strings at all (warn, don't refuse).
    invalid: List[str] = field(default_factory=list)


def _as_dependency_list(deps: Any) -> Tuple[List[str], List[str]]:
    """Normalize a stored `dependencies` value into (strings, invalid-entry notes)."""
    if deps is None:
        return [], []
    if isinstance(deps, str):
        deps = [deps]  # a single requirement stored as a bare string
    if not isinstance(deps, (list, tuple)):
        return [], [f"dependencies must be a list, got {type(deps).__name__}"]
    specs: List[str] = []
    invalid: List[str] = []
    for d in deps:
        if isinstance(d, str) and d.strip():
            specs.append(d.strip())
        else:
            invalid.append(f"{d!r} (not a requirement string)")
    return specs, invalid


def format_dependencies(deps: Any) -> str:
    """Comma-separated dependencies for display, whatever the stored value is."""
    specs, invalid = _as_dependency_list(deps)
    shown = specs + [i.split(" (")[0] for i in invalid]
    return ", ".join(shown) or "None"


def parse_dependency(spec: str) -> Tuple[str, str]:
    """Split a requirement string into (name, specifier). Raises ValueError if invalid."""
    try:
        req = Requirement(spec.strip())
    except InvalidRequirement as e:
        raise ValueError(f"Invalid dependency {spec!r}: {e}") from e
    return req.name, str(req.specifier)


def invalid_dependencies(deps: Any) -> List[str]:
    """Error messages for entries that can't be parsed (used to reject new input)."""
    specs, errors = _as_dependency_list(deps)
    for d in specs:
        try:
            parse_dependency(d)
        except ValueError as e:
            errors.append(str(e))
    return errors


def _check_one(spec: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (missing_note, invalid_note) for one requirement string."""
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None, f"{spec} (not a valid requirement string; ignored)"

    if req.marker is not None and not req.marker.evaluate():
        return None, None  # not needed on this platform

    if req.name.lower() == "python":
        current = platform.python_version()
        if req.specifier and not req.specifier.contains(current, prereleases=True):
            return f"{spec} (running Python {current})", None
        return None, None

    try:
        installed: Optional[str] = importlib.metadata.version(req.name)
    except importlib.metadata.PackageNotFoundError:
        installed = None

    if installed is None:
        if not req.specifier and _is_importable(req.name):
            return None, None
        return f"{spec} (not installed)", None

    if req.specifier and not req.specifier.contains(installed, prereleases=True):
        return f"{spec} (installed: {installed})", None
    return None, None


_dep_cache: Dict[Tuple[str, ...], Tuple[float, DependencyReport]] = {}


def check_dependencies(deps: Any) -> DependencyReport:
    """Check stored dependencies. Never raises, whatever the stored value is."""
    specs, invalid = _as_dependency_list(deps)
    key = tuple(specs) + ("\0invalid",) + tuple(invalid)
    now = time.monotonic()
    hit = _dep_cache.get(key)
    if hit is not None and now - hit[0] < _DEP_CACHE_TTL_SECONDS:
        return hit[1]

    missing: List[str] = []
    for spec in specs:
        m, inv = _check_one(spec)
        if m:
            missing.append(m)
        if inv:
            invalid.append(inv)
    report = DependencyReport(missing=missing, invalid=invalid)
    if len(_dep_cache) > 1024:
        _dep_cache.clear()
    _dep_cache[key] = (now, report)
    return report


def missing_dependencies(deps: Any) -> List[str]:
    """Unmet (parseable) dependencies; empty list means all satisfied."""
    return list(check_dependencies(deps).missing)


def _is_importable(module: str) -> bool:
    """Whether a module exists, without importing anything.

    `importlib.util.find_spec('a.b')` imports package `a`, which would run
    arbitrary package code inside the server (and can write to the stdio
    stream). So only the top-level name goes through `find_spec` (which
    doesn't import for top-level names); each further part is looked up with
    `PathFinder` inside the parent's search path.
    """
    parts = module.replace("-", "_").split(".")
    if not all(p.isidentifier() for p in parts):
        return False
    try:
        spec = importlib.util.find_spec(parts[0])
        for i in range(1, len(parts)):
            locations = spec.submodule_search_locations if spec is not None else None
            if not locations:
                return False  # a plain module has no submodules
            spec = importlib.machinery.PathFinder.find_spec(".".join(parts[: i + 1]), list(locations))
    except (ImportError, ValueError):
        return False
    return spec is not None


def install_hint(missing: List[str]) -> str:
    specs = " ".join(f'"{m.split(" (")[0]}"' for m in missing)
    return f"Install into the server's environment, e.g. `uv pip install {specs}`."


# ----------------------------------------------------------------------------
# Parameter schemas
# ----------------------------------------------------------------------------


class SchemaUsageError(Exception):
    """The schema itself can't be applied (e.g. a $ref that doesn't resolve)."""


def _unresolvable_refs(schema: Mapping[str, Any]) -> List[str]:
    """`$ref`s in `schema` that can't be resolved without network access.

    Uses the `referencing` library (what jsonschema itself resolves with), so
    only real schema positions are inspected: a "$ref" key inside a `default`
    or `examples` value, or a property named "$ref", is not a reference.
    Anchors, `$id`-relative and percent-encoded refs resolve normally.
    """
    spec = specification_with(str(schema.get("$schema", "")), default=DRAFT202012)
    root: Resource[Any] = spec.create_resource(schema)
    base = root.id() or ""
    resolver = Registry().with_resource(uri=base, resource=root).resolver(base_uri=base)
    bad: List[str] = []

    def walk(resource: Resource[Any], res: Any) -> None:
        contents = resource.contents
        if isinstance(contents, dict) and isinstance(contents.get("$ref"), str):
            try:
                res.lookup(contents["$ref"])
            except Unresolvable:
                bad.append(contents["$ref"])
        for sub in resource.subresources():
            walk(sub, res.in_subresource(sub))

    walk(root, resolver)
    return bad


def _describes_object(schema: Mapping[str, Any]) -> bool:
    t = schema.get("type")
    if t is None or t == "object":
        return True
    return isinstance(t, list) and "object" in t


@functools.lru_cache(maxsize=512)
def _check_schema_cached(schema_json: str) -> Optional[str]:
    schema = json.loads(schema_json)
    if isinstance(schema, bool):
        return None
    if not isinstance(schema, dict):
        return "parameters_schema must be a JSON object"
    if "$schema" in schema and not isinstance(schema["$schema"], str):
        return "Invalid parameters_schema: $schema must be a string"
    try:
        cls = validator_for(schema, default=Draft202012Validator)
        cls.check_schema(schema)
    except SchemaError as e:
        path = "/".join(str(p) for p in e.path)
        return f"Invalid parameters_schema: {e.message}" + (f" (at '{path}')" if path else "")
    except Exception as e:
        return f"Invalid parameters_schema: {type(e).__name__}: {e}"
    if not _describes_object(schema):
        return "parameters_schema must describe an object (params is always a dict)"
    try:
        bad = _unresolvable_refs(schema)
    except Exception as e:
        return f"Invalid parameters_schema: could not resolve references ({type(e).__name__}: {e})"
    if bad:
        return (
            f"Invalid parameters_schema: $ref {bad[0]!r} does not resolve "
            "(references must point inside the schema; remote refs are not fetched)"
        )
    return None


def check_schema(schema: Any) -> Optional[str]:
    """Return an error message if `schema` is not a usable JSON Schema, else None."""
    if schema is None:
        return None
    try:
        key = json.dumps(schema, sort_keys=True)
    except (TypeError, ValueError):
        return "parameters_schema must be JSON-serializable"
    return _check_schema_cached(key)


def validate_params(schema: Any, params: Any) -> List[str]:
    """Validate params against a schema. Returns readable error strings (empty = valid).

    Raises SchemaUsageError if the schema can't be applied.
    """
    if schema is None or schema is True or schema == {}:
        return []
    if not isinstance(params, dict):
        return [f"parameters must be an object, got {type(params).__name__}"]
    try:
        cls = (
            validator_for(schema, default=Draft202012Validator)
            if isinstance(schema, dict) and isinstance(schema.get("$schema", ""), str)
            else Draft202012Validator
        )
        errors = sorted(cls(schema).iter_errors(params), key=lambda e: list(e.path))
    except Exception as exc:  # referencing errors, malformed legacy schemas
        raise SchemaUsageError(f"{type(exc).__name__}: {exc}") from exc
    out = []
    for err in errors[:_MAX_SCHEMA_ERRORS]:
        where = ".".join(str(p) for p in err.path)
        out.append(f"{where}: {err.message}" if where else err.message)
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


@functools.lru_cache(maxsize=1024)
def _find_skill_refs_cached(code: str) -> Tuple[str, ...]:
    refs: List[str] = []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
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
    return tuple(dict.fromkeys(refs))


def find_skill_refs(code: str) -> List[str]:
    """Names of skills a skill invokes via ``invoke_skill('<literal>', ...)``.

    Dynamic names (variables, f-strings) can't be resolved statically and are
    ignored.
    """
    return list(_find_skill_refs_cached(code))
