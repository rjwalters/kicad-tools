"""Issue #6098: don't re-run the pure-Python A* on a net it already proved unreachable.

When the C++ search gives up, ``CppPathfinder`` hands the net to the
10-100x-slower pure-Python A*.  The negotiated loop re-presents an unroutable
net on every iteration, rescue pass and rip-up probe, so one walled-in net
paid that slow search dozens of times (33 runs, ~70 s of a 73 s
``route-auto --strategy hierarchical`` run on the #6008 wall board).

A C++ "open set exhausted" alone is NOT a proof for the Python search: the two
searches have different passability (#3456 -- the Python A* rescues nets the
C++ search drains on).  So the first give-up still falls back.  What IS a
proof is the Python search's own drain: no goal reached, every reachable cell
expanded.  ``CppPathfinder`` memoizes that per request and grid state, and a
later give-up on an identical grid skips the rerun.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(not is_cpp_available(), reason="C++ router backend unavailable")


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )


def _grid(wall: bool = True) -> RoutingGrid:
    """A 10x6 mm two-layer grid with a full-height wall at x = 5 mm."""
    grid = RoutingGrid(width=10.0, height=6.0, rules=_rules(), layer_stack=LayerStack.two_layer())
    if wall:
        col = grid.world_to_grid(5.0, 3.0)[0]
        grid._blocked[:, :, col - 2 : col + 3] = True
        grid._is_obstacle[:, :, col - 2 : col + 3] = True
        grid.bump_occupancy_generation()
    return grid


def _pads() -> tuple[Pad, Pad]:
    common = {"width": 0.6, "height": 0.6, "net": 1, "net_name": "SIG", "layer": Layer.F_CU}
    return Pad(x=2.0, y=3.0, **common), Pad(x=8.0, y=3.0, **common)


# ---------------------------------------------------------------------------
# Router.last_route_exhausted: only a drained search is a proof
# ---------------------------------------------------------------------------


def test_drained_search_is_a_proof() -> None:
    router = Router(_grid(), _rules())
    assert router.route(*_pads()) is None
    assert router.last_route_exhausted is True


def test_success_is_not_a_proof() -> None:
    router = Router(_grid(wall=False), _rules())
    assert router.route(*_pads()) is not None
    assert router.last_route_exhausted is False


def test_iteration_capped_search_is_not_a_proof() -> None:
    router = Router(_grid(), _rules())
    router._max_iterations_override = 5
    assert router.route(*_pads()) is None
    assert router.last_route_exhausted is False


def test_drain_after_a_rejected_goal_is_not_a_proof() -> None:
    """Which goal survives path validation depends on search order."""
    router = Router(_grid(wall=False), _rules())
    with mock.patch.object(Router, "_reconstruct_route", return_value=None):
        assert router.route(*_pads()) is None
    assert router.last_route_exhausted is False


def test_proof_is_reset_by_the_next_route() -> None:
    router = Router(_grid(), _rules())
    router.route(*_pads())
    assert router.last_route_exhausted is True
    router._max_iterations_override = 5
    router.route(*_pads())
    assert router.last_route_exhausted is False


# ---------------------------------------------------------------------------
# CppPathfinder: the memo
# ---------------------------------------------------------------------------


def _pathfinder(grid: RoutingGrid) -> CppPathfinder:
    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp_grid, _rules(), diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder


def _count_python_runs(pathfinder: CppPathfinder, calls: int, **kwargs) -> int:
    """Route the walled net ``calls`` times; return how often Python ran."""
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        for _ in range(calls):
            assert pathfinder.route(*_pads(), **kwargs) is None
    return spy.call_count


@requires_cpp
def test_repeat_give_up_on_an_unchanged_grid_skips_python() -> None:
    pathfinder = _pathfinder(_grid())
    assert _count_python_runs(pathfinder, 4) == 1
    stats = pathfinder.fallback_stats
    assert stats["python_fallback_proven_skips"] == 3
    assert stats["python_fallback_seconds_saved"] > 0.0


@requires_cpp
def test_cost_knobs_do_not_defeat_the_memo() -> None:
    """``present_cost_factor`` grows every negotiated iteration; it reorders a
    drained search but cannot change what it reaches."""
    pathfinder = _pathfinder(_grid())
    with mock.patch.object(Router, "route", autospec=True, side_effect=Router.route) as spy:
        for factor in (0.5, 1.0, 2.0, 4.0):
            pathfinder.route(*_pads(), negotiated_mode=True, present_cost_factor=factor)
    assert spy.call_count == 1


@requires_cpp
def test_a_changed_grid_runs_python_again() -> None:
    grid = _grid()
    pathfinder = _pathfinder(grid)
    assert _count_python_runs(pathfinder, 1) == 1
    grid._pad_blocked[0, 0, 0] = not grid._pad_blocked[0, 0, 0]
    assert _count_python_runs(pathfinder, 1) == 1


@requires_cpp
def test_generation_bump_alone_keeps_the_proof() -> None:
    """Rip-up/restore bumps the generation with byte-identical planes."""
    grid = _grid()
    pathfinder = _pathfinder(grid)
    _count_python_runs(pathfinder, 1)
    grid.bump_occupancy_generation()
    assert _count_python_runs(pathfinder, 1) == 0


@requires_cpp
def test_a_different_request_runs_python() -> None:
    pathfinder = _pathfinder(_grid())
    _count_python_runs(pathfinder, 1)
    # Negotiated sharing changes passability, so it is a different request.
    assert _count_python_runs(pathfinder, 1, negotiated_mode=True) == 1
    pathfinder.set_relief_mode(True)
    assert _count_python_runs(pathfinder, 1, negotiated_mode=True) == 1


@requires_cpp
def test_an_unproven_python_failure_is_not_memoized() -> None:
    """A Python failure that did not drain (cap, timeout, rejected goal) is
    retried: only the Python search's own drain is a proof."""
    pathfinder = _pathfinder(_grid())

    def capped(self, *args, **kwargs):
        self.last_route_exhausted = False
        return None

    with mock.patch.object(Router, "route", autospec=True, side_effect=capped) as spy:
        for _ in range(3):
            pathfinder.route(*_pads())
    assert spy.call_count == 3
    assert pathfinder.fallback_stats["python_fallback_proven_skips"] == 0


@requires_cpp
def test_opt_out_restores_every_fallback(monkeypatch) -> None:
    monkeypatch.setenv("KICAD_ROUTER_SKIP_PROVEN_FALLBACK", "0")
    pathfinder = _pathfinder(_grid())
    assert _count_python_runs(pathfinder, 3) == 3


@requires_cpp
def test_first_give_up_still_falls_back() -> None:
    """A C++ drain is not a proof for the Python search (#3456)."""
    pathfinder = _pathfinder(_grid())
    assert _count_python_runs(pathfinder, 1) == 1
    assert pathfinder.fallback_stats["python_fallback_proven_skips"] == 0


# ---------------------------------------------------------------------------
# Issue #6133: the fingerprint covers reservations and fill/keepout CONTENT
# ---------------------------------------------------------------------------


@requires_cpp
def test_unchanged_board_with_extra_state_still_hits() -> None:
    grid = _grid()
    grid._reserved_for_nets[(0, 5, 5)] = frozenset({1, 2})
    pathfinder = _pathfinder(grid)
    assert _count_python_runs(pathfinder, 3) == 1


@requires_cpp
def test_a_changed_reservation_invalidates_the_proof() -> None:
    grid = _grid()
    pathfinder = _pathfinder(grid)
    assert _count_python_runs(pathfinder, 1) == 1
    grid._reserved_for_nets[(0, 5, 5)] = frozenset({1, 2})
    assert _count_python_runs(pathfinder, 1) == 1
    # Same key, different owners (e.g. a re-reservation) also invalidates.
    grid._reserved_for_nets[(0, 5, 5)] = frozenset({3})
    assert _count_python_runs(pathfinder, 1) == 1
    # Hard -> soft flips the same cell from fence to attractor.
    grid._soft_reservations.add((0, 5, 5))
    assert _count_python_runs(pathfinder, 1) == 1
    assert _count_python_runs(pathfinder, 1) == 0


@requires_cpp
def test_changed_fill_contents_invalidate_the_proof() -> None:
    from shapely.geometry import box

    from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles

    def fills(x1: float) -> FixedFillObstacles:
        return FixedFillObstacles(fills=(FixedFill("GND", 9, 0, 0.2, box(0.0, 0.0, x1, 1.0)),))

    grid = _grid()
    pathfinder = _pathfinder(grid)
    grid.fixed_fills = fills(1.0)
    assert _count_python_runs(pathfinder, 1) == 1
    # Equal content in a NEW object keeps the proof; different content drops it.
    grid.fixed_fills = fills(1.0)
    assert _count_python_runs(pathfinder, 1) == 0
    grid.fixed_fills = fills(2.0)
    assert _count_python_runs(pathfinder, 1) == 1


@requires_cpp
def test_changed_keepout_contents_invalidate_the_proof() -> None:
    import numpy as np

    from kicad_tools.router.rule_area_grid import GridRuleArea

    area = GridRuleArea(
        name="k",
        polygon=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
        layers=frozenset({0}),
        blocks_tracks=True,
        blocks_vias=False,
        only=None,
        exempt=frozenset(),
        gx0=0,
        gy0=0,
        mask=np.zeros((4, 4), dtype=bool),
    )
    grid = _grid()
    grid._rule_area_keepouts = [area]
    pathfinder = _pathfinder(grid)
    assert _count_python_runs(pathfinder, 1) == 1
    assert _count_python_runs(pathfinder, 1) == 0
    area.mask[1, 1] = True  # same list object, same length, new contents
    assert _count_python_runs(pathfinder, 1) == 1


@requires_cpp
def test_changed_net_name_map_invalidates_the_proof() -> None:
    pathfinder = _pathfinder(_grid())
    assert _count_python_runs(pathfinder, 1) == 1
    pathfinder.set_net_name_to_id({"SIG": 1})
    assert _count_python_runs(pathfinder, 1) == 1
    assert _count_python_runs(pathfinder, 1) == 0
