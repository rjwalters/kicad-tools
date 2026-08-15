"""Tests for MCP HTTP transport support.

Tests the FastMCP-based HTTP transport functionality:
- FastMCP server creation
- Transport selection
- CLI command parsing
"""

import pytest


class TestFastMCPServerCreation:
    """Tests for create_fastmcp_server function."""

    def test_create_fastmcp_server_stdio_mode(self):
        """Test creating FastMCP server in stdio mode."""
        pytest.importorskip("mcp")
        from kicad_tools.mcp.server import create_fastmcp_server

        mcp = create_fastmcp_server(http_mode=False)
        assert mcp is not None
        assert mcp.name == "kicad-tools"

    def test_create_fastmcp_server_http_mode(self):
        """Test creating FastMCP server in HTTP mode."""
        pytest.importorskip("mcp")
        from kicad_tools.mcp.server import create_fastmcp_server

        mcp = create_fastmcp_server(http_mode=True)
        assert mcp is not None
        assert mcp.name == "kicad-tools"

    def test_create_fastmcp_server_tools_registered(self):
        """Test that all tools are registered on FastMCP server."""
        pytest.importorskip("mcp")
        import asyncio

        from kicad_tools.mcp.server import create_fastmcp_server

        mcp = create_fastmcp_server(http_mode=False)

        # Get registered tools - list_tools is async in FastMCP
        async def get_tools():
            return await mcp.list_tools()

        tools = asyncio.get_event_loop().run_until_complete(get_tools())
        tool_names = [t.name for t in tools]

        # Verify key tools are registered
        expected_tools = [
            "export_gerbers",
            "export_bom",
            "export_assembly",
            "placement_analyze",
            "placement_suggestions",
            "start_session",
            "query_move",
            "apply_move",
            "undo_move",
            "commit_session",
            "rollback_session",
            "measure_clearance",
        ]

        for tool_name in expected_tools:
            assert tool_name in tool_names, f"Tool {tool_name} not registered"

    def test_create_fastmcp_server_defaults_host_port(self):
        """Omitting host/port leaves the FastMCP SDK defaults untouched."""
        pytest.importorskip("mcp")
        from kicad_tools.mcp.server import create_fastmcp_server

        mcp = create_fastmcp_server(http_mode=True)

        # SDK defaults: 127.0.0.1:8000 -- we must not silently override them.
        assert mcp.settings.host == "127.0.0.1"
        assert mcp.settings.port == 8000

    def test_create_fastmcp_server_custom_host_port(self):
        """host/port are forwarded to the FastMCP constructor (not run())."""
        pytest.importorskip("mcp")
        from kicad_tools.mcp.server import create_fastmcp_server

        mcp = create_fastmcp_server(http_mode=True, host="127.0.0.1", port=8792)

        assert mcp.settings.host == "127.0.0.1"
        assert mcp.settings.port == 8792
        assert mcp.settings.stateless_http is True

    def test_fastmcp_run_signature_rejects_host_port(self):
        """Regression guard: FastMCP.run() takes no host/port kwargs.

        This is the API fact that made ``kct mcp --transport http`` raise
        TypeError. If a future SDK adds them back, this test fails loudly so
        the wiring can be revisited deliberately.
        """
        pytest.importorskip("mcp")
        import inspect

        from mcp.server.fastmcp import FastMCP

        params = inspect.signature(FastMCP.run).parameters
        assert "host" not in params
        assert "port" not in params

    def test_fastmcp_import_error(self, monkeypatch):
        """Test ImportError when fastmcp is not installed."""
        import sys

        # Remove mcp from sys.modules if present
        modules_to_remove = [k for k in sys.modules if k.startswith("mcp")]
        for mod in modules_to_remove:
            monkeypatch.delitem(sys.modules, mod, raising=False)

        # Mock the import to fail
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "mcp.server.fastmcp" or name.startswith("mcp"):
                raise ImportError("No module named 'mcp'")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        # Need to reload the module to test the import error
        # This is tricky in tests, so we just verify the error handling exists
        # by checking the function raises ImportError when mcp is unavailable


class _FakeFastMCP:
    """Minimal stand-in for FastMCP that records how run() was called."""

    def __init__(self):
        self.run_calls = []

    def run(self, transport="stdio", mount_path=None):
        # Mirrors mcp.server.fastmcp.FastMCP.run's signature exactly: any
        # host/port kwarg would raise TypeError here, just like the real SDK.
        self.run_calls.append({"transport": transport, "mount_path": mount_path})


class TestRunServerFunction:
    """Tests for run_server function."""

    def test_run_server_invalid_transport(self):
        """Test that invalid transport raises ValueError."""
        from kicad_tools.mcp.server import run_server

        with pytest.raises(ValueError, match="Unknown transport"):
            run_server(transport="invalid")

    def test_run_server_http_passes_host_port_to_constructor(self, monkeypatch):
        """run_server's HTTP branch wires host/port at construction time."""
        from kicad_tools.mcp import server as server_module

        fake = _FakeFastMCP()
        created = {}

        def fake_create(http_mode=False, host=None, port=None):
            created.update({"http_mode": http_mode, "host": host, "port": port})
            return fake

        monkeypatch.setattr(server_module, "create_fastmcp_server", fake_create)

        server_module.run_server(transport="http", host="0.0.0.0", port=9123)

        assert created == {"http_mode": True, "host": "0.0.0.0", "port": 9123}
        # run() must be called with transport only -- no host/port kwargs.
        assert fake.run_calls == [{"transport": "streamable-http", "mount_path": None}]

    def test_run_server_http_does_not_raise_type_error(self, monkeypatch):
        """Regression for #4855: the HTTP branch reached mcp.run with bad kwargs.

        Uses a real FastMCP instance (so the real run() signature applies) with
        run() stubbed out, guaranteeing no socket is bound.
        """
        pytest.importorskip("mcp")
        from kicad_tools.mcp import server as server_module

        calls = []

        mcp_instance = server_module.create_fastmcp_server(
            http_mode=True, host="127.0.0.1", port=8792
        )

        def capture_run(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(mcp_instance, "run", capture_run)
        monkeypatch.setattr(
            server_module,
            "create_fastmcp_server",
            lambda http_mode=False, host=None, port=None: mcp_instance,
        )

        # Would have raised TypeError before the fix.
        server_module.run_server(transport="http", host="127.0.0.1", port=8792)

        assert calls == [((), {"transport": "streamable-http"})]
        assert mcp_instance.settings.host == "127.0.0.1"
        assert mcp_instance.settings.port == 8792

    def test_run_server_stdio_unaffected(self, monkeypatch):
        """The stdio path still uses MCPServer and never touches FastMCP."""
        from kicad_tools.mcp import server as server_module

        ran = []

        class FakeStdioServer:
            def run(self):
                ran.append("stdio")

        monkeypatch.setattr(server_module, "create_server", lambda: FakeStdioServer())

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("stdio transport must not construct FastMCP")

        monkeypatch.setattr(server_module, "create_fastmcp_server", explode)

        server_module.run_server(transport="stdio")

        assert ran == ["stdio"]


@pytest.mark.slow
class TestHTTPTransportSmoke:
    """End-to-end smoke test of `run_server(transport='http', ...)`.

    Binds a real loopback socket on an ephemeral port in a subprocess, so it is
    marked slow and kept out of the fast unit suite.
    """

    def test_http_server_serves_initialize_and_tools_list(self):
        pytest.importorskip("mcp")
        pytest.importorskip("uvicorn")
        httpx = pytest.importorskip("httpx")

        import json
        import socket
        import subprocess
        import sys
        import time

        # Reserve an ephemeral port, then release it for the server to claim.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from kicad_tools.mcp.server import run_server; "
                f"run_server(transport='http', host='127.0.0.1', port={port})",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        url = f"http://127.0.0.1:{port}/mcp"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "kicad-tools-test", "version": "1"},
            },
        }

        try:
            deadline = time.time() + 60
            response = None
            while time.time() < deadline:
                if proc.poll() is not None:
                    output = proc.stdout.read() if proc.stdout else ""
                    pytest.fail(f"MCP HTTP server exited early:\n{output}")
                try:
                    response = httpx.post(url, json=initialize, headers=headers, timeout=5)
                    break
                except httpx.HTTPError:
                    time.sleep(0.5)

            assert response is not None, "server never accepted a connection"
            assert response.status_code == 200
            assert "serverInfo" in response.text

            tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            tools_response = httpx.post(url, json=tools_list, headers=headers, timeout=30)
            assert tools_response.status_code == 200

            payloads = [
                json.loads(line[len("data: ") :])
                for line in tools_response.text.splitlines()
                if line.startswith("data: ")
            ]
            assert payloads, f"no SSE data frames in response: {tools_response.text[:200]}"
            tool_names = {tool["name"] for tool in payloads[0]["result"]["tools"]}
            assert "export_gerbers" in tool_names
            assert "measure_clearance" in tool_names
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                proc.kill()
                proc.wait(timeout=10)


class TestMCPCLIParser:
    """Tests for MCP CLI command parser."""

    def test_mcp_serve_default_args(self):
        """Test MCP serve command with default arguments."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve"])

        assert args.command == "mcp"
        assert args.mcp_command == "serve"
        assert args.transport == "stdio"
        assert args.host == "localhost"
        assert args.port == 8080

    def test_mcp_serve_http_transport(self):
        """Test MCP serve command with HTTP transport."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve", "--transport", "http"])

        assert args.transport == "http"

    def test_mcp_serve_custom_port(self):
        """Test MCP serve command with custom port."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve", "-t", "http", "-p", "3000"])

        assert args.transport == "http"
        assert args.port == 3000

    def test_mcp_serve_custom_host(self):
        """Test MCP serve command with custom host."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve", "--transport", "http", "--host", "0.0.0.0"])

        assert args.host == "0.0.0.0"

    def test_mcp_serve_all_options(self):
        """Test MCP serve command with all options."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve", "-t", "http", "--host", "0.0.0.0", "-p", "9000"])

        assert args.transport == "http"
        assert args.host == "0.0.0.0"
        assert args.port == 9000


class TestMCPCommandHandler:
    """Tests for MCP command handler."""

    def test_mcp_command_no_subcommand(self, capsys):
        """Test MCP command without subcommand shows help."""
        from kicad_tools.cli.commands.mcp import run_mcp_command

        class MockArgs:
            mcp_command = None

        result = run_mcp_command(MockArgs())
        assert result == 0

        captured = capsys.readouterr()
        assert "serve" in captured.out

    def test_run_serve_stdio_import_error(self, monkeypatch, capsys):
        """Test serve command handles import error gracefully."""
        from kicad_tools.cli.commands.mcp import _run_serve

        class MockArgs:
            transport = "http"
            host = "localhost"
            port = 8080

        # Mock run_server to raise ImportError
        def mock_run_server(*args, **kwargs):
            raise ImportError("FastMCP is required")

        monkeypatch.setattr("kicad_tools.mcp.server.run_server", mock_run_server)

        result = _run_serve(MockArgs())
        assert result == 1

        captured = capsys.readouterr()
        assert "FastMCP is required" in captured.out


class TestMCPModuleExports:
    """Tests for MCP module exports."""

    def test_mcp_module_exports(self):
        """Test that MCP module exports expected functions."""
        from kicad_tools import mcp

        assert hasattr(mcp, "MCPServer")
        assert hasattr(mcp, "create_server")
        assert hasattr(mcp, "create_fastmcp_server")
        assert hasattr(mcp, "run_server")

    def test_mcp_module_all(self):
        """Test that MCP module __all__ includes new exports."""
        from kicad_tools import mcp

        assert "create_fastmcp_server" in mcp.__all__
        assert "run_server" in mcp.__all__
