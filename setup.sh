#!/bin/bash
# Skill Orchestrator MCP - Quick Setup Script
# Run this to install and configure the MCP server

set -e

echo "🚀 Setting up Skill Orchestrator MCP..."

# Check if uv is available
if command -v uv &> /dev/null; then
    echo "✅ Using uv for installation"
    uv pip install -e .
else
    echo "⚠️ uv not found, using pip"
    pip install -e .
fi

# Install built-in skills
echo "📦 Installing built-in skills..."
python -c "from skill_orchestrator_mcp.builtin_skills import install_builtin_skills; install_builtin_skills()"

# Show configuration instructions
echo ""
echo "✅ Installation complete!"
echo ""
echo "📝 To configure Claude Desktop, add this to your config:"
echo ""
cat << 'EOF'
{
  "mcpServers": {
    "skill-orchestrator": {
      "command": "skill-orchestrator-mcp"
    }
  }
}
EOF
echo ""
echo "Or for development with uvx:"
echo ""
cat << 'EOF'
{
  "mcpServers": {
    "skill-orchestrator": {
      "command": "uvx",
      "args": ["--from", "FULL_PATH_TO_THIS_DIR", "skill-orchestrator-mcp"]
    }
  }
}
EOF
echo ""
echo "📁 Skills stored at: ~/.skill_orchestrator/skills"
echo "📁 State stored at: ~/.skill_orchestrator/state"
echo ""
echo "🎉 Ready to go! Restart Claude Desktop to load the new server."
