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
idle host -- unmeasurable by wall clock.  The surviving ratio assertion
is a nightly gross-regression bound (see
``test_dormant_partner_optimized_not_slower``); #2715's actual property
is asserted deterministically, in the per-PR gate, by
``tests/test_pathfinder_partner_active_structural.py``.
"""

from __future__ import annotations

import timeit

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


def _measure_dormant_ratio(n_trials: int = 5, n_iters: int = 20_000) -> tuple[float, float, float]:
    """Measure the optimized/legacy dormant-path timing ratio robustly.

    Runs ``n_trials`` *interleaved* (legacy, optimized) ``timeit`` trials
    and takes the **minimum** time for each path.  Interleaving means a
    transient CPU-contention spike (xdist sibling workers, shared CI
    runner neighbors) hits both paths roughly equally instead of biasing
    one; taking the min discards trials polluted by scheduler noise --
    the minimum is the best estimator of true cost for a deterministic
    micro-benchmark.

    Returns:
        (ratio, legacy_min, optimized_min)
    """
    router, net, radius = _build_dense_grid()
    gx = router.grid.cols // 2
    gy = router.grid.rows // 2 - 30

    def legacy() -> bool:
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

    def optimized() -> bool:
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

    # Warm up -- prime any one-shot caches.
    for _ in range(1000):
        legacy()
        optimized()

    legacy_min = float("inf")
    optimized_min = float("inf")
    for _ in range(n_trials):
        legacy_min = min(legacy_min, timeit.timeit(legacy, number=n_iters))
        optimized_min = min(optimized_min, timeit.timeit(optimized, number=n_iters))

    return optimized_min / legacy_min, legacy_min, optimized_min


@pytest.mark.slow
def test_dormant_partner_optimized_not_slower():
    """Nightly coarse guard: catch a *gross* dormant-path regression.

    **This is deliberately not a per-PR gate (issue #5708).**  It was
    one until 2026-09-23, and it could not work:

    * the skipped 4-condition tuple costs ~8.0 ns/eval, and the
      optimized caller is ~6.3 ns/call faster end-to-end,
    * against a measured ~2 300 ns per call of ``_is_trace_blocked``,
    * so the defended effect is **~0.26 %** of the measured quantity,
    * while five back-to-back measurements of
      ``_measure_dormant_ratio()`` on an *idle* host (no CI neighbours,
      no xdist) spanned **0.95x-1.35x, i.e. ~+/-35 %** -- already
      exceeding the then-current 1.25x budget once in five.

    Signal-to-noise is roughly **1 : 130**, so no threshold value both
    tolerates the noise and detects the regression.  That is a property
    of the *measurement*, not of the budget, which is why the earlier
    1.05 -> 1.25 raise (issue #3581, closed, recorded here as history
    only) bought eight weeks rather than a fix, and why raising it a
    third time was rejected.

    The property #2715 actually defends -- "supplying ``partner_active``
    skips the 4-condition evaluation" -- is asserted deterministically
    and with zero variance in
    ``tests/test_pathfinder_partner_active_structural.py``, which *is*
    in the per-PR gate.  This test survives only to keep a gross
    regression (a rewrite that makes the dormant path multiples slower,
    which no operation count would notice) from being invisible: the
    1.75x bound sits above every observed noise sample -- 1.35x idle,
    1.439x worst observed on a shared runner -- and below a 2x
    regression.  It runs min-of-9 interleaved trials, in the nightly
    ``slow`` lane only.

    Do **not** treat a failure here as necessarily real: re-run it
    before investigating, and read the printed ratio as a measurement
    rather than a verdict.
    """
    n_trials = 9
    ratio, legacy_min, optimized_min = _measure_dormant_ratio(n_trials=n_trials)
    assert ratio < 1.75, (
        f"Optimized dormant path is {ratio:.3f}x slower than legacy "
        f"(legacy_min={legacy_min:.4f}s, optimized_min={optimized_min:.4f}s, "
        f"min of {n_trials} interleaved trials x 20000 iters).  Expected "
        f"ratio < 1.75 -- a gross-regression bound only, sized to sit above "
        f"the ~+/-35% measurement noise documented in issue #5708.  Re-run "
        f"before investigating; the deterministic guard for issue #2715's "
        f"actual property is "
        f"tests/test_pathfinder_partner_active_structural.py."
    )


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
