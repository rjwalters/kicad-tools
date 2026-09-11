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


@pytest.mark.parametrize("value", [-1, True, "0.2", float("nan")])
def test_invalid_project_dimensions_fail_closed(tmp_path, value):
    p = tmp_path / "board.kicad_pro"
    p.write_text(json.dumps({"board": {"design_settings": {"rules": {"min_clearance": value}}}}))
    with pytest.raises(ValueError, match="finite nonnegative"):
        escape.EscapeRules.from_project(p)
