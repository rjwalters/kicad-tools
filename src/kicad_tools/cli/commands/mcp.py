"""MCP server command handlers.

Machine output (``--format json``, issue #4674): ``mcp setup`` prints exactly
one JSON document describing the client, the config file it targets, the
server entry it resolved, and whether it wrote (``written`` / ``replaced``) or
only previewed (``dry_run``).  ``mcp serve`` is exempt -- it is a long-running
server whose machine contract *is* the MCP protocol.  See
``docs/reference/machine-output.md``.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from ..format_options import FORMAT_JSON, emit_json
from .mcp_clients import (
    SERVER_NAME,
    STATUS_REPLACED,
    STATUS_UNCHANGED,
    ClientConfigError,
    parse_opencode_version,
    render_codex_config,
    render_opencode_config,
)

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
    transport = getattr(args, "transport", "stdio")
    host = getattr(args, "host", "localhost")
    port = getattr(args, "port", 8080)

    try:
        from kicad_tools.mcp.server import run_server

        run_server(transport=transport, host=host, port=port)
        return 0
    except ImportError as e:
        print(f"Error: {e}")
        print()
        print("The MCP server requires the 'mcp' extra:")
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
    as_json = getattr(args, "format", "text") == FORMAT_JSON

    if client in ("codex", "opencode"):
        return _run_setup_harness(args, client, dry_run, as_json)
    if getattr(args, "project", None) is not None:
        print("Error: --project only applies to --client opencode", file=sys.stderr)
        return 2

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
    if not as_json:
        print(f"MCP client: {client}")
        print(f"Config file: {config_path}")
        print(f"Command: {command} {' '.join(cmd_args)}")
        print()

    if dry_run:
        if as_json:
            emit_json(
                {
                    "command": "setup",
                    "client": client,
                    "config_path": str(config_path),
                    "dry_run": True,
                    "written": False,
                    "replaced": False,
                    "server": server_config,
                    "success": True,
                }
            )
            return 0
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

    replaced = "kicad-tools" in existing["mcpServers"]
    if replaced and not as_json:
        old = existing["mcpServers"]["kicad-tools"]
        old_cmd = f"{old.get('command', '')} {' '.join(old.get('args', []))}"
        print("Replacing existing kicad-tools config:")
        print(f"  was: {old_cmd}")
        print(f"  now: {command} {' '.join(cmd_args)}")
    elif not replaced and not as_json:
        print("Adding kicad-tools MCP server config.")

    existing["mcpServers"]["kicad-tools"] = server_config

    # Write config
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n")

    if as_json:
        emit_json(
            {
                "command": "setup",
                "client": client,
                "config_path": str(config_path),
                "dry_run": False,
                "written": True,
                "replaced": replaced,
                "server": server_config,
                "success": True,
            }
        )
        return 0

    print()
    print(f"Wrote {config_path}")

    if client == "claude-code":
        print()
        print("Restart Claude Code to pick up the new MCP server.")
    else:
        print()
        print("Restart Claude Desktop to pick up the new MCP server.")

    return 0


# ---------------------------------------------------------------------------
# Codex CLI and opencode (issue #5953)
# ---------------------------------------------------------------------------


def _get_codex_config_path() -> Path:
    """``$CODEX_HOME/config.toml``, defaulting to ``~/.codex/config.toml``."""
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "config.toml"


def _pick_opencode_file(directory: Path) -> Path:
    """``opencode.json`` in ``directory``, or an existing ``opencode.jsonc``."""
    json_path = directory / "opencode.json"
    jsonc_path = directory / "opencode.jsonc"
    if not json_path.exists() and jsonc_path.exists():
        return jsonc_path
    return json_path


def _get_opencode_config_path(project: str | None = None) -> Path:
    """User-level ``$XDG_CONFIG_HOME/opencode/opencode.json`` or a project file.

    opencode resolves its global config through XDG on every platform
    (``~/.config/opencode`` when ``XDG_CONFIG_HOME`` is unset, macOS included).
    """
    if project is not None:
        return _pick_opencode_file(Path(project).expanduser().resolve())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return _pick_opencode_file(base / "opencode")


def _detect_opencode_schema(config_text: str | None) -> tuple[str, str]:
    """Pick the opencode config schema; returns ``(schema, reason)``.

    The installed ``opencode --version`` wins (major 1 -> ``v1``).  Without an
    ``opencode`` binary, an existing config that only holds legacy top-level
    ``mcp.<name>`` entries keeps ``v1``; everything else gets ``v2``, the
    layout current opencode (and ``opencode mcp add``) writes.
    """
    exe = shutil.which("opencode")
    if exe:
        try:
            proc = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=15, check=False
            )
            major = parse_opencode_version(proc.stdout + proc.stderr)
        except (OSError, subprocess.SubprocessError):
            major = None
        if major is not None:
            return ("v1" if major < 2 else "v2"), f"opencode {major}.x on PATH"
    if config_text:
        try:
            mcp = json.loads(config_text).get("mcp")
        except (json.JSONDecodeError, AttributeError):
            mcp = None
        if isinstance(mcp, dict) and mcp and "servers" not in mcp:
            return "v1", "existing config uses legacy mcp.<name> entries"
    return "v2", "default"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _run_setup_harness(args, client: str, dry_run: bool, as_json: bool) -> int:
    """Register ``kct mcp serve`` with Codex CLI or opencode."""
    project = getattr(args, "project", None)
    schema_opt = getattr(args, "opencode_schema", None) or "auto"

    def fail(message: str, config_path: Path | None) -> int:
        if as_json:
            emit_json(
                {
                    "command": "setup",
                    "client": client,
                    "config_path": str(config_path) if config_path else None,
                    "dry_run": dry_run,
                    "written": False,
                    "replaced": False,
                    "success": False,
                    "error": message,
                }
            )
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    if client == "codex" and project is not None:
        return fail("--project only applies to --client opencode", None)

    command, cmd_args = _find_kct_command()
    command = os.path.abspath(command)

    config_path = (
        _get_codex_config_path() if client == "codex" else _get_opencode_config_path(project)
    )
    try:
        existing = config_path.read_text() if config_path.exists() else None
    except OSError as exc:
        return fail(f"cannot read {config_path}: {exc}", config_path)

    schema = None
    schema_reason = None
    try:
        if client == "codex":
            new_text, status, entry = render_codex_config(existing, command, cmd_args)
        else:
            if schema_opt == "auto":
                schema, schema_reason = _detect_opencode_schema(existing)
            else:
                schema, schema_reason = schema_opt, "--opencode-schema"
            new_text, status, entry = render_opencode_config(existing, command, cmd_args, schema)
    except ClientConfigError as exc:
        return fail(f"{config_path}: {exc}", config_path)

    unchanged = status == STATUS_UNCHANGED
    replaced = status == STATUS_REPLACED
    written = not dry_run and not unchanged

    if written:
        try:
            _write_atomic(config_path, new_text)
        except OSError as exc:
            return fail(f"cannot write {config_path}: {exc}", config_path)

    if as_json:
        payload = {
            "command": "setup",
            "client": client,
            "config_path": str(config_path),
            "dry_run": dry_run,
            "written": written,
            "replaced": replaced,
            "unchanged": unchanged,
            "server_name": SERVER_NAME,
            "server": entry,
            "success": True,
        }
        if schema is not None:
            payload["opencode_schema"] = schema
        emit_json(payload)
        return 0

    print(f"MCP client: {client}")
    print(f"Config file: {config_path}")
    if schema is not None:
        print(f"opencode schema: {schema} ({schema_reason})")
    print(f"Command: {command} {' '.join(cmd_args)}")
    print()

    if client == "codex":
        snippet = f"[mcp_servers.{SERVER_NAME}]\n" + "".join(
            f"{k} = {json.dumps(v)}\n" for k, v in entry.items()
        )
    else:
        key = ("mcp", "servers", SERVER_NAME) if schema == "v2" else ("mcp", SERVER_NAME)
        nested: dict = entry
        for part in reversed(key):
            nested = {part: nested}
        snippet = json.dumps(nested, indent=2) + "\n"

    if unchanged:
        print(f"Already configured: the {SERVER_NAME!r} server entry is up to date.")
        return 0
    action = "replace the existing" if replaced else "add the"
    if dry_run:
        print(f"Dry run - no changes made. Would {action} {SERVER_NAME!r} server entry:")
        print()
        print(snippet, end="")
        return 0

    verb = "Replaced the existing" if replaced else "Added the"
    print(f"{verb} {SERVER_NAME!r} server entry:")
    print()
    print(snippet, end="")
    print()
    print(f"Wrote {config_path}")
    harness = "Codex CLI" if client == "codex" else "opencode"
    print(f"Restart {harness} to pick up the new MCP server.")
    return 0
