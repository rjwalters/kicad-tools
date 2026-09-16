"""Physical goal-directed progress with an unchanged distinct-node ceiling."""

import importlib.util
import sys
from pathlib import Path

import pytest
from shapely.geometry import LineString, box

spec = importlib.util.spec_from_file_location(
    "pour_escape_goal",
    Path(__file__).resolve().parents[1] / "boards/06-diffpair-test/pour_escape.py",
)
escape = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = escape
spec.loader.exec_module(escape)


def search(primary, segments=(), *, bounds=(-2, -4, 25, 4), budget=1400):
    return escape.find_escape(
        (0, 0),
        "power",
        "F.Cu",
        [],
        segments,
        [],
        primary,
        bounds,
        escape.EscapeRules(),
        step=0.1,
        node_budget=budget,
    )


@pytest.mark.parametrize("layer,via", [("F.Cu", False), ("In1.Cu", True)])
def test_distant_target_reached_with_small_unchanged_budget(layer, via):
    result = search([(box(19.8, -0.2, 20.2, 0.2), {layer}, "fill")])
    assert result is not None and result.via == via
    assert result.points[0] == (0, 0)
    assert result.points[-1][0] > 19.5
    assert result.rules == escape.EscapeRules()


def test_goal_ordering_detours_and_checks_entire_emitted_segments():
    wall = box(2, -1, 2.3, 1)
    target = box(5, -0.3, 5.5, 0.3)
    result = search(
        [(target, {"F.Cu"}, "fill")],
        [(wall, "foreign", "F.Cu")],
        bounds=(-1, -3, 7, 3),
        budget=5000,
    )
    assert result is not None and not result.via
    path = LineString(result.points)
    assert path.distance(wall) >= result.rules.clearance + result.rules.width / 2
    assert any(abs(y) > 1.3 for x, y in result.points)
    assert target.buffer(-0.025).intersects(path.boundary.geoms[-1])


def test_impossible_wall_drains_bounded_region_without_partial_result():
    assert (
        search(
            [(box(2, -0.2, 2.4, 0.2), {"F.Cu"}, "fill")],
            [(box(0.5, -2, 0.8, 2), "foreign", "F.Cu")],
            bounds=(-0.2, -0.2, 3, 0.2),
            budget=200,
        )
        is None
    )


def test_tiny_distinct_node_cap_returns_no_partial_path():
    assert search([(box(20, -0.2, 20.4, 0.2), {"F.Cu"}, "fill")], budget=10) is None
