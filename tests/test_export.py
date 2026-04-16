"""Tests for the export module."""

import csv
import io
from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.exceptions import ConfigurationError
from kicad_tools.export.bom_formats import (
    BOMExportConfig,
    GenericBOMFormatter,
    JLCPCBBOMFormatter,
    PCBWayBOMFormatter,
    SeeedBOMFormatter,
    export_bom,
    get_bom_formatter,
)
from kicad_tools.export.pnp import (
    GenericPnPFormatter,
    JLCPCBPnPFormatter,
    PCBWayPnPFormatter,
    PlacementData,
    PnPExportConfig,
    export_pnp,
    extract_placements,
    get_aux_origin,
    get_pnp_formatter,
)


# Mock BOM item for testing
@dataclass
class MockBOMItem:
    reference: str
    value: str
    footprint: str
    quantity: int = 1
    lcsc: str = ""
    manufacturer: str = ""
    mfr_part: str = ""
    dnp: bool = False


# Mock Footprint for testing
@dataclass
class MockFootprint:
    reference: str
    value: str
    name: str
    position: tuple
    rotation: float
    layer: str
    exclude_from_pos_files: bool = False
    attr: str = "smd"
    dnp: bool = False


class TestBOMExportConfig:
    """Tests for BOMExportConfig."""

    def test_defaults(self):
        config = BOMExportConfig()
        assert config.include_dnp is False
        assert config.group_by_value is True
        assert config.include_lcsc is True
        assert config.include_mfr is True


class TestJLCPCBBOMFormatter:
    """Tests for JLCPCB BOM formatter."""

    @pytest.fixture
    def items(self) -> list[MockBOMItem]:
        return [
            MockBOMItem("R1", "10k", "0402", lcsc="C123456"),
            MockBOMItem("R2", "10k", "0402", lcsc="C123456"),
            MockBOMItem("C1", "100nF", "0402", lcsc="C456789"),
            MockBOMItem("U1", "STM32", "LQFP48", lcsc="C999999"),
        ]

    def test_headers(self):
        formatter = JLCPCBBOMFormatter()
        headers = formatter.get_headers()
        assert headers == ["Comment", "Designator", "Footprint", "LCSC Part #"]

    def test_format_grouped(self, items):
        formatter = JLCPCBBOMFormatter()
        output = formatter.format(items)

        reader = csv.reader(io.StringIO(output))
        rows = list(reader)

        # Header + 3 grouped rows (R1+R2, C1, U1)
        assert len(rows) == 4

        # Check header
        assert rows[0] == ["Comment", "Designator", "Footprint", "LCSC Part #"]

        # Check grouped resistors
        r_row = next(r for r in rows[1:] if "10k" in r[0])
        assert "R1" in r_row[1] and "R2" in r_row[1]
        assert r_row[3] == "C123456"

    def test_format_ungrouped(self, items):
        config = BOMExportConfig(group_by_value=False)
        formatter = JLCPCBBOMFormatter(config)
        output = formatter.format(items)

        reader = csv.reader(io.StringIO(output))
        rows = list(reader)

        # Header + 4 rows (R1+R2 still share key, C1, U1)
        # Note: Items with identical (value, footprint, lcsc) still get one row
        assert len(rows) == 4

    def test_filter_dnp(self, items):
        items.append(MockBOMItem("R3", "1k", "0402", dnp=True))

        # Without DNP
        formatter = JLCPCBBOMFormatter()
        output = formatter.format(items)
        assert "R3" not in output

        # With DNP
        config = BOMExportConfig(include_dnp=True)
        formatter = JLCPCBBOMFormatter(config)
        output = formatter.format(items)
        assert "R3" in output


class TestPCBWayBOMFormatter:
    """Tests for PCBWay BOM formatter."""

    @pytest.fixture
    def items(self) -> list[MockBOMItem]:
        return [
            MockBOMItem("R1", "10k", "0402", manufacturer="Yageo", mfr_part="RC0402"),
            MockBOMItem("R2", "10k", "0402", manufacturer="Yageo", mfr_part="RC0402"),
        ]

    def test_headers(self):
        formatter = PCBWayBOMFormatter()
        headers = formatter.get_headers()
        assert "Manufacturer" in headers
        assert "Mfr. Part #" in headers

    def test_format_includes_quantity(self, items):
        formatter = PCBWayBOMFormatter()
        output = formatter.format(items)

        reader = csv.reader(io.StringIO(output))
        rows = list(reader)

        # Find the row with resistors (grouped)
        data_row = rows[1]
        # Qty column should be 2 (R1 + R2)
        assert data_row[2] == "2"


class TestGenericBOMFormatter:
    """Tests for Generic BOM formatter."""

    def test_all_fields_included(self):
        config = BOMExportConfig(include_lcsc=True, include_mfr=True)
        formatter = GenericBOMFormatter(config)
        headers = formatter.get_headers()

        assert "LCSC" in headers
        assert "Manufacturer" in headers
        assert "MPN" in headers


class TestGetBOMFormatter:
    """Tests for get_bom_formatter function."""

    def test_get_jlcpcb(self):
        formatter = get_bom_formatter("jlcpcb")
        assert isinstance(formatter, JLCPCBBOMFormatter)

    def test_get_pcbway(self):
        formatter = get_bom_formatter("pcbway")
        assert isinstance(formatter, PCBWayBOMFormatter)

    def test_get_seeed(self):
        formatter = get_bom_formatter("seeed")
        assert isinstance(formatter, SeeedBOMFormatter)

    def test_get_generic(self):
        formatter = get_bom_formatter("generic")
        assert isinstance(formatter, GenericBOMFormatter)

    def test_case_insensitive(self):
        formatter = get_bom_formatter("JLCPCB")
        assert isinstance(formatter, JLCPCBBOMFormatter)

    def test_unknown_raises(self):
        with pytest.raises(ConfigurationError, match="Unknown manufacturer"):
            get_bom_formatter("unknown")


class TestExportBOM:
    """Tests for export_bom convenience function."""

    def test_export_bom(self):
        items = [MockBOMItem("R1", "10k", "0402", lcsc="C123")]
        output = export_bom(items, "jlcpcb")
        assert "R1" in output
        assert "C123" in output


class TestPlacementData:
    """Tests for PlacementData dataclass."""

    def test_creation(self):
        pd = PlacementData(
            reference="R1",
            value="10k",
            footprint="0402",
            x=10.5,
            y=20.3,
            rotation=90.0,
            layer="F.Cu",
        )
        assert pd.reference == "R1"
        assert pd.x == 10.5
        assert pd.rotation == 90.0


class TestPnPExportConfig:
    """Tests for PnPExportConfig."""

    def test_defaults(self):
        config = PnPExportConfig()
        assert config.x_offset == 0.0
        assert config.y_offset == 0.0
        assert config.mirror_x is False
        assert config.rotation_offset == 0.0


class TestJLCPCBPnPFormatter:
    """Tests for JLCPCB pick-and-place formatter."""

    @pytest.fixture
    def placements(self) -> list[PlacementData]:
        return [
            PlacementData("R1", "10k", "0402", 10.0, 20.0, 0.0, "F.Cu"),
            PlacementData("U1", "STM32", "LQFP48", 50.0, 50.0, 45.0, "F.Cu"),
            PlacementData("C1", "100nF", "0402", 15.0, 25.0, 180.0, "B.Cu"),
        ]

    def test_headers(self):
        formatter = JLCPCBPnPFormatter()
        headers = formatter.get_headers()
        assert headers == ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]

    def test_format_includes_all(self, placements):
        formatter = JLCPCBPnPFormatter()
        output = formatter.format(placements)

        assert "R1" in output
        assert "U1" in output
        assert "C1" in output

    def test_layer_capitalized_top_bottom(self, placements):
        formatter = JLCPCBPnPFormatter()
        output = formatter.format(placements)

        reader = csv.reader(io.StringIO(output))
        rows = list(reader)

        # JLCPCB expects capitalized "Top" and "Bottom" layer values
        layer_values = [row[-1] for row in rows[1:]]  # skip header
        assert "Top" in layer_values
        assert "Bottom" in layer_values

        # Ensure we do NOT have lowercase-only "top"/"bottom"
        # (The values must be exactly "Top" and "Bottom", not "top" and "bottom")
        for val in layer_values:
            assert val in ("Top", "Bottom"), f"Unexpected layer value: {val!r}"

    def test_filter_top_only(self, placements):
        config = PnPExportConfig(top_only=True)
        formatter = JLCPCBPnPFormatter(config)
        output = formatter.format(placements)

        assert "R1" in output
        assert "U1" in output
        assert "C1" not in output  # C1 is on bottom

    def test_filter_bottom_only(self, placements):
        config = PnPExportConfig(bottom_only=True)
        formatter = JLCPCBPnPFormatter(config)
        output = formatter.format(placements)

        assert "C1" in output
        assert "R1" not in output
        assert "U1" not in output


class TestPnPTransforms:
    """Tests for coordinate transforms."""

    def test_offset(self):
        config = PnPExportConfig(x_offset=5.0, y_offset=-3.0)
        formatter = JLCPCBPnPFormatter(config)

        placement = PlacementData("R1", "10k", "0402", 10.0, 20.0, 0.0, "F.Cu")
        transformed = formatter.apply_transforms(placement)

        assert transformed.x == 15.0
        assert transformed.y == 17.0

    def test_mirror(self):
        config = PnPExportConfig(mirror_x=True)
        formatter = JLCPCBPnPFormatter(config)

        placement = PlacementData("R1", "10k", "0402", 10.0, 20.0, 0.0, "F.Cu")
        transformed = formatter.apply_transforms(placement)

        assert transformed.x == -10.0
        assert transformed.y == 20.0

    def test_rotation_offset(self):
        config = PnPExportConfig(rotation_offset=90.0)
        formatter = JLCPCBPnPFormatter(config)

        placement = PlacementData("R1", "10k", "0402", 10.0, 20.0, 45.0, "F.Cu")
        transformed = formatter.apply_transforms(placement)

        assert transformed.rotation == 135.0

    def test_rotation_wraparound(self):
        config = PnPExportConfig(rotation_offset=180.0)
        formatter = JLCPCBPnPFormatter(config)

        placement = PlacementData("R1", "10k", "0402", 10.0, 20.0, 270.0, "F.Cu")
        transformed = formatter.apply_transforms(placement)

        assert transformed.rotation == 90.0


class TestGetPnPFormatter:
    """Tests for get_pnp_formatter function."""

    def test_get_jlcpcb(self):
        formatter = get_pnp_formatter("jlcpcb")
        assert isinstance(formatter, JLCPCBPnPFormatter)

    def test_get_pcbway(self):
        formatter = get_pnp_formatter("pcbway")
        assert isinstance(formatter, PCBWayPnPFormatter)

    def test_get_generic(self):
        formatter = get_pnp_formatter("generic")
        assert isinstance(formatter, GenericPnPFormatter)

    def test_unknown_raises(self):
        with pytest.raises(ConfigurationError, match="Unknown manufacturer"):
            get_pnp_formatter("unknown")


class TestExtractPlacements:
    """Tests for extract_placements function."""

    def test_extract_basic(self):
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
            MockFootprint("U1", "STM32", "LQFP48", (50.0, 50.0), 45.0, "F.Cu"),
        ]

        placements = extract_placements(footprints)

        assert len(placements) == 2
        assert placements[0].reference == "R1"
        assert placements[0].x == 10.0
        assert placements[1].reference == "U1"
        assert placements[1].rotation == 45.0

    def test_excludes_pos_excluded(self):
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
            MockFootprint(
                "MH1", "MountHole", "MH_3mm", (0.0, 0.0), 0.0, "F.Cu", exclude_from_pos_files=True
            ),
        ]

        placements = extract_placements(footprints)

        assert len(placements) == 1
        assert placements[0].reference == "R1"


class TestExportPnP:
    """Tests for export_pnp convenience function."""

    def test_export_pnp(self):
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
        ]

        output = export_pnp(footprints, "jlcpcb")

        assert "R1" in output
        assert "10k" in output


class TestSetupAuxAxisOrigin:
    """Tests for aux_axis_origin parsing in PCB Setup."""

    def test_aux_axis_origin_default(self):
        """Setup dataclass should default aux_axis_origin to (0, 0)."""
        from kicad_tools.schema.pcb import Setup

        setup = Setup()
        assert setup.aux_axis_origin == (0.0, 0.0)

    def test_aux_axis_origin_parsed_from_pcb(self):
        """Verify aux_axis_origin is parsed from the multilayer_zones fixture."""
        from kicad_tools.schema.pcb import PCB

        fixture = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"
        pcb = PCB.load(fixture)
        assert pcb.setup is not None
        # The fixture has (aux_axis_origin 0 0)
        assert pcb.setup.aux_axis_origin == (0.0, 0.0)


class TestGetAuxOrigin:
    """Tests for get_aux_origin helper function."""

    def test_get_aux_origin_from_fixture(self):
        """get_aux_origin should return the aux_axis_origin from the PCB file."""
        fixture = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"
        origin = get_aux_origin(fixture)
        assert origin == (0.0, 0.0)


class TestExportPnPWithAuxOrigin:
    """Tests for export_pnp with auxiliary origin auto-detection."""

    def test_export_pnp_without_pcb_path(self):
        """export_pnp without pcb_path should work as before."""
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
        ]
        output = export_pnp(footprints, "jlcpcb")
        assert "R1" in output
        assert "10.0000mm" in output
        assert "20.0000mm" in output

    def test_export_pnp_with_pcb_path_zero_origin(self):
        """export_pnp with pcb_path but (0,0) aux origin should not change coords."""
        fixture = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
        ]
        output = export_pnp(footprints, "jlcpcb", pcb_path=fixture)
        assert "10.0000mm" in output
        assert "20.0000mm" in output

    def test_export_pnp_use_aux_origin_disabled(self):
        """export_pnp with use_aux_origin=False should skip origin auto-detection."""
        fixture = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"
        config = PnPExportConfig(use_aux_origin=False)
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
        ]
        output = export_pnp(footprints, "jlcpcb", config=config, pcb_path=fixture)
        assert "10.0000mm" in output
        assert "20.0000mm" in output

    def test_export_pnp_manual_offset_combined(self):
        """Manual x_offset/y_offset should combine with aux origin offset."""
        # Even with zero aux origin, manual offsets should still be applied
        fixture = Path(__file__).parent / "fixtures" / "projects" / "multilayer_zones.kicad_pcb"
        config = PnPExportConfig(x_offset=5.0, y_offset=-3.0)
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu"),
        ]
        output = export_pnp(footprints, "jlcpcb", config=config, pcb_path=fixture)
        # 10 + 5 = 15, 20 + (-3) = 17
        assert "15.0000mm" in output
        assert "17.0000mm" in output


class TestGerberExporter:
    """Tests for GerberExporter (basic tests without kicad-cli)."""

    def test_find_kicad_cli_not_found(self):
        from kicad_tools.export.gerber import find_kicad_cli

        # This might find kicad-cli or not depending on environment
        # Just test it doesn't crash
        result = find_kicad_cli()
        assert result is None or isinstance(result, Path)

    def test_manufacturer_presets_exist(self):
        from kicad_tools.export.gerber import MANUFACTURER_PRESETS

        assert "jlcpcb" in MANUFACTURER_PRESETS
        assert "pcbway" in MANUFACTURER_PRESETS
        assert "oshpark" in MANUFACTURER_PRESETS


class TestAssemblyConfig:
    """Tests for AssemblyConfig."""

    def test_defaults(self):
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig()
        assert config.include_bom is True
        assert config.include_pnp is True
        assert config.include_gerbers is True

    def test_filename_templates(self):
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig()
        assert "{manufacturer}" in config.bom_filename
        assert "{manufacturer}" in config.pnp_filename


class TestAssemblyPackageResult:
    """Tests for AssemblyPackageResult."""

    def test_success_when_no_errors(self):
        from kicad_tools.export.assembly import AssemblyPackageResult

        result = AssemblyPackageResult(
            output_dir=Path("output"),
            bom_path=Path("output/bom.csv"),
            pnp_path=Path("output/cpl.csv"),
        )
        assert result.success is True

    def test_failure_when_errors(self):
        from kicad_tools.export.assembly import AssemblyPackageResult

        result = AssemblyPackageResult(
            output_dir=Path("output"),
            errors=["BOM generation failed"],
        )
        assert result.success is False

    def test_str_representation(self):
        from kicad_tools.export.assembly import AssemblyPackageResult

        result = AssemblyPackageResult(
            output_dir=Path("output"),
            bom_path=Path("output/bom.csv"),
        )
        output = str(result)
        assert "Assembly Package" in output
        assert "BOM" in output


class TestExtractPlacementsTHTFiltering:
    """Tests for THT filtering in extract_placements."""

    @pytest.fixture
    def mixed_footprints(self) -> list[MockFootprint]:
        """Board with SMD and THT components."""
        return [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint("C1", "100nF", "0402", (15.0, 25.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint(
                "J1", "Conn_01x04", "PinHeader_1x04", (50.0, 10.0), 0.0, "F.Cu", attr="through_hole"
            ),
            MockFootprint("U1", "STM32", "LQFP48", (30.0, 30.0), 45.0, "F.Cu", attr="smd"),
        ]

    def test_default_config_includes_tht(self, mixed_footprints):
        """Default PnPExportConfig does not exclude THT."""
        config = PnPExportConfig()
        placements = extract_placements(mixed_footprints, config)
        refs = {p.reference for p in placements}
        assert refs == {"R1", "C1", "J1", "U1"}

    def test_exclude_tht_filters_through_hole(self, mixed_footprints):
        """exclude_tht=True should remove through_hole footprints."""
        config = PnPExportConfig(exclude_tht=True)
        placements = extract_placements(mixed_footprints, config)
        refs = {p.reference for p in placements}
        assert refs == {"R1", "C1", "U1"}
        assert "J1" not in refs

    def test_exclude_tht_keeps_smd(self, mixed_footprints):
        """exclude_tht should not affect SMD components."""
        config = PnPExportConfig(exclude_tht=True)
        placements = extract_placements(mixed_footprints, config)
        assert len(placements) == 3

    def test_all_tht_board_produces_empty_cpl(self):
        """Board with only THT components + exclude_tht should produce empty list."""
        footprints = [
            MockFootprint(
                "J1", "Conn", "PinHeader", (10.0, 10.0), 0.0, "F.Cu", attr="through_hole"
            ),
            MockFootprint(
                "J2", "Conn", "PinHeader", (20.0, 10.0), 0.0, "F.Cu", attr="through_hole"
            ),
        ]
        config = PnPExportConfig(exclude_tht=True)
        placements = extract_placements(footprints, config)
        assert len(placements) == 0


class TestExtractPlacementsDNPFiltering:
    """Tests for DNP filtering in extract_placements."""

    def test_dnp_excluded_by_default(self):
        """DNP footprints should be excluded by default."""
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint("R2", "1k", "0402", (15.0, 25.0), 0.0, "F.Cu", attr="smd", dnp=True),
        ]
        placements = extract_placements(footprints)
        refs = {p.reference for p in placements}
        assert refs == {"R1"}

    def test_dnp_included_when_configured(self):
        """DNP footprints should be included when include_dnp=True."""
        footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint("R2", "1k", "0402", (15.0, 25.0), 0.0, "F.Cu", attr="smd", dnp=True),
        ]
        config = PnPExportConfig(include_dnp=True)
        placements = extract_placements(footprints, config)
        refs = {p.reference for p in placements}
        assert refs == {"R1", "R2"}


class TestJLCPCBPnPFormatterDefaults:
    """Tests for JLCPCB formatter default THT exclusion."""

    def test_jlcpcb_defaults_exclude_tht(self):
        """JLCPCB formatter should default to exclude_tht=True."""
        formatter = JLCPCBPnPFormatter()
        assert formatter.config.exclude_tht is True

    def test_jlcpcb_explicit_include_tht(self):
        """Passing exclude_tht=False should override JLCPCB default."""
        config = PnPExportConfig(exclude_tht=False)
        formatter = JLCPCBPnPFormatter(config)
        assert formatter.config.exclude_tht is False

    def test_generic_does_not_exclude_tht(self):
        """Generic formatter should include THT by default."""
        formatter = GenericPnPFormatter()
        assert formatter.config.exclude_tht is False

    def test_pcbway_does_not_exclude_tht(self):
        """PCBWay formatter should include THT by default."""
        formatter = PCBWayPnPFormatter()
        assert formatter.config.exclude_tht is False


class TestExportPnPTHTIntegration:
    """Integration tests for THT filtering through export_pnp."""

    @pytest.fixture
    def mixed_footprints(self) -> list[MockFootprint]:
        return [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint(
                "J1", "Conn", "PinHeader_1x04", (50.0, 10.0), 0.0, "F.Cu", attr="through_hole"
            ),
        ]

    def test_jlcpcb_excludes_tht(self, mixed_footprints):
        """JLCPCB export should exclude THT components by default."""
        output = export_pnp(mixed_footprints, "jlcpcb")
        assert "R1" in output
        assert "J1" not in output

    def test_jlcpcb_include_tht_override(self, mixed_footprints):
        """JLCPCB export with exclude_tht=False should include THT."""
        config = PnPExportConfig(exclude_tht=False)
        output = export_pnp(mixed_footprints, "jlcpcb", config=config)
        assert "R1" in output
        assert "J1" in output

    def test_generic_includes_tht(self, mixed_footprints):
        """Generic export should include THT components by default."""
        output = export_pnp(mixed_footprints, "generic")
        assert "R1" in output
        assert "J1" in output

    def test_pcbway_includes_tht(self, mixed_footprints):
        """PCBWay export should include THT components by default."""
        output = export_pnp(mixed_footprints, "pcbway")
        assert "R1" in output
        assert "J1" in output


class TestAssemblyPackageJLCPCBTHTIntegration:
    """Integration test: AssemblyPackage JLCPCB path excludes THT components."""

    def test_jlcpcb_assembly_package_excludes_tht(self, tmp_path):
        """AssemblyPackage with manufacturer='jlcpcb' and no explicit pnp_config
        should produce a CPL file that excludes through-hole components.

        This guards against a regression where AssemblyPackage._generate_pnp()
        created a bare PnPExportConfig (exclude_tht=False) instead of letting
        the JLCPCB formatter supply its own default (exclude_tht=True).
        """
        from unittest.mock import MagicMock, patch

        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        # Create a minimal PCB file so the constructor's existence check passes
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20221018))")

        config = AssemblyConfig(
            include_bom=False,
            include_gerbers=False,
            include_pnp=True,
            # Intentionally leave pnp_config=None to exercise the default path
        )

        pkg = AssemblyPackage(
            pcb_path=pcb_file,
            schematic_path=None,
            manufacturer="jlcpcb",
            config=config,
        )

        # Mock PCB.load to return footprints with mixed SMD and THT
        mock_pcb = MagicMock()
        mock_pcb.footprints = [
            MockFootprint("R1", "10k", "0402", (10.0, 20.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint("C1", "100nF", "0402", (15.0, 25.0), 0.0, "F.Cu", attr="smd"),
            MockFootprint(
                "J1", "Conn_01x04", "PinHeader_1x04", (50.0, 10.0), 0.0, "F.Cu", attr="through_hole"
            ),
        ]

        # Patch PCB at the schema module level; the lazy import in
        # _generate_pnp (``from ..schema.pcb import PCB``) resolves
        # against this module.
        with patch("kicad_tools.schema.pcb.PCB") as MockPCB:
            MockPCB.load.return_value = mock_pcb
            result = pkg.export(output_dir=tmp_path / "out")

        assert result.pnp_path is not None
        cpl_content = result.pnp_path.read_text()

        # SMD components must be present
        assert "R1" in cpl_content
        assert "C1" in cpl_content
        # THT connector must be excluded
        assert "J1" not in cpl_content
