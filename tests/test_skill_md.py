"""SKILL.md (agentskills.io) export / import."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skill_orchestrator_mcp import server, skill_md
from skill_orchestrator_mcp.models import CreateSkillInput, SkillCategory, SkillTestCase
from skill_orchestrator_mcp.skill_versions import CONTENT_FIELDS

from .conftest import Storage

SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer", "default": 1}, "label": {"type": "string"}},
    "required": ["x"],
}
CODE = (
    "label = params.get('label', 'n')\n"
    "__result__ = {label: params['x'] * 2, 'pattern': re.escape('a.b')}\n"
)


def _skill(name: str = "csv_double") -> CreateSkillInput:
    return CreateSkillInput(
        name=name,
        description='Doubles x. Handles "quotes", colons: and unicode ✓ fine.',
        code=CODE,
        category=SkillCategory.DATA_PROCESSING,
        tags=["math", "demo, with comma"],
        dependencies=["json", "packaging>=20,<1000"],
        parameters_schema=SCHEMA,
        examples=["Double a number", '{"x": 3}'],
        tests=[
            SkillTestCase(name="basic", parameters={"x": 2}, expected_result={"n": 4, "pattern": "a\\.b"}),
            SkillTestCase(parameters={"x": 0, "label": "z"}),
        ],
    )


# ----------------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------------


async def test_export_import_round_trip(storage: Storage, tmp_path: Path) -> None:
    assert "verified" in await server.create_skill(_skill())
    original = json.loads((storage.skills_dir / "csv_double.json").read_text())

    out = await server.export_skill("csv_double", str(tmp_path))
    folder = tmp_path / "csv-double"
    await server.delete_skill("csv_double", confirm=True)
    msg = await server.import_skill(str(folder))

    assert "Exported 'csv_double'" in out
    assert "Skill created successfully" in msg and "verified (3/3" in msg
    imported = json.loads((storage.skills_dir / "csv_double.json").read_text())
    for field in CONTENT_FIELDS:
        assert imported.get(field) == original.get(field), field


async def test_export_layout_follows_agentskills_spec(storage: Storage, tmp_path: Path) -> None:
    await server.create_skill(_skill())

    await server.export_skill("csv_double", str(tmp_path))

    folder = tmp_path / "csv-double"
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) == [
        "SKILL.md",
        "examples/examples.json",
        "references/parameters.schema.json",
        "scripts/csv_double.py",
        "scripts/run.py",
    ]
    front, body = skill_md.parse_frontmatter((folder / "SKILL.md").read_text(encoding="utf-8"))
    # Spec: name matches the folder, lowercase + hyphens, <= 64 chars.
    assert front["name"] == "csv-double" == folder.name
    assert front["description"].startswith("Doubles x.")
    assert len(front["description"]) <= 1024
    assert all(isinstance(v, str) for v in front["metadata"].values())
    assert front["metadata"]["entrypoint"] == "scripts/csv_double.py"
    assert "python scripts/run.py" in body
    assert (folder / "scripts/csv_double.py").read_text(encoding="utf-8") == CODE.strip()


async def test_exported_runner_works_standalone(storage: Storage, tmp_path: Path) -> None:
    await server.create_skill(_skill())
    await server.export_skill("csv_double", str(tmp_path))

    proc = subprocess.run(
        [sys.executable, str(tmp_path / "csv-double" / "scripts" / "run.py"), '{"x": 5}'],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"n": 10, "pattern": "a\\.b"}


async def test_awkward_name_round_trips(storage: Storage, tmp_path: Path) -> None:
    """`a__b_` is not a valid spec name; metadata keeps the original."""
    await server.create_skill(
        CreateSkillInput(name="a__b_", description="Odd name for testing", code="__result__ = 1")
    )
    await server.export_skill("a__b_", str(tmp_path))
    await server.delete_skill("a__b_", confirm=True)

    msg = await server.import_skill(str(tmp_path / "a-b" / "SKILL.md"))

    assert "**Name:** a__b_" in msg


async def test_import_with_rename(storage: Storage, tmp_path: Path) -> None:
    await server.create_skill(_skill())
    await server.export_skill("csv_double", str(tmp_path))

    msg = await server.import_skill(str(tmp_path / "csv-double"), name="csv_double_copy")

    assert "**Name:** csv_double_copy" in msg
    assert server.SKILL_INDEX.get("csv_double_copy") is not None


async def test_export_refuses_existing_folder_without_overwrite(
    storage: Storage, tmp_path: Path
) -> None:
    await server.create_skill(_skill())
    await server.export_skill("csv_double", str(tmp_path))
    (tmp_path / "csv-double" / "NOTES.md").write_text("mine")

    again = await server.export_skill("csv_double", str(tmp_path))
    forced = await server.export_skill("csv_double", str(tmp_path), overwrite=True)

    assert again.startswith("Error:") and "overwrite=True" in again
    assert forced.startswith("✅")
    assert (tmp_path / "csv-double" / "NOTES.md").read_text() == "mine"


async def test_relative_and_out_of_root_paths_refused(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await server.create_skill(_skill())
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setenv("SKILL_ORCHESTRATOR_PORTABLE_ROOT", str(allowed))

    relative = await server.export_skill("csv_double", "some/dir")
    outside = await server.export_skill("csv_double", str(tmp_path / "elsewhere"))
    inside = await server.export_skill("csv_double", str(allowed))
    imp_outside = await server.import_skill(str(tmp_path / "elsewhere"))

    assert "must be absolute" in relative
    assert "outside the allowed folder" in outside and "outside the allowed folder" in imp_outside
    assert inside.startswith("✅")


# ----------------------------------------------------------------------------
# Importing folders written by other tools
# ----------------------------------------------------------------------------


def _write(folder: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return folder


FOREIGN_SKILL_MD = """---
name: word-counter
description: >
  Counts words in a text.
  Use when the user asks how long a text is.
license: MIT
metadata:
  author: someone
  version: '1.0'
allowed-tools:
  - Bash
---

# Word counter

Run `scripts/count.py`.
"""


async def test_import_foreign_skill_md(storage: Storage, tmp_path: Path) -> None:
    folder = _write(
        tmp_path / "word-counter",
        {
            "SKILL.md": FOREIGN_SKILL_MD,
            "scripts/count.py": "__result__ = len(params.get('text', '').split())\n",
        },
    )

    msg = await server.import_skill(str(folder))

    assert "Skill created successfully" in msg, msg
    disk = json.loads((storage.skills_dir / "word_counter.json").read_text())
    assert disk["description"] == (
        "Counts words in a text. Use when the user asks how long a text is."
    )
    assert disk["category"] == "custom" and disk["status"] == "unverified"


async def test_import_runs_the_gate(storage: Storage, tmp_path: Path) -> None:
    folder = _write(
        tmp_path / "broken",
        {
            "SKILL.md": '---\nname: broken\ndescription: "Always fails its test"\n---\n',
            "scripts/broken.py": "raise RuntimeError('nope')\n",
            "examples/examples.json": json.dumps({"tests": [{"parameters": {}}]}),
        },
    )

    rejected = await server.import_skill(str(folder))
    drafted = await server.import_skill(str(folder), save_as_draft=True)

    assert "was not saved" in rejected and "RuntimeError: nope" in rejected
    assert "Saved as a **draft**" in drafted


@pytest.mark.parametrize(
    "files, expected",
    [
        ({"scripts/a.py": "x=1"}, "No SKILL.md"),
        ({"SKILL.md": "no frontmatter"}, "must start with '---'"),
        ({"SKILL.md": "---\nname: x\n"}, "not closed"),
        ({"SKILL.md": "---\ndescription: hello there\n---\n"}, "needs a 'name'"),
        ({"SKILL.md": "---\nname: only-docs\ndescription: Just instructions\n---\n"}, "Instruction-only"),
        (
            {
                "SKILL.md": "---\nname: two\ndescription: Two scripts here\n---\n",
                "scripts/a.py": "x=1",
                "scripts/b.py": "x=2",
            },
            "metadata.entrypoint",
        ),
        (
            {
                "SKILL.md": (
                    "---\nname: esc\ndescription: Escapes the folder\nmetadata:\n"
                    "  entrypoint: ../outside.py\n---\n"
                ),
            },
            "outside the skill folder",
        ),
        ({"SKILL.md": "---\nname: [a, b]\ndescription: x\n---\n"}, "Unsupported YAML"),
    ],
)
async def test_import_errors(
    storage: Storage, tmp_path: Path, files: dict[str, str], expected: str
) -> None:
    (tmp_path / "outside.py").write_text("__result__ = 'escaped'")
    folder = _write(tmp_path / "skill", files)
    folder.mkdir(exist_ok=True)

    msg = await server.import_skill(str(folder))

    assert msg.startswith("Error:") and expected in msg, msg


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_import_refuses_symlink_escape(storage: Storage, tmp_path: Path) -> None:
    (tmp_path / "secret.py").write_text("__result__ = 'secret'")
    folder = _write(
        tmp_path / "link",
        {"SKILL.md": "---\nname: link\ndescription: Symlinked script\n---\n"},
    )
    (folder / "scripts").mkdir()
    (folder / "scripts" / "link.py").symlink_to(tmp_path / "secret.py")

    msg = await server.import_skill(str(folder))

    assert "outside the skill folder" in msg


# ----------------------------------------------------------------------------
# Frontmatter subset parser
# ----------------------------------------------------------------------------


def test_frontmatter_scalars_and_blocks() -> None:
    text = (
        "---\n"
        "a: plain value # comment\n"
        "b: 'it''s'\n"
        'c: "tab\\tand \\u2713"\n'
        "d: |\n  line 1\n  line 2\n"
        "e: >-\n  folded\n  text\n"
        "f:\n  - one\n  - 'two'\n"
        "g:\n  k: v\n  k2: \"v2\"\n"
        "h: continued\n  on next line\n"
        "---\nbody"
    )

    data, body = skill_md.parse_frontmatter(text)

    assert data == {
        "a": "plain value",
        "b": "it's",
        "c": "tab\tand ✓",
        "d": "line 1\nline 2\n",
        "e": "folded text",
        "f": ["one", "two"],
        "g": {"k": "v", "k2": "v2"},
        "h": "continued on next line",
    }
    assert body == "body"


def test_dump_then_parse_is_lossless() -> None:
    front = {
        "name": "x",
        "description": 'He said "hi": ok #1\nnew line',
        "metadata": {"tags": json.dumps(["a, b", "c"])},
    }

    data, _ = skill_md.parse_frontmatter(skill_md.dump_frontmatter(front) + "body")

    assert data == front


@pytest.mark.parametrize(
    "name, spec",
    [("csv_summary", "csv-summary"), ("a__b_", "a-b"), ("x" * 100, "x" * 64), ("run", "run")],
)
def test_spec_names(name: str, spec: str) -> None:
    assert skill_md.to_spec_name(name) == spec


def test_script_module_avoids_runner_clash() -> None:
    assert skill_md.script_module_name("run") == "run_skill"
    assert skill_md.script_module_name("csv") == "csv"
