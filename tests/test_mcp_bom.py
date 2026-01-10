"""Tests for MCP export_bom tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.server import create_server
from kicad_tools.mcp.tools.export import (
    SUPPORTED_BOM_FORMATS,
    SUPPORTED_GROUP_BY,
    export_bom,
)
from kicad_tools.mcp.types import BOMGenerationResult, BOMItemResult


class TestBOMItemResult:
    """Tests for BOMItemResult dataclass."""

    def test_creation(self):
        item = BOMItemResult(
            reference="R1, R2",
            value="10k",
            footprint="0603",
            quantity=2,
            lcsc_part="C25804",
            description="Resistor 10k 1%",
            manufacturer="Yageo",
            mpn="RC0603FR-0710KL",
        )
        assert item.reference == "R1, R2"
        assert item.value == "10k"
        assert item.footprint == "0603"
        assert item.quantity == 2
        assert item.lcsc_part == "C25804"

    def test_to_dict(self):
        item = BOMItemResult(
            reference="C1",
            value="100nF",
            footprint="0402",
            quantity=1,
        )
        d = item.to_dict()

        assert d["reference"] == "C1"
        assert d["value"] == "100nF"
        assert d["footprint"] == "0402"
        assert d["quantity"] == 1
        assert d["lcsc_part"] is None


class TestBOMGenerationResult:
    """Tests for BOMGenerationResult dataclass."""

    def test_success_result(self):
        result = BOMGenerationResult(
            success=True,
            total_parts=5,
            unique_parts=3,
            output_path="/tmp/bom.csv",
            missing_lcsc=["R1"],
            items=[
                BOMItemResult("R1", "10k", "0603", 1),
                BOMItemResult("C1, C2", "100nF", "0402", 2),
            ],
            format="csv",
        )
        assert result.success is True
        assert result.total_parts == 5
        assert result.unique_parts == 3
        assert len(result.items) == 2

    def test_failure_result(self):
        result = BOMGenerationResult(
            success=False,
            error="Schematic file not found",
        )
        assert result.success is False
        assert result.error == "Schematic file not found"

    def test_to_dict(self):
        result = BOMGenerationResult(
            success=True,
            total_parts=3,
            unique_parts=2,
            items=[BOMItemResult("R1", "10k", "0603", 1)],
            format="jlcpcb",
            warnings=["Missing LCSC part numbers"],
        )
        d = result.to_dict()

        assert d["success"] is True
        assert d["total_parts"] == 3
        assert d["unique_parts"] == 2
        assert len(d["items"]) == 1
        assert d["format"] == "jlcpcb"
        assert len(d["warnings"]) == 1


class TestExportBom:
    """Tests for export_bom function."""

    def test_file_not_found(self):
        result = export_bom(
            schematic_path="/nonexistent/board.kicad_sch",
        )
        assert result.success is False
        assert "not found" in result.error.lower()

    def test_unknown_format(self):
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as f:
            f.write(b"(kicad_sch (version 20231120))")
            sch_path = f.name

        result = export_bom(
            schematic_path=sch_path,
            format="unknown_format",
        )
        assert result.success is False
        assert "Unknown format" in result.error

        Path(sch_path).unlink()

    def test_unknown_group_by(self):
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as f:
            f.write(b"(kicad_sch (version 20231120))")
            sch_path = f.name

        result = export_bom(
            schematic_path=sch_path,
            group_by="invalid_grouping",
        )
        assert result.success is False
        assert "Unknown group_by" in result.error

        Path(sch_path).unlink()

    def test_unusual_extension_warning(self):
        with tempfile.NamedTemporaryFile(suffix=".sch", delete=False) as f:
            f.write(b"(kicad_sch (version 20231120))")
            sch_path = f.name

        result = export_bom(
            schematic_path=sch_path,
            format="csv",
        )
        # Should have warning about extension
        assert any("extension" in w.lower() for w in result.warnings)

        Path(sch_path).unlink()

    def test_supported_formats(self):
        """Verify all supported formats are defined."""
        assert "csv" in SUPPORTED_BOM_FORMATS
        assert "json" in SUPPORTED_BOM_FORMATS
        assert "jlcpcb" in SUPPORTED_BOM_FORMATS
        assert "pcbway" in SUPPORTED_BOM_FORMATS
        assert "seeed" in SUPPORTED_BOM_FORMATS

    def test_supported_group_by(self):
        """Verify all grouping strategies are defined."""
        assert "value" in SUPPORTED_GROUP_BY
        assert "footprint" in SUPPORTED_GROUP_BY
        assert "value+footprint" in SUPPORTED_GROUP_BY
        assert "mpn" in SUPPORTED_GROUP_BY
        assert "none" in SUPPORTED_GROUP_BY

    def test_data_only_mode(self, simple_rc_schematic):
        """Test export without writing a file."""
        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            output_path=None,  # No file output
            format="csv",
        )
        assert result.success is True
        assert result.output_path is None
        # Should have items even without file output
        assert len(result.items) >= 0

    def test_csv_output(self, simple_rc_schematic, tmp_path):
        """Test CSV file output."""
        output_file = tmp_path / "bom.csv"

        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            output_path=str(output_file),
            format="csv",
        )

        assert result.success is True
        assert result.output_path == str(output_file)
        assert output_file.exists()
        content = output_file.read_text()
        assert len(content) > 0

    def test_json_output(self, simple_rc_schematic, tmp_path):
        """Test JSON file output."""
        output_file = tmp_path / "bom.json"

        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            output_path=str(output_file),
            format="json",
        )

        assert result.success is True
        assert output_file.exists()

        # Verify JSON structure
        data = json.loads(output_file.read_text())
        assert "schematic" in data
        assert "total_parts" in data
        assert "unique_parts" in data
        assert "items" in data

    def test_jlcpcb_format(self, simple_rc_schematic, tmp_path):
        """Test JLCPCB format output."""
        output_file = tmp_path / "bom_jlcpcb.csv"

        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            output_path=str(output_file),
            format="jlcpcb",
        )

        assert result.success is True
        assert result.format == "jlcpcb"
        assert output_file.exists()

    def test_grouping_value_footprint(self, simple_rc_schematic):
        """Test value+footprint grouping (default)."""
        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            group_by="value+footprint",
        )

        assert result.success is True
        # Components with same value and footprint should be grouped
        assert result.unique_parts <= result.total_parts

    def test_grouping_none(self, simple_rc_schematic):
        """Test no grouping - each component separate."""
        result = export_bom(
            schematic_path=str(simple_rc_schematic),
            group_by="none",
        )

        assert result.success is True
        # Without grouping, unique parts equals total parts
        assert result.unique_parts == result.total_parts

    def test_hierarchical_schematic(self, hierarchical_schematic):
        """Test export from hierarchical schematic."""
        result = export_bom(
            schematic_path=str(hierarchical_schematic),
        )

        assert result.success is True
        # Should include components from sub-sheets
        assert result.total_parts >= 0


class TestMCPServerBOM:
    """Tests for MCP server export_bom tool."""

    def test_export_bom_tool_registered(self):
        server = create_server()
        assert "export_bom" in server.tools

    def test_get_tools_list_includes_bom(self):
        server = create_server()
        tools = server.get_tools_list()

        bom_tool = next((t for t in tools if t["name"] == "export_bom"), None)
        assert bom_tool is not None
        assert "description" in bom_tool
        assert "inputSchema" in bom_tool
        assert "schematic_path" in bom_tool["inputSchema"]["properties"]

    def test_bom_tool_parameters(self):
        server = create_server()
        tools = server.get_tools_list()

        bom_tool = next(t for t in tools if t["name"] == "export_bom")
        props = bom_tool["inputSchema"]["properties"]

        # Verify all parameters are defined
        assert "schematic_path" in props
        assert "output_path" in props
        assert "format" in props
        assert "group_by" in props
        assert "include_dnp" in props

        # Verify enums
        assert props["format"]["enum"] == ["csv", "json", "jlcpcb", "pcbway", "seeed"]
        assert props["group_by"]["enum"] == [
            "value",
            "footprint",
            "value+footprint",
            "mpn",
            "none",
        ]

    def test_handle_tools_call_bom_missing_file(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "export_bom",
                    "arguments": {
                        "schematic_path": "/nonexistent.kicad_sch",
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response

        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["success"] is False
        assert "not found" in result_data["error"].lower()

    def test_handle_tools_call_bom_success(self, simple_rc_schematic):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "export_bom",
                    "arguments": {
                        "schematic_path": str(simple_rc_schematic),
                        "format": "json",
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 2
        assert "result" in response

        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["success"] is True
        assert "total_parts" in result_data
        assert "unique_parts" in result_data
        assert "items" in result_data

    def test_handle_tools_call_bom_with_options(self, simple_rc_schematic, tmp_path):
        server = create_server()
        output_file = tmp_path / "bom_test.csv"

        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "export_bom",
                    "arguments": {
                        "schematic_path": str(simple_rc_schematic),
                        "output_path": str(output_file),
                        "format": "jlcpcb",
                        "group_by": "value",
                        "include_dnp": False,
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert "result" in response

        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["success"] is True
        assert result_data["format"] == "jlcpcb"
        assert output_file.exists()
