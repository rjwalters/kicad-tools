"""Tests for MCP (Model Context Protocol) server infrastructure."""

from __future__ import annotations

import pytest

# Check if MCP dependencies are available
try:
    import pydantic  # noqa: F401

    HAS_MCP_DEPS = True
except ImportError:
    HAS_MCP_DEPS = False

mcp_deps_required = pytest.mark.skipif(
    not HAS_MCP_DEPS,
    reason="MCP dependencies not installed (install with: pip install kicad-tools[mcp])",
)


@mcp_deps_required
class TestMCPTools:
    """Test MCPTools registration system."""

    def test_register_tool_basic(self):
        """Test basic tool registration."""
        from kicad_tools.mcp.tools import MCPTools

        tools = MCPTools()

        @tools.register()
        def my_tool(arg: str) -> dict:
            """A test tool."""
            return {"result": arg}

        assert tools.tool_count == 1
        assert "my_tool" in tools.tool_names

    def test_register_multiple_tools(self):
        """Test registering multiple tools."""
        from kicad_tools.mcp.tools import MCPTools

        tools = MCPTools()

        @tools.register()
        def tool_one(x: int) -> int:
            return x * 2

        @tools.register()
        def tool_two(s: str) -> str:
            return s.upper()

        assert tools.tool_count == 2
        assert "tool_one" in tools.tool_names
        assert "tool_two" in tools.tool_names

    def test_register_with_custom_decorator(self):
        """Test registration with custom decorator factory."""
        from kicad_tools.mcp.tools import MCPTools

        tools = MCPTools()

        custom_decorator_called = False

        def custom_decorator(mcp):
            nonlocal custom_decorator_called
            custom_decorator_called = True
            return mcp.tool()

        @tools.register(decorator=custom_decorator)
        def custom_tool(x: int) -> int:
            return x

        assert tools.tool_count == 1
        assert "custom_tool" in tools.tool_names

    def test_tool_names_property(self):
        """Test tool_names property returns correct list."""
        from kicad_tools.mcp.tools import MCPTools

        tools = MCPTools()

        @tools.register()
        def alpha_tool():
            pass

        @tools.register()
        def beta_tool():
            pass

        names = tools.tool_names
        assert len(names) == 2
        assert "alpha_tool" in names
        assert "beta_tool" in names


@mcp_deps_required
class TestMCPError:
    """Test MCPError structured error responses."""

    def test_create_basic_error(self):
        """Test creating a basic MCPError."""
        from kicad_tools.mcp.errors import MCPError

        error = MCPError(
            error_type="TEST_ERROR",
            message="Something went wrong",
        )

        assert error.error_type == "TEST_ERROR"
        assert error.message == "Something went wrong"
        assert error.suggestions == []
        assert error.context == {}

    def test_create_error_with_suggestions(self):
        """Test creating an error with suggestions."""
        from kicad_tools.mcp.errors import MCPError

        error = MCPError(
            error_type="FILE_NOT_FOUND",
            message="File not found: test.kicad_sch",
            suggestions=["Check the file path", "Ensure the file exists"],
            context={"file": "test.kicad_sch"},
        )

        assert error.error_type == "FILE_NOT_FOUND"
        assert len(error.suggestions) == 2
        assert error.context["file"] == "test.kicad_sch"

    def test_from_kicad_tools_error(self):
        """Test creating MCPError from KiCadToolsError."""
        from kicad_tools.exceptions import FileNotFoundError
        from kicad_tools.mcp.errors import MCPError

        exc = FileNotFoundError(
            "Schematic not found",
            context={"file": "design.kicad_sch"},
            suggestions=["Check the path"],
        )

        error = MCPError.from_exception(exc)

        assert error.error_type == "FILE_NOT_FOUND"
        assert error.message == "Schematic not found"
        assert "Check the path" in error.suggestions
        assert error.context["file"] == "design.kicad_sch"

    def test_from_generic_exception(self):
        """Test creating MCPError from generic exception."""
        from kicad_tools.mcp.errors import MCPError

        exc = ValueError("Invalid input")
        error = MCPError.from_exception(exc)

        assert error.error_type == "VALUEERROR"
        assert error.message == "Invalid input"

    def test_model_dump(self):
        """Test MCPError serialization."""
        from kicad_tools.mcp.errors import MCPError

        error = MCPError(
            error_type="TEST",
            message="Test message",
            suggestions=["suggestion1"],
            context={"key": "value"},
        )

        data = error.model_dump()
        assert data["error_type"] == "TEST"
        assert data["message"] == "Test message"
        assert data["suggestions"] == ["suggestion1"]
        assert data["context"]["key"] == "value"


@mcp_deps_required
class TestMCPTypes:
    """Test MCP response type models."""

    def test_mcp_result_success(self):
        """Test MCPResult success case."""
        from kicad_tools.mcp.types import MCPResult

        result = MCPResult(success=True)
        assert result.success is True
        assert result.error is None

    def test_mcp_result_error(self):
        """Test MCPResult error case."""
        from kicad_tools.mcp.errors import MCPError
        from kicad_tools.mcp.types import MCPResult

        error = MCPError(error_type="TEST", message="Test error")
        result = MCPResult(success=False, error=error)

        assert result.success is False
        assert result.error is not None
        assert result.error.error_type == "TEST"

    def test_file_result(self):
        """Test FileResult model."""
        from kicad_tools.mcp.types import FileResult

        result = FileResult(success=True, file_path="/path/to/file.kicad_sch")
        assert result.success is True
        assert result.file_path == "/path/to/file.kicad_sch"

    def test_list_result(self):
        """Test ListResult model."""
        from kicad_tools.mcp.types import ListResult

        result = ListResult(success=True, items=["a", "b", "c"], count=3)
        assert result.success is True
        assert result.items == ["a", "b", "c"]
        assert result.count == 3

    def test_analysis_result(self):
        """Test AnalysisResult model."""
        from kicad_tools.mcp.types import AnalysisResult

        result = AnalysisResult(
            success=True,
            summary="Analysis complete",
            details={"components": 10},
            metrics={"coverage": 0.95},
        )
        assert result.success is True
        assert result.summary == "Analysis complete"
        assert result.details["components"] == 10
        assert result.metrics["coverage"] == 0.95


@mcp_deps_required
class TestMCPErrorMapping:
    """Test exception to error type mapping."""

    def test_map_file_not_found(self):
        """Test mapping FileNotFoundError."""
        from kicad_tools.exceptions import FileNotFoundError
        from kicad_tools.mcp.errors import (
            ERROR_FILE_NOT_FOUND,
            map_exception_to_error_type,
        )

        exc = FileNotFoundError("File not found")
        assert map_exception_to_error_type(exc) == ERROR_FILE_NOT_FOUND

    def test_map_parse_error(self):
        """Test mapping ParseError."""
        from kicad_tools.exceptions import ParseError
        from kicad_tools.mcp.errors import ERROR_PARSE_ERROR, map_exception_to_error_type

        exc = ParseError("Parse failed")
        assert map_exception_to_error_type(exc) == ERROR_PARSE_ERROR

    def test_map_validation_error(self):
        """Test mapping ValidationError."""
        from kicad_tools.exceptions import ValidationError
        from kicad_tools.mcp.errors import (
            ERROR_VALIDATION_ERROR,
            map_exception_to_error_type,
        )

        exc = ValidationError(["Error 1", "Error 2"])
        assert map_exception_to_error_type(exc) == ERROR_VALIDATION_ERROR

    def test_map_unknown_error(self):
        """Test mapping unknown exception types."""
        from kicad_tools.mcp.errors import ERROR_INTERNAL_ERROR, map_exception_to_error_type

        exc = RuntimeError("Unknown error")
        assert map_exception_to_error_type(exc) == ERROR_INTERNAL_ERROR


@mcp_deps_required
class TestMCPServerCreation:
    """Test MCP server creation (without MCP dependency)."""

    def test_check_mcp_not_available(self):
        """Test detection of missing MCP package."""
        # This test verifies the import check works
        # The actual mcp package may or may not be installed
        from kicad_tools.mcp.server import _check_mcp_available

        # Just verify the function runs without error
        result = _check_mcp_available()
        assert isinstance(result, bool)

    def test_server_module_imports(self):
        """Test that server module can be imported."""
        from kicad_tools.mcp import server

        assert hasattr(server, "run_mcp")
        assert hasattr(server, "create_server")

    def test_mcp_module_exports(self):
        """Test that main MCP module exports expected symbols."""
        from kicad_tools import mcp

        assert hasattr(mcp, "MCPTools")
        assert hasattr(mcp, "MCPError")
        assert hasattr(mcp, "run_mcp")


class TestMCPCLI:
    """Test MCP CLI command handling."""

    def test_mcp_command_handler_exists(self):
        """Test that MCP command handler is registered."""
        from kicad_tools.cli.commands import run_mcp_command

        assert callable(run_mcp_command)

    def test_mcp_parser_exists(self):
        """Test that MCP parser is included in main parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        # Parse mcp help to verify the subparser exists
        # This will succeed if the parser is properly registered
        args = parser.parse_args(["mcp"])
        assert args.command == "mcp"

    def test_mcp_serve_parser(self):
        """Test MCP serve subcommand parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve", "--http", "--port", "9000", "--debug"])

        assert args.command == "mcp"
        assert args.mcp_command == "serve"
        assert args.mcp_http is True
        assert args.mcp_port == 9000
        assert args.mcp_debug is True

    def test_mcp_serve_defaults(self):
        """Test MCP serve subcommand default values."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["mcp", "serve"])

        assert args.mcp_http is False
        assert args.mcp_port == 8000
        assert args.mcp_debug is False
