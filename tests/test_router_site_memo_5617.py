"""Equivalence pins for the per-cell via-site and disc-kernel memos (Issue #5617).

Background
----------
A py-spy profile of the Diff-Pair regression job's re-route step
(``boards/06-diffpair-test``, ``--seed 42``, retained in
``docs/diagnostics/issue-5617/phase4-native-split.md``) attributes phase 4 --
524.2 s, the dominant phase of the dominant step -- as ~67.6 % native C++ A*
and ~20.1 % pure-Python A* fallback.  Inside that fallback three predicates
were re-evaluating work that does not vary with what they were re-evaluated
for:

* ``ComponentHoleIndex.clear`` -- a *physical drill* floor, so a predicate of
  the candidate CELL alone, yet run once in ``_check_via_placement_cached``
  and again per non-plane layer inside ``_is_via_blocked`` (2.4 s);
* the #5240 non-through-hole pad-drill sweep -- likewise cell-only, but run
  on every ``_check_via_placement_cached`` call because ``_via_cache`` is
  bypassed whenever ``allow_sharing`` is set, i.e. in exactly the negotiated
  mode the fallback runs in (7.3 s across its six NumPy lines);
* ``_is_trace_blocked``'s #3229 Euclidean-disc kernel -- a pure function of
  the radius and the region offsets *relative to the centre*, rebuilt from
  ``np.arange`` on every A* neighbour expansion.

And one hot leaf, ``RouteHaloGeometry.cell_known`` (15.9 s, 3.0 % of phase 4),
reached the four occupancy planes through a freshly allocated ``_CellView``
and four bound-property calls.

This module pins all four changes as **verdict-preserving**: each new form is
compared against the exact expression it replaced.

Sibling of ``test_router_via_halo_memo_5617.py`` (the route-halo via probe,
same issue) and ``test_route_halo_bounds_pruning.py`` (#5240's prune).
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Layer, Pad, Route, Via
from kicad_tools.router.rules import DesignRules


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


def _context() -> tuple[RoutingGrid, Router]:
    """Four-layer board with a through-hole pad, an SMD pad and a stored via.

    The through-hole pad seeds ``grid._component_hole_index`` (so the drill
    floor has something to reject against) and the SMD pad seeds the
    non-through-hole geometry arrays (so the pad-drill sweep is non-trivial).
    """
    rules = _rules()
    grid = RoutingGrid(
        width=20, height=20, rules=rules, layer_stack=LayerStack.four_layer_all_signal()
    )
    tx, ty = grid.grid_to_world(40, 40)
    grid.add_pad(
        Pad(
            x=tx,
            y=ty,
            width=1.2,
            height=1.2,
            layer=Layer.F_CU,
            net=1,
            net_name="N1",
            through_hole=True,
            drill=0.6,
            ref="J1",
            pin="1",
        )
    )
    sx, sy = grid.grid_to_world(90, 40)
    grid.add_pad(
        Pad(
            x=sx,
            y=sy,
            width=0.9,
            height=0.5,
            layer=Layer.F_CU,
            net=2,
            net_name="N2",
            rotation=30.0,
            ref="U1",
            pin="1",
        )
    )
    vx, vy = grid.grid_to_world(60, 60)
    route = Route(net=2, net_name="N2")
    route.vias.append(Via(vx, vy, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), 2, "N2"))
    grid.mark_route(route)

    router = Router(grid, rules)
    router.set_net_name_to_id({"N1": 1, "N2": 2})
    return grid, router


# ---------------------------------------------------------------------------
# 1. ComponentHoleIndex.clear memo
# ---------------------------------------------------------------------------

_HOLE_SWEEP = [(gx, gy) for gx in range(32, 50) for gy in range(32, 50)]


def _hole_clear_uncached(router: Router, gx: int, gy: int) -> bool:
    """The exact pre-memo expression both call sites evaluated."""
    wx, wy = router.grid.grid_to_world(gx, gy)
    return router.grid._component_hole_index.clear(
        wx, wy, router.rules.via_drill, router.rules.min_hole_to_hole
    )


def test_hole_sweep_is_not_degenerate():
    """Guard: the sweep is only meaningful if it sees BOTH verdicts."""
    _, router = _context()
    assert {_hole_clear_uncached(router, gx, gy) for gx, gy in _HOLE_SWEEP} == {True, False}


def test_hole_memo_matches_uncached_verdict_everywhere():
    _, router = _context()
    for gx, gy in _HOLE_SWEEP:
        expected = _hole_clear_uncached(router, gx, gy)
        # Populate, then serve from the memo; both must match.
        assert router._component_hole_clear_cached(gx, gy) is expected
        assert router._component_hole_clear_cached(gx, gy) is expected


def test_hole_memo_collapses_the_per_layer_repeat():
    """One evaluation per CELL, not one per non-plane layer."""
    grid, router = _context()
    calls: list[tuple[float, float]] = []
    index = grid._component_hole_index
    original = index.clear

    def counting(x, y, drill, clearance):
        calls.append((x, y))
        return original(x, y, drill, clearance)

    index.clear = counting  # type: ignore[method-assign]
    for layer in range(grid.num_layers):
        router._is_via_blocked(41, 41, layer, net=2, allow_sharing=True)
    assert len(calls) == 1


def test_hole_memo_drops_when_pad_geometry_is_invalidated():
    """A moved through-hole pad must flip the verdict, not be hidden by the memo."""
    grid, router = _context()
    # A cell well clear of the original drill at (40, 40).
    probe = (70, 70)
    assert router._component_hole_clear_cached(*probe) is True

    pad = next(p for p in grid._pads if p.through_hole)
    pad.x, pad.y = grid.grid_to_world(*probe)
    router.invalidate_pad_geometry_cache()

    assert _hole_clear_uncached(router, *probe) is False
    assert router._component_hole_clear_cached(*probe) is False


# ---------------------------------------------------------------------------
# 2. Non-through-hole pad-drill sweep memo
# ---------------------------------------------------------------------------

_PAD_SWEEP = [(gx, gy) for gx in range(82, 100) for gy in range(32, 50)]


def _pad_drill_clear_uncached(router: Router, gx: int, gy: int) -> bool:
    """The exact pre-memo inline sweep from ``_check_via_placement_cached``."""
    pxs, pys, half_w, half_h, cos_r, sin_r = router._non_th_pad_geometry()
    if not pxs.size:
        return True
    wx, wy = router.grid.grid_to_world(gx, gy)
    drill_radius = router.rules.via_drill / 2.0
    dx = wx - pxs
    dy = wy - pys
    lx = cos_r * dx - sin_r * dy
    ly = sin_r * dx + cos_r * dy
    ex = np.maximum(np.abs(lx) - half_w, 0.0)
    ey = np.maximum(np.abs(ly) - half_h, 0.0)
    return not bool(np.any(np.hypot(ex, ey) < drill_radius))


def test_pad_drill_sweep_is_not_degenerate():
    _, router = _context()
    assert {_pad_drill_clear_uncached(router, gx, gy) for gx, gy in _PAD_SWEEP} == {True, False}


def test_pad_drill_memo_matches_uncached_verdict_everywhere():
    _, router = _context()
    for gx, gy in _PAD_SWEEP:
        expected = _pad_drill_clear_uncached(router, gx, gy)
        assert router._non_th_pad_drill_clear(gx, gy) is expected
        assert router._non_th_pad_drill_clear(gx, gy) is expected


def test_pad_drill_memo_drops_on_in_place_pad_mutation():
    """The #5330 contract: mutating a Pad in place then invalidating must be seen."""
    grid, router = _context()
    probe = (110, 70)
    assert router._non_th_pad_drill_clear(*probe) is True

    pad = next(p for p in grid._pads if not p.through_hole)
    pad.x, pad.y = grid.grid_to_world(*probe)
    router.invalidate_pad_geometry_cache()

    assert _pad_drill_clear_uncached(router, *probe) is False
    assert router._non_th_pad_drill_clear(*probe) is False


def test_pad_drill_memo_survives_clear_via_cache_consistently():
    """``clear_via_cache`` routes through ``invalidate_pad_geometry_cache``."""
    _, router = _context()
    router._non_th_pad_drill_clear(90, 40)
    assert router._pad_drill_site_cache
    router.clear_via_cache()
    assert not router._pad_drill_site_cache
    assert not router._hole_site_cache


# ---------------------------------------------------------------------------
# 3. _is_trace_blocked's Euclidean-disc kernel memo
# ---------------------------------------------------------------------------


def _disc_uncached(radius: int, gx: int, gy: int, x1: int, y1: int, x2: int, y2: int):
    """The exact pre-memo inline construction."""
    ys = np.arange(y1, y2)
    xs = np.arange(x1, x2)
    dy_grid = ys - gy
    dx_grid = xs - gx
    dist_sq = (dy_grid * dy_grid)[:, None] + (dx_grid * dx_grid)[None, :]
    return dist_sq, dist_sq <= radius * radius


@pytest.mark.parametrize("radius", [0, 1, 2, 5])
@pytest.mark.parametrize(
    "gx,gy",
    [(60, 60), (0, 0), (1, 3)],  # interior and two clamped-at-the-edge centres
)
def test_disc_kernel_matches_the_inline_form(radius, gx, gy):
    grid, router = _context()
    x1 = max(0, gx - radius)
    y1 = max(0, gy - radius)
    x2 = min(grid.cols, gx + radius + 1)
    y2 = min(grid.rows, gy + radius + 1)
    expected_dist, expected_disc = _disc_uncached(radius, gx, gy, x1, y1, x2, y2)
    dist_sq, within_disc = router._trace_disc_kernel(radius, y1 - gy, y2 - gy, x1 - gx, x2 - gx)
    assert dist_sq.shape == expected_dist.shape
    assert np.array_equal(dist_sq, expected_dist)
    assert np.array_equal(within_disc, expected_disc)
    assert dist_sq.dtype == expected_dist.dtype


def test_disc_kernel_is_keyed_on_centre_relative_offsets():
    """Two interior centres at the same radius share one entry -- that is the win."""
    _, router = _context()
    router._trace_disc_cache.clear()
    router._trace_disc_kernel(2, -2, 3, -2, 3)
    assert len(router._trace_disc_cache) == 1
    # A different interior centre produces the same offsets -> still one entry.
    router._trace_disc_kernel(2, -2, 3, -2, 3)
    assert len(router._trace_disc_cache) == 1
    # A clamped (edge) region has different offsets -> a distinct entry.
    router._trace_disc_kernel(2, 0, 3, -2, 3)
    assert len(router._trace_disc_cache) == 2


def test_disc_kernel_arrays_are_never_mutated_by_is_trace_blocked():
    """The memo hands out a SHARED array; every use site must be read-only."""
    grid, router = _context()
    router._trace_disc_cache.clear()
    before: dict = {}
    for gx, gy in ((58, 58), (60, 60), (62, 62)):
        for sharing in (False, True):
            router._is_trace_blocked(gx, gy, 0, 2, sharing, radius=2)
            for key, (dist_sq, disc) in router._trace_disc_cache.items():
                if key not in before:
                    before[key] = (dist_sq.copy(), disc.copy())
    assert before, "the sweep never populated the memo"
    for key, (dist_sq, disc) in router._trace_disc_cache.items():
        assert np.array_equal(dist_sq, before[key][0])
        assert np.array_equal(disc, before[key][1])


# ---------------------------------------------------------------------------
# 4. RouteHaloGeometry.cell_known direct-read rewrite
# ---------------------------------------------------------------------------


def _cell_known_via_cellview(halo, x: int, y: int, layer: int) -> bool:
    """The exact pre-#5617 ``_CellView``-based implementation."""
    grid = halo.grid
    if not (0 <= x < grid.cols and 0 <= y < grid.rows and 0 <= layer < grid.num_layers):
        return False
    cell = grid.cell_at(layer, y, x)
    if not cell.blocked or cell.net <= 0 or cell.is_obstacle or cell.pad_blocked:
        return False
    if grid._static_blocked is not None and grid._static_blocked[layer, y, x]:
        return False
    if (layer, y, x) in grid._reserved_for_nets:
        return False
    halo._refresh()
    return bool(halo._cells[layer, y, x] == cell.net)


def test_cell_known_matches_the_cellview_implementation():
    """Every cell in a window that spans pad copper, via copper and free space."""
    grid, router = _context()
    halo = grid._route_halo
    assert halo is not None
    seen = set()
    for layer in range(grid.num_layers):
        for x in range(34, 96, 3):
            for y in range(34, 68, 3):
                expected = _cell_known_via_cellview(halo, x, y, layer)
                assert halo.cell_known(x, y, layer) is expected, (x, y, layer)
                seen.add(expected)
    assert seen == {True, False}, "window never exercised both verdicts"


@pytest.mark.parametrize(
    "x,y,layer",
    [(-1, 10, 0), (10, -1, 0), (10, 10, -1), (10_000, 10, 0), (10, 10_000, 0), (10, 10, 99)],
)
def test_cell_known_rejects_out_of_bounds(x, y, layer):
    grid, _ = _context()
    halo = grid._route_halo
    assert halo.cell_known(x, y, layer) is False
    assert _cell_known_via_cellview(halo, x, y, layer) is False


def test_cell_known_honours_reserved_and_static_blockage():
    """The two post-copper gates keep their original short-circuit order."""
    grid, router = _context()
    halo = grid._route_halo
    known = [
        (x, y, layer)
        for layer in range(grid.num_layers)
        for x in range(54, 68)
        for y in range(54, 68)
        if halo.cell_known(x, y, layer)
    ]
    assert known, "fixture produced no known cells"
    layer, y, x = known[0][2], known[0][1], known[0][0]
    grid._reserved_for_nets[(layer, y, x)] = {999}
    assert halo.cell_known(x, y, layer) is False
    assert _cell_known_via_cellview(halo, x, y, layer) is False


# ---------------------------------------------------------------------------
# 5. End-to-end: the public predicates are unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sharing", [False, True])
def test_via_placement_verdicts_unchanged_by_the_site_memos(sharing):
    """``_check_via_placement_cached`` agrees with a memo-free router cell-for-cell."""
    _, router = _context()
    _, reference = _context()
    # Defeat every memo on the reference router by clearing them before each
    # query -- the same verdicts must come out either way.
    for gx in range(34, 96, 5):
        for gy in range(34, 68, 5):
            reference._hole_site_cache.clear()
            reference._pad_drill_site_cache.clear()
            reference._via_cache.clear()
            expected = reference._check_via_placement_cached(gx, gy, 2, sharing)
            assert router._check_via_placement_cached(gx, gy, 2, sharing) is expected


@pytest.mark.parametrize("sharing", [False, True])
@pytest.mark.parametrize("radius", [1, 2, 4])
def test_trace_blocked_verdicts_unchanged_by_the_disc_memo(sharing, radius):
    _, router = _context()
    _, reference = _context()
    for gx in range(0, 100, 7):
        for gy in range(0, 100, 7):
            reference._trace_disc_cache.clear()
            expected = reference._is_trace_blocked(gx, gy, 0, 2, sharing, radius=radius)
            assert router._is_trace_blocked(gx, gy, 0, 2, sharing, radius=radius) is expected
