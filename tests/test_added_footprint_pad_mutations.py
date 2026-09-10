"""New and reloaded footprints must persist the same pad geometry edits."""

import pytest

from kicad_tools.schema.pcb import PCB


@pytest.mark.parametrize("reload_first", [False, True])
@pytest.mark.parametrize("initial_rotation", [0, 90])
def test_added_pad_geometry_survives_save(tmp_path, reload_first, initial_rotation):
    library = tmp_path / "repeated.kicad_mod"
    library.write_text("""(footprint "repeated" (layer "F.Cu")
      (pad "SH" smd rect (at -2 0) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "SH" smd rect (at 2 0 30) (size 2 1) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "" np_thru_hole circle (at 0 3) (size 1 1) (drill 1) (layers "*.Cu" "*.Mask")))""")
    board = PCB.create(width=20, height=20)
    board.add_footprint_from_file(library, "J1", 10, 10, rotation=initial_rotation)
    path = tmp_path / "board.kicad_pcb"
    if reload_first:
        board.save(path)
        board = PCB.load(path)
    fp = board.get_footprint("J1")
    assert [p.rotation for p in fp.pads] == [
        initial_rotation,
        30 + initial_rotation,
        initial_rotation,
    ]
    for index, pad in enumerate(fp.pads):
        pad.rotation += 90
        pad.position = (pad.position[0], pad.position[1] + index + 1)
    fp.pads[0].layers = ["B.Cu", "B.Paste", "B.Mask"]
    board.save(path)
    restored = PCB.load(path).get_footprint("J1")
    assert [p.rotation for p in restored.pads] == [
        90 + initial_rotation,
        120 + initial_rotation,
        90 + initial_rotation,
    ]
    assert [p.position for p in restored.pads] == [(-2, 1), (2, 2), (0, 6)]
    assert restored.pads[0].layers == ["B.Cu", "B.Paste", "B.Mask"]
    assert restored.pads[1].layers == ["F.Cu", "F.Paste", "F.Mask"]
