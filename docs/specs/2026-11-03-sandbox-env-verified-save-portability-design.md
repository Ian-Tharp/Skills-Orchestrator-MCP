# Skill Orchestrator — Scrubbed Sandbox Env, Verified Saves, SKILL.md Portability

**Date:** 2026-11-03
**Status:** Implemented
**Builds on:** 2026-11-02-safe-self-edit-observability-design.md

## Problem

- The skill subprocess was spawned without `env=`, so every skill and
  `execute_code` call inherited the server's full environment, including any
  API keys the MCP client passed to the server. Comparable tools (Cloudflare
  Code Mode, Anthropic sandbox-runtime, Claude API skill containers) keep
  credentials out of the code sandbox.
- Skills were saved without ever being run. SkillsBench (Feb 2026) found
  self-generated skills gave no average benefit and hurt 16 of 84 tasks
  unless verified by execution; ASI (COLM 2025) gained only from
  execution-verified skills.
- Skills were stored as orchestrator-only JSON. `SKILL.md` (agentskills.io,
  Dec 2025) is the cross-vendor format, so skills could not move between
  agents.

## Design

### 1. Scrubbed environment (`sandbox_env.py`)

- `build_child_env()` returns the complete child environment: a per-platform
  allowlist copied from the server env (case-insensitive on Windows), then
  fixed values `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1`.
  - All: `PATH`, `LANG`, `LANGUAGE`, `LC_ALL`, `LC_CTYPE`, `TZ`.
  - POSIX: `HOME`, `TMPDIR`.
  - Windows: `SYSTEMROOT` (sockets/random), `SYSTEMDRIVE`, `WINDIR`,
    `COMSPEC`, `PATHEXT`, `TEMP`, `TMP`, `USERPROFILE`, `HOMEDRIVE`,
    `HOMEPATH`, `APPDATA`, `LOCALAPPDATA` (user site-packages),
    `PROGRAMDATA`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS`.
- `SKILL_ORCHESTRATOR_ENV_PASSTHROUGH=NAME1,NAME2` adds exact names. It is
  read from the server's environment on each run; tool arguments never touch
  the environment, so clients cannot widen it. Invalid names are ignored and
  reported (names only) in `get_orchestrator_status`.
- Proxy variables are deliberately not in the default list (they often embed
  credentials); add them via passthrough if skills need them.
- The in-process mode is unchanged and documented as unscrubbed.

### 2. Verified-save gate (`skill_verification.py`)

- Test cases: `tests` (`parameters`, `context`, optional `expected_result`
  compared after a JSON round trip, optional `timeout`, optional `name`) plus
  any example string that parses as a JSON object (run as parameters). Max 10.
- `_verify_skill` runs each case through the normal subprocess runner with
  the candidate injected into the composition registry (so unsaved code can
  call itself and other skills), `commit_state=False` (state ops are
  dropped), and no telemetry. Parameters are validated against the schema
  (defaults applied) regardless of the enforcement mode: a test with bad
  parameters is a broken test.
- Outcomes: any failing case → not saved, failures returned; with
  `save_as_draft=True` → saved with `status: draft`. No runnable cases or
  unmet dependencies → `status: unverified` (or refused when
  `SKILL_ORCHESTRATOR_REQUIRE_VERIFIED_SKILLS=on`, unless drafted). All pass →
  `status: verified`. A compact `verification` record (counts, failures,
  timestamp) is stored on the skill.
- `update_skill` re-verifies only when a behavior field changes (code,
  schema, dependencies, tests, examples) and re-checks the version after the
  run to avoid saving over a concurrent edit.
- `tests` joins `CONTENT_FIELDS`, so it is versioned, diffed and restored by
  rollback. Rollback also restores the target version's status.
- `verify_skill` re-runs the tests on the saved skill and updates status and
  the current snapshot in place (no new version).
- The skill index omits `tests` (like `code`) to keep search light.

### 3. Telemetry-driven lifecycle

- `ExecutionTelemetry.recent_failures(versions, window)` counts failures in
  each skill's last `window` executed runs of its current version, in one
  pass over the log.
- Degraded = at least `DEGRADED_MIN_RUNS` (5) runs and failure share above
  `DEGRADED_FAILURE_RATE` (0.5) over the last `DEGRADED_WINDOW` (20).
  Computed on read for `search_skills` (current page only), `get_skill` and
  `get_orchestrator_status`. Not stored, never deletes or rolls back.

### 4. Dependency installation

PR #2 added no install path (`uv run --with`, `pip install`). None is added
here; the README states that one would be unsafe without network and
package allowlists and must stay off by default if ever added.

### 5. SKILL.md export/import (`skill_md.py`)

- Folder: `SKILL.md`, `scripts/<name>.py`, `scripts/run.py` (standalone
  runner with the same common imports), `examples/examples.json`,
  `references/parameters.schema.json`.
- Frontmatter: `name` (spec form: lowercase, single hyphens, ≤64, equal to
  the folder name), `description` (≤1024), `compatibility`, and string-only
  `metadata` holding the original name, entrypoint, category, tags and
  dependencies (JSON lists), version and status.
- Parser: a strict YAML subset (quoted/plain scalars, `|`/`>` blocks, one
  nested mapping level, simple lists). Unsupported syntax is an error. No new
  dependency.
- Import: code from `metadata.entrypoint`, else the single `scripts/*.py`
  (excluding `run.py`); instruction-only skills are refused. Reads are
  resolved and must stay inside the folder (symlinks too), with size caps.
  The result goes through `create_skill`, so the gate applies.
- Export: absolute paths only, refuses a non-empty folder without
  `overwrite`, never deletes. `SKILL_ORCHESTRATOR_PORTABLE_ROOT` optionally
  confines both tools.

## New MCP tools (additive)

`verify_skill`, `export_skill`, `import_skill`. New optional parameters:
`create_skill.tests`, `create_skill.save_as_draft`, `update_skill.tests`,
`update_skill.save_as_draft`.

## Behavior changes

- Skills no longer see the server's environment variables.
- A `create_skill` / `update_skill` whose test cases fail is refused.
  Skills without runnable cases save as before (status `unverified`).
- `search_skills` JSON items gain `status`, `degraded`, `recent_runs`,
  `recent_failures`; the index no longer carries `tests`.

## Not covered

- Filesystem, network and resource isolation (needs a container/VM or OS
  sandbox). Credentials stored in files are still readable by skills.
- Grandchild processes started by a skill are not killed on timeout.
- Verification runs real side effects (files, network) other than state.
- Instruction-only SKILL.md skills (no script) cannot be imported.
