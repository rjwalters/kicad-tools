"""Tests for package extraction and footprint matching."""

from __future__ import annotations

from kicad_tools.datasheet.footprint_matcher import (
    FootprintMatch,
    FootprintMatcher,
    GeneratorSuggestion,
)
from kicad_tools.datasheet.package import (
    PACKAGE_TYPES,
    PackageInfo,
    extract_dimension_from_text,
    get_default_body_size,
    get_default_pitch,
    parse_package_name,
)


class TestPackageInfo:
    """Tests for PackageInfo dataclass."""

    def test_package_info_creation(self):
        """Test basic PackageInfo creation."""
        pkg = PackageInfo(
            name="LQFP48",
            type="qfp",
            pin_count=48,
            body_width=7.0,
            body_length=7.0,
            pitch=0.5,
        )
        assert pkg.name == "LQFP48"
        assert pkg.type == "qfp"
        assert pkg.pin_count == 48
        assert pkg.body_width == 7.0
        assert pkg.body_length == 7.0
        assert pkg.pitch == 0.5
        assert pkg.height is None
        assert pkg.exposed_pad is None

    def test_package_info_with_exposed_pad(self):
        """Test PackageInfo with exposed pad."""
        pkg = PackageInfo(
            name="QFN32",
            type="qfn",
            pin_count=32,
            body_width=5.0,
            body_length=5.0,
            pitch=0.5,
            exposed_pad=(3.0, 3.0),
        )
        assert pkg.exposed_pad == (3.0, 3.0)

    def test_package_info_to_dict(self):
        """Test PackageInfo.to_dict() method."""
        pkg = PackageInfo(
            name="SOIC8",
            type="soic",
            pin_count=8,
            body_width=3.9,
            body_length=4.9,
            pitch=1.27,
            source_page=5,
            confidence=0.85,
        )
        data = pkg.to_dict()
        assert data["name"] == "SOIC8"
        assert data["type"] == "soic"
        assert data["pin_count"] == 8
        assert data["body_width"] == 3.9
        assert data["body_length"] == 4.9
        assert data["pitch"] == 1.27
        assert data["source_page"] == 5
        assert data["confidence"] == 0.85
        assert data["exposed_pad"] is None


class TestParsePackageName:
    """Tests for parse_package_name function."""

    def test_parse_lqfp48(self):
        """Test parsing LQFP48."""
        result = parse_package_name("LQFP48")
        assert result["type"] == "qfp"
        assert result["pin_count"] == 48

    def test_parse_qfn32(self):
        """Test parsing QFN-32."""
        result = parse_package_name("QFN-32")
        assert result["type"] == "qfn"
        assert result["pin_count"] == 32

    def test_parse_soic_with_size(self):
        """Test parsing SOIC with size in name."""
        result = parse_package_name("SOIC-8_3.9x4.9mm")
        assert result["type"] == "soic"
        assert result["pin_count"] == 8
        assert result["body_size"] == (3.9, 4.9)

    def test_parse_with_pitch(self):
        """Test parsing with pitch in name."""
        result = parse_package_name("LQFP-48_P0.5mm")
        assert result["pitch"] == 0.5

    def test_parse_tssop(self):
        """Test parsing TSSOP (should get default pitch)."""
        result = parse_package_name("TSSOP16")
        assert result["type"] == "soic"
        assert result["pin_count"] == 16
        assert result["pitch"] == 0.65

    def test_parse_dip(self):
        """Test parsing DIP package."""
        result = parse_package_name("DIP8")
        assert result["type"] == "dip"
        assert result["pin_count"] == 8

    def test_parse_bga(self):
        """Test parsing BGA package."""
        result = parse_package_name("BGA100")
        assert result["type"] == "bga"
        assert result["pin_count"] == 100


class TestExtractDimensionFromText:
    """Tests for extract_dimension_from_text function."""

    def test_extract_labeled_dimensions(self):
        """Test extracting dimensions with labels."""
        text = "Package dimensions: A = 7.0mm, B = 7.0mm"
        result = extract_dimension_from_text(text)
        assert result["A"] == 7.0
        assert result["B"] == 7.0

    def test_extract_body_dimensions(self):
        """Test extracting body dimensions."""
        text = "Package size: 10.0 x 10.0mm"
        result = extract_dimension_from_text(text)
        assert result["D"] == 10.0
        assert result["E"] == 10.0

    def test_extract_pitch(self):
        """Test extracting pitch."""
        text = "Pin pitch: 0.5mm"
        result = extract_dimension_from_text(text)
        assert result["PITCH"] == 0.5

    def test_extract_mil_to_mm(self):
        """Test conversion from mil to mm."""
        text = "A = 200 mil"
        result = extract_dimension_from_text(text)
        assert abs(result["A"] - 5.08) < 0.01


class TestGetDefaultPitch:
    """Tests for get_default_pitch function."""

    def test_qfp_default_pitch(self):
        """Test QFP default pitch."""
        assert get_default_pitch("qfp") == 0.5

    def test_soic_default_pitch(self):
        """Test SOIC default pitch."""
        assert get_default_pitch("soic") == 1.27

    def test_dip_default_pitch(self):
        """Test DIP default pitch."""
        assert get_default_pitch("dip") == 2.54

    def test_unknown_type_default(self):
        """Test unknown type returns default."""
        assert get_default_pitch("unknown") == 0.5


class TestGetDefaultBodySize:
    """Tests for get_default_body_size function."""

    def test_lqfp48_body_size(self):
        """Test LQFP48 default body size."""
        width, length = get_default_body_size("qfp", 48)
        assert width == 7.0
        assert length == 7.0

    def test_lqfp100_body_size(self):
        """Test LQFP100 default body size."""
        width, length = get_default_body_size("qfp", 100)
        assert width == 14.0
        assert length == 14.0

    def test_soic8_body_size(self):
        """Test SOIC8 default body size."""
        width, length = get_default_body_size("soic", 8)
        assert width == 3.9


class TestFootprintMatch:
    """Tests for FootprintMatch dataclass."""

    def test_footprint_match_creation(self):
        """Test FootprintMatch creation."""
        match = FootprintMatch(
            library="Package_QFP",
            footprint="LQFP-48_7x7mm_P0.5mm",
            confidence=0.95,
        )
        assert match.library == "Package_QFP"
        assert match.footprint == "LQFP-48_7x7mm_P0.5mm"
        assert match.confidence == 0.95

    def test_full_name_property(self):
        """Test full_name property."""
        match = FootprintMatch(
            library="Package_QFP",
            footprint="LQFP-48_7x7mm_P0.5mm",
            confidence=0.95,
        )
        assert match.full_name == "Package_QFP:LQFP-48_7x7mm_P0.5mm"

    def test_to_dict(self):
        """Test to_dict method."""
        match = FootprintMatch(
            library="Package_QFP",
            footprint="LQFP-48_7x7mm_P0.5mm",
            confidence=0.95,
            dimension_match={"pin_count": True, "pitch": True},
        )
        data = match.to_dict()
        assert data["library"] == "Package_QFP"
        assert data["footprint"] == "LQFP-48_7x7mm_P0.5mm"
        assert data["full_name"] == "Package_QFP:LQFP-48_7x7mm_P0.5mm"
        assert data["confidence"] == 0.95
        assert data["dimension_match"]["pin_count"] is True


class TestGeneratorSuggestion:
    """Tests for GeneratorSuggestion dataclass."""

    def test_generator_suggestion_creation(self):
        """Test GeneratorSuggestion creation."""
        suggestion = GeneratorSuggestion(
            generator="qfp",
            params={"pins": 48, "pitch": 0.5, "body_size": 7.0},
            confidence=0.9,
        )
        assert suggestion.generator == "qfp"
        assert suggestion.params["pins"] == 48
        assert suggestion.confidence == 0.9

    def test_command_generation(self):
        """Test CLI command generation."""
        suggestion = GeneratorSuggestion(
            generator="qfp",
            params={"pins": 48, "pitch": 0.5, "body_size": 7.0},
            confidence=0.9,
        )
        assert "kct lib generate-footprint" in suggestion.command
        assert "qfp" in suggestion.command
        assert "--pins 48" in suggestion.command
        assert "--pitch 0.5" in suggestion.command

    def test_to_dict(self):
        """Test to_dict method."""
        suggestion = GeneratorSuggestion(
            generator="soic",
            params={"pins": 8, "pitch": 1.27},
            confidence=0.85,
        )
        data = suggestion.to_dict()
        assert data["generator"] == "soic"
        assert data["params"]["pins"] == 8
        assert data["confidence"] == 0.85
        assert "command" in data


class TestFootprintMatcher:
    """Tests for FootprintMatcher class."""

    def test_matcher_creation(self):
        """Test FootprintMatcher creation."""
        matcher = FootprintMatcher()
        assert matcher is not None

    def test_find_matches_lqfp(self):
        """Test finding matches for LQFP package."""
        matcher = FootprintMatcher()
        pkg = PackageInfo(
            name="LQFP48",
            type="qfp",
            pin_count=48,
            body_width=7.0,
            body_length=7.0,
            pitch=0.5,
        )
        matches = matcher.find_matches(pkg)
        assert len(matches) > 0
        # Best match should have high confidence
        assert matches[0].confidence > 0.7
        # Should be in QFP library
        assert matches[0].library == "Package_QFP"

    def test_find_matches_soic(self):
        """Test finding matches for SOIC package."""
        matcher = FootprintMatcher()
        pkg = PackageInfo(
            name="SOIC8",
            type="soic",
            pin_count=8,
            body_width=3.9,
            body_length=4.9,
            pitch=1.27,
        )
        matches = matcher.find_matches(pkg)
        assert len(matches) > 0
        assert matches[0].library == "Package_SO"

    def test_suggest_generator(self):
        """Test generator suggestion."""
        matcher = FootprintMatcher()
        pkg = PackageInfo(
            name="QFN32",
            type="qfn",
            pin_count=32,
            body_width=5.0,
            body_length=5.0,
            pitch=0.5,
        )
        suggestion = matcher.suggest_generator(pkg)
        assert suggestion.generator == "qfn"
        assert suggestion.params["pins"] == 32
        assert suggestion.params["pitch"] == 0.5
        assert suggestion.params["body_size"] == 5.0

    def test_suggest_generator_with_ep(self):
        """Test generator suggestion with exposed pad."""
        matcher = FootprintMatcher()
        pkg = PackageInfo(
            name="QFN32",
            type="qfn",
            pin_count=32,
            body_width=5.0,
            body_length=5.0,
            pitch=0.5,
            exposed_pad=(3.0, 3.0),
        )
        suggestion = matcher.suggest_generator(pkg)
        assert suggestion.params["ep_width"] == 3.0
        assert suggestion.params["ep_length"] == 3.0

    def test_get_all_suggestions(self):
        """Test getting both matches and suggestions."""
        matcher = FootprintMatcher()
        pkg = PackageInfo(
            name="LQFP48",
            type="qfp",
            pin_count=48,
            body_width=7.0,
            body_length=7.0,
            pitch=0.5,
        )
        result = matcher.get_all_suggestions(pkg)
        assert "matches" in result
        assert "suggestion" in result
        assert "best_match" in result
        assert len(result["matches"]) > 0


class TestPackageTypes:
    """Tests for PACKAGE_TYPES dictionary."""

    def test_common_packages_defined(self):
        """Test that common packages are defined."""
        assert "LQFP" in PACKAGE_TYPES
        assert "QFN" in PACKAGE_TYPES
        assert "SOIC" in PACKAGE_TYPES
        assert "DIP" in PACKAGE_TYPES
        assert "BGA" in PACKAGE_TYPES

    def test_package_types_have_type_field(self):
        """Test that all packages have a type field."""
        for name, info in PACKAGE_TYPES.items():
            assert "type" in info, f"{name} missing 'type' field"

    def test_ssop_has_default_pitch(self):
        """Test SSOP has default pitch."""
        assert "pitch" in PACKAGE_TYPES["SSOP"]
        assert PACKAGE_TYPES["SSOP"]["pitch"] == 0.65
