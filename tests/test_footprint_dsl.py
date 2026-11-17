"""Tests for the compressed footprint DSL parser."""

import pytest

from kicad_tools.library.dsl import parse_footprint_dsl
from kicad_tools.library.footprint import Footprint
from kicad_tools.library.generators import (
    create_bga,
    create_chip,
    create_dfn,
    create_dip,
    create_qfn,
    create_qfp,
    create_soic,
    create_sot,
)


class TestPassiveSizes:
    """Test parsing of passive/chip component DSL strings."""

    @pytest.mark.parametrize(
        "size",
        ["0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512"],
    )
    def test_all_chip_sizes(self, size: str):
        fp = parse_footprint_dsl(size)
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 2  # Chip passives have 2 pads

    def test_chip_matches_direct_generator(self):
        """Round-trip: DSL result matches direct generator call."""
        dsl_fp = parse_footprint_dsl("0402")
        direct_fp = create_chip(size="0402")
        assert dsl_fp.name == direct_fp.name
        assert len(dsl_fp.pads) == len(direct_fp.pads)

    def test_chip_0603_matches_direct(self):
        dsl_fp = parse_footprint_dsl("0603")
        direct_fp = create_chip(size="0603")
        assert dsl_fp.name == direct_fp.name

    def test_chip_2512_boundary(self):
        fp = parse_footprint_dsl("2512")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 2


class TestSOTVariants:
    """Test parsing of SOT DSL strings."""

    @pytest.mark.parametrize(
        "spec,expected_variant",
        [
            ("sot23", "SOT-23"),
            ("sot23-5", "SOT-23-5"),
            ("sot23-6", "SOT-23-6"),
            ("sot223", "SOT-223"),
            ("sot89", "SOT-89"),
        ],
    )
    def test_sot_variants(self, spec: str, expected_variant: str):
        fp = parse_footprint_dsl(spec)
        assert isinstance(fp, Footprint)

    def test_sot23_matches_direct(self):
        dsl_fp = parse_footprint_dsl("sot23")
        direct_fp = create_sot(variant="SOT-23")
        assert dsl_fp.name == direct_fp.name
        assert len(dsl_fp.pads) == len(direct_fp.pads)

    def test_sot23_5_matches_direct(self):
        dsl_fp = parse_footprint_dsl("sot23-5")
        direct_fp = create_sot(variant="SOT-23-5")
        assert dsl_fp.name == direct_fp.name


class TestSOIC:
    """Test parsing of SOIC DSL strings."""

    @pytest.mark.parametrize("pins", [8, 14, 16])
    def test_soic_pin_counts(self, pins: int):
        fp = parse_footprint_dsl(f"soic{pins}")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == pins

    def test_soic8_matches_direct(self):
        dsl_fp = parse_footprint_dsl("soic8")
        direct_fp = create_soic(pins=8)
        assert dsl_fp.name == direct_fp.name
        assert len(dsl_fp.pads) == len(direct_fp.pads)

    def test_soic_with_pitch(self):
        fp = parse_footprint_dsl("soic8_p1.27mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 8

    def test_soic_with_pitch_matches_direct(self):
        dsl_fp = parse_footprint_dsl("soic8_p1.27mm")
        direct_fp = create_soic(pins=8, pitch=1.27)
        assert dsl_fp.name == direct_fp.name


class TestQFP:
    """Test parsing of QFP DSL strings."""

    @pytest.mark.parametrize("pins", [32, 48, 100])
    def test_qfp_pin_counts(self, pins: int):
        fp = parse_footprint_dsl(f"qfp{pins}")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == pins

    def test_qfp48_with_pitch(self):
        fp = parse_footprint_dsl("qfp48_p0.5mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 48

    def test_qfp_with_pitch_and_width(self):
        fp = parse_footprint_dsl("qfp100_p0.5mm_w14mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 100

    def test_qfp48_matches_direct(self):
        dsl_fp = parse_footprint_dsl("qfp48_p0.5mm")
        direct_fp = create_qfp(pins=48, pitch=0.5)
        assert dsl_fp.name == direct_fp.name

    def test_qfp144_large_pin_count(self):
        fp = parse_footprint_dsl("qfp144")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 144

    def test_lqfp_alias(self):
        """LQFP should route to QFP generator."""
        fp = parse_footprint_dsl("lqfp48_p0.5mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 48


class TestQFN:
    """Test parsing of QFN DSL strings."""

    def test_qfn16_with_width(self):
        fp = parse_footprint_dsl("qfn16_w3mm")
        assert isinstance(fp, Footprint)

    def test_qfn24_with_pitch(self):
        fp = parse_footprint_dsl("qfn24_p0.5mm")
        assert isinstance(fp, Footprint)

    def test_qfn_matches_direct(self):
        dsl_fp = parse_footprint_dsl("qfn16_w3mm")
        direct_fp = create_qfn(pins=16, body_size=3.0)
        assert dsl_fp.name == direct_fp.name


class TestBGA:
    """Test parsing of BGA DSL strings."""

    def test_bga100(self):
        fp = parse_footprint_dsl("bga100_p0.8mm")
        assert isinstance(fp, Footprint)
        # 10x10 grid = 100 pads
        assert len(fp.pads) == 100

    def test_bga256(self):
        fp = parse_footprint_dsl("bga256_p0.8mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 256

    def test_bga_matches_direct(self):
        dsl_fp = parse_footprint_dsl("bga100_p0.8mm")
        direct_fp = create_bga(rows=10, cols=10, pitch=0.8)
        assert dsl_fp.name == direct_fp.name

    def test_bga_non_square_raises(self):
        with pytest.raises(ValueError, match="not a perfect square"):
            parse_footprint_dsl("bga99_p0.8mm")


class TestDFN:
    """Test parsing of DFN DSL strings."""

    def test_dfn8_with_width_and_pitch(self):
        fp = parse_footprint_dsl("dfn8_w3mm_p0.5mm")
        assert isinstance(fp, Footprint)

    def test_dfn_matches_direct(self):
        dsl_fp = parse_footprint_dsl("dfn8_w3mm_p0.5mm")
        direct_fp = create_dfn(pins=8, body_width=3.0, body_length=3.0, pitch=0.5)
        assert dsl_fp.name == direct_fp.name


class TestDIP:
    """Test parsing of DIP DSL strings."""

    @pytest.mark.parametrize("pins", [8, 16])
    def test_dip_pin_counts(self, pins: int):
        fp = parse_footprint_dsl(f"dip{pins}")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == pins

    def test_dip8_matches_direct(self):
        dsl_fp = parse_footprint_dsl("dip8")
        direct_fp = create_dip(pins=8)
        assert dsl_fp.name == direct_fp.name


class TestCaseInsensitivity:
    """Verify case-insensitive parsing."""

    def test_uppercase_soic(self):
        fp = parse_footprint_dsl("SOIC8")
        assert isinstance(fp, Footprint)

    def test_mixed_case_soic(self):
        fp = parse_footprint_dsl("Soic8")
        assert isinstance(fp, Footprint)

    def test_all_cases_identical(self):
        fp_lower = parse_footprint_dsl("soic8")
        fp_upper = parse_footprint_dsl("SOIC8")
        fp_mixed = parse_footprint_dsl("Soic8")
        assert fp_lower.name == fp_upper.name == fp_mixed.name
        assert len(fp_lower.pads) == len(fp_upper.pads) == len(fp_mixed.pads)

    def test_uppercase_qfp(self):
        fp = parse_footprint_dsl("QFP48")
        assert isinstance(fp, Footprint)

    def test_uppercase_sot(self):
        fp = parse_footprint_dsl("SOT23")
        assert isinstance(fp, Footprint)


class TestErrorHandling:
    """Test error handling for invalid DSL strings."""

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            parse_footprint_dsl("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="non-empty string"):
            parse_footprint_dsl(None)  # type: ignore[arg-type]

    def test_unknown_prefix_raises(self):
        with pytest.raises(ValueError, match="Unknown package prefix"):
            parse_footprint_dsl("xyz123")

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_footprint_dsl("---")

    def test_helpful_error_message(self):
        """Error messages should list valid options."""
        with pytest.raises(ValueError) as exc_info:
            parse_footprint_dsl("abc16")
        msg = str(exc_info.value)
        assert "soic" in msg.lower() or "Valid prefixes" in msg


class TestModifiers:
    """Test modifier parsing (pitch, width, height)."""

    def test_pitch_without_mm_suffix(self):
        fp = parse_footprint_dsl("soic8_p1.27")
        assert isinstance(fp, Footprint)

    def test_pitch_with_mm_suffix(self):
        fp = parse_footprint_dsl("soic8_p1.27mm")
        assert isinstance(fp, Footprint)

    def test_width_modifier(self):
        fp = parse_footprint_dsl("qfn16_w3mm")
        assert isinstance(fp, Footprint)

    def test_multiple_modifiers(self):
        fp = parse_footprint_dsl("qfp100_p0.5mm_w14mm")
        assert isinstance(fp, Footprint)
        assert len(fp.pads) == 100


class TestImportExport:
    """Test that parse_footprint_dsl is importable from the library package."""

    def test_import_from_library(self):
        from kicad_tools.library import parse_footprint_dsl as pfd

        fp = pfd("soic8")
        assert isinstance(fp, Footprint)
