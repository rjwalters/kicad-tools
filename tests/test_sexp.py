"""Tests for S-expression parser and file I/O."""

import pytest
from pathlib import Path

from kicad_tools.core.sexp import SExp, parse_sexp, SExpParser, SExpSerializer
from kicad_tools.core.sexp_file import (
    serialize_sexp, load_schematic, save_schematic,
    load_pcb, save_pcb, load_symbol_lib, save_symbol_lib
)


class TestSExpBasicParsing:
    """Basic parsing tests."""

    def test_parse_simple(self):
        """Parse a simple S-expression."""
        text = '(test "value")'
        sexp = parse_sexp(text)
        assert sexp.tag == "test"
        assert sexp.get_string(0) == "value"

    def test_parse_nested(self):
        """Parse nested S-expressions."""
        text = '(outer (inner "value"))'
        sexp = parse_sexp(text)
        assert sexp.tag == "outer"
        inner = sexp.find("inner")
        assert inner is not None
        assert inner.get_string(0) == "value"

    def test_parse_numbers(self):
        """Parse numeric values."""
        text = "(point 1.5 -2.3)"
        sexp = parse_sexp(text)
        assert sexp.tag == "point"
        assert sexp.get_float(0) == 1.5
        assert sexp.get_float(1) == -2.3

    def test_parse_integers(self):
        """Parse integer values."""
        text = "(count 42 -7)"
        sexp = parse_sexp(text)
        assert sexp.get_int(0) == 42
        assert sexp.get_int(1) == -7

    def test_parse_scientific_notation(self):
        """Parse scientific notation."""
        text = "(value 1.5e-3)"
        sexp = parse_sexp(text)
        assert sexp.get_float(0) == pytest.approx(0.0015)

    def test_parse_empty_list(self):
        """Parse empty list."""
        text = "()"
        sexp = parse_sexp(text)
        assert sexp.tag == ""
        assert len(sexp.values) == 0

    def test_parse_with_comments(self):
        """Parse with comments."""
        text = """(test ; this is a comment
            "value")"""
        sexp = parse_sexp(text)
        assert sexp.tag == "test"
        assert sexp.get_string(0) == "value"


class TestSExpStringParsing:
    """String parsing tests."""

    def test_parse_escaped_newline(self):
        """Parse string with escaped newline."""
        text = r'(text "line1\nline2")'
        sexp = parse_sexp(text)
        assert sexp.get_string(0) == "line1\nline2"

    def test_parse_escaped_tab(self):
        """Parse string with escaped tab."""
        text = r'(text "col1\tcol2")'
        sexp = parse_sexp(text)
        assert sexp.get_string(0) == "col1\tcol2"

    def test_parse_escaped_quote(self):
        """Parse string with escaped quote."""
        text = r'(text "say \"hello\"")'
        sexp = parse_sexp(text)
        assert sexp.get_string(0) == 'say "hello"'

    def test_parse_escaped_backslash(self):
        """Parse string with escaped backslash."""
        text = r'(text "path\\to\\file")'
        sexp = parse_sexp(text)
        assert sexp.get_string(0) == "path\\to\\file"


class TestSExpErrors:
    """Error handling tests."""

    def test_unexpected_end_in_list(self):
        """Error on unclosed list."""
        with pytest.raises(ValueError, match="Unexpected end"):
            parse_sexp("(test")

    def test_unexpected_end_in_string(self):
        """Error on unclosed string."""
        with pytest.raises(ValueError, match="Unexpected end"):
            parse_sexp('(test "unclosed')

    def test_trailing_content(self):
        """Error on trailing content."""
        with pytest.raises(ValueError, match="Unexpected content"):
            parse_sexp("(test) extra")


class TestSExpMethods:
    """Tests for SExp methods."""

    def test_find_all(self):
        """Find all matching children."""
        text = "(root (item 1) (item 2) (other 3))"
        sexp = parse_sexp(text)
        items = sexp.find_all("item")
        assert len(items) == 2

    def test_find_not_found(self):
        """Find returns None when not found."""
        sexp = parse_sexp("(test)")
        assert sexp.find("missing") is None

    def test_getitem_by_index(self):
        """Get value by index."""
        sexp = parse_sexp("(test 1 2 3)")
        assert sexp[0] == 1
        assert sexp[1] == 2
        assert sexp[2] == 3
        assert sexp[99] is None

    def test_getitem_by_tag(self):
        """Get child by tag."""
        sexp = parse_sexp("(outer (inner 42))")
        inner = sexp["inner"]
        assert inner is not None
        assert inner.tag == "inner"

    def test_get_value(self):
        """Get value by index."""
        sexp = parse_sexp("(test 1 2 3)")
        assert sexp.get_value(0) == 1
        assert sexp.get_value(99) is None

    def test_get_string_from_number(self):
        """Get string from numeric value."""
        sexp = parse_sexp("(test 42)")
        assert sexp.get_string(0) == "42"

    def test_get_int_from_string(self):
        """Get int from string value."""
        sexp = SExp("test")
        sexp.add("42")
        assert sexp.get_int(0) == 42

    def test_get_int_invalid_string(self):
        """Get int from invalid string returns None."""
        sexp = SExp("test")
        sexp.add("not a number")
        assert sexp.get_int(0) is None

    def test_get_float_from_int(self):
        """Get float from integer."""
        sexp = parse_sexp("(test 42)")
        assert sexp.get_float(0) == 42.0

    def test_get_float_invalid_string(self):
        """Get float from invalid string returns None."""
        sexp = SExp("test")
        sexp.add("not a number")
        assert sexp.get_float(0) is None

    def test_iter_children(self):
        """Iterate over child SExp nodes."""
        sexp = parse_sexp("(root 1 (a) 2 (b) 3)")
        children = list(sexp.iter_children())
        assert len(children) == 2
        assert children[0].tag == "a"
        assert children[1].tag == "b"

    def test_has_tag(self):
        """Check for tag presence."""
        sexp = parse_sexp("(root (child))")
        assert sexp.has_tag("child") is True
        assert sexp.has_tag("missing") is False

    def test_add(self):
        """Add values."""
        sexp = SExp("test")
        sexp.add(1).add(2).add(3)
        assert len(sexp.values) == 3
        assert sexp.values == [1, 2, 3]

    def test_set_value(self):
        """Set value at index."""
        sexp = SExp("test")
        sexp.add(1).add(2)
        sexp.set_value(1, 99)
        assert sexp.values[1] == 99

    def test_set_value_extends_list(self):
        """Set value extends list if needed."""
        sexp = SExp("test")
        sexp.set_value(2, "value")
        assert len(sexp.values) == 3
        assert sexp.values[2] == "value"

    def test_remove_child(self):
        """Remove child by tag."""
        sexp = parse_sexp("(root (a) (b) (c))")
        assert sexp.remove_child("b") is True
        assert len(sexp.find_all("b")) == 0

    def test_remove_child_not_found(self):
        """Remove returns False if not found."""
        sexp = parse_sexp("(root (a))")
        assert sexp.remove_child("missing") is False

    def test_repr_empty(self):
        """String representation of empty SExp."""
        sexp = SExp("test")
        assert repr(sexp) == "SExp('test')"

    def test_repr_with_values(self):
        """String representation with values."""
        sexp = SExp("test")
        sexp.add(1)
        assert "SExp" in repr(sexp)
        assert "test" in repr(sexp)


class TestSExpSerialization:
    """Tests for serialization."""

    def test_serialize_simple(self):
        """Serialize simple S-expression."""
        text = '(test "value")'
        sexp = parse_sexp(text)
        result = serialize_sexp(sexp)
        assert "test" in result
        assert "value" in result

    def test_serialize_roundtrip(self):
        """Serialize and parse should preserve data."""
        original = parse_sexp('(test 1.5 "hello" (nested 42))')
        serialized = serialize_sexp(original)
        reparsed = parse_sexp(serialized)
        assert reparsed.tag == "test"
        assert reparsed.get_float(0) == 1.5
        assert reparsed.get_string(1) == "hello"
        nested = reparsed.find("nested")
        assert nested is not None
        assert nested.get_int(0) == 42

    def test_serialize_needs_quoting(self):
        """Serialize strings that need quoting."""
        sexp = SExp("test")
        sexp.add("hello world")  # Has space
        result = serialize_sexp(sexp)
        assert '"hello world"' in result

    def test_serialize_empty_string(self):
        """Serialize empty string."""
        sexp = SExp("test")
        sexp.add("")
        result = serialize_sexp(sexp)
        assert '""' in result

    def test_serialize_number_like_string(self):
        """Serialize strings that look like numbers."""
        sexp = SExp("test")
        sexp.add("123")  # Should be quoted
        result = serialize_sexp(sexp)
        # The string "123" should be quoted to avoid ambiguity
        assert '"123"' in result

    def test_serialize_float_int_value(self):
        """Serialize float that equals an int."""
        serializer = SExpSerializer()
        result = serializer._format_value(42.0)
        assert result == "42"


class TestSExpFileIO:
    """Tests for file I/O functions."""

    def test_load_schematic(self, minimal_schematic: Path):
        """Load a schematic file."""
        sexp = load_schematic(minimal_schematic)
        assert sexp.tag == "kicad_sch"

    def test_load_schematic_not_found(self, tmp_path: Path):
        """Error on file not found."""
        with pytest.raises(FileNotFoundError):
            load_schematic(tmp_path / "nonexistent.kicad_sch")

    def test_load_schematic_invalid(self, tmp_path: Path):
        """Error on invalid schematic."""
        bad_file = tmp_path / "bad.kicad_sch"
        bad_file.write_text("(not_a_schematic)")
        with pytest.raises(ValueError, match="Not a KiCad schematic"):
            load_schematic(bad_file)

    def test_save_schematic(self, minimal_schematic: Path, tmp_path: Path):
        """Save a schematic file."""
        sexp = load_schematic(minimal_schematic)
        output = tmp_path / "saved.kicad_sch"
        save_schematic(sexp, output)
        assert output.exists()
        # Verify it can be reloaded
        reloaded = load_schematic(output)
        assert reloaded.tag == "kicad_sch"

    def test_save_schematic_invalid(self, tmp_path: Path):
        """Error on saving non-schematic."""
        sexp = SExp("not_kicad_sch")
        with pytest.raises(ValueError):
            save_schematic(sexp, tmp_path / "bad.kicad_sch")

    def test_load_pcb(self, minimal_pcb: Path):
        """Load a PCB file."""
        sexp = load_pcb(minimal_pcb)
        assert sexp.tag == "kicad_pcb"

    def test_load_pcb_not_found(self, tmp_path: Path):
        """Error on PCB file not found."""
        with pytest.raises(FileNotFoundError):
            load_pcb(tmp_path / "nonexistent.kicad_pcb")

    def test_load_pcb_invalid(self, tmp_path: Path):
        """Error on invalid PCB."""
        bad_file = tmp_path / "bad.kicad_pcb"
        bad_file.write_text("(not_a_pcb)")
        with pytest.raises(ValueError, match="Not a KiCad PCB"):
            load_pcb(bad_file)

    def test_save_pcb(self, minimal_pcb: Path, tmp_path: Path):
        """Save a PCB file."""
        sexp = load_pcb(minimal_pcb)
        output = tmp_path / "saved.kicad_pcb"
        save_pcb(sexp, output)
        assert output.exists()

    def test_save_pcb_invalid(self, tmp_path: Path):
        """Error on saving non-PCB."""
        sexp = SExp("not_kicad_pcb")
        with pytest.raises(ValueError):
            save_pcb(sexp, tmp_path / "bad.kicad_pcb")

    def test_load_symbol_lib(self, tmp_path: Path):
        """Load a symbol library."""
        lib_file = tmp_path / "test.kicad_sym"
        lib_file.write_text("""(kicad_symbol_lib
            (version 20231120)
            (symbol "Device:R" (property "Reference" "R"))
        )""")
        sexp = load_symbol_lib(lib_file)
        assert sexp.tag == "kicad_symbol_lib"

    def test_load_symbol_lib_not_found(self, tmp_path: Path):
        """Error on symbol lib not found."""
        with pytest.raises(FileNotFoundError):
            load_symbol_lib(tmp_path / "nonexistent.kicad_sym")

    def test_load_symbol_lib_invalid(self, tmp_path: Path):
        """Error on invalid symbol library."""
        bad_file = tmp_path / "bad.kicad_sym"
        bad_file.write_text("(not_a_symbol_lib)")
        with pytest.raises(ValueError, match="Not a KiCad symbol library"):
            load_symbol_lib(bad_file)

    def test_save_symbol_lib(self, tmp_path: Path):
        """Save a symbol library."""
        sexp = SExp("kicad_symbol_lib")
        sexp.add(SExp("version").add(20231120))
        output = tmp_path / "saved.kicad_sym"
        save_symbol_lib(sexp, output)
        assert output.exists()

    def test_save_symbol_lib_invalid(self, tmp_path: Path):
        """Error on saving non-symbol library."""
        sexp = SExp("not_kicad_symbol_lib")
        with pytest.raises(ValueError):
            save_symbol_lib(sexp, tmp_path / "bad.kicad_sym")
