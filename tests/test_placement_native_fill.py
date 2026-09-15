"""Real native fills must respect fixed copper, not just restore it afterwards."""

import json

import pytest
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from kicad_tools.router.optimizer.pcb import _extract_balanced_blocks
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.placement_fill import fill_around_fixed_copper, find_kicad_python
from tests.test_routing_placement_disposition import board_text

NATIVE_PYTHON = find_kicad_python()


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize(
    "project_clearance",
    [None, 0.8, "custom", "zone_pair", "group", "protected_group", "legacy_group"],
)
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
    group = ""
    if project_clearance in {"group", "protected_group", "legacy_group"}:
        identity = "00000000-0000-4000-8000-000000000789"
        if project_clearance == "legacy_group":
            identity = "00000000-0000-0000-0000-00001234abcd"
            eligible = eligible.replace("(zone", "(zone (tstamp 1234abcd)", 1)
        elif project_clearance == "protected_group":
            zone = zone.replace("(zone", f'(zone (uuid "{identity}")', 1)
        else:
            eligible = eligible.replace("(zone", f'(zone (uuid "{identity}")', 1)
        group = f'''(group "WideCopper" (id "00000000-0000-4000-8000-000000000790")
          (members "{identity}"))'''
    source = board_text(name_only=name_only).rstrip()[:-1] + zone + eligible + group + ")"
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(source)
    if isinstance(project_clearance, str):
        condition = (
            "(condition \"A.memberOf('WideCopper') || B.memberOf('WideCopper')\")"
            if project_clearance in {"group", "protected_group", "legacy_group"}
            else "(condition \"A.Type == 'Zone' && B.Type == 'Zone'\")"
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


def test_protected_only_zones_need_no_native_runtime(tmp_path, monkeypatch):
    from kicad_tools.zones import placement_fill

    board = tmp_path / "fixed.kicad_pcb"
    source = '(kicad_pcb (net 1 "BAD") (zone (net 1) (layer "F.Cu")))'
    board.write_text(source)
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    fill_around_fixed_copper(board, frozenset({"BAD"}))
    assert board.read_text() == source


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize("filled", [False, True], ids=["empty", "hole"])
def test_native_fill_preserves_empty_space_in_protected_zone(tmp_path, filled):
    # The weakly-simple contour is KiCad's bridged representation of a hole.
    copper = (
        """(filled_polygon (layer "F.Cu")
      (pts (xy 103 101) (xy 118 101) (xy 118 111) (xy 103 111) (xy 103 101)
           (xy 112 103) (xy 112 108) (xy 116 108) (xy 116 103) (xy 112 103)
           (xy 103 101)))"""
        if filled
        else ""
    )
    fixed = f"""(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (filled_areas_thickness no)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 111) (xy 102 111)))
      {copper})"""
    eligible = """(zone (net 2) (net_name "GOOD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (island_removal_mode 0) (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 101 101) (xy 119 101) (xy 119 111) (xy 101 111))))"""
    board = tmp_path / "mixed.kicad_pcb"
    board.write_text(board_text().rstrip()[:-1] + fixed + eligible + ")")
    fill_around_fixed_copper(board, frozenset({"BAD"}), python=NATIVE_PYTHON)
    assert board.read_text().count(fixed) == 1
    zone = next(z for z in PCB.load(board).zones if z.net_name == "GOOD")
    shape = unary_union([Polygon(p).buffer(0) for p in zone.filled_polygons])
    # Schema coordinates are normalized to the board outline origin (100, 100).
    assert shape.contains(Point(14, 5))
    if filled:
        assert not shape.contains(Point(10, 5))


def test_unavailable_native_runtime_preserves_partial_board(tmp_path, monkeypatch):
    from kicad_tools.zones import placement_fill

    board = tmp_path / "partial.kicad_pcb"
    source = board_text()[:-1] + '(zone (net 2) (layer "F.Cu")))'
    board.write_text(source)
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    with pytest.raises(RuntimeError, match="selective zone-fill support is unavailable"):
        fill_around_fixed_copper(board, frozenset({"BAD"}))
    assert board.read_text() == source


@pytest.mark.skipif(NATIVE_PYTHON is None, reason="KiCad Python runtime unavailable")
@pytest.mark.parametrize("auto_fix", [False, True])
@pytest.mark.parametrize("invalid_net", ["BAD", "GND"])
def test_real_cli_routes_and_fills_without_changing_excluded_zone(
    tmp_path, auto_fix, invalid_net, capsys
):
    from kicad_tools.cli.route_cmd import main

    fixed = """(zone (net 1) (net_name "BAD") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))
      (filled_polygon (layer "F.Cu") (pts (xy 104 102) (xy 109 102) (xy 109 104) (xy 104 104))))"""
    plane = """(zone (net 4) (net_name "PLANE") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110))))"""
    source = tmp_path / "source.kicad_pcb"
    if invalid_net == "GND":
        # An uninset power zone used to be replaced by auto-pour before the
        # preservation-aware loader could capture its original geometry.
        fixed = fixed.replace('"BAD"', '"GND"').replace(
            "(xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)",
            "(xy 100 100) (xy 120 100) (xy 120 112) (xy 100 112)",
        )
    original = board_text().replace('"BAD"', f'"{invalid_net}"')[:-1] + fixed + plane + ")"
    source.write_text(original)
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "report.json"
    options = ["--auto-fix"] if auto_fix else []
    result = main([str(source), "-o", str(output), "--complete-report", str(report), *options])
    assert result in {2, 3}
    if auto_fix:
        assert "Auto-Fix DRC Violations" in capsys.readouterr().out
    assert source.read_text() == original
    assert output.read_text().count(fixed) == 1
    pcb = PCB.load(output)
    assert any(s.net_name == "GOOD" for s in pcb.segments)
    assert any(z.net_name == "PLANE" and z.filled_polygons for z in pcb.zones)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["requested_blocked_nets"] == [invalid_net]
    assert disposition.get("zone_fill_status") != "failed"


def test_cli_fill_failure_retains_routes_and_cannot_report_success(tmp_path, monkeypatch, capsys):
    from kicad_tools.cli.route_cmd import main
    from kicad_tools.zones import placement_fill

    source = tmp_path / "mixed.kicad_pcb"
    original = (
        board_text()[:-1]
        + """(zone (net 4) (net_name "PLANE") (layer "F.Cu")
      (hatch edge 0.5) (connect_pads yes (clearance 0.3)) (min_thickness 0.2)
      (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
      (polygon (pts (xy 102 101) (xy 118 101) (xy 118 110) (xy 102 110)))))"""
    )
    source.write_text(original)
    output = tmp_path / "routed.kicad_pcb"
    report = tmp_path / "report.json"
    monkeypatch.setattr(placement_fill, "find_kicad_python", lambda: None)
    result = main(
        [str(source), "-o", str(output), "--nets", "GOOD", "--complete-report", str(report)]
    )
    assert result != 0
    assert source.read_text() == original
    assert any(s.net_name == "GOOD" for s in PCB.load(output).segments)
    disposition = json.loads(report.read_text())["placement_disposition"]
    assert disposition["requested_blocked_nets"] == []
    assert disposition["zone_fill_status"] == "failed"
    assert disposition["clean_success"] is False
    assert "SUCCESS:" not in capsys.readouterr().out
