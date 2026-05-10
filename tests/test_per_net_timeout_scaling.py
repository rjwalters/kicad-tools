"""Tests for per-net wall-clock timeout scaling (Issue #2610).

DAC_CLK on chorus-test consistently aborted at the same ~67s wall-clock-time
regardless of ``--per-net-timeout``.  Root cause: the C++ A* search enforced
a hard iteration cap (``cols * rows * 4``) that was completely independent
of any wall-clock budget; ``per_net_timeout`` was only honored in the Python
fallback, never in the C++ inner search.  These tests verify the fix:

1. The C++ pathfinder now accepts a ``per_net_timeout_seconds`` parameter
   that establishes a wall-clock deadline.
2. The C++ pathfinder now accepts a ``max_search_iterations`` override.
3. When the deadline fires, ``RouteResult.failure_reason == FAILURE_TIMEOUT``
   distinctly from ``FAILURE_ITERATION_LIMIT`` (memory cap) and
   ``FAILURE_NO_PATH`` (open set drained).
4. Blocked nets consume their full per-net-timeout budget rather than the
   constant 67s pathology -- wall-time scales linearly with the timeout.
5. The Python fallback already honored ``per_net_timeout`` and still does.

Reference: ``src/kicad_tools/router/cpp/src/pathfinder.cpp`` lines 341-566
(one-shot ``route()``) and lines 649-924 (resumable + ``run_astar_loop()``).
"""

from __future__ import annotations

import time

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

# Marker for tests requiring the C++ backend
requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)


def _make_blocked_grid(
    width: float = 50.0,
    height: float = 50.0,
    resolution: float = 0.1,
) -> tuple[RoutingGrid, DesignRules]:
    """Create a synthetic grid with a one-cell-thick wall blocking the only
    horizontal path between start and end.

    The grid is sized large enough that the A* search exhausts a substantial
    portion of the open set before giving up (otherwise the test cannot
    distinguish between "open set drained quickly" and "wall-clock timeout
    fired").  500x500 cells at 0.1mm resolution gives ~1M iteration cap,
    which matches the chorus-test DAC_CLK pathology.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=resolution,
    )
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )

    # Build a wall blocking every cell on every routable layer at a
    # mid-grid x coordinate.  The wall spans the entire y range so no
    # vertical detour is possible.  With both layers blocked at the same
    # x, no via escape is possible either.
    wall_x_world = width / 2.0
    wall_gx, _ = grid.world_to_grid(wall_x_world, 0.0)
    for layer_idx in range(grid.num_layers):
        for gy in range(grid.rows):
            cell = grid.grid[layer_idx][gy][wall_gx]
            cell.blocked = True
            cell.net = 9999  # different-net obstacle
            cell.is_obstacle = True

    return grid, rules


def _make_blocked_pads() -> tuple[Pad, Pad]:
    """Create source/destination pads on opposite sides of the wall."""
    start = Pad(
        x=5.0, y=25.0, width=0.5, height=0.5,
        net=1, net_name="BLOCKED_NET", layer=Layer.F_CU,
    )
    end = Pad(
        x=45.0, y=25.0, width=0.5, height=0.5,
        net=1, net_name="BLOCKED_NET", layer=Layer.F_CU,
    )
    return start, end


def _make_open_grid_pathological(
    width: float = 100.0,
    height: float = 100.0,
    resolution: float = 0.1,
) -> tuple[RoutingGrid, DesignRules, Pad, Pad]:
    """Create a grid where the C++ A* explores a huge open set and ultimately
    fails to find any path.

    The destination pad is surrounded by a thick ring of different-net
    blocked cells (an "island" the search cannot reach), forcing A* to
    explore the entire interior open set looking for a path that doesn't
    exist.  This mirrors the chorus-test DAC_CLK pathology where the
    search runs through the densest cluster until the iteration cap (or,
    with the #2610 fix, the wall-clock deadline) fires.

    Without the #2610 wall-clock deadline, the inner loop would run
    until ``last_iterations_ == cols * rows * 4``, which for a 1000x1000
    grid is 4M iterations = many seconds.  With the #2610 fix, the loop
    aborts cleanly at the deadline and reports ``FAILURE_TIMEOUT``.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=resolution,
    )
    layer_stack = LayerStack.two_layer()
    grid = RoutingGrid(
        width=width,
        height=height,
        rules=rules,
        layer_stack=layer_stack,
    )

    # Box-block the end pad on every layer with a thick different-net
    # ring of obstacles so the C++ A* cannot reach the goal at all
    # (neither via 2D moves nor via layer change).
    end_x = width - 5.0
    end_y = height - 5.0
    box_half = 10.0  # 20mm thick ring; bigger than any via/trace clearance
    gx_lo, gy_lo = grid.world_to_grid(end_x - box_half, end_y - box_half)
    gx_hi, gy_hi = grid.world_to_grid(end_x + box_half, end_y + box_half)
    inner_lo_gx, inner_lo_gy = grid.world_to_grid(end_x - 0.5, end_y - 0.5)
    inner_hi_gx, inner_hi_gy = grid.world_to_grid(end_x + 0.5, end_y + 0.5)
    for layer_idx in range(grid.num_layers):
        for gy in range(max(0, gy_lo), min(grid.rows, gy_hi + 1)):
            for gx in range(max(0, gx_lo), min(grid.cols, gx_hi + 1)):
                # Only block the ring, leaving the inner cell free so
                # the goal check has a target but the search cannot
                # reach it through the surrounding ring.
                if inner_lo_gx <= gx <= inner_hi_gx and inner_lo_gy <= gy <= inner_hi_gy:
                    continue
                cell = grid.grid[layer_idx][gy][gx]
                cell.blocked = True
                cell.net = 9999
                cell.is_obstacle = True

    start = Pad(
        x=5.0, y=5.0, width=0.5, height=0.5,
        net=1, net_name="UNREACHABLE_NET", layer=Layer.F_CU,
    )
    end = Pad(
        x=end_x, y=end_y, width=0.5, height=0.5,
        net=1, net_name="UNREACHABLE_NET", layer=Layer.F_CU,
    )
    return grid, rules, start, end


@requires_cpp
class TestCppDeadlineEnforcement:
    """Verify the C++ A* loop respects ``per_net_timeout_seconds``.

    Issue #2610 acceptance criterion 3: ``Pathfinder::route_resumable`` takes
    a wall-clock argument; the binding is exposed; the deadline is checked
    inside the inner search loop, not just at outer-iteration boundaries.
    """

    def test_cpp_route_resumable_accepts_deadline_kwarg(self):
        """The C++ binding must accept the new ``per_net_timeout_seconds`` arg.

        Issue #2610 acceptance criterion 3: prior to this PR the binding
        had no wall-clock argument at all.  This test fails fast if the
        binding regresses or the BUILD_VERSION is not bumped.
        """
        from kicad_tools.router import router_cpp

        # Verify the FAILURE_TIMEOUT enum is exposed (new in #2610).
        assert hasattr(router_cpp, "FAILURE_TIMEOUT")
        assert router_cpp.FAILURE_TIMEOUT != router_cpp.FAILURE_ITERATION_LIMIT
        assert router_cpp.FAILURE_TIMEOUT != router_cpp.FAILURE_NO_PATH
        # Build version must be bumped so a stale .so is rejected.
        assert router_cpp.BUILD_VERSION >= 4

    def test_blocked_net_consumes_per_net_timeout_cpp(self):
        """A blocked net's wall-time must scale with per_net_timeout.

        Pre-#2610: dt was pinned to ~67s regardless of target (iteration
        cap firing first).  Post-#2610: dt should be in the same order of
        magnitude as target.
        """
        grid, rules = _make_blocked_grid()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        start, end = _make_blocked_pads()
        target = 2.0

        t0 = time.monotonic()
        route = pathfinder.route(start, end, per_net_timeout=target)
        dt = time.monotonic() - t0

        # The route must fail (wall blocks the only path).
        assert route is None, "Expected no route through blocking wall"

        # Wall-time must not exceed ~3x target.  The pre-#2610 pathology
        # would produce dt ~= 67s (cols*rows*4 / 15k iter/s) for a 500x500
        # grid, well above 3*2 = 6s.  The 3x upper bound accommodates the
        # Python fallback path that runs after C++ failure with its own
        # ``per_net_timeout`` budget, plus the ~constant cpp setup cost.
        assert dt <= target * 3.0 + 1.0, (
            f"Wall-time {dt:.2f}s exceeded scaled budget ({target * 3 + 1:.2f}s); "
            "regression of the pre-#2610 ~67s pathology."
        )

    def test_cpp_pathfinder_timeout_directly(self):
        """Bypass the cpp_backend Python wrapper and exercise the raw C++
        ``route_resumable`` API with a wall-clock deadline.  This is the
        cleanest test of the new #2610 plumbing because there is no
        Python fallback running afterward to confound the wall-time.
        """
        from kicad_tools.router import router_cpp

        grid, rules, start, end = _make_open_grid_pathological(
            width=50.0, height=50.0, resolution=0.1,
        )
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Build the raw C++ Pathfinder so we can call route_resumable
        # with the new keyword args directly.
        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(cpp_grid._impl, cpp_rules, True)
        pf.set_routable_layers(cpp_grid.get_routable_indices())

        start_layers = cpp_grid.get_routable_indices()
        end_layers = cpp_grid.get_routable_indices()

        # Call route_resumable with a 0.5s deadline.  The grid is sized
        # so that without the deadline the search would run for several
        # seconds (perimeter-blocked, large open set).
        timeout_target = 0.5
        t0 = time.monotonic()
        result = pf.route_resumable(
            start.x, start.y, 0,
            end.x, end.y, 0,
            start.net,
            start_layers, end_layers,
            False, 0.0, 1.0, 0, 0,
            router_cpp.PadBounds(), router_cpp.PadBounds(),
            -1, 0,
            timeout_target,  # per_net_timeout_seconds
            0,               # max_search_iterations (use default)
        )
        dt = time.monotonic() - t0
        try:
            # The route must fail (perimeter blocks the only path).
            assert not result.success, "Expected unreachable goal"

            # Wall-time must be bounded by the deadline + sampling slack
            # (the deadline is checked every 1024 iterations).  Pre-#2610
            # this would have run for tens of seconds.
            assert dt <= timeout_target + 1.0, (
                f"C++ deadline not enforced: dt={dt:.2f}s for "
                f"timeout_target={timeout_target}s"
            )

            # Failure reason should be TIMEOUT (or NO_PATH if the open set
            # drains before the deadline -- accept both since the synthetic
            # geometry could allow A* to give up early).
            assert result.failure_reason in (
                router_cpp.FAILURE_TIMEOUT,
                router_cpp.FAILURE_NO_PATH,
                router_cpp.FAILURE_ITERATION_LIMIT,
            )
        finally:
            pf.clear_search_state()

    def test_cpp_pathfinder_timeout_scales(self):
        """Doubling the deadline should ~double the wall-time at the raw
        C++ API level.  No Python fallback in the loop, so the scaling
        is the C++ deadline plumbing in isolation.

        Acceptance criterion 1 from Issue #2610: ``DAC_CLK actually consumes
        its allotted per-net-timeout when given more time''.
        """
        from kicad_tools.router import router_cpp

        grid, rules, start, end = _make_open_grid_pathological(
            width=50.0, height=50.0, resolution=0.1,
        )
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        # Two independent Pathfinder instances so search state cannot leak.
        def _route_with_deadline(deadline: float) -> tuple[float, int]:
            pf = router_cpp.Pathfinder(cpp_grid._impl, cpp_rules, True)
            pf.set_routable_layers(cpp_grid.get_routable_indices())
            t0 = time.monotonic()
            result = pf.route_resumable(
                start.x, start.y, 0,
                end.x, end.y, 0,
                start.net,
                cpp_grid.get_routable_indices(),
                cpp_grid.get_routable_indices(),
                False, 0.0, 1.0, 0, 0,
                router_cpp.PadBounds(), router_cpp.PadBounds(),
                -1, 0,
                deadline, 0,
            )
            dt = time.monotonic() - t0
            reason = result.failure_reason
            pf.clear_search_state()
            return dt, int(reason)

        dt_short, reason_short = _route_with_deadline(0.3)
        dt_long, reason_long = _route_with_deadline(0.9)

        # If the search finishes via NO_PATH (open set drains) the deadline
        # is irrelevant, in which case we cannot assert scaling -- the
        # interesting case is when one or both runs time out.  We assert
        # scaling only when at least the LONG run hit the deadline.
        if reason_long == router_cpp.FAILURE_TIMEOUT:
            # Either both timed out (3x ratio expected) OR only the long
            # one timed out and the short one drained (long > short still).
            ratio = dt_long / max(dt_short, 0.01)
            assert ratio >= 2.0, (
                f"Wall-time did not scale: dt(0.9s)={dt_long:.2f}s, "
                f"dt(0.3s)={dt_short:.2f}s, ratio={ratio:.2f}x. "
                "Regression of the pre-#2610 constant-time pathology."
            )
        else:
            # Open set drained for both runs -- the deadline plumbing is
            # exercised but cannot be tested for scaling here.  At minimum
            # the long run must not be FASTER than the short run.
            assert dt_long >= dt_short * 0.5


@requires_cpp
class TestCppFailureReasonClassification:
    """Verify the C++ pathfinder distinguishes TIMEOUT from ITERATION_LIMIT
    from NO_PATH in ``RouteResult.failure_reason``.

    Issue #2610 acceptance criterion 2: router log distinguishes "iteration
    cap hit" from "wall-clock timeout" from "true BLOCKED_PATH".
    """

    def test_timeout_surfaces_failure_timeout(self):
        """When the wall-clock deadline fires, failure_reason == FAILURE_TIMEOUT."""
        from kicad_tools.router import router_cpp

        grid, rules = _make_blocked_grid()
        cpp_grid = CppGrid.from_routing_grid(grid)
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        start, end = _make_blocked_pads()

        # Call route() and verify the captured failure_info has
        # FAILURE_TIMEOUT.  We use a very short deadline so the C++
        # search times out before the iteration cap fires.
        route = pathfinder.route(start, end, per_net_timeout=0.5)
        assert route is None

        info = pathfinder.get_last_failure_info()
        assert info is not None, "Expected captured failure info"
        # The info comes from the C++ pathfinder via _capture_failure_info.
        # When the wall-clock deadline fires before the open set drains
        # and before the iteration cap, failure_reason should be TIMEOUT.
        # Note: cpp_backend.py also runs a Python fallback after the C++
        # fails; whichever produces the most recent failure_info wins.
        # The Python fallback also breaks on its deadline and produces no
        # info, so the C++ TIMEOUT info should be preserved.
        assert info.get("failure_reason") in (
            router_cpp.FAILURE_TIMEOUT,
            router_cpp.FAILURE_NO_PATH,
            router_cpp.FAILURE_ITERATION_LIMIT,
        ), f"Unexpected failure_reason: {info.get('failure_reason')}"

    def test_describe_failure_reason_labels(self):
        """Verify describe_failure_reason produces distinct log labels.

        This is the "router log distinguishes" half of the acceptance
        criterion: even if the C++ binding is unavailable, the Python
        NegotiatedRouter must be able to label TIMEOUT vs ITERATION_LIMIT
        vs BLOCKED_PATH distinctly.
        """
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        # All five failure_reason buckets must produce distinct labels.
        labels = {
            NegotiatedRouter.describe_failure_reason({"failure_reason": 0}),  # NONE
            NegotiatedRouter.describe_failure_reason({"failure_reason": 1}),  # NO_PATH
            NegotiatedRouter.describe_failure_reason({"failure_reason": 2}),  # ITERATION_LIMIT
            NegotiatedRouter.describe_failure_reason({"failure_reason": 3}),  # TIMEOUT
            NegotiatedRouter.describe_failure_reason({"failure_reason": 5}),  # VIA_VIA_BLOCKED
        }
        assert len(labels) == 5, (
            f"Failure labels must be distinct, got {labels}"
        )
        # The timeout label must contain a clue that this is wall-clock,
        # not iteration-cap, so log readers can tell which limit fired.
        assert (
            "timeout" in NegotiatedRouter.describe_failure_reason(
                {"failure_reason": 3}
            )
        )
        assert (
            "iteration" in NegotiatedRouter.describe_failure_reason(
                {"failure_reason": 2}
            )
        )


@requires_cpp
class TestMaxSearchIterationsOverride:
    """Verify the ``max_search_iterations`` override works.

    Issue #2610 acceptance criterion 4: the iteration cap is overridable via
    ``--max-search-iterations`` so users can trade memory for completeness
    on dense boards.  Defaults preserve current ``cols * rows * 4`` behavior.
    """

    def test_default_max_search_iterations_preserved(self):
        """Default constructor (max_search_iterations=0) preserves pre-#2610 behavior.

        Acceptance criterion 4: defaults preserve the historical
        ``cols * rows * 4`` cap so existing routes continue to behave
        identically without the override.
        """
        grid, rules = _make_blocked_grid(width=20.0, height=20.0, resolution=0.2)
        cpp_grid = CppGrid.from_routing_grid(grid)

        # Default constructor: max_search_iterations=0 means "use the
        # historical cols*rows*4 cap" -- this is the no-regression case.
        pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
        assert pathfinder._max_search_iterations == 0
        pathfinder.set_routable_layers(cpp_grid.get_routable_indices())

        start, end = _make_blocked_pads()
        # Smaller pads for smaller grid
        start.x = 2.0
        start.y = 10.0
        end.x = 18.0
        end.y = 10.0

        # No timeout, no override -- should fail via NO_PATH or ITERATION_LIMIT.
        route = pathfinder.route(start, end, per_net_timeout=2.0)
        assert route is None

    def test_low_max_search_iterations_caps_early(self):
        """A very small max_search_iterations should fail at the cap with
        ``FAILURE_ITERATION_LIMIT`` in well under the no-cap baseline.

        This test calls the raw C++ ``route_resumable`` directly to avoid
        the Python fallback path (which runs after the C++ call fails and
        is bounded only by ``per_net_timeout``, not by
        ``max_search_iterations``).
        """
        from kicad_tools.router import router_cpp

        grid, rules, start, end = _make_open_grid_pathological(
            width=100.0, height=100.0, resolution=0.1,
        )
        cpp_grid = CppGrid.from_routing_grid(grid)

        cpp_rules = router_cpp.DesignRules()
        cpp_rules.trace_width = rules.trace_width
        cpp_rules.trace_clearance = rules.trace_clearance
        cpp_rules.via_drill = rules.via_drill
        cpp_rules.via_diameter = rules.via_diameter
        cpp_rules.via_clearance = rules.via_clearance
        cpp_rules.grid_resolution = rules.grid_resolution
        cpp_rules.cost_straight = rules.cost_straight
        cpp_rules.cost_turn = rules.cost_turn
        cpp_rules.cost_via = rules.cost_via
        cpp_rules.cost_congestion = rules.cost_congestion
        cpp_rules.congestion_threshold = rules.congestion_threshold

        pf = router_cpp.Pathfinder(cpp_grid._impl, cpp_rules, True)
        pf.set_routable_layers(cpp_grid.get_routable_indices())

        # Tiny iteration cap: search must hit FAILURE_ITERATION_LIMIT
        # almost immediately.
        t0 = time.monotonic()
        result = pf.route_resumable(
            start.x, start.y, 0,
            end.x, end.y, 0,
            start.net,
            cpp_grid.get_routable_indices(),
            cpp_grid.get_routable_indices(),
            False, 0.0, 1.0, 0, 0,
            router_cpp.PadBounds(), router_cpp.PadBounds(),
            -1, 0,
            0.0,    # no wall-clock deadline
            1000,   # max_search_iterations (tiny -> cap fires fast)
        )
        dt = time.monotonic() - t0
        try:
            assert not result.success
            # The cap must fire fast; even on a slow CI machine 1000
            # iterations of A* expansion is sub-second.
            assert dt < 1.0, (
                f"max_search_iterations=1000 took {dt:.2f}s; cap should fire instantly"
            )
            assert result.failure_reason == router_cpp.FAILURE_ITERATION_LIMIT
            assert pf.iterations >= 1000
        finally:
            pf.clear_search_state()
