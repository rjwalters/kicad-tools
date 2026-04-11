"""Tests for MCP screenshot_board and screenshot_schematic tools.

Tests the screenshot pipeline: kicad-cli SVG export -> cairosvg PNG
conversion -> base64 encoding -> MCP response.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.mcp.tools.screenshot import (
    DEFAULT_LAYERS,
    LAYER_PRESETS,
    _check_cairosvg,
    _png_dimensions,
    _resolve_layers,
    screenshot_board,
    screenshot_schematic,
)

# Test fixture: small voltage divider board
VOLTAGE_DIVIDER_PCB = str(
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)

VOLTAGE_DIVIDER_SCH = str(
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_sch"
)


# ---------------------------------------------------------------------------
# Unit tests for internal helpers
# ---------------------------------------------------------------------------


class TestResolveLayers:
    """Tests for _resolve_layers helper."""

    def test_none_returns_defaults(self):
        """None input returns default layer list."""
        result = _resolve_layers(None)
        assert result == DEFAULT_LAYERS

    def test_preset_name(self):
        """Preset name string resolves to known layer list."""
        result = _resolve_layers("copper")
        assert result == LAYER_PRESETS["copper"]

    def test_comma_separated_string(self):
        """Comma-separated string is split into layer list."""
        result = _resolve_layers("F.Cu,B.Cu,Edge.Cuts")
        assert result == ["F.Cu", "B.Cu", "Edge.Cuts"]

    def test_list_passthrough(self):
        """List input is passed through unchanged."""
        layers = ["F.Cu", "B.Cu"]
        result = _resolve_layers(layers)
        assert result == layers

    def test_all_presets_exist(self):
        """All documented presets resolve successfully."""
        for name in ["default", "copper", "assembly", "front", "back"]:
            result = _resolve_layers(name)
            assert isinstance(result, list)
            assert len(result) > 0


class TestPngDimensions:
    """Tests for _png_dimensions helper."""

    def test_too_short_returns_zero(self):
        """Bytes shorter than 24 returns (0, 0)."""
        assert _png_dimensions(b"short") == (0, 0)

    def test_valid_png_header(self):
        """Valid PNG header bytes return correct dimensions."""
        # Create a minimal PNG-like header with known dimensions
        # PNG signature (8 bytes) + IHDR length (4 bytes) + "IHDR" (4 bytes)
        # + width (4 bytes) + height (4 bytes)
        header = b"\x89PNG\r\n\x1a\n"  # 8 bytes signature
        header += b"\x00\x00\x00\r"  # IHDR length
        header += b"IHDR"  # chunk type
        header += (100).to_bytes(4, byteorder="big")  # width = 100
        header += (200).to_bytes(4, byteorder="big")  # height = 200
        header += b"\x00" * 5  # bit depth, color type, etc.

        w, h = _png_dimensions(header)
        assert w == 100
        assert h == 200


# ---------------------------------------------------------------------------
# Unit tests for error handling
# ---------------------------------------------------------------------------


class TestScreenshotBoardErrors:
    """Tests for screenshot_board error handling."""

    def test_nonexistent_file(self):
        """Non-existent PCB file returns error."""
        result = screenshot_board(pcb_path="/nonexistent/board.kicad_pcb")
        assert result["success"] is False
        assert "not found" in result["error_message"]
        assert result["image_base64"] is None

    def test_wrong_extension(self, tmp_path):
        """Wrong file extension returns error."""
        bad_file = tmp_path / "board.txt"
        bad_file.write_text("not a pcb")
        result = screenshot_board(pcb_path=str(bad_file))
        assert result["success"] is False
        assert "Invalid file extension" in result["error_message"]

    @patch("kicad_tools.mcp.tools.screenshot.find_kicad_cli", return_value=None)
    def test_kicad_cli_not_found(self, mock_find, tmp_path):
        """Missing kicad-cli returns error with install URL."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb ...)")
        result = screenshot_board(pcb_path=str(pcb_file))
        assert result["success"] is False
        assert "kicad-cli not found" in result["error_message"]
        assert "kicad.org/download" in result["error_message"]

    @patch("kicad_tools.mcp.tools.screenshot._check_cairosvg", return_value=False)
    @patch(
        "kicad_tools.mcp.tools.screenshot.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")
    )
    def test_cairosvg_not_installed(self, mock_find, mock_cairo, tmp_path):
        """Missing cairosvg returns error with install hint."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb ...)")
        result = screenshot_board(pcb_path=str(pcb_file))
        assert result["success"] is False
        assert "cairosvg" in result["error_message"]
        assert "kicad-tools[screenshot]" in result["error_message"]


class TestScreenshotSchematicErrors:
    """Tests for screenshot_schematic error handling."""

    def test_nonexistent_file(self):
        """Non-existent schematic file returns error."""
        result = screenshot_schematic(sch_path="/nonexistent/schematic.kicad_sch")
        assert result["success"] is False
        assert "not found" in result["error_message"]

    def test_wrong_extension(self, tmp_path):
        """Wrong file extension returns error."""
        bad_file = tmp_path / "schematic.txt"
        bad_file.write_text("not a schematic")
        result = screenshot_schematic(sch_path=str(bad_file))
        assert result["success"] is False
        assert "Invalid file extension" in result["error_message"]

    @patch("kicad_tools.mcp.tools.screenshot.find_kicad_cli", return_value=None)
    def test_kicad_cli_not_found(self, mock_find, tmp_path):
        """Missing kicad-cli returns error with install URL."""
        sch_file = tmp_path / "schematic.kicad_sch"
        sch_file.write_text("(kicad_sch ...)")
        result = screenshot_schematic(sch_path=str(sch_file))
        assert result["success"] is False
        assert "kicad-cli not found" in result["error_message"]
        assert "kicad.org/download" in result["error_message"]


# ---------------------------------------------------------------------------
# Registry integration tests
# ---------------------------------------------------------------------------


class TestScreenshotRegistry:
    """Tests for screenshot tool registration."""

    def test_screenshot_board_in_registry(self):
        """screenshot_board appears in TOOL_REGISTRY after import."""
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        assert "screenshot_board" in TOOL_REGISTRY

    def test_screenshot_schematic_in_registry(self):
        """screenshot_schematic appears in TOOL_REGISTRY after import."""
        from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

        assert "screenshot_schematic" in TOOL_REGISTRY

    def test_screenshot_board_tool_spec(self):
        """screenshot_board has valid tool spec."""
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("screenshot_board")
        assert tool is not None
        assert tool.name == "screenshot_board"
        assert tool.category == "screenshot"
        assert "pcb_path" in tool.parameters["properties"]
        assert callable(tool.handler)

    def test_screenshot_schematic_tool_spec(self):
        """screenshot_schematic has valid tool spec."""
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("screenshot_schematic")
        assert tool is not None
        assert tool.name == "screenshot_schematic"
        assert tool.category == "screenshot"
        assert "sch_path" in tool.parameters["properties"]
        assert callable(tool.handler)

    def test_screenshot_tools_in_category(self):
        """Screenshot tools appear in their category."""
        from kicad_tools.mcp.tools.registry import list_tools

        screenshot_tools = list_tools(category="screenshot")
        names = [t.name for t in screenshot_tools]
        assert "screenshot_board" in names
        assert "screenshot_schematic" in names


# ---------------------------------------------------------------------------
# MCP server integration tests
# ---------------------------------------------------------------------------


class TestScreenshotMCPServer:
    """Tests for screenshot tool integration with MCP server."""

    def test_screenshot_board_in_server(self):
        """screenshot_board appears in MCPServer tools."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()
        assert "screenshot_board" in server.tools

    def test_screenshot_schematic_in_server(self):
        """screenshot_schematic appears in MCPServer tools."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()
        assert "screenshot_schematic" in server.tools

    def test_server_tool_list_includes_screenshots(self):
        """tools/list response includes screenshot tools."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()
        tools_list = server.get_tools_list()
        tool_names = [t["name"] for t in tools_list]
        assert "screenshot_board" in tool_names
        assert "screenshot_schematic" in tool_names


# ---------------------------------------------------------------------------
# MCP content block tests (unit)
# ---------------------------------------------------------------------------


class TestMCPContentBlocks:
    """Tests for _mcp_content passthrough in MCP server."""

    def test_mcp_content_passthrough(self):
        """Server uses _mcp_content blocks when present in handler result."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        # Create a mock tool that returns _mcp_content
        from kicad_tools.mcp.server import ToolDefinition

        mock_content = [
            {"type": "text", "text": '{"success": true}'},
            {"type": "image", "data": "base64data", "mimeType": "image/png"},
        ]

        def mock_handler(params):
            return {"success": True, "_mcp_content": mock_content}

        server.tools["test_image_tool"] = ToolDefinition(
            name="test_image_tool",
            description="Test",
            parameters={"type": "object", "properties": {}},
            handler=mock_handler,
        )

        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "test_image_tool", "arguments": {}},
            }
        )

        assert response["result"]["content"] == mock_content

    def test_no_mcp_content_uses_json(self):
        """Server wraps result as JSON text when no _mcp_content."""
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()

        from kicad_tools.mcp.server import ToolDefinition

        def mock_handler(params):
            return {"success": True, "value": 42}

        server.tools["test_plain_tool"] = ToolDefinition(
            name="test_plain_tool",
            description="Test",
            parameters={"type": "object", "properties": {}},
            handler=mock_handler,
        )

        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "test_plain_tool", "arguments": {}},
            }
        )

        content = response["result"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"


# ---------------------------------------------------------------------------
# Integration tests (require kicad-cli)
# ---------------------------------------------------------------------------


class TestScreenshotBoardIntegration:
    """Integration tests for screenshot_board with real kicad-cli."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_kicad_cli(self):
        """Skip if kicad-cli is not available."""
        from kicad_tools.cli.runner import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not found")

    @pytest.fixture(autouse=True)
    def _skip_if_no_cairosvg(self):
        """Skip if cairosvg is not installed."""
        if not _check_cairosvg():
            pytest.skip("cairosvg not installed")

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_board_success(self):
        """Screenshot of voltage divider board succeeds."""
        result = screenshot_board(pcb_path=VOLTAGE_DIVIDER_PCB)

        assert result["success"] is True
        assert result["image_base64"] is not None
        assert len(result["image_base64"]) > 0
        assert result["width_px"] > 0
        assert result["height_px"] > 0
        assert result["error_message"] is None

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_max_size_respected(self):
        """Image dimensions respect max_size_px constraint."""
        result = screenshot_board(
            pcb_path=VOLTAGE_DIVIDER_PCB,
            max_size_px=800,
        )

        assert result["success"] is True
        assert max(result["width_px"], result["height_px"]) <= 800

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_output_path(self, tmp_path):
        """Screenshot is saved to output_path when specified."""
        output = tmp_path / "test_board.png"
        result = screenshot_board(
            pcb_path=VOLTAGE_DIVIDER_PCB,
            output_path=str(output),
        )

        assert result["success"] is True
        assert output.exists()
        assert output.stat().st_size > 0
        assert result["output_path"] == str(output)

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_base64_decodes_to_valid_png(self):
        """Base64 data decodes to valid PNG bytes."""
        result = screenshot_board(pcb_path=VOLTAGE_DIVIDER_PCB)

        assert result["success"] is True
        png_bytes = base64.b64decode(result["image_base64"])
        # PNG signature
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_copper_preset(self):
        """Copper layer preset produces valid screenshot."""
        result = screenshot_board(
            pcb_path=VOLTAGE_DIVIDER_PCB,
            layers="copper",
        )

        assert result["success"] is True
        assert result["layers_rendered"] == LAYER_PRESETS["copper"]

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_black_and_white(self):
        """Black and white mode produces valid screenshot."""
        result = screenshot_board(
            pcb_path=VOLTAGE_DIVIDER_PCB,
            black_and_white=True,
        )

        assert result["success"] is True
        assert result["image_base64"] is not None

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_PCB).exists(),
        reason="voltage divider board fixture not found",
    )
    def test_screenshot_vision_api_size(self):
        """Default screenshot fits within vision API 1568px limit."""
        result = screenshot_board(pcb_path=VOLTAGE_DIVIDER_PCB)

        assert result["success"] is True
        assert result["width_px"] <= 1568
        assert result["height_px"] <= 1568


class TestScreenshotSchematicIntegration:
    """Integration tests for screenshot_schematic with real kicad-cli."""

    @pytest.fixture(autouse=True)
    def _skip_if_no_kicad_cli(self):
        """Skip if kicad-cli is not available."""
        from kicad_tools.cli.runner import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not found")

    @pytest.fixture(autouse=True)
    def _skip_if_no_cairosvg(self):
        """Skip if cairosvg is not installed."""
        if not _check_cairosvg():
            pytest.skip("cairosvg not installed")

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_SCH).exists(),
        reason="voltage divider schematic fixture not found",
    )
    def test_screenshot_schematic_success(self):
        """Screenshot of voltage divider schematic succeeds."""
        result = screenshot_schematic(sch_path=VOLTAGE_DIVIDER_SCH)

        assert result["success"] is True
        assert result["image_base64"] is not None
        assert len(result["image_base64"]) > 0
        assert result["width_px"] > 0
        assert result["height_px"] > 0
        assert result["error_message"] is None

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_SCH).exists(),
        reason="voltage divider schematic fixture not found",
    )
    def test_screenshot_schematic_base64_decodes_to_valid_png(self):
        """Base64 data from schematic screenshot decodes to valid PNG bytes."""
        result = screenshot_schematic(sch_path=VOLTAGE_DIVIDER_SCH)

        assert result["success"] is True
        png_bytes = base64.b64decode(result["image_base64"])
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_SCH).exists(),
        reason="voltage divider schematic fixture not found",
    )
    def test_screenshot_schematic_max_size_respected(self):
        """Schematic image dimensions respect max_size_px constraint."""
        result = screenshot_schematic(
            sch_path=VOLTAGE_DIVIDER_SCH,
            max_size_px=800,
        )

        assert result["success"] is True
        assert max(result["width_px"], result["height_px"]) <= 800

    @pytest.mark.skipif(
        not Path(VOLTAGE_DIVIDER_SCH).exists(),
        reason="voltage divider schematic fixture not found",
    )
    def test_screenshot_schematic_output_path(self, tmp_path):
        """Schematic screenshot is saved to output_path when specified."""
        output = tmp_path / "test_schematic.png"
        result = screenshot_schematic(
            sch_path=VOLTAGE_DIVIDER_SCH,
            output_path=str(output),
        )

        assert result["success"] is True
        assert output.exists()
        assert output.stat().st_size > 0
        assert result["output_path"] == str(output)
