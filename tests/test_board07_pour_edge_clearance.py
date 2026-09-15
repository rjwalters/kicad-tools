"""Pour-repair bridges must respect the copper-to-edge floor (issue #5333).

Board07's ``2c9bcb95`` full-recipe artifact shipped two ``+1V2`` In2.Cu
bridges 0.153 mm from the board edge against the 0.3 mm jlcpcb floor, and
KiCad's native refill reported both as blocking ``copper_edge_clearance``
findings.  Both came from the touching-fill bridge candidates: the fills meet
at a point ON the 0.5 mm zone inset, so every candidate direction starts by
walking 0.35 mm *outward* from that point, and the emitter's bounds test used
a fixed ``inset - 0.3`` slack that happened to admit the 45-degree one.

The slack is now derived from the real outline, the real copper width and the
real fab floor, so the diagonal candidates are rejected and the vertical one
(which runs ALONG the inset, not across it) is emitted instead.
"""

import importlib.util
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, box

from kicad_tools.manufacturers import get_profile
from kicad_tools.zones.pour_escape import edge_clear_centerline_bounds

# generate_pcb's authoring frame -- the recipe runs pour repair and
# quantization here and only translates to the sheet-centered frame at the
# very end (step 9), so these are the coordinates the emitter sees.
OUTLINE = (100.0, 100.0, 210.0, 195.0)
#: Where the two fills touch: on the 0.5 mm zone inset of the left edge.
TOUCH = (100.5, 157.0)


class TestEdgeClearCenterlineBounds:
    def test_window_accounts_for_half_the_copper_width(self):
        assert edge_clear_centerline_bounds(OUTLINE, width=0.2, edge_clearance=0.3) == (
            pytest.approx(100.4),
            pytest.approx(100.4),
            pytest.approx(209.6),
            pytest.approx(194.6),
        )

    def test_the_shipped_bridge_endpoint_is_outside_the_window(self):
        """0.2475 mm of 45-degree overshoot from the inset lands at 0.153 mm."""
        lo_x, _lo_y, _hi_x, _hi_y = edge_clear_centerline_bounds(
            OUTLINE, width=0.2, edge_clearance=0.3
        )
        shipped_x = TOUCH[0] - 0.35 * (0.5**0.5)
        assert shipped_x == pytest.approx(100.2525, abs=1e-4)
        assert shipped_x - OUTLINE[0] - 0.1 == pytest.approx(0.1525, abs=1e-4)
        assert shipped_x < lo_x

    def test_window_can_be_empty(self):
        lo_x, lo_y, hi_x, hi_y = edge_clear_centerline_bounds(
            (0.0, 0.0, 0.5, 0.5), width=0.2, edge_clearance=0.3
        )
        assert lo_x > hi_x
        assert lo_y > hi_y


def _load_recipe(monkeypatch):
    root = Path(__file__).parents[1] / "boards/07-matchgroup-test"

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        return module

    load("generate_pcb", root / "generate_pcb.py")
    load("generate_schematic", root / "generate_schematic.py")
    return load("board07_edge_clearance_test", root / "generate_design.py")


def _board_with_edge_touching_fills(tmp_path):
    pcb = tmp_path / "edge_touching.kicad_pcb"
    pcb.write_text(
        """(kicad_pcb (version 20221018) (generator test)
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "+1V2")
  (gr_rect (start 100 100) (end 210 195) (layer "Edge.Cuts") (width 0.1))
  (zone (net 1) (net_name "+1V2") (layer "In2.Cu") (hatch edge 0.5)
    (connect_pads (clearance 0.15)) (min_thickness 0.15)
    (polygon (pts (xy 100.5 153) (xy 104.5 153) (xy 104.5 161) (xy 100.5 161)))
    (filled_polygon (layer "In2.Cu") (pts (xy 100.5 153) (xy 104.5 153) (xy 100.5 157)))
    (filled_polygon (layer "In2.Cu") (pts (xy 100.5 157) (xy 104.5 161) (xy 100.5 161)))
  )
)"""
    )
    return pcb


def test_edge_touching_bridge_clears_the_copper_to_edge_floor(tmp_path, monkeypatch):
    recipe = _load_recipe(monkeypatch)
    pcb = _board_with_edge_touching_fills(tmp_path)

    _vias, bridges = recipe._repair_pour_connectivity(pcb, ["+1V2"])
    assert bridges > 0, "the point-touching fills must still be bridged"

    from kicad_tools.schema.pcb import PCB

    result = PCB.load(pcb)
    emitted = [seg for seg in result.segments if seg.net_number == 1]
    assert emitted

    edge_floor = get_profile("jlcpcb").get_design_rules(layers=4).min_copper_to_edge_mm
    origin = result.board_origin
    outline_poly = box(*OUTLINE)
    for seg in emitted:
        start = (seg.start[0] + origin[0], seg.start[1] + origin[1])
        end = (seg.end[0] + origin[0], seg.end[1] + origin[1])
        assert start != end
        copper = LineString([start, end]).buffer(seg.width / 2.0)
        gap = copper.distance(outline_poly.exterior)
        assert gap >= edge_floor - 1e-9, f"{start}->{end} is {gap:.4f} mm from the board edge"

    # The pre-fix emitter took the 45-degree candidate and shipped copper at
    # 0.153 mm.  The vertical candidate runs ALONG the inset instead.
    assert all(
        seg.start[0] + origin[0] == pytest.approx(TOUCH[0], abs=1e-6)
        and seg.end[0] + origin[0] == pytest.approx(TOUCH[0], abs=1e-6)
        for seg in emitted
    ), [(seg.start, seg.end) for seg in emitted]
