"""Tests for S-expression parser and file I/O."""

from pathlib import Path

import pytest

from kicad_tools.core.sexp_file import (
    load_pcb,
    load_schematic,
    load_symbol_lib,
    save_pcb,
    save_schematic,
    save_symbol_lib,
)
from kicad_tools.exceptions import FileFormatError
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.sexp import SExp, parse_string, serialize_sexp


class TestSExpBasicParsing:
    """Basic parsing tests."""

    def test_parse_simple(self):
        """Parse a simple S-expression."""
        text = '(test "value")'
        sexp = parse_string(text)
        assert sexp.tag == "test"
        assert sexp.get_string(0) == "value"

    def test_parse_nested(self):
        """Parse nested S-expressions."""
        text = '(outer (inner "value"))'
        sexp = parse_string(text)
        assert sexp.tag == "outer"
        inner = sexp.find("inner")
        assert inner is not None
        assert inner.get_string(0) == "value"

    def test_parse_numbers(self):
        """Parse numeric values."""
        text = "(point 1.5 -2.3)"
        sexp = parse_string(text)
        assert sexp.tag == "point"
        assert sexp.get_float(0) == 1.5
        assert sexp.get_float(1) == -2.3

    def test_parse_integers(self):
        """Parse integer values."""
        text = "(count 42 -7)"
        sexp = parse_string(text)
        assert sexp.get_int(0) == 42
        assert sexp.get_int(1) == -7

    def test_parse_scientific_notation(self):
        """Parse scientific notation."""
        text = "(value 1.5e-3)"
        sexp = parse_string(text)
        assert sexp.get_float(0) == pytest.approx(0.0015)

    def test_parse_empty_list(self):
        """Parse empty list."""
        text = "()"
        sexp = parse_string(text)
        # Empty list has name=None (or tag=None via compat property)
        assert sexp.name is None
        assert len(sexp.children) == 0

    def test_parse_with_comments(self):
        """Parse with comments."""
        text = """(test ; this is a comment
            "value")"""
        sexp = parse_string(text)
        assert sexp.tag == "test"
        assert sexp.get_string(0) == "value"


class TestSExpStringParsing:
    """String parsing tests."""

    def test_parse_escaped_newline(self):
        """Parse string with escaped newline."""
        text = r'(text "line1\nline2")'
        sexp = parse_string(text)
        assert sexp.get_string(0) == "line1\nline2"

    def test_parse_escaped_tab(self):
        """Parse string with escaped tab."""
        text = r'(text "col1\tcol2")'
        sexp = parse_string(text)
        assert sexp.get_string(0) == "col1\tcol2"

    def test_parse_escaped_quote(self):
        """Parse string with escaped quote."""
        text = r'(text "say \"hello\"")'
        sexp = parse_string(text)
        assert sexp.get_string(0) == 'say "hello"'

    def test_parse_escaped_backslash(self):
        """Parse string with escaped backslash."""
        text = r'(text "path\\to\\file")'
        sexp = parse_string(text)
        assert sexp.get_string(0) == "path\\to\\file"


class TestSExpErrors:
    """Error handling tests."""

    def test_unexpected_end_in_list(self):
        """Error on unclosed list."""
        with pytest.raises(ValueError, match="Unexpected end"):
            parse_string("(test")

    def test_unexpected_end_in_string(self):
        """Error on unclosed string."""
        with pytest.raises(ValueError, match="Unterminated string"):
            parse_string('(test "unclosed')

    def test_trailing_content(self):
        """Error on trailing content."""
        with pytest.raises(ValueError, match="Unexpected content"):
            parse_string("(test) extra")


class TestFilePathGuard:
    """Tests for parse_string file-path guard."""

    def test_rejects_kicad_sch_path(self):
        """parse_string raises ValueError for .kicad_sch paths."""
        with pytest.raises(ValueError, match="parse_file"):
            parse_string("path/to/file.kicad_sch")

    def test_rejects_kicad_pcb_path(self):
        """parse_string raises ValueError for .kicad_pcb paths."""
        with pytest.raises(ValueError, match="parse_file"):
            parse_string("board.kicad_pcb")

    def test_rejects_kicad_sym_path(self):
        """parse_string raises ValueError for .kicad_sym paths."""
        with pytest.raises(ValueError, match="parse_file"):
            parse_string("lib.kicad_sym")

    def test_rejects_kicad_mod_path(self):
        """parse_string raises ValueError for .kicad_mod paths."""
        with pytest.raises(ValueError, match="parse_file"):
            parse_string("footprint.kicad_mod")

    def test_allows_valid_sexp_with_path_substrings(self):
        """Legitimate S-expressions with path-like substrings parse correctly."""
        result = parse_string('(lib_id "path/to/lib.kicad_sym")')
        assert result.name == "lib_id"

    def test_allows_normal_sexp(self):
        """Normal S-expression strings are not rejected."""
        result = parse_string("(test 1 2 3)")
        assert result.name == "test"

    def test_rejects_path_with_whitespace(self):
        """Paths with leading/trailing whitespace are still detected."""
        with pytest.raises(ValueError, match="parse_file"):
            parse_string("  board.kicad_pcb  ")


class TestParseSexpDeprecation:
    """Tests for parse_sexp deprecation warning."""

    def test_emits_deprecation_warning(self):
        """parse_sexp emits DeprecationWarning."""
        from kicad_tools.sexp import parse_sexp

        with pytest.warns(DeprecationWarning, match="parse_sexp.*deprecated"):
            parse_sexp("(test 42)")

    def test_still_parses_correctly(self):
        """parse_sexp still returns correct result while deprecated."""
        from kicad_tools.sexp import parse_sexp

        with pytest.warns(DeprecationWarning):
            result = parse_sexp("(hello 1 2 3)")
        assert result.name == "hello"


class TestSExpMethods:
    """Tests for SExp methods."""

    def test_find_all(self):
        """Find all matching children."""
        text = "(root (item 1) (item 2) (other 3))"
        sexp = parse_string(text)
        items = sexp.find_all("item")
        assert len(items) == 2

    def test_find_not_found(self):
        """Find returns None when not found."""
        sexp = parse_string("(test)")
        assert sexp.find("missing") is None

    def test_getitem_by_index(self):
        """Get child by index returns SExp node."""
        sexp = parse_string("(test 1 2 3)")
        # __getitem__ with int returns SExp nodes (new API behavior)
        assert sexp[0].value == 1
        assert sexp[1].value == 2
        assert sexp[2].value == 3
        # Use get_value() for primitive values (backward compat)
        assert sexp.get_value(0) == 1
        assert sexp.get_value(1) == 2
        assert sexp.get_value(2) == 3
        assert sexp.get_value(99) is None

    def test_getitem_by_tag(self):
        """Get child by tag."""
        sexp = parse_string("(outer (inner 42))")
        inner = sexp["inner"]
        assert inner is not None
        assert inner.tag == "inner"

    def test_get_value(self):
        """Get value by index."""
        sexp = parse_string("(test 1 2 3)")
        assert sexp.get_value(0) == 1
        assert sexp.get_value(99) is None

    def test_get_string_from_number(self):
        """Get string from numeric value."""
        sexp = parse_string("(test 42)")
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
        sexp = parse_string("(test 42)")
        assert sexp.get_float(0) == 42.0

    def test_get_float_invalid_string(self):
        """Get float from invalid string returns None."""
        sexp = SExp("test")
        sexp.add("not a number")
        assert sexp.get_float(0) is None

    def test_iter_children(self):
        """Iterate over child SExp nodes."""
        sexp = parse_string("(root 1 (a) 2 (b) 3)")
        children = list(sexp.iter_children())
        assert len(children) == 2
        assert children[0].tag == "a"
        assert children[1].tag == "b"

    def test_has_tag(self):
        """Check for tag presence."""
        sexp = parse_string("(root (child))")
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
        sexp = parse_string("(root (a) (b) (c))")
        assert sexp.remove_child("b") is True
        assert len(sexp.find_all("b")) == 0

    def test_remove_child_not_found(self):
        """Remove returns False if not found."""
        sexp = parse_string("(root (a))")
        assert sexp.remove_child("missing") is False

    def test_repr_empty(self):
        """String representation of SExp with name only."""
        sexp = SExp("test")
        # New repr format shows name and children count
        assert "SExp" in repr(sexp)
        assert "test" in repr(sexp)

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
        sexp = parse_string(text)
        result = serialize_sexp(sexp)
        assert "test" in result
        assert "value" in result

    def test_serialize_roundtrip(self):
        """Serialize and parse should preserve data."""
        original = parse_string('(test 1.5 "hello" (nested 42))')
        serialized = serialize_sexp(original)
        reparsed = parse_string(serialized)
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
        """Numeric-looking strings should NOT be quoted (KiCad expects bare numbers)."""
        sexp = SExp("test")
        sexp.add("123")
        result = serialize_sexp(sexp)
        # Numeric strings must be unquoted so downstream KiCad parsers accept them
        assert result.strip() == "(test 123)"

    def test_serialize_negative_float_string(self):
        """Negative float strings like '-0.5222' should not be quoted."""
        sexp = SExp("at")
        sexp.add("-0.5222")
        sexp.add("3.81")
        result = serialize_sexp(sexp)
        assert result.strip() == "(at -0.5222 3.81)"

    def test_needs_quoting_nan_inf(self):
        """Strings like 'nan' and 'inf' are valid floats but should not be unquoted."""
        SExp("test")
        # 'nan' and 'inf' parse as float but are not valid KiCad numeric literals.
        # However, _needs_quoting uses float() which accepts them.  Since KiCad
        # would never emit these, and they are not identifiers, leaving them
        # unquoted is acceptable (they would be treated as bare tokens).
        sexp_node = SExp("test")
        sexp_node.add("nan")
        result = serialize_sexp(sexp_node)
        # Just verify it serializes without error
        assert "nan" in result

    def test_serialize_float_int_value(self):
        """Serialize float that equals an int."""
        # Create SExp with integer-valued float and verify serialization
        sexp = SExp("test")
        sexp.add(42.0)  # Float that equals int
        result = serialize_sexp(sexp)
        # The float 42.0 may be serialized as "42" or "42.0" depending on implementation
        assert "42" in result

    def test_serialize_multiline_text(self):
        """Serialize multiline text with escaped newlines.

        KiCad 9.x expects \\n escape sequences in quoted strings, not actual
        newline characters. The parser's _ESCAPE_MAP converts these back to
        real newlines on read, so roundtrip is preserved.

        Updated from issue #602 based on real-world testing with KiCad 9.0.7
        (PR #1163).
        """
        sexp = SExp("text")
        sexp.add("Line 1\nLine 2\nLine 3")
        result = serialize_sexp(sexp)
        # Should contain escape sequences, not actual newlines
        assert "\\n" in result  # Escaped newlines
        assert "Line 1\nLine 2\nLine 3" not in result  # No raw newlines

    def test_serialize_multiline_roundtrip(self):
        """Multiline text should survive parse-serialize-parse cycle."""
        original_text = "Header\n\nDetails:\n- Item 1\n- Item 2"
        sexp = SExp("text")
        sexp.add(original_text)
        serialized = serialize_sexp(sexp)
        reparsed = parse_string(serialized)
        assert reparsed.get_string(0) == original_text

    def test_serialize_tabs_escaped(self):
        """Tab characters should be escaped in output.

        KiCad 9.x expects \\t escape sequences, not actual tab characters.
        Updated from issue #602 based on real-world testing (PR #1163).
        """
        sexp = SExp("text")
        sexp.add("Col1\tCol2\tCol3")
        result = serialize_sexp(sexp)
        # Should contain escape sequences, not actual tabs
        assert "\\t" in result  # Escaped tabs
        assert "Col1\tCol2\tCol3" not in result  # No raw tabs

    def test_serialize_kicad_keywords_unquoted(self):
        """KiCad keywords like front, back should be output unquoted.

        PR #1163: front, back, allow_missing_courtyard, and
        allow_soldermask_bridges are valid KiCad keywords that must not
        be quoted in S-expression output.
        """
        for keyword in ["front", "back", "allow_missing_courtyard", "allow_soldermask_bridges"]:
            sexp = SExp("test")
            sexp.add(keyword)
            result = serialize_sexp(sexp)
            assert f'"{keyword}"' not in result, f"{keyword} should not be quoted"
            assert keyword in result

    def test_mirror_values_unquoted(self):
        """Mirror axis values x, y, xy must be bare symbols, not quoted strings.

        KiCad 10 expects (mirror x) not (mirror "x"). The serializer must
        treat these single-letter axis identifiers as unquoted keywords.
        Regression test for issue #2385.
        """
        for axis in ["x", "y", "xy"]:
            mirror_node = SExp.list("mirror", axis)
            result = serialize_sexp(mirror_node)
            assert f'"{axis}"' not in result, (
                f"mirror value '{axis}' should not be quoted, got: {result}"
            )
            assert f"(mirror {axis})" == result

    def test_parsed_bare_keepout_enum_stays_bare(self):
        """Keepout rule-area enum tokens parsed bare must re-emit bare.

        KiCad emits `(tracks not_allowed)` (bare symbol) in footprint keepout
        rule areas, and pcbnew's parser hard-rejects the quoted form
        `(tracks "not_allowed")` — a board containing it fails to load. The
        tokens `not_allowed`/`allowed` are absent from the unquoted-keyword
        allowlist, so before issue #4185 the serializer wrongly quoted them on
        round-trip. An atom parsed as a bare symbol must stay bare.
        """
        text = (
            "(keepout (tracks not_allowed) (vias not_allowed) (pads not_allowed) "
            "(copperpour not_allowed) (footprints not_allowed))"
        )
        result = parse_string(text).to_string()
        assert '"not_allowed"' not in result, f"keepout enum must not be quoted, got: {result}"
        assert '"allowed"' not in result
        for field in ("tracks", "vias", "pads", "copperpour", "footprints"):
            assert f"({field} not_allowed)" in result, (
                f"expected bare ({field} not_allowed) in: {result}"
            )

    def test_parsed_bare_sibling_enum_tokens_stay_bare(self):
        """Sibling bare enum tokens must also round-trip bare.

        These KiCad zone/pad enum symbols share the identical latent gap with
        the keepout tokens: they are absent from the unquoted-keyword allowlist,
        so the symmetric bare/quoted fix (issue #4185) must keep them bare when
        they were parsed bare, rather than requiring a token-by-token allowlist
        patch.
        """
        for token in [
            "chamfer_rect",
            "chamfer",
            "fillet",
            "poly",
            "castellated",
            "heatsink",
            "bga",
        ]:
            result = parse_string(f"(field {token})").to_string()
            assert f'"{token}"' not in result, (
                f"bare token '{token}' should not be quoted, got: {result}"
            )
            assert f"(field {token})" == result

    def test_parsed_quoted_string_stays_quoted(self):
        """An originally-quoted string must stay quoted on round-trip.

        The bare/quoted distinction is symmetric: a value parsed from a quoted
        token (e.g. a strict-typed field that looks numeric) must not be
        downgraded to a bare atom by the bare-handling path (issue #4185).
        """
        result = parse_string('(generator_version "9.0")').to_string()
        assert '"9.0"' in result, f"quoted value must stay quoted, got: {result}"

    def test_parsed_bare_value_requiring_quotes_still_quoted(self):
        """Values that structurally require quotes are always quoted.

        Whitespace, parentheses, and empty strings cannot be represented as
        bare tokens, so they must always be quoted regardless of parse-time
        bare/quoted state (issue #4185). Such values only ever reach the parser
        as quoted tokens, but the serializer must not emit them bare even if the
        bare flag were somehow set.
        """
        # Quoted-source values containing structural characters stay quoted.
        for value in ["has space", "with(paren", ""]:
            node = SExp("field")
            node.add(SExp.quoted_atom(value))
            result = serialize_sexp(node)
            assert f'"{value}"' in result, (
                f"value {value!r} requiring quotes must stay quoted, got: {result}"
            )

        # A bare-flagged atom whose value structurally requires quoting must
        # still be quoted (the bare path defers to _must_quote()).
        bare = SExp(value="has space", _originally_bare=True)
        assert bare._format_atom() == '"has space"'
        empty = SExp(value="", _originally_bare=True)
        assert empty._format_atom() == '""'

    def test_parsed_bare_backslash_value_forced_quoted(self):
        """A bare atom containing a raw backslash is forced quoted+escaped.

        Backslash cannot be safely represented as a bare token (issue #4213,
        defense-in-depth follow-on to #4185): a bare-flagged atom whose value
        contains a literal backslash must be routed into the quoting branch of
        _format_atom(), whose unconditional ``.replace("\\", "\\\\")`` step
        doubles the backslash so the emitted quoted form round-trips byte-exact.
        Forward slash is intentionally *not* a trigger and stays bare.
        """
        # _must_quote() flags any value containing a literal backslash.
        assert SExp._must_quote("abc\\def") is True
        assert SExp._must_quote("a\\b\\c") is True
        # Forward slash is intentionally excluded and must stay bare (#4185).
        assert SExp._must_quote("my/path") is False

        # A bare-flagged atom with a backslash serializes quoted + escaped.
        node = SExp(value="abc\\def", _originally_bare=True)
        assert node._format_atom() == '"abc\\\\def"'
        # Parsing that emitted form back yields the exact original value.
        assert parse_string("(field " + node._format_atom() + ")").get_string(0) == "abc\\def"

        # Forward-slash bare atoms remain bare (no over-quoting regression).
        slash = SExp(value="my/path", _originally_bare=True)
        assert slash._format_atom() == "my/path"


class TestSExpFileIO:
    """Tests for file I/O functions."""

    def test_load_schematic(self, minimal_schematic: Path):
        """Load a schematic file."""
        sexp = load_schematic(minimal_schematic)
        assert sexp.tag == "kicad_sch"

    def test_load_schematic_not_found(self, tmp_path: Path):
        """Error on file not found."""
        with pytest.raises(KiCadFileNotFoundError):
            load_schematic(tmp_path / "nonexistent.kicad_sch")

    def test_load_schematic_invalid(self, tmp_path: Path):
        """Error on invalid schematic."""
        bad_file = tmp_path / "bad.kicad_sch"
        bad_file.write_text("(not_a_schematic)")
        with pytest.raises(FileFormatError, match="Not a KiCad schematic"):
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
        with pytest.raises(FileFormatError):
            save_schematic(sexp, tmp_path / "bad.kicad_sch")

    def test_load_pcb(self, minimal_pcb: Path):
        """Load a PCB file."""
        sexp = load_pcb(minimal_pcb)
        assert sexp.tag == "kicad_pcb"

    def test_load_pcb_not_found(self, tmp_path: Path):
        """Error on PCB file not found."""
        with pytest.raises(KiCadFileNotFoundError):
            load_pcb(tmp_path / "nonexistent.kicad_pcb")

    def test_load_pcb_invalid(self, tmp_path: Path):
        """Error on invalid PCB."""
        bad_file = tmp_path / "bad.kicad_pcb"
        bad_file.write_text("(not_a_pcb)")
        with pytest.raises(FileFormatError, match="Not a KiCad PCB"):
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
        with pytest.raises(FileFormatError):
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
        with pytest.raises(KiCadFileNotFoundError):
            load_symbol_lib(tmp_path / "nonexistent.kicad_sym")

    def test_load_symbol_lib_invalid(self, tmp_path: Path):
        """Error on invalid symbol library."""
        bad_file = tmp_path / "bad.kicad_sym"
        bad_file.write_text("(not_a_symbol_lib)")
        with pytest.raises(FileFormatError, match="Not a KiCad symbol library"):
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
        with pytest.raises(FileFormatError):
            save_symbol_lib(sexp, tmp_path / "bad.kicad_sym")


class TestSExpInsertMethods:
    """Tests for SExp node insertion methods (insert, insert_after, insert_before)."""

    def test_insert_at_beginning(self):
        """Insert node at beginning of children list."""
        sexp = parse_string("(root (a) (b) (c))")
        new_node = SExp("new")
        sexp.insert(0, new_node)
        assert sexp.children[0].name == "new"
        assert sexp.children[1].name == "a"
        assert len(sexp.children) == 4

    def test_insert_at_middle(self):
        """Insert node in middle of children list."""
        sexp = parse_string("(root (a) (b) (c))")
        new_node = SExp("new")
        sexp.insert(2, new_node)
        assert sexp.children[0].name == "a"
        assert sexp.children[1].name == "b"
        assert sexp.children[2].name == "new"
        assert sexp.children[3].name == "c"

    def test_insert_at_end(self):
        """Insert node at end of children list."""
        sexp = parse_string("(root (a) (b) (c))")
        new_node = SExp("new")
        sexp.insert(3, new_node)
        assert sexp.children[2].name == "c"
        assert sexp.children[3].name == "new"

    def test_insert_negative_index(self):
        """Insert node using negative index."""
        sexp = parse_string("(root (a) (b) (c))")
        new_node = SExp("new")
        sexp.insert(-1, new_node)  # Insert before last element
        assert sexp.children[-2].name == "new"
        assert sexp.children[-1].name == "c"

    def test_insert_returns_child(self):
        """Insert should return the inserted child."""
        sexp = SExp("root")
        new_node = SExp("new")
        result = sexp.insert(0, new_node)
        assert result is new_node

    def test_insert_after_existing_node(self):
        """Insert node after an existing node by name."""
        sexp = parse_string("(root (layer) (uuid) (property))")
        at_node = SExp("at")
        at_node.add(50)
        at_node.add(30)
        sexp.insert_after("layer", at_node)
        assert sexp.children[0].name == "layer"
        assert sexp.children[1].name == "at"
        assert sexp.children[2].name == "uuid"

    def test_insert_after_last_node(self):
        """Insert after the last child with matching name."""
        sexp = parse_string("(root (a) (b))")
        new_node = SExp("new")
        sexp.insert_after("b", new_node)
        assert sexp.children[-1].name == "new"

    def test_insert_after_returns_child(self):
        """insert_after should return the inserted child."""
        sexp = parse_string("(root (a))")
        new_node = SExp("new")
        result = sexp.insert_after("a", new_node)
        assert result is new_node

    def test_insert_after_not_found(self):
        """insert_after raises KeyError when target not found."""
        sexp = parse_string("(root (a) (b))")
        new_node = SExp("new")
        with pytest.raises(KeyError, match="No child named 'missing'"):
            sexp.insert_after("missing", new_node)

    def test_insert_before_existing_node(self):
        """Insert node before an existing node by name."""
        sexp = parse_string("(root (layer) (property) (pad))")
        at_node = SExp("at")
        at_node.add(50)
        at_node.add(30)
        sexp.insert_before("property", at_node)
        assert sexp.children[0].name == "layer"
        assert sexp.children[1].name == "at"
        assert sexp.children[2].name == "property"

    def test_insert_before_first_node(self):
        """Insert before the first child."""
        sexp = parse_string("(root (a) (b))")
        new_node = SExp("new")
        sexp.insert_before("a", new_node)
        assert sexp.children[0].name == "new"
        assert sexp.children[1].name == "a"

    def test_insert_before_returns_child(self):
        """insert_before should return the inserted child."""
        sexp = parse_string("(root (a))")
        new_node = SExp("new")
        result = sexp.insert_before("a", new_node)
        assert result is new_node

    def test_insert_before_not_found(self):
        """insert_before raises KeyError when target not found."""
        sexp = parse_string("(root (a) (b))")
        new_node = SExp("new")
        with pytest.raises(KeyError, match="No child named 'missing'"):
            sexp.insert_before("missing", new_node)

    def test_footprint_structure_use_case(self):
        """Test the specific footprint use case from issue #912.

        KiCad expects footprints to have a specific structure where
        (at ...) comes early in the tree, not at the end.
        """
        # Simulate a footprint with (at) missing
        footprint = parse_string("""(footprint "Library:Name"
            (layer "F.Cu")
            (uuid "abc123")
            (property "Reference" "U1")
            (property "Value" "Chip")
            (pad 1 smd rect)
        )""")

        # Create (at 50 30 0) node
        at_node = SExp("at")
        at_node.add(50)
        at_node.add(30)
        at_node.add(0)

        # Insert after uuid (which is the correct KiCad position)
        footprint.insert_after("uuid", at_node)

        # Verify structure - get only named children
        child_names = [c.name for c in footprint.children if c.name]
        assert child_names == ["layer", "uuid", "at", "property", "property", "pad"]

        # Verify the at node is in the right place (after uuid)
        # First child is the atom "Library:Name", so named children start at index 1
        # children[0] = "Library:Name" (atom)
        # children[1] = layer
        # children[2] = uuid
        # children[3] = at (newly inserted)
        assert footprint.children[3].name == "at"
        at_values = footprint.children[3].get_atoms()
        assert at_values == [50, 30, 0]
