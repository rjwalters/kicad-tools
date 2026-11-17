"""KiCad 6 ``Sheet file`` / ``Sheet name`` spelling is read by every walker (issue #7617)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kicad_tools.cli.sch_wire_common import _collect_schematic_files
from kicad_tools.core.sheet_properties import SHEET_FILE_KEYS, SHEET_NAME_KEYS
from kicad_tools.operations.netlist import _get_sheet_entries, build_netlist_from_schematic
from kicad_tools.schema import Schematic
from kicad_tools.schema.bom import extract_bom
from kicad_tools.schema.hierarchy import build_hierarchy

FIXTURES = Path(__file__).parent / "fixtures" / "hierarchical"


def _copy_project(dest: Path, *, kicad6: bool) -> Path:
    dest.mkdir()
    for f in FIXTURES.glob("*.kicad_sch"):
        text = f.read_text(encoding="utf-8")
        if kicad6:
            text = text.replace('"Sheetfile"', '"Sheet file"').replace(
                '"Sheetname"', '"Sheet name"'
            )
        (dest / f.name).write_text(text, encoding="utf-8")
    return dest / "root.kicad_sch"


@pytest.fixture
def projects(tmp_path):
    new = _copy_project(tmp_path / "new", kicad6=False)
    old = _copy_project(tmp_path / "old", kicad6=True)
    assert "Sheetfile" not in old.read_text(encoding="utf-8")
    return new, old


def test_key_tuples_cover_both_spellings():
    assert SHEET_FILE_KEYS == ("Sheetfile", "Sheet file")
    assert SHEET_NAME_KEYS == ("Sheetname", "Sheet name")


def test_build_hierarchy_matches(projects):
    new, old = projects
    h_new, h_old = build_hierarchy(str(new)), build_hierarchy(str(old))
    names = lambda h: sorted((n.name, Path(n.path).name) for n in h.all_nodes())  # noqa: E731
    assert len(h_new.all_nodes()) > 1
    assert names(h_old) == names(h_new)


def test_schematic_sheets_match(projects):
    new, old = projects
    key = lambda p: [(s.name, s.filename) for s in Schematic.load(p).sheets]  # noqa: E731
    assert key(new)
    assert key(old) == key(new)


def test_collect_schematic_files_matches(projects):
    new, old = projects
    rel = lambda p: [f.name for f in _collect_schematic_files(p)]  # noqa: E731
    assert len(rel(new)) > 1
    assert rel(old) == rel(new)


def test_extract_bom_matches(projects):
    new, old = projects
    refs = lambda p: sorted(i.reference for i in extract_bom(str(p)).items)  # noqa: E731
    assert refs(old) == refs(new)


def test_netlist_walker_matches(projects):
    new, old = projects
    key = lambda p: [(e.filename, e.sheetname) for e in _get_sheet_entries(p)]  # noqa: E731
    assert key(new)
    assert key(old) == key(new)
    comps = lambda p: sorted(c.reference for c in build_netlist_from_schematic(p).components)  # noqa: E731
    assert comps(old) == comps(new)


def test_mixed_spellings_across_sheets(tmp_path):
    root = _copy_project(tmp_path / "mixed", kicad6=False)
    text = root.read_text(encoding="utf-8")
    # Convert only the first sheet to the KiCad 6 spelling.
    text = text.replace('"Sheetfile"', '"Sheet file"', 1).replace('"Sheetname"', '"Sheet name"', 1)
    root.write_text(text, encoding="utf-8")
    assert len(Schematic.load(root).sheets) == len(
        Schematic.load(FIXTURES / "root.kicad_sch").sheets
    )
    assert [f.name for f in _collect_schematic_files(root)] == [
        f.name for f in _collect_schematic_files(FIXTURES / "root.kicad_sch")
    ]


def test_sheet_without_file_property_is_reported(tmp_path, caplog):
    root = _copy_project(tmp_path / "nofile", kicad6=False)
    root.write_text(
        root.read_text(encoding="utf-8").replace('"Sheetfile"', '"Bogus"'), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING):
        h = build_hierarchy(str(root))
        entries = _get_sheet_entries(root)
    assert len(h.all_nodes()) == 1
    assert entries == []
    assert "no file property" in caplog.text
