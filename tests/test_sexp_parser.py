"""Tests for the S-expression parser module."""


import pytest

from kicad_tools.sexp.parser import SExp, parse_file, parse_string


class TestSExpAtoms:
    """Tests for SExp atom creation and properties."""

    def test_atom_from_string(self):
        """Create atom from string value."""
        atom = SExp(value="hello")
        assert atom.is_atom is True
        assert atom.is_list is False
        assert atom.value == "hello"

    def test_atom_from_int(self):
        """Create atom from integer value."""
        atom = SExp(value=42)
        assert atom.is_atom is True
        assert atom.value == 42

    def test_atom_from_float(self):
        """Create atom from float value."""
        atom = SExp(value=3.14)
        assert atom.is_atom is True
        assert atom.value == 3.14

    def test_both_name_and_value_raises(self):
        """Cannot have both name and value."""
        with pytest.raises(ValueError, match="both name and value"):
            SExp(name="test", value="value")


class TestSExpLists:
    """Tests for SExp list operations."""

    def test_list_creation(self):
        """Create list node."""
        node = SExp(name="test")
        assert node.name == "test"
        assert node.is_list is True
        assert node.is_atom is False

    def test_list_with_children(self):
        """Create list with children."""
        node = SExp(name="parent", children=[
            SExp(value="child1"),
            SExp(value="child2"),
        ])
        assert len(node.children) == 2

    def test_append_child(self):
        """Append child to list."""
        node = SExp(name="parent")
        child = SExp(value="child")
        node.append(child)
        assert len(node.children) == 1
        assert node.children[0] is child

    def test_remove_child(self):
        """Remove child from list."""
        child = SExp(value="child")
        node = SExp(name="parent", children=[child])
        result = node.remove(child)
        assert result is True
        assert len(node.children) == 0

    def test_remove_nonexistent(self):
        """Remove returns False for nonexistent child."""
        node = SExp(name="parent")
        other = SExp(value="other")
        result = node.remove(other)
        assert result is False


class TestSExpAccess:
    """Tests for SExp child access methods."""

    def test_getitem_by_index(self):
        """Access child by integer index."""
        node = SExp(name="parent", children=[
            SExp(value="first"),
            SExp(value="second"),
        ])
        assert node[0].value == "first"
        assert node[1].value == "second"

    def test_getitem_by_name(self):
        """Access child by name."""
        node = SExp(name="parent", children=[
            SExp(name="child", children=[SExp(value="data")]),
        ])
        child = node["child"]
        assert child.name == "child"

    def test_getitem_not_found(self):
        """KeyError when child not found."""
        node = SExp(name="parent", children=[
            SExp(name="child1"),
        ])
        with pytest.raises(KeyError) as exc:
            _ = node["missing"]
        assert "missing" in str(exc.value)
        assert "Available" in str(exc.value)

    def test_getitem_tuple(self):
        """Multi-level access with tuple key."""
        node = SExp(name="parent", children=[
            SExp(name="child", children=[
                SExp(name="grandchild", children=[SExp(value="data")]),
            ]),
        ])
        result = node["child", "grandchild"]
        assert result.name == "grandchild"

    def test_get_with_default(self):
        """get() returns default when not found."""
        node = SExp(name="parent")
        result = node.get("missing", "default")
        assert result == "default"

    def test_get_existing(self):
        """get() returns child when found."""
        node = SExp(name="parent", children=[
            SExp(name="child"),
        ])
        result = node.get("child")
        assert result.name == "child"


class TestSExpAtomMethods:
    """Tests for SExp atom manipulation methods."""

    def test_get_atoms(self):
        """Get all atom values."""
        node = SExp(name="test", children=[
            SExp(value=1),
            SExp(name="nested"),
            SExp(value=2),
        ])
        atoms = node.get_atoms()
        assert atoms == [1, 2]

    def test_get_first_atom(self):
        """Get first atom value."""
        node = SExp(name="test", children=[
            SExp(name="nested"),
            SExp(value="first"),
            SExp(value="second"),
        ])
        assert node.get_first_atom() == "first"

    def test_get_first_atom_none(self):
        """get_first_atom returns None when no atoms."""
        node = SExp(name="test", children=[
            SExp(name="nested"),
        ])
        assert node.get_first_atom() is None

    def test_set_atom(self):
        """Set atom at index."""
        node = SExp(name="test", children=[
            SExp(value="old"),
        ])
        node.set_atom(0, "new")
        assert node.children[0].value == "new"

    def test_set_atom_invalid_index(self):
        """set_atom raises on invalid index."""
        node = SExp(name="test", children=[])
        with pytest.raises(IndexError):
            node.set_atom(0, "value")


class TestSExpFind:
    """Tests for SExp find methods."""

    def test_find_by_name(self):
        """Find descendant by name."""
        node = SExp(name="root", children=[
            SExp(name="a", children=[
                SExp(name="target"),
            ]),
        ])
        result = node.find("target")
        assert result is not None
        assert result.name == "target"

    def test_find_not_found(self):
        """find returns None when not found."""
        node = SExp(name="root")
        result = node.find("missing")
        assert result is None

    def test_find_all(self):
        """Find all descendants by name."""
        node = SExp(name="root", children=[
            SExp(name="item", children=[SExp(value=1)]),
            SExp(name="item", children=[SExp(value=2)]),
            SExp(name="other"),
        ])
        results = node.find_all("item")
        assert len(results) == 2

    def test_find_with_attrs(self):
        """Find with attribute matching."""
        node = SExp(name="root", children=[
            SExp(name="item", children=[
                SExp(name="id", children=[SExp(value="a")]),
            ]),
            SExp(name="item", children=[
                SExp(name="id", children=[SExp(value="b")]),
            ]),
        ])
        result = node.find("item", id="b")
        assert result is not None

    def test_iter_all(self):
        """Iterate all descendants."""
        node = SExp(name="root", children=[
            SExp(name="a", children=[
                SExp(name="b"),
            ]),
        ])
        names = [n.name for n in node.iter_all() if n.name]
        assert names == ["root", "a", "b"]


class TestSExpBuilders:
    """Tests for SExp static builder methods."""

    def test_list_builder(self):
        """SExp.list() creates list node."""
        node = SExp.list("test", "arg1", 42)
        assert node.name == "test"
        assert len(node.children) == 2
        assert node.children[0].value == "arg1"
        assert node.children[1].value == 42

    def test_list_builder_with_sexp(self):
        """SExp.list() with SExp child."""
        child = SExp(name="child")
        node = SExp.list("parent", child)
        assert node.children[0] is child

    def test_atom_builder(self):
        """SExp.atom() creates atom node."""
        atom = SExp.atom("value")
        assert atom.is_atom is True
        assert atom.value == "value"


class TestParseString:
    """Tests for parse_string function."""

    def test_parse_simple_list(self):
        """Parse simple S-expression."""
        result = parse_string('(test "value")')
        assert result.name == "test"
        assert result.children[0].value == "value"

    def test_parse_nested(self):
        """Parse nested S-expression."""
        result = parse_string('(outer (inner 42))')
        assert result.name == "outer"
        inner = result["inner"]
        assert inner.children[0].value == 42

    def test_parse_numbers(self):
        """Parse numeric values."""
        result = parse_string("(nums 1 2.5 -3)")
        atoms = result.get_atoms()
        assert atoms == [1, 2.5, -3]

    def test_parse_quoted_string(self):
        """Parse quoted string."""
        result = parse_string('(text "hello world")')
        assert result.children[0].value == "hello world"

    def test_parse_unquoted_string(self):
        """Parse unquoted symbol."""
        result = parse_string("(symbol name)")
        assert result.children[0].value == "name"

    def test_parse_empty_list(self):
        """Parse empty list."""
        result = parse_string("()")
        assert result.name is None
        assert len(result.children) == 0

    def test_parse_deeply_nested(self):
        """Parse deeply nested structure."""
        result = parse_string("(a (b (c (d 1))))")
        d = result["b"]["c"]["d"]
        assert d.children[0].value == 1

    def test_parse_with_comments(self):
        """Parse with comments stripped (uses # style comments)."""
        result = parse_string('''
            (test # this is a comment
                "value") # another comment
        ''')
        assert result.name == "test"
        assert result.children[0].value == "value"


class TestSerialization:
    """Tests for to_string serialization."""

    def test_serialize_atom_string(self):
        """Serialize string atom - regular strings are quoted."""
        atom = SExp(value="hello")
        result = atom.to_string()
        assert result == '"hello"'

    def test_serialize_atom_number(self):
        """Serialize number atom."""
        atom = SExp(value=42)
        assert atom.to_string() == "42"

    def test_serialize_simple_list(self):
        """Serialize simple list."""
        # Use a string that needs quoting (contains a period)
        node = SExp.list("test", "my.value")
        result = node.to_string(compact=True)
        assert result == '(test "my.value")'

    def test_serialize_keyword_unquoted(self):
        """Keywords like 'value' are not quoted."""
        node = SExp.list("test", "value")
        result = node.to_string(compact=True)
        assert result == '(test value)'  # 'value' is a keyword

    def test_serialize_roundtrip(self):
        """Parse and serialize preserves structure."""
        original = '(test 1 "hello" (nested 2))'
        parsed = parse_string(original)
        serialized = parsed.to_string(compact=True)
        reparsed = parse_string(serialized)

        assert reparsed.name == "test"
        assert reparsed.get_atoms() == [1, "hello"]

    def test_serialize_empty_string(self):
        """Empty string is quoted."""
        atom = SExp(value="")
        result = atom.to_string()
        assert result == '""'

    def test_serialize_string_with_spaces(self):
        """Strings with spaces are quoted."""
        atom = SExp(value="hello world")
        result = atom.to_string()
        assert result == '"hello world"'


class TestParseFile:
    """Tests for parse_file function."""

    def test_parse_file(self, tmp_path):
        """Parse from file path."""
        sexp_file = tmp_path / "test.sexp"
        sexp_file.write_text('(test "data")')

        result = parse_file(sexp_file)
        assert result.name == "test"
        assert result.children[0].value == "data"

    def test_parse_file_string_path(self, tmp_path):
        """Parse from string path."""
        sexp_file = tmp_path / "test.sexp"
        sexp_file.write_text("(test 42)")

        result = parse_file(str(sexp_file))
        assert result.children[0].value == 42


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_parse_kicad_schematic_header(self):
        """Parse minimal KiCad schematic header."""
        sexp = '''(kicad_sch
            (version 20231120)
            (generator "test")
        )'''
        result = parse_string(sexp)
        assert result.name == "kicad_sch"
        version = result["version"]
        assert version.children[0].value == 20231120

    def test_parse_scientific_notation(self):
        """Parse scientific notation numbers."""
        result = parse_string("(value 1.5e-3)")
        assert result.children[0].value == pytest.approx(0.0015)

    def test_getitem_truncates_available(self):
        """Error message truncates long available lists."""
        children = [SExp(name=f"child{i}") for i in range(20)]
        node = SExp(name="parent", children=children)

        with pytest.raises(KeyError) as exc:
            _ = node["missing"]
        # Should show truncated list with "more" suffix
        assert "more" in str(exc.value)

    def test_getitem_no_children(self):
        """Error message handles no named children."""
        node = SExp(name="parent", children=[SExp(value=1)])
        with pytest.raises(KeyError) as exc:
            _ = node["missing"]
        assert "No named children" in str(exc.value)

    def test_find_all_empty(self):
        """find_all returns empty list when none match."""
        node = SExp(name="root")
        results = node.find_all("missing")
        assert results == []


class TestRoundTrip:
    """Tests for round-trip parsing and serialization."""

    def test_roundtrip_preserves_structure(self):
        """Parse → serialize → parse preserves structure."""
        original = '''(kicad_pcb
            (version 20240108)
            (generator "test")
            (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
            )
        )'''
        parsed = parse_string(original)
        serialized = parsed.to_string()
        reparsed = parse_string(serialized)

        assert reparsed.name == "kicad_pcb"
        assert reparsed["version"].get_first_atom() == 20240108
        assert reparsed["generator"].get_first_atom() == "test"
        layers = reparsed["layers"]
        assert len(layers.children) == 2

    def test_roundtrip_keywords_unquoted(self):
        """Keywords like signal, thru_hole, rect are not quoted."""
        sexp = '''(pad "1" thru_hole rect
            (at 0 0)
            (size 1.6 1.6)
            (layers "*.Cu" "*.Mask")
        )'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        # Keywords should not be quoted
        assert "thru_hole" in serialized
        assert '"thru_hole"' not in serialized
        assert "rect" in serialized
        assert '"rect"' not in serialized

    def test_roundtrip_layer_names_quoted(self):
        """Layer names like F.Cu are quoted."""
        sexp = '''(layer "F.Cu")'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        # Layer names with dots should be quoted
        assert '"F.Cu"' in serialized

    def test_roundtrip_uses_spaces_not_tabs(self):
        """Serialization uses 2-space indentation, not tabs."""
        sexp = '''(kicad_pcb
            (version 20240108)
            (general
                (thickness 1.6)
            )
        )'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        # Should not contain tabs
        assert '\t' not in serialized
        # Should use 2-space indentation
        assert '  (version' in serialized

    def test_roundtrip_fp_text_types_unquoted(self):
        """fp_text types like reference, value are not quoted."""
        sexp = '''(fp_text reference "U1"
            (at 0 0)
            (layer "F.SilkS")
        )'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        # 'reference' keyword should not be quoted
        assert "reference" in serialized
        assert '"reference"' not in serialized
        # But "U1" should be quoted
        assert '"U1"' in serialized

    def test_roundtrip_fill_types_unquoted(self):
        """Fill types like none, solid are not quoted."""
        sexp = '''(fill none)'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        assert "none" in serialized
        assert '"none"' not in serialized

    def test_roundtrip_boolean_values_unquoted(self):
        """Boolean values like yes, no are not quoted."""
        sexp = '''(legacy_teardrops no)'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()

        assert " no)" in serialized
        assert '"no"' not in serialized

    def test_roundtrip_preserves_numbers(self):
        """Numbers are preserved correctly through round-trip."""
        sexp = '''(at 125.0 147.5 90)'''
        parsed = parse_string(sexp)
        serialized = parsed.to_string()
        reparsed = parse_string(serialized)

        atoms = reparsed.get_atoms()
        assert atoms[0] == pytest.approx(125.0)
        assert atoms[1] == pytest.approx(147.5)
        assert atoms[2] == 90

    def test_roundtrip_pcb_file(self, tmp_path):
        """Round-trip a sample PCB structure."""
        pcb_content = '''(kicad_pcb
            (version 20240108)
            (generator "test")
            (general
                (thickness 1.6)
                (legacy_teardrops no)
            )
            (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
            )
            (footprint "Package_DIP:DIP-8"
                (layer "F.Cu")
                (at 125.0 147.0)
                (pad "1" thru_hole rect
                    (at 0 0)
                    (size 1.6 1.6)
                    (drill 0.8)
                    (layers "*.Cu" "*.Mask")
                )
            )
        )'''
        parsed = parse_string(pcb_content)

        # Save to temp file
        output_path = tmp_path / "test.kicad_pcb"
        output_path.write_text(parsed.to_string())

        # Reload and verify
        reparsed = parse_file(output_path)
        assert reparsed.name == "kicad_pcb"
        assert reparsed["version"].get_first_atom() == 20240108

        # Verify footprint structure
        footprint = reparsed.find("footprint")
        assert footprint is not None
        pad = footprint.find("pad")
        assert pad is not None
