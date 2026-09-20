"""Regression tests for Issue #5599: post-route clearance-validation resume
loop diagnostics + materially-different-candidate resume strategy.

Root causes fixed (verified by reproducing board 04 USER_LED/BOOT0 under the
exact production recipe):

1. ``_validate_route_clearance`` returned a bare ``(x, y)`` location -- no
   violation KIND -- so nothing could answer "what specifically keeps failing
   clearance on the resume attempts".  It now returns
   :class:`RouteClearanceViolation` (kind + location + measured gap), and the
   resume loop publishes per-attempt records (kind, world/grid location,
   rejected goal cell, strategy, boost amount) through
   ``fallback_stats['resume_diagnostics']``.

2. The reject-goal-cell resume mechanism (#2447) was a NO-OP after its first
   use: ``reconstruct_path`` always draws a final segment to the END PAD
   CENTER regardless of which goal node A* accepted, so the Python loop
   computed the pad-center cell for every candidate and re-rejected the same
   cell while the search kept accepting neighboring goal nodes.  Binding
   surface v42 adds ``RouteResult.goal_gx/goal_gy/goal_layer`` (the accepted
   goal node) and the loop rejects THAT cell.

3. Five flat-20.0-boosted resumes produce near-clone candidates when the same
   violation site keeps rejecting them (the preserved open set's shared
   corridor prefix is CLOSED -- frozen g-scores are immune to the fresh
   avoidance cost).  The strategy now (a) escalates the avoidance boost
   exponentially while the same violation site repeats and (b) RESTARTS the
   search from scratch once a site has repeated, with the accumulated boosts
   and goal rejections active and the REMAINING iteration budget (deterministic
   -- no new wall-clock branch).

The #3923 repeat-exhaustion short-circuit and the #3881 iteration-cap skip
live in ``_try_python_fallback`` and are untouched here -- pinned by
``tests/test_cpp_resume_exhaustion_skip_3923.py`` and
``tests/test_per_net_iteration_cap_3881.py``.
"""

from __future__ import annotations

from unittest import mock

import pytest

from kicad_tools.router import cpp_backend
from kicad_tools.router.cpp_backend import (
    _REQUIRED_CPP_BUILD_VERSION,
    CppGrid,
    CppPathfinder,
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

MAX_RESUME_ATTEMPTS = 5


def _make_pathfinder(**kwargs) -> CppPathfinder:
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
    pathfinder = CppPathfinder(cpp_grid, rules, diagonal_routing=True, **kwargs)
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder


def _make_pads(net: int = 1, net_name: str = "NET1") -> tuple[Pad, Pad]:
    start = Pad(
        x=2.0,
        y=5.0,
        width=0.6,
        height=0.6,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
    )
    end = Pad(
        x=8.0,
        y=5.0,
        width=0.6,
        height=0.6,
        net=net,
        net_name=net_name,
        layer=Layer.F_CU,
    )
    return start, end


class _FakeResult:
    """A minimal successful search result with an explicit accepted goal node.

    (``RouteResult`` attributes are read-only through the nanobind surface,
    so the fake is a plain Python object carrying the same attribute shape.)
    """

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
    """Stands in for the C++ Pathfinder: hands out fake successful results,
    each accepting a DIFFERENT goal node (as a real search does once the
    previous goal cell is rejected).
    """

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

    # -- search-time state pushes (idempotent no-ops on the fake) ----------
    def set_search_partner_clearance(self, *args, **kwargs) -> None:
        return None

    def set_search_pair_widths(self, *args, **kwargs) -> None:
        return None

    def set_search_fill_clearances(self, *args, **kwargs) -> None:
        return None

    def set_search_strict_pad_kernel(
        self, enabled: bool, cx: int = -1, cy: int = -1, radius: int = 0
    ) -> None:
        self.strict_pad_kernel_calls.append(
            (bool(enabled), int(cx), int(cy), int(radius))
        )

    def clear_search_state(self) -> None:
        return None

    # -- search entry points -----------------------------------------------
    def route_resumable(self, *args, **kwargs) -> router_cpp.RouteResult:
        # args[18] is timeout_seconds, args[19] is max_search_iterations.
        self.route_resumable_calls.append((float(args[18]), int(args[19])))
        return _FakeResult(self._fresh_goal(), self.net)

    def resume(self, gx: int, gy: int, layer: int) -> router_cpp.RouteResult:
        self.resume_calls.append((int(gx), int(gy), int(layer)))
        return _FakeResult(self._fresh_goal(), self.net)


@requires_cpp
class TestBindingSurfaceGoalFields:
    def test_build_version_bumped_both_sides(self) -> None:
        """v42 (goal_gx/goal_gy/goal_layer) must be pinned in BOTH
        ``types.hpp`` (via the compiled .so) and ``cpp_backend.py``."""
        assert int(router_cpp.BUILD_VERSION) == 42
        assert _REQUIRED_CPP_BUILD_VERSION == 42

    def test_route_result_carries_goal_fields_defaulting_to_sentinel(self) -> None:
        result = router_cpp.RouteResult()
        assert result.goal_gx == -1
        assert result.goal_gy == -1
        assert result.goal_layer == -1


@requires_cpp
class TestViolationKindInstrumentation:
    def test_validate_result_kind_mapping_covers_cpp_codes(self) -> None:
        from kicad_tools.router.cpp_backend import _CPP_VIOLATION_KINDS

        assert _CPP_VIOLATION_KINDS[1] == "seg-pad"
        assert _CPP_VIOLATION_KINDS[2] == "seg-seg"
        assert _CPP_VIOLATION_KINDS[5] == "via-via"
        assert _CPP_VIOLATION_KINDS[6] == "drill"
        assert _CPP_VIOLATION_KINDS[8] == "via-pad"

    def test_cpp_validator_violation_type_maps_into_record(self) -> None:
        """Board06-style seg-seg fixture: the C++ ``violation_type`` code (2)
        flows into the #5599 record kind via RouteClearanceViolation."""
        from kicad_tools.router.cpp_backend import RouteClearanceViolation

        grid = router_cpp.Grid3D(600, 600, 2, 0.05, 100.0, 100.0)
        obstacle = (108.7, 117.8), (124.5, 117.8)
        candidate = (109.6, 118.25), (109.55, 118.20)
        grid.add_stored_segment(*obstacle[0], *obstacle[1], 0.225, 0, 23)
        segment = router_cpp.Segment()
        segment.x1, segment.y1 = candidate[0]
        segment.x2, segment.y2 = candidate[1]
        segment.width, segment.layer, segment.net = 0.375, 0, 26
        vresult = grid.validate_route([segment], [], 26, [], 0.15, 0.2, 0.102)
        assert not vresult.valid
        violation = RouteClearanceViolation(
            vresult.violation_x,
            vresult.violation_y,
            cpp_backend._CPP_VIOLATION_KINDS[int(vresult.violation_type)],
            int(vresult.violation_type),
            float(vresult.min_clearance),
        )
        assert violation.kind == "seg-seg"
        # Tuple compatibility with the historical (x, y) return.
        assert (violation[0], violation[1]) == (violation.x, violation.y)


@requires_cpp
class TestResumeStrategy:
    """Drive ``route()`` with a fake C++ impl + a validator that always
    rejects, so the full resume loop (escalation, restart, goal rejection,
    exhaustion) runs deterministically."""

    def _run_always_rejecting(
        self,
        violation_xy: tuple[float, float] | None = None,
    ) -> tuple[CppPathfinder, _FakeImpl, mock.MagicMock]:
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_5599")
        fake = _FakeImpl(net=start.net)
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder,
                "_validate_route_clearance",
                side_effect=lambda *a, **k: cpp_backend.RouteClearanceViolation(
                    5.0, 5.0 if violation_xy is None else violation_xy[1],
                    "seg-pad", 1, 0.1,
                ),
            ) as validate,
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None),
        ):
            pathfinder.route(start, end)
        return pathfinder, fake, validate

    def test_repeated_site_escalates_boost_and_restarts_search(self) -> None:
        pathfinder, fake, _ = self._run_always_rejecting()

        stats = pathfinder.fallback_stats
        record = stats["resume_diagnostics"]["NET_5599"]
        assert record["exhausted"] is True

        attempts = record["attempts"]
        assert [a["strategy"] for a in attempts] == [
            "resume",
            "resume",
            "restart",
            "restart",
            "restart",
            "exhausted",
        ]
        # Issue #5599 escalation: 20 * 4**run while the same site repeats.
        assert [a["boost_amount"] for a in attempts] == [
            20.0,
            80.0,
            320.0,
            1280.0,
            5120.0,
            20480.0,
        ]
        # One initial search + one fresh restart per repeated-site attempt.
        assert len(fake.route_resumable_calls) == 4
        # Issue #5599: the evidence-gated strict foreign-pad kernel is reset
        # at route() entry and armed -- LOCALIZED to the violation site --
        # from the first repeat on; never armed for a fresh search.
        assert fake.strict_pad_kernel_calls[0] == (False, -1, -1, 0)
        for call in fake.strict_pad_kernel_calls[1:]:
            enabled, cx, cy, radius = call
            assert enabled is True
            assert cx >= 0 and cy >= 0 and radius > 0, (
                "strict mode must be localized to the repeated violation site"
            )

    def test_restart_replays_every_rejected_goal_cell(self) -> None:
        pathfinder, fake, _ = self._run_always_rejecting()
        record = pathfinder.fallback_stats["resume_diagnostics"]["NET_5599"]
        rejected = [tuple(a["rejected_goal"]) for a in record["attempts"][:5]]

        # Plain resumes reject the current accepted goal node...
        assert fake.resume_calls[0] == rejected[0]
        assert fake.resume_calls[1] == rejected[1]
        # ...and the first restart replays ALL rejections collected so far
        # (the current attempt's included) before yielding a candidate.
        restart_replay = fake.resume_calls[2 : 2 + 3]
        assert restart_replay == rejected[:3]

    def test_goal_rejection_uses_accepted_goal_node_not_pad_center(self) -> None:
        """The reject cell must come from ``RouteResult.goal_*`` (the node A*
        accepted), not the last segment's endpoint (always the pad center)."""
        pathfinder, fake, _ = self._run_always_rejecting()
        record = pathfinder.fallback_stats["resume_diagnostics"]["NET_5599"]
        goals = [tuple(a["rejected_goal"]) for a in record["attempts"][:5]]
        # Every fake result accepts a distinct goal node -- if the loop were
        # still deriving the cell from the (constant) last-segment endpoint,
        # all five would be identical.
        assert len(set(goals)) == 5
        assert fake.resume_calls[0] == goals[0]

    def test_restart_budget_is_remaining_share_of_per_net_cap(self) -> None:
        pathfinder = _make_pathfinder(per_net_iterations=1_000_000)
        assert pathfinder._effective_search_iterations == 1_000_000
        start, end = _make_pads(net_name="NET_BUDGET")
        fake = _FakeImpl(net=start.net)
        fake.iterations = 400_000
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder,
                "_validate_route_clearance",
                return_value=cpp_backend.RouteClearanceViolation(5.0, 5.0, "seg-pad", 1, 0.1),
            ),
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None),
        ):
            pathfinder.route(start, end)

        # Initial search: the full effective cap.
        assert fake.route_resumable_calls[0] == (0.0, 1_000_000)
        # First restart (attempt 2): remaining = 1_000_000 - 400_000, split
        # over the 3 attempts still eligible to restart (attempts 2, 3, 4).
        assert fake.route_resumable_calls[1] == (0.0, (1_000_000 - 400_000) // 3)

    def test_repeated_goal_vicinity_violation_rejects_landing_band(self) -> None:
        """A repeated violation AT the goal pad's neighborhood must reject the
        landing's BAND (growing radius), not just the single landing cell --
        the single-cell walk is what kept board 04's BOOT0 inside the pad's
        failing center band for all six attempts."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_BAND")
        fake = _FakeImpl(net=start.net)

        # (7.6, 5.0) sits inside the end pad's metal+margin vicinity (end pad
        # at (8.0, 5.0)); (5.0, 5.0) in the other tests does not.
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder,
                "_validate_route_clearance",
                return_value=cpp_backend.RouteClearanceViolation(7.6, 5.0, "seg-pad", 1, 0.1),
            ),
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None),
        ):
            pathfinder.route(start, end)

        attempts = pathfinder.fallback_stats["resume_diagnostics"]["NET_BAND"]["attempts"]
        counts = [a.get("rejected_goal_count") for a in attempts]
        assert counts[0] == 1, "first sighting keeps the historical single-cell rejection"
        assert counts[1] == 9, "first repeat rejects the 3x3 landing band"
        assert counts[2] >= 17, "later repeats reject growing bands"
        # All rejections were actually issued to the C++ side.
        assert len(fake.resume_calls) >= sum(c for c in counts if c)

    def test_distinct_violation_sites_never_restart(self) -> None:
        """A violation that MOVES between attempts is not the near-clone
        signature -- the loop keeps plain resumes and the historical flat
        20.0 boost."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_MOVING")
        fake = _FakeImpl(net=start.net)

        sites = iter([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0), (6.0, 6.0)])

        def moving_violation(*args, **kwargs):
            x, y = next(sites)
            return cpp_backend.RouteClearanceViolation(x, y, "seg-pad", 1, 0.1)

        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(
                pathfinder, "_validate_route_clearance", side_effect=moving_violation
            ),
            mock.patch.object(pathfinder, "_try_python_fallback", return_value=None),
        ):
            pathfinder.route(start, end)

        record = pathfinder.fallback_stats["resume_diagnostics"]["NET_MOVING"]
        attempts = record["attempts"]
        assert all(a["strategy"] in ("resume", "exhausted") for a in attempts)
        assert len(fake.route_resumable_calls) == 1
        assert all(a["boost_amount"] == 20.0 for a in attempts)
        # Distinct sites never arm the strict kernel (it stays disarmed from
        # the route()-entry reset).
        assert not any(c[0] for c in fake.strict_pad_kernel_calls)

    def test_recovered_route_records_non_exhausted_diagnostics(self) -> None:
        """A loop that recovers after failed attempts records exhausted=False
        (and does NOT warn about a give-up)."""
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_RECOVER")
        fake = _FakeImpl(net=start.net)

        verdicts = iter(
            [cpp_backend.RouteClearanceViolation(5.0, 5.0, "seg-pad", 1, 0.1)] * 2 + [None]
        )

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
        record = pathfinder.fallback_stats["resume_diagnostics"]["NET_RECOVER"]
        assert record["exhausted"] is False
        assert len(record["attempts"]) == 2

    def test_diagnostics_absent_for_clean_first_try_route(self) -> None:
        pathfinder = _make_pathfinder()
        start, end = _make_pads(net_name="NET_CLEAN")
        fake = _FakeImpl(net=start.net)
        with (
            mock.patch.object(pathfinder, "_impl", fake),
            mock.patch.object(pathfinder, "_validate_route_clearance", return_value=None),
        ):
            route = pathfinder.route(start, end)

        assert route is not None
        assert "NET_CLEAN" not in pathfinder.fallback_stats["resume_diagnostics"]

    def test_per_attempt_records_carry_full_shape(self) -> None:
        pathfinder, fake, _ = self._run_always_rejecting()
        attempts = pathfinder.fallback_stats["resume_diagnostics"]["NET_5599"]["attempts"]
        for a in attempts:
            assert set(a) >= {
                "attempt",
                "violation_kind",
                "violation_type_code",
                "violation_xy",
                "violation_cell",
                "min_clearance",
                "rejected_goal",
                "strategy",
                "boost_amount",
            }
        assert attempts[0]["violation_kind"] == "seg-pad"
        assert attempts[0]["min_clearance"] == 0.1
        # The final (budget-exhausting) attempt rejects no goal cell.
        assert attempts[-1]["rejected_goal"] is None
        assert attempts[-1]["strategy"] == "exhausted"
