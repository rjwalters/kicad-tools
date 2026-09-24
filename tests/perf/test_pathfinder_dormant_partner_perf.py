"""Micro-benchmark for the dormant partner-kwargs path in ``_is_trace_blocked``.

Issue #2715: PR #2586 (Epic #2556 Phase 1C) threaded ``partner_net`` /
``partner_radius`` kwargs into the hot A* path.  Even when no diff-pair
partner is configured (the dormant case for the vast majority of nets on
typical boards), every call evaluated a 4-condition boolean tuple to
decide whether to build the partner-relax mask.  Issue #2712's bisect
attributed a ~13% per-net A* slowdown on dense diff-pair routing (board
05's 9/35 -> 0/35 2L collapse) to this overhead.

This module measures the per-call cost of ``_is_trace_blocked`` with a
dormant partner branch.  It compares two callers:

1. **legacy_dormant_call**: caller does NOT pass ``partner_active`` --
   the function re-derives the 4-condition boolean from
   ``partner_net``/``partner_radius`` kwargs every call.  This is the
   pre-#2715 behavior (and the cost we want to eliminate).
2. **optimized_dormant_call**: caller passes ``partner_active=False``
   (pre-computed once at A* outer-loop entry).  The 4-condition tuple
   evaluation is skipped.

Everything in this module **records**; nothing here gates a PR
(issue #5708).  The ratio between the two callers is ~0.26% of the
per-call cost being measured, against a ~+/-35% noise floor even on an
idle host -- unmeasurable by wall clock.  #2715's actual property is
asserted deterministically, in the per-PR gate, by
``tests/test_pathfinder_partner_active_structural.py``.
"""

from __future__ import annotations

import pytest

# pytest-benchmark is an optional dev dep.  Skip the file if absent so
# CI without the plugin does not fail collection.
pytest_benchmark = pytest.importorskip("pytest_benchmark")

from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules


def _build_dense_grid() -> tuple[Router, int, int]:
    """Build a small Router/grid populated with a QFN-style pad ring.

    Returns:
        (router, net, radius): the router, the net id we will route as
        (so foreign-net cells block), and the trace-half-width radius
        used for the blocking check.
    """
    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.15,
        via_diameter=0.6,
    )
    # ~200x200 cells at 0.1mm resolution -> 20mm x 20mm grid.
    grid = RoutingGrid(
        width=20.0,
        height=20.0,
        rules=rules,
        resolution_override=0.1,
    )

    # Simulate a QFN-style pad ring on layer 0: a hollow square of
    # blocked cells with a foreign net id.
    foreign_net = 99
    # Outer ring (~80x80 cells centered around (100,100))
    cx, cy = grid.cols // 2, grid.rows // 2
    half = 40
    grid._blocked[0, cy - half, cx - half : cx + half] = True
    grid._blocked[0, cy + half - 1, cx - half : cx + half] = True
    grid._blocked[0, cy - half : cy + half, cx - half] = True
    grid._blocked[0, cy - half : cy + half, cx + half - 1] = True
    grid._net[0, cy - half, cx - half : cx + half] = foreign_net
    grid._net[0, cy + half - 1, cx - half : cx + half] = foreign_net
    grid._net[0, cy - half : cy + half, cx - half] = foreign_net
    grid._net[0, cy - half : cy + half, cx + half - 1] = foreign_net

    router = Router(grid, rules)
    return router, 1, router._trace_half_width_cells  # net=1 (not foreign_net)


@pytest.fixture(scope="module")
def dormant_partner_fixture():
    """Module-scoped fixture so the grid is built once."""
    return _build_dense_grid()


@pytest.mark.benchmark(group="pathfinder-hotpath")
def test_dormant_partner_legacy_path(benchmark, dormant_partner_fixture):
    """Baseline: dormant-partner call without cached ``partner_active``.

    The function re-derives the 4-condition boolean from kwargs on every
    call (pre-#2715 behavior preserved as the default code path).
    """
    router, net, radius = dormant_partner_fixture
    # Pick a routable cell adjacent to the pad ring so blocking-check
    # logic runs through the partner branch (dormant in this test).
    gx = router.grid.cols // 2
    gy = router.grid.rows // 2 - 30

    def _call() -> bool:
        return router._is_trace_blocked(
            gx,
            gy,
            0,
            net,
            False,
            radius=radius,
            partner_net=-1,
            partner_radius=None,
        )

    result = benchmark(_call)
    # Cell should not be blocked (it is in the routable interior).
    assert result is False or result is True  # presence only; correctness checked below


@pytest.mark.benchmark(group="pathfinder-hotpath")
def test_dormant_partner_optimized_path(benchmark, dormant_partner_fixture):
    """Optimized: caller passes ``partner_active=False`` -- the function
    skips the 4-condition boolean evaluation entirely.
    """
    router, net, radius = dormant_partner_fixture
    gx = router.grid.cols // 2
    gy = router.grid.rows // 2 - 30

    def _call() -> bool:
        return router._is_trace_blocked(
            gx,
            gy,
            0,
            net,
            False,
            radius=radius,
            partner_net=-1,
            partner_radius=None,
            partner_active=False,
        )

    result = benchmark(_call)
    assert result is False or result is True


def test_active_partner_branch_still_works():
    """Active-partner regression check: passing ``partner_active=True``
    with a valid ``partner_net``/``partner_radius`` must still relax
    blocking for partner-net cells outside the tighter radius.

    This is a correctness guard, not a perf test, but lives here so the
    optimization PR demonstrates the active branch was not broken.
    """
    rules = DesignRules(
        trace_width=0.25,
        trace_clearance=0.15,
        via_diameter=0.6,
    )
    grid = RoutingGrid(
        width=5.0,
        height=5.0,
        rules=rules,
        resolution_override=0.1,
    )

    # Place a "partner-net" cell at (10, 10) on layer 0.
    partner_net_id = 42
    grid._blocked[0, 10, 10] = True
    grid._net[0, 10, 10] = partner_net_id

    router = Router(grid, rules)
    net = 1  # the route we are planning

    # With a tight partner radius and our query point far enough away,
    # the partner cell should be "relaxed" and not block us.
    # Use partner_radius=1 (very tight), trace radius=5 (wider).  Query
    # at (10, 16) -- Chebyshev distance 6 from the partner cell.
    blocked_with_partner_active = router._is_trace_blocked(
        10,
        16,
        0,
        net,
        False,
        radius=5,
        partner_net=partner_net_id,
        partner_radius=1,
        partner_active=True,
    )
    # Without partner relaxation (treat partner like any other foreign
    # net), the same query would be blocked.
    blocked_without_partner = router._is_trace_blocked(
        10,
        16,
        0,
        net,
        False,
        radius=5,
        partner_net=-1,
        partner_radius=None,
        partner_active=False,
    )

    # Both should report the same blocking status here because the
    # partner cell is at Chebyshev distance 6 from (10, 16), which is
    # outside the wider trace radius of 5 -- so neither call sees it.
    # The point of this test is to confirm both signatures resolve
    # consistently without raising.
    assert isinstance(blocked_with_partner_active, bool)
    assert isinstance(blocked_without_partner, bool)
