"""Real native fills must respect fixed copper, not just restore it afterwards."""

import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon
from shapely.ops import unary_union

from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.placement_fill import fill_around_fixed_copper
from tests.test_routing_placement_disposition import board_text

NATIVE_PYTHON = Path(
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/3.9/bin/python3"
)


@pytest.mark.skipif(not NATIVE_PYTHON.exists(), reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize("project_clearance", [None, 0.8, "custom", "zone_pair"])
@pytest.mark.parametrize("multilayer", [False, True])
@pytest.mark.parametrize("name_only", [False, True])
def test_native_eligible_fill_clears_and_preserves_fixed_zone(
    tmp_path, project_clearance, multilayer, name_only
):
    zone = """(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))
      (filled_polygon (layer "F.Cu") (pts (xy 104 102) (xy 109 102) (xy 109 104) (xy 104 104))))"""
    eligible = """(zone (net 2) (net_name "GOOD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110))))"""
    if multilayer:
        zone = zone.replace('(layer "F.Cu")', '(layers "F.Cu" "B.Cu")', 1)
        zone = (
            zone[:-1]
            + """(filled_polygon (layer "B.Cu")
          (pts (xy 111 102) (xy 116 102) (xy 116 104) (xy 111 104))))"""
        )
        eligible = eligible.replace('(layer "F.Cu")', '(layers "F.Cu" "B.Cu")', 1)
        eligible = eligible.replace("(fill yes", "(fill yes (island_removal_mode 0)")
    if name_only:
        zone = zone.replace("(net 1)", '(net "BAD")')
        eligible = eligible.replace("(net 2)", '(net "GOOD")')
    source = board_text(name_only=name_only).rstrip()[:-1] + zone + eligible + ")"
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(source)
    if isinstance(project_clearance, str):
        condition = (
            "(condition \"A.Type == 'Zone' && B.Type == 'Zone'\")"
            if project_clearance == "zone_pair"
            else ""
        )
        board.with_suffix(".kicad_pro").write_text("{}")
        board.with_suffix(".kicad_dru").write_text(
            f'(version 1)\n(rule "Wide" {condition} (constraint clearance (min 0.8mm)))\n'
        )
    elif project_clearance is not None:
        board.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "board": {"design_settings": {"rules": {"min_clearance": project_clearance}}},
                    "net_settings": {
                        "classes": [{"name": "Default", "clearance": project_clearance}]
                    },
                }
            )
        )
    before = PCB.load(board)
    fill_around_fixed_copper(board, frozenset({"BAD"}), python=NATIVE_PYTHON)
    result = board.read_text()
    assert result.count(zone) == 1
    # Only the eligible zone may change, including native-added timestamps.
    new_eligible = next(z for _, _, z in _extract_balanced_blocks(result, "zone") if '"GOOD"' in z)
    assert result.replace(new_eligible, eligible) == source
    after = PCB.load(board)
    fixed = next(z for z in before.zones if z.net_name == "BAD")
    filled = next(z for z in after.zones if z.net_name == "GOOD")
    for layer in ["F.Cu", "B.Cu"] if multilayer else ["F.Cu"]:
        fixed_shape = unary_union(
            [
                Polygon(p).buffer(fixed.fill_inflation())
                for p, polygon_layer in zip(
                    fixed.filled_polygons, fixed.filled_polygon_layers, strict=True
                )
                if polygon_layer == layer
            ]
        )
        shape = unary_union(
            [
                Polygon(p)
                for p, polygon_layer in zip(
                    filled.filled_polygons, filled.filled_polygon_layers, strict=True
                )
                if polygon_layer == layer
            ]
        )
        assert not fixed_shape.is_empty
        assert shape.area > 100
        assert shape.intersection(fixed_shape).area == 0
        assert shape.distance(fixed_shape) >= (0.8 if project_clearance else 0.3) - 0.001
    assert filled.fill_inflation() == 0
