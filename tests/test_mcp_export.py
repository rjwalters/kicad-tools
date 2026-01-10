"""Tests for MCP export tools."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.server import create_server
from kicad_tools.mcp.tools.export import (
    SUPPORTED_MANUFACTURERS,
    _determine_file_type,
    _extract_layer_from_filename,
    export_gerbers,
)
from kicad_tools.mcp.types import GerberExportResult, GerberFile, get_file_type


class TestGerberFile:
    """Tests for GerberFile dataclass."""

    def test_creation(self):
        gf = GerberFile(
            filename="board-F_Cu.gbr",
            layer="F.Cu",
            file_type="copper",
            size_bytes=12345,
        )
        assert gf.filename == "board-F_Cu.gbr"
        assert gf.layer == "F.Cu"
        assert gf.file_type == "copper"
        assert gf.size_bytes == 12345


class TestGerberExportResult:
    """Tests for GerberExportResult dataclass."""

    def test_success_result(self):
        result = GerberExportResult(
            success=True,
            output_dir="/tmp/gerbers",
            zip_file="/tmp/gerbers/board.zip",
            files=[
                GerberFile("board-F_Cu.gbr", "F.Cu", "copper", 1000),
                GerberFile("board-B_Cu.gbr", "B.Cu", "copper", 1000),
            ],
            layer_count=2,
        )
        assert result.success is True
        assert result.error is None
        assert len(result.files) == 2
        assert result.layer_count == 2

    def test_failure_result(self):
        result = GerberExportResult(
            success=False,
            output_dir="/tmp/gerbers",
            error="PCB file not found",
        )
        assert result.success is False
        assert result.error == "PCB file not found"

    def test_to_dict(self):
        result = GerberExportResult(
            success=True,
            output_dir="/tmp/gerbers",
            zip_file="/tmp/gerbers/board.zip",
            files=[GerberFile("board-F_Cu.gbr", "F.Cu", "copper", 1000)],
            layer_count=2,
            warnings=["Test warning"],
        )
        d = result.to_dict()

        assert d["success"] is True
        assert d["output_dir"] == "/tmp/gerbers"
        assert d["zip_file"] == "/tmp/gerbers/board.zip"
        assert len(d["files"]) == 1
        assert d["files"][0]["filename"] == "board-F_Cu.gbr"
        assert d["layer_count"] == 2
        assert d["warnings"] == ["Test warning"]


class TestGetFileType:
    """Tests for get_file_type function."""

    def test_copper_layers(self):
        assert get_file_type("F.Cu") == "copper"
        assert get_file_type("B.Cu") == "copper"
        assert get_file_type("In1.Cu") == "copper"

    def test_soldermask_layers(self):
        assert get_file_type("F.Mask") == "soldermask"
        assert get_file_type("B.Mask") == "soldermask"

    def test_silkscreen_layers(self):
        assert get_file_type("F.SilkS") == "silkscreen"
        assert get_file_type("B.SilkS") == "silkscreen"

    def test_paste_layers(self):
        assert get_file_type("F.Paste") == "paste"
        assert get_file_type("B.Paste") == "paste"

    def test_outline(self):
        assert get_file_type("Edge.Cuts") == "outline"

    def test_unknown(self):
        assert get_file_type("Unknown.Layer") == "other"


class TestExtractLayerFromFilename:
    """Tests for _extract_layer_from_filename function."""

    def test_copper_layers(self):
        assert _extract_layer_from_filename("board-F_Cu.gbr") == "F.Cu"
        assert _extract_layer_from_filename("board-B_Cu.gbr") == "B.Cu"
        assert _extract_layer_from_filename("board-In1_Cu.gbr") == "In1.Cu"

    def test_mask_layers(self):
        assert _extract_layer_from_filename("board-F_Mask.gbr") == "F.Mask"
        assert _extract_layer_from_filename("board-B_Mask.gbr") == "B.Mask"

    def test_silkscreen_layers(self):
        assert _extract_layer_from_filename("board-F_SilkS.gbr") == "F.SilkS"
        assert _extract_layer_from_filename("board-F_Silkscreen.gbr") == "F.SilkS"

    def test_edge_cuts(self):
        assert _extract_layer_from_filename("board-Edge_Cuts.gbr") == "Edge.Cuts"

    def test_drill_files(self):
        assert _extract_layer_from_filename("board-PTH.drl") == "PTH"
        assert _extract_layer_from_filename("board-NPTH.drl") == "NPTH"
        assert _extract_layer_from_filename("board.drl") == "drill"


class TestDetermineFileType:
    """Tests for _determine_file_type function."""

    def test_drill_by_extension(self):
        assert _determine_file_type("board.drl", "PTH") == "drill"
        assert _determine_file_type("board.xln", "NPTH") == "drill"

    def test_archive(self):
        assert _determine_file_type("board.zip", "unknown") == "archive"

    def test_protel_extensions(self):
        assert _determine_file_type("board.gtl", "unknown") == "copper"
        assert _determine_file_type("board.gbl", "unknown") == "copper"
        assert _determine_file_type("board.gts", "unknown") == "soldermask"
        assert _determine_file_type("board.gto", "unknown") == "silkscreen"

    def test_by_layer(self):
        assert _determine_file_type("board-F_Cu.gbr", "F.Cu") == "copper"


class TestExportGerbers:
    """Tests for export_gerbers function."""

    def test_file_not_found(self):
        result = export_gerbers(
            pcb_path="/nonexistent/board.kicad_pcb",
            output_dir="/tmp/gerbers",
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_unknown_manufacturer(self):
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as f:
            f.write(b"(kicad_pcb (version 20231014))")
            pcb_path = f.name

        result = export_gerbers(
            pcb_path=pcb_path,
            output_dir="/tmp/gerbers",
            manufacturer="unknown_mfr",
        )
        assert result.success is False
        assert "Unknown manufacturer" in result.error

        # Cleanup
        Path(pcb_path).unlink()

    def test_unusual_extension_warning(self):
        with tempfile.NamedTemporaryFile(suffix=".pcb", delete=False) as f:
            f.write(b"(kicad_pcb (version 20231014))")
            pcb_path = f.name

        result = export_gerbers(
            pcb_path=pcb_path,
            output_dir="/tmp/gerbers",
            manufacturer="unknown_mfr",  # Will fail before exporter runs
        )
        # Should have warning about extension
        assert any("extension" in w.lower() for w in result.warnings)

        # Cleanup
        Path(pcb_path).unlink()

    def test_supported_manufacturers(self):
        """Verify all supported manufacturers are valid."""
        assert "generic" in SUPPORTED_MANUFACTURERS
        assert "jlcpcb" in SUPPORTED_MANUFACTURERS
        assert "pcbway" in SUPPORTED_MANUFACTURERS
        assert "oshpark" in SUPPORTED_MANUFACTURERS
        assert "seeed" in SUPPORTED_MANUFACTURERS

    @patch("kicad_tools.mcp.tools.export.GerberExporter")
    def test_export_with_mock(self, mock_exporter_class):
        """Test export with mocked GerberExporter."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake PCB file
            pcb_path = Path(tmpdir) / "board.kicad_pcb"
            pcb_path.write_text("(kicad_pcb (version 20231014))")

            # Create fake output files
            output_dir = Path(tmpdir) / "gerbers"
            output_dir.mkdir()
            (output_dir / "board-F_Cu.gbr").write_text("gerber content")
            (output_dir / "board-B_Cu.gbr").write_text("gerber content")
            (output_dir / "gerbers.zip").write_bytes(b"PK...")

            # Mock the exporter
            mock_exporter = MagicMock()
            mock_exporter.export.return_value = output_dir / "gerbers.zip"
            mock_exporter_class.return_value = mock_exporter

            result = export_gerbers(
                pcb_path=str(pcb_path),
                output_dir=str(output_dir),
                manufacturer="jlcpcb",
            )

            assert result.success is True
            assert result.layer_count == 2
            assert len(result.files) >= 2


class TestMCPServer:
    """Tests for MCP server."""

    def test_create_server(self):
        server = create_server()
        assert server.name == "kicad-tools"
        assert "export_gerbers" in server.tools

    def test_get_tools_list(self):
        server = create_server()
        tools = server.get_tools_list()

        assert len(tools) >= 1

        export_tool = next(t for t in tools if t["name"] == "export_gerbers")
        assert "description" in export_tool
        assert "inputSchema" in export_tool
        assert "pcb_path" in export_tool["inputSchema"]["properties"]
        assert "output_dir" in export_tool["inputSchema"]["properties"]

    def test_handle_initialize(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        assert "protocolVersion" in response["result"]
        assert "serverInfo" in response["result"]

    def test_handle_tools_list(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert "result" in response
        assert "tools" in response["result"]

    def test_handle_tools_call_missing_file(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "export_gerbers",
                    "arguments": {
                        "pcb_path": "/nonexistent.kicad_pcb",
                        "output_dir": "/tmp/out",
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 3
        assert "result" in response
        # Result should contain error info
        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["success"] is False

    def test_handle_unknown_tool(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "unknown_tool",
                    "arguments": {},
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 4
        assert "error" in response

    def test_handle_unknown_method(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "unknown/method",
                "params": {},
            }
        )

        assert "error" in response
        assert response["error"]["code"] == -32601

    def test_handle_notification(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

        # Notifications return empty response
        assert response == {}

    def test_call_tool_directly(self):
        server = create_server()

        # Should raise for unknown tool
        with pytest.raises(ValueError, match="Unknown tool"):
            server.call_tool("nonexistent", {})
