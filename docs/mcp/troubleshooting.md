# Troubleshooting Guide

Solutions for common issues with the kicad-tools MCP server.

## Installation Issues

### "No module named 'kicad_tools'"

**Cause:** kicad-tools is not installed or not in the Python path.

**Solutions:**

1. Install kicad-tools:
   ```bash
   pip install "kicad-tools[mcp]"
   ```

2. Verify installation:
   ```bash
   python -c "import kicad_tools; print(kicad_tools.__file__)"
   ```

3. If using a virtual environment, ensure it's activated:
   ```bash
   source ~/venv/bin/activate
   pip install "kicad-tools[mcp]"
   ```

### "No module named 'fastmcp'"

**Cause:** MCP dependencies not installed.

**Solution:** Install with MCP extras:
```bash
pip install "kicad-tools[mcp]"
```

### "Permission denied" when installing

**Cause:** Insufficient permissions for system Python.

**Solutions:**

1. Use `--user` flag:
   ```bash
   pip install --user "kicad-tools[mcp]"
   ```

2. Use a virtual environment (recommended):
   ```bash
   python -m venv ~/kicad-tools-env
   source ~/kicad-tools-env/bin/activate
   pip install "kicad-tools[mcp]"
   ```

---

## Claude Desktop Integration Issues

### Server Not Starting

**Symptoms:** Claude doesn't recognize kicad-tools commands.

**Diagnosis:**

1. Test server manually:
   ```bash
   python -m kicad_tools.mcp.server
   ```
   If this fails, fix the installation first.

2. Check Claude Desktop logs:
   - macOS: `~/Library/Logs/Claude/`
   - Look for errors related to MCP servers

**Solutions:**

1. Use absolute path to Python in config:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "/usr/local/bin/python3",
         "args": ["-m", "kicad_tools.mcp.server"]
       }
     }
   }
   ```

2. Find your Python path:
   ```bash
   which python3
   # Use this path in the config
   ```

### "Server disconnected" Error

**Cause:** Server crashed or couldn't parse request.

**Solutions:**

1. Enable debug logging:
   ```json
   {
     "mcpServers": {
       "kicad-tools": {
         "command": "python",
         "args": ["-m", "kicad_tools.mcp.server"],
         "env": {
           "KCT_LOG_LEVEL": "DEBUG"
         }
       }
     }
   }
   ```

2. Check server output:
   ```bash
   # Run manually and watch for errors
   python -m kicad_tools.mcp.server 2>&1 | tee /tmp/mcp-debug.log
   ```

### Config File Not Found

**Locations by platform:**

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux:** `~/.config/Claude/claude_desktop_config.json`

Create the file if it doesn't exist:
```bash
# macOS
mkdir -p ~/Library/Application\ Support/Claude
touch ~/Library/Application\ Support/Claude/claude_desktop_config.json
```

---

## Runtime Errors

### "PCB file not found"

**Causes:**
- Invalid file path
- File doesn't exist
- Permission issues

**Solutions:**

1. Use absolute paths:
   ```
   /Users/name/projects/board.kicad_pcb
   ```

2. Verify file exists:
   ```bash
   ls -la /path/to/your/board.kicad_pcb
   ```

3. Check permissions:
   ```bash
   # Ensure read access
   chmod +r /path/to/your/board.kicad_pcb
   ```

### "Session not found"

**Causes:**
- Session ID is invalid
- Session expired (default timeout: 30 minutes)
- Server was restarted

**Solutions:**

1. Start a new session:
   ```
   "Start a new placement session for my board"
   ```

2. Extend session timeout (see Configuration):
   ```bash
   export KCT_SESSION_TIMEOUT=3600
   ```

### "Invalid file extension"

**Cause:** Wrong file type for the operation.

**Solution:** Use correct file types:
- PCB tools: `.kicad_pcb` files
- BOM export: `.kicad_sch` files

### "Component not found"

**Causes:**
- Typo in component reference
- Component doesn't exist
- Reference designator is case-sensitive

**Solutions:**

1. Verify exact reference designator:
   - Check your schematic/PCB in KiCad
   - References are usually uppercase (e.g., "C1" not "c1")

2. Use placement_analyze to list components:
   ```
   "Analyze the placement of my board and show me all component references"
   ```

### "Unknown manufacturer"

**Cause:** Unsupported manufacturer preset.

**Solution:** Use one of the supported values:
- `generic`
- `jlcpcb`
- `pcbway`
- `oshpark`
- `seeed`

---

## Performance Issues

### Slow Analysis

**Causes:**
- Large PCB file
- All analysis checks enabled
- Slow disk access

**Solutions:**

1. Disable unnecessary checks:
   ```
   "Analyze placement but skip thermal and signal integrity checks"
   ```

2. Ensure file is on local disk (not network drive)

### High Memory Usage

**For boards with >500 components:**

1. Close other applications
2. Ensure adequate RAM (2GB+ recommended)
3. Analyze in smaller batches if possible

### Server Hanging

**Causes:**
- Large file being processed
- Infinite loop (rare)

**Solutions:**

1. Wait longer for large files (up to 30 seconds)

2. If truly stuck, restart Claude Desktop

3. Check for very complex boards:
   - >1000 components
   - >1000 nets
   - Very dense routing

---

## File Handling Issues

### Changes Not Saved

**Causes:**
- Session not committed
- Write permission denied
- Wrong output path

**Solutions:**

1. Always commit sessions:
   ```
   "Commit the placement changes"
   ```

2. Check write permissions:
   ```bash
   touch /path/to/board.kicad_pcb
   # If this fails, fix permissions
   ```

3. Save to different location:
   ```
   "Commit changes to ~/Desktop/board_modified.kicad_pcb"
   ```

### Corrupted PCB File

**Prevention:**
- Always backup before modifications
- Use `query_move` before `apply_move`
- Use `rollback_session` if unsure

**Recovery:**
- KiCad creates autosave files
- Check for `.kicad_pcb-bak` files
- Use version control (git)

### Missing BOM Data

**Causes:**
- Components don't have all fields populated
- Hierarchical schematic not fully loaded

**Solutions:**

1. Ensure schematic has all sheets:
   - BOM export automatically handles hierarchical schematics

2. Check component fields in KiCad:
   - Value
   - Footprint
   - LCSC (for assembly)

---

## Getting Help

### Debug Information to Collect

When reporting issues, include:

1. **kicad-tools version:**
   ```bash
   pip show kicad-tools
   ```

2. **Python version:**
   ```bash
   python --version
   ```

3. **Error message:** Full error text from Claude or terminal

4. **Debug log:**
   ```bash
   KCT_LOG_LEVEL=DEBUG python -m kicad_tools.mcp.server 2> debug.log
   ```

5. **Reproduction steps:** What you asked Claude to do

### Reporting Issues

File issues at: https://github.com/rjwalters/kicad-tools/issues

Include:
- Operating system
- Python version
- kicad-tools version
- Debug information above
- Minimal reproduction case (if possible)

---

## Quick Reference: Common Errors

| Error | Likely Cause | Quick Fix |
|-------|--------------|-----------|
| "No module named" | Not installed | `pip install "kicad-tools[mcp]"` |
| "PCB file not found" | Wrong path | Use absolute path |
| "Session not found" | Expired session | Start new session |
| "Component not found" | Wrong reference | Check exact name in KiCad |
| "Unknown manufacturer" | Invalid preset | Use: jlcpcb, pcbway, oshpark, seeed |
| Server disconnected | Crash | Check logs, restart |
| Slow response | Large file | Wait, or disable some checks |
