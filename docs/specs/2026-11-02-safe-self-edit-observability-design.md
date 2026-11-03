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

## Hardening after review

A bug-hunt review reproduced the following problems; each is fixed and covered by `tests/test_review_regressions.py`.

- **Run status:** an exception with an empty message (bare `assert`) was recorded as `ok`. `sys.exit(0)` silently dropped state writes, and `os._exit(0)` looked like success. An unserializable `__result__` lost the whole result payload, including the state ops. The runner now catches `BaseException`, treats `SystemExit(0/None)` as a clean finish, falls back to the exception type as the message, and degrades an unserializable result to its `repr()`.
- **Composition compatibility:** nested calls once again pass the caller's `parameters` object through unchanged (any type, same object) unless a schema needs defaults filled. Children get the helper functions captured by the runner, so a parent that defines its own `set_state` can't hijack them.
- **Cross-process safety:** each MCP client runs its own server process. State commits, the state tools, skill create/update/rollback/delete and telemetry appends/compaction now take an OS-level file lock (`locking.py`, `fcntl`/`msvcrt`), so `expected_version` and the state merge hold across processes. A state commit stages every namespace file before replacing any, and its message says exactly what was written.
- **Legacy data:** malformed `dependencies` values (`null`, numbers, a bare string), free-form dependency notes, string versions (`"1.0"`), boolean schemas and schemas with unresolvable `$ref`s no longer crash an invocation or break unrelated skills. Free-form dependencies become warnings, and `SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT` adds a switch.
- **Safety:** dependency checks never import a package (a dotted name used to run its parent package's code inside the server, which could write to the stdio stream). Snapshot paths come from the validated skill name, never from the `name` field inside a file. A skill created over leftover history starts clean. `NaN` can't be written to state.
- **Telemetry:** "last run" and recent-run order use timestamps, not file order, since nested records are written when the parent finishes. `record()` never raises, and `|` in errors no longer breaks the markdown tables.
- **Performance:** dependency, schema and skill-reference checks are cached, and the registry is built off the event loop. With 200 skills, building the registry takes about 6 ms (it took about 144 ms before the fix, and about 5 ms before this change).

A second review of those fixes found and fixed:

- **Lock waits blocked the event loop.** Tools that take a file lock now do that work in a worker thread, so a lock held by another process no longer freezes every other request. Thread and file waits share one deadline. On file systems without lock support, locking falls back to in-process only, with a warning, instead of timing out every write.
- **Nested `sys.exit(0)`** committed a cut-off parent as `ok`. A child's `SystemExit` is still raised in the caller (so `except SystemExit` keeps working), but one that reaches the top level now fails the run. A child whose exception can't be converted to a string is still rolled back.
- **Composition helpers:** only the state bridge is pinned to the runner's helpers. A parent that wraps `invoke_skill` (for example to trace its subtree) applies to grandchildren again, as before. In `warn` mode, non-dict nested parameters pass through with a warning.
- **Contracts:**
  - `$ref`s are resolved with `referencing` (the library jsonschema uses), so `"$ref"` keys inside `default`/`examples`, a property named `$ref`, anchors, `$id`-relative and percent-encoded refs are all accepted, and remote refs are reported as unresolvable.
  - A non-string `$schema` is rejected cleanly.
  - Dotted dependency names check the submodule itself, still without importing.
  - A bare-string dependency is displayed and checked as one requirement.
- **Edge cases:**
  - An infinite `version` is tolerated.
  - `create_skill` won't overwrite an existing empty or corrupt skill file.
  - Temp files are cleaned up on every failed replace.
  - Telemetry `clear()` is best effort and only rewrites the log when it removes something.

## Behavior changes

- `delete_skill` on a skill that other skills call needs `force=True`.
- `invoke_skill` refuses skills with unmet dependencies or invalid parameters (in `strict` mode) instead of running them.
- The `invoke_skill` header now includes the version: `## Skill Execution: name (v3)`.
- `sys.exit(0)` in a top-level skill is now a clean finish (its state writes commit). Before, the run ended without a result. In a nested skill it rolls back the child; if the caller doesn't catch it, the run fails.

## Not covered

- Nested-call telemetry is lost when the run times out (the child is killed before it can report).
- The legacy in-process execution mode has no state bridge or composition.
- No automatic rollback: the agent decides using the per-version stats.
