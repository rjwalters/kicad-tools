"""Pour repair must emit valid nonzero copper at point-touching fill islands."""

import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("nearby_foreign_track", [False, True])
def test_touching_fill_islands_get_a_real_bridge(tmp_path, monkeypatch, nearby_foreign_track):
    root = Path(__file__).parents[1] / "boards/07-matchgroup-test"

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("generate_pcb", root / "generate_pcb.py")
    load("generate_schematic", root / "generate_schematic.py")
    recipe = load("board07_bridge_test", root / "generate_design.py")
    pcb = tmp_path / "touching.kicad_pcb"
    pcb.write_text("""(kicad_pcb (version 20221018) (generator test)
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "+1V2")
  (gr_rect (start 100 100) (end 110 110) (layer "Edge.Cuts") (width 0.1))
  (zone (net 1) (net_name "+1V2") (layer "In2.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.15)) (min_thickness 0.15)
    (polygon (pts (xy 101 101) (xy 109 101) (xy 109 109) (xy 101 109)))
    (filled_polygon (layer "In2.Cu") (pts (xy 101 101) (xy 105 105) (xy 101 109)))
    (filled_polygon (layer "In2.Cu") (pts (xy 109 101) (xy 109 109) (xy 105 105)))
  )
)""")
    if nearby_foreign_track:
        text = pcb.read_text().replace(
            '(net 0 "") (net 1 "+1V2")', '(net 0 "") (net 1 "+1V2") (net 2 "DQ0")'
        )
        text = (
            text.rstrip()[:-1]
            + '  (segment (start 103.751 106.956) (end 105.9375 104.7696) (width 0.15) (layer "In2.Cu") (net 2))\n)\n'
        )
        pcb.write_text(text)
    _, bridges = recipe._repair_pour_connectivity(pcb, ["+1V2"])
    assert bridges > 0
    from kicad_tools.schema.pcb import PCB

    result = PCB.load(pcb)
    assert result.segments
    assert all(s.start != s.end for s in result.segments)

    if nearby_foreign_track:
        from shapely.geometry import LineString

        own = [s for s in result.segments if s.net_number == 1]
        foreign = [s for s in result.segments if s.net_number == 2]
        assert own and foreign
        assert all(
            LineString([s.start, s.end])
            .buffer(s.width / 2)
            .distance(LineString([t.start, t.end]).buffer(t.width / 2))
            >= 0.15
            for s in own
            for t in foreign
        )
        # Only the fourth (opposite diagonal) orientation clears this witness.
        assert all(s.start[0] != s.end[0] and s.start[1] != s.end[1] for s in own)
