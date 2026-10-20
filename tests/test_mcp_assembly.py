"""Tests for MCP assembly export tools."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        # Same ids as ``kct export --mfr`` (#7276), tier ids included
        assert "jlcpcb-tier1" in ASSEMBLY_MANUFACTURERS


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
        """An omitted manufacturer has no hard default: it resolves from target_fab (#7276)."""
        server = create_server()
        tools = server.get_tools_list()

        assembly_tool = next(t for t in tools if t["name"] == "export_assembly")
        manufacturer_prop = assembly_tool["inputSchema"]["properties"]["manufacturer"]

        assert "default" not in manufacturer_prop
        assert "jlcpcb-tier1" in manufacturer_prop["enum"]


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

        from kicad_tools.export.assembly import AssemblyPackageResult
        from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult

        pcb = tmp_path / "rot.kicad_pcb"
        pcb.write_text(ROTATION_PCB)
        sch = tmp_path / "rot.kicad_sch"
        sch.write_text("(kicad_sch (version 20231014))")
        out = tmp_path / "out"
        out.mkdir()
        pnp = out / "cpl.csv"
        pnp.write_text("Designator,Layer\nU1,top\n")

        fake = AssemblyPackageResult(
            output_dir=out,
            pnp_path=pnp,
            rotation_corrections_applied=2,
            pnp_placement_count=1,
            pnp_layers=["top"],
        )
        package = ManufacturingResult(output_dir=out, assembly_result=fake)
        with patch.object(ManufacturingPackage, "export", return_value=package):
            result = export_assembly(str(pcb), str(sch), str(out), manufacturer="jlcpcb")
        assert result.pnp is not None
        assert result.pnp.rotation_corrections == 2
        assert result.pnp.component_count == 1
        assert result.pnp.layers == ["top"]
        assert "cost_estimate" not in result.to_dict()


# =============================================================================
# Issue #7260: export_assembly runs kct export's pipeline (preflight, strict)
# =============================================================================

BOARD_00 = Path(__file__).resolve().parent.parent / "boards" / "00-simple-led" / "output"


def _board_00_copy(dest: Path, *, with_reports: bool = True) -> tuple[Path, Path]:
    """Scratch copy of board 00 -- never export from ``boards/`` (#5746 sidecars)."""
    if not BOARD_00.is_dir():
        pytest.skip("boards/00-simple-led/output not present")
    dest.mkdir(parents=True, exist_ok=True)
    names = [
        "simple_led_routed.kicad_pcb",
        "simple_led_routed.kicad_pro",
        "simple_led_routed.kicad_dru",
        "simple_led.kicad_sch",
        "simple_led.kicad_pro",
    ]
    for name in names:
        shutil.copy2(BOARD_00 / name, dest / name)
    pcb, sch = dest / "simple_led_routed.kicad_pcb", dest / "simple_led.kicad_sch"
    if with_reports:
        _write_clean_reports(pcb, sch)
    return pcb, sch


def _write_clean_reports(pcb: Path, sch: Path) -> None:
    """Write clean DRC/ERC reports beside the scratch copy, hash-bound to it.

    Board 00's committed reports were deleted as producer-less in #7190, so
    the fixture writes its own instead of copying them out of ``boards/`` (#7299).
    """
    from kicad_tools.drc.report_binding import DRC_HASH_KEY, ERC_HASH_KEY, sha256_file

    drc = {
        "file": str(pcb),
        "manufacturer": "jlcpcb",
        "layers": 2,
        DRC_HASH_KEY: sha256_file(pcb),
        "summary": {
            "errors": 0,
            "warnings": 0,
            "infos": 0,
            "rules_checked": 32,
            "passed": True,
        },
        "violations": [],
    }
    erc = {
        "$schema": "https://schemas.kicad.org/erc.v1.json",
        "coordinate_units": "mm",
        "source": sch.name,
        ERC_HASH_KEY: sha256_file(sch),
        "sheets": [{"path": "/", "violations": []}],
    }
    (pcb.parent / "drc_report.json").write_text(json.dumps(drc, indent=2))
    (pcb.parent / "erc_report.json").write_text(json.dumps(erc, indent=2))


def _package_files(out: Path) -> list[str]:
    if not out.exists():
        return []
    return sorted(p.name for p in out.iterdir())


class TestExportAssemblyPreflight7260:
    @pytest.mark.parametrize("strict", [True, False])
    def test_r1_removed_is_refused(self, tmp_path: Path, strict: bool):
        """Repro 1: BOM lists R1, PCB does not -> unbuildable, refused either way."""
        from kicad_tools.cli.pcb_remove_footprint import run_remove_footprint

        pcb, sch = _board_00_copy(tmp_path / "board")
        assert run_remove_footprint(pcb, "R1", force=True) == 0
        out = tmp_path / "out"

        result = export_assembly(
            str(pcb), str(sch), str(out), manufacturer="jlcpcb", strict_preflight=strict
        )

        assert result.success is False
        assert result.error is not None
        assert "bom_pcb_match" in result.error and "R1" in result.error
        assert any(p["name"] == "bom_pcb_match" and p["status"] == "FAIL" for p in result.preflight)
        assert result.zip_file is None
        files = _package_files(out)
        assert not [f for f in files if f.startswith(("bom_", "cpl_")) or f.endswith(".zip")]

    def test_clean_board_exports(self, tmp_path: Path):
        """No DRC/ERC report on disk is a WARN: never blocks, even under strict."""
        pcb, sch = _board_00_copy(tmp_path / "board", with_reports=False)
        out = tmp_path / "out"

        result = export_assembly(str(pcb), str(sch), str(out), manufacturer="generic")

        assert result.success is True, result.error
        assert result.error is None
        assert result.gerbers is not None and result.gerbers.layer_count >= 2
        assert result.bom is not None and result.bom.component_count == 3
        assert result.pnp is not None and result.pnp.component_count >= 1
        assert result.zip_file is not None and Path(result.zip_file).exists()
        statuses = {p["name"]: p["status"] for p in result.preflight}
        assert statuses.get("drc") == "WARN"
        assert "FAIL" not in statuses.values()
        # kct export's report PDF and project zip are off for MCP.
        assert "kicad_project.zip" not in _package_files(out)
        # Documented side effect (#5746): DRC constraints next to the board.
        assert pcb.with_suffix(".kicad_dru").exists()

    def _board_with_drc_error(self, tmp_path: Path) -> tuple[Path, Path]:
        """Board 00 whose DRC report records a (non-short) error: a plain preflight FAIL."""
        pcb, sch = _board_00_copy(tmp_path / "board")
        report_path = pcb.parent / "drc_report.json"
        report = json.loads(report_path.read_text())
        report["file"] = str(pcb)
        report["summary"]["errors"] = 1
        report["summary"]["passed"] = False
        report["violations"].append(
            {
                "rule_id": "dimension_trace_width",
                "type": "dimension_trace_width",
                "severity": "error",
                "message": "Trace width 0.110mm below minimum 0.127mm",
                "location": [148.0, 85.0],
                "layer": "F.Cu",
                "actual_value": 0.11,
                "required_value": 0.127,
                "items": [],
                "nets": [],
            }
        )
        report_path.write_text(json.dumps(report))
        return pcb, sch

    def test_preflight_fail_refused_by_default(self, tmp_path: Path):
        pcb, sch = self._board_with_drc_error(tmp_path)
        out = tmp_path / "out"

        result = export_assembly(str(pcb), str(sch), str(out), manufacturer="generic")

        assert result.success is False
        assert result.error is not None and "Preflight FAIL [drc]" in result.error
        assert any(p["name"] == "drc" and p["status"] == "FAIL" for p in result.preflight)
        assert result.zip_file is None
        assert not [f for f in _package_files(out) if f.endswith((".csv", ".zip"))]

    def test_strict_preflight_false_downgrades_to_warning(self, tmp_path: Path):
        pcb, sch = self._board_with_drc_error(tmp_path)
        out = tmp_path / "out"

        result = export_assembly(
            str(pcb), str(sch), str(out), manufacturer="generic", strict_preflight=False
        )

        assert result.success is True, result.error
        assert any("Preflight FAIL [drc]" in w for w in result.warnings)
        assert any(p["name"] == "drc" and p["status"] == "FAIL" for p in result.preflight)
        assert result.bom is not None and Path(result.bom.output_path).exists()
        assert result.zip_file is not None

    def test_mcp_server_default_is_strict(self, tmp_path: Path):
        pcb, sch = self._board_with_drc_error(tmp_path)
        server = create_server()
        out = server.call_tool(
            "export_assembly",
            {
                "pcb_path": str(pcb),
                "schematic_path": str(sch),
                "output_dir": str(tmp_path / "out"),
                "manufacturer": "generic",
            },
        )
        assert out["success"] is False
        assert out["zip_file"] is None
        assert any(p["status"] == "FAIL" for p in out["preflight"])

    def test_schema_documents_strict_preflight(self):
        server = create_server()
        tool = next(t for t in server.get_tools_list() if t["name"] == "export_assembly")
        prop = tool["inputSchema"]["properties"]["strict_preflight"]
        assert prop["type"] == "boolean"
        assert prop["default"] is True
        assert ".kicad_dru" in tool["description"]

    def test_mcp_export_does_not_parse_csv(self):
        """The BOM/CPL facts come from the generator, not CSV text (#7260)."""
        import inspect

        from kicad_tools.mcp.tools import export as export_mod

        source = inspect.getsource(export_mod.export_assembly)
        assert "read_text" not in source
        assert 'split(",")' not in source


class TestValidateAssemblyBomLcscValues7260:
    """``validate_assembly_bom`` reports kct export's bom_lcsc_values check."""

    def _run(self, tmp_path: Path, items, *, pcb: bool = True):
        from kicad_tools.export.preflight import PreflightChecker
        from kicad_tools.mcp.tools.export import validate_assembly_bom
        from kicad_tools.parts.cache import PartsCache
        from kicad_tools.parts.models import Part
        from kicad_tools.schema.bom import BOM

        sch = tmp_path / "board.kicad_sch"
        sch.write_text(
            "(kicad_sch (version 20230121) (generator eeschema)\n"
            '  (uuid "12345678-1234-1234-1234-123456789012")\n)'
        )
        if pcb:
            (tmp_path / "board.kicad_pcb").write_text("(kicad_pcb (version 20240108))")

        cache = PartsCache(db_path=tmp_path / "parts.db")
        cache.put(
            Part(
                lcsc_part="C1525",
                mfr_part="CL05B104KO5NNNC",
                manufacturer="Samsung Electro-Mechanics",
                description="16V 100nF X7R 0402 Multilayer Ceramic Capacitors MLCC",
                package="0402",
                value="100nF",
                is_basic=True,
            )
        )

        validation = MagicMock()
        validation.items = [object()]
        validation.to_dict.return_value = {"summary": {"assembly_ready": True}, "items": []}
        validator = MagicMock()
        validator.__enter__.return_value.validate_bom.return_value = validation

        with (
            patch("kicad_tools.assembly.validation.AssemblyValidator", return_value=validator),
            patch("kicad_tools.parts.cache.PartsCache", return_value=cache),
            patch.object(PreflightChecker, "_load_bom", return_value=BOM(items=items)),
        ):
            return validate_assembly_bom(str(sch))

    @staticmethod
    def _item(ref: str, value: str):
        from kicad_tools.schema.bom import BOMItem

        return BOMItem(
            reference=ref,
            value=value,
            footprint="Capacitor_SMD:C_0402_1005Metric",
            lib_id="Device:C",
            lcsc="C1525",
        )

    def test_value_mismatch_fails_and_blocks_readiness(self, tmp_path: Path):
        result = self._run(tmp_path, [self._item("C10", "16nF")])

        assert result["success"] is True
        assert result["bom_lcsc_values"]["status"] == "FAIL"
        assert "C1525" in result["bom_lcsc_values"]["details"]
        assert result["summary"]["assembly_ready"] is False

    def test_matching_part_keeps_readiness(self, tmp_path: Path):
        result = self._run(tmp_path, [self._item("C1", "100nF")])

        assert result["bom_lcsc_values"]["status"] == "OK"
        assert result["summary"]["assembly_ready"] is True

    def test_no_pcb_no_check(self, tmp_path: Path):
        result = self._run(tmp_path, [self._item("C10", "16nF")], pcb=False)

        assert "bom_lcsc_values" not in result
