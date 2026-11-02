"""
Built-in Skills for the Skill Orchestrator

These are pre-created skills that are immediately useful for common tasks.
They demonstrate best practices and can be customized.
"""

import json
from pathlib import Path
from typing import Any, Dict, List

# Directory for skills
SKILLS_DIR = Path.home() / ".skill_orchestrator" / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================================================
# Skill Definitions
# =============================================================================

BUILTIN_SKILLS: List[Dict[str, Any]] = [
    {
        "name": "json_transform",
        "description": "Transform JSON data using JSONPath-like queries and filters. Supports selecting fields, filtering arrays, and reshaping data structures.",
        "code": '''
"""
JSON Transform Skill
Transforms JSON data using simple query operations.

Parameters:
  data: The JSON data to transform (dict or list)
  operations: List of operations to apply
    - select: ["field1", "field2"] - select specific fields
    - filter: {"field": "value"} - filter array items
    - rename: {"old": "new"} - rename fields
    - flatten: True - flatten nested structures
"""
import json

def transform(data, operations):
    result = data

    for op in operations:
        op_type = op.get("type")

        if op_type == "select":
            fields = op.get("fields", [])
            if isinstance(result, list):
                result = [{k: item.get(k) for k in fields} for item in result]
            elif isinstance(result, dict):
                result = {k: result.get(k) for k in fields}

        elif op_type == "filter":
            conditions = op.get("conditions", {})
            if isinstance(result, list):
                result = [
                    item for item in result
                    if all(item.get(k) == v for k, v in conditions.items())
                ]

        elif op_type == "rename":
            mapping = op.get("mapping", {})
            if isinstance(result, list):
                result = [
                    {mapping.get(k, k): v for k, v in item.items()}
                    for item in result
                ]
            elif isinstance(result, dict):
                result = {mapping.get(k, k): v for k, v in result.items()}

        elif op_type == "flatten":
            if isinstance(result, list) and len(result) == 1:
                result = result[0]

    return result

# Get params from context
data = params.get("data", {})
operations = params.get("operations", [])

result = transform(data, operations)
print(json.dumps(result, indent=2, default=str))
''',
        "category": "data_processing",
        "tags": ["json", "transform", "query", "filter"],
        "dependencies": [],
        "examples": [
            "Transform this JSON to only include name and email fields",
            "Filter the JSON array where status equals active",
            "Rename the 'usr' field to 'user' in this data"
        ],
        "parameters_schema": {
            "type": "object",
            "properties": {
                "data": {"type": ["object", "array"]},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "type": {"enum": ["select", "filter", "rename", "flatten"]},
                            "fields": {"type": "array"},
                            "conditions": {"type": "object"},
                            "mapping": {"type": "object"}
                        }
                    }
                }
            }
        }
    },
    {
        "name": "text_statistics",
        "description": "Analyze text and return statistics including word count, sentence count, reading time, and most common words.",
        "code": '''
"""
Text Statistics Skill
Analyzes text and returns useful statistics.

Parameters:
  text: The text to analyze
  top_words: Number of most common words to return (default 10)
"""
import re
from collections import Counter

text = params.get("text", "")
top_words = params.get("top_words", 10)

# Clean and tokenize
words = re.findall(r'\\b[a-zA-Z]+\\b', text.lower())
sentences = re.split(r'[.!?]+', text)
sentences = [s.strip() for s in sentences if s.strip()]
paragraphs = [p.strip() for p in text.split('\\n\\n') if p.strip()]

# Calculate statistics
word_count = len(words)
unique_words = len(set(words))
sentence_count = len(sentences)
paragraph_count = len(paragraphs)
avg_word_length = sum(len(w) for w in words) / word_count if words else 0
avg_sentence_length = word_count / sentence_count if sentences else 0

# Reading time (assuming 200 words per minute)
reading_time_minutes = word_count / 200

# Most common words (excluding common stop words)
stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'is', 'it', 'that', 'this', 'with', 'as', 'be', 'was', 'are', 'been', 'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'can'}
content_words = [w for w in words if w not in stop_words and len(w) > 2]
most_common = Counter(content_words).most_common(top_words)

result = {
    "word_count": word_count,
    "unique_words": unique_words,
    "sentence_count": sentence_count,
    "paragraph_count": paragraph_count,
    "average_word_length": round(avg_word_length, 2),
    "average_sentence_length": round(avg_sentence_length, 2),
    "reading_time_minutes": round(reading_time_minutes, 2),
    "vocabulary_richness": round(unique_words / word_count * 100, 2) if word_count else 0,
    "most_common_words": [{"word": w, "count": c} for w, c in most_common]
}

print(json.dumps(result, indent=2))
''',
        "category": "text_analysis",
        "tags": ["text", "statistics", "word-count", "analysis"],
        "dependencies": [],
        "examples": [
            "Analyze this article and give me statistics",
            "How long would it take to read this text?",
            "What are the most common words in this document?"
        ]
    },
    {
        "name": "date_calculator",
        "description": "Calculate dates: add/subtract days, find differences between dates, get relative dates (next Monday, last Friday), and format dates.",
        "code": '''
"""
Date Calculator Skill
Perform date calculations and formatting.

Parameters:
  operation: "add", "subtract", "diff", "format", "relative"
  date: Base date (ISO format or "today")
  days/weeks/months: Amount to add/subtract
  date2: Second date for diff operation
  format: Output format string
  relative: Relative date description ("next monday", "last friday")
"""
from datetime import datetime, timedelta
import calendar

def parse_date(date_str):
    if date_str == "today" or date_str is None:
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return datetime.fromisoformat(date_str.replace('Z', '+00:00').split('T')[0])

def get_relative_date(relative_desc, base_date=None):
    base = base_date or datetime.now()
    relative_desc = relative_desc.lower()

    # Parse relative descriptions
    day_map = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6
    }

    for day_name, day_num in day_map.items():
        if day_name in relative_desc:
            current_day = base.weekday()
            if 'next' in relative_desc:
                days_ahead = day_num - current_day
                if days_ahead <= 0:
                    days_ahead += 7
                return base + timedelta(days=days_ahead)
            elif 'last' in relative_desc:
                days_behind = current_day - day_num
                if days_behind <= 0:
                    days_behind += 7
                return base - timedelta(days=days_behind)

    return base

operation = params.get("operation", "format")
date_str = params.get("date", "today")
base_date = parse_date(date_str)

result = {}

if operation == "add":
    days = params.get("days", 0)
    weeks = params.get("weeks", 0)
    months = params.get("months", 0)
    new_date = base_date + timedelta(days=days + weeks*7)
    if months:
        new_month = base_date.month + months
        new_year = base_date.year + (new_month - 1) // 12
        new_month = ((new_month - 1) % 12) + 1
        new_date = base_date.replace(year=new_year, month=new_month)
    result = {"original": base_date.isoformat(), "result": new_date.isoformat()}

elif operation == "subtract":
    days = params.get("days", 0)
    weeks = params.get("weeks", 0)
    new_date = base_date - timedelta(days=days + weeks*7)
    result = {"original": base_date.isoformat(), "result": new_date.isoformat()}

elif operation == "diff":
    date2 = parse_date(params.get("date2", "today"))
    diff = abs((date2 - base_date).days)
    result = {
        "date1": base_date.isoformat(),
        "date2": date2.isoformat(),
        "days_difference": diff,
        "weeks_difference": round(diff / 7, 2)
    }

elif operation == "relative":
    relative_desc = params.get("relative", "next monday")
    new_date = get_relative_date(relative_desc, base_date)
    result = {"description": relative_desc, "date": new_date.isoformat()}

elif operation == "format":
    fmt = params.get("format", "%Y-%m-%d")
    result = {
        "iso": base_date.isoformat(),
        "formatted": base_date.strftime(fmt),
        "day_name": base_date.strftime("%A"),
        "month_name": base_date.strftime("%B"),
        "week_number": base_date.isocalendar()[1]
    }

print(json.dumps(result, indent=2))
''',
        "category": "utility",
        "tags": ["date", "time", "calculator", "format"],
        "dependencies": [],
        "examples": [
            "What date is 30 days from today?",
            "How many days until Christmas?",
            "When is next Friday?",
            "What day of the week is January 1st, 2025?"
        ]
    },
    {
        "name": "markdown_table_generator",
        "description": "Generate well-formatted Markdown tables from various data formats (JSON, CSV-like, or dict lists).",
        "code": '''
"""
Markdown Table Generator Skill
Creates formatted Markdown tables from data.

Parameters:
  data: List of dicts, or dict with headers and rows
  headers: Optional list of column headers (auto-detected if not provided)
  alignment: Dict mapping column names to 'left', 'center', or 'right'
  max_width: Maximum column width before truncation
"""

data = params.get("data", [])
headers = params.get("headers", None)
alignment = params.get("alignment", {})
max_width = params.get("max_width", 50)

# Handle different data formats
if isinstance(data, dict) and "headers" in data and "rows" in data:
    headers = data["headers"]
    rows = data["rows"]
elif isinstance(data, list) and len(data) > 0:
    if isinstance(data[0], dict):
        headers = headers or list(data[0].keys())
        rows = [[str(item.get(h, ""))[:max_width] for h in headers] for item in data]
    else:
        rows = [[str(cell)[:max_width] for cell in row] for row in data]
        headers = headers or [f"Col{i+1}" for i in range(len(rows[0]))]
else:
    print("Error: Invalid data format")
    raise SystemExit()

# Calculate column widths
col_widths = [len(h) for h in headers]
for row in rows:
    for i, cell in enumerate(row):
        col_widths[i] = max(col_widths[i], len(str(cell)))

# Generate alignment row
def get_alignment(header):
    align = alignment.get(header, "left")
    width = col_widths[headers.index(header)]
    if align == "center":
        return ":" + "-" * (width - 2) + ":"
    elif align == "right":
        return "-" * (width - 1) + ":"
    else:
        return "-" * width

# Build table
lines = []

# Header row
header_row = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
lines.append(header_row)

# Alignment row
align_row = "| " + " | ".join(get_alignment(h) for h in headers) + " |"
lines.append(align_row)

# Data rows
for row in rows:
    data_row = "| " + " | ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row)) + " |"
    lines.append(data_row)

table = "\\n".join(lines)
print(table)
''',
        "category": "utility",
        "tags": ["markdown", "table", "format", "display"],
        "dependencies": [],
        "examples": [
            "Create a markdown table from this data",
            "Format this list as a table",
            "Convert this JSON to a markdown table"
        ]
    },
    {
        "name": "code_complexity_analyzer",
        "description": "Analyze Python code complexity including cyclomatic complexity, line counts, function counts, and identify complex areas.",
        "code": '''
"""
Code Complexity Analyzer Skill
Analyzes Python code for complexity metrics.

Parameters:
  code: Python code to analyze
  threshold: Complexity threshold for warnings (default 10)
"""
import re
import ast

code = params.get("code", "")
threshold = params.get("threshold", 10)

# Basic metrics
lines = code.split("\\n")
total_lines = len(lines)
blank_lines = sum(1 for l in lines if not l.strip())
comment_lines = sum(1 for l in lines if l.strip().startswith("#"))
code_lines = total_lines - blank_lines - comment_lines

# Parse AST
try:
    tree = ast.parse(code)
except SyntaxError as e:
    print(json.dumps({"error": f"Syntax error: {e}"}))
    raise SystemExit()

# Count structures
functions = []
classes = []
imports = []

class Visitor(ast.NodeVisitor):
    def __init__(self):
        self.current_function = None

    def visit_FunctionDef(self, node):
        complexity = self._calculate_complexity(node)
        functions.append({
            "name": node.name,
            "line": node.lineno,
            "args": len(node.args.args),
            "complexity": complexity,
            "is_complex": complexity > threshold
        })
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        self.visit_FunctionDef(node)

    def visit_ClassDef(self, node):
        methods = [n.name for n in ast.walk(node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        classes.append({
            "name": node.name,
            "line": node.lineno,
            "methods": len(methods)
        })
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            imports.append(alias.name)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            imports.append(f"{module}.{alias.name}")

    def _calculate_complexity(self, node):
        """Calculate cyclomatic complexity for a function."""
        complexity = 1  # Base complexity
        for child in ast.walk(node):
            if isinstance(child, (ast.If, ast.While, ast.For, ast.ExceptHandler)):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += len(child.values) - 1
            elif isinstance(child, ast.comprehension):
                complexity += 1
        return complexity

visitor = Visitor()
visitor.visit(tree)

# Build result
result = {
    "metrics": {
        "total_lines": total_lines,
        "code_lines": code_lines,
        "blank_lines": blank_lines,
        "comment_lines": comment_lines,
        "comment_ratio": round(comment_lines / code_lines * 100, 2) if code_lines else 0
    },
    "structure": {
        "functions": len(functions),
        "classes": len(classes),
        "imports": len(imports)
    },
    "functions": functions,
    "classes": classes,
    "imports": imports,
    "warnings": [f for f in functions if f.get("is_complex")]
}

if result["warnings"]:
    result["summary"] = f"⚠️ Found {len(result['warnings'])} function(s) with complexity > {threshold}"
else:
    result["summary"] = "✅ All functions have acceptable complexity"

print(json.dumps(result, indent=2))
''',
        "category": "text_analysis",
        "tags": ["code", "analysis", "complexity", "python", "metrics"],
        "dependencies": [],
        "examples": [
            "Analyze the complexity of this code",
            "Find complex functions in this Python file",
            "Give me metrics for this code"
        ]
    }
]

# =============================================================================
# Installation Function
# =============================================================================

def install_builtin_skills() -> Dict[str, Any]:
    """Install all built-in skills."""
    import hashlib
    from datetime import datetime, timezone

    installed: List[str] = []
    skipped: List[str] = []

    for skill_def in BUILTIN_SKILLS:
        skill_path = SKILLS_DIR / f"{skill_def['name']}.json"

        if skill_path.exists():
            skipped.append(skill_def['name'])
            continue

        # Generate ID
        timestamp = datetime.now(timezone.utc).isoformat()
        skill_id = hashlib.sha256(f"{skill_def['name']}:{timestamp}".encode()).hexdigest()[:12]

        # Create full skill data
        skill_data = {
            "id": skill_id,
            "name": skill_def["name"],
            "description": skill_def["description"],
            "code": skill_def["code"],
            "category": skill_def.get("category", "utility"),
            "tags": skill_def.get("tags", []),
            "dependencies": skill_def.get("dependencies", []),
            "parameters_schema": skill_def.get("parameters_schema"),
            "examples": skill_def.get("examples", []),
            "created_at": timestamp,
            "updated_at": timestamp,
            "version": 1,
            "invocation_count": 0,
            "builtin": True
        }

        with open(skill_path, 'w') as f:
            json.dump(skill_data, f, indent=2)

        installed.append(skill_def['name'])

    return {
        "installed": installed,
        "skipped": skipped,
        "total": len(BUILTIN_SKILLS)
    }


if __name__ == "__main__":
    result = install_builtin_skills()
    print("Built-in skills installation complete:")
    print(f"  Installed: {len(result['installed'])} skills")
    print(f"  Skipped (already exist): {len(result['skipped'])} skills")
    if result['installed']:
        print(f"  New skills: {', '.join(result['installed'])}")
