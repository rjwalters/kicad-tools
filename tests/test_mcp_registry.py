"""Tests for the unified MCP tool registry.

Tests the registry that provides a single source of truth for tool definitions
used by both stdio and HTTP transports.
"""

from __future__ import annotations

import pytest

from kicad_tools.mcp.tools.registry import (
    TOOL_REGISTRY,
    ToolSpec,
    clear_registry,
    get_tool,
    list_tools,
    register_tool,
)


class TestToolSpec:
    """Tests for ToolSpec dataclass."""

    def test_toolspec_creation(self):
        """Test that ToolSpec can be created with required fields."""
        spec = ToolSpec(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}, "required": []},
            handler=lambda params: {"success": True},
        )

        assert spec.name == "test_tool"
        assert spec.description == "A test tool"
        assert spec.category == "general"  # default

    def test_toolspec_with_category(self):
        """Test that ToolSpec accepts custom category."""
        spec = ToolSpec(
            name="test_tool",
            description="A test tool",
            parameters={"type": "object", "properties": {}},
            handler=lambda params: {},
            category="custom",
        )

        assert spec.category == "custom"


class TestToolRegistry:
    """Tests for the tool registry."""

    def test_registry_populated(self):
        """Test that registry is populated with tools on import."""
        # Registry should have tools registered at import time
        assert len(TOOL_REGISTRY) > 0

    def test_registry_has_expected_tools(self):
        """Test that registry contains expected tool names."""
        expected_tools = [
            "export_gerbers",
            "export_bom",
            "export_assembly",
            "validate_assembly_bom",
            "placement_analyze",
            "placement_suggestions",
            "start_session",
            "query_move",
            "apply_move",
            "undo_move",
            "commit_session",
            "rollback_session",
            "record_decision",
            "get_decision_history",
            "annotate_decision",
            "get_session_context",
            "create_checkpoint",
            "restore_checkpoint",
            "get_session_summary",
            "measure_clearance",
            "get_unrouted_nets",
            "route_net",
            "validate_pattern",
            "adapt_pattern",
            "get_component_requirements",
            "list_pattern_components",
            "detect_mistakes",
            "list_mistake_categories",
        ]

        for tool_name in expected_tools:
            assert tool_name in TOOL_REGISTRY, f"Tool {tool_name} not found in registry"

    def test_get_tool(self):
        """Test getting a tool by name."""
        tool = get_tool("export_gerbers")

        assert tool is not None
        assert tool.name == "export_gerbers"
        assert "Gerber" in tool.description

    def test_get_tool_not_found(self):
        """Test getting a non-existent tool."""
        tool = get_tool("nonexistent_tool")

        assert tool is None

    def test_list_tools(self):
        """Test listing all tools."""
        tools = list_tools()

        assert len(tools) > 0
        assert all(isinstance(t, ToolSpec) for t in tools)

    def test_list_tools_by_category(self):
        """Test listing tools filtered by category."""
        export_tools = list_tools(category="export")
        session_tools = list_tools(category="session")

        assert len(export_tools) > 0
        assert all(t.category == "export" for t in export_tools)

        assert len(session_tools) > 0
        assert all(t.category == "session" for t in session_tools)

    def test_tool_has_valid_parameters(self):
        """Test that all tools have valid JSON Schema parameters."""
        for name, tool in TOOL_REGISTRY.items():
            assert "type" in tool.parameters, f"Tool {name} missing 'type' in parameters"
            assert tool.parameters["type"] == "object", f"Tool {name} parameters not object type"
            assert "properties" in tool.parameters, f"Tool {name} missing 'properties'"

    def test_tool_has_handler(self):
        """Test that all tools have callable handlers."""
        for name, tool in TOOL_REGISTRY.items():
            assert callable(tool.handler), f"Tool {name} handler not callable"


class TestToolCategories:
    """Tests for tool categorization."""

    def test_export_category(self):
        """Test that export tools are categorized correctly."""
        export_tools = list_tools(category="export")
        export_names = [t.name for t in export_tools]

        assert "export_gerbers" in export_names
        assert "export_bom" in export_names
        assert "export_assembly" in export_names
        assert "validate_assembly_bom" in export_names

    def test_session_category(self):
        """Test that session tools are categorized correctly."""
        session_tools = list_tools(category="session")
        session_names = [t.name for t in session_tools]

        assert "start_session" in session_names
        assert "query_move" in session_names
        assert "apply_move" in session_names
        assert "commit_session" in session_names

    def test_context_category(self):
        """Test that context tools are categorized correctly."""
        context_tools = list_tools(category="context")
        context_names = [t.name for t in context_tools]

        assert "record_decision" in context_names
        assert "get_decision_history" in context_names
        assert "create_checkpoint" in context_names


class TestToolHandlers:
    """Tests for tool handler invocation."""

    def test_handler_accepts_dict_params(self):
        """Test that handlers accept dictionary parameters."""
        # Get a simple tool that doesn't require files
        tool = get_tool("list_mistake_categories")

        # Handler should accept empty dict and return a result
        result = tool.handler({})

        assert isinstance(result, dict)

    def test_handler_returns_dict(self):
        """Test that handlers return dictionary results."""
        tool = get_tool("list_pattern_components")

        result = tool.handler({})

        assert isinstance(result, dict)


class TestRegistryConsistency:
    """Tests for registry consistency with server implementations."""

    def test_registry_matches_mcp_server_tools(self):
        """Test that registry tools match MCPServer tools."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        # All registry tools should be in server
        for name in TOOL_REGISTRY:
            assert name in server.tools, f"Registry tool {name} not in MCPServer"

        # All server tools should be in registry
        for name in server.tools:
            assert name in TOOL_REGISTRY, f"Server tool {name} not in registry"

    def test_tool_descriptions_match(self):
        """Test that tool descriptions match between registry and server."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        for name, registry_tool in TOOL_REGISTRY.items():
            server_tool = server.tools[name]
            assert registry_tool.description == server_tool.description, (
                f"Description mismatch for {name}"
            )

    def test_tool_parameters_match(self):
        """Test that tool parameters match between registry and server."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        for name, registry_tool in TOOL_REGISTRY.items():
            server_tool = server.tools[name]
            assert registry_tool.parameters == server_tool.parameters, (
                f"Parameters mismatch for {name}"
            )
