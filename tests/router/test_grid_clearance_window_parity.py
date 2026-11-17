"""The vectorised clearance-window walk must equal the per-cell walk.

Issue #5240: ``RoutingGrid._mark_segment`` / ``_unmark_segment`` used to
visit every cell of a segment's clearance envelope one at a time, allocating
a ``_CellView`` and paying two NumPy scalar round-trips per cell.  Board 06's
re-route drives ~29M such visits per run, so the envelope is now written with
slice assignment.  That is only a safe substitution if the block form makes
*exactly* the same per-cell decisions, including:

* which cells take the segment's net (only previously-unblocked ones);
* Issue #3545's static-blockage restore on rip-up (pad halos / keepouts keep
  their original owner instead of being freed);
* pad cells keeping ``blocked`` and getting ``original_net`` back;
* the ``_congestion`` / ``_congestion_counted`` ledger, which must stay
  idempotent across the overlapping windows of adjacent line points;
* Issue #4794's occupancy-generation counter advancing on every write.

These tests run both implementations over the same randomised boards and
compare full grid state.  The scalar walk is selected by giving the grid a
backend object that is not the ``numpy`` module itself (the production guard
that keeps GPU-backed grids on the per-cell path), so nothing about the
comparison depends on editing the code under test.
"""

from __future__ import annotations

import random

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.primitives import Layer, Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules

SIGNAL_LAYERS = (Layer.F_CU, Layer.B_CU)


class _ProxyBackend:
    """A NumPy stand-in that is not ``numpy`` itself.

    ``_mark_segment`` / ``_unmark_segment`` select the per-cell walk with
    ``self._backend is not np``; this keeps every other backend call working
    while forcing that branch.
    """

    def __getattr__(self, name: str) -> object:
        return getattr(np, name)


def _rules() -> DesignRules:
    return DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        via_diameter=0.6,
        via_drill=0.3,
        via_clearance=0.2,
        min_hole_to_hole=0.5,
        grid_resolution=0.127,
    )


def _build_grid(scalar: bool) -> RoutingGrid:
    grid = RoutingGrid(
        width=12, height=12, rules=_rules(), layer_stack=LayerStack.four_layer_all_signal()
    )
    if scalar:
        grid._backend = _ProxyBackend()  # type: ignore[assignment]
    return grid


def _add_pads(grid: RoutingGrid, rng: random.Random) -> None:
    """Seed pads so ``pad_blocked`` / ``original_net`` / static halos exist."""
    for net in (1, 2, 3):
        for _ in range(3):
            x = rng.uniform(1.0, 11.0)
            y = rng.uniform(1.0, 11.0)
            grid.add_pad(
                Pad(
                    x=x,
                    y=y,
                    width=0.9,
                    height=0.9,
                    layer=SIGNAL_LAYERS[net % len(SIGNAL_LAYERS)],
                    net=net,
                    net_name=f"N{net}",
                )
            )


def _routes(rng: random.Random) -> list[Route]:
    """Deliberately overlapping routes so envelopes collide and stack."""
    routes: list[Route] = []
    for index in range(6):
        net = 1 + index % 3
        route = Route(net=net, net_name=f"N{net}")
        x1, y1 = rng.uniform(1.0, 11.0), rng.uniform(1.0, 11.0)
        for _ in range(3):
            x2 = min(11.5, max(0.5, x1 + rng.uniform(-2.5, 2.5)))
            y2 = min(11.5, max(0.5, y1 + rng.uniform(-2.5, 2.5)))
            route.segments.append(
                Segment(
                    x1,
                    y1,
                    x2,
                    y2,
                    0.2 + 0.1 * (index % 3),
                    SIGNAL_LAYERS[index % len(SIGNAL_LAYERS)],
                    net,
                    f"N{net}",
                )
            )
            x1, y1 = x2, y2
        route.vias.append(Via(x1, y1, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net, f"N{net}"))
        routes.append(route)
    return routes


def _state(grid: RoutingGrid) -> dict[str, object]:
    counted = grid._congestion_counted
    return {
        "blocked": np.array(grid._blocked, copy=True),
        "net": np.array(grid._net, copy=True),
        "pad_blocked": np.array(grid._pad_blocked, copy=True),
        "congestion": np.array(grid._congestion, copy=True),
        "counted": None if counted is None else np.array(counted, copy=True),
        "routes": len(grid.routes),
    }


def _assert_same(scalar: dict[str, object], window: dict[str, object]) -> None:
    assert scalar["routes"] == window["routes"]
    for key in ("blocked", "net", "pad_blocked", "congestion"):
        np.testing.assert_array_equal(
            window[key], scalar[key], err_msg=f"{key} diverged from the per-cell walk"
        )
    if scalar["counted"] is None or window["counted"] is None:
        assert scalar["counted"] is None and window["counted"] is None
    else:
        np.testing.assert_array_equal(window["counted"], scalar["counted"])


def _drive(grid: RoutingGrid, routes: list[Route]) -> None:
    """Mark everything, rip up half of it, then re-mark one route."""
    for route in routes:
        grid.mark_route(route)
    for route in routes[::2]:
        grid.unmark_route(route)
    grid.mark_route(routes[0])


@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_window_walk_matches_cell_walk(seed: int) -> None:
    scalar_grid = _build_grid(scalar=True)
    window_grid = _build_grid(scalar=False)
    assert window_grid._backend is np, "window path requires the NumPy backend"

    for grid in (scalar_grid, window_grid):
        _add_pads(grid, random.Random(seed))
        _drive(grid, _routes(random.Random(seed + 1000)))

    # Guard against a vacuous comparison: the board must actually carry copper
    # and have debited congestion during the rip-up half of ``_drive``.
    assert bool(window_grid._blocked.any())
    assert window_grid._congestion_counted is not None

    _assert_same(_state(scalar_grid), _state(window_grid))


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_static_blockage_survives_ripup_on_both_walks(seed: int) -> None:
    """Issue #3545: a pad halo overlapped by a route's own clearance."""
    grids = {}
    for name, scalar in (("scalar", True), ("window", False)):
        grid = _build_grid(scalar=scalar)
        _add_pads(grid, random.Random(seed))
        routes = _routes(random.Random(seed))
        for route in routes:
            grid.mark_route(route)
        for route in routes:
            grid.unmark_route(route)
        grids[name] = grid

    static = grids["window"]._static_blocked
    assert static is not None and bool(static.any())
    # Every statically blocked cell must still be blocked after a full rip-up
    # on BOTH walks (the regression Issue #3545 fixed), and both walks must
    # agree cell for cell.
    for grid in grids.values():
        assert bool(np.all(grid._blocked[static]))
    _assert_same(_state(grids["scalar"]), _state(grids["window"]))


@pytest.mark.parametrize("seed", [21, 22])
def test_preexisting_congestion_credit_is_debited_identically(seed: int) -> None:
    """Pad / static cells that WERE counted must be debited on both walks.

    ``_mark_segment`` only credits congestion for cells it newly blocks, so
    in an ordinary run a pad cell is never counted and the pad branch's
    ``delta=-1`` is a no-op.  ``_update_congestion``'s contract is broader
    than that (``mark_route_usage`` and hand-supplied raster cells can credit
    any cell), so this seeds credit on pad cells directly and checks the two
    walks still agree -- without it, narrowing the released mask to only the
    freed cells would go unnoticed.
    """
    states = []
    for scalar in (True, False):
        grid = _build_grid(scalar=scalar)
        _add_pads(grid, random.Random(seed))
        layers, ys, xs = np.nonzero(grid._pad_blocked)
        assert len(xs) > 0
        for layer, gy, gx in zip(layers.tolist(), ys.tolist(), xs.tolist(), strict=True):
            grid._update_congestion(gx, gy, layer)
        _drive(grid, _routes(random.Random(seed)))
        states.append(_state(grid))
    _assert_same(states[0], states[1])


def test_occupancy_generation_advances_identically() -> None:
    """Issue #4794: the window walk mirrors the per-cell bump count."""
    deltas = []
    for scalar in (True, False):
        grid = _build_grid(scalar=scalar)
        _add_pads(grid, random.Random(7))
        routes = _routes(random.Random(7))
        before = grid.occupancy_generation
        _drive(grid, routes)
        deltas.append(grid.occupancy_generation - before)
    assert deltas[0] > 0
    assert deltas[0] == deltas[1]
