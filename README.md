# Skill Orchestrator MCP Server

A self-evolving MCP (Model Context Protocol) server that enables AI agents to dynamically create, manage, and execute reusable skills. Implements Anthropic's "Code Execution with MCP" pattern for context-efficient agent-tool interaction.

---

## Table of Contents

- [The Vision: AI That Grows With You](#the-vision-ai-that-grows-with-you)
- [Key Capabilities](#key-capabilities)
- [Why This Matters: Token Efficiency & Context Management](#why-this-matters-token-efficiency--context-management)
- [How AI Agents Can Leverage This System](#how-ai-agents-can-leverage-this-system)
- [Available Tools](#available-tools-22-mcp-tools)
- [Skill Composition System](#skill-composition-system)
- [State Bridge](#state-bridge)
- [Safe Self-Editing: Versions, Contracts, Telemetry](#safe-self-editing-versions-contracts-telemetry)
- [Verified Saves and Skill Lifecycle](#verified-saves-and-skill-lifecycle)
- [Portability: SKILL.md Export and Import](#portability-skillmd-export-and-import)
- [Debugging & Development](#debugging--development)
- [Configuration](#configuration)
- [Storage Architecture](#storage-architecture)
- [Skill Categories](#skill-categories)
- [Quick Start](#quick-start)
- [Usage Examples](#usage-examples)
- [Architecture](#architecture)
- [Security Considerations](#security-considerations)
- [Future Roadmap](#future-roadmap)
- [Background & Inspiration](#background--inspiration)
- [Contributing](#contributing)
- [License](#license)

---

## The Vision: AI That Grows With You

Traditional AI interactions are stateless - every conversation starts from scratch, and complex operations flood the context window with intermediate data. The Skill Orchestrator changes this fundamental limitation:

**Before:** AI processes everything in context, losing work between sessions, re-discovering solutions repeatedly.

**After:** AI creates persistent, composable skills that accumulate knowledge. It offloads heavy computation, maintains memory across sessions, and builds domain expertise over time.

This isn't just a tool server - it's infrastructure for **AI self-improvement** and **context-efficient operation**.

---

## Key Capabilities

### Dynamic Skill Registry
- **Create** reusable code patterns on-the-fly during conversations
- **Store** skills persistently for use across sessions
- **Search** skills by name, description, category, or tags (progressive discovery)
- **Invoke** skills with parameters
- **Compose** skills by calling other skills (skill chaining)
- **Version** every edit, diff versions, and roll back a bad change
- **Enforce** declared Python dependencies and a JSON Schema for parameters
- **Verify** skills by running their test cases before they are saved
- **Export / import** skills as agentskills.io `SKILL.md` folders

### Code Execution Engine
- **Execute** Python code in a separate subprocess with an allowlisted environment (no API keys); see [Security](#security-considerations) for what is and isn't isolated
- **Inject** context variables into execution namespace
- **Capture** stdout, stderr, and return values
- **Timeout** protection with real process boundaries
- **Debug helpers** built-in (debug_context, pretty_print)

### State Persistence
- **Store** state across conversations with dot-notation keys
- **Organize** with namespaces for different projects/domains
- **Track** workflow progress, preferences, and decisions
- **Share** state between skills and sessions: skills read and write it directly via the state bridge, and writes commit only if the run succeeds

### Execution Telemetry
- **Record** every run (top-level, nested and `execute_code`) with status, latency and error
- **Compare** success rate and latency per skill version after an edit
- **Spot** failing skills from `get_orchestrator_status`

### Artifact System (Out-of-Band Storage)
- **Store** large outputs without flooding context
- **Retrieve** content in chunks as needed
- **TTL-based** expiration for automatic cleanup
- **Handle-based** references (`artifact://...`) for efficiency

### Progressive Discovery
- **Search** skills by metadata without loading code
- **Filter** by category, tags, or text query
- **Paginate** results for large skill libraries
- **Load** full definitions only when inspection is needed

---

## Why This Matters: Token Efficiency & Context Management

Traditional MCP usage passes tool definitions and results through the context window, consuming excessive tokens. The code execution pattern solves this:

| Problem | Solution |
|---------|----------|
| Large data floods context | Process locally with `execute_code`, return only results |
| Repeating complex logic | Create a skill once, invoke by name forever |
| Intermediate results | Store as artifacts, retrieve chunks as needed |
| Session state lost | Persist to state namespace, recall next session |
| Tool definitions bloat | Progressive disclosure - search metadata, load code on-demand |
| Re-discovering solutions | Skills accumulate - build once, use everywhere |

**Real-world impact:** Anthropic's research shows this pattern can reduce context usage by 98%+ for data-heavy operations.

---

## How AI Agents Can Leverage This System

### 1. Context Offloading
Instead of processing large datasets in conversation context:
```python
# DON'T: Pass 10MB CSV through context
# DO: Process locally, return summary
await execute_code({
    "code": """
import pandas as pd
df = pd.read_csv('/path/to/large.csv')
summary = {
    'rows': len(df),
    'columns': list(df.columns),
    'sample': df.head(3).to_dict()
}
__result__ = summary
""",
    "return_mode": "summary"  # Returns preview + artifact handle
})
```

### 2. Self-Improvement Through Skill Creation
When discovering a useful pattern, encode it as a skill:
```python
# Discovered a good codebase mapping approach? Save it:
await create_skill({
    "name": "context_efficient_codebase_mapper",
    "description": "Maps unfamiliar codebases without flooding context",
    "code": "...",  # The pattern you discovered
    "tags": ["codebase", "analysis", "context-efficiency"]
})
# Now available in all future conversations!
```

### 3. Building Domain Expertise
Create specialized skills for different domains:
- **Game Development:** Decision chronicles, asset pipelines, design pattern helpers
- **Data Science:** Processing pipelines, visualization generators, report builders
- **Writing:** Style-consistent generators, editing workflows, research organizers
- **DevOps:** Deployment automators, monitoring analyzers, incident responders

### 4. Memory & Continuity
The state system enables persistent memory:
```python
# End of session - save context
await set_state({
    "key": "session.last_task",
    "value": {"file": "server.py", "line": 450, "task": "implementing composition"},
    "namespace": "project_x"
})

# Next session - recall context
state = await get_state("session.last_task", "project_x")
# AI knows exactly where you left off
```

### 5. Skill Composition for Complex Workflows
Build sophisticated pipelines from simple primitives:
```python
# A parent skill that orchestrates multiple child skills
comprehension = invoke_skill('domain_context_switcher', {'action': 'detect', 'message': user_input})
session_data = invoke_skill('persistent_session_context', {'action': 'recall', 'domain': comprehension['domain']})
tasks = invoke_skill('omni_task_manager', {'action': 'list', 'domain': comprehension['domain']})

# Combine results into intelligent response
__result__ = {
    'context': comprehension,
    'session': session_data,
    'relevant_tasks': tasks,
    'call_depth': __call_depth__
}
```

---

## Available Tools (22 MCP Tools)

### Skill Management

| Tool | Description |
|------|-------------|
| `create_skill` | Create a new reusable skill with code, metadata, examples and test cases (tests run before saving) |
| `invoke_skill` | Execute a stored skill with parameters and context |
| `get_skill` | Get full skill details including code, status and health (optionally a past `version`) |
| `update_skill` | Update code, description, tags, dependencies, schema, category, examples or tests; saves a new version (tests run first) |
| `verify_skill` | Re-run a skill's test cases and update its status (verified / draft / unverified) |
| `delete_skill` | Delete a skill permanently (refused if other skills call it, unless `force=True`) |
| `search_skills` | Search and filter skills with pagination; shows status and `degraded` |

### Portability

| Tool | Description |
|------|-------------|
| `export_skill` | Write a skill as an agentskills.io folder (`SKILL.md` + `scripts/`) |
| `import_skill` | Create a skill from such a folder (goes through the verified-save gate) |

### Versioning

| Tool | Description |
|------|-------------|
| `list_skill_versions` | Version history with per-version runs, failure rate and latency |
| `diff_skill_versions` | Metadata changes and a unified code diff between two versions |
| `rollback_skill` | Restore an earlier version's content (saved as a new version) |

### Code Execution

| Tool | Description |
|------|-------------|
| `execute_code` | Execute Python code in isolated subprocess environment |

### Artifact Management

| Tool | Description |
|------|-------------|
| `store_artifact` | Store large content out-of-band, returns `artifact://...` handle |
| `get_artifact` | Retrieve artifact content in chunks (prevents context flooding) |

### State Management

| Tool | Description |
|------|-------------|
| `set_state` | Store a value in persistent state (dot notation supported) |
| `get_state` | Retrieve a value from state |
| `list_state` | List all keys in a namespace with pagination |
| `delete_state` | Delete a key (and everything under it) |

### Utilities

| Tool | Description |
|------|-------------|
| `list_categories` | List available skill categories |
| `get_orchestrator_status` | Get server status, statistics, and storage info |
| `get_skill_stats` | Execution telemetry: overview of all skills, or one skill's totals, per-version breakdown and recent runs |

### MCP Resources

| Resource | Description |
|----------|-------------|
| `skills://list` | List top 200 skills by usage |
| `skills://{name}` | Get specific skill by name |

---

## Skill Composition System

Skills can invoke other skills, enabling powerful composition patterns. This transforms isolated utilities into composable building blocks.

### Composition Functions (Available in All Skills)

```python
# Call another skill and get its __result__
result = invoke_skill('child_skill', {'param': 'value'}, {'context': 'data'})

# List all available skills (name -> description dict)
skills = list_available_skills()

# Get metadata about a skill without invoking it
info = get_skill_info('some_skill')

# Context variables available in all skills:
# __call_depth__ - Current nesting depth (1 for top-level, 2 for first child, etc.)
# __parent_skill__ - Name of the calling skill (None for top-level)
# __skill_name__ - Current skill's name
# __skill_version__ - Version of the skill that is running
```

Nested calls are held to the same contracts as top-level ones: a child with
unmet dependencies raises `RuntimeError`, and parameters that fail the child's
`parameters_schema` raise `ValueError`.

### Safety Features

| Feature | Description |
|---------|-------------|
| **Recursion Detection** | Circular calls detected and blocked with clear stack trace |
| **Depth Limiting** | Maximum 10 levels deep (configurable) |
| **Error Messages** | Shows available skills when "not found" error occurs |
| **Stack Tracking** | Full call chain visible in errors |

### Example: Composing a Workflow

```python
# Parent skill that orchestrates multiple children
await create_skill({
    "name": "smart_assistant",
    "description": "Context-aware assistant using skill composition",
    "code": """
message = params.get('message', '')

# 1. Understand the domain context
context = invoke_skill('domain_context_switcher', {
    'action': 'detect',
    'message': message
})

# 2. Load session history
session = invoke_skill('persistent_session_context', {
    'action': 'recall',
    'domain': context.get('primary_domain')
})

# 3. Get relevant tasks
tasks = invoke_skill('omni_task_manager', {
    'action': 'list',
    'filters': {'domain': context.get('primary_domain')}
})

__result__ = {
    'response': f'Processed: {message}',
    'domain_context': context,
    'session_history': session,
    'relevant_tasks': tasks,
    'composition_depth': __call_depth__
}
""",
    "category": "workflow",
    "tags": ["assistant", "composition", "context-aware"]
})
```

---

## State Bridge

Skills (and `execute_code`) can use the same persistent state as the
`get_state` / `set_state` tools, without a round trip through the agent:

```python
count = get_state('stats.runs', namespace='my_project', default=0)
set_state('stats.runs', count + 1, namespace='my_project')
delete_state('scratch', namespace='my_project')       # True if it existed
keys = list_state_keys('my_project')                  # ['stats.runs', ...]
```

How writes behave:

- **All or nothing.** Writes are buffered and committed only when the whole
  run succeeds. If the skill raises or times out, nothing is written and the
  output says how many writes were discarded.
- **Nested rollback.** If a child skill raises and the parent catches it, the
  child's writes are rolled back; the parent's are kept.
- **Read your writes.** Within a run, `get_state` sees pending writes.
- **Merge, not overwrite.** On commit, the run's operations are replayed onto
  the current file under a cross-process file lock, so keys the skill didn't
  touch (for example, written meanwhile by another agent or another server
  process) are preserved. Last writer wins per key.
- `sys.exit(0)` in the top-level skill ends the run cleanly (writes are
  committed); any other exit code, an exception or a timeout discards them.
  In a nested skill, `sys.exit()` rolls back that child's writes; the caller
  can catch the `SystemExit`, and if nothing does, the whole run fails (it
  is not treated as a clean finish).
- Values must be strict JSON (checked at `set_state` time; `NaN` and
  `Infinity` are rejected). Namespaces follow the same rules as the tools (no
  path separators, no leading dot).

The legacy in-process mode (`SKILL_ORCHESTRATOR_EXEC_MODE=inprocess`) does not
provide the state bridge or composition.

---

## Safe Self-Editing: Versions, Contracts, Telemetry

These features let an agent edit its own skills without fear of silently
breaking them.

### Version history and rollback

Every create, update and rollback saves a full snapshot
(`skills/.versions/<name>/v<N>.json`, newest 50 kept by default).

```python
update_skill(name="csv_summary", code=new_code, change_note="stream rows",
             expected_version=3)            # refused if someone else edited it first,
                                            # even from another server process
list_skill_versions("csv_summary")          # runs / failure rate / p50 per version
diff_skill_versions("csv_summary", from_version=3)
rollback_skill("csv_summary", version=3)    # saved as v5; the rollback can be undone too
```

An update that changes nothing does not create a version.

### Contracts: dependencies and parameter schemas

- **`dependencies`** are requirement strings (`"pandas"`, `"requests>=2.31"`,
  `"python>=3.11"`). They are checked against the interpreter that runs skills,
  without importing anything. Unparseable strings are rejected at
  create/update; unmet ones produce a warning there, and the skill is refused
  at invoke time with an install hint instead of failing mid-run with
  `ImportError`. Free-form entries already stored in older skills (e.g.
  `"json (stdlib)"`) are reported as warnings and never block a run. Control
  this with `SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT` (`strict`, `warn`,
  `off`).
- **`parameters_schema`** must be a valid JSON Schema for an object (the draft
  is taken from `$schema`, default 2020-12; every `$ref` must resolve inside
  the schema, since remote references are never fetched). Every invocation is validated against it before
  the skill runs, and top-level `default` values are filled in. Control this with
  `SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT` (`strict`, `warn`, `off`). Legacy
  skills whose stored schema is invalid run with a warning.
- **Skill-to-skill references** (`invoke_skill('name', ...)` with a literal
  name) are detected: create/update warns about unknown skills, `get_skill`
  lists them, and `delete_skill` refuses to delete a skill others call unless
  `force=True`.

Runs refused by a contract are recorded in telemetry with status `rejected`.

### Execution telemetry

Every run is appended to `~/.skill_orchestrator/telemetry/executions.jsonl`
(local only; once it passes 12,500 records it is trimmed to the newest 10,000) with timestamp, skill, version, status
(`ok`, `error`, `timeout`, `rejected`), wall-clock and in-child execution time,
error summary, parent skill and depth for nested calls, and the number of
state writes committed.

```python
get_skill_stats()                  # table of all skills + last errors
get_skill_stats("csv_summary")     # totals, per-version breakdown, recent runs
```

The typical loop after an edit: invoke a few times, check
`list_skill_versions` or `get_skill_stats`, and `rollback_skill` if the new
version's failure rate went up.

Nested calls made by a run that times out are not recorded (the child process
is killed before it can report them); the top-level timeout is.

---

## Verified Saves and Skill Lifecycle

Self-written skills only help when they actually work: SkillsBench (2026)
found that agent-generated skills gave no benefit on average and hurt some
tasks unless they were checked by running them. So `create_skill` and
`update_skill` run the skill's test cases in the sandbox **before** saving.

```python
create_skill(
    name="csv_row_count",
    description="Count data rows in a CSV string",
    code="__result__ = len(params['text'].strip().splitlines()) - 1",
    tests=[
        {"parameters": {"text": "a,b\n1,2\n3,4"}, "expected_result": 2},
        {"name": "header only", "parameters": {"text": "a,b"}, "expected_result": 0},
    ],
    examples=["How many rows are in this CSV?", '{"text": "a,b"}'],
)
```

- **Test cases** are `tests` (parameters, context, optional
  `expected_result` compared as JSON, optional `timeout`) plus any example
  that is a JSON object, which is run as parameters. Up to 10 cases.
- Parameters are checked against `parameters_schema` (defaults filled in)
  before each case runs.
- A case passes if the run finishes without error (and, when given, the
  result matches). **If any case fails the skill is not saved** and the
  failures are returned. Pass `save_as_draft=True` to keep it anyway with
  status `draft`.
- Test runs **never commit state** (writes are dropped) and are not recorded
  in telemetry. They do run the code, so a skill with side effects (files,
  network) has them during verification too.
- `update_skill` re-runs the tests only when code, schema, dependencies,
  tests or examples change. A failing update leaves the current version in
  place.
- `verify_skill(name)` re-runs the tests without creating a version, e.g.
  after installing a missing dependency or fixing a skill a draft calls.
- `rollback_skill` restores the status the target version had.

**Statuses:** `verified` (all cases passed), `unverified` (nothing to run:
no cases, or unmet dependencies), `draft` (a case failed and it was saved
anyway). Skills created before this feature show as `unverified`. Set
`SKILL_ORCHESTRATOR_REQUIRE_VERIFIED_SKILLS=on` to refuse unverified saves
(they can still be saved as drafts).

**Degraded:** `search_skills`, `get_skill` and `get_orchestrator_status`
flag a skill as degraded when more than 50% of its current version's last
20 runs failed (minimum 5 runs). A new version starts clean. Nothing is
deleted or rolled back automatically; the flag tells the agent to look at
`get_skill_stats` and consider `rollback_skill`. Invoking a `draft` adds a
warning to the output.

---

## Portability: SKILL.md Export and Import

`SKILL.md` ([agentskills.io](https://agentskills.io), Dec 2025) is the
cross-vendor skill format. Skills can be moved in and out of it:

```python
export_skill("csv_row_count", directory="/home/me/skills")
import_skill(path="/home/me/skills/csv-row-count")   # on another machine/server
```

The folder looks like this:

```
csv-row-count/
├── SKILL.md                            # frontmatter: name, description, compatibility, metadata
├── scripts/
│   ├── csv_row_count.py                # the skill code
│   └── run.py                          # python scripts/run.py '{"text": "a,b"}' -> prints 0
├── examples/examples.json              # {"examples": [...], "tests": [...]}
└── references/parameters.schema.json   # only if the skill has a schema
```

- Spec names are lowercase with hyphens, so `csv_row_count` becomes
  `csv-row-count`. The original name, category, tags, dependencies, version
  and entrypoint are kept in `metadata` (string values, lists as JSON) so
  the round trip is exact.
- `import_skill` also accepts SKILL.md folders from other tools: the code is
  `metadata.entrypoint`, or the only `.py` file in `scripts/`.
  Instruction-only skills (no script) are refused. Use `name=` to import
  under a different name.
- Imports go through `create_skill`, so test cases run before saving.
- Paths must be absolute. Reads never leave the skill folder (symlinks
  included). `export_skill` won't write into a non-empty folder unless
  `overwrite=True`, and never deletes files. Set
  `SKILL_ORCHESTRATOR_PORTABLE_ROOT` to confine both tools to one folder.
- `scripts/run.py` runs the code without the orchestrator; composition and
  state helpers (`invoke_skill`, `get_state`, ...) aren't available there.

---

## Debugging & Development

### Built-in Debug Helpers

All skill executions have access to debugging utilities:

```python
# Inspect all available variables in execution context
debug_context(**locals())
# Output:
# === Execution Context Debug ===
#   params: dict = {'file': 'data.csv'}
#   context: dict = {'env': 'production'}
#   __call_depth__: int = 1
# ===============================

# Pretty-print JSON-serializable objects
pretty_print({"users": [{"name": "Alice"}, {"name": "Bob"}]})
# Output:
# {
#   "users": [
#     {"name": "Alice"},
#     {"name": "Bob"}
#   ]
# }
```

### Enhanced Error Messages

When skills fail, error messages include:
- Full traceback with line numbers
- Available context variables list
- Tip to use `debug_context()` for inspection

```
**Error:** name 'undefined_var' is not defined
```traceback...```

**Available context variables:** params, context, __call_depth__, custom_var
**Tip:** Use `debug_context(**locals())` in your skill code to inspect available variables.
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SKILL_ORCHESTRATOR_DEFAULT_EXECUTION_TIME_SECONDS` | 30 | Default timeout for code execution |
| `SKILL_ORCHESTRATOR_MAX_EXECUTION_TIME_SECONDS` | 180 | Maximum allowed timeout |
| `SKILL_ORCHESTRATOR_TERMINATION_GRACE_SECONDS` | 5 | Grace period before force-kill |
| `SKILL_ORCHESTRATOR_MAX_CALL_DEPTH` | 10 | Maximum skill nesting depth |
| `SKILL_ORCHESTRATOR_EXEC_MODE` | subprocess | Execution mode (subprocess or inprocess) |
| `SKILL_ORCHESTRATOR_SCHEMA_ENFORCEMENT` | strict | Parameter schema checks: `strict` (reject), `warn` (run and report), `off` |
| `SKILL_ORCHESTRATOR_DEPENDENCY_ENFORCEMENT` | strict | Dependency checks: `strict` (refuse), `warn` (run and report), `off` |
| `SKILL_ORCHESTRATOR_TELEMETRY` | on | Set to `off` to stop recording executions |
| `SKILL_ORCHESTRATOR_TELEMETRY_MAX_RECORDS` | 10000 | Telemetry log size cap (oldest records dropped) |
| `SKILL_ORCHESTRATOR_MAX_SKILL_VERSIONS` | 50 | Snapshots kept per skill |
| `SKILL_ORCHESTRATOR_ENV_PASSTHROUGH` | (empty) | Comma-separated extra env var names copied into the skill process (see [Security](#security-considerations)) |
| `SKILL_ORCHESTRATOR_REQUIRE_VERIFIED_SKILLS` | off | `on` refuses to save skills that have no runnable test cases (unless saved as drafts) |
| `SKILL_ORCHESTRATOR_DEGRADED_FAILURE_RATE` | 0.5 | Share of recent runs that must fail for a skill to show as degraded |
| `SKILL_ORCHESTRATOR_DEGRADED_WINDOW` | 20 | Number of recent runs (of the current version) considered |
| `SKILL_ORCHESTRATOR_DEGRADED_MIN_RUNS` | 5 | Minimum recent runs before a skill can be degraded |
| `SKILL_ORCHESTRATOR_PORTABLE_ROOT` | (unset) | If set, `export_skill` / `import_skill` only accept paths inside this folder |
| `SKILL_ORCHESTRATOR_TRUST_TIER` | standard | `trusted`, `standard` or `restricted` (see [Trust tiers](#trust-tiers)). An unknown value means `restricted` |
| `SKILL_ORCHESTRATOR_MEMORY_LIMIT_MB` | per tier | Override the tier's memory limit (`0`/`off` = none) |
| `SKILL_ORCHESTRATOR_FILE_SIZE_LIMIT_MB` | per tier | Override the tier's largest-file-written limit (`0`/`off` = none; POSIX only) |
| `SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES` | per tier | `on`/`off`: override whether skills may start processes |

### Timeout Configuration

```python
# Per-call timeout (up to max)
await invoke_skill({
    "skill_name": "long_running_skill",
    "timeout": 150,  # seconds
    "return_mode": "full"
})
```

---

## Storage Architecture

```
~/.skill_orchestrator/
├── skills/
│   ├── skill_name.json          # Full skill definition (name, code, metadata)
│   ├── .skill_index.json        # Metadata-only index (fast search, excludes code)
│   ├── .skill_usage.json        # Invocation counts (write-light tracking)
│   └── .versions/
│       └── skill_name/v1.json   # Full snapshot of every saved version
├── state/
│   └── namespace_name.json      # Key-value pairs for each namespace
├── telemetry/
│   └── executions.jsonl         # One line per execution (capped)
├── sessions/                     # Reserved for future use
└── artifacts/
    ├── {id}.meta.json           # Artifact metadata (MIME type, size, TTL)
    └── {id}.data                # Artifact binary content
```

### Storage Design Principles

- **Progressive Disclosure:** Skill index excludes code - load only when needed
- **Write-Light:** Usage counts in separate file to avoid rewriting skills on every invoke
- **Atomic Operations:** Temp file + rename pattern for data integrity
- **Thread-Safe:** Locks on all shared state (index, usage, artifacts)

---

## Skill Categories

| Category | Description | Example Use Cases |
|----------|-------------|-------------------|
| `data_processing` | Data transformation and analysis | CSV processing, JSON transformation |
| `file_operations` | File system operations | Batch renaming, directory organization |
| `api_integration` | External API connections | REST clients, webhook handlers |
| `text_analysis` | Text processing and NLP | Summarization, extraction, formatting |
| `automation` | Workflow automation | Build pipelines, deployment scripts |
| `visualization` | Charts and visual outputs | Data plots, diagrams |
| `computation` | Math and scientific computing | Calculations, simulations |
| `workflow` | Multi-step pipelines | Orchestration, chaining |
| `utility` | General utilities | Helpers, converters |
| `custom` | Uncategorized | Domain-specific skills |

---

## Quick Start

### Installation

```bash
# Clone the repository
git clone https://github.com/your-username/skill-orchestrator-mcp
cd skill-orchestrator-mcp

# Install with uv (recommended)
uv venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
uv sync
```

### Running the Server

```bash
# Using the CLI
skill-orchestrator-mcp

# Or directly with Python
python -m skill_orchestrator_mcp.server

# With uvx for development
uvx --from . skill-orchestrator-mcp
```

### Configure for Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "skill-orchestrator": {
      "command": "path/to/.venv/bin/skill-orchestrator-mcp"
    }
  }
}
```

Or with uvx:

```json
{
  "mcpServers": {
    "skill-orchestrator": {
      "command": "uvx",
      "args": ["--from", "/path/to/skill-orchestrator-mcp", "skill-orchestrator-mcp"]
    }
  }
}
```

---

## Usage Examples

### Creating a Data Processing Skill

```python
await create_skill({
    "name": "process_csv_summary",
    "description": "Load a CSV and return summary statistics without flooding context",
    "code": """
import csv
from pathlib import Path

file_path = params.get('file_path')
sample_rows = params.get('sample_rows', 5)

with open(file_path, 'r') as f:
    reader = csv.DictReader(f)
    rows = list(reader)

__result__ = {
    "file": Path(file_path).name,
    "row_count": len(rows),
    "columns": list(rows[0].keys()) if rows else [],
    "sample": rows[:sample_rows]
}
""",
    "category": "data_processing",
    "tags": ["csv", "data", "summary"],
    "examples": ["Summarize the sales.csv file", "Show me what's in data.csv"]
})
```

### Searching Skills (Progressive Discovery)

```python
# Search by metadata - doesn't load code (context efficient)
await search_skills({
    "query": "csv",
    "category": "data_processing",
    "limit": 10,
    "response_format": "json",
    "fields": ["name", "description", "tags"]
})

# Only load code when you need to inspect/modify
await get_skill("process_csv_summary")
```

### Invoking a Skill

```python
await invoke_skill({
    "skill_name": "process_csv_summary",
    "parameters": {
        "file_path": "/path/to/data.csv",
        "sample_rows": 3
    },
    "return_mode": "summary",  # preview + artifact handle (default)
    "timeout": 60
})
```

### Direct Code Execution

```python
await execute_code({
    "code": """
# Process data without passing through context
data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
filtered = [x for x in data if x > 5]
total = sum(filtered)

print(f"Filtered: {filtered}")
print(f"Sum: {total}")

__result__ = {"filtered": filtered, "sum": total}
""",
    "timeout": 10,
    "return_mode": "summary"
})
```

### State Management

```python
# Store workflow progress
await set_state({
    "key": "workflow.current_step",
    "value": {"step": 3, "status": "processing", "started": "2025-01-16T10:00:00Z"},
    "namespace": "my_project"
})

# Retrieve later (even in a different conversation)
state = await get_state("workflow.current_step", "my_project")

# List all keys in a namespace
await list_state({
    "namespace": "my_project",
    "response_format": "json"
})
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      Claude / AI Agent                          │
├─────────────────────────────────────────────────────────────────┤
│                     MCP Protocol Layer                          │
├─────────────────────────────────────────────────────────────────┤
│                Skill Orchestrator MCP Server                    │
├────────────┬────────────┬────────────┬────────────┬────────────┤
│   Skill    │    Code    │   State    │  Artifact  │   Usage    │
│  Registry  │  Executor  │  Manager   │   Store    │  Tracker   │
│            │ (subprocess)│            │ (chunked)  │ (batched)  │
├────────────┴────────────┴────────────┴────────────┴────────────┤
│                      Skill Index                                │
│              (metadata-only, progressive disclosure)            │
├─────────────────────────────────────────────────────────────────┤
│                   File System Storage                           │
│          ~/.skill_orchestrator/{skills,state,artifacts}         │
└─────────────────────────────────────────────────────────────────┘
```

### Execution Flow

1. **Subprocess Mode (Default):** Creates temp directory with runner script and payload, spawns subprocess, enforces real timeouts via process boundary
2. **Skill Composition:** Registry of all skills passed to subprocess, enabling `invoke_skill()` calls within skills
3. **Artifact Handling:** Large outputs stored to disk, lightweight handles returned
4. **Progressive Discovery:** Index searched without loading code, full definitions loaded on-demand

---

## Security Considerations

The skill runner is a **process boundary, not a sandbox**. Skills and
`execute_code` run code the agent wrote, as the same OS user as the server.
Treat the server as having the permissions of that user.

### What is isolated

| Protection | Details |
|---|---|
| **Separate process** | Each run is a fresh `python` subprocess in a throwaway temp directory, with `stdin` closed. A crash or `sys.exit()` can't take down the server. |
| **Real timeouts** | The run is killed when the timeout passes (default 30 s, max 180 s). |
| **Process-tree kill** | Each run is the root of its own process tree (a new session/process group on POSIX, a job object on Windows). When the run ends, finished or timed out, everything it started is killed too, so background processes cannot outlive it or hold its output open. |
| **Resource limits** | Memory, file-size and process limits set by the [trust tier](#trust-tiers). |
| **Scrubbed environment** | The child does **not** inherit the server's environment. It gets only what Python needs to start: `PATH`, locale (`LANG`, `LC_*`, `TZ`), `HOME`/`TMPDIR` on POSIX, and `SYSTEMROOT`, `SYSTEMDRIVE`, `WINDIR`, `COMSPEC`, `PATHEXT`, `TEMP`/`TMP`, `USERPROFILE`, `HOMEDRIVE`/`HOMEPATH`, `APPDATA`/`LOCALAPPDATA`, `PROGRAMDATA`, `PROCESSOR_ARCHITECTURE`, `NUMBER_OF_PROCESSORS` on Windows, plus `PYTHONIOENCODING=utf-8` and `PYTHONDONTWRITEBYTECODE=1`. API keys, tokens, proxy settings and `SKILL_ORCHESTRATOR_*` settings are not passed. Processes the skill starts inherit the same scrubbed environment. |
| **Operator-only passthrough** | `SKILL_ORCHESTRATOR_ENV_PASSTHROUGH=NAME1,NAME2` copies those named variables from the server's environment. It is read from the server's own environment, so an MCP client cannot set it; tool arguments (`context`, `parameters`) become Python variables, never environment variables. Only pass what skills must have, and remember any skill can then read it. `get_orchestrator_status` lists the names (never values). |
| **State writes** | Skills reach persistent state only through the state bridge (validated namespaces, committed only on success). |
| **Verified saves** | New and changed skills run their test cases before they are saved (see above). |

### What is NOT isolated

- **Filesystem:** a skill can read and write anything the server's user can,
  including `~/.skill_orchestrator` itself (skills, state, telemetry) and
  credential files such as `~/.aws/credentials` or `~/.netrc`. The scrubbed
  environment only removes secrets that live in environment variables.
- **Network:** unrestricted outbound access.
- **Limits are guard rails, not a boundary:** they stop runaway or buggy
  code. Code that sets out to escape can, because it runs as the server's
  user: on POSIX a process can leave its process group with `setsid()`
  (the restricted tier blocks new processes, which closes this), and on
  Windows a same-user process can open and change its job object. There is
  no CPU limit beyond the wall-clock timeout, and no disk-quota limit.
- **Other processes:** no namespaces, seccomp, or containers.
- **In-process mode:** `SKILL_ORCHESTRATOR_EXEC_MODE=inprocess` runs code
  inside the server with full access to its memory and environment (no
  scrub, no timeout, no limits). Do not use it with untrusted code. The
  restricted tier ignores it.
- **Verification side effects:** test cases run the real code, so a skill
  that writes files or calls an API does so while being verified.

For untrusted code, run the whole server inside a container or VM with no
credentials mounted and a network egress allowlist.

### Trust tiers

The operator picks how much the server trusts the code it runs with
`SKILL_ORCHESTRATOR_TRUST_TIER`. Like the env passthrough, it is read from the
server's own environment on every run; no tool call can change it.

| | `trusted` | `standard` (default) | `restricted` |
|---|---|---|---|
| Memory | no limit | 4096 MB | 1024 MB |
| Largest file written | no limit | 1024 MB | 64 MB |
| Starting processes | allowed | allowed | blocked |
| Process-tree kill | yes | yes | yes |
| `execute_code`, `create_skill`, `update_skill`, `import_skill`, `rollback_skill`, `delete_skill` | allowed | allowed | refused |
| Which skills run | all (drafts warn) | all (drafts warn) | `verified` only, including nested calls |
| Env passthrough | honored | honored | ignored |
| In-process mode | honored | honored | ignored (always a subprocess) |
| A limit that can't be enforced | warning in the output | warning in the output | run refused |

Use `restricted` to serve a vetted skill library: author and verify skills
under `standard`, then switch. `verify_skill` stays available, so a saved
skill with test cases can be verified in place. An unknown tier value
(e.g. a typo) is treated as `restricted`, and `get_orchestrator_status`
says so.

The three limit variables override a tier's limits one at a time; they
never change its tool policy. How each limit is enforced:

| Limit | Linux / macOS | Windows |
|---|---|---|
| Memory | `RLIMIT_AS`, per process | Job object, committed memory of the whole tree |
| File size | `RLIMIT_FSIZE`, per file | Not enforced |
| No processes | `RLIMIT_NPROC=0`. On Linux this also blocks new threads. Not enforced for root: run as another user, or set `SKILL_ORCHESTRATOR_ALLOW_SUBPROCESSES=on` to accept that | Job object active-process limit of 1 |

POSIX limits are set by the runner before any skill code runs, with hard
limits lowered so the code cannot raise them again. On Windows the runner
waits until it is inside its job object before running any code. A
memory-limit hit surfaces as a normal `MemoryError` in the run's output.
CI covers Linux and Windows; macOS uses the same POSIX calls but is not
tested.

### No package installation

The orchestrator never installs packages. Declared `dependencies` are only
checked against the interpreter that runs skills; unmet ones are refused at
invoke time with an install hint for the operator. There is deliberately no
`uv run --with` / `pip install` path: installing arbitrary packages chosen by
an agent runs their install hooks and fetches code from the network, which
is unsafe without a network allowlist and a package allowlist. If such a path
is ever added it must stay off by default.

```bash
# Use subprocess mode (default, recommended)
# SKILL_ORCHESTRATOR_EXEC_MODE=subprocess

# Legacy in-process mode (not recommended - no timeout, no env scrub)
# SKILL_ORCHESTRATOR_EXEC_MODE=inprocess
```

---

## Future Roadmap

- [ ] Docker-based sandboxed execution for enhanced security (filesystem/network isolation)
- [x] Skill composition and chaining (Implemented!)
- [ ] Built-in skill templates for common patterns
- [ ] Remote skill sharing and discovery
- [x] Execution analytics and performance tracking (Implemented!)
- [ ] OAuth integration for API-connected skills
- [ ] WebSocket transport for streaming outputs
- [ ] Semantic skill discovery (embedding-based search)
- [x] Skill versioning with rollback support (Implemented!)
- [x] Dependency and parameter-schema enforcement (Implemented!)
- [x] State bridge: persistent state from inside skills (Implemented!)
- [x] Verified-save gate: test cases run before a skill is saved (Implemented!)
- [x] Scrubbed environment for skill processes (Implemented!)
- [x] SKILL.md (agentskills.io) export and import (Implemented!)
- [x] Operator trust tiers, process-tree kill and resource limits (Implemented!)
- [ ] Filesystem and network isolation for skill processes

---

## Background & Inspiration

This server implements patterns from:
- [Anthropic's "Code Execution with MCP"](https://www.anthropic.com/engineering/code-execution-with-mcp) - Reduce context usage dramatically
- [Docker MCP Gateway](https://github.com/docker/mcp-gateway) - Orchestrate multiple MCP servers
- [Cloudflare's "Code Mode"](https://blog.cloudflare.com/code-mode/) - TypeScript sandbox execution

---

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - see LICENSE file for details.

## Acknowledgments

- Anthropic for the MCP specification and code execution patterns
- Docker for MCP Gateway inspiration
- Cloudflare for Code Mode insights
- The FastMCP community
