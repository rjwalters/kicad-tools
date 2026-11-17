"""Coarse routing density represents current cells, not rip-up history."""

import numpy as np
import pytest

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules


def grid():
    return RoutingGrid(12, 12, DesignRules(grid_resolution=0.25, congestion_grid_size=10))


def route(net=1, y=5, via=False):
    return Route(
        net,
        f"N{net}",
        [Segment(2, y, 10, y, 0.2, Layer.F_CU, net)],
        [Via(6, y, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net)] if via else [],
    )


def assert_matches_current_cells(g):
    occupied = g._blocked.copy()
    if g._static_blocked is not None:
        occupied &= ~g._static_blocked
    expected = np.zeros_like(g.congestion)
    for layer, y, x in np.argwhere(occupied):
        cy = min(y // g.congestion_size, g.congestion_rows - 1)
        cx = min(x // g.congestion_size, g.congestion_cols - 1)
        expected[layer, cy, cx] += 1
    np.testing.assert_array_equal(g.congestion, expected)


@pytest.mark.parametrize("with_via", [False, True])
def test_mark_unmark_and_remark_match_fresh_density(with_via):
    g = grid()
    r = route(via=with_via)
    g._history_cost.fill(3.5)
    for _ in range(4):
        g.mark_route(r)
        assert_matches_current_cells(g)
        original = g.congestion.copy()
        g.unmark_route(r)
        assert not np.any(g.congestion)
        assert_matches_current_cells(g)
        np.testing.assert_array_equal(g._history_cost, 3.5)
    fresh = grid()
    fresh.mark_route(r)
    np.testing.assert_array_equal(fresh.congestion, original)


def test_ripup_preserves_static_pad_occupancy_and_never_counts_it():
    g = grid()
    g.add_pad(Pad(6, 5, 1.5, 1.5, 1, "N1", ref="U1", pin="1"))
    static = g._blocked.copy()
    assert not np.any(g.congestion)
    r = route(via=True)
    g.mark_route(r)
    assert_matches_current_cells(g)
    g.unmark_route(r)
    np.testing.assert_array_equal(g._blocked, static)
    assert not np.any(g.congestion)


def test_overlapping_marks_follow_actual_owner_and_do_not_double_decrement():
    g = grid()
    first, second = route(1, 5), route(2, 5.5)
    g.mark_route(first)
    first_density = g.congestion.copy()
    g.mark_route(second)
    assert_matches_current_cells(g)
    g.unmark_route(second)
    np.testing.assert_array_equal(g.congestion, first_density)
    g.unmark_route(second)  # already released cells must not subtract twice
    np.testing.assert_array_equal(g.congestion, first_density)
    g.unmark_route(first)
    assert not np.any(g.congestion)


def test_resync_replaces_geometry_without_retaining_old_density():
    g, fresh = grid(), grid()
    old, new = route(1, 4, via=True), route(1, 8, via=True)
    g.mark_route(old)
    for _ in range(3):
        g.resync_route_occupancy([(old, new)])
        assert_matches_current_cells(g)
        g.resync_route_occupancy([(new, old)])
        assert_matches_current_cells(g)
    fresh.mark_route(old)
    np.testing.assert_array_equal(g.congestion, fresh.congestion)


def test_static_reset_discards_route_density_but_keeps_history_and_usage():
    g = grid()
    g.mark_route(route(via=True))
    g._history_cost.fill(4)
    g._usage_count.fill(2)
    assert g.reset_route_occupancy_to_static()
    assert not np.any(g.congestion)
    np.testing.assert_array_equal(g._history_cost, 4)
    np.testing.assert_array_equal(g._usage_count, 2)
    g.mark_route(route(via=True))
    assert_matches_current_cells(g)


def test_temporary_unblock_restores_density_even_after_exception():
    g = grid()
    g.mark_route(route(via=True))
    original = g.congestion.copy()
    with pytest.raises(RuntimeError), g.temporarily_unblock_routed_nets():
        assert not np.any(g.congestion)
        raise RuntimeError("rollback")
    np.testing.assert_array_equal(g.congestion, original)
    assert_matches_current_cells(g)


def test_unmarking_noncounted_manual_cell_cannot_make_density_negative():
    g = grid()
    # Low-level callers can supply a raster independently of route marking.
    cell = g.cell_at(0, 20, 20)
    cell.blocked, cell.net = True, 1
    g._unmark_segment(Segment(5, 5, 5, 5, 0.2, Layer.F_CU, 1), clearance_cells=0)
    assert not np.any(g.congestion)


@pytest.mark.parametrize("pad_metal", [False, True])
def test_route_cell_promoted_to_static_loses_only_its_route_density(pad_metal):
    g = grid()
    r = route()
    g.mark_route(r)
    gx, gy = g.world_to_grid(6, 5)
    # Late static geometry may replace a cell which route marking counted.
    g._static_blocked[0, gy, gx] = True
    g._original_net[0, gy, gx] = 9
    g._pad_blocked[0, gy, gx] = pad_metal
    g.unmark_route(r)
    assert g._blocked[0, gy, gx]
    assert g._net[0, gy, gx] == 9
    assert not np.any(g.congestion)


def test_hard_reservations_keep_skipped_route_cells_out_of_density():
    g = grid()
    gx, gy = g.world_to_grid(6, 5)
    g.reserve_corridor_cells(0, [(gx, gy)], {2})
    r = route(1, via=True)
    g.mark_route(r)
    assert not g._blocked[0, gy, gx]
    assert_matches_current_cells(g)
    g.unmark_route(r)
    assert not np.any(g.congestion)
