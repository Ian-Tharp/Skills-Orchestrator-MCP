# 🚀 Skill Orchestrator MCP Server

A self-evolving MCP (Model Context Protocol) server that enables AI agents to dynamically create, manage, and execute reusable skills. Implements Anthropic's "Code Execution with MCP" pattern for context-efficient agent-tool interaction.

## 🌟 Key Features

### Dynamic Skill Registry
- **Create** reusable code patterns (skills) on-the-fly
- **Store** skills persistently for future conversations
- **Search** skills by name, description, category, or tags
- **Invoke** skills with parameters
- **Version** skills automatically on updates

### Code Execution Engine
- **Execute** Python code in isolated namespaces
- **Inject** context variables into execution
- **Capture** stdout, stderr, and return values
- **Timeout** protection for runaway code

### State Persistence
- **Store** state across conversations
- **Organize** with namespaces
- **Access** nested values with dot notation
- **Track** workflow progress

### Progressive Discovery
- **List** available skills efficiently
- **Filter** by category or tags
- **Load** full definitions only when needed
- **Minimize** context window usage

## 📖 Background

This server implements patterns from:
- [Anthropic's "Code Execution with MCP"](https://www.anthropic.com/engineering/code-execution-with-mcp) - Reduce context usage by 98%+
- [Docker MCP Gateway](https://github.com/docker/mcp-gateway) - Orchestrate multiple MCP servers
- [Cloudflare's "Code Mode"](https://blog.cloudflare.com/code-mode/) - TypeScript sandbox execution

### Why Code Execution with MCP?

Traditional MCP usage passes tool definitions and results through the context window, consuming excessive tokens. The code execution pattern:

1. **Progressive Disclosure**: Load tool definitions on-demand
2. **Local Processing**: Filter/transform data in code before returning
3. **Efficient Control Flow**: Handle loops and conditionals in code
4. **State Persistence**: Write intermediate results to files/state
5. **Skill Evolution**: Create and save reusable code patterns

## 🚀 Quick Start

### Installation

#### Windows (PowerShell)

```powershell
uv venv
.\.venv\Scripts\Activate.ps1
uv sync
```

#### Windows (Git Bash) / macOS / Linux

```bash
# Install with uv (recommended)
uv venv

# Windows Git Bash:
source .venv/Scripts/activate

# macOS/Linux:
# source .venv/bin/activate

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
      "command": "path-to-your-.venv\\Scripts\\skill-orchestrator-mcp.exe"
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

## 🛠️ Available Tools

### Skill Management

| Tool | Description |
|------|-------------|
| `create_skill` | Create a new reusable skill |
| `invoke_skill` | Execute a stored skill |
| `get_skill` | Get full skill details including code |
| `update_skill` | Update an existing skill |
| `delete_skill` | Delete a skill permanently |
| `search_skills` | Search and filter skills |

### Code Execution

| Tool | Description |
|------|-------------|
| `execute_code` | Execute Python code in isolated environment |

### Artifacts (Handle-based Outputs)

| Tool | Description |
|------|-------------|
| `store_artifact` | Store large content out-of-band and get an `artifact://...` handle |
| `get_artifact` | Retrieve artifact content in chunks (prevents context blowups) |

### State Management

| Tool | Description |
|------|-------------|
| `set_state` | Store a value in persistent state |
| `get_state` | Retrieve a value from state |
| `list_state` | List all keys in a namespace |

### Utilities

| Tool | Description |
|------|-------------|
| `list_categories` | List available skill categories |
| `get_orchestrator_status` | Get server status and statistics |

## 📚 Usage Examples

### Creating a Skill

```python
# Create a CSV processing skill
await create_skill({
    "name": "process_csv_summary",
    "description": "Load a CSV and return a summary with row count, columns, and sample data",
    "code": """
import csv
from pathlib import Path

def main(file_path, sample_rows=5):
    path = Path(file_path)
    with open(path, 'r') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    return {
        "file": path.name,
        "row_count": len(rows),
        "columns": list(rows[0].keys()) if rows else [],
        "sample": rows[:sample_rows]
    }

result = main(params.get('file_path'), params.get('sample_rows', 5))
print(json.dumps(result, indent=2))
""",
    "category": "data_processing",
    "tags": ["csv", "data", "summary"],
    "dependencies": [],
    "examples": [
        "Summarize the sales.csv file",
        "Show me what's in the data.csv"
    ]
})
```

### Searching Skills (Progressive Discovery)

Use `search_skills` to find relevant skills **without loading code** (keeps context efficient). Then call `get_skill` only when you need to inspect/modify code.

```python
await search_skills({
  "query": "csv",
  "limit": 10,
  "cursor": 0,
  "response_format": "json",
  "fields": ["name", "description", "category", "tags"]
})

# JSON response shape:
# {
#   "total": 123,
#   "cursor": 0,
#   "next_cursor": 10,
#   "items": [ ... ]
# }
```

### Invoking a Skill

```python
await invoke_skill({
    "skill_name": "process_csv_summary",
    "parameters": {
        "file_path": "/path/to/data.csv",
        "sample_rows": 3
    },
    # return_mode defaults to "summary" to keep the context window small.
    # Use "full" if you explicitly want the full response in-context.
    "return_mode": "summary",
    # Increase for long-running skills (max is configurable server-side)
    "timeout": 150
})
```

### Direct Code Execution

```python
await execute_code({
    "code": """
# Process some data without passing it through context
data = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
filtered = [x for x in data if x > 5]
total = sum(filtered)
print(f"Filtered values: {filtered}")
print(f"Sum: {total}")
""",
    "timeout": 10,
    # return_mode defaults to "summary" (artifact handle + short preview)
    "return_mode": "summary"
})
```

### State Management

```python
# Store workflow progress
await set_state({
    "key": "workflow.current_step",
    "value": 3,
    "namespace": "my_project"
})

# Retrieve later
await get_state("workflow.current_step", "my_project")
# Returns: 3
```

## 🗂️ Skill Categories

| Category | Description |
|----------|-------------|
| `data_processing` | Processing and transforming data |
| `file_operations` | Reading, writing, and managing files |
| `api_integration` | Connecting to external APIs |
| `text_analysis` | Text processing and NLP |
| `automation` | Workflow automation |
| `visualization` | Charts and visual outputs |
| `computation` | Math and scientific computing |
| `workflow` | Multi-step pipelines |
| `utility` | General utilities |
| `custom` | Uncategorized |

## 📁 Storage Locations

```
~/.skill_orchestrator/
├── skills/          # Stored skill definitions (JSON)
│   ├── .skill_index.json  # Metadata-only skill index (generated)
│   └── .skill_usage.json  # Usage counts (generated)
├── state/           # Persistent state files (JSON)
├── sessions/        # Session data (future use)
└── artifacts/       # Large outputs stored out-of-band (artifact:// handles)
```

## 🔒 Security Considerations

- Code runs in isolated namespaces but has access to standard library
- Network and file operations are allowed (use carefully)
- Execution timeout prevents runaway code (default 30s; configurable)
- Output is truncated to prevent memory issues
- By default, code execution happens in a **subprocess boundary** so timeouts are enforced.
  - Set `SKILL_ORCHESTRATOR_EXEC_MODE=inprocess` to use legacy in-process `exec()` (not recommended).

### Timeout Configuration

- Per-call: pass `"timeout": 150` to `execute_code` or `invoke_skill`
- Server defaults (env vars):
  - `SKILL_ORCHESTRATOR_DEFAULT_EXECUTION_TIME_SECONDS` (default `30`)
  - `SKILL_ORCHESTRATOR_MAX_EXECUTION_TIME_SECONDS` (default `180`)
  - `SKILL_ORCHESTRATOR_TERMINATION_GRACE_SECONDS` (default `5`)

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Claude / AI Agent                     │
├─────────────────────────────────────────────────────────┤
│                   MCP Protocol Layer                     │
├─────────────────────────────────────────────────────────┤
│              Skill Orchestrator MCP Server               │
├──────────┬──────────┬──────────┬──────────┬────────────┤
│  Skill   │  Code    │  State   │ Progress │  Resource  │
│ Registry │ Executor │ Manager  │ Tracker  │   Access   │
├──────────┴──────────┴──────────┴──────────┴────────────┤
│                   File System Storage                    │
│        ~/.skill_orchestrator/{skills,state,sessions}     │
└─────────────────────────────────────────────────────────┘
```

## 🔮 Future Enhancements

- [ ] Docker-based sandboxed execution
- [ ] Skill composition and chaining
- [ ] Built-in skill templates
- [ ] Remote skill sharing
- [ ] Execution analytics
- [ ] OAuth integration for API skills
- [ ] WebSocket transport for streaming

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📄 License

MIT License - see LICENSE file for details.

## 🙏 Acknowledgments

- Anthropic for the MCP specification and code execution patterns
- Docker for MCP Gateway inspiration
- Cloudflare for Code Mode insights
- The FastMCP community

---
