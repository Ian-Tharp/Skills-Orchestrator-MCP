"""
SKILL.md export / import (agentskills.io folder format).

A skill is exported as a folder that other agents and tools can read:

    <directory>/<skill-name>/
        SKILL.md                            frontmatter (name, description, metadata) + instructions
        scripts/<skill_name>.py             the skill code
        scripts/run.py                      runs the skill without the orchestrator
        examples/examples.json              {"examples": [...], "tests": [...]}
        references/parameters.schema.json   the JSON Schema, if the skill has one

The agentskills.io ``name`` is lowercase letters, digits and single hyphens,
so ``csv_summary`` is exported as ``csv-summary``. The original name and the
orchestrator-only fields go in ``metadata`` (string values only, per the
spec; lists are stored as JSON strings) so an import gets the same skill back.

Only the YAML subset that SKILL.md frontmatter uses is supported: scalars
(plain, single- or double-quoted, ``|`` / ``>`` blocks), one level of nested
mapping, and simple ``- item`` lists. Anything else is an error rather than a
guess. This avoids a YAML dependency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

SKILL_MD = "SKILL.md"
RUNNER_NAME = "run.py"
EXAMPLES_FILE = "examples/examples.json"
SCHEMA_FILE = "references/parameters.schema.json"

MAX_SKILL_MD_BYTES = 256_000
MAX_JSON_BYTES = 1_000_000
MAX_CODE_BYTES = 200_000

SPEC_NAME_MAX = 64
SPEC_DESCRIPTION_MAX = 1024
_SPEC_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_ORCH_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s+(.*)|\s*)$")


class SkillMdError(ValueError):
    """A SKILL.md folder could not be read or written."""


# ----------------------------------------------------------------------------
# Names
# ----------------------------------------------------------------------------


def to_spec_name(name: str) -> str:
    """Orchestrator snake_case name -> agentskills.io name (lowercase, single hyphens)."""
    spec = re.sub(r"-+", "-", name.lower().replace("_", "-")).strip("-")
    spec = spec[:SPEC_NAME_MAX].rstrip("-")
    if not _SPEC_NAME_RE.match(spec):
        raise SkillMdError(f"Cannot derive a SKILL.md name from {name!r}")
    return spec


def from_spec_name(spec: str) -> str:
    """agentskills.io name -> orchestrator name (hyphens become underscores)."""
    name = spec.strip().lower().replace("-", "_")
    if not name[:1].isalpha():
        name = "skill_" + name
    return name


def script_module_name(name: str) -> str:
    """File stem for the code (avoids clashing with the runner)."""
    return name if name + ".py" != RUNNER_NAME else name + "_skill"


# ----------------------------------------------------------------------------
# Frontmatter (YAML subset)
# ----------------------------------------------------------------------------


def _quote(value: str) -> str:
    # A JSON string is a valid YAML double-quoted scalar.
    return json.dumps(value, ensure_ascii=False)


def dump_frontmatter(data: Mapping[str, Any]) -> str:
    lines = ["---"]
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, Mapping):
            lines.append(f"{key}:")
            for k, v in value.items():
                lines.append(f"  {k}: {_quote(str(v))}")
        else:
            lines.append(f"{key}: {_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _parse_scalar(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError as e:
            raise SkillMdError(f"Bad double-quoted value: {raw[:60]}") from e
        if not isinstance(value, str):
            raise SkillMdError(f"Bad double-quoted value: {raw[:60]}")
        return value
    if raw.startswith("'"):
        if len(raw) < 2 or not raw.endswith("'"):
            raise SkillMdError(f"Unterminated single-quoted value: {raw[:60]}")
        return raw[1:-1].replace("''", "'")
    # Plain scalar: a comment starts at " #".
    hash_at = raw.find(" #")
    if hash_at >= 0:
        raw = raw[:hash_at].rstrip()
    if raw[:1] in ("[", "{", "&", "*", "!"):
        raise SkillMdError(f"Unsupported YAML value (flow/anchor/tag): {raw[:60]}")
    return raw


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _block_scalar(style: str, lines: List[str]) -> str:
    """Body of a `|` or `>` block (lines already collected, still indented)."""
    content = list(lines)
    while content and not content[-1].strip():
        content.pop()
    if not content:
        return ""
    indent = min(_indent(ln) for ln in content if ln.strip())
    stripped = [ln[indent:] if ln.strip() else "" for ln in content]
    if style.startswith("|"):
        text = "\n".join(stripped)
    else:
        paragraphs: List[str] = []
        current: List[str] = []
        for ln in stripped:
            if ln:
                current.append(ln.strip())
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        text = "\n".join(p for p in paragraphs)
    # "|-" / ">-" strip the final newline; plain and "+" keep one.
    return text if style.endswith("-") else text + "\n"


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Split SKILL.md text into (frontmatter dict, markdown body)."""
    text = text.lstrip("﻿").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillMdError("SKILL.md must start with '---' frontmatter")
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as e:
        raise SkillMdError("SKILL.md frontmatter is not closed with '---'") from e
    body = "\n".join(lines[end + 1 :])
    block = lines[1:end]

    data: Dict[str, Any] = {}
    i = 0
    while i < len(block):
        line = block[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        if _indent(line) != 0:
            raise SkillMdError(f"Unexpected indentation in frontmatter: {line.strip()[:60]}")
        m = _KEY_RE.match(line)
        if not m:
            raise SkillMdError(f"Unsupported frontmatter line: {line[:60]}")
        key, raw = m.group(1), (m.group(2) or "").strip()
        i += 1
        # Collect the indented lines that belong to this key.
        child: List[str] = []
        while i < len(block) and (not block[i].strip() or _indent(block[i]) > 0):
            child.append(block[i])
            i += 1
        if raw and raw[0] in "|>":
            if not re.match(r"^[|>][+-]?$", raw):
                raise SkillMdError(f"Unsupported block scalar header for {key!r}: {raw}")
            data[key] = _block_scalar(raw, child)
        elif raw:
            if any(c.strip() for c in child):
                # Plain multi-line scalar: fold continuation lines.
                raw = " ".join([raw, *(c.strip() for c in child if c.strip())])
            data[key] = _parse_scalar(raw)
        else:
            items = [c for c in child if c.strip() and not c.strip().startswith("#")]
            if not items:
                data[key] = ""
            elif all(c.strip().startswith("- ") or c.strip() == "-" for c in items):
                data[key] = [_parse_scalar(c.strip()[1:]) for c in items]
            else:
                mapping: Dict[str, str] = {}
                for c in items:
                    cm = _KEY_RE.match(c.strip())
                    if not cm or not cm.group(2):
                        raise SkillMdError(
                            f"Only one level of 'key: value' is supported under {key!r}"
                        )
                    mapping[cm.group(1)] = _parse_scalar(cm.group(2))
                data[key] = mapping
    return data, body


# ----------------------------------------------------------------------------
# Export
# ----------------------------------------------------------------------------


def _runner_source(module: str, common_imports: str) -> str:
    return f'''"""Run this skill without Skill Orchestrator.

Usage:
    python scripts/run.py '{{"x": 2}}'        # parameters as a JSON object
    echo '{{"x": 2}}' | python scripts/run.py

Prints the skill's __result__ as JSON. Orchestrator helpers (invoke_skill,
get_state, set_state, ...) are not available here.
"""

import json
import sys
from pathlib import Path

COMMON_IMPORTS = {common_imports!r}
SKILL_FILE = Path(__file__).with_name({module + ".py"!r})


def main() -> None:
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    elif not sys.stdin.isatty():
        raw = sys.stdin.read()
    else:
        raw = ""
    params = json.loads(raw) if raw.strip() else {{}}
    if not isinstance(params, dict):
        sys.exit("parameters must be a JSON object")
    namespace = {{"__name__": "__skill__", "params": params, "context": {{}}, "__result__": None}}
    exec(COMMON_IMPORTS, namespace)
    exec(compile(SKILL_FILE.read_text(encoding="utf-8"), str(SKILL_FILE), "exec"), namespace)
    print(json.dumps(namespace.get("__result__"), indent=2, default=str))


if __name__ == "__main__":
    main()
'''


def _body(skill: Mapping[str, Any], module: str) -> str:
    name = str(skill["name"])
    lines = [
        f"# {name}",
        "",
        str(skill.get("description") or "").strip(),
        "",
        "## How to use",
        "",
        f"The code is in `scripts/{module}.py`. It reads its inputs from a `params` dict",
        "(and optional `context` dict) and puts its output in `__result__`.",
        "",
        "- Run it directly: `python scripts/run.py '<parameters as JSON>'` prints the result.",
        f"- In Skill Orchestrator MCP: `import_skill` this folder, then `invoke_skill(\"{name}\", ...)`.",
    ]
    if skill.get("parameters_schema"):
        lines += [
            "",
            "## Parameters",
            "",
            f"JSON Schema (also in `{SCHEMA_FILE}`):",
            "",
            "```json",
            json.dumps(skill["parameters_schema"], indent=2),
            "```",
        ]
    examples = [str(e) for e in skill.get("examples") or []]
    tests = skill.get("tests") or []
    if examples or tests:
        lines += ["", "## Examples", ""]
        lines += [f"- {e}" for e in examples]
        if tests:
            lines.append(f"- {len(tests)} test case(s) with expected results: `{EXAMPLES_FILE}`")
    deps = [str(d) for d in skill.get("dependencies") or []]
    lines += ["", "## Requirements", "", "- Python 3.10+"]
    lines += [f"- `{d}`" for d in deps]
    return "\n".join(lines) + "\n"


def export_skill_dir(
    skill: Mapping[str, Any],
    directory: Path,
    *,
    common_imports: str,
    overwrite: bool = False,
) -> Tuple[Path, List[Path]]:
    """Write a skill as an agentskills.io folder under ``directory``.

    Returns (skill folder, files written). Refuses to touch an existing
    non-empty folder unless ``overwrite``; even then only the files listed
    above are replaced, nothing is deleted.
    """
    name = str(skill["name"])
    spec_name = to_spec_name(name)
    module = script_module_name(name)
    root = directory / spec_name
    if root.exists() and not root.is_dir():
        raise SkillMdError(f"{root} exists and is not a directory")
    if root.is_dir() and any(root.iterdir()) and not overwrite:
        raise SkillMdError(f"{root} already exists and is not empty (pass overwrite=True)")

    description = str(skill.get("description") or "").strip()[:SPEC_DESCRIPTION_MAX]
    deps = [str(d) for d in skill.get("dependencies") or []]
    metadata: Dict[str, str] = {
        "orchestrator-name": name,
        "entrypoint": f"scripts/{module}.py",
        "category": str(skill.get("category") or "custom"),
        "tags": json.dumps([str(t) for t in skill.get("tags") or []]),
        "dependencies": json.dumps(deps),
        "version": str(skill.get("version", 1)),
    }
    if skill.get("status"):
        metadata["status"] = str(skill["status"])
    compatibility = "Python 3.10+" + (f"; requires {', '.join(deps)}" if deps else "")
    front = {
        "name": spec_name,
        "description": description,
        "compatibility": compatibility[:500],
        "metadata": metadata,
    }

    files: Dict[str, str] = {
        SKILL_MD: dump_frontmatter(front) + "\n" + _body(skill, module),
        f"scripts/{module}.py": str(skill.get("code") or ""),
        f"scripts/{RUNNER_NAME}": _runner_source(module, common_imports),
        EXAMPLES_FILE: json.dumps(
            {"examples": list(skill.get("examples") or []), "tests": list(skill.get("tests") or [])},
            indent=2,
            default=str,
        )
        + "\n",
    }
    if skill.get("parameters_schema"):
        files[SCHEMA_FILE] = json.dumps(skill["parameters_schema"], indent=2) + "\n"

    written: List[Path] = []
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        written.append(path)
    return root, written


# ----------------------------------------------------------------------------
# Import
# ----------------------------------------------------------------------------


def _read_inside(root: Path, rel: str, limit: int) -> Optional[str]:
    """Read root/rel if it exists, refusing paths (or symlinks) that leave root."""
    path = (root / rel).resolve()
    if path != root and root not in path.parents:
        raise SkillMdError(f"{rel!r} points outside the skill folder")
    if not path.is_file():
        return None
    if path.stat().st_size > limit:
        raise SkillMdError(f"{rel} is larger than {limit} bytes")
    return path.read_text(encoding="utf-8")


def _json_list(raw: Any, field: str) -> List[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        value = json.loads(str(raw))
    except ValueError:
        # Foreign SKILL.md: a comma-separated string.
        return [p.strip() for p in str(raw).split(",") if p.strip()]
    if not isinstance(value, list):
        raise SkillMdError(f"metadata.{field} must be a JSON list")
    return [str(x) for x in value]


def read_skill_dir(path: Path) -> Dict[str, Any]:
    """Read an agentskills.io folder into create_skill fields.

    Returns a dict with name, description, code, category, tags,
    dependencies, parameters_schema, examples and tests.
    """
    if path.is_file() and path.name == SKILL_MD:
        path = path.parent
    root = path.resolve()
    if not root.is_dir():
        raise SkillMdError(f"{path} is not a directory")
    text = _read_inside(root, SKILL_MD, MAX_SKILL_MD_BYTES)
    if text is None:
        raise SkillMdError(f"No {SKILL_MD} in {path}")
    front, _body_text = parse_frontmatter(text)

    spec_name = front.get("name")
    description = front.get("description")
    if not isinstance(spec_name, str) or not spec_name.strip():
        raise SkillMdError("SKILL.md frontmatter needs a 'name'")
    if not isinstance(description, str) or not description.strip():
        raise SkillMdError("SKILL.md frontmatter needs a 'description'")
    meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}
    assert isinstance(meta, dict)

    name = meta.get("orchestrator-name")
    if not (isinstance(name, str) and _ORCH_NAME_RE.match(name)):
        name = from_spec_name(spec_name)

    entry = meta.get("entrypoint")
    code: Optional[str]
    if isinstance(entry, str) and entry.strip():
        entry = entry.strip()
        if not entry.endswith(".py"):
            raise SkillMdError(f"metadata.entrypoint must be a .py file, got {entry!r}")
        code = _read_inside(root, entry, MAX_CODE_BYTES)
        if code is None:
            raise SkillMdError(f"metadata.entrypoint {entry!r} does not exist")
    else:
        scripts_dir = root / "scripts"
        candidates = (
            sorted(
                p.name
                for p in scripts_dir.iterdir()
                if p.suffix == ".py" and p.name != RUNNER_NAME and not p.name.startswith("_")
            )
            if scripts_dir.is_dir()
            else []
        )
        if not candidates:
            raise SkillMdError(
                "No Python code found (scripts/*.py). Instruction-only skills cannot be "
                "imported as executable skills."
            )
        if len(candidates) > 1:
            raise SkillMdError(
                "Several scripts found (" + ", ".join(candidates) + "); set "
                "metadata.entrypoint in SKILL.md to choose one"
            )
        code = _read_inside(root, f"scripts/{candidates[0]}", MAX_CODE_BYTES)
        assert code is not None

    examples: List[str] = []
    tests: List[Dict[str, Any]] = []
    raw_examples = _read_inside(root, EXAMPLES_FILE, MAX_JSON_BYTES)
    if raw_examples is not None:
        try:
            ex_data = json.loads(raw_examples)
        except ValueError as e:
            raise SkillMdError(f"{EXAMPLES_FILE} is not valid JSON: {e}") from e
        if not isinstance(ex_data, dict):
            raise SkillMdError(f"{EXAMPLES_FILE} must be a JSON object")
        examples = [str(e) for e in ex_data.get("examples") or []]
        raw_tests = ex_data.get("tests") or []
        if not isinstance(raw_tests, list) or not all(isinstance(t, dict) for t in raw_tests):
            raise SkillMdError(f"'tests' in {EXAMPLES_FILE} must be a list of objects")
        tests = raw_tests

    schema: Optional[Dict[str, Any]] = None
    raw_schema = _read_inside(root, SCHEMA_FILE, MAX_JSON_BYTES)
    if raw_schema is not None:
        try:
            loaded = json.loads(raw_schema)
        except ValueError as e:
            raise SkillMdError(f"{SCHEMA_FILE} is not valid JSON: {e}") from e
        if not isinstance(loaded, dict):
            raise SkillMdError(f"{SCHEMA_FILE} must be a JSON object")
        schema = loaded

    return {
        "name": name,
        "description": description.strip(),
        "code": code,
        "category": str(meta.get("category") or "custom"),
        "tags": _json_list(meta.get("tags"), "tags"),
        "dependencies": _json_list(meta.get("dependencies"), "dependencies"),
        "parameters_schema": schema,
        "examples": examples,
        "tests": tests,
    }
