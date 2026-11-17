"""Footprint ``(path ...)`` remap from an old schematic's UUIDs to a new one's (Issue #6076)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest

from kicad_tools.core.pcb_path_remap import (
    PathRemapError,
    remap_pcb_file_paths,
    remap_pcb_paths,
    schematic_instance_paths,
)
from kicad_tools.schematic.models.schematic import Schematic

_LIB = """(kicad_symbol_lib
\t(version 20231120)
\t(generator "test")
\t(symbol "WIDGET"
\t\t(property "Reference" "U" (at 0 5.08 0) (effects (font (size 1.27 1.27))))
\t\t(property "Value" "WIDGET" (at 0 2.54 0) (effects (font (size 1.27 1.27))))
\t\t(symbol "WIDGET_1_1"
\t\t\t(pin input line (at -7.62 2.54 0) (length 2.54) (name "IN" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
\t\t)
\t)
)
"""


def _schematic(tmp_path: Path, name: str, refs: list[str], *, random_ids: bool) -> Path:
    lib = tmp_path / "widgets.kicad_sym"
    lib.write_text(_LIB)
    sch = Schematic(
        title="Remap",
        local_symbol_libs=[lib],
        sheet_uuid=str(uuid.uuid4()) if random_ids else None,
    )
    for i, ref in enumerate(refs):
        sym = sch.add_symbol("widgets:WIDGET", 25.4 * (i + 1), 25.4, ref, "W")
        if random_ids:
            sym.uuid_str = str(uuid.uuid4())  # an "old", uuid4-era schematic
    path = tmp_path / f"{name}.kicad_sch"
    sch.write(path)
    return path


def _symbol_uuids(sch_path: Path) -> dict[str, str]:
    sch = Schematic.load(sch_path)
    return {s.reference: s.uuid_str for s in sch.symbols}


def _board(footprints: list[tuple[str, str | None]]) -> str:
    """A board text with one footprint per ``(reference, path)``."""
    blocks = []
    for i, (ref, path) in enumerate(footprints):
        path_line = f'\n\t\t(path "{path}")' if path is not None else ""
        blocks.append(
            f'\t(footprint "R_0603"\n\t\t(layer "F.Cu")\n\t\t(uuid "{uuid.uuid4()}")\n'
            f"\t\t(at {10 * i} 10)\n"
            f'\t\t(property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))'
            f"{path_line}\n"
            f'\t\t(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))\n\t)'
        )
    return (
        '(kicad_pcb\n\t(version 20241229)\n\t(generator "pcbnew")\n'
        + "\n".join(blocks)
        + '\n\t(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 1)'
        ' (uuid "11111111-2222-4333-8444-555555555555"))\n)\n'
    )


@pytest.fixture
def pair(tmp_path):
    old = _schematic(tmp_path, "old", ["U1", "U2"], random_ids=True)
    new = _schematic(tmp_path, "new", ["U1", "U2"], random_ids=False)
    return old, new


def test_paths_follow_the_reference(pair):
    old, new = pair
    o, n = _symbol_uuids(old), _symbol_uuids(new)
    text = _board([("U1", f"/{o['U1']}"), ("U2", f"/{o['U2']}")])
    result = remap_pcb_paths(text, old, new)
    assert result.mapping == {f"/{o['U1']}": f"/{n['U1']}", f"/{o['U2']}": f"/{n['U2']}"}
    assert result.changed == 2
    # Nothing but the path atoms changed.
    mask = re.compile(r'\(path "[^"]*"\)')
    assert mask.sub("P", result.text) == mask.sub("P", text)
    assert f'(path "/{n["U1"]}")' in result.text
    assert o["U1"] not in result.text


def test_root_inclusive_spelling_is_mapped_too(pair):
    old, new = pair
    o, n = _symbol_uuids(old), _symbol_uuids(new)
    old_root = Schematic.load(old).sheet_uuid
    new_root = Schematic.load(new).sheet_uuid
    text = _board([("U1", f"/{old_root}/{o['U1']}")])
    result = remap_pcb_paths(text, old, new, allow_unlinked=True)
    assert result.mapping == {f"/{old_root}/{o['U1']}": f"/{new_root}/{n['U1']}"}


def test_file_wrapper_rewrites_in_place(pair, tmp_path):
    old, new = pair
    o, n = _symbol_uuids(old), _symbol_uuids(new)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board([("U1", f"/{o['U1']}"), ("U2", f"/{o['U2']}")]))
    remap_pcb_file_paths(pcb, old, new)
    assert f'(path "/{n["U2"]}")' in pcb.read_text()
    # Idempotent once remapped: new -> new.
    before = pcb.read_bytes()
    remap_pcb_file_paths(pcb, new, new)
    assert pcb.read_bytes() == before


@pytest.mark.parametrize(
    "case, needle",
    [
        ("unknown", "matches no symbol"),
        ("wrong_ref", "belongs to U1"),
        ("missing_in_new", "no symbol instances of U2"),
        ("pathless", "no (path ...)"),
        ("shared", "is shared with"),
    ],
)
def test_refuses_unmapped_or_ambiguous(tmp_path, case, needle):
    old = _schematic(tmp_path, "old", ["U1", "U2"], random_ids=True)
    refs = ["U1"] if case == "missing_in_new" else ["U1", "U2"]
    new = _schematic(tmp_path, "new", refs, random_ids=False)
    o = _symbol_uuids(old)
    footprints = {
        "unknown": [("U1", f"/{uuid.uuid4()}")],
        "wrong_ref": [("U2", f"/{o['U1']}")],
        "missing_in_new": [("U1", f"/{o['U1']}"), ("U2", f"/{o['U2']}")],
        "pathless": [("U1", f"/{o['U1']}"), ("H1", None)],
        "shared": [("U1", f"/{o['U1']}"), ("U1", f"/{o['U1']}")],
    }[case]
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_board(footprints))
    before = pcb.read_bytes()
    with pytest.raises(PathRemapError) as err:
        remap_pcb_file_paths(pcb, old, new)
    assert needle in str(err.value)
    assert pcb.read_bytes() == before  # refused: nothing written


def test_allow_unlinked_leaves_pathless_footprints_alone(pair):
    old, new = pair
    o = _symbol_uuids(old)
    result = remap_pcb_paths(
        _board([("U1", f"/{o['U1']}"), ("H1", None)]), old, new, allow_unlinked=True
    )
    assert result.unlinked == ["H1"]
    assert len(result.mapping) == 1


def test_ambiguous_new_reference_is_refused(tmp_path):
    old = _schematic(tmp_path, "old", ["U1"], random_ids=True)
    new = _schematic(tmp_path, "new", ["U1", "U1"], random_ids=False)  # duplicate ref
    o = _symbol_uuids(old)
    with pytest.raises(PathRemapError, match="2 symbol instances of U1"):
        remap_pcb_paths(_board([("U1", f"/{o['U1']}")]), old, new)


def test_instance_paths_include_both_spellings(pair):
    old, _new = pair
    root, paths = schematic_instance_paths(old)
    o = _symbol_uuids(old)
    assert paths[f"/{o['U1']}"] == ("U1", 1)
    assert paths[f"/{root}/{o['U1']}"] == ("U1", 1)
