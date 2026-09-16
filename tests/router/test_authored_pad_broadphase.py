"""Broad-phase rejection must preserve the exact authored pad predicate."""

import random

import pytest

import kicad_tools.router.grid as module
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Segment
from kicad_tools.router.rules import DesignRules


def test_far_pads_do_not_enter_exact_rectangle_distance(monkeypatch):
    grid = RoutingGrid(10, 10, DesignRules(net_clearance_floors={2: 0.2}))
    calls = []
    exact = module._pad_rect_segment_centerline_distance

    def measured(*args):
        calls.append(args)
        return exact(*args)

    monkeypatch.setattr(module, "_pad_rect_segment_centerline_distance", measured)
    pads = [Pad(50 + i, 50, 1, 2, 2, "foreign", rotation=45) for i in range(100)]
    assert grid.authored_segment_pads_clear(Segment(1, 1, 1, 1, 0.6, Layer.F_CU, 1), pads)
    assert not calls


@pytest.mark.parametrize("rotation", [0, 30, 45, 89, 135, 270])
@pytest.mark.parametrize("shape", ["rect", "circle"])
def test_broadphase_matches_exact_geometry_near_rotated_pad(rotation, shape):
    rules = DesignRules(net_clearance_floors={1: 0.2, 2: 0.4})
    grid = RoutingGrid(10, 10, rules)
    pad = Pad(5, 5, 3, 0.4, 2, "foreign", rotation=rotation, shape=shape)
    rng = random.Random(5398)
    for _ in range(100):
        x, y = rng.uniform(2, 8), rng.uniform(2, 8)
        seg = Segment(x, y, x + rng.choice([0, 0.5]), y + rng.choice([0, 0.5]), 0.6, Layer.F_CU, 1)
        if shape == "circle":
            distance = grid._point_to_segment_distance(5, 5, seg.x1, seg.y1, seg.x2, seg.y2) - 1.5
        else:
            distance = module._pad_rect_segment_centerline_distance(
                pad, seg.x1, seg.y1, seg.x2, seg.y2
            )
        expected = distance - seg.width / 2 >= rules.clearance_for_nets(1, 2, 0) - 1e-9
        assert grid.authored_segment_pads_clear(seg, [pad]) == expected


def test_bound_tracks_late_floor_changes_without_widening_other_pairs():
    rules = DesignRules(net_clearance_floors={1: 0.2, 2: 0.2, 99: 8.0})
    grid = RoutingGrid(10, 10, rules)
    pad = Pad(5, 5, 1, 1, 2, "foreign")
    segment = Segment(3, 4, 3, 6, 0.2, Layer.F_CU, 1)
    assert grid.authored_segment_pads_clear(segment, [pad])
    rules.net_clearance_floors[2] = 2.0
    assert not grid.authored_segment_pads_clear(segment, [pad])
    rules.net_clearance_floors.clear()
    assert grid.authored_segment_pads_clear(segment, [pad])
