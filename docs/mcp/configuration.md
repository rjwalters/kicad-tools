# MCP Server Configuration

Configuration options and environment settings for the kicad-tools MCP server.

## Server Startup

### Basic Usage

```bash
# Standard startup (stdio transport)
python -m kicad_tools.mcp.server
```

### Logging Configuration

The server logs to stderr to avoid interfering with the JSON-RPC protocol on stdout.

```bash
# Enable debug logging
KCT_LOG_LEVEL=DEBUG python -m kicad_tools.mcp.server

# Log to file
python -m kicad_tools.mcp.server 2> /tmp/kicad-tools-mcp.log
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `KCT_LOG_LEVEL` | `INFO` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `KCT_SESSION_TIMEOUT` | `1800` | Session timeout in seconds (30 minutes) |

### Example Configuration

```bash
# In your shell profile (~/.zshrc, ~/.bashrc)
export KCT_LOG_LEVEL=INFO
export KCT_SESSION_TIMEOUT=3600  # 1 hour sessions
```

## Claude Desktop Configuration

### Full Configuration Example

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "python",
      "args": ["-m", "kicad_tools.mcp.server"],
      "env": {
        "KCT_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### With Virtual Environment

```json
{
  "mcpServers": {
    "kicad-tools": {
      "command": "/Users/YOUR_USERNAME/venvs/kicad-tools/bin/python",
      "args": ["-m", "kicad_tools.mcp.server"],
      "env": {
        "KCT_LOG_LEVEL": "DEBUG",
        "PYTHONPATH": "/Users/YOUR_USERNAME/projects/kicad-tools/src"
      }
    }
  }
}
```

### Development Mode

For development, you can point to a local checkout:

```json
{
  "mcpServers": {
    "kicad-tools-dev": {
      "command": "/Users/YOUR_USERNAME/venvs/kicad-tools/bin/python",
      "args": ["-m", "kicad_tools.mcp.server"],
      "env": {
        "KCT_LOG_LEVEL": "DEBUG",
        "PYTHONPATH": "/Users/YOUR_USERNAME/projects/kicad-tools/src"
      }
    }
  }
}
```

## Session Management

### Session Lifecycle

Sessions are created when you call `start_session` and are automatically cleaned up when:

1. `commit_session` is called (saves changes)
2. `rollback_session` is called (discards changes)
3. Session timeout expires (default: 30 minutes)

### Session Timeout

Sessions expire after a period of inactivity to prevent resource leaks:

```bash
# Extend session timeout to 1 hour
export KCT_SESSION_TIMEOUT=3600
```

### Multiple Sessions

The server supports multiple concurrent sessions. Each session is identified by a unique session ID returned from `start_session`.

```
Session 1: /path/to/board1.kicad_pcb
Session 2: /path/to/board2.kicad_pcb
```

## Manufacturer Presets

### Supported Manufacturers

| Preset | DRC Rules | BOM Format | PnP Format |
|--------|-----------|------------|------------|
| `generic` | Standard | CSV | CSV |
| `jlcpcb` | JLCPCB rules | JLCPCB CSV | JLCPCB CPL |
| `pcbway` | PCBWay rules | PCBWay CSV | PCBWay CSV |
| `oshpark` | OSHPark rules | CSV | - |
| `seeed` | Seeed Fusion rules | Seeed CSV | Seeed CSV |

### DRC Rule Details

#### JLCPCB (4-layer example)
- Minimum trace width: 0.09mm
- Minimum clearance: 0.09mm
- Minimum via drill: 0.2mm
- Minimum via diameter: 0.45mm
- Edge clearance: 0.3mm

#### OSHPark
- Minimum trace width: 0.15mm (6 mil)
- Minimum clearance: 0.15mm (6 mil)
- Minimum via drill: 0.25mm (10 mil)

## File Handling

### Supported File Types

| Extension | Description | Tools |
|-----------|-------------|-------|
| `.kicad_pcb` | KiCad PCB file | All PCB tools |
| `.kicad_sch` | KiCad schematic | BOM export |

### Path Resolution

The server accepts:
- Absolute paths: `/Users/name/projects/board.kicad_pcb`
- Home directory paths: `~/projects/board.kicad_pcb`

**Note:** Relative paths may not work correctly depending on the MCP client's working directory.

### File Permissions

The server needs:
- **Read** access to PCB and schematic files
- **Write** access to output directories
- **Write** access to PCB files when committing placement changes

## Performance Considerations

### Large PCB Files

For PCB files with >500 components:

1. **Initial load time** may be 2-5 seconds
2. **Placement analysis** may take 5-10 seconds
3. Consider using specific analysis flags to reduce computation:
   ```
   placement_analyze with check_thermal=false, check_signal_integrity=false
   ```

### Memory Usage

Typical memory usage:
- Small boards (<100 components): ~50MB
- Medium boards (100-500 components): ~100-200MB
- Large boards (>500 components): ~300-500MB

## Protocol Details

### Transport

The server uses stdio transport:
- **Input**: JSON-RPC 2.0 requests on stdin
- **Output**: JSON-RPC 2.0 responses on stdout
- **Logs**: Informational messages on stderr

### Protocol Version

The server implements MCP protocol version `2024-11-05`.

### Message Format

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "placement_analyze",
    "arguments": {
      "pcb_path": "/path/to/board.kicad_pcb"
    }
  }
}
```

### Response Format

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"overall_score\": 78.5, ...}"
      }
    ]
  }
}
```
