"""The CLI flip must serialize a complete geometric side change (#5019)."""

from argparse import Namespace

import pytest

from kicad_tools.cli.pcb_modify import cmd_flip
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_string, serialize_sexp


@pytest.mark.parametrize(
    "reference",
    [
        '(fp_text reference "C1" (at 0 -1) (layer "F.SilkS"))',
        '(property "Reference" "C1" (at 0 -1) (layer "F.SilkS"))',
    ],
)
def test_flip_round_trip_preserves_binding_and_mirrors_geometry(reference, tmp_path):
    tree = parse_string(f"""(kicad_pcb
      (layers (0 "F.Cu" signal) (31 "B.Cu" signal)) (net 1 "VCC")
      (footprint "x" (layer "F.Cu") (at 10 20 30) {reference}
        (fp_line (start -1 -1) (end 1 -1) (stroke (width .1) (type default)) (layer "F.Fab"))
        (pad "1" smd rect (at -.8 .3 30) (size .9 .95)
          (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "VCC"))))""")
    before = PCB(tree).get_pad_position("C1", "1")
    original = serialize_sexp(tree)
    assert cmd_flip(tree, Namespace(reference="C1", dry_run=True))
    assert serialize_sexp(tree) == original
    assert cmd_flip(tree, Namespace(reference="C1", dry_run=False))
    path = tmp_path / "flipped.kicad_pcb"
    path.write_text(serialize_sexp(tree))
    pcb = PCB.load(path)
    fp = pcb.get_footprint("C1")
    assert fp.layer == "B.Cu"
    assert set(fp.pads[0].layers) == {"B.Cu", "B.Paste", "B.Mask"}
    assert fp.pads[0].net_name == "VCC"
    assert pcb.get_pad_position("C1", "1") == pytest.approx((20 - before[0], before[1]))
    assert '(layer "B.SilkS")' in path.read_text()
    assert '(layer "B.Fab")' in path.read_text()
    assert cmd_flip(tree, Namespace(reference="C1", dry_run=False))
    path.write_text(serialize_sexp(tree))
    restored = PCB.load(path)
    assert restored.get_pad_position("C1", "1") == pytest.approx(before)
    assert restored.get_footprint("C1").layer == "F.Cu"
    assert set(restored.get_footprint("C1").pads[0].layers) == {"F.Cu", "F.Paste", "F.Mask"}
