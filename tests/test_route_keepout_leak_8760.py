"""Issue #8760: the post-route optimizer straightened tracks through keepouts.

A ``(tracks not_allowed)`` rule area is stamped as static ``blocked`` cells
with ``net == 0``; the optimizer's collision checkers only acted on obstacle,
pad or foreign-net cells, so a shortcut pass collapsed the search's detour
back into a straight track across the area (found with ``--backend cpp``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.collision import GridCollisionChecker, make_collision_checker
from kicad_tools.router.optimizer.config import OptimizationConfig
from kicad_tools.router.optimizer.trace import TraceOptimizer, optimize_routes_grid_synced
from kicad_tools.router.rule_area_grid import committed_rule_area_hits

from .test_grid_keepout_rule_areas_6008 import BACKENDS, _board, _keepout, _load

# Sits on the straight line between the two pads (y = 108), off any pad.
SMALL = [(114.65, 107.65), (115.35, 107.65), (115.35, 108.35), (114.65, 108.35)]
MOUNT = [(113.8, 107.1), (116.2, 107.1), (116.2, 109.5), (113.8, 109.5)]

CASES = [
    pytest.param(SMALL, '"*.Cu"', id="small-star"),
    pytest.param(SMALL, '"F.Cu"', id="small-front"),
    pytest.param(MOUNT, '"F.Cu" "B.Cu"', id="mounting-hole-both"),
]


def _text(poly, layers) -> str:
    return _board(_keepout("mh", poly, layers=layers, vias="allowed"))


def _inside(seg, poly) -> bool:
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    n = 200
    for i in range(n + 1):
        x = seg.x1 + (seg.x2 - seg.x1) * i / n
        y = seg.y1 + (seg.y2 - seg.y1) * i / n
        if min(xs) <= x <= max(xs) and min(ys) <= y <= max(ys):
            return True
    return False


@pytest.mark.parametrize("poly,layers", CASES)
@pytest.mark.parametrize("force_python", BACKENDS)
def test_optimizer_checker_refuses_shortcut_through_rule_area(
    tmp_path: Path, force_python: bool, poly, layers
) -> None:
    router = _load(tmp_path, _text(poly, layers), force_python)
    router._install_grid_rule_area_keepouts()
    checker = GridCollisionChecker(router.grid)
    assert not checker.path_is_clear(105, 108, 125, 108, Layer.F_CU, 0.2, 1)
    # A path well clear of the area stays legal.
    assert checker.path_is_clear(105, 104, 125, 104, Layer.F_CU, 0.2, 1)


@pytest.mark.parametrize("poly,layers", CASES)
@pytest.mark.parametrize("force_python", BACKENDS)
def test_optimized_route_stays_out_of_rule_area(
    tmp_path: Path, force_python: bool, poly, layers
) -> None:
    router = _load(tmp_path, _text(poly, layers), force_python)
    router.route_all(suppress_no_timeout_warning=True)
    opt = TraceOptimizer(
        config=OptimizationConfig(
            merge_collinear=True,
            eliminate_zigzags=True,
            compress_staircase=True,
            convert_45_corners=True,
            corner_chamfer_size=0.5,
            minimize_vias=True,
        ),
        collision_checker=make_collision_checker(router.grid),
    )
    optimize_routes_grid_synced(router, opt)
    segs = [s for r in router.routes for s in r.segments]
    assert segs, "board must route"
    front_only = layers == '"F.Cu"'
    assert not any(_inside(s, poly) for s in segs if not front_only or s.layer == Layer.F_CU)
    assert committed_rule_area_hits(router.grid, router.routes) == []


def test_committed_hits_reports_leaked_track(tmp_path: Path) -> None:
    from kicad_tools.router.primitives import Route, Segment

    router = _load(tmp_path, _text(MOUNT, '"F.Cu"'), True)
    router._install_grid_rule_area_keepouts()
    leak = Route(
        net=1,
        net_name="/SIG",
        segments=[Segment(105, 108, 125, 108, 0.2, Layer.F_CU, 1)],
        vias=[],
    )
    assert committed_rule_area_hits(router.grid, [leak]) == [(1, "mh", "track")]
    ok = Route(
        net=1,
        net_name="/SIG",
        segments=[Segment(105, 104, 125, 104, 0.2, Layer.F_CU, 1)],
        vias=[],
    )
    assert committed_rule_area_hits(router.grid, [ok]) == []
