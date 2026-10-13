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
import shlex
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
    # Diagnostics go to stderr: in stdio mode stdout is the JSON-RPC
    # transport, and a client would parse these lines as frames (#5961).
    except ImportError as e:
        print(f"Error: {e}", file=sys.stderr)
        print(file=sys.stderr)
        print("The MCP server requires the 'mcp' extra:", file=sys.stderr)
        print("  pip install 'kicad-tools[mcp]'", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nServer stopped.", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"Error starting server: {e}", file=sys.stderr)
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


#: Name the Claude clients register the server under.  Codex and opencode use
#: ``SERVER_NAME`` ("kct"); Claude keeps "kicad-tools" so existing configs and
#: docs stay valid.
CLAUDE_SERVER_NAME = "kicad-tools"

#: Upper bound on one ``claude mcp ...`` call.
CLAUDE_CLI_TIMEOUT_S = 30

METHOD_CLAUDE_CLI = "claude-cli"
METHOD_JSON_MERGE = "json-merge"


def _get_claude_code_user_config_path() -> Path:
    """Claude Code's user-scope config: ``$CLAUDE_CONFIG_DIR/.claude.json``.

    Defaults to ``~/.claude.json``.  Claude Code reads user-scope MCP servers
    from the top-level ``mcpServers`` of this file (issue #6418).
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(config_dir).expanduser() if config_dir else Path.home()
    return base / ".claude.json"


def _get_claude_code_project_config_path() -> Path:
    """Claude Code's project-scope config: ``./.mcp.json``."""
    return Path.cwd() / ".mcp.json"


def _get_legacy_claude_code_config_path() -> Path:
    """``~/.claude/mcp.json``: where kct wrote before #6418.  Claude Code ignores it."""
    return Path.home() / ".claude" / "mcp.json"


def _find_claude_cli() -> str | None:
    """The ``claude`` executable on PATH, if any."""
    return shutil.which("claude")


def _read_json_object(path: Path) -> dict | None:
    """Parse ``path`` as a JSON object; ``None`` when the file does not exist.

    Raises :class:`ClientConfigError` when the file exists but cannot be read,
    is not valid JSON, is not an object, or has a non-object ``mcpServers``.
    Callers must then refuse to write: ``~/.claude.json`` holds all of Claude
    Code's state, so replacing an unparseable file with ``{}`` would erase it.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError as exc:
        raise ClientConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientConfigError(
            f"{path} is not valid JSON ({exc}); refusing to overwrite it"
        ) from exc
    if not isinstance(data, dict):
        raise ClientConfigError(f"{path} does not hold a JSON object; refusing to overwrite it")
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        raise ClientConfigError(f"{path}: 'mcpServers' is not an object; refusing to overwrite it")
    return data


def _server_entry(data: dict | None, name: str):
    """``data["mcpServers"][name]``, or ``None``."""
    servers = (data or {}).get("mcpServers")
    return servers.get(name) if isinstance(servers, dict) else None


def _merge_mcp_server(path: Path, name: str, entry: dict) -> None:
    """Set ``mcpServers[name]`` in ``path``, keeping every other key verbatim.

    Re-reads the file (it may have changed since the caller looked) and
    swaps the new content in with a single ``os.replace``, so a reader never
    sees a half-written file, and preserves the file mode.

    Atomic is not race-free: this is an unlocked read-modify-write.  If
    another process -- typically a running Claude Code, which rewrites
    ``~/.claude.json`` itself -- writes the file between the re-read and the
    ``os.replace``, its update is lost.  The window is milliseconds and only
    exists on the fallback route (no ``claude`` CLI, or the CLI failed);
    ``docs/mcp/setup.md`` suggests quitting Claude Code first in that case.
    """
    data = _read_json_object(path) or {}
    data.setdefault("mcpServers", {})[name] = entry
    _write_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _describe_entry(entry) -> str:
    """``command args...`` of a server entry, for the "was:" line."""
    if not isinstance(entry, dict):
        return json.dumps(entry)
    args = entry.get("args")
    args = args if isinstance(args, list) else []
    return f"{entry.get('command', '')} {' '.join(map(str, args))}"


def _legacy_claude_code_config() -> Path | None:
    """The stale ``~/.claude/mcp.json`` if it still has a kicad-tools entry."""
    path = _get_legacy_claude_code_config_path()
    try:
        data = _read_json_object(path)
    except ClientConfigError:
        return None
    return path if _server_entry(data, CLAUDE_SERVER_NAME) is not None else None


def _register_via_claude_cli(exe: str, entry: dict, replace: bool) -> tuple[str | None, bool]:
    """Register the user-scope server with ``claude mcp add-json``.

    ``claude mcp add`` fails on an existing name, so a differing entry is
    removed first.  Returns ``(error, removed)``: ``error`` is ``None`` on
    success, otherwise a message (the caller then falls back to the JSON
    merge, which also restores an entry the ``remove`` already dropped);
    ``removed`` says whether ``claude mcp remove`` succeeded, so the caller
    can tell the user the old entry is gone if the fallback fails too.
    """

    def run(argv: list[str]) -> str | None:
        label = "claude " + " ".join(argv[:2])
        try:
            proc = subprocess.run(
                [exe, *argv],
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT_S,
                env=dict(os.environ),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"`{label}` failed: {exc}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            return f"`{label}` exited {proc.returncode}" + (f": {detail}" if detail else "")
        return None

    removed = False
    if replace:
        error = run(["mcp", "remove", "--scope", "user", CLAUDE_SERVER_NAME])
        if error:
            return error, removed
        removed = True
    error = run(["mcp", "add-json", "--scope", "user", CLAUDE_SERVER_NAME, json.dumps(entry)])
    return error, removed


def _run_setup_claude_code(args, dry_run: bool, as_json: bool) -> int:
    """Register ``kct mcp serve`` with Claude Code (issue #6418).

    User scope (default) targets ``~/.claude.json`` (``$CLAUDE_CONFIG_DIR``
    honoured): through ``claude mcp add-json --scope user`` when ``claude`` is
    on PATH, otherwise -- or when the CLI fails -- by merging the top-level
    ``mcpServers`` entry in place.  Project scope merges ``./.mcp.json``.
    """
    scope = getattr(args, "scope", None) or "user"
    command, cmd_args = _find_kct_command()
    entry = {"type": "stdio", "command": command, "args": cmd_args, "env": {}}
    config_path = (
        _get_claude_code_project_config_path()
        if scope == "project"
        else _get_claude_code_user_config_path()
    )
    legacy = _legacy_claude_code_config()
    exe = _find_claude_cli() if scope == "user" else None
    method = METHOD_CLAUDE_CLI if exe else METHOD_JSON_MERGE

    def payload(**fields) -> dict:
        doc = {
            "command": "setup",
            "client": "claude-code",
            "scope": scope,
            "config_path": str(config_path),
            "method": method,
            "dry_run": dry_run,
            "server_name": CLAUDE_SERVER_NAME,
            "legacy_config": str(legacy) if legacy else None,
        }
        doc.update(fields)
        return doc

    def fail(message: str) -> int:
        if as_json:
            emit_json(
                payload(
                    written=False, replaced=False, unchanged=False, success=False, error=message
                )
            )
        else:
            print(f"Error: {message}", file=sys.stderr)
        return 1

    try:
        current = _server_entry(_read_json_object(config_path), CLAUDE_SERVER_NAME)
    except ClientConfigError as exc:
        return fail(str(exc))

    unchanged = current == entry
    replaced = current is not None and not unchanged
    written = False
    cli_error = None
    removed = False

    if not dry_run and not unchanged:
        if exe:
            cli_error, removed = _register_via_claude_cli(exe, entry, replace=current is not None)
            if cli_error is None:
                try:
                    after = _server_entry(_read_json_object(config_path), CLAUDE_SERVER_NAME)
                except ClientConfigError:
                    after = None
                if after != entry:
                    cli_error = f"`claude mcp add-json` exited 0 but {config_path} lacks the entry"
            if cli_error is not None:
                method = METHOD_JSON_MERGE
        if method == METHOD_JSON_MERGE:
            # If ``claude mcp remove`` already dropped the old entry, a failed
            # merge leaves the user with none -- say so, and how to restore it.
            lost = (
                f" Note: the previous {CLAUDE_SERVER_NAME!r} entry was already removed by"
                f" `claude mcp remove`; restore it with: claude mcp add-json --scope user"
                f" {CLAUDE_SERVER_NAME} {shlex.quote(json.dumps(current))}"
                if removed
                else ""
            )
            try:
                _merge_mcp_server(config_path, CLAUDE_SERVER_NAME, entry)
            except ClientConfigError as exc:
                return fail(f"{exc}{lost}")
            except OSError as exc:
                return fail(f"cannot write {config_path}: {exc}{lost}")
        written = True

    if as_json:
        emit_json(
            payload(
                written=written,
                replaced=replaced,
                unchanged=unchanged,
                server=entry,
                cli_error=cli_error,
                success=True,
            )
        )
        return 0

    print("MCP client: claude-code")
    print(f"Scope: {scope}")
    print(f"Config file: {config_path}")
    how = "claude mcp add-json" if method == METHOD_CLAUDE_CLI else "direct JSON merge"
    print(f"Method: {method} ({how})")
    print(f"Command: {command} {' '.join(cmd_args)}")
    print()
    snippet = json.dumps({"mcpServers": {CLAUDE_SERVER_NAME: entry}}, indent=2)

    if unchanged:
        print(f"Already configured: the {CLAUDE_SERVER_NAME!r} server entry is up to date.")
    elif dry_run:
        action = "replace the existing" if replaced else "add the"
        print(f"Dry run - no changes made. Would {action} {CLAUDE_SERVER_NAME!r} server entry:")
        print()
        print(snippet)
    else:
        if cli_error:
            print(f"Warning: {cli_error}")
            print(f"Fell back to editing {config_path} directly.")
            print()
        if replaced:
            old_cmd = _describe_entry(current)
            print(f"Replaced the existing {CLAUDE_SERVER_NAME!r} server entry:")
            print(f"  was: {old_cmd}")
            print(f"  now: {command} {' '.join(cmd_args)}")
        else:
            print(f"Added the {CLAUDE_SERVER_NAME!r} server entry:")
        print()
        print(snippet)
        print()
        print(f"Wrote {config_path}")
        print("Restart Claude Code to pick up the new MCP server.")

    if legacy:
        print()
        print(
            f"Note: {legacy} also has a {CLAUDE_SERVER_NAME!r} entry, but Claude Code "
            "never reads that file (older kct versions wrote it). It was left "
            "unchanged; you can delete it."
        )
    return 0


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

    if getattr(args, "scope", None) is not None and client != "claude-code":
        print("Error: --scope only applies to --client claude-code", file=sys.stderr)
        return 2
    if client in ("codex", "opencode"):
        return _run_setup_harness(args, client, dry_run, as_json)
    if getattr(args, "project", None) is not None:
        print("Error: --project only applies to --client opencode", file=sys.stderr)
        return 2
    if client == "claude-code":
        return _run_setup_claude_code(args, dry_run, as_json)

    command, cmd_args = _find_kct_command()

    server_config = {
        "command": command,
        "args": cmd_args,
        "env": {},
    }

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

    # An unreadable config is reported, never replaced with {} (#6418).
    try:
        existing = _read_json_object(config_path)
    except ClientConfigError as exc:
        if as_json:
            emit_json(
                {
                    "command": "setup",
                    "client": client,
                    "config_path": str(config_path),
                    "dry_run": False,
                    "written": False,
                    "replaced": False,
                    "success": False,
                    "error": str(exc),
                }
            )
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    old = _server_entry(existing, "kicad-tools")
    replaced = old is not None
    if replaced and not as_json:
        old_cmd = _describe_entry(old)
        print("Replacing existing kicad-tools config:")
        print(f"  was: {old_cmd}")
        print(f"  now: {command} {' '.join(cmd_args)}")
    elif not replaced and not as_json:
        print("Adding kicad-tools MCP server config.")

    _merge_mcp_server(config_path, "kicad-tools", server_config)

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
    """Write ``text`` to ``path`` via a temp file and ``os.replace``.

    A symlinked config (e.g. ``~/.claude.json`` managed from a dotfiles repo)
    is resolved first, so the temp file lands next to the real target and the
    replace updates the target -- the link itself survives.
    """
    path = Path(os.path.realpath(path))
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
