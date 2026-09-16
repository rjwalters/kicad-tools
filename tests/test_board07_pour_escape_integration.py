"""Board07 integration of the shared, bounded physical escape helper."""

import json
from types import SimpleNamespace

import pytest
from shapely.geometry import LineString

from kicad_tools.schema.pcb import PCB
from tests.test_board07_pour_topology import generate_design_mod as _recipe_fixture

recipe = _recipe_fixture


def _board(path, *, named=False, outline=True, project=True):
    lines = [
        '(kicad_pcb (version 20260206) (generator "test")',
        '(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))',
        '(net 0 "") (net 1 "GND") (net 2 "foreign")',
    ]
    for ref, x in [("R1", 102), ("R2", 108)]:
        lines.append(
            f'(footprint "test:pad" (layer "F.Cu") (at {x} 45) '
            f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
            '(pad "1" smd circle (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net 1 "GND")))'
        )
    if outline:
        lines.append(
            '(gr_rect (start 100 40) (end 110 50) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
        )
    lines += [
        '(zone (net 1) (net_name "GND") (layer "F.Cu") (fill yes) '
        "(polygon (pts (xy 107.5 44.5) (xy 108.5 44.5) (xy 108.5 45.5) (xy 107.5 45.5))) "
        '(filled_polygon (layer "F.Cu") (pts (xy 107.5 44.5) (xy 108.5 44.5) (xy 108.5 45.5) (xy 107.5 45.5))))',
        '(segment (start 102 45) (end 102.6 45) (width 0.2) (layer "F.Cu") (net 1) (uuid "existing-stub"))',
        '(via (at 102.6 45) (size 0.5) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "existing-via"))',
        # A finite F.Cu wall requires a turn; the wide back-layer obstacle
        # rejects all new via hops. This is an algorithm fixture, not a
        # native-clean board (the existing via overlaps that back obstacle).
        '(segment (start 105 40) (end 105 47) (width 0.2) (layer "F.Cu") (net 2) (uuid "wall"))',
        '(segment (start 100 45) (end 110 45) (width 20) (layer "B.Cu") (net 2) (uuid "back-obstacle"))',
    ]
    text = "\n".join(lines) + "\n)"
    if named:
        text = text.replace('(net 0 "") (net 1 "GND") (net 2 "foreign")', "")
        text = (
            text.replace('(net 1 "GND")', '(net "GND")')
            .replace("(net 1)", '(net "GND")')
            .replace("(net 2)", '(net "foreign")')
        )
    path.write_text(text)
    if project:
        path.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "board": {
                        "design_settings": {
                            "rules": {
                                "min_clearance": 0.2,
                                "min_track_width": 0.2,
                                "min_via_diameter": 0.6,
                                "min_through_hole_diameter": 0.3,
                            }
                        }
                    }
                }
            )
        )


@pytest.mark.parametrize("named", [False, True])
def test_real_fallback_turns_around_wall_using_saved_outline(tmp_path, recipe, monkeypatch, named):
    board = tmp_path / "translated.kicad_pcb"
    _board(board, named=named)
    monkeypatch.setattr(recipe.generate_pcb, "BOARD_ORIGIN_Y", 100)
    original_text = board.read_text()
    original = PCB.load(board)
    assert recipe.generate_pcb.BOARD_ORIGIN_X == 100
    assert not recipe._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert recipe._repair_pour_connectivity(board, ["GND"]) == (0, 1)
    repaired = PCB.load(board)
    added = repaired.segments[len(original.segments) :]
    assert len(added) > 1
    ox, oy = repaired.board_origin
    wall = LineString([(105, 40), (105, 47)]).buffer(0.1)
    for segment in added:
        assert segment.net_name == "GND" and segment.net_name_only is named
        assert segment.layer == "F.Cu" and segment.width == 0.2
        points = [(x + ox, y + oy) for x, y in (segment.start, segment.end)]
        assert all(100.5 <= x <= 109.5 and 40.5 <= y <= 49.5 for x, y in points)
        assert LineString(points).buffer(segment.width / 2).distance(wall) >= 0.2
    assert recipe._audit_pour_nets(board, ["GND"])["GND"]["connected"]
    assert repaired.nets == original.nets
    for kind in ["footprint", "segment", "via", "zone"]:
        assert all(
            block in board.read_text()
            for block in recipe._find_sexp_blocks(original_text, "(" + kind)
        )


@pytest.mark.parametrize("missing", ["outline", "project"])
def test_fallback_requires_artifact_context(tmp_path, recipe, monkeypatch, missing):
    board = tmp_path / "missing.kicad_pcb"
    _board(board, outline=missing != "outline", project=missing != "project")
    before = board.read_bytes()

    def unexpected_load():
        pytest.fail("No helper may load without the artifact context")

    monkeypatch.setattr(recipe, "_load_pour_escape", unexpected_load)
    assert recipe._repair_pour_connectivity(board, ["GND"]) == (0, 0)
    assert board.read_bytes() == before


def test_unknown_rules_fail_without_writing_copper(tmp_path, recipe):
    board = tmp_path / "custom.kicad_pcb"
    _board(board)
    board.with_suffix(".kicad_dru").write_text(
        '(version 1) (rule "custom" (constraint clearance (min 1)))'
    )
    before = board.read_bytes()
    with pytest.raises(ValueError, match="custom DRC rules"):
        recipe._repair_pour_connectivity(board, ["GND"])
    assert board.read_bytes() == before


@pytest.mark.parametrize("named", [False, True])
def test_adapter_and_configured_via_emission(tmp_path, recipe, monkeypatch, named):
    board = tmp_path / "adapter.kicad_pcb"
    _board(board, named=named)
    rule_type, _ = recipe._load_pour_escape()
    rules = rule_type(diameter=0.8, drill=0.4, width=0.25)
    calls = []

    class Rules:
        @staticmethod
        def from_project(path):
            assert path == board.with_suffix(".kicad_pro")
            return rules

    def escape(start, net, layer, pads, segments, vias, primary, bounds, actual_rules):
        calls.append(vias)
        assert bounds == (100.5, 40.5, 109.5, 49.5)
        assert actual_rules is rules
        assert all(len(via) == 4 for via in vias)
        assert vias[0][2:] == (0.25, 0.3)  # copper radius, drill DIAMETER
        return SimpleNamespace(points=(start, (start[0], 48), (108, 48), (108, 45)), via=True)

    monkeypatch.setattr(recipe, "_load_pour_escape", lambda: (Rules, escape))
    original = PCB.load(board)
    assert recipe._repair_pour_connectivity(board, ["GND"]) == (1, 1)
    repaired = PCB.load(board)
    via = repaired.vias[-1]
    assert (via.size, via.drill, via.net_name, via.net_name_only) == (0.8, 0.4, "GND", named)
    assert all(
        s.width == 0.25 and s.net_name_only is named
        for s in repaired.segments[len(original.segments) :]
    )
    assert len(calls) == 1


@pytest.mark.parametrize("exact_sidecar", [False, True])
def test_project_resolution_preserves_rule_context(tmp_path, recipe, monkeypatch, exact_sidecar):
    board = tmp_path / "routed.kicad_pcb"
    _board(board)
    exact = board.with_suffix(".kicad_pro")
    generated = tmp_path / "matchgroup_test.kicad_pro"
    if not exact_sidecar:
        exact.rename(generated)
    else:
        generated.write_text("{}")
    real_rules, _ = recipe._load_pour_escape()
    seen = []

    class Rules:
        @staticmethod
        def from_project(path):
            seen.append(path)
            return real_rules.from_project(path)

    monkeypatch.setattr(recipe, "_load_pour_escape", lambda: (Rules, lambda *args: None))
    before = board.read_bytes()
    assert recipe._repair_pour_connectivity(board, ["GND"]) == (0, 0)
    assert seen == [exact if exact_sidecar else generated]
    assert board.read_bytes() == before


def test_orphan_artifact_rules_do_not_fall_back_to_other_project(tmp_path, recipe):
    board = tmp_path / "orphan.kicad_pcb"
    _board(board, project=False)
    (tmp_path / "matchgroup_test.kicad_pro").write_text("{}")
    board.with_suffix(".kicad_dru").write_text(
        '(version 1) (rule "custom" (constraint clearance (min 1)))'
    )
    before = board.read_bytes()
    with pytest.raises(ValueError, match="project missing"):
        recipe._repair_pour_connectivity(board, ["GND"])
    assert board.read_bytes() == before


def test_helper_loader_ignores_unrelated_module_alias(recipe, monkeypatch):
    import sys

    sentinel = object()
    monkeypatch.setitem(sys.modules, "pour_escape", sentinel)
    previous_path = sys.path.copy()
    rules, search = recipe._load_pour_escape()
    assert rules.__name__ == "EscapeRules" and callable(search)
    assert sys.modules["pour_escape"] is sentinel
    assert sys.path == previous_path
