"""A selected phase is relative to the board, and survives router reconstruction."""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.io import (
    PadPosition,
    _count_off_grid_with_offset,
    auto_select_grid_resolution,
)
from kicad_tools.router.parallel import ParallelRouter
from kicad_tools.router.rules import DesignRules


@pytest.mark.parametrize("origin", [(0.0, 0.0), (100.0, 100.0), (93.5, 40.0), (-12.3, 7.4)])
def test_selected_phase_matches_real_grid_in_translated_board(origin):
    pads = [
        PadPosition(origin[0] + 0.038 + i * 0.127, origin[1] + j * 0.127)
        for i in range(4)
        for j in range(2)
    ]
    selected = auto_select_grid_resolution(pads, 0.3, candidates=[0.127], board_origin=origin)
    rules = DesignRules(grid_resolution=0.127, grid_origin_offset=selected.origin_offset)
    grid = RoutingGrid(2, 2, rules, origin_x=origin[0], origin_y=origin[1])
    assert selected.origin_offset == pytest.approx((0.038, 0))
    assert (
        _count_off_grid_with_offset(pads, 0.127, grid.origin_x, grid.origin_y)
        == selected.off_grid_pads
        == 0
    )


@pytest.mark.parametrize("mode", ["reset", "serialize", "parallel"])
def test_router_reconstruction_applies_phase_exactly_once(mode):
    rules = DesignRules(grid_resolution=0.127, grid_origin_offset=(0.038, 0.051))
    router = Autorouter(2, 2, origin_x=100, origin_y=40, rules=rules, force_python=True)
    expected = (router.grid.origin_x, router.grid.origin_y)
    if mode == "reset":
        router._reset_for_new_trial()
        router._reset_for_new_trial()
    elif mode == "serialize":
        cfg = router._serialize_for_parallel()
        router = Autorouter(
            cfg["width"],
            cfg["height"],
            origin_x=cfg["origin_x"],
            origin_y=cfg["origin_y"],
            rules=DesignRules(**cfg["rules_dict"]),
            force_python=True,
        )
    else:
        ParallelRouter(router).route_parallel()
    assert (router.grid.origin_x, router.grid.origin_y) == pytest.approx(expected)


def test_board07_selection_matches_grid_count_for_real_pad_census():
    from pathlib import Path

    from kicad_tools.router.io import extract_board_origin, extract_pad_positions

    pcb = (
        Path(__file__).parents[1]
        / "boards/07-matchgroup-test/regression-fixture/matchgroup_test.kicad_pcb"
    )
    pads = extract_pad_positions(pcb)
    origin = extract_board_origin(pcb)
    assert origin is not None and len(pads) == 244
    selected = auto_select_grid_resolution(pads, 0.3, candidates=[0.127], board_origin=origin)
    grid = RoutingGrid(
        110,
        95,
        DesignRules(grid_resolution=0.127, grid_origin_offset=selected.origin_offset),
        origin_x=origin[0],
        origin_y=origin[1],
    )
    actual = sum(
        any(
            abs((value - base) / 0.127 - round((value - base) / 0.127)) > 0.1
            for value, base in ((p.x, grid.origin_x), (p.y, grid.origin_y))
        )
        for p in pads
    )
    assert actual == selected.off_grid_pads


def test_adaptive_coarse_selection_keeps_fine_zone_offsets_in_world_frame():
    from kicad_tools.router.io import _is_on_grid_with_offset, compute_multi_resolution_plan
    from kicad_tools.router.primitives import Pad

    origin = (100.038, 40.051)
    pads = [
        Pad(
            origin[0] + i * 0.4,
            origin[1],
            0.15,
            0.15,
            net=i + 1,
            net_name=f"N{i}",
            ref="U1",
            pin=str(i),
        )
        for i in range(8)
    ]
    plan = compute_multi_resolution_plan(
        pads, 0.15, board_width=10, board_height=10, board_origin=origin
    )
    assert plan is not None and plan.fine_zones
    zone = plan.fine_zones[0]
    assert all(zone.contains(p.x, p.y) for p in pads)
    assert all(
        _is_on_grid_with_offset(p.x, zone.resolution, zone.x_offset)
        and _is_on_grid_with_offset(p.y, zone.resolution, zone.y_offset)
        for p in pads
    )
