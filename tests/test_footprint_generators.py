"""
Tests for parametric footprint generators.

Tests cover:
- SOIC, QFP, QFN, SOT, chip, DIP, and pin header generators
- IPC-7351 naming conventions
- Correct pad positions and dimensions
- .kicad_mod export format
"""

import tempfile
from pathlib import Path

import pytest

from kicad_tools.library import (
    Footprint,
    create_bga,
    create_bga_standard,
    create_chip,
    create_dfn,
    create_dfn_standard,
    create_dip,
    create_pin_header,
    create_qfn,
    create_qfp,
    create_soic,
    create_sot,
)

# =============================================================================
# Footprint Data Class Tests
# =============================================================================


class TestFootprint:
    """Tests for the Footprint data class."""

    def test_create_empty_footprint(self):
        """Test creating an empty footprint."""
        fp = Footprint(name="Test")
        assert fp.name == "Test"
        assert fp.pads == []
        assert fp.graphics == []

    def test_add_pad(self):
        """Test adding pads to a footprint."""
        fp = Footprint(name="Test")
        fp.add_pad("1", x=0, y=0, width=1.0, height=0.5)

        assert len(fp.pads) == 1
        assert fp.pads[0].name == "1"
        assert fp.pads[0].x == 0
        assert fp.pads[0].y == 0

    def test_add_line(self):
        """Test adding a line to a footprint."""
        fp = Footprint(name="Test")
        fp.add_line((0, 0), (1, 1), "F.SilkS", 0.12)

        assert len(fp.graphics) == 1
        assert fp.graphics[0].start_x == 0
        assert fp.graphics[0].end_x == 1

    def test_add_rect(self):
        """Test adding a rectangle to a footprint."""
        fp = Footprint(name="Test")
        fp.add_rect((-1, -1), (1, 1), "F.CrtYd", 0.05)

        assert len(fp.graphics) == 1
        assert fp.graphics[0].start_x == -1
        assert fp.graphics[0].end_x == 1

    def test_to_sexp(self):
        """Test S-expression export."""
        fp = Footprint(name="Test", description="Test footprint", tags=["test"])
        fp.add_pad("1", x=-0.5, y=0, width=0.6, height=0.4)
        fp.add_pad("2", x=0.5, y=0, width=0.6, height=0.4)

        sexp = fp.to_sexp()

        assert '(footprint "Test"' in sexp
        assert '(descr "Test footprint")' in sexp
        assert '(tags "test")' in sexp
        assert '(pad "1" smd roundrect' in sexp
        assert '(pad "2" smd roundrect' in sexp

    def test_save_footprint(self):
        """Test saving footprint to file."""
        fp = Footprint(name="Test")
        fp.add_pad("1", x=0, y=0, width=1.0, height=0.5)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "test.kicad_mod"
            fp.save(path)

            assert path.exists()
            content = path.read_text()
            assert '(footprint "Test"' in content


# =============================================================================
# SOIC Generator Tests
# =============================================================================


class TestSOICGenerator:
    """Tests for SOIC footprint generator."""

    def test_create_soic_8(self):
        """Test creating SOIC-8 with default dimensions."""
        fp = create_soic(pins=8)

        assert fp.name == "SOIC-8_3.9x4.9mm_P1.27mm"
        assert len(fp.pads) == 8
        assert fp.attr == "smd"

    def test_create_soic_16(self):
        """Test creating SOIC-16."""
        fp = create_soic(pins=16)

        assert "SOIC-16" in fp.name
        assert len(fp.pads) == 16

    def test_soic_pin_positions(self):
        """Test SOIC pad positions are correct."""
        fp = create_soic(pins=8, pitch=1.27)

        # Pins 1-4 on left, 5-8 on right
        left_pads = [p for p in fp.pads if p.x < 0]
        right_pads = [p for p in fp.pads if p.x > 0]

        assert len(left_pads) == 4
        assert len(right_pads) == 4

        # Check pitch between pins
        left_sorted = sorted(left_pads, key=lambda p: p.y)
        for i in range(len(left_sorted) - 1):
            pitch = left_sorted[i + 1].y - left_sorted[i].y
            assert abs(pitch - 1.27) < 0.01

    def test_soic_custom_dimensions(self):
        """Test SOIC with custom dimensions."""
        fp = create_soic(
            pins=8,
            pitch=1.27,
            body_width=4.0,
            body_length=5.0,
            pad_width=2.0,
            pad_height=0.65,
        )

        assert fp.pads[0].width == 2.0
        assert fp.pads[0].height == 0.65

    def test_soic_custom_name(self):
        """Test SOIC with custom name."""
        fp = create_soic(pins=8, name="MyCustomSOIC")

        assert fp.name == "MyCustomSOIC"

    def test_soic_invalid_pins(self):
        """Test SOIC with invalid pin count."""
        with pytest.raises(ValueError, match="even number"):
            create_soic(pins=7)

        with pytest.raises(ValueError, match="4-32"):
            create_soic(pins=64)


# =============================================================================
# QFP Generator Tests
# =============================================================================


class TestQFPGenerator:
    """Tests for QFP/LQFP footprint generator."""

    def test_create_lqfp_48(self):
        """Test creating LQFP-48."""
        fp = create_qfp(pins=48, pitch=0.5, body_size=7.0)

        assert "LQFP-48" in fp.name
        assert len(fp.pads) == 48

    def test_qfp_square_pin_count(self):
        """Test QFP with pins divisible by 4."""
        fp = create_qfp(pins=64, body_size=10.0)

        assert len(fp.pads) == 64
        # 16 pins per side
        bottom = [p for p in fp.pads if p.y > 4]
        assert len(bottom) == 16

    def test_qfp_rectangular(self):
        """Test rectangular QFP."""
        fp = create_qfp(
            pins=64,
            pitch=0.5,
            body_width=10.0,
            body_length=14.0,
            pins_x=16,
            pins_y=16,
        )

        assert len(fp.pads) == 64

    def test_qfp_invalid_square_pins(self):
        """Test QFP with pins not divisible by 4."""
        with pytest.raises(ValueError, match="divisible by 4"):
            create_qfp(pins=50, body_size=10.0)


# =============================================================================
# QFN Generator Tests
# =============================================================================


class TestQFNGenerator:
    """Tests for QFN footprint generator."""

    def test_create_qfn_16(self):
        """Test creating QFN-16."""
        fp = create_qfn(pins=16, pitch=0.5, body_size=3.0)

        assert "QFN-16" in fp.name
        # 16 signal pins, no EP by default if not in standards
        assert len(fp.pads) >= 16

    def test_qfn_with_exposed_pad(self):
        """Test QFN with exposed thermal pad."""
        fp = create_qfn(pins=16, body_size=3.0, exposed_pad=1.7)

        assert "_EP" in fp.name
        assert len(fp.pads) == 17  # 16 + exposed pad

        # Find the exposed pad (should be center, larger)
        ep = [p for p in fp.pads if p.name == "17"][0]
        assert ep.x == 0
        assert ep.y == 0
        assert ep.width == 1.7

    def test_qfn_pad_positions(self):
        """Test QFN pad positions around perimeter."""
        fp = create_qfn(pins=16, body_size=4.0)

        # 4 pins per side
        for i in range(1, 17):
            pad = [p for p in fp.pads if p.name == str(i)][0]
            # All pads should be near edges (not center)
            assert abs(pad.x) > 1.0 or abs(pad.y) > 1.0


# =============================================================================
# BGA Generator Tests
# =============================================================================


class TestBGAGenerator:
    """Tests for BGA footprint generator."""

    def test_create_bga_basic(self):
        """Test creating BGA with specified rows/cols/pitch."""
        fp = create_bga(rows=10, cols=10, pitch=0.8)

        assert "BGA-100" in fp.name
        assert len(fp.pads) == 100
        assert fp.attr == "smd"

    def test_bga_pin_naming(self):
        """Test BGA uses A1, A2, B1, B2... naming convention."""
        fp = create_bga(rows=3, cols=3, pitch=1.0)

        # Check first row has A prefix
        a1 = [p for p in fp.pads if p.name == "A1"]
        a2 = [p for p in fp.pads if p.name == "A2"]
        a3 = [p for p in fp.pads if p.name == "A3"]
        assert len(a1) == 1
        assert len(a2) == 1
        assert len(a3) == 1

        # Check second row has B prefix
        b1 = [p for p in fp.pads if p.name == "B1"]
        assert len(b1) == 1

        # Check third row has C prefix
        c1 = [p for p in fp.pads if p.name == "C1"]
        assert len(c1) == 1

    def test_bga_skips_i_and_o(self):
        """Test BGA skips I and O in row naming (avoid confusion with 1 and 0)."""
        # Create a BGA with enough rows to reach I and O
        fp = create_bga(rows=12, cols=2, pitch=1.0)

        # Row 9 should be J (skipping I)
        j1 = [p for p in fp.pads if p.name == "J1"]
        assert len(j1) == 1

        # Should not have any I or O prefixes
        i_pads = [p for p in fp.pads if p.name.startswith("I")]
        o_pads = [p for p in fp.pads if p.name.startswith("O")]
        assert len(i_pads) == 0
        assert len(o_pads) == 0

    def test_bga_depopulated_balls(self):
        """Test BGA with depopulated (missing) balls."""
        fp = create_bga(rows=3, cols=3, pitch=1.0, depopulated=["A1", "C3"])

        # Should have 9 - 2 = 7 pads
        assert len(fp.pads) == 7

        # A1 and C3 should not exist
        a1 = [p for p in fp.pads if p.name == "A1"]
        c3 = [p for p in fp.pads if p.name == "C3"]
        assert len(a1) == 0
        assert len(c3) == 0

    def test_bga_thermal_pad(self):
        """Test BGA with center thermal/ground pad."""
        fp = create_bga(rows=4, cols=4, pitch=1.0, thermal_pad=2.0)

        # 16 balls + 1 thermal pad = 17 pads
        assert len(fp.pads) == 17
        assert "_EP" in fp.name

        # Thermal pad should be at center with size 2.0
        ep = [p for p in fp.pads if p.name == "17"][0]
        assert ep.x == 0
        assert ep.y == 0
        assert ep.width == 2.0
        assert ep.height == 2.0

    def test_bga_pad_positions(self):
        """Test BGA ball positions match pitch and count."""
        fp = create_bga(rows=4, cols=4, pitch=1.0)

        # Get all pads
        pads = {p.name: p for p in fp.pads}

        # Check pitch between adjacent balls in same row
        a1 = pads["A1"]
        a2 = pads["A2"]
        assert abs(a2.x - a1.x - 1.0) < 0.01

        # Check pitch between adjacent balls in same column
        b1 = pads["B1"]
        assert abs(b1.y - a1.y - 1.0) < 0.01

    def test_bga_ipc7351_naming(self):
        """Test BGA auto-generated name format."""
        fp = create_bga(rows=10, cols=10, pitch=0.8, body_size=12.0)

        assert "BGA-100" in fp.name
        assert "12.0x12.0mm" in fp.name
        assert "P0.8mm" in fp.name

    def test_bga_invalid_params(self):
        """Test BGA error handling for invalid parameters."""
        with pytest.raises(ValueError, match="at least 1 row"):
            create_bga(rows=0, cols=5, pitch=0.8)

        with pytest.raises(ValueError, match="at least 1 column"):
            create_bga(rows=5, cols=0, pitch=0.8)

        with pytest.raises(ValueError, match="Pitch must be positive"):
            create_bga(rows=5, cols=5, pitch=-0.8)

    def test_bga_standard_packages(self):
        """Test creating BGA from standard package presets."""
        fp = create_bga_standard("BGA-256_17x17_0.8mm")

        assert fp.name == "BGA-256_17x17_0.8mm"
        assert len(fp.pads) == 256

    def test_bga_standard_invalid(self):
        """Test error for unknown standard package."""
        with pytest.raises(ValueError, match="Unknown BGA package"):
            create_bga_standard("BGA-INVALID")

    def test_bga_save_kicad_mod(self):
        """Test BGA produces valid .kicad_mod file."""
        fp = create_bga(rows=4, cols=4, pitch=1.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "BGA-16.kicad_mod"
            fp.save(path)

            assert path.exists()
            content = path.read_text()
            assert "(footprint" in content
            assert "(pad" in content

    def test_bga_custom_name(self):
        """Test BGA with custom name."""
        fp = create_bga(rows=4, cols=4, pitch=1.0, name="MyCustomBGA")

        assert fp.name == "MyCustomBGA"


# =============================================================================
# DFN Generator Tests
# =============================================================================


class TestDFNGenerator:
    """Tests for DFN footprint generator."""

    def test_create_dfn_basic(self):
        """Test creating DFN with pins/pitch/body size."""
        fp = create_dfn(pins=8, pitch=0.5, body_width=3.0, body_length=3.0)

        assert "DFN-8" in fp.name
        assert len(fp.pads) == 8
        assert fp.attr == "smd"

    def test_dfn_exposed_pad_tuple(self):
        """Test DFN with exposed thermal pad (tuple size)."""
        fp = create_dfn(pins=8, body_width=3.0, body_length=3.0, exposed_pad=(1.5, 2.0))

        # 8 pins + 1 exposed pad = 9 pads
        assert len(fp.pads) == 9
        assert "_EP" in fp.name

        # Exposed pad should be at center
        ep = [p for p in fp.pads if p.name == "9"][0]
        assert ep.x == 0
        assert ep.y == 0
        assert ep.width == 1.5
        assert ep.height == 2.0

    def test_dfn_exposed_pad_float(self):
        """Test DFN with exposed thermal pad (square, float size)."""
        fp = create_dfn(pins=6, body_width=2.0, body_length=2.0, exposed_pad=1.0)

        # 6 pins + 1 exposed pad = 7 pads
        assert len(fp.pads) == 7

        ep = [p for p in fp.pads if p.name == "7"][0]
        assert ep.width == 1.0
        assert ep.height == 1.0

    def test_dfn_exposed_pad_auto(self):
        """Test DFN with auto-calculated exposed pad."""
        fp = create_dfn(pins=8, body_width=3.0, body_length=3.0, exposed_pad=True)

        # Should have exposed pad
        assert len(fp.pads) == 9
        assert "_EP" in fp.name

    def test_dfn_no_exposed_pad(self):
        """Test DFN without exposed pad."""
        fp = create_dfn(pins=8, body_width=3.0, body_length=3.0, exposed_pad=None)

        assert len(fp.pads) == 8
        assert "_EP" not in fp.name

    def test_dfn_wettable_flanks(self):
        """Test DFN with wettable flanks option."""
        fp_normal = create_dfn(pins=8, body_width=3.0, body_length=3.0, wettable_flanks=False)
        fp_wf = create_dfn(pins=8, body_width=3.0, body_length=3.0, wettable_flanks=True)

        # Wettable flank pads should be larger
        normal_pad = fp_normal.pads[0]
        wf_pad = fp_wf.pads[0]

        assert wf_pad.width > normal_pad.width
        assert "_WF" in fp_wf.name

    def test_dfn_pad_positions(self):
        """Test DFN pads are on correct sides (top and bottom)."""
        fp = create_dfn(pins=8, pitch=0.5, body_width=3.0, body_length=3.0)

        # 4 pads on bottom (positive y), 4 on top (negative y)
        bottom_pads = [p for p in fp.pads if p.y > 0]
        top_pads = [p for p in fp.pads if p.y < 0]

        assert len(bottom_pads) == 4
        assert len(top_pads) == 4

    def test_dfn_ipc7351_naming(self):
        """Test DFN auto-generated name format."""
        fp = create_dfn(pins=8, pitch=0.5, body_width=3.0, body_length=3.0)

        assert "DFN-8" in fp.name
        assert "3.0x3.0mm" in fp.name
        assert "P0.5mm" in fp.name

    def test_dfn_invalid_pins(self):
        """Test DFN error handling for odd pin count."""
        with pytest.raises(ValueError, match="even number"):
            create_dfn(pins=7, body_width=3.0, body_length=3.0)

        with pytest.raises(ValueError, match="at least 2 pins"):
            create_dfn(pins=1, body_width=3.0, body_length=3.0)

    def test_dfn_standard_packages(self):
        """Test creating DFN from standard package presets."""
        fp = create_dfn_standard("DFN-8_3x3_0.5mm")

        assert fp.name == "DFN-8_3x3_0.5mm"
        # 8 pins + exposed pad
        assert len(fp.pads) == 9

    def test_dfn_standard_invalid(self):
        """Test error for unknown standard package."""
        with pytest.raises(ValueError, match="Unknown DFN package"):
            create_dfn_standard("DFN-INVALID")

    def test_dfn_save_kicad_mod(self):
        """Test DFN produces valid .kicad_mod file."""
        fp = create_dfn(pins=8, body_width=3.0, body_length=3.0)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "DFN-8.kicad_mod"
            fp.save(path)

            assert path.exists()
            content = path.read_text()
            assert "(footprint" in content
            assert "(pad" in content

    def test_dfn_custom_name(self):
        """Test DFN with custom name."""
        fp = create_dfn(pins=8, body_width=3.0, body_length=3.0, name="MyCustomDFN")

        assert fp.name == "MyCustomDFN"


# =============================================================================
# SOT Generator Tests
# =============================================================================


class TestSOTGenerator:
    """Tests for SOT footprint generator."""

    def test_create_sot23(self):
        """Test creating SOT-23."""
        fp = create_sot("SOT-23")

        assert fp.name == "SOT-23"
        assert len(fp.pads) == 3

    def test_create_sot23_5(self):
        """Test creating SOT-23-5."""
        fp = create_sot("SOT-23-5")

        assert fp.name == "SOT-23-5"
        assert len(fp.pads) == 5

    def test_create_sot23_6(self):
        """Test creating SOT-23-6."""
        fp = create_sot("SOT-23-6")

        assert fp.name == "SOT-23-6"
        assert len(fp.pads) == 6

    def test_create_sot223(self):
        """Test creating SOT-223."""
        fp = create_sot("SOT-223")

        assert fp.name == "SOT-223"
        assert len(fp.pads) == 4

        # Tab pad should be larger
        tab = [p for p in fp.pads if p.name == "4"][0]
        assert tab.width > 2.0  # Tab is larger

    def test_create_sot89(self):
        """Test creating SOT-89."""
        fp = create_sot("SOT-89")

        assert fp.name == "SOT-89"
        assert len(fp.pads) == 3

    def test_sot_invalid_variant(self):
        """Test SOT with invalid variant."""
        with pytest.raises(ValueError, match="Unknown SOT variant"):
            create_sot("SOT-999")


# =============================================================================
# Chip Component Generator Tests
# =============================================================================


class TestChipGenerator:
    """Tests for chip component footprint generator."""

    def test_create_chip_0603(self):
        """Test creating 0603 chip."""
        fp = create_chip("0603")

        assert "0603" in fp.name
        assert "1608" in fp.name  # Metric size
        assert len(fp.pads) == 2

    def test_create_chip_with_prefix(self):
        """Test creating chip with prefix."""
        fp = create_chip("0402", prefix="R")

        assert fp.name.startswith("R_")
        assert "0402" in fp.name

    def test_create_chip_metric_naming(self):
        """Test chip with metric naming."""
        fp = create_chip("0402", prefix="C", metric=True)

        assert "1005" in fp.name
        assert "0402" not in fp.name

    def test_chip_sizes(self):
        """Test various chip sizes."""
        sizes = ["0201", "0402", "0603", "0805", "1206"]

        for size in sizes:
            fp = create_chip(size)
            assert len(fp.pads) == 2
            # Larger sizes should have larger pads
            if size == "0201":
                assert fp.pads[0].height < 0.5
            elif size == "1206":
                assert fp.pads[0].height > 1.5

    def test_chip_invalid_size(self):
        """Test chip with invalid size."""
        with pytest.raises(ValueError, match="Unknown chip size"):
            create_chip("9999")


# =============================================================================
# DIP Generator Tests
# =============================================================================


class TestDIPGenerator:
    """Tests for DIP footprint generator."""

    def test_create_dip_8(self):
        """Test creating DIP-8."""
        fp = create_dip(pins=8)

        assert "DIP-8" in fp.name
        assert len(fp.pads) == 8
        assert fp.attr == "through_hole"

    def test_dip_wide(self):
        """Test creating wide DIP."""
        fp = create_dip(pins=28, row_spacing=15.24)

        assert "W15.24mm" in fp.name
        # Check row spacing
        left_pads = [p for p in fp.pads if p.x < 0]
        right_pads = [p for p in fp.pads if p.x > 0]
        spacing = right_pads[0].x - left_pads[0].x
        assert abs(spacing - 15.24) < 0.01

    def test_dip_pin_layout(self):
        """Test DIP pin layout."""
        fp = create_dip(pins=8)

        # Pins 1-4 on left, 5-8 on right
        pin1 = [p for p in fp.pads if p.name == "1"][0]
        pin8 = [p for p in fp.pads if p.name == "8"][0]

        assert pin1.x < 0  # Left side
        assert pin8.x > 0  # Right side

        # Pin 1 should be square
        assert pin1.shape == "rect"

    def test_dip_invalid_pins(self):
        """Test DIP with invalid pin count."""
        with pytest.raises(ValueError, match="even number"):
            create_dip(pins=7)


# =============================================================================
# Pin Header Generator Tests
# =============================================================================


class TestPinHeaderGenerator:
    """Tests for pin header footprint generator."""

    def test_create_single_row(self):
        """Test creating single row header."""
        fp = create_pin_header(pins=10, rows=1)

        assert "1x10" in fp.name
        assert len(fp.pads) == 10

    def test_create_dual_row(self):
        """Test creating dual row header."""
        fp = create_pin_header(pins=20, rows=2)

        assert "2x10" in fp.name
        assert len(fp.pads) == 20

    def test_header_pin1_square(self):
        """Test that pin 1 is square."""
        fp = create_pin_header(pins=4, rows=1)

        pin1 = [p for p in fp.pads if p.name == "1"][0]
        assert pin1.shape == "rect"

        # Other pins should be round
        pin2 = [p for p in fp.pads if p.name == "2"][0]
        assert pin2.shape == "circle"

    def test_header_invalid_rows(self):
        """Test header with invalid row count."""
        with pytest.raises(ValueError, match="Rows must be 1 or 2"):
            create_pin_header(pins=10, rows=3)


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for footprint generators."""

    def test_save_and_reload(self):
        """Test saving and verifying .kicad_mod content."""
        fp = create_soic(pins=8)

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "SOIC-8.kicad_mod"
            fp.save(path)

            content = path.read_text()

            # Verify key elements
            assert "(footprint" in content
            assert "(version" in content
            assert "(generator" in content
            assert "(layer" in content
            assert "(property" in content
            assert "(pad" in content
            assert "(fp_line" in content or "(fp_rect" in content

    def test_all_generators_produce_valid_output(self):
        """Test that all generators produce valid footprints."""
        footprints = [
            create_soic(pins=8),
            create_qfp(pins=48, body_size=7.0),
            create_qfn(pins=16, body_size=3.0),
            create_sot("SOT-23"),
            create_chip("0603"),
            create_dip(pins=8),
            create_pin_header(pins=6, rows=1),
            create_bga(rows=4, cols=4, pitch=1.0),
            create_dfn(pins=8, body_width=3.0, body_length=3.0),
        ]

        for fp in footprints:
            sexp = fp.to_sexp()

            # Basic structure checks
            assert sexp.startswith("(footprint")
            assert sexp.endswith(")")
            assert "(version" in sexp
            assert "(pad" in sexp

    def test_ipc7351_naming(self):
        """Test IPC-7351 naming convention compliance."""
        # SOIC
        soic = create_soic(pins=8, pitch=1.27, body_width=3.9, body_length=4.9)
        assert "SOIC-8" in soic.name
        assert "P1.27mm" in soic.name

        # Chip
        chip = create_chip("0603", prefix="R")
        assert "R_0603" in chip.name
        assert "Metric" in chip.name

        # QFN
        qfn = create_qfn(pins=16, body_size=3.0, exposed_pad=1.5)
        assert "QFN-16" in qfn.name
        assert "EP" in qfn.name

        # BGA
        bga = create_bga(rows=10, cols=10, pitch=0.8, body_size=12.0)
        assert "BGA-100" in bga.name
        assert "P0.8mm" in bga.name

        # DFN
        dfn = create_dfn(pins=8, pitch=0.5, body_width=3.0, body_length=3.0, exposed_pad=(1.5, 2.0))
        assert "DFN-8" in dfn.name
        assert "P0.5mm" in dfn.name
        assert "EP" in dfn.name


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_minimum_pin_counts(self):
        """Test minimum valid pin counts."""
        # These should work
        create_soic(pins=4)
        create_qfp(pins=8, body_size=5.0, pins_x=2, pins_y=2)
        create_qfn(pins=4, body_size=2.0)
        create_dip(pins=4)
        create_pin_header(pins=1, rows=1)

    def test_large_pin_counts(self):
        """Test large pin counts."""
        fp = create_qfp(pins=144, body_size=20.0)
        assert len(fp.pads) == 144

    def test_custom_names_preserved(self):
        """Test that custom names are preserved."""
        name = "MyCustomFootprint_v2"

        fp = create_soic(pins=8, name=name)
        assert fp.name == name

        fp = create_qfn(pins=16, body_size=3.0, name=name)
        assert fp.name == name

    def test_fluent_api(self):
        """Test fluent API for adding elements."""
        fp = Footprint(name="Test")
        result = fp.add_pad("1", 0, 0, 1, 1).add_line((0, 0), (1, 1)).add_rect((-1, -1), (1, 1))

        assert result is fp
        assert len(fp.pads) == 1
        assert len(fp.graphics) == 2
