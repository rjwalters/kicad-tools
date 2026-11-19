"""Library child uuids must not be copied into placed footprints (#9485)."""

import re

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string

SHARED = "11111111-2222-4333-8444-555555555555"

LIBRARY = f"""(footprint "SOT-89-like" (version 20240108) (generator "pcbnew")
  (layer "F.Cu")
  (property "Reference" "REF**" (at 0 -2 0) (layer "F.SilkS")
    (uuid "{SHARED[:-1]}0") (effects (font (size 1 1) (thickness 0.15))))
  (property "Value" "X" (at 0 2 0) (layer "F.Fab")
    (uuid "{SHARED[:-1]}1") (effects (font (size 1 1) (thickness 0.15))))
  (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type solid)) (layer "F.SilkS")
    (uuid "{SHARED[:-1]}2"))
  (fp_circle (center 0 0) (end 1 0) (stroke (width 0.1) (type solid)) (fill none)
    (layer "F.Fab") (uuid "{SHARED[:-1]}3"))
  (fp_text user "${{REFERENCE}}" (at 0 0 0) (layer "F.Fab") (uuid "{SHARED[:-1]}4")
    (effects (font (size 1 1) (thickness 0.15))))
  (fp_line (start -1 1) (end 1 1) (stroke (width 0.1) (type solid)) (layer "F.SilkS"))
  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
    (uuid "{SHARED[:-1]}5")))
"""


def _build(tmp_path, layers=("F.Cu", "F.Cu", "B.Cu")):
    lib = tmp_path / "sot89.kicad_mod"
    lib.write_text(LIBRARY)
    board = PCB.create(width=30, height=30)
    for i, layer in enumerate(layers):
        board.add_footprint_from_file(lib, f"Q{i + 1}", 5 + 5 * i, 10, layer=layer)
    out = tmp_path / "board.kicad_pcb"
    board.save(out)
    return out.read_text()


def _uuids(text):
    return re.findall(r'\((?:uuid|tstamp) "?([0-9a-fA-F-]+)"?\)', text)


def test_placed_footprints_have_unique_uuids(tmp_path):
    text = _build(tmp_path)
    values = _uuids(text)
    assert values, "expected uuids in the saved board"
    assert len(values) == len(set(values))
    assert not any(v.startswith(SHARED[:-1]) for v in values)


def test_many_instances_stay_unique(tmp_path):
    text = _build(tmp_path, layers=("F.Cu",) * 31)
    values = _uuids(text)
    assert len(values) == len(set(values))


def test_rebuild_is_byte_stable(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert _build(a) == _build(b)


def test_footprint_without_child_uuids_unchanged(tmp_path):
    lib = tmp_path / "plain.kicad_mod"
    lib.write_text(
        '(footprint "plain" (layer "F.Cu")'
        ' (fp_line (start 0 0) (end 1 0) (stroke (width 0.1) (type solid)) (layer "F.SilkS")))'
    )
    board = PCB.create(width=20, height=20)
    fp1 = board.add_footprint_from_file(lib, "R1", 5, 5)
    board.add_footprint_from_file(lib, "R2", 10, 5)
    out = tmp_path / "b.kicad_pcb"
    board.save(out)
    values = _uuids(out.read_text())
    assert len(values) == len(set(values))
    assert fp1.reference == "R1"


GROUPED = f"""(footprint "grouped" (version 20240108) (generator "pcbnew")
  (layer "F.Cu")
  (fp_line (start -1 -1) (end 1 -1) (stroke (width 0.1) (type solid)) (layer "F.SilkS")
    (uuid "{SHARED[:-1]}0"))
  (fp_line (start -1 1) (end 1 1) (stroke (width 0.1) (type solid)) (layer "F.SilkS")
    (uuid "{SHARED[:-1]}1"))
  (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask")
    (uuid "{SHARED[:-1]}2"))
  (group "" (uuid "{SHARED[:-1]}3")
    (members "{SHARED[:-1]}0" "{SHARED[:-1]}1" "{SHARED[:-1]}2")))
"""


def _member_refs(text):
    tree = parse_string(text)
    return [
        str(atom.value)
        for members in tree.find_all("members")
        for atom in members.children
        if atom.is_atom
    ]


def test_group_members_follow_renamed_uuids(tmp_path):
    lib = tmp_path / "grouped.kicad_mod"
    lib.write_text(GROUPED)
    board = PCB.create(width=30, height=30)
    board.add_footprint_from_file(lib, "G1", 5, 5)
    board.add_footprint_from_file(lib, "G2", 12, 5)
    out = tmp_path / "board.kicad_pcb"
    board.save(out)
    text = out.read_text()
    values = _uuids(text)
    assert len(values) == len(set(values))
    assert not any(v.startswith(SHARED[:-1]) for v in values)
    refs = _member_refs(text)
    # 2 placements x 3 members: every reference kept, none dangling
    assert len(refs) == 6
    assert all(ref in set(values) for ref in refs)


def test_group_members_resolve_within_their_own_footprint(tmp_path):
    lib = tmp_path / "grouped.kicad_mod"
    lib.write_text(GROUPED)
    board = PCB.create(width=30, height=30)
    board.add_footprint_from_file(lib, "G1", 5, 5)
    board.add_footprint_from_file(lib, "G2", 12, 5)
    out = tmp_path / "board.kicad_pcb"
    board.save(out)
    footprints = parse_string(out.read_text()).find_all("footprint")
    assert len(footprints) == 2
    for fp in footprints:
        local = {
            str(node.get_first_atom())
            for node in fp.iter_all()
            if node.name in ("uuid", "tstamp") and node.get_first_atom() is not None
        }
        refs = [
            str(atom.value)
            for members in fp.find_all("members")
            for atom in members.children
            if atom.is_atom
        ]
        assert len(refs) == 3
        assert set(refs) <= local
