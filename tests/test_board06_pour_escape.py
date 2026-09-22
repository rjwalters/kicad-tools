"""Physical constraints of the bounded Board06 pour escape fallback."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, Point, box

spec = importlib.util.spec_from_file_location(
    "board06_pour_escape",
    Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/pour_escape.py",
)
escape = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = escape
spec.loader.exec_module(escape)


@pytest.mark.parametrize("offset", [0, 100])
def test_long_corridor_uses_legal_via_and_preserves_geometry(offset):
    start = (offset, 0)
    pads = [(Point(start).buffer(0.15), "GND", {"F.Cu"}, 0, start)]
    segments = [
        (box(offset - 1, 0.41, offset + 18, 1), "other", "F.Cu"),
        (box(offset - 1, -1, offset + 18, -0.41), "other", "F.Cu"),
    ]
    primary = [(box(offset + 19, -2, offset + 21, 2), {"In1.Cu"}, "fill")]
    before = [g.wkb for g, _, _ in segments]
    result = escape.find_escape(
        start,
        "GND",
        "F.Cu",
        pads,
        segments,
        [],
        primary,
        (offset - 0.5, -0.5, offset + 21, 0.5),
        escape.EscapeRules(),
    )
    assert result is not None and result.via
    assert result.points[-1][0] - offset > 18
    copper = LineString(result.points).buffer(result.rules.width / 2)
    assert all(copper.distance(g) >= result.rules.clearance for g, _, _ in segments)
    assert [g.wkb for g, _, _ in segments] == before


def test_front_copper_needs_no_via():
    result = escape.find_escape(
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [],
        [(box(0.5, -0.5, 1, 0.5), {"F.Cu"}, "seg")],
        (-1, -1, 2, 2),
        escape.EscapeRules(),
    )
    assert result is not None and not result.via


def test_budget_exhaustion_and_blocked_start_return_no_partial_path():
    args = (
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [],
        [(box(5, 5, 6, 6), {"F.Cu"}, "fill")],
        (-1, -1, 7, 7),
        escape.EscapeRules(),
    )
    assert escape.find_escape(*args, node_budget=2) is None
    args = list(args)
    args[4] = [(box(-0.1, -0.1, 0.1, 0.1), "other", "F.Cu")]
    assert escape.find_escape(*args) is None


def test_actual_existing_drill_blocks_otherwise_legal_via():
    args = (
        (0, 0),
        "GND",
        "F.Cu",
        [],
        [],
        [(Point(1, 0), "GND", 0.5, 0.8)],
        [(box(-0.1, -0.1, 0.1, 0.1), {"In1.Cu"}, "fill")],
        (-0.3, -0.3, 0.3, 0.3),
        escape.EscapeRules(),
    )
    assert escape.find_escape(*args) is None
    args = list(args)
    args[5] = [(Point(1, 0), "GND", 0.5, 0.2)]
    assert escape.find_escape(*args).via


def test_project_constraints_strengthen_defaults(tmp_path):
    p = tmp_path / "board.kicad_pro"
    p.write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_track_width": 0.3,
                            "min_through_hole_diameter": 0.4,
                            "min_via_annular_width": 0.15,
                        }
                    }
                },
                "net_settings": {"classes": [{"clearance": 0.4}]},
            }
        )
    )
    rules = escape.EscapeRules.from_project(p)
    assert rules.width == 0.3 and rules.clearance == 0.4
    assert rules.diameter == pytest.approx(0.7)
    p.with_suffix(".kicad_dru").write_text("(version 1)")
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


def _kct_managed_dru(*rule_lines):
    from kicad_tools.manufacturers.dru_generator import (
        DRU_FLOORS_BLOCK_BEGIN,
        DRU_FLOORS_BLOCK_END,
    )

    body = "\n".join(rule_lines)
    return f"(version 1)\n\n{DRU_FLOORS_BLOCK_BEGIN}\n{body}\n{DRU_FLOORS_BLOCK_END}\n"


def test_kct_managed_dru_sidecar_folds_in_floors(tmp_path):
    """A pure kct fab-floors sidecar (Issue #4600) is known-safe, not rejected.

    Every rule's floor here is set stronger than the ``EscapeRules``
    dataclass default so folding it in is observable (a weaker fab floor,
    like board 06's real 0.1016mm jlcpcb-tier1 trace width against the
    0.2mm default, would be masked by the "strengthen, never weaken"
    ``max()`` and prove nothing).
    """
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    p.with_suffix(".kicad_dru").write_text(
        _kct_managed_dru(
            '(rule "Trace Width - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'track'\")\n"
            "  (constraint track_width (min 0.3mm)))",
            '(rule "Clearance - jlcpcb-tier1"\n  (constraint clearance (min 0.25mm)))',
            '(rule "Via Drill - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
            "  (constraint hole_size (min 0.35mm)))",
            '(rule "Via Diameter - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'via' && A.Via_Type != 'Micro'\")\n"
            "  (constraint via_diameter (min 0.6mm)))",
            '(rule "Annular Ring - jlcpcb-tier1"\n'
            "  (condition \"A.Via_Type != 'Micro'\")\n"
            "  (constraint annular_width (min 0.2mm)))",
            '(rule "PTH Annular Ring - jlcpcb-tier1"\n'
            "  (condition \"A.Type == 'pad'\")\n"
            "  (constraint annular_width (min 0.5mm)))",
            '(rule "Copper to Edge - jlcpcb-tier1"\n  (constraint edge_clearance (min 0.3mm)))',
        )
    )
    rules = escape.EscapeRules.from_project(p)
    assert rules.width == pytest.approx(0.3)
    assert rules.clearance == pytest.approx(0.25)
    assert rules.drill == pytest.approx(0.35)
    # The pad-only "PTH Annular Ring" floor (0.5mm) must not leak into the
    # via annulus -- only the via "Annular Ring" floor (0.2mm) applies.
    assert rules.annulus == pytest.approx(0.2)
    # diameter is re-derived from drill + 2*annulus (0.35 + 0.4 = 0.75),
    # which exceeds the raw "Via Diameter" floor (0.6mm).
    assert rules.diameter == pytest.approx(0.75)


def test_kct_managed_dru_sidecar_with_extra_content_still_rejected(tmp_path):
    """Anything beyond the exact managed block is unmodeled -- fail closed."""
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    managed = _kct_managed_dru(
        '(rule "Clearance - jlcpcb-tier1"\n  (constraint clearance (min 0.1016mm)))'
    )
    p.with_suffix(".kicad_dru").write_text(
        managed + '\n(rule "Custom hand rule"\n  (constraint clearance (min 1mm)))\n'
    )
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


def test_kct_managed_dru_sidecar_with_unknown_rule_family_rejected(tmp_path):
    """A rule family this recipe has never enumerated is not silently ignored."""
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({}))
    p.with_suffix(".kicad_dru").write_text(
        _kct_managed_dru(
            '(rule "Something New - jlcpcb-tier1"\n  (constraint clearance (min 1mm)))'
        )
    )
    with pytest.raises(ValueError, match="custom DRC"):
        escape.EscapeRules.from_project(p)


@pytest.mark.parametrize("value", [-1, True, "0.2", float("nan")])
def test_invalid_project_dimensions_fail_closed(tmp_path, value):
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": value}}}}))
    with pytest.raises(ValueError, match="finite nonnegative"):
        escape.EscapeRules.from_project(p)


@pytest.fixture
def pour_recipe(monkeypatch):
    root = Path(__file__).resolve().parents[1] / "boards/06-diffpair-test"
    # Board scripts use sibling imports; isolate them from other recipe tests.
    for name in ("generate_pcb", "generate_schematic", "generate_design", "pour_escape"):
        module_spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
        module = importlib.util.module_from_spec(module_spec)
        monkeypatch.setitem(sys.modules, name, module)
        module_spec.loader.exec_module(module)
    return sys.modules["generate_design"]


def _write_isolated_via_board(path):
    """Two power islands separated by a foreign trace on every copper layer."""
    lines = [
        '(kicad_pcb (version 20240108) (generator "test")',
        "  (general (thickness 1.6))",
        '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))',
        '  (net 0 "") (net 1 "+3V3") (net 2 "barrier")',
        "  (gr_rect (start 98.5 47.5) (end 198.5 127.5) "
        '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))',
    ]
    # Primary has two pads; the stranded pad already has a legal via and
    # a small B.Cu fill island. Every fixed straight bridge hits the barrier.
    for ref, x, layer in (("U1", 110, "F.Cu"), ("J1", 120.5, "B.Cu"), ("J2", 121.5, "B.Cu")):
        lines.append(
            f'  (footprint "test:pad" (layer "{layer}") (at {x} 70) '
            f'(property "Reference" "{ref}" (at 0 0) (layer "F.SilkS")) '
            f'(pad "1" smd circle (at 0 0) (size 0.3 0.3) '
            f'(layers "{layer}") (net 1 "+3V3")))'
        )
    lines += [
        "  (segment (start 110 70) (end 110.7 70) (width 0.2) "
        '(layer "F.Cu") (net 1) (uuid "existing-stub"))',
        "  (via (at 110.7 70) (size 0.5) (drill 0.3) "
        '(layers "F.Cu" "B.Cu") (net 1) (uuid "existing-via"))',
    ]
    for layer in ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu"):
        lines.append(
            f"  (segment (start 115 67) (end 115 73) (width 0.4) "
            f'(layer "{layer}") (net 2) (uuid "barrier-{layer}"))'
        )
    for x1, y1, x2, y2 in ((109.8, 69.6, 111.1, 70.4), (120, 69, 122, 71)):
        pts = f"(pts (xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2}))"
        lines.append(
            '  (zone (net 1) (net_name "+3V3") (layer "B.Cu") '
            f'(fill yes) (polygon {pts}) (filled_polygon (layer "B.Cu") {pts}))'
        )
    path.write_text("\n".join(lines) + "\n)\n")
    path.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "board": {
                    "design_settings": {
                        "rules": {
                            "min_clearance": 0.2,
                            "min_track_width": 0.2,
                            "min_through_hole_diameter": 0.3,
                            "min_via_annular_width": 0.1,
                            "min_hole_to_hole": 0.5,
                        }
                    }
                }
            }
        )
    )


def test_repair_escapes_pad_with_existing_via_on_isolated_fill(tmp_path, pour_recipe, capsys):
    from shapely.ops import unary_union

    board = tmp_path / "island.kicad_pcb"
    _write_isolated_via_board(board)
    before_text = board.read_text()
    project = board.with_suffix(".kicad_pro")
    before_rules = project.read_bytes()
    rules = escape.EscapeRules.from_project(project)
    _, before_segments, before_vias = pour_recipe._parse_copper(before_text)
    before = pour_recipe._audit_pour_nets(board, ["+3V3"])["+3V3"]
    assert not before["connected"] and len(before["pad_groups"]) == 2
    assert len(before_vias) == 1

    placed_vias, bridges = pour_recipe._repair_pour_connectivity(board, ["+3V3"])

    assert placed_vias == 1 and bridges == 1
    assert "Grid escape: +3V3 U1.1" in capsys.readouterr().out
    assert pour_recipe._audit_pour_nets(board, ["+3V3"])["+3V3"]["connected"]
    after_text = board.read_text()
    _, segments, vias = pour_recipe._parse_copper(after_text)
    added_segments = segments[len(before_segments) :]
    added_vias = vias[len(before_vias) :]
    assert len(added_segments) > 1 and len(added_vias) == 1
    assert all(seg["net"] == "+3V3" and seg["layer"] == "F.Cu" for seg in added_segments)
    assert all(seg["w"] == pytest.approx(rules.width) for seg in added_segments)
    via = added_vias[0]
    assert via["size"] == pytest.approx(rules.diameter)
    assert via["drill"] == pytest.approx(rules.drill)
    assert (via["size"] - via["drill"]) / 2 == pytest.approx(rules.annulus)

    # Independently check the committed copper forms a complete path from
    # U1 to the new via, which actually contacts the primary B.Cu fill.
    copper = unary_union(
        [
            LineString([(seg["x1"], seg["y1"]), (seg["x2"], seg["y2"])]).buffer(seg["w"] / 2)
            for seg in added_segments
        ]
    )
    via_point = Point(via["x"], via["y"])
    assert copper.geom_type == "Polygon"
    assert copper.intersects(Point(110, 70)) and copper.intersects(via_point)
    assert via_point.buffer(via["size"] / 2).intersects(box(120, 69, 122, 71))
    barrier = LineString([(115, 67), (115, 73)]).buffer(0.2)
    assert copper.distance(barrier) >= rules.clearance - 1e-6
    assert via_point.buffer(via["size"] / 2).distance(barrier) >= rules.clearance
    old_via = before_vias[0]
    assert (
        via_point.distance(Point(old_via["x"], old_via["y"]))
        - (via["drill"] + old_via["drill"]) / 2
        >= rules.hole_gap
    )
    for x in (110, 120.5, 121.5):
        assert not via_point.buffer(via["size"] / 2).intersects(Point(x, 70).buffer(0.15))

    # Existing authored geometry and constraints are preserved byte-for-byte.
    for kind in ("footprint", "segment", "via", "zone"):
        assert all(
            block in after_text for block in pour_recipe._find_sexp_blocks(before_text, f"({kind}")
        )
    assert project.read_bytes() == before_rules


@pytest.mark.parametrize("layer", ["F.Cu", "In1.Cu"])
def test_distant_primary_uses_fixed_budget_for_forward_progress(layer):
    """A wide free area must not spend the whole budget expanding sideways."""
    primary = box(39.5, -1, 40.5, 1)
    result = escape.find_escape(
        (0, 0),
        "power",
        "F.Cu",
        [],
        [],
        [],
        [(primary, {layer}, "fill")],
        (-45, -45, 45, 45),
        escape.EscapeRules(),
        node_budget=10_000,
    )
    assert result is not None
    assert result.via == (layer != "F.Cu")
    assert result.points[0] == (0, 0)
    endpoint = Point(result.points[-1])
    contact = endpoint.buffer(result.rules.diameter / 2) if result.via else endpoint
    assert primary.intersects(contact)


def test_missing_primary_returns_no_repair():
    assert (
        escape.find_escape(
            (0, 0),
            "power",
            "F.Cu",
            [],
            [],
            [],
            [],
            (-1, -1, 1, 1),
            escape.EscapeRules(),
        )
        is None
    )
