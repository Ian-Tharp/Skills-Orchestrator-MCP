# Skill Orchestrator MCP Server

A self-evolving MCP (Model Context Protocol) server that enables AI agents to dynamically create, manage, and execute reusable skills. Implements Anthropic's "Code Execution with MCP" pattern for context-efficient agent-tool interaction.

---

## Table of Contents

- [The Vision: AI That Grows With You](#the-vision-ai-that-grows-with-you)
- [Key Capabilities](#key-capabilities)
- [Why This Matters: Token Efficiency & Context Management](#why-this-matters-token-efficiency--context-management)
- [How AI Agents Can Leverage This System](#how-ai-agents-can-leverage-this-system)
- [Available Tools](#available-tools-19-mcp-tools)
- [Skill Composition System](#skill-composition-system)
- [State Bridge](#state-bridge)
- [Safe Self-Editing: Versions, Contracts, Telemetry](#safe-self-editing-versions-contracts-telemetry)
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

### Code Execution Engine
- **Execute** Python code in isolated subprocess environments
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

## Available Tools (19 MCP Tools)

### Skill Management

| Tool | Description |
|------|-------------|
| `create_skill` | Create a new reusable skill with code, metadata, and examples |
| `invoke_skill` | Execute a stored skill with parameters and context |
| `get_skill` | Get full skill details including code (optionally a past `version`) |
| `update_skill` | Update code, description, tags, dependencies, schema, category or examples; saves a new version |
| `delete_skill` | Delete a skill permanently (refused if other skills call it, unless `force=True`) |
| `search_skills` | Search and filter skills with pagination |

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
  In a nested skill, `sys.exit()` ends only that child: the caller gets a
  `RuntimeError` it can catch, and the child's writes are rolled back.
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

- Code runs in isolated subprocess with separate Python interpreter
- Execution timeout enforced via process boundary (can't be bypassed)
- Standard library access (use carefully for network/file operations)
- Output truncation prevents memory issues
- Skills can read and write persistent state through the state bridge; namespaces are validated the same way as the MCP tools, so a skill cannot write outside the state directory through it (it can still use the filesystem directly, see above)
- Graceful termination with configurable grace period

```bash
# Use subprocess mode (default, recommended)
# SKILL_ORCHESTRATOR_EXEC_MODE=subprocess

# Legacy in-process mode (not recommended - no timeout guarantee)
# SKILL_ORCHESTRATOR_EXEC_MODE=inprocess
```

---

## Future Roadmap

- [ ] Docker-based sandboxed execution for enhanced security
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
- [ ] Skill testing framework

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
