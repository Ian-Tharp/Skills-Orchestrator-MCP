"""
Skill Orchestrator MCP Server

A self-evolving MCP server that enables Claude to:
- Create and manage dynamic skills (reusable code patterns)
- Execute Python code in sandboxed environments
- Persist state and context across conversations
- Progressively discover and compose capabilities

Inspired by:
- Anthropic's "Code Execution with MCP" pattern
- Docker MCP Gateway orchestration
- Cloudflare's "Code Mode"
"""

__version__ = "0.1.0"
__author__ = "Ian & Claude"

from .server import mcp, run_server

__all__ = ["mcp", "run_server", "__version__"]
