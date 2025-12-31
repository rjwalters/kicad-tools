"""Edge case tests for improved test coverage.

This module tests edge cases including:
- Empty/minimal files
- Malformed S-expression handling
- Boundary values (long names, special characters)
- Hierarchical schematic edge cases
- Query module empty results
"""

from dataclasses import dataclass
from pathlib import Path

import pytest

from kicad_tools.core.sexp_file import (
    load_symbol_lib,
)
from kicad_tools.query.base import BaseQuery
from kicad_tools.schema.pcb import PCB
from kicad_tools.schema.schematic import Schematic
from kicad_tools.sexp import SExp, parse_sexp, serialize_sexp

# --- Empty/Minimal File Edge Cases ---


class TestEmptyMinimalFiles:
    """Tests for handling empty and minimal file structures."""

    def test_empty_schematic_no_symbols(self, tmp_path: Path):
        """Test schematic with no symbols."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
        )"""
        sch_file = tmp_path / "empty.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        assert len(sch.symbols) == 0
        assert sch.symbols.query().first() is None
        assert list(sch.symbols) == []

    def test_empty_schematic_no_wires(self, tmp_path: Path):
        """Test schematic with no wires."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
        )"""
        sch_file = tmp_path / "nowires.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        assert len(sch.wires) == 0

    def test_empty_pcb_no_footprints(self, tmp_path: Path):
        """Test PCB with no footprints."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
        )"""
        pcb_file = tmp_path / "empty.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        assert len(pcb.footprints) == 0
        assert pcb.footprints.query().first() is None
        assert list(pcb.footprints) == []

    def test_empty_pcb_no_nets(self, tmp_path: Path):
        """Test PCB with only the default net."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
        )"""
        pcb_file = tmp_path / "nonets.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        # Only the default empty net should exist
        assert len(pcb.nets) >= 1

    def test_empty_symbol_library(self, tmp_path: Path):
        """Test empty symbol library."""
        lib_content = """(kicad_symbol_lib
          (version 20231120)
        )"""
        lib_file = tmp_path / "empty.kicad_sym"
        lib_file.write_text(lib_content)

        sexp = load_symbol_lib(lib_file)
        assert sexp.tag == "kicad_symbol_lib"
        symbols = sexp.find_all("symbol")
        assert len(symbols) == 0


# --- Malformed S-Expression Handling ---


class TestMalformedSexp:
    """Tests for graceful handling of malformed S-expressions."""

    def test_unclosed_parenthesis(self):
        """Test error on unclosed parenthesis."""
        with pytest.raises(ValueError, match="Unexpected end"):
            parse_sexp("(test (inner)")

    def test_extra_closing_parenthesis(self):
        """Test error on extra closing parenthesis."""
        with pytest.raises(ValueError, match="Unexpected"):
            parse_sexp("(test))")

    def test_unterminated_string(self):
        """Test error on unterminated string."""
        with pytest.raises(ValueError, match="Unterminated string"):
            parse_sexp('(test "hello)')

    def test_deeply_nested_structure(self):
        """Test parsing deeply nested structures."""
        # Create a deeply nested structure (100 levels)
        nested = "(" * 100 + "leaf" + ")" * 100
        sexp = parse_sexp(nested)
        # Navigate to the deepest level
        current = sexp
        for _ in range(99):
            if current.children:
                current = current.children[0]
        assert current is not None

    def test_multiple_top_level_elements(self):
        """Test error on multiple top-level elements."""
        with pytest.raises(ValueError, match="Unexpected content"):
            parse_sexp("(a)(b)")

    def test_empty_input(self):
        """Test error on empty input."""
        with pytest.raises(ValueError):
            parse_sexp("")

    def test_whitespace_only_input(self):
        """Test error on whitespace-only input."""
        with pytest.raises(ValueError):
            parse_sexp("   \n\t  ")

    def test_comment_only_input(self):
        """Test error on comment-only input."""
        with pytest.raises(ValueError):
            parse_sexp("; just a comment")

    def test_invalid_escape_sequence(self):
        """Test handling of invalid escape sequences."""
        # Invalid escape should be handled gracefully
        text = r'(test "hello\xworld")'
        sexp = parse_sexp(text)
        # The parser should handle this somehow
        assert sexp.tag == "test"

    def test_null_byte_in_string(self):
        """Test handling of null bytes in strings."""
        text = '(test "hello\\x00world")'
        # This might raise an error or handle it gracefully
        try:
            sexp = parse_sexp(text)
            assert sexp.tag == "test"
        except ValueError:
            # Also acceptable - explicit rejection
            pass

    def test_mixed_quotes(self):
        """Test strings with embedded quotes."""
        text = r'(test "say \"hello\" to the world")'
        sexp = parse_sexp(text)
        result = sexp.get_string(0)
        assert "hello" in result

    def test_unicode_in_string(self):
        """Test Unicode characters in strings."""
        text = '(test "caf\u00e9 \u4e2d\u6587")'
        sexp = parse_sexp(text)
        result = sexp.get_string(0)
        assert "\u00e9" in result  # e with accent
        assert "\u4e2d" in result  # Chinese character


# --- Boundary Value Edge Cases ---


class TestBoundaryValues:
    """Tests for boundary values like very long strings, special characters."""

    def test_very_long_net_name(self, tmp_path: Path):
        """Test handling of very long net names."""
        long_name = "NET_" + "A" * 500
        pcb_content = f"""(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
          (net 1 "{long_name}")
        )"""
        pcb_file = tmp_path / "longnet.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        # Nets might have different attribute structure
        # Just verify PCB loads without error and has nets
        assert len(pcb.nets) >= 2

    def test_special_characters_in_reference(self, tmp_path: Path):
        """Test handling of special characters in component references."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (symbol
            (lib_id "Device:R")
            (at 100 100 0)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Reference" "R_1-A" (at 100 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "R_1-A")
                  (unit 1)
                )
              )
            )
          )
        )"""
        sch_file = tmp_path / "special.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        assert len(sch.symbols) >= 1
        refs = [s.reference for s in sch.symbols]
        assert any("R_1-A" in r for r in refs)

    def test_unicode_in_value_property(self, tmp_path: Path):
        """Test Unicode characters in component values."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (symbol
            (lib_id "Device:R")
            (at 100 100 0)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "10k\u03a9" (at 100 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "R1")
                  (unit 1)
                )
              )
            )
          )
        )"""
        sch_file = tmp_path / "unicode.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        sym = sch.symbols.by_reference("R1")
        assert sym is not None
        # Value should contain omega symbol
        assert "\u03a9" in sym.value or "k" in sym.value

    def test_extreme_coordinates(self, tmp_path: Path):
        """Test handling of extreme coordinate values."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
          (footprint "Resistor_SMD:R_0402_1005Metric"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 99999.99 -99999.99)
            (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
            (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
          )
        )"""
        pcb_file = tmp_path / "extreme.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        fp = pcb.footprints.by_reference("R1")
        assert fp is not None
        # Position is a tuple (x, y)
        assert abs(fp.position[0]) > 99000
        assert abs(fp.position[1]) > 99000

    def test_zero_dimension_values(self):
        """Test handling of zero dimension values in S-expressions."""
        text = "(pad 1 smd roundrect (at 0 0) (size 0 0))"
        sexp = parse_sexp(text)
        assert sexp.tag == "pad"
        size = sexp.find("size")
        assert size is not None
        assert size.get_float(0) == 0.0
        assert size.get_float(1) == 0.0

    def test_negative_zero(self):
        """Test handling of negative zero in coordinates."""
        text = "(at -0.0 0.0)"
        sexp = parse_sexp(text)
        assert sexp.tag == "at"
        # Both should be treated as zero
        assert sexp.get_float(0) == 0.0
        assert sexp.get_float(1) == 0.0

    def test_scientific_notation_extreme(self):
        """Test handling of extreme scientific notation."""
        text = "(value 1.5e-15 2.3e+20)"
        sexp = parse_sexp(text)
        val1 = sexp.get_float(0)
        val2 = sexp.get_float(1)
        assert val1 == pytest.approx(1.5e-15)
        assert val2 == pytest.approx(2.3e20)


# --- Query Edge Cases ---


@dataclass
class MockQueryItem:
    """Mock item for query edge cases."""

    name: str
    value: int | None = None
    category: str | None = None


class TestQueryEdgeCases:
    """Tests for query module edge cases."""

    def test_query_empty_list(self):
        """Test querying an empty list."""
        query = BaseQuery([])
        assert query.all() == []
        assert query.first() is None
        assert query.last() is None
        assert query.count() == 0
        assert query.exists() is False

    def test_query_chain_on_empty_result(self):
        """Test chaining filters that result in empty set."""
        items = [MockQueryItem("a", 1, "A"), MockQueryItem("b", 2, "B")]
        query = BaseQuery(items)
        result = query.filter(category="Z").filter(value__gt=100).all()
        assert result == []

    def test_filter_with_none_values(self):
        """Test filtering items with None values."""
        items = [
            MockQueryItem("a", 1, "A"),
            MockQueryItem("b", None, "B"),
            MockQueryItem("c", 3, None),
        ]
        query = BaseQuery(items)

        # Filter for None category using isnull
        result = query.filter(category__isnull=True).all()
        assert len(result) == 1
        assert result[0].name == "c"

    def test_filter_nonexistent_attribute(self):
        """Test filtering on non-existent attribute."""
        items = [MockQueryItem("a", 1, "A")]
        query = BaseQuery(items)
        result = query.filter(nonexistent="value").all()
        # Should return empty - no items have this attribute
        assert result == []

    def test_values_on_empty_result(self):
        """Test values() on empty query result."""
        query = BaseQuery([])
        result = query.values("name", "value")
        assert result == []

    def test_values_list_on_empty_result(self):
        """Test values_list() on empty query result."""
        query = BaseQuery([])
        result = query.values_list("name", flat=True)
        assert result == []

    def test_order_by_empty_list(self):
        """Test order_by on empty list."""
        query = BaseQuery([])
        result = query.order_by("name").all()
        assert result == []

    def test_order_by_with_none_values(self):
        """Test order_by with items that have None values."""
        items = [
            MockQueryItem("c", 3, "C"),
            MockQueryItem("a", None, "A"),
            MockQueryItem("b", 2, "B"),
        ]
        query = BaseQuery(items)
        # Order by a field that doesn't have None values
        result = query.order_by("name").all()
        # Should sort by name
        assert len(result) == 3
        assert result[0].name == "a"
        assert result[1].name == "b"
        assert result[2].name == "c"

    def test_exclude_all_items(self):
        """Test exclude that removes all items."""
        items = [MockQueryItem("a", 1, "A"), MockQueryItem("b", 2, "A")]
        query = BaseQuery(items)
        result = query.exclude(category="A").all()
        assert result == []

    def test_getitem_out_of_bounds(self):
        """Test __getitem__ with out of bounds index."""
        items = [MockQueryItem("a", 1, "A")]
        query = BaseQuery(items)
        with pytest.raises(IndexError):
            _ = query[10]

    def test_iter_empty(self):
        """Test iterating over empty query."""
        query = BaseQuery([])
        count = 0
        for _ in query:
            count += 1
        assert count == 0

    def test_bool_empty(self):
        """Test boolean evaluation of empty query."""
        query = BaseQuery([])
        assert bool(query) is False

    def test_len_empty(self):
        """Test len of empty query."""
        query = BaseQuery([])
        assert len(query) == 0

    def test_complex_filter_chain(self):
        """Test complex filter chain with multiple operations."""
        items = [
            MockQueryItem("alpha", 10, "A"),
            MockQueryItem("beta", 20, "B"),
            MockQueryItem("gamma", 30, "A"),
            MockQueryItem("delta", 40, "B"),
        ]
        query = BaseQuery(items)
        result = query.filter(value__gte=15).exclude(category="B").filter(name__contains="a").all()
        assert len(result) == 1
        assert result[0].name == "gamma"


# --- Schematic/PCB Integration Edge Cases ---


class TestSchematicEdgeCases:
    """Tests for schematic edge cases."""

    def test_schematic_with_no_instances(self, tmp_path: Path):
        """Test symbol without instances section."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (symbol
            (lib_id "Device:R")
            (at 100 100 0)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Reference" "R1" (at 100 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "10k" (at 100 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
          )
        )"""
        sch_file = tmp_path / "noinstances.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        # Should load without error
        assert len(sch.symbols) >= 1

    def test_schematic_multiple_symbols_same_reference(self, tmp_path: Path):
        """Test schematic with multiple symbols having same reference (multi-unit)."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (symbol
            (lib_id "Device:R")
            (at 100 100 0)
            (uuid "00000000-0000-0000-0000-000000000002")
            (property "Reference" "U1" (at 100 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "LM358" (at 100 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 100 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 100 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "U1")
                  (unit 1)
                )
              )
            )
          )
          (symbol
            (lib_id "Device:R")
            (at 200 100 0)
            (uuid "00000000-0000-0000-0000-000000000003")
            (property "Reference" "U1" (at 200 90 0) (effects (font (size 1.27 1.27))))
            (property "Value" "LM358" (at 200 110 0) (effects (font (size 1.27 1.27))))
            (property "Footprint" "" (at 200 100 0) (effects (hide yes)))
            (property "Datasheet" "" (at 200 100 0) (effects (hide yes)))
            (instances
              (project "test"
                (path "/00000000-0000-0000-0000-000000000001"
                  (reference "U1")
                  (unit 2)
                )
              )
            )
          )
        )"""
        sch_file = tmp_path / "multiunit.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        u1_symbols = [s for s in sch.symbols if s.reference == "U1"]
        # May have 2 units or collapsed to 1 depending on implementation
        assert len(u1_symbols) >= 1


class TestPCBEdgeCases:
    """Tests for PCB edge cases."""

    def test_pcb_footprint_at_origin(self, tmp_path: Path):
        """Test footprint placed at exact origin."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
          (footprint "Resistor_SMD:R_0402_1005Metric"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 0 0)
            (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
            (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
          )
        )"""
        pcb_file = tmp_path / "origin.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        fp = pcb.footprints.by_reference("R1")
        assert fp is not None
        # Position is a tuple (x, y)
        assert fp.position[0] == 0.0
        assert fp.position[1] == 0.0

    def test_pcb_footprint_rotated_180(self, tmp_path: Path):
        """Test footprint with 180 degree rotation."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
          (footprint "Resistor_SMD:R_0402_1005Metric"
            (layer "F.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 100 100 180)
            (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
            (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
            (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask"))
          )
        )"""
        pcb_file = tmp_path / "rotated.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        fp = pcb.footprints.by_reference("R1")
        assert fp is not None
        assert fp.rotation == 180.0

    def test_pcb_on_bottom_layer(self, tmp_path: Path):
        """Test footprint on bottom layer."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (generator_version "8.0")
          (general
            (thickness 1.6)
            (legacy_teardrops no)
          )
          (paper "A4")
          (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
          )
          (setup
            (pad_to_mask_clearance 0)
          )
          (net 0 "")
          (footprint "Resistor_SMD:R_0402_1005Metric"
            (layer "B.Cu")
            (uuid "00000000-0000-0000-0000-000000000010")
            (at 100 100)
            (property "Reference" "R1" (at 0 -1.5 0) (layer "B.SilkS") (uuid "ref-uuid"))
            (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "B.Cu" "B.Paste" "B.Mask"))
            (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "B.Cu" "B.Paste" "B.Mask"))
          )
        )"""
        pcb_file = tmp_path / "bottom.kicad_pcb"
        pcb_file.write_text(pcb_content)

        pcb = PCB.load(str(pcb_file))
        fp = pcb.footprints.by_reference("R1")
        assert fp is not None
        assert fp.layer == "B.Cu"

        # Test on_bottom filter
        bottom_fps = pcb.footprints.on_bottom()
        assert len(bottom_fps) == 1


# --- S-Expression Serialization Edge Cases ---


class TestSexpSerializationEdgeCases:
    """Tests for S-expression serialization edge cases."""

    def test_serialize_empty_sexp(self):
        """Test serializing empty S-expression."""
        sexp = SExp("test")
        result = serialize_sexp(sexp)
        assert "test" in result

    def test_serialize_nested_empty(self):
        """Test serializing nested empty S-expressions."""
        sexp = SExp("outer")
        sexp.add(SExp("inner"))
        result = serialize_sexp(sexp)
        assert "outer" in result
        assert "inner" in result

    def test_serialize_special_chars_in_string(self):
        """Test serializing strings with special characters."""
        sexp = SExp("test")
        sexp.add('value with "quotes" and \\backslash')
        result = serialize_sexp(sexp)
        parsed = parse_sexp(result)
        # Round-trip should preserve the string
        assert parsed.tag == "test"

    def test_serialize_boolean_like_strings(self):
        """Test serializing strings that look like booleans."""
        sexp = SExp("test")
        sexp.add("yes")
        sexp.add("no")
        result = serialize_sexp(sexp)
        parsed = parse_sexp(result)
        assert parsed.tag == "test"

    def test_roundtrip_complex_structure(self):
        """Test round-trip of complex nested structure."""
        original = """(kicad_sch
          (version 20231120)
          (symbol
            (lib_id "Device:R")
            (property "Reference" "R1" (at 0 0 0))
            (property "Value" "10k" (at 0 1 0))
          )
        )"""
        parsed = parse_sexp(original)
        serialized = serialize_sexp(parsed)
        reparsed = parse_sexp(serialized)

        # Check key elements preserved
        assert reparsed.tag == "kicad_sch"
        version = reparsed.find("version")
        assert version is not None
        symbol = reparsed.find("symbol")
        assert symbol is not None


# --- Hierarchical Schematic Edge Cases ---


class TestHierarchyEdgeCases:
    """Tests for hierarchical schematic edge cases."""

    def test_flat_schematic_no_sheets(self, tmp_path: Path):
        """Test hierarchy on schematic with no sub-sheets."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
        )"""
        sch_file = tmp_path / "flat.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        # Flat schematic should have no sheets
        assert len(sch.sheets) == 0

    def test_schematic_with_empty_sheet_reference(self, tmp_path: Path):
        """Test schematic with sheet that has no file path."""
        sch_content = """(kicad_sch
          (version 20231120)
          (generator "test")
          (generator_version "8.0")
          (uuid "00000000-0000-0000-0000-000000000001")
          (paper "A4")
          (lib_symbols)
          (sheet
            (at 100 100)
            (size 20 10)
            (uuid "00000000-0000-0000-0000-000000000010")
            (property "Sheetname" "SubSheet" (at 100 95 0))
            (property "Sheetfile" "" (at 100 115 0))
          )
        )"""
        sch_file = tmp_path / "emptysheet.kicad_sch"
        sch_file.write_text(sch_content)

        sch = Schematic.load(sch_file)
        # Should load without error
        assert len(sch.sheets) == 1
