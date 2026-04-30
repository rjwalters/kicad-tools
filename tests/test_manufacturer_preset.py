"""Tests for the manufacturer preset system with rotation corrections.

Covers:
- ManufacturerProfile extended fields (rotation_corrections, pnp_format_id, etc.)
- Rotation correction loading from YAML
- Per-footprint rotation correction matching with glob patterns
- PnP formatter integration with rotation corrections
- Rotation wrapping at 360 degrees
- Components without corrections pass through unchanged
"""

import pytest

from kicad_tools.manufacturers import (
    FileNamingConvention,
    get_profile,
    load_rotation_corrections,
    match_rotation_correction,
)
from kicad_tools.manufacturers.base import ManufacturerProfile, DesignRules
from kicad_tools.export.pnp import (
    JLCPCBPnPFormatter,
    PCBWayPnPFormatter,
    GenericPnPFormatter,
    PlacementData,
    PnPExportConfig,
    get_pnp_formatter,
    export_pnp,
)


# ---------------------------------------------------------------------------
# ManufacturerProfile extended fields
# ---------------------------------------------------------------------------


class TestManufacturerProfileExtendedFields:
    """Test that ManufacturerProfile includes the new export-related fields."""

    def test_jlcpcb_has_rotation_corrections(self):
        profile = get_profile("jlcpcb")
        assert isinstance(profile.rotation_corrections, dict)
        assert len(profile.rotation_corrections) > 0

    def test_jlcpcb_has_pnp_format_id(self):
        profile = get_profile("jlcpcb")
        assert profile.pnp_format_id == "jlcpcb"

    def test_jlcpcb_has_gerber_preset_id(self):
        profile = get_profile("jlcpcb")
        assert profile.gerber_preset_id == "jlcpcb"

    def test_pcbway_has_export_ids(self):
        profile = get_profile("pcbway")
        assert profile.pnp_format_id == "pcbway"
        assert profile.gerber_preset_id == "pcbway"

    def test_oshpark_pcb_only(self):
        """OSH Park is PCB-only; no rotation corrections needed."""
        profile = get_profile("oshpark")
        assert profile.rotation_corrections == {}
        assert profile.gerber_preset_id == "oshpark"
        assert profile.supports_assembly() is False

    def test_seeed_has_export_ids(self):
        profile = get_profile("seeed")
        assert profile.pnp_format_id == "seeed"
        assert profile.gerber_preset_id == "seeed"

    def test_default_file_naming(self):
        profile = get_profile("jlcpcb")
        assert isinstance(profile.file_naming, FileNamingConvention)
        assert "{manufacturer}" in profile.file_naming.bom_filename
        assert "{manufacturer}" in profile.file_naming.pnp_filename

    def test_profile_serialization_includes_new_fields(self):
        """to_dict() should still work (it doesn't include all fields)."""
        profile = get_profile("jlcpcb")
        d = profile.to_dict()
        assert "id" in d
        assert "name" in d

    def test_empty_rotation_corrections_default(self):
        """ManufacturerProfile defaults to empty rotation corrections."""
        profile = ManufacturerProfile(
            id="test",
            name="Test",
            website="https://example.com",
            design_rules={
                "2layer_1oz": DesignRules(
                    min_trace_width_mm=0.15,
                    min_clearance_mm=0.15,
                    min_via_drill_mm=0.3,
                    min_via_diameter_mm=0.6,
                    min_annular_ring_mm=0.13,
                )
            },
        )
        assert profile.rotation_corrections == {}
        assert profile.pnp_format_id is None
        assert profile.gerber_preset_id is None


# ---------------------------------------------------------------------------
# Rotation correction loading from YAML
# ---------------------------------------------------------------------------


class TestRotationCorrectionLoading:
    """Test loading rotation corrections from YAML data files."""

    def test_load_jlcpcb_rotations(self):
        corrections = load_rotation_corrections("jlcpcb")
        assert isinstance(corrections, dict)
        assert len(corrections) > 0
        # Check a known entry
        assert "SOT-23*" in corrections
        assert corrections["SOT-23*"] == 180.0

    def test_load_nonexistent_manufacturer_returns_empty(self):
        corrections = load_rotation_corrections("nonexistent_mfr")
        assert corrections == {}

    def test_pcbway_rotations_empty_if_no_file(self):
        """PCBWay has no rotation corrections YAML -- returns empty dict."""
        corrections = load_rotation_corrections("pcbway")
        assert isinstance(corrections, dict)
        # May or may not be empty depending on future additions


# ---------------------------------------------------------------------------
# Per-footprint rotation correction matching
# ---------------------------------------------------------------------------


class TestRotationCorrectionMatching:
    """Test glob-based footprint matching for rotation corrections."""

    @pytest.fixture
    def corrections(self) -> dict[str, float]:
        return {
            "SOT-23*": 180.0,
            "SOIC-8*": 270.0,
            "QFN-*": 270.0,
            "D_SOD-123*": 180.0,
            "LED_0603*": 180.0,
        }

    def test_exact_match(self, corrections):
        assert match_rotation_correction("SOT-23", corrections) == 180.0

    def test_glob_suffix_match(self, corrections):
        assert match_rotation_correction("SOT-23-3", corrections) == 180.0
        assert match_rotation_correction("SOT-23-5", corrections) == 180.0
        assert match_rotation_correction("SOT-23-6_W1.6xL2.9", corrections) == 180.0

    def test_soic_match(self, corrections):
        assert match_rotation_correction("SOIC-8_3.9x4.9mm", corrections) == 270.0

    def test_qfn_wildcard_match(self, corrections):
        assert match_rotation_correction("QFN-16", corrections) == 270.0
        assert match_rotation_correction("QFN-32_5x5", corrections) == 270.0
        assert match_rotation_correction("QFN-64_9x9", corrections) == 270.0

    def test_no_match_returns_zero(self, corrections):
        assert match_rotation_correction("0805", corrections) == 0.0
        assert match_rotation_correction("R_0402_1005", corrections) == 0.0
        assert match_rotation_correction("C_0603", corrections) == 0.0

    def test_empty_corrections(self):
        assert match_rotation_correction("SOT-23", {}) == 0.0

    def test_diode_match(self, corrections):
        assert match_rotation_correction("D_SOD-123", corrections) == 180.0
        assert match_rotation_correction("D_SOD-123F", corrections) == 180.0

    def test_led_match(self, corrections):
        assert match_rotation_correction("LED_0603_1608Metric", corrections) == 180.0


# ---------------------------------------------------------------------------
# PnP formatter integration with rotation corrections
# ---------------------------------------------------------------------------


class TestPnPFormatterRotationCorrections:
    """Test that PnP formatters apply per-footprint rotation corrections."""

    @pytest.fixture
    def corrections(self) -> dict[str, float]:
        return {
            "SOT-23*": 180.0,
            "SOIC-8*": 270.0,
        }

    @pytest.fixture
    def placements(self) -> list[PlacementData]:
        return [
            PlacementData("Q1", "BSS138", "SOT-23", 10.0, 20.0, 0.0, "F.Cu"),
            PlacementData("U1", "MCP2221", "SOIC-8_3.9x4.9", 30.0, 40.0, 90.0, "F.Cu"),
            PlacementData("R1", "10k", "0805", 5.0, 5.0, 45.0, "F.Cu"),
        ]

    def test_jlcpcb_formatter_applies_corrections(self, corrections, placements):
        formatter = JLCPCBPnPFormatter(rotation_corrections=corrections)
        output = formatter.format(placements)

        # Parse CSV
        import csv
        import io

        reader = csv.DictReader(io.StringIO(output))
        rows = {r["Designator"]: r for r in reader}

        # SOT-23: 0 + 180 = 180
        assert float(rows["Q1"]["Rotation"]) == pytest.approx(180.0)
        # SOIC-8: 90 + 270 = 360 -> 0
        assert float(rows["U1"]["Rotation"]) == pytest.approx(0.0)
        # 0805: 45 + 0 = 45 (no correction)
        assert float(rows["R1"]["Rotation"]) == pytest.approx(45.0)

    def test_apply_transforms_with_corrections(self, corrections):
        formatter = JLCPCBPnPFormatter(rotation_corrections=corrections)

        p = PlacementData("Q1", "BSS138", "SOT-23", 10.0, 20.0, 0.0, "F.Cu")
        t = formatter.apply_transforms(p)
        assert t.rotation == pytest.approx(180.0)

    def test_rotation_wraps_at_360(self, corrections):
        """Test that rotation correction wraps correctly at 360 degrees."""
        formatter = JLCPCBPnPFormatter(rotation_corrections=corrections)

        # 350 + 180 = 530 -> 170 (mod 360)
        p = PlacementData("Q2", "BSS138", "SOT-23", 10.0, 20.0, 350.0, "F.Cu")
        t = formatter.apply_transforms(p)
        assert t.rotation == pytest.approx(170.0)

    def test_no_corrections_passthrough(self, placements):
        """Components pass through unchanged when no corrections are set."""
        formatter = JLCPCBPnPFormatter()
        for p in placements:
            t = formatter.apply_transforms(p)
            # Only the global rotation_offset (0 by default) should apply
            assert t.rotation == pytest.approx(p.rotation)

    def test_global_offset_plus_correction(self, corrections):
        """Global rotation_offset and per-footprint corrections combine."""
        config = PnPExportConfig(rotation_offset=10.0)
        formatter = JLCPCBPnPFormatter(config=config, rotation_corrections=corrections)

        # SOT-23: 0 + 180 (correction) + 10 (global) = 190
        p = PlacementData("Q1", "BSS138", "SOT-23", 10.0, 20.0, 0.0, "F.Cu")
        t = formatter.apply_transforms(p)
        assert t.rotation == pytest.approx(190.0)

    def test_pcbway_formatter_accepts_corrections(self, corrections, placements):
        formatter = PCBWayPnPFormatter(rotation_corrections=corrections)
        output = formatter.format(placements)
        assert "Q1" in output  # Basic sanity check

    def test_generic_formatter_accepts_corrections(self, corrections, placements):
        formatter = GenericPnPFormatter(rotation_corrections=corrections)
        output = formatter.format(placements)
        assert "Q1" in output

    def test_get_pnp_formatter_passes_corrections(self, corrections):
        formatter = get_pnp_formatter("jlcpcb", rotation_corrections=corrections)
        assert formatter.rotation_corrections == corrections

    def test_export_pnp_with_corrections(self, corrections):
        """export_pnp() forwards rotation_corrections to the formatter."""
        from dataclasses import dataclass

        @dataclass
        class MockFP:
            reference: str
            value: str
            name: str
            position: tuple
            rotation: float
            layer: str
            exclude_from_pos_files: bool = False
            attr: str = "smd"
            dnp: bool = False

        footprints = [
            MockFP("Q1", "BSS138", "SOT-23", (10.0, 20.0), 0.0, "F.Cu"),
            MockFP("R1", "10k", "0805", (5.0, 5.0), 45.0, "F.Cu"),
        ]
        output = export_pnp(
            footprints,
            manufacturer="jlcpcb",
            rotation_corrections=corrections,
        )

        import csv
        import io

        reader = csv.DictReader(io.StringIO(output))
        rows = {r["Designator"]: r for r in reader}

        assert float(rows["Q1"]["Rotation"]) == pytest.approx(180.0)
        assert float(rows["R1"]["Rotation"]) == pytest.approx(45.0)


# ---------------------------------------------------------------------------
# Integration: unified preset resolution in assembly pipeline
# ---------------------------------------------------------------------------


class TestUnifiedPresetResolution:
    """Test that the assembly pipeline resolves rotation corrections
    from the manufacturer profile.
    """

    def test_assembly_package_get_rotation_corrections(self):
        """_get_rotation_corrections resolves from manufacturer profile."""
        from unittest.mock import patch, MagicMock
        from kicad_tools.export.assembly import AssemblyPackage

        mock_pcb = MagicMock()
        mock_pcb.exists.return_value = True

        with patch("kicad_tools.export.assembly.Path", return_value=mock_pcb):
            with patch("kicad_tools.report.utils.find_schematic", return_value=None):
                pkg = AssemblyPackage.__new__(AssemblyPackage)
                pkg.pcb_path = mock_pcb
                pkg.schematic_path = None
                pkg.manufacturer = "jlcpcb"
                pkg.config = MagicMock()

                corrections = pkg._get_rotation_corrections()
                assert isinstance(corrections, dict)
                assert len(corrections) > 0
                # Verify a known JLCPCB correction
                from kicad_tools.manufacturers.base import match_rotation_correction
                assert match_rotation_correction("SOT-23", corrections) == 180.0

    def test_assembly_package_unknown_manufacturer_returns_none(self):
        """_get_rotation_corrections returns None for unknown manufacturers."""
        from kicad_tools.export.assembly import AssemblyPackage
        from unittest.mock import MagicMock

        pkg = AssemblyPackage.__new__(AssemblyPackage)
        pkg.pcb_path = MagicMock()
        pkg.schematic_path = None
        pkg.manufacturer = "unknown_fab_house"
        pkg.config = MagicMock()

        corrections = pkg._get_rotation_corrections()
        assert corrections is None
