"""Regression tests for issue #7648: KiCad string escaping in writers.

``sch set-footprint`` (and the sibling ``set_value_text`` /
``set_lib_id_text`` editors) used the new value as a ``re.sub`` replacement
template, wrote it unescaped, and round-tripped sheets through universal
newlines.  A raw LF inside a quoted string makes KiCad silently drop a
hierarchical sub-sheet from the design (``kicad-cli`` netlist component
count falls, exit code 0), so every quoted string we write must be escaped
the way KiCad's ``OUTPUTFORMATTER::Quotes`` does (``\\``, ``\"``, ``\n``,
``\r``) and files must be read and written with ``newline=""``.

These tests assert on our own written text and parser; they do not need
KiCad installed.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import kicad_tools.cli.sch_set_footprint as sch_set_footprint_mod
from kicad_tools.cli.modify_schematic import (
    parse_check,
    set_footprint_text,
    set_lib_id_text,
    set_symbol_flag_text,
    set_value_text,
)
from kicad_tools.cli.sch_set_footprint import run_set_footprint
from kicad_tools.cli.sch_set_value import run_set_value
from kicad_tools.cli.sch_wire_common import _collect_schematic_files
from kicad_tools.library import create_chip
from kicad_tools.library.footprint import Footprint, Pad
from kicad_tools.sexp import (
    Document,
    SExp,
    escape_string,
    parse_file,
    parse_string,
    quote_string,
    read_text_exact,
)

HIERARCHICAL_FIXTURES = Path(__file__).parent / "fixtures" / "hierarchical"

# Every row of the issue's table, plus the unescaped-text cases it lists.
HOSTILE_IDS = [
    pytest.param(r"Lib:FP\nx", id="backslash-n"),
    pytest.param(r"Lib:FP\012x", id="octal-012"),
    pytest.param(r"Lib:FP\rx", id="backslash-r"),
    pytest.param(r"Lib:FP\g<0>x", id="group-ref"),
    pytest.param(r"Lib:FP\qx", id="bad-escape"),
    pytest.param('Lib:FP"x', id="embedded-quote"),
    pytest.param("Lib:FP\nx", id="real-LF"),
    pytest.param("Lib:FP\rx", id="real-CR"),
    pytest.param("Lib:FP\\", id="trailing-backslash"),
    pytest.param("", id="empty"),
]

MINIMAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(on_board yes)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C1"
\t\t\t(at 120 48 0)
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t)
\t\t(property "Footprint" "Old\\"quoted"
\t\t\t(at 120 54 0)
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t)
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raw_newlines_in_strings(text: str) -> list[int]:
    """Offsets of every raw CR or LF that sits inside a quoted string."""
    hits: list[int] = []
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            elif ch in "\r\n":
                hits.append(i)
        elif ch == '"':
            in_string = True
        i += 1
    return hits


def _placed_symbols(root: SExp) -> list[SExp]:
    return [c for c in root.children if c.name == "symbol" and c.find_child("lib_id")]


def _symbol(root: SExp, reference: str) -> SExp:
    for sym in _placed_symbols(root):
        for prop in sym.children:
            if prop.name == "property" and prop.get_atoms()[:2] == ["Reference", reference]:
                return sym
    raise AssertionError(f"symbol {reference} not found")


def _property(sym: SExp, key: str) -> str:
    for prop in sym.children:
        atoms = prop.get_atoms() if prop.name == "property" else []
        if atoms and atoms[0] == key:
            return atoms[1]
    raise AssertionError(f"property {key} not found")


def _component_count(root_sheet: Path) -> int:
    """Parse-based stand-in for ``kicad-cli sch export netlist`` <comp> count.

    Counts placed, non-power symbols across every sheet of the hierarchy.
    """
    count = 0
    for sheet in _collect_schematic_files(root_sheet):
        for sym in _placed_symbols(parse_file(sheet)):
            if not str(_property(sym, "Reference")).startswith("#"):
                count += 1
    return count


def _copy_hierarchy(tmp_path: Path) -> Path:
    for f in HIERARCHICAL_FIXTURES.glob("*.kicad_sch"):
        shutil.copy(f, tmp_path / f.name)
    return tmp_path / "root.kicad_sch"


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


class TestQuoteHelper:
    @pytest.mark.parametrize(
        "value",
        ["plain", 'a"b', "a\\b", "a\nb", "a\rb", "a\r\nb", "\\n", '\\\\"', "", "tab\there"],
    )
    def test_quote_string_round_trips_through_parser(self, value):
        quoted = quote_string(value)
        assert "\n" not in quoted and "\r" not in quoted
        assert parse_string(f"(x {quoted})").get_atoms() == [value]

    def test_escape_string_matches_kicad_quotes(self):
        assert escape_string('\\"\n\r') == '\\\\\\"\\n\\r'
        # KiCad's Quotes leaves tabs alone.
        assert escape_string("\t") == "\t"


# ---------------------------------------------------------------------------
# 1. Text editors: no template interpretation, KiCad escaping
# ---------------------------------------------------------------------------


class TestTextEditors:
    @pytest.mark.parametrize("new_id", HOSTILE_IDS)
    def test_set_footprint_text_writes_literal_value(self, new_id):
        out, ok, _ = set_footprint_text(MINIMAL_SCHEMATIC, "R1", new_id)
        assert ok
        assert _raw_newlines_in_strings(out) == []
        root = parse_string(out)
        assert _property(_symbol(root, "R1"), "Footprint") == new_id
        # The other symbol is untouched.
        assert _property(_symbol(root, "C1"), "Footprint") == 'Old"quoted'

    @pytest.mark.parametrize("new_value", HOSTILE_IDS)
    def test_set_value_text_writes_literal_value(self, new_value):
        out, ok, _ = set_value_text(MINIMAL_SCHEMATIC, "R1", new_value)
        assert ok
        assert _raw_newlines_in_strings(out) == []
        assert _property(_symbol(parse_string(out), "R1"), "Value") == new_value

    @pytest.mark.parametrize("new_lib_id", HOSTILE_IDS)
    def test_set_lib_id_text_writes_literal_value(self, new_lib_id):
        out, ok, _ = set_lib_id_text(MINIMAL_SCHEMATIC, "R1", new_lib_id)
        assert ok
        assert _raw_newlines_in_strings(out) == []
        sym = _symbol(parse_string(out), "R1")
        assert sym.find_child("lib_id").get_atoms() == [new_lib_id]

    def test_replaces_whole_old_value_with_escaped_quote(self):
        """An old value holding ``\\"`` is replaced as one string, not split."""
        out, ok, _ = set_footprint_text(MINIMAL_SCHEMATIC, "C1", "New:FP")
        assert ok
        assert 'quoted"' not in out
        assert _property(_symbol(parse_string(out), "C1"), "Footprint") == "New:FP"

    def test_setting_the_same_value_succeeds(self):
        out, ok, _ = set_footprint_text(MINIMAL_SCHEMATIC, "R1", "Resistor_SMD:R_0402_1005Metric")
        assert ok
        assert out == MINIMAL_SCHEMATIC

    def test_set_symbol_flag_text_still_splices(self):
        out, ok, _ = set_symbol_flag_text(MINIMAL_SCHEMATIC, "R1", "on_board", "no")
        assert ok
        assert "(on_board no)" in out
        assert parse_check(out) is None

    def test_parse_check_reports_broken_text(self):
        assert parse_check(MINIMAL_SCHEMATIC) is None
        assert parse_check(MINIMAL_SCHEMATIC.rstrip() + "(") is not None
        assert parse_check("(kicad_pcb)") is not None

    def test_parse_check_reads_back_expected_values(self):
        ok = [("R1", "Value", "10k"), ("C1", "Footprint", 'Old"quoted')]
        assert parse_check(MINIMAL_SCHEMATIC, ok) is None
        problem = parse_check(MINIMAL_SCHEMATIC, [("R1", "Value", "22k")])
        assert problem is not None and "R1 Value" in problem


# ---------------------------------------------------------------------------
# 1b. run_set_footprint / run_set_value on a hierarchy
# ---------------------------------------------------------------------------


class TestRunSetFootprintHierarchy:
    @pytest.mark.parametrize("new_id", HOSTILE_IDS)
    def test_hostile_id_keeps_every_sheet(self, tmp_path, new_id):
        root = _copy_hierarchy(tmp_path)
        before = _component_count(root)
        assert before == 6

        rc = run_set_footprint(
            root, mapping={"R2": new_id}, dry_run=False, backup=False, validate=False
        )
        assert rc == 0

        sub = tmp_path / "sub_a.kicad_sch"
        text = read_text_exact(sub)
        assert _raw_newlines_in_strings(text) == []
        assert _property(_symbol(parse_string(text), "R2"), "Footprint") == new_id
        assert _component_count(root) == before

    def test_raw_cr_in_subsheet_survives_round_trip(self, tmp_path):
        """A raw CR already in a sub-sheet string is kept byte-for-byte."""
        root = _copy_hierarchy(tmp_path)
        sub = tmp_path / "sub_a.kicad_sch"
        original = sub.read_bytes()
        old_c1 = b'"Capacitor_SMD:C_0402_1005Metric"'
        assert old_c1 in original
        with_cr = original.replace(old_c1, b'"Capacitor_SMD:C_0402\r1005Metric"', 1)
        sub.write_bytes(with_cr)

        rc = run_set_footprint(
            root, mapping={"R2": "Lib:New"}, dry_run=False, backup=False, validate=False
        )
        assert rc == 0

        after = sub.read_bytes()
        assert b'"Capacitor_SMD:C_0402\r1005Metric"' in after
        # The only change is R2's footprint.
        old_r2 = b'(property "Footprint" "Resistor_SMD:R_0402_1005Metric"'
        assert with_cr.count(old_r2) == 1
        assert after == with_cr.replace(old_r2, b'(property "Footprint" "Lib:New"', 1)
        assert _component_count(root) == 6

    def test_crlf_sheet_keeps_its_line_endings(self, tmp_path):
        sch = tmp_path / "crlf.kicad_sch"
        sch.write_bytes(MINIMAL_SCHEMATIC.replace("\n", "\r\n").encode())

        rc = run_set_footprint(
            sch, mapping={"R1": "Lib:FP\nx"}, dry_run=False, backup=False, validate=False
        )
        assert rc == 0

        data = sch.read_bytes()
        assert data.count(b"\n") == data.count(b"\r\n")
        text = data.decode()
        assert _property(_symbol(parse_string(text), "R1"), "Footprint") == "Lib:FP\nx"

    def test_unparseable_edit_is_refused(self, tmp_path, monkeypatch):
        sch = tmp_path / "test.kicad_sch"
        sch.write_text(MINIMAL_SCHEMATIC)

        def broken(text, reference, new_footprint):
            return text + "(", True, "broken on purpose"

        monkeypatch.setattr(sch_set_footprint_mod, "set_footprint_text", broken)
        rc = run_set_footprint(
            sch, mapping={"R1": "Lib:FP"}, dry_run=False, backup=False, validate=False
        )
        assert rc == 1
        assert sch.read_text() == MINIMAL_SCHEMATIC

    def test_mangled_but_parseable_splice_is_refused(self, tmp_path, monkeypatch):
        """The pre-#7648 template splice of ``\\g<0>`` still parses; the
        read-back check catches it and nothing is written."""
        import re

        sch = tmp_path / "test.kicad_sch"
        sch.write_text(MINIMAL_SCHEMATIC)

        def old_template_splice(text, reference, new_footprint):
            pattern = re.compile(r'(\(property "Footprint" )"[^"]*"')
            return pattern.sub(rf'\g<1>"{new_footprint}"', text, count=1), True, "old"

        monkeypatch.setattr(sch_set_footprint_mod, "set_footprint_text", old_template_splice)
        rc = run_set_footprint(
            sch, mapping={"R1": r"Lib:FP\g<0>x"}, dry_run=False, backup=False, validate=False
        )
        assert rc == 1
        assert sch.read_text() == MINIMAL_SCHEMATIC

    @pytest.mark.parametrize("new_value", HOSTILE_IDS)
    def test_run_set_value_hostile_value(self, tmp_path, new_value):
        root = _copy_hierarchy(tmp_path)
        rc = run_set_value(root, ref="R2", value=new_value, dry_run=False, backup=False)
        assert rc == 0
        text = read_text_exact(tmp_path / "sub_a.kicad_sch")
        assert _raw_newlines_in_strings(text) == []
        assert _property(_symbol(parse_string(text), "R2"), "Value") == new_value
        assert _component_count(root) == 6


# ---------------------------------------------------------------------------
# 2. SExp.to_string escapes \r; parse_file keeps a raw CR
# ---------------------------------------------------------------------------


class TestSExpCarriageReturn:
    def test_to_string_escapes_cr(self):
        node = SExp.list("a", SExp.quoted_atom("x\ry"))
        assert node.to_string() == '(a "x\\ry")'

    def test_escaped_cr_round_trip_is_stable(self):
        text = '(a "x\\ry")'
        node = parse_string(text)
        assert node.get_atoms() == ["x\ry"]
        assert node.to_string() == text

    def test_parse_file_keeps_raw_cr_in_string(self, tmp_path):
        p = tmp_path / "raw_cr.kicad_sch"
        p.write_bytes(b'(kicad_sch (title "a\rb"))')
        root = parse_file(p)
        assert root.find_child("title").get_atoms() == ["a\rb"]

    def test_document_save_writes_escaped_cr(self, tmp_path):
        src = tmp_path / "in.kicad_sch"
        src.write_bytes(b'(kicad_sch (title "a\rb"))')
        doc = Document.load(src)
        out = tmp_path / "out.kicad_sch"
        doc.save(out)
        data = out.read_bytes()
        assert b"\r" not in data
        assert b'"a\\rb"' in data
        assert parse_file(out).find_child("title").get_atoms() == ["a\rb"]


# ---------------------------------------------------------------------------
# 3. Footprint.to_sexp escapes its string fields
# ---------------------------------------------------------------------------


class TestFootprintEscaping:
    def test_to_sexp_escapes_strings(self):
        fp = Footprint(name="R\nx", description='say "hi"\r', tags=["a\\b"])
        fp.pads.append(Pad(name='1"', layers=("F.Cu", 'odd"layer')))
        text = fp.to_sexp()
        assert _raw_newlines_in_strings(text) == []

        root = parse_string(text)
        assert root.get_atoms()[0] == "R\nx"
        assert root.find_child("descr").get_atoms() == ['say "hi"\r']
        assert root.find_child("tags").get_atoms() == ["a\\b"]
        values = [
            c.get_atoms()[1]
            for c in root.children
            if c.name == "property" and c.get_atoms()[0] == "Value"
        ]
        assert values == ["R\nx"]
        pad = root.find_child("pad")
        assert pad.get_atoms()[0] == '1"'
        assert pad.find_child("layers").get_atoms() == ["F.Cu", 'odd"layer']

    def test_create_chip_save_escapes_name(self, tmp_path):
        path = tmp_path / "chip.kicad_mod"
        create_chip(size="0603", name="R\nx").save(path)
        text = read_text_exact(path)
        assert '"R\\nx"' in text
        assert _raw_newlines_in_strings(text) == []
        assert parse_file(path).get_atoms()[0] == "R\nx"
