"""Tests for MCP screenshot_board and screenshot_schematic tools.

Tests the screenshot pipeline: kicad-cli SVG export -> cairosvg PNG
conversion -> base64 encoding -> MCP response.
"""

from __future__ import annotations

import base64
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.mcp.tools.screenshot import (
    _FALLBACK_RENDER_PX,
    DEFAULT_LAYERS,
    LAYER_PRESETS,
    _check_cairosvg,
    _macos_cairo_lib_dirs,
    _png_dimensions,
    _resolve_layers,
    _svg_to_png,
    _try_preload_cairo_macos,
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
# Unit tests for _svg_to_png dimensionless SVG handling
# ---------------------------------------------------------------------------


def _make_fake_png(width: int, height: int) -> bytes:
    """Build a minimal PNG byte string with the given dimensions in the IHDR."""
    header = b"\x89PNG\r\n\x1a\n"  # 8-byte PNG signature
    header += b"\x00\x00\x00\r"  # IHDR chunk length
    header += b"IHDR"  # chunk type
    header += width.to_bytes(4, byteorder="big")
    header += height.to_bytes(4, byteorder="big")
    header += b"\x00" * 5  # bit depth, color type, etc.
    return header


class TestSvgToPngDimensionlessSvg:
    """Tests for _svg_to_png handling of SVGs without width/height attributes."""

    def test_dimensionless_svg_uses_fallback_width(self, tmp_path):
        """A viewBox-only SVG (no width/height) renders successfully via the
        fallback output_width and produces correct dimensions."""
        svg_file = tmp_path / "dimensionless.svg"
        svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100"></svg>')
        png_file = tmp_path / "output.png"

        fake_png_first = _make_fake_png(4096, 2048)
        fake_png_second = _make_fake_png(1568, 784)

        fake_cairosvg = types.ModuleType("cairosvg")
        call_log = []

        def fake_svg2png(**kwargs):
            call_log.append(kwargs)
            if len(call_log) == 1:
                return fake_png_first
            return fake_png_second

        fake_cairosvg.svg2png = fake_svg2png

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            ok, err, w, h = _svg_to_png(svg_file, png_file)

        assert ok is True
        assert err == ""
        assert w > 0
        assert h > 0

        # First call should include output_width=_FALLBACK_RENDER_PX
        assert call_log[0]["output_width"] == _FALLBACK_RENDER_PX

        # Second call should include target dimensions
        assert "output_width" in call_log[1]
        assert "output_height" in call_log[1]

    def test_dimensionless_svg_without_viewbox(self, tmp_path):
        """An SVG with neither width/height nor viewBox still gets
        output_width on the first call; if cairosvg returns 0x0 the function
        returns a graceful failure (not a crash)."""
        svg_file = tmp_path / "empty.svg"
        svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        png_file = tmp_path / "output.png"

        # Simulate cairosvg returning a 0x0 PNG (too short to parse)
        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: b""

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            ok, err, w, h = _svg_to_png(svg_file, png_file)

        assert ok is False
        assert w == 0
        assert h == 0

    def test_svg_with_explicit_dimensions_still_works(self, tmp_path):
        """An SVG with explicit width/height attributes still renders
        correctly with the fallback width on the first pass."""
        svg_file = tmp_path / "sized.svg"
        svg_file.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600"></svg>'
        )
        png_file = tmp_path / "output.png"

        fake_png_first = _make_fake_png(4096, 3072)
        fake_png_second = _make_fake_png(1568, 1176)

        fake_cairosvg = types.ModuleType("cairosvg")
        calls = []

        def fake_svg2png(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return fake_png_first
            return fake_png_second

        fake_cairosvg.svg2png = fake_svg2png

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            ok, err, w, h = _svg_to_png(svg_file, png_file)

        assert ok is True
        assert err == ""
        assert w <= 1568
        assert h <= 1568

    def test_svg_to_png_catches_value_error(self, tmp_path):
        """If cairosvg.svg2png raises ValueError, _svg_to_png returns a
        graceful failure tuple instead of propagating the exception."""
        svg_file = tmp_path / "bad.svg"
        svg_file.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        png_file = tmp_path / "output.png"

        fake_cairosvg = types.ModuleType("cairosvg")

        def raise_value_error(**kwargs):
            raise ValueError("The SVG size is undefined")

        fake_cairosvg.svg2png = raise_value_error

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            ok, err, w, h = _svg_to_png(svg_file, png_file)

        assert ok is False
        assert "SVG size is undefined" in err
        assert w == 0
        assert h == 0


class TestCheckCairosvgValueError:
    """Tests for _check_cairosvg handling of ValueError."""

    def test_returns_false_on_value_error(self):
        """_check_cairosvg returns False when svg2png raises ValueError
        (e.g. from newer cairosvg with dimensionless probe SVG)."""
        fake_cairosvg = types.ModuleType("cairosvg")

        def raise_value_error(**kwargs):
            raise ValueError("The SVG size is undefined")

        fake_cairosvg.svg2png = raise_value_error

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            assert _check_cairosvg() is False

    def test_probe_svg_has_dimensions(self):
        """The probe SVG passed to svg2png includes width and height
        attributes so it works with all cairosvg versions."""
        fake_cairosvg = types.ModuleType("cairosvg")
        received_args = {}

        def capture_svg2png(**kwargs):
            received_args.update(kwargs)
            return b"\x89PNG"

        fake_cairosvg.svg2png = capture_svg2png

        with patch.dict(sys.modules, {"cairosvg": fake_cairosvg}):
            result = _check_cairosvg()

        assert result is True
        svg_bytes = received_args.get("bytestring", b"")
        assert b"width=" in svg_bytes
        assert b"height=" in svg_bytes


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


# ---------------------------------------------------------------------------
# macOS cairo auto-detection tests
# ---------------------------------------------------------------------------


class TestMacosCairoLibDirs:
    """Tests for _macos_cairo_lib_dirs helper."""

    def test_homebrew_prefix_respected(self, tmp_path):
        """HOMEBREW_PREFIX env var is included first in candidates."""
        fake_lib = tmp_path / "lib"
        fake_lib.mkdir()

        with patch.dict("os.environ", {"HOMEBREW_PREFIX": str(tmp_path)}, clear=False):
            dirs = _macos_cairo_lib_dirs()

        assert str(fake_lib) in dirs
        # HOMEBREW_PREFIX entry should be first
        assert dirs[0] == str(fake_lib)

    def test_deduplicates_when_homebrew_prefix_matches_default(self, tmp_path):
        """If HOMEBREW_PREFIX/lib matches a default path, no duplicates."""
        # Use /opt/homebrew as HOMEBREW_PREFIX so its /lib overlaps
        with patch.dict("os.environ", {"HOMEBREW_PREFIX": "/opt/homebrew"}, clear=False):
            dirs = _macos_cairo_lib_dirs()

        # /opt/homebrew/lib should appear at most once
        assert dirs.count("/opt/homebrew/lib") <= 1

    def test_only_existing_dirs_returned(self):
        """Directories that do not exist on disk are excluded."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove HOMEBREW_PREFIX to avoid interference
            import os

            env = os.environ.copy()
            env.pop("HOMEBREW_PREFIX", None)
            with patch.dict("os.environ", env, clear=True):
                dirs = _macos_cairo_lib_dirs()
                for d in dirs:
                    assert Path(d).is_dir()

    def test_returns_empty_when_no_dirs_exist(self):
        """Returns empty list when none of the candidate dirs exist."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("kicad_tools.mcp.tools.screenshot.Path.is_dir", return_value=False):
                dirs = _macos_cairo_lib_dirs()
                assert dirs == []


class TestTryPreloadCairoMacos:
    """Tests for _try_preload_cairo_macos helper."""

    def test_succeeds_when_lib_exists_and_probe_passes(self, tmp_path):
        """Pre-loading from a valid path makes the probe succeed."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        dylib_path = lib_dir / "libcairo.dylib"
        dylib_path.write_bytes(b"fake dylib")

        fake_cairosvg = types.ModuleType("cairosvg")
        # First call (in _check_cairosvg) raises OSError; after preload succeeds
        call_count = [0]

        def fake_svg2png(**kwargs):
            call_count[0] += 1
            return b"\x89PNG"

        fake_cairosvg.svg2png = fake_svg2png

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._macos_cairo_lib_dirs",
                return_value=[str(lib_dir)],
            ),
            patch("kicad_tools.mcp.tools.screenshot.ctypes.cdll") as mock_cdll,
        ):
            mock_cdll.LoadLibrary.return_value = None
            result = _try_preload_cairo_macos()

        assert result is True
        mock_cdll.LoadLibrary.assert_called_once_with(str(dylib_path))

    def test_returns_false_when_no_dylib_exists(self, tmp_path):
        """Returns False when no libcairo.dylib is found in any candidate dir."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        # No libcairo.dylib created

        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: b"\x89PNG"

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._macos_cairo_lib_dirs",
                return_value=[str(lib_dir)],
            ),
        ):
            result = _try_preload_cairo_macos()

        assert result is False

    def test_returns_false_when_load_library_fails(self, tmp_path):
        """Returns False when ctypes.cdll.LoadLibrary raises OSError."""
        lib_dir = tmp_path / "lib"
        lib_dir.mkdir()
        (lib_dir / "libcairo.dylib").write_bytes(b"fake")

        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: (_ for _ in ()).throw(OSError("cannot load"))

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._macos_cairo_lib_dirs",
                return_value=[str(lib_dir)],
            ),
            patch("kicad_tools.mcp.tools.screenshot.ctypes.cdll") as mock_cdll,
        ):
            mock_cdll.LoadLibrary.side_effect = OSError("bad library")
            result = _try_preload_cairo_macos()

        assert result is False

    def test_tries_multiple_dirs_on_failure(self, tmp_path):
        """Tries next directory when first one fails."""
        dir1 = tmp_path / "dir1"
        dir1.mkdir()
        (dir1 / "libcairo.dylib").write_bytes(b"fake")

        dir2 = tmp_path / "dir2"
        dir2.mkdir()
        (dir2 / "libcairo.dylib").write_bytes(b"fake")

        fake_cairosvg = types.ModuleType("cairosvg")
        probe_calls = [0]

        def fake_svg2png(**kwargs):
            probe_calls[0] += 1
            if probe_calls[0] == 1:
                raise OSError("still broken")
            return b"\x89PNG"

        fake_cairosvg.svg2png = fake_svg2png

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._macos_cairo_lib_dirs",
                return_value=[str(dir1), str(dir2)],
            ),
            patch("kicad_tools.mcp.tools.screenshot.ctypes.cdll") as mock_cdll,
        ):
            mock_cdll.LoadLibrary.return_value = None
            result = _try_preload_cairo_macos()

        assert result is True
        assert mock_cdll.LoadLibrary.call_count == 2

    def test_returns_false_when_no_candidates(self):
        """Returns False when _macos_cairo_lib_dirs returns empty list."""
        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: b"\x89PNG"

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._macos_cairo_lib_dirs",
                return_value=[],
            ),
        ):
            result = _try_preload_cairo_macos()

        assert result is False


class TestCheckCairosvgMacosAutoDetect:
    """Tests for _check_cairosvg macOS auto-detection integration."""

    def test_oserror_on_darwin_triggers_preload(self):
        """On macOS, OSError from probe triggers _try_preload_cairo_macos."""
        fake_cairosvg = types.ModuleType("cairosvg")
        call_count = [0]

        def fake_svg2png(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise OSError("no library called 'cairo-2' was found")
            return b"\x89PNG"

        fake_cairosvg.svg2png = fake_svg2png

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch("kicad_tools.mcp.tools.screenshot.sys") as mock_sys,
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
                return_value=True,
            ) as mock_preload,
        ):
            mock_sys.platform = "darwin"
            result = _check_cairosvg()

        assert result is True
        mock_preload.assert_called_once()

    def test_oserror_on_linux_does_not_trigger_preload(self):
        """On Linux, OSError from probe returns False without attempting preload."""
        fake_cairosvg = types.ModuleType("cairosvg")

        def raise_os_error(**kwargs):
            raise OSError("no library called 'cairo-2' was found")

        fake_cairosvg.svg2png = raise_os_error

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch("kicad_tools.mcp.tools.screenshot.sys") as mock_sys,
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
            ) as mock_preload,
        ):
            mock_sys.platform = "linux"
            result = _check_cairosvg()

        assert result is False
        mock_preload.assert_not_called()

    def test_import_error_returns_false_without_preload(self):
        """ImportError returns False without any macOS preload attempt."""
        with (
            patch.dict(sys.modules, {"cairosvg": None}),
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
            ) as mock_preload,
        ):
            result = _check_cairosvg()

        assert result is False
        mock_preload.assert_not_called()

    def test_successful_probe_does_not_trigger_preload(self):
        """When probe succeeds initially, no preload is attempted."""
        fake_cairosvg = types.ModuleType("cairosvg")
        fake_cairosvg.svg2png = lambda **kwargs: b"\x89PNG"

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
            ) as mock_preload,
        ):
            result = _check_cairosvg()

        assert result is True
        mock_preload.assert_not_called()

    def test_value_error_on_darwin_triggers_preload(self):
        """On macOS, ValueError from probe also triggers auto-detection."""
        fake_cairosvg = types.ModuleType("cairosvg")

        def raise_value_error(**kwargs):
            raise ValueError("The SVG size is undefined")

        fake_cairosvg.svg2png = raise_value_error

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch("kicad_tools.mcp.tools.screenshot.sys") as mock_sys,
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
                return_value=False,
            ) as mock_preload,
        ):
            mock_sys.platform = "darwin"
            result = _check_cairosvg()

        assert result is False
        mock_preload.assert_called_once()

    def test_preload_failure_returns_false(self):
        """When preload attempt fails, _check_cairosvg returns False."""
        fake_cairosvg = types.ModuleType("cairosvg")

        def raise_os_error(**kwargs):
            raise OSError("no library called 'cairo-2' was found")

        fake_cairosvg.svg2png = raise_os_error

        with (
            patch.dict(sys.modules, {"cairosvg": fake_cairosvg}),
            patch("kicad_tools.mcp.tools.screenshot.sys") as mock_sys,
            patch(
                "kicad_tools.mcp.tools.screenshot._try_preload_cairo_macos",
                return_value=False,
            ),
        ):
            mock_sys.platform = "darwin"
            result = _check_cairosvg()

        assert result is False
