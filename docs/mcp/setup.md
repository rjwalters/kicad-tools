# MCP Server Setup Guide

This guide covers detailed setup instructions for the kicad-tools MCP server with various MCP clients.

## Prerequisites

### System Requirements

- **Python**: 3.10 or higher
- **Operating System**: macOS, Linux, or Windows
- **Memory**: 512MB minimum (for large PCB files, 2GB+ recommended)

### Python Installation

Verify your Python version:

```bash
python --version
# Python 3.10.0 or higher required
```

## Installation

### Option 1: pip (Recommended)

```bash
# Install with MCP dependencies
pip install "kicad-tools[mcp]"

# Verify installation
python -c "from kicad_tools.mcp import create_server; print('MCP server available')"
```

### Option 2: From Source

```bash
# Clone repository
git clone https://github.com/rjwalters/kicad-tools.git
cd kicad-tools

# Install with MCP dependencies
pip install -e ".[mcp]"
```

### Option 3: pipx (Isolated Environment)

```bash
# Install pipx if not available
pip install pipx
pipx ensurepath

# Install kicad-tools with MCP
pipx install "kicad-tools[mcp]"
```

## Quick Setup (Recommended)

The easiest way to configure MCP is with the built-in setup command:

```bash
# For Claude Code (default)
kct mcp setup

# For Claude Desktop
kct mcp setup --client claude-desktop

# For Codex CLI ([mcp_servers.kct] in $CODEX_HOME/config.toml)
kct mcp setup --client codex

# For opencode (user-level ~/.config/opencode/opencode.json)
kct mcp setup --client opencode
# ...or the project-level opencode.json in the current directory
kct mcp setup --client opencode --project

# Preview without making changes
kct mcp setup --client codex --dry-run
```

If you're developing from source with `uv`:

```bash
uv run kct mcp setup
```

This auto-detects the correct `kct` binary path and writes the
appropriate MCP config file. It handles development installs (uv),
global installs (pip/pipx), and virtual environments.

Every client is merged, never overwritten: other MCP servers and settings in
the file are kept, and re-running when the entry is already current is a
no-op. An existing file that cannot be parsed is left alone and reported as
an error (exit 1). Add `--format json` for a machine-readable result.

## Manual Client Configuration

### Claude Code

1. **Config file**: `~/.claude/mcp.json`

2. **For development installs** (using uv from the repo):
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "uv",
         "args": ["run", "--project", "/path/to/kicad-tools", "kct", "mcp", "serve"],
         "env": {}
       }
     }
   }
   ```

3. **For global installs** (pip/pipx):
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "kct",
         "args": ["mcp", "serve"],
         "env": {}
       }
     }
   }
   ```

4. **Restart Claude Code** to pick up the new MCP server.

### Claude Desktop (macOS)

1. **Locate the config file**:
   ```bash
   # Default location
   ~/Library/Application Support/Claude/claude_desktop_config.json
   ```

2. **Create or edit the configuration**:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "python",
         "args": ["-m", "kicad_tools.mcp.server"]
       }
     }
   }
   ```

3. **For pipx installations**, use the full path:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "/Users/YOUR_USERNAME/.local/bin/python",
         "args": ["-m", "kicad_tools.mcp.server"]
       }
     }
   }
   ```

4. **Restart Claude Desktop** completely (quit and reopen)

5. **Verify** by asking Claude: "What PCB tools do you have available?"

### Claude Desktop (Windows)

1. **Locate the config file**:
   ```
   %APPDATA%\Claude\claude_desktop_config.json
   ```

2. **Create or edit the configuration**:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "python",
         "args": ["-m", "kicad_tools.mcp.server"]
       }
     }
   }
   ```

3. **Restart Claude Desktop**

### Claude Desktop (Linux)

1. **Locate the config file**:
   ```bash
   ~/.config/Claude/claude_desktop_config.json
   ```

2. **Create or edit the configuration**:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "python3",
         "args": ["-m", "kicad_tools.mcp.server"]
       }
     }
   }
   ```

3. **Restart Claude Desktop**

### Codex CLI

`kct mcp setup --client codex` writes the same table as
`codex mcp add kct -- <kct> mcp serve`, into `$CODEX_HOME/config.toml`
(default `~/.codex/config.toml`). Pointing `CODEX_HOME` at a per-run directory
(as benchmark harnesses do) registers the server for that run only.

```toml
[mcp_servers.kct]
command = "/absolute/path/to/kct"
args = ["mcp", "serve"]
```

The file is edited in place: comments, other tables and any extra keys you put
on `[mcp_servers.kct]` (`startup_timeout_sec`, an `[mcp_servers.kct.env]`
table) are kept; only `command` and `args` are rewritten. Check with
`codex mcp list`.

### opencode

`kct mcp setup --client opencode` adds a `local` server to
`$XDG_CONFIG_HOME/opencode/opencode.json` (default
`~/.config/opencode/opencode.json`, on macOS too), or with `--project [DIR]`
to `DIR/opencode.json` (default: the current directory). If only an
`opencode.jsonc` exists it is targeted instead, but files with comments are
refused rather than rewritten.

opencode v2 nests servers under `mcp.servers`; this is what `opencode mcp add`
writes (verified against opencode 2.0.22):

```json
{
  "mcp": {
    "servers": {
      "kct": {"type": "local", "command": ["/absolute/path/to/kct", "mcp", "serve"]}
    }
  }
}
```

opencode v1 keys servers directly under `mcp` and needs `"enabled": true`:

```json
{"mcp": {"kct": {"type": "local", "command": ["/absolute/path/to/kct", "mcp", "serve"], "enabled": true}}}
```

`--opencode-schema auto` (the default) picks the layout from
`opencode --version`; without an `opencode` binary it keeps a file that already
uses the v1 layout on v1, and otherwise writes v2. Force it with
`--opencode-schema v1` or `v2`. Writing v2 over a legacy `mcp.kct` entry
moves it to `mcp.servers.kct` (keeping `environment` and other keys) so the
two never coexist.

### Other MCP Clients

The kicad-tools MCP server uses stdio transport and follows the MCP specification. Any MCP-compatible client can connect using:

```bash
python -m kicad_tools.mcp.server
```

The server reads JSON-RPC requests from stdin and writes responses to stdout.

## Verification

### Check Server Startup

Test that the server starts correctly:

```bash
# Start server manually (will wait for input)
python -m kicad_tools.mcp.server

# In another terminal, test with a simple request
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | python -m kicad_tools.mcp.server
```

Expected response:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": {"tools": {"listChanged": false}},
    "serverInfo": {"name": "kicad-tools", "version": "0.1.0"}
  }
}
```

### List Available Tools

```bash
echo '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' | python -m kicad_tools.mcp.server
```

### Test with a PCB File

```bash
# Create a test request file
cat > /tmp/test_request.json << 'EOF'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"placement_analyze","arguments":{"pcb_path":"/path/to/your/board.kicad_pcb"}}}
EOF

# Run the test
cat /tmp/test_request.json | python -m kicad_tools.mcp.server
```

## Environment Setup

### Virtual Environment (Recommended)

```bash
# Create virtual environment
python -m venv ~/kicad-tools-venv

# Activate
source ~/kicad-tools-venv/bin/activate  # macOS/Linux
# or
~/kicad-tools-venv\Scripts\activate  # Windows

# Install
pip install "kicad-tools[mcp]"
```

Update Claude Desktop config to use the virtual environment:

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "/Users/YOUR_USERNAME/kicad-tools-venv/bin/python",
      "args": ["-m", "kicad_tools.mcp.server"]
    }
  }
}
```

### Conda Environment

```bash
# Create environment
conda create -n kicad-tools python=3.11
conda activate kicad-tools

# Install
pip install "kicad-tools[mcp]"
```

Update config:

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "/path/to/conda/envs/kicad-tools/bin/python",
      "args": ["-m", "kicad_tools.mcp.server"]
    }
  }
}
```

## Next Steps

- [Tool Reference](tools.md) - Learn about available tools
- [Example Workflows](workflows.md) - See common usage patterns
- [Troubleshooting](troubleshooting.md) - Solutions for common issues
