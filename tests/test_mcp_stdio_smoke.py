"""End-to-end MCP stdio smoke test for every ``kct mcp setup`` client (issue #5961).

``tests/test_mcp_setup_harness.py`` (#5953) pins the config files that
``kct mcp setup`` *writes*.  This module checks that the server those configs
*launch* actually speaks MCP over stdio the way Claude Code, Claude Desktop,
Codex CLI and opencode expect:

1. For each client, take the launch command that
   ``kct mcp setup --client X --dry-run --format json`` reports -- not a
   hand-written one -- and also check the config file a real (sandboxed)
   setup writes parses in that client's format and names the same command.
2. Spawn that command and drive it as a client would: ``initialize``,
   ``notifications/initialized``, ``tools/list`` (must match
   ``TOOL_REGISTRY``), then one cheap ``tools/call`` (``board_summary`` on a
   small fixture board) to exercise the dispatch path, not just discovery.
3. Every byte the server writes to stdout must be a JSON-RPC 2.0 frame.  A
   stray ``print`` corrupts the stdio transport (the #5938 failure class).
4. Close stdin and require a clean, prompt exit.

Why a hand-rolled client instead of ``mcp.client.stdio``: the stdout-purity
assertion needs the raw byte stream, which the SDK client hides (it drops or
logs non-JSON lines), and owning the subprocess lets us put the server in its
own process group with a hard timeout -- a hung grandchild (``uv`` ->
``kct``) must never block the suite (#5877).  The protocol here is just
newline-delimited JSON, so the client is a few dozen lines.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

# The tools/call path validates arguments with jsonschema (the `mcp` extra,
# issue #5804); without it the server can start but not dispatch.  CI's Test
# job installs `--extra dev`, which pulls both in, so this never skips there.
pytest.importorskip("jsonschema", reason="MCP stdio smoke test needs the 'mcp' extra")

from kicad_tools.cli.commands import mcp as mcp_cmd  # noqa: E402
from kicad_tools.cli.parser import create_parser  # noqa: E402
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY  # noqa: E402

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process-group kill uses POSIX killpg"
)

CLIENTS = ["claude-code", "claude-desktop", "codex", "opencode"]
FIXTURE_PCB = Path(__file__).parent / "fixtures" / "stale_nets.kicad_pcb"

# Hard ceiling on any single wait for the server.  Normal frames arrive in
# well under a second; this only bounds the failure case.
READ_TIMEOUT_S = 30.0
SHUTDOWN_TIMEOUT_S = 10.0


# ---------------------------------------------------------------------------
# Launch command, as reported by `kct mcp setup`
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox_home(tmp_path, monkeypatch):
    """Point every config location ``kct mcp setup`` touches at tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _setup(client: str, capsys, *extra: str) -> dict[str, Any]:
    argv = ["mcp", "setup", "--client", client, "--format", "json", *extra]
    if client == "opencode":
        # Pin the schema so the test never shells out to a real `opencode`
        # binary for version detection; the launch command is schema-independent.
        argv += ["--opencode-schema", "v2"]
    rc = mcp_cmd.run_mcp_command(create_parser().parse_args(argv))
    out = capsys.readouterr().out
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["success"] is True
    return payload


def _argv_from_entry(client: str, entry: dict[str, Any]) -> list[str]:
    """The process argv a client builds from its server entry."""
    if client == "opencode":
        assert entry["type"] == "local"
        command = entry["command"]
        assert isinstance(command, list) and all(isinstance(c, str) for c in command)
        return list(command)
    assert isinstance(entry["command"], str)
    assert isinstance(entry["args"], list)
    return [entry["command"], *entry["args"]]


def _launch_argv(client: str, capsys) -> list[str]:
    payload = _setup(client, capsys, "--dry-run")
    assert payload["dry_run"] is True
    assert payload["written"] is False
    return _argv_from_entry(client, payload["server"])


def _read_written_entry(client: str, config_path: Path) -> dict[str, Any]:
    """Parse the written config with the client's own format, return our entry."""
    text = config_path.read_text()
    if client == "codex":
        return tomllib.loads(text)["mcp_servers"]["kct"]
    data = json.loads(text)
    if client == "opencode":
        return data["mcp"]["servers"]["kct"]
    return data["mcpServers"]["kicad-tools"]


def _assert_launches_mcp_serve(argv: list[str]) -> None:
    """The command must exist, be executable, and run ``kct mcp serve``.

    ``_find_kct_command`` has three shapes: ``uv run --project ROOT kct mcp
    serve`` (dev checkout), ``kct mcp serve`` (global install), and the
    ``python -m kicad_tools.mcp.server`` fallback.
    """
    command = Path(argv[0])
    assert command.is_absolute(), argv
    assert command.is_file() and os.access(command, os.X_OK), f"not executable: {command}"
    args = argv[1:]
    if args[-2:] == ["mcp", "serve"]:
        if command.name.startswith("uv"):
            assert args[:2] == ["run", "--project"] and args[3:] == ["kct", "mcp", "serve"]
        else:
            assert args == ["mcp", "serve"]
    else:
        assert args == ["-m", "kicad_tools.mcp.server"], argv


# ---------------------------------------------------------------------------
# Minimal newline-delimited JSON-RPC client with a process-group watchdog
# ---------------------------------------------------------------------------


class StdioSession:
    """Drive one server process; record every stdout line for the purity check."""

    def __init__(self, argv: list[str], stderr_path: Path, *, unbuffered: bool = True) -> None:
        env = dict(os.environ)
        # `uv run` would otherwise re-sync the project environment on every
        # launch: slow, network-dependent, and racy under xdist.  The test's
        # own interpreter is already that environment, so skipping the sync
        # changes nothing about the server being exercised.
        env["UV_NO_SYNC"] = "1"
        if unbuffered:
            env["PYTHONUNBUFFERED"] = "1"
        else:
            # PYTHONUNBUFFERED also makes CPython setvbuf() libc's stdout to
            # unbuffered, which would hide native-buffering bugs that a real
            # `kct mcp serve` (no -u) has (#5982).
            env.pop("PYTHONUNBUFFERED", None)
        self._stderr = stderr_path.open("wb")
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            env=env,
            start_new_session=True,  # own process group -> killpg reaps uv's child too
        )
        self.raw_lines: list[bytes] = []
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        self._next_id = 0

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.raw_lines.append(line)
            self._lines.put(line)
        self._lines.put(None)  # EOF

    def kill_group(self) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.kill_group()
        self._reader.join(timeout=5)
        self._stderr.close()

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + READ_TIMEOUT_S
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                self.kill_group()
                pytest.fail(f"no response to {method!r} within {READ_TIMEOUT_S}s")
            if line is None:
                pytest.fail(f"server closed stdout before answering {method!r}")
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                self.kill_group()
                pytest.fail(
                    f"non-JSON-RPC output on stdout while awaiting {method!r}: {line!r} "
                    "(stray stdout writes corrupt the stdio transport)"
                )
            if isinstance(frame, dict) and frame.get("id") == request_id:
                return frame

    def shutdown(self) -> int:
        """Close stdin -- the stdio transport's shutdown signal -- and wait."""
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self.kill_group()
            pytest.fail(f"server did not exit within {SHUTDOWN_TIMEOUT_S}s of stdin EOF")


def _assert_only_jsonrpc_frames(raw_lines: list[bytes], stderr_path: Path) -> None:
    for raw in raw_lines:
        text = raw.decode("utf-8", errors="replace")
        try:
            frame = json.loads(text)
        except json.JSONDecodeError:
            pytest.fail(
                f"non-JSON-RPC output on stdout: {text!r}\n"
                f"stderr:\n{stderr_path.read_text(errors='replace')}"
            )
        assert isinstance(frame, dict) and frame.get("jsonrpc") == "2.0", text
        assert ("result" in frame) != ("error" in frame) or "method" in frame, text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client", CLIENTS)
def test_written_config_parses_and_names_launch_command(client, sandbox_home, capsys):
    """The config file each client reads parses in its own format and names a real server."""
    launch = _launch_argv(client, capsys)
    _assert_launches_mcp_serve(launch)

    payload = _setup(client, capsys)
    config_path = Path(payload["config_path"])
    assert config_path.is_relative_to(sandbox_home)
    entry = _read_written_entry(client, config_path)
    assert _argv_from_entry(client, entry) == launch


@pytest.mark.parametrize("client", CLIENTS)
def test_stdio_handshake_tools_list_and_call(client, sandbox_home, tmp_path, capsys):
    argv = _launch_argv(client, capsys)
    if argv[0] != sys.executable and shutil.which(Path(argv[0]).name) is None:
        # The setup command found this tool on PATH a moment ago; this only
        # guards against a PATH that changed underneath us.
        pytest.skip(f"launch command {argv[0]} not on PATH")

    stderr_path = tmp_path / f"{client}.stderr"
    session = StdioSession(argv, stderr_path)
    try:
        # 1. initialize
        init = session.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": f"kct-smoke-{client}", "version": "0"},
            },
        )
        assert "error" not in init, init
        result = init["result"]
        assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]
        assert "tools" in result["capabilities"]
        assert result["serverInfo"]["name"]

        # 2. initialized notification (no response expected)
        session.notify("notifications/initialized")

        # 3. tools/list == TOOL_REGISTRY
        listed = session.request("tools/list")
        assert "error" not in listed, listed
        tools = listed["result"]["tools"]
        assert {t["name"] for t in tools} == set(TOOL_REGISTRY)
        assert len(tools) == len(TOOL_REGISTRY)
        for tool in tools:
            assert isinstance(tool["description"], str) and tool["description"], tool["name"]
            schema = tool["inputSchema"]
            assert isinstance(schema, dict) and schema.get("type") == "object", tool["name"]

        # 4. one cheap tool call on a small fixture board
        called = session.request(
            "tools/call",
            {"name": "board_summary", "arguments": {"pcb_path": str(FIXTURE_PCB)}},
        )
        assert "error" not in called, called
        content = called["result"]["content"]
        assert content and content[0]["type"] == "text"
        summary = json.loads(content[0]["text"])
        assert not called["result"].get("isError"), summary
        assert "error" not in summary, summary

        # 5. clean shutdown on stdin EOF
        returncode = session.shutdown()
    finally:
        session.close()

    assert returncode == 0, stderr_path.read_text(errors="replace")
    _assert_only_jsonrpc_frames(session.raw_lines, stderr_path)
    # initialize, tools/list, tools/call -- and nothing for the notification.
    assert len(session.raw_lines) == 3


def test_handler_stdout_writes_do_not_corrupt_transport(monkeypatch):
    """A tool that print()s (the router, the placement optimizer do) must not
    leak onto the stdio transport; its output belongs on stderr (#5961)."""
    import io

    from kicad_tools.mcp.server import create_server

    server = create_server()
    tool = server.tools["board_summary"]

    def noisy_handler(params: dict[str, Any]) -> dict[str, Any]:
        print("progress: routing net 1/3")
        return {"ok": True}

    monkeypatch.setattr(tool, "handler", noisy_handler)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "board_summary", "arguments": {"pcb_path": "x"}},
        },
    ]
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    server.run()

    lines = stdout.getvalue().splitlines()
    assert len(lines) == 1, lines
    frame = json.loads(lines[0])
    assert frame["id"] == 1 and json.loads(frame["result"]["content"][0]["text"]) == {"ok": True}
    assert "progress: routing net 1/3" in stderr.getvalue()
    assert sys.stdout is stdout  # redirect is scoped to the serve loop


# ---------------------------------------------------------------------------
# fd-level stdout isolation (#5965)
# ---------------------------------------------------------------------------

# A real `kct mcp serve`-style server process whose `board_summary` handler is
# swapped for one that writes to stdout *below* sys.stdout, keyed on the
# `pcb_path` argument (the tool's schema requires that string).  Run as a
# separate interpreter so fd 1 really is the pipe the client reads.
_FD_NOISE_SERVER = r"""
import ctypes
import os
import subprocess

from kicad_tools.mcp.server import create_server

def noisy(params):
    mode = params["pcb_path"]
    if mode == "fd1":
        os.write(1, b"junk-from-fd1\n")
    elif mode == "child":
        # The child inherits the server's fd 1, exactly like a tool shelling
        # out to kicad-cli without capturing its output.
        subprocess.run(["echo", "junk-from-child"], check=True)
    elif mode == "native":
        libc = ctypes.CDLL(None)
        libc.printf(b"junk-from-native\n")
        libc.fflush(None)
    elif mode == "native-unflushed":
        # No newline, no fflush: the bytes sit in libc's stdout buffer until
        # something flushes it -- which must happen before fd 1 is restored,
        # or they trail onto the JSON-RPC stream at exit (#5982).
        libc = ctypes.CDLL(None)
        libc.printf(b"junk-from-native-unflushed")
    return {"mode": mode}

server = create_server()
server.tools["board_summary"].handler = noisy
server.run()
"""

FD_NOISE = {
    "fd1": "junk-from-fd1",
    "child": "junk-from-child",
    "native": "junk-from-native",
    "native-unflushed": "junk-from-native-unflushed",
}


def test_fd_level_stdout_writes_do_not_corrupt_transport(tmp_path):
    """Writes to fd 1 that bypass sys.stdout -- os.write, an inheriting child
    process, C-level printf -- must land on stderr, never on the transport."""
    stderr_path = tmp_path / "fd-noise.stderr"
    session = StdioSession([sys.executable, "-c", _FD_NOISE_SERVER], stderr_path, unbuffered=False)
    try:
        init = session.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "kct-fd-noise", "version": "0"},
            },
        )
        assert "error" not in init, init
        for mode in FD_NOISE:
            called = session.request(
                "tools/call", {"name": "board_summary", "arguments": {"pcb_path": mode}}
            )
            assert "error" not in called, called
            assert json.loads(called["result"]["content"][0]["text"]) == {"mode": mode}
        returncode = session.shutdown()
    finally:
        session.close()

    stderr = stderr_path.read_text(errors="replace")
    assert returncode == 0, stderr
    _assert_only_jsonrpc_frames(session.raw_lines, stderr_path)
    assert len(session.raw_lines) == 1 + len(FD_NOISE)
    for junk in FD_NOISE.values():
        assert junk in stderr, f"{junk!r} missing from stderr:\n{stderr}"


def _fd_identity(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return st.st_dev, st.st_ino


def _run_with_real_fd1(monkeypatch, handler) -> tuple[Any, Any]:
    """Run the stdio loop with sys.stdout bound to the real fd 1."""
    import io

    from kicad_tools.mcp.server import create_server

    server = create_server()
    monkeypatch.setattr(server.tools["board_summary"], "handler", handler)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "board_summary", "arguments": {"pcb_path": "x"}},
    }
    real_stdout = open(1, "w", closefd=False)  # noqa: SIM115
    real_stderr = open(2, "w", closefd=False)  # noqa: SIM115
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(sys, "stdout", real_stdout)
    monkeypatch.setattr(sys, "stderr", real_stderr)
    return server, real_stdout


def test_fd1_restored_after_run_returns(monkeypatch, capfd):
    def handler(params: dict[str, Any]) -> dict[str, Any]:
        os.write(1, b"junk-from-fd1\n")
        return {"ok": True}

    before = _fd_identity(1)
    server, real_stdout = _run_with_real_fd1(monkeypatch, handler)
    server.run()

    assert _fd_identity(1) == before
    assert sys.stdout is real_stdout
    os.write(1, b"after-run\n")  # fd 1 is usable and back on the original target
    out, err = capfd.readouterr()
    lines = out.splitlines()
    assert len(lines) == 2, lines
    frame = json.loads(lines[0])
    assert frame["id"] == 1 and json.loads(frame["result"]["content"][0]["text"]) == {"ok": True}
    assert lines[1] == "after-run"
    assert "junk-from-fd1" in err


def test_fd1_restored_after_run_raises(monkeypatch, capfd):
    class Stop(BaseException):
        """Escapes handle_request's ``except Exception`` like KeyboardInterrupt."""

    def handler(params: dict[str, Any]) -> dict[str, Any]:
        os.write(1, b"junk-before-raise\n")
        raise Stop

    before = _fd_identity(1)
    server, real_stdout = _run_with_real_fd1(monkeypatch, handler)
    with pytest.raises(Stop):
        server.run()

    assert _fd_identity(1) == before
    assert sys.stdout is real_stdout
    os.write(1, b"after-raise\n")
    out, err = capfd.readouterr()
    assert out == "after-raise\n"
    assert "junk-before-raise" in err
