"""Factory-generated scoped floors must reach the physical escape search."""

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from shapely.geometry import Point, box

from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.manufacturers.dru_generator import generate_dru, merge_dru_floors

spec = importlib.util.spec_from_file_location(
    "pour_escape_factory",
    Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/pour_escape.py",
)
escape = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = escape
spec.loader.exec_module(escape)


def factory_text(**overrides):
    values = {
        "min_trace_width_mm": 0.1016,
        "min_clearance_mm": 0.1016,
        "min_via_drill_mm": 0.2,
        "min_via_diameter_mm": 0.4,
        "min_annular_ring_mm": 0.1,
        "min_silk_to_pad_clearance_mm": 0.15,
        "min_smd_pad_clearance_mm": 0.15,
        "min_pth_hole_to_track_mm": 0.28,
        "min_inner_pth_hole_to_copper_mm": 0.3,
    }
    values.update(overrides)
    return merge_dru_floors(None, generate_dru(DesignRules(**values), "jlcpcb"))


def load_rules(tmp_path, text):
    project = tmp_path / "board.kicad_pro"
    project.write_text(json.dumps({}))
    project.with_suffix(".kicad_dru").write_text(text)
    return escape.EscapeRules.from_project(project)


def test_actual_factory_output_preserves_scoped_floors(tmp_path):
    rules = load_rules(tmp_path, factory_text())
    assert rules.pth_hole_track == 0.28
    assert rules.inner_pth_hole_copper == 0.3
    assert rules.hole_copper == 0.25
    assert rules.hole_gap == 0.5
    assert rules.clearance == 0.2
    assert rules.width == 0.2
    # Pad-pad and silk-pad floors do not apply to emitted tracks or vias.
    strong = load_rules(
        tmp_path, factory_text(min_smd_pad_clearance_mm=1.2, min_silk_to_pad_clearance_mm=1.4)
    )
    assert strong == rules


@pytest.mark.parametrize(
    "before,after",
    [
        ("(layer inner)", "(layer outer)"),
        ("  (layer inner)\n", ""),
        ("A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD'", "A.Type == 'Track'"),
        ("constraint silk_clearance", "constraint clearance"),
        ("constraint hole_clearance", "constraint physical_clearance"),
        ("A.Pad_Type == 'Through-hole' || B.Pad_Type == 'Through-hole'", "A.Type == 'Via'"),
        ("(min 0.28mm)", "(min 0..28mm)"),
        ("(version 1)", "(version 2)"),
        ("PTH Hole to Track -", "Unknown Copper Rule -"),
        ("(layer inner)", "(layer inner)\n  (severity ignore)"),
    ],
)
def test_changed_factory_scope_or_malformed_rule_fails_closed(tmp_path, before, after):
    text = factory_text()
    assert before in text
    with pytest.raises(ValueError, match="custom DRC rules"):
        load_rules(tmp_path, text.replace(before, after))


def test_extra_custom_rule_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="custom DRC rules"):
        load_rules(tmp_path, factory_text() + '(rule "custom" (constraint clearance (min 1mm)))')


def trace_escape(rules, layer, distance, legacy=False):
    center = (0, distance)
    pad = (Point(center).buffer(0.32), "foreign", {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}, 0.3, center)
    if not legacy:
        pad += ("J1.1", True, Point(center).buffer(0.32))
    return escape.find_escape(
        (0, 0),
        "power",
        layer,
        [pad],
        [],
        [],
        [(box(0.8, -0.2, 1.2, 0.2), {layer}, "fill")],
        (-0.1, -0.01, 1.3, 0.01),
        rules,
        node_budget=1000,
    )


@pytest.mark.parametrize("legacy", [False, True])
def test_factory_pth_track_floor_rejects_previously_allowed_path(tmp_path, legacy):
    rules = load_rules(tmp_path, factory_text())
    # Edge gap: 0.66 - hole radius0.30 - half trace width0.10 =0.26mm.
    old = trace_escape(escape.EscapeRules(), "F.Cu", 0.66, legacy)
    assert old is not None and not old.via
    assert trace_escape(rules, "F.Cu", 0.66, legacy) is None
    legal = trace_escape(rules, "F.Cu", 0.72, legacy)
    assert legal is not None and legal.rules == rules
    assert legal.rules.width == 0.2


def test_inner_scope_does_not_strengthen_outer_track(tmp_path):
    rules = load_rules(tmp_path, factory_text())
    # Hole-to-trace gap0.29 clears outer0.28, fails inner0.30.
    assert trace_escape(rules, "F.Cu", 0.69) is not None
    assert trace_escape(rules, "B.Cu", 0.69) is not None
    assert trace_escape(rules, "In1.Cu", 0.69) is None
    assert trace_escape(rules, "In2.Cu", 0.69) is None


def test_inner_pth_floor_reaches_new_through_via_copper(tmp_path):
    rules = load_rules(tmp_path, factory_text(min_inner_pth_hole_to_copper_mm=0.8))
    # Larger inner floor makes its barrel exclusion observable over hole-hole
    # clearance; a zero-length grid bounds forces the sole candidate via.
    center = (0, 1.2)
    pads = [
        (Point(center).buffer(0.32), "foreign", {"F.Cu", "In1.Cu", "In2.Cu", "B.Cu"}, 0.3, center)
    ]
    args = (
        (0, 0),
        "power",
        "F.Cu",
        pads,
        [],
        [],
        [(box(-0.1, -0.1, 0.1, 0.1), {"In1.Cu"}, "fill")],
        (-0.3, -0.3, 0.3, 0.3),
    )
    before = escape.find_escape(*args, replace(rules, inner_pth_hole_copper=0), node_budget=1)
    assert before is not None and before.via
    assert escape.find_escape(*args, rules, node_budget=1) is None


def test_inner_floor_requires_existing_pad_inner_copper(tmp_path):
    rules = load_rules(tmp_path, factory_text(min_inner_pth_hole_to_copper_mm=0.8))
    center = (0, 1.2)
    pads = [(Point(center).buffer(0.32), "foreign", {"F.Cu", "B.Cu"}, 0.3, center)]
    result = escape.find_escape(
        (0, 0),
        "power",
        "F.Cu",
        pads,
        [],
        [],
        [(box(-0.1, -0.1, 0.1, 0.1), {"In1.Cu"}, "fill")],
        (-0.3, -0.3, 0.3, 0.3),
        rules,
        node_budget=1,
    )
    assert result is not None and result.via


def test_nondrilled_smd_is_unaffected_by_pth_floors(tmp_path):
    rules = load_rules(
        tmp_path, factory_text(min_pth_hole_to_track_mm=2, min_inner_pth_hole_to_copper_mm=3)
    )
    center = (0, 0.66)
    pads = [(Point(center).buffer(0.32), "foreign", {"F.Cu"}, 0, center)]
    result = escape.find_escape(
        (0, 0),
        "power",
        "F.Cu",
        pads,
        [],
        [],
        [(box(0.8, -0.2, 1.2, 0.2), {"F.Cu"}, "fill")],
        (-0.1, -0.01, 1.3, 0.01),
        rules,
        node_budget=1000,
    )
    assert result is not None and not result.via


def test_generic_drill_gap_still_rejects_same_net_via(tmp_path):
    rules = load_rules(tmp_path, factory_text())
    # Same-net barrel joins are electrically allowed, but two0.3mm drills
    # at0.75mm have only0.45mm edge gap, below the unchanged0.5mm floor.
    result = escape.find_escape(
        (0, 0),
        "power",
        "F.Cu",
        [],
        [],
        [(Point(0, 0.75), "power", 0.25, 0.3)],
        [(box(-0.1, -0.1, 0.1, 0.1), {"In1.Cu"}, "fill")],
        (-0.3, -0.3, 0.3, 0.3),
        rules,
        node_budget=1,
    )
    assert result is None
