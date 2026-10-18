"""Tests for MCP assembly export tools."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pydantic")

from kicad_tools.mcp.server import create_server
from kicad_tools.mcp.tools.export import (
    ASSEMBLY_MANUFACTURERS,
    export_assembly,
)
from kicad_tools.mcp.types import (
    AssemblyExportResult,
    BOMExportResult,
    PnPExportResult,
)


class TestBOMExportResult:
    """Tests for BOMExportResult dataclass."""

    def test_creation(self):
        result = BOMExportResult(
            output_path="/tmp/bom.csv",
            component_count=127,
            unique_parts=47,
            missing_lcsc=3,
        )
        assert result.output_path == "/tmp/bom.csv"
        assert result.component_count == 127
        assert result.unique_parts == 47
        assert result.missing_lcsc == 3

    def test_to_dict(self):
        result = BOMExportResult(
            output_path="/tmp/bom.csv",
            component_count=127,
            unique_parts=47,
            missing_lcsc=3,
        )
        d = result.to_dict()

        assert d["output_path"] == "/tmp/bom.csv"
        assert d["component_count"] == 127
        assert d["unique_parts"] == 47
        assert d["missing_lcsc"] == 3


class TestPnPExportResult:
    """Tests for PnPExportResult dataclass."""

    def test_creation(self):
        result = PnPExportResult(
            output_path="/tmp/cpl.csv",
            component_count=92,
            layers=["top", "bottom"],
            rotation_corrections=5,
        )
        assert result.output_path == "/tmp/cpl.csv"
        assert result.component_count == 92
        assert result.layers == ["top", "bottom"]
        assert result.rotation_corrections == 5

    def test_default_values(self):
        result = PnPExportResult(
            output_path="/tmp/cpl.csv",
            component_count=50,
        )
        assert result.layers == []
        assert result.rotation_corrections == 0

    def test_to_dict(self):
        result = PnPExportResult(
            output_path="/tmp/cpl.csv",
            component_count=92,
            layers=["top"],
            rotation_corrections=2,
        )
        d = result.to_dict()

        assert d["output_path"] == "/tmp/cpl.csv"
        assert d["component_count"] == 92
        assert d["layers"] == ["top"]
        assert d["rotation_corrections"] == 2


class TestAssemblyExportResult:
    """Tests for AssemblyExportResult dataclass."""

    def test_success_result(self):
        bom = BOMExportResult("/tmp/bom.csv", 100, 40, 2)
        pnp = PnPExportResult("/tmp/cpl.csv", 80, ["top"], 0)

        result = AssemblyExportResult(
            success=True,
            output_dir="/tmp/assembly",
            manufacturer="jlcpcb",
            bom=bom,
            pnp=pnp,
            zip_file="/tmp/assembly/board-jlcpcb-assembly.zip",
            warnings=["2 parts missing LCSC part numbers"],
        )
        assert result.success is True
        assert result.error is None
        assert result.bom.component_count == 100
        assert result.pnp.component_count == 80

    def test_failure_result(self):
        result = AssemblyExportResult(
            success=False,
            output_dir="/tmp/assembly",
            manufacturer="jlcpcb",
            error="PCB file not found",
        )
        assert result.success is False
        assert result.error == "PCB file not found"

    def test_to_dict(self):
        bom = BOMExportResult("/tmp/bom.csv", 100, 40, 0)
        result = AssemblyExportResult(
            success=True,
            output_dir="/tmp/assembly",
            manufacturer="jlcpcb",
            bom=bom,
            warnings=["Test warning"],
        )
        d = result.to_dict()

        assert d["success"] is True
        assert d["output_dir"] == "/tmp/assembly"
        assert d["manufacturer"] == "jlcpcb"
        assert d["bom"]["component_count"] == 100
        assert d["pnp"] is None
        assert d["gerbers"] is None
        assert d["warnings"] == ["Test warning"]


class TestExportAssembly:
    """Tests for export_assembly function."""

    def test_pcb_file_not_found(self):
        result = export_assembly(
            pcb_path="/nonexistent/board.kicad_pcb",
            schematic_path="/nonexistent/board.kicad_sch",
            output_dir="/tmp/assembly",
        )
        assert result.success is False
        assert "not found" in result.error.lower()
        assert "pcb" in result.error.lower()

    def test_schematic_file_not_found(self):
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as f:
            f.write(b"(kicad_pcb (version 20231014))")
            pcb_path = f.name

        result = export_assembly(
            pcb_path=pcb_path,
            schematic_path="/nonexistent/board.kicad_sch",
            output_dir="/tmp/assembly",
        )
        assert result.success is False
        assert "not found" in result.error.lower()
        assert "schematic" in result.error.lower()

        # Cleanup
        Path(pcb_path).unlink()

    def test_unknown_manufacturer(self):
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pcb_f:
            pcb_f.write(b"(kicad_pcb (version 20231014))")
            pcb_path = pcb_f.name

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sch_f:
            sch_f.write(b"(kicad_sch (version 20231014))")
            sch_path = sch_f.name

        result = export_assembly(
            pcb_path=pcb_path,
            schematic_path=sch_path,
            output_dir="/tmp/assembly",
            manufacturer="unknown_mfr",
        )
        assert result.success is False
        assert "Unknown manufacturer" in result.error

        # Cleanup
        Path(pcb_path).unlink()
        Path(sch_path).unlink()

    def test_unusual_extension_warning(self):
        with tempfile.NamedTemporaryFile(suffix=".pcb", delete=False) as pcb_f:
            pcb_f.write(b"(kicad_pcb (version 20231014))")
            pcb_path = pcb_f.name

        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sch_f:
            sch_f.write(b"(kicad_sch (version 20231014))")
            sch_path = sch_f.name

        result = export_assembly(
            pcb_path=pcb_path,
            schematic_path=sch_path,
            output_dir="/tmp/assembly",
            manufacturer="unknown_mfr",  # Will fail before exporter runs
        )
        # Should have warning about extension
        assert any("extension" in w.lower() for w in result.warnings)

        # Cleanup
        Path(pcb_path).unlink()
        Path(sch_path).unlink()

    def test_supported_manufacturers(self):
        """Verify all supported assembly manufacturers are valid."""
        assert "generic" in ASSEMBLY_MANUFACTURERS
        assert "jlcpcb" in ASSEMBLY_MANUFACTURERS
        assert "pcbway" in ASSEMBLY_MANUFACTURERS
        assert "seeed" in ASSEMBLY_MANUFACTURERS
        # oshpark is not in assembly manufacturers (gerber-only)
        assert "oshpark" not in ASSEMBLY_MANUFACTURERS


class TestMCPServerAssembly:
    """Tests for MCP server with assembly tool."""

    def test_create_server_has_assembly_tool(self):
        server = create_server()
        assert "export_assembly" in server.tools

    def test_get_tools_list_includes_assembly(self):
        server = create_server()
        tools = server.get_tools_list()

        assembly_tool = next((t for t in tools if t["name"] == "export_assembly"), None)
        assert assembly_tool is not None
        assert "description" in assembly_tool
        assert "inputSchema" in assembly_tool
        assert "pcb_path" in assembly_tool["inputSchema"]["properties"]
        assert "schematic_path" in assembly_tool["inputSchema"]["properties"]
        assert "output_dir" in assembly_tool["inputSchema"]["properties"]
        assert "manufacturer" in assembly_tool["inputSchema"]["properties"]

    def test_assembly_tool_required_params(self):
        server = create_server()
        tools = server.get_tools_list()

        assembly_tool = next(t for t in tools if t["name"] == "export_assembly")
        required = assembly_tool["inputSchema"]["required"]

        assert "pcb_path" in required
        assert "schematic_path" in required
        assert "output_dir" in required

    def test_assembly_tool_manufacturer_enum(self):
        server = create_server()
        tools = server.get_tools_list()

        assembly_tool = next(t for t in tools if t["name"] == "export_assembly")
        manufacturer_prop = assembly_tool["inputSchema"]["properties"]["manufacturer"]

        assert "enum" in manufacturer_prop
        assert "jlcpcb" in manufacturer_prop["enum"]
        assert "pcbway" in manufacturer_prop["enum"]
        assert "seeed" in manufacturer_prop["enum"]
        assert "generic" in manufacturer_prop["enum"]

    def test_handle_tools_call_assembly_missing_file(self):
        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "export_assembly",
                    "arguments": {
                        "pcb_path": "/nonexistent.kicad_pcb",
                        "schematic_path": "/nonexistent.kicad_sch",
                        "output_dir": "/tmp/out",
                    },
                },
            }
        )

        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response
        # Result should contain error info
        result_text = response["result"]["content"][0]["text"]
        result_data = json.loads(result_text)
        assert result_data["success"] is False
        assert "not found" in result_data["error"].lower()

    def test_handle_tools_call_assembly_default_manufacturer(self):
        """Test that default manufacturer is jlcpcb."""
        server = create_server()
        tools = server.get_tools_list()

        assembly_tool = next(t for t in tools if t["name"] == "export_assembly")
        manufacturer_prop = assembly_tool["inputSchema"]["properties"]["manufacturer"]

        assert manufacturer_prop.get("default") == "jlcpcb"


ROTATION_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (net 0 "")
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (at 150 150)
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "IC" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -2.7 0) (size 1.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
  (footprint "Package_TO_SOT_SMD:SOT-23"
    (layer "F.Cu")
    (at 160 150)
    (property "Reference" "Q1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "NPN" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -1 0) (size 0.8 0.9) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 170 150)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS"))
    (property "Value" "100nF" (at 0 0 0) (layer "F.Fab"))
    (pad "1" smd rect (at -0.5 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask"))
  )
)
"""


class TestRotationCorrectionCount:
    """rotation_corrections reports real counts, not a hardcoded 0 (#7107)."""

    @staticmethod
    def _count(tmp_path: Path, manufacturer: str):
        from kicad_tools.export.assembly import AssemblyPackage, AssemblyPackageResult

        pcb = tmp_path / "rot.kicad_pcb"
        pcb.write_text(ROTATION_PCB)
        sch = tmp_path / "rot.kicad_sch"
        sch.write_text("(kicad_sch (version 20231014))")
        out = tmp_path / "out"
        out.mkdir()
        package = AssemblyPackage(pcb, sch, manufacturer=manufacturer)
        result = AssemblyPackageResult(output_dir=out)
        package._generate_pnp(out, result)
        return result.rotation_corrections_applied

    def test_jlcpcb_counts_soic_and_sot23(self, tmp_path: Path):
        assert self._count(tmp_path, "jlcpcb") == 2

    def test_manufacturer_without_corrections_reports_zero(self, tmp_path: Path):
        assert self._count(tmp_path, "generic") == 0

    def test_mcp_export_surfaces_count(self, tmp_path: Path):
        from unittest.mock import patch

        from kicad_tools.export.assembly import AssemblyPackage, AssemblyPackageResult

        pcb = tmp_path / "rot.kicad_pcb"
        pcb.write_text(ROTATION_PCB)
        sch = tmp_path / "rot.kicad_sch"
        sch.write_text("(kicad_sch (version 20231014))")
        out = tmp_path / "out"
        out.mkdir()
        pnp = out / "cpl.csv"
        pnp.write_text("Designator,Layer\nU1,top\n")

        fake = AssemblyPackageResult(output_dir=out, pnp_path=pnp, rotation_corrections_applied=2)
        with patch.object(AssemblyPackage, "export", return_value=fake):
            result = export_assembly(str(pcb), str(sch), str(out), manufacturer="jlcpcb")
        assert result.pnp is not None
        assert result.pnp.rotation_corrections == 2
        assert "cost_estimate" not in result.to_dict()
