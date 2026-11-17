"""Regression tests for Issue #5617: wall-clock attribution for the
post-route clearance-validation resume loop and the Python fallback it can
trigger.

Issue #5617 profiled the Diff-Pair regression CI job's dominant re-route
step and found phase 4 ("Routing nets...") is 62-67% of the job.  The
issue's own investigation could only inspect ONE hand-picked net
(``USB3_RX1-``) by diffing Actions log timestamps by hand.  This adds
whole-board, machine-readable timing so future work does not need to repeat
that by-hand inspection:

- ``CppPathfinder.fallback_stats['resume_loop_exhausted_seconds']`` --
  summed wall-clock time spent inside the resume loop (issue #5599) for
  every net that burned its full ``max_resume_attempts`` budget before
  handing off to the Python fallback.  That entire span's search work is
  discarded (the net never gets a route from it), so this number is a
  direct measurement of "genuinely wasted" time in phase 4.
- ``CppPathfinder.fallback_stats['python_fallback_seconds_by_reason']`` --
  summed wall-clock time inside ``_try_python_fallback`` itself, bucketed
  by whether the fallback was triggered by resume-loop exhaustion (the
  ``"resume attempts" in reason`` marker the #3923 short-circuit already
  keys on) or by any other cause.

This is pure additive telemetry -- no search behaviour, threshold, boost
amount, or clearance model is touched.  These tests assert only that the
new fields are populated correctly; they do not (and must not) assert
anything about routing OUTCOMES, which the existing #5599 / #3923 suites
already cover and this file leaves untouched.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    RouteClearanceViolation,
    is_cpp_available,
    router_cpp,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# Mirrors tests/test_cpp_resume_exhaustion_skip_3923.py's reason strings --
# the exact substrings emitted at the two resume-loop dead-end call sites.
CLEARANCE_EXHAUSTED_REASON = "post-route clearance validation failed; exhausted 5 resume attempts"
RESUME_FAILED_REASON = "resume after rejected goal cell failed: no path (C++ A* open set exhausted)"
INITIAL_SEARCH_FAILED_REASON = "no path (C++ A* open set exhausted)"


def _make_pathfinder() -> CppPathfinder:
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )
    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder


def _make_pads(net: int = 1, net_name: str = "NET1") -> tuple[Pad, Pad]:
    start = Pad(x=2.0, y=5.0, width=0.6, height=0.6, net=net, net_name=net_name, layer=Layer.F_CU)
    end = Pad(x=8.0, y=5.0, width=0.6, height=0.6, net=net, net_name=net_name, layer=Layer.F_CU)
    return start, end


class _FakeResult:
    def __init__(self, goal: tuple[int, int, int], net: int = 1) -> None:
        self.success = True
        self.net = net
        seg = router_cpp.Segment()
        seg.x1, seg.y1, seg.x2, seg.y2 = 2.0, 5.0, 8.0, 5.0
        seg.width, seg.layer, seg.net = 0.2, 0, net
        self.segments: list[router_cpp.Segment] = [seg]
        self.vias: list = []
        self.goal_gx, self.goal_gy, self.goal_layer = goal
        self.failure_reason = int(router_cpp.FAILURE_NONE)


class _FakeImpl:
    """Stands in for the C++ Pathfinder, always handing back a fake
    successful search result with a fresh (never-before-seen) goal node."""

    def __init__(self, net: int = 1) -> None:
        self.net = net
        self.iterations = 100_000
        self.route_resumable_calls: list[tuple[float, int]] = []
        self.resume_calls: list[tuple[int, int, int]] = []
        self.strict_pad_kernel_calls: list[tuple[bool, int, int, int]] = []
        self._next_goal = 100

    def _fresh_goal(self) -> tuple[int, int, int]:
        goal = (self._next_goal, 50, 0)
        self._next_goal += 1
        return goal

    def set_search_partner_clearance(self, *args, **kwargs) -> None:
        return None

    def set_search_pair_widths(self, *args, **kwargs) -> None:
        return None

    def set_search_fill_clearances(self, *args, **kwargs) -> None:
        return None

    def set_search_strict_pad_kernel(
        self, enabled: bool, cx: int = -1, cy: int = -1, radius: int = 0
    ) -> None:
        self.strict_pad_kernel_calls.append((bool(enabled), int(cx), int(cy), int(radius)))

    def clear_search_state(self) -> None:
        return None

    def route_resumable(self, *args, **kwargs) -> router_cpp.RouteResult:
        self.route_resumable_calls.append((float(args[18]), int(args[19])))
        return _FakeResult(self._fresh_goal(), self.net)

    def resume(self, gx: int, gy: int, layer: int) -> router_cpp.RouteResult:
        self.resume_calls.append((int(gx), int(gy), int(layer)))
        return _FakeResult(self._fresh_goal(), self.net)


@requires_cpp
class TestFallbackStatsDefaults:
    def test_new_fields_present_and_zero_when_nothing_ran(self) -> None:
        pathfinder = _make_pathfinder()
        stats = pathfinder.fallback_stats
        assert stats["resume_loop_exhausted_seconds"] == 0.0
        assert stats["python_fallback_seconds_by_reason"] == {}


@requires_cpp
class TestResumeLoopExhaustedSeconds:
    """A net whose resume loop burns the full budget must attribute its
    ENTIRE loop span (discarded search work) to the aggregate."""

    def test_exhausted_loop_records_positive_wall_time(self) -> None:
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_5617")
        fake = _FakeImpl(net=start.net)
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder,
                "_validate_route_clearance",
                side_effect=lambda *a, **k: RouteClearanceViolation(5.0, 5.0, "seg-pad", 1, 0.1),
            ),
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None),
        ):
            pathfinder.route(start, end)

        stats = pathfinder.fallback_stats
        assert stats["resume_loop_exhausted_seconds"] >= 0.0
        record = stats["resume_diagnostics"]["NET_5617"]
        assert record["exhausted"] is True
        last_attempt = record["attempts"][-1]
        assert last_attempt["strategy"] == "exhausted"
        assert "resume_loop_seconds" in last_attempt
        assert last_attempt["resume_loop_seconds"] >= 0.0
        # The aggregate is exactly this net's span (only one net routed).
        assert stats["resume_loop_exhausted_seconds"] == last_attempt["resume_loop_seconds"]

    def test_recovered_loop_does_not_add_to_exhausted_aggregate(self) -> None:
        """A loop that recovers (never exhausts) must NOT contribute to
        ``resume_loop_exhausted_seconds`` -- that aggregate is specifically
        the DISCARDED-work case, not every resume attempt."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_RECOVER_5617")
        fake = _FakeImpl(net=start.net)

        verdicts = iter([RouteClearanceViolation(5.0, 5.0, "seg-pad", 1, 0.1)] * 2 + [None])
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder,
                "_validate_route_clearance",
                side_effect=lambda *a, **k: next(verdicts),
            ),
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None) as fb,
        ):
            route = pathfinder.route(start, end)

        assert route is not None
        fb.assert_not_called()
        assert pathfinder.fallback_stats["resume_loop_exhausted_seconds"] == 0.0


@requires_cpp
class TestPythonFallbackSecondsByReason:
    """``_try_python_fallback`` buckets its own wall time by trigger cause,
    using the SAME ``"resume attempts" in reason`` classification the #3923
    short-circuit already applies (so the bucketing cannot drift from that
    guard's own notion of "resume exhaustion")."""

    def test_resume_exhaustion_reason_buckets_under_resume_exhaustion(self) -> None:
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="RESUME_TIMED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason=CLEARANCE_EXHAUSTED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )

        stats = pathfinder.fallback_stats
        by_reason = stats["python_fallback_seconds_by_reason"]
        assert set(by_reason) == {"resume_exhaustion"}
        assert by_reason["resume_exhaustion"] >= 0.0

    def test_other_reason_buckets_under_other(self) -> None:
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="OTHER_TIMED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason=INITIAL_SEARCH_FAILED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )

        stats = pathfinder.fallback_stats
        by_reason = stats["python_fallback_seconds_by_reason"]
        assert set(by_reason) == {"other"}
        assert by_reason["other"] >= 0.0

    def test_case_2_resume_failed_reason_buckets_under_other(self) -> None:
        """Case 2 ("resume after rejected goal cell failed") lacks the
        "resume attempts" substring -- it must NOT be conflated with case 1
        (clearance exhaustion) in the aggregate, matching the #3923 guard's
        own case-1-only scope."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="CASE2_TIMED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason=RESUME_FAILED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
            )

        by_reason = pathfinder.fallback_stats["python_fallback_seconds_by_reason"]
        assert set(by_reason) == {"other"}

    def test_repeat_short_circuit_does_not_add_a_second_sample(self) -> None:
        """The #3923 short-circuit returns None BEFORE the Python fallback
        (and this timing code) ever runs -- the repeat must not inflate the
        aggregate with a second, near-zero sample."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="REPEAT_TIMED")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start,
                end,
                reason=CLEARANCE_EXHAUSTED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )
            first_sample = pathfinder.fallback_stats["python_fallback_seconds_by_reason"][
                "resume_exhaustion"
            ]
            result = pathfinder._try_python_fallback(
                start,
                end,
                reason=CLEARANCE_EXHAUSTED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )

        assert result is None
        second_sample = pathfinder.fallback_stats["python_fallback_seconds_by_reason"][
            "resume_exhaustion"
        ]
        assert second_sample == first_sample

    def test_multiple_fallbacks_accumulate(self) -> None:
        pathfinder = _make_pathfinder()
        start_a, end_a = _make_pads(net_name="ACCUM_A")
        start_b, end_b = _make_pads(net_name="ACCUM_B")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pathfinder._try_python_fallback(
                start_a,
                end_a,
                reason=CLEARANCE_EXHAUSTED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )
            first = pathfinder.fallback_stats["python_fallback_seconds_by_reason"][
                "resume_exhaustion"
            ]
            pathfinder._try_python_fallback(
                start_b,
                end_b,
                reason=CLEARANCE_EXHAUSTED_REASON,
                cpp_failure_reason=int(router_cpp.FAILURE_NONE),
            )
            second = pathfinder.fallback_stats["python_fallback_seconds_by_reason"][
                "resume_exhaustion"
            ]

        # Accumulation, not overwrite: the second sample's total is >= the
        # first (both are non-negative wall-clock durations summed).
        assert second >= first
