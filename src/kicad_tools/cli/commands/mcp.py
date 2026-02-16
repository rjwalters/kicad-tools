"""MCP server command handlers."""

import json
import os
import shutil
import sys
from pathlib import Path

__all__ = ["run_mcp_command"]


def run_mcp_command(args) -> int:
    """Handle mcp command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success)
    """
    mcp_subcommand = getattr(args, "mcp_command", None)

    if mcp_subcommand == "serve":
        return _run_serve(args)
    elif mcp_subcommand == "setup":
        return _run_setup(args)
    else:
        # Default to showing help
        print("Usage: kct mcp <command> [OPTIONS]")
        print()
        print("Commands:")
        print("  serve    Start the MCP server")
        print("  setup    Configure MCP client integration")
        return 0


def _run_serve(args) -> int:
    """Run the MCP server.

    Args:
        args: Parsed command line arguments with transport options

    Returns:
        Exit code (0 for success)
    """
    from kicad_tools.mcp.server import run_server

    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "localhost")
    port = getattr(args, "port", 8080)

    try:
        run_server(transport=transport, host=host, port=port)
        return 0
    except ImportError as e:
        print(f"Error: {e}")
        print()
        print("To use HTTP transport, install the MCP dependencies:")
        print("  pip install 'kicad-tools[mcp]'")
        return 1
    except KeyboardInterrupt:
        print("\nServer stopped.")
        return 0
    except Exception as e:
        print(f"Error starting server: {e}")
        return 1


def _find_kct_command() -> tuple[str, list[str]]:
    """Find the best way to invoke 'kct mcp serve'.

    Priority order:
    1. uv run (if in a uv-managed project) — most portable for dev installs
    2. Global kct binary (if on PATH and not inside a .venv)
    3. python -m fallback

    Returns:
        Tuple of (command, args) for the MCP server config.
    """
    # 1. Check if we're in a uv-managed project (dev install)
    uv_path = shutil.which("uv")
    project_root = _find_project_root()
    if uv_path and project_root:
        return (uv_path, ["run", "--project", str(project_root), "kct", "mcp", "serve"])

    # 2. Check if kct is globally installed (not in a .venv)
    kct_path = shutil.which("kct")
    if kct_path and ".venv" not in kct_path:
        return (kct_path, ["mcp", "serve"])

    # 3. Fall back to python -m
    python_path = sys.executable
    return (python_path, ["-m", "kicad_tools.mcp.server"])


def _find_project_root() -> Path | None:
    """Find the kicad-tools project root by looking for pyproject.toml."""
    # Start from this file's location and walk up
    current = Path(__file__).resolve()
    for parent in current.parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.exists():
            try:
                content = pyproject.read_text()
                if "kicad-tools" in content or "kicad_tools" in content:
                    return parent
            except OSError:
                continue
    return None


def _get_claude_code_config_path() -> Path:
    """Get the Claude Code MCP config file path."""
    return Path.home() / ".claude" / "mcp.json"


def _get_claude_desktop_config_path() -> Path:
    """Get the Claude Desktop MCP config file path."""
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "Claude"
            / "claude_desktop_config.json"
        )
    elif sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    else:
        return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _run_setup(args) -> int:
    """Configure MCP client integration.

    Detects the best way to invoke kct and writes the MCP config
    for the specified client.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success)
    """
    client = getattr(args, "client", "claude-code")
    dry_run = getattr(args, "dry_run", False)

    command, cmd_args = _find_kct_command()

    server_config = {
        "command": command,
        "args": cmd_args,
        "env": {},
    }

    if client == "claude-code":
        config_path = _get_claude_code_config_path()
    else:
        config_path = _get_claude_desktop_config_path()

    # Show what we'll do
    print(f"MCP client: {client}")
    print(f"Config file: {config_path}")
    print(f"Command: {command} {' '.join(cmd_args)}")
    print()

    if dry_run:
        print("Dry run — no changes made.")
        print()
        print("Would write:")
        print(json.dumps({"mcpServers": {"kicad-tools": server_config}}, indent=2))
        return 0

    # Read existing config or create new
    existing: dict = {}
    if config_path.exists():
        try:
            existing = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            existing = {}

    # Merge in the kicad-tools server
    if "mcpServers" not in existing:
        existing["mcpServers"] = {}

    if "kicad-tools" in existing["mcpServers"]:
        old = existing["mcpServers"]["kicad-tools"]
        old_cmd = f"{old.get('command', '')} {' '.join(old.get('args', []))}"
        print("Replacing existing kicad-tools config:")
        print(f"  was: {old_cmd}")
        print(f"  now: {command} {' '.join(cmd_args)}")
    else:
        print("Adding kicad-tools MCP server config.")

    existing["mcpServers"]["kicad-tools"] = server_config

    # Write config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n")

    print()
    print(f"Wrote {config_path}")

    if client == "claude-code":
        print()
        print("Restart Claude Code to pick up the new MCP server.")
    else:
        print()
        print("Restart Claude Desktop to pick up the new MCP server.")

    return 0
