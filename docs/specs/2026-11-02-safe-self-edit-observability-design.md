# Skill Orchestrator — Safe Self-Editing and Observability

**Date:** 2026-11-02
**Status:** Implemented
**Roadmap items:** "Execution analytics and performance tracking", "Skill versioning with rollback support"

## Problem

Agents can create and rewrite skills, but nothing protects them when they do:

- `update_skill` overwrote the skill in place. The version number went up, but the old code was gone, so a bad edit could not be undone.
- Usage counts were the only signal. There was no record of whether a run succeeded, how long it took, or what failed, so there was no way to tell whether an edit made a skill better or worse.
- `dependencies` and `parameters_schema` were stored but never checked. A missing package showed up as an `ImportError` halfway through a run; bad parameters showed up as a `KeyError` somewhere inside the skill.
- Skills had no access to persistent state. A skill that needed memory had to hand data back to the agent, which then called `set_state`: an extra round trip, and a crash in between left the two out of sync.

## Design

### 1. Execution telemetry (`telemetry.py`)

- One JSONL line per execution: top-level `invoke_skill`, each nested `invoke_skill` inside the child process, and `execute_code`.
- Fields: `ts`, `kind` (`skill` / `nested_skill` / `execute_code`), `skill`, `version`, `status` (`ok` / `error` / `timeout` / `rejected`), `duration_ms` (wall clock, server side), `exec_ms` (code only, child side), `error` (one line, ≤500 chars), `parent`, `depth`, `state_writes`.
- The child keeps a trace of nested calls and returns it in `result.json`; the server writes those records after the run.
- Aggregates are computed on read: counts, success rate (rejected runs excluded, since they never ran), p50/p95/max latency, last error, and the same split by version.
- Append is O(1). The log is compacted to the newest `max_records` (default 10,000) once it passes 125% of that.
- Local only, best effort: a telemetry failure never fails a run. `SKILL_ORCHESTRATOR_TELEMETRY=off` disables it.
- Surfaces: `get_skill_stats` (overview or one skill), a line in `get_skill`, per-version columns in `list_skill_versions`, and a summary in `get_orchestrator_status` (which now reports the installed package version instead of a hard-coded string).

### 2. Version history (`skill_versions.py`)

- A full snapshot for every saved version in `skills/.versions/<name>/v<N>.json`. The dotdir keeps snapshots out of the index's `*.json` glob.
- `create_skill` saves v1. `update_skill` saves vN+1. A skill created before this feature gets its current version snapshotted before its first edit.
- `rollback_skill(name, version)` copies that version's content fields (`code`, `description`, `category`, `tags`, `dependencies`, `parameters_schema`, `examples`) into a **new** version. History is never rewritten, so a rollback can itself be rolled back.
- `update_skill` gains `parameters_schema`, `category`, `examples`, `change_note`, and `expected_version` (optimistic concurrency for multiple agents sharing one store). An update that changes no content field does not create a version.
- `diff_skill_versions` shows metadata changes and a unified code diff.
- Retention: newest 50 per skill (`SKILL_ORCHESTRATOR_MAX_SKILL_VERSIONS`). Deleting a skill removes its history and telemetry, so a later skill with the same name starts clean.
- Skill files are now written atomically (temp file + rename).

### 3. Contracts (`skill_contracts.py`)

- **Dependencies:** PEP 508 requirement strings, parsed with `packaging`. Met when an installed distribution satisfies the specifier, or (for a bare name) when the name is importable, which covers the stdlib and import names like `bs4`. Environment markers are respected. Unparseable strings are rejected at create/update; unmet ones are a warning there and a refusal at invoke time, with an install hint. The server computes unmet dependencies for each registry entry, and the child refuses nested calls to those skills.
- **Parameter schemas:** validated as JSON Schema (Draft 2020-12, via `jsonschema`) at create/update; they must describe an object. At invoke time, top-level `default`s are filled in, then the parameters are validated, for top-level and nested calls alike. `SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT` = `strict` (default; reject), `warn` (run and report), or `off`. A legacy stored schema that is invalid is reported as a warning rather than blocking the skill.
- **Skill references:** an AST scan finds `invoke_skill('<literal>', ...)` calls. Create/update warns about unknown targets and self-calls; `get_skill` lists them; `delete_skill` refuses to delete a skill other skills call unless `force=True`.
- Refused runs are recorded with status `rejected` and do not count toward usage.
- `jsonschema` (already pulled in by `mcp`) and `packaging` are now declared dependencies.

### 4. State bridge

- In the child, the helpers are `get_state(key, namespace='default', default=None)`, `set_state(key, value, namespace='default')`, `delete_state(key, namespace='default')` and `list_state_keys(namespace='default')`. They are available to skills and to `execute_code`.
- Reads load the namespace file directly (read-only) and replay the run's pending ops on top (read your writes).
- Writes are validated (namespace rules, JSON-serializable, no setting through a non-object) and buffered as ops. The server commits them only when the run completes without error or timeout. Commit replays ops onto the current file contents under a lock, so concurrent writes to other keys survive. If any op is invalid, nothing is written.
- A nested `invoke_skill` that raises has its ops truncated (and the read cache cleared) before the exception propagates. A parent that catches the error therefore keeps its own writes but not the child's.
- The MCP state tools now also take the state lock and write atomically, and a `delete_state` tool was added for parity.

## Behavior changes

- `delete_skill` on a skill that other skills call needs `force=True`.
- `invoke_skill` refuses skills with unmet dependencies or invalid parameters (in `strict` mode) instead of running them.
- The `invoke_skill` header now includes the version: `## Skill Execution: name (v3)`.

## Not covered

- Nested-call telemetry is lost when the run times out (the child is killed before it can report).
- The legacy in-process execution mode has no state bridge or composition.
- No automatic rollback: the agent decides using the per-version stats.
