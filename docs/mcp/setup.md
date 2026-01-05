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

## Client Configuration

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
