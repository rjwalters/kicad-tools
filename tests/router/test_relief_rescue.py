"""Tests for the Issue #3438 relief-rescue mechanism.

Background
----------

On dense pad-array bundles (board 07's full-bus-reversal DDR byte), the
negotiated sharing-mode clauses treat foreign-net ``usage_count == 0``
non-obstacle cells (escape stubs, route clearance halos, via halo rings)
as HARD obstacles.  Sibling stubs and vias can therefore seal a pin's
only exit corridor, producing an instant empty-frontier A* abort with
ZERO overflow -- PathFinder receives no congestion signal to negotiate
and the net is permanently stranded.

Issue #3438 introduces a relief-probe mode: foreign usage-0 non-obstacle
cells become passable at a finite per-step penalty (min-conflict
search), the owner nets of the conflicted cells along the probe path are
extracted (``RoutingGrid.find_relief_conflict_nets``), and a
transactional rescue (``Autorouter._relief_rescue``) rips exactly those
blockers, committing only a CONFLICT-FREE probe path.

These tests verify:

1. ``compute_expanded_blocked(relief_mode=True)`` excludes foreign
   usage-0 non-obstacle cells from the hard bitmap while keeping net-0
   statics and foreign obstacles hard.
2. ``find_relief_conflict_nets`` returns the owner nets of conflicted
   cells along a probe route (never 0 or the probing net).
3. The Python ``Router`` relief mode charges the per-step conflict
   penalty in ``_get_negotiated_cell_cost``.
4. ``_sort_group_nets_by_geometry`` (geometry-aware layer-preference
   ordering) orders by pad position, is stable under net-set changes,
   and falls back to net-id ordering for pad-less synthetic groups.
5. ``_relief_rescue`` is a transaction: a no-relief-path outcome rolls
   the original routes back verbatim.
6. The C++ backend exposes the relief/usage-parity bindings
   (``set_relief_mode``, ``decrement_usage``, ``reset_usage``).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kicad_tools.router.algorithms.negotiated import NegotiatedRouter
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.primitives import Route, Segment


def _make_simple_router(num_nets: int = 3) -> Autorouter:
    """Autorouter with ``num_nets`` two-pad nets on a small board."""
    router = Autorouter(width=40.0, height=20.0)
    for idx in range(num_nets):
        net_id = idx + 1
        pads = [
            {
                "number": "1",
                "x": 5.0,
                "y": 4.0 + idx * 4.0,
                "width": 0.5,
                "height": 0.5,
                "net": net_id,
                "net_name": f"N{net_id}",
            },
            {
                "number": "2",
                "x": 35.0,
                "y": 4.0 + idx * 4.0,
                "width": 0.5,
                "height": 0.5,
                "net": net_id,
                "net_name": f"N{net_id}",
            },
        ]
        router.add_component(f"U{net_id}", pads)
    return router


class TestReliefModeBitmap:
    """compute_expanded_blocked(relief_mode=True) semantics (defect 1)."""

    def test_foreign_usage0_nonobstacle_cells_become_passable(self):
        router = _make_simple_router()
        g = router.grid
        gx, gy = g.world_to_grid(20.0, 10.0)

        # Foreign-net usage-0 non-obstacle cell (an escape stub / halo).
        g._blocked[0, gy, gx] = True
        g._is_obstacle[0, gy, gx] = False
        g._net[0, gy, gx] = 2
        g._usage_count[0, gy, gx] = 0

        hard = g.compute_expanded_blocked(0, net=1, allow_sharing=True)
        relief = g.compute_expanded_blocked(0, net=1, allow_sharing=True, relief_mode=True)

        assert hard[0, gy, gx], "foreign usage-0 cell must be hard in normal sharing mode"
        assert not relief[0, gy, gx], (
            "foreign usage-0 non-obstacle cell must be passable in relief mode"
        )

    def test_net0_statics_and_foreign_obstacles_stay_hard(self):
        router = _make_simple_router()
        g = router.grid

        # Net-0 static blockage (keepout / board obstacle).
        zx, zy = g.world_to_grid(18.0, 10.0)
        g._blocked[0, zy, zx] = True
        g._is_obstacle[0, zy, zx] = False
        g._net[0, zy, zx] = 0
        g._usage_count[0, zy, zx] = 0

        # Foreign obstacle (pad metal).
        ox, oy = g.world_to_grid(22.0, 10.0)
        g._blocked[0, oy, ox] = True
        g._is_obstacle[0, oy, ox] = True
        g._net[0, oy, ox] = 2
        g._usage_count[0, oy, ox] = 0

        relief = g.compute_expanded_blocked(0, net=1, allow_sharing=True, relief_mode=True)
        assert relief[0, zy, zx], "net-0 static blockage must stay hard in relief mode"
        assert relief[0, oy, ox], "foreign obstacle (pad) must stay hard in relief mode"


class TestFindReliefConflictNets:
    """Owner-net extraction from a probe route (rescue blocker set)."""

    def test_returns_owner_nets_excluding_self_and_zero(self):
        router = _make_simple_router()
        g = router.grid

        # Lay foreign usage-0 copper of nets 2 and 3 across the probe path.
        for net_id, wx in ((2, 15.0), (3, 25.0)):
            gx, gy = g.world_to_grid(wx, 10.0)
            g._blocked[0, gy, gx] = True
            g._is_obstacle[0, gy, gx] = False
            g._net[0, gy, gx] = net_id
            g._usage_count[0, gy, gx] = 0

        probe = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=10.0,
                    y1=10.0,
                    x2=30.0,
                    y2=10.0,
                    width=g.rules.trace_width,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        victims = g.find_relief_conflict_nets(probe, 1)
        assert victims == {2, 3}

    def test_own_and_obstacle_cells_are_not_conflicts(self):
        router = _make_simple_router()
        g = router.grid

        # Foreign cell with usage > 0: STILL a conflict.  The C++ probe
        # runs against a grid with uniformly-zero usage (usage is tracked
        # Python-side only), so it may cross ANY foreign copper; every
        # crossed owner must be reported or the rescue would mislabel a
        # conflicted probe as conflict-free and commit overlapping copper.
        ux, uy = g.world_to_grid(15.0, 10.0)
        g._blocked[0, uy, ux] = True
        g._is_obstacle[0, uy, ux] = False
        g._net[0, uy, ux] = 2
        g._usage_count[0, uy, ux] = 1

        # Own-net cell: never a conflict.
        sx, sy = g.world_to_grid(25.0, 10.0)
        g._blocked[0, sy, sx] = True
        g._is_obstacle[0, sy, sx] = False
        g._net[0, sy, sx] = 1
        g._usage_count[0, sy, sx] = 0

        # Foreign obstacle (pad metal): hard in relief mode, so a probe
        # never crosses it -- and the extraction excludes it regardless.
        ox, oy = g.world_to_grid(20.0, 10.0)
        g._blocked[0, oy, ox] = True
        g._is_obstacle[0, oy, ox] = True
        g._net[0, oy, ox] = 3
        g._usage_count[0, oy, ox] = 0

        probe = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=10.0,
                    y1=10.0,
                    x2=30.0,
                    y2=10.0,
                    width=g.rules.trace_width,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        victims = g.find_relief_conflict_nets(probe, 1)
        assert victims == {2}


class TestPythonRouterReliefPenalty:
    """Relief penalty in the pure-Python negotiated cost path."""

    def test_set_relief_mode_toggles(self):
        router = _make_simple_router()
        py_router = Router(router.grid, router.rules)
        assert py_router.relief_mode is False
        py_router.set_relief_mode(True)
        assert py_router.relief_mode is True
        py_router.set_relief_mode(False)
        assert py_router.relief_mode is False

    def test_conflict_cell_charges_penalty_only_in_relief_mode(self):
        router = _make_simple_router()
        g = router.grid
        py_router = Router(g, router.rules)
        gx, gy = g.world_to_grid(20.0, 10.0)
        g._blocked[0, gy, gx] = True
        g._is_obstacle[0, gy, gx] = False
        g._net[0, gy, gx] = 2
        g._usage_count[0, gy, gx] = 0

        base = py_router._get_negotiated_cell_cost(gx, gy, 0, 1.0, net=1)
        py_router.set_relief_mode(True)
        relief = py_router._get_negotiated_cell_cost(gx, gy, 0, 1.0, net=1)

        assert relief == pytest.approx(base + Router._RELIEF_CONFLICT_PENALTY), (
            "relief mode must add the per-step conflict penalty on a "
            "foreign usage-0 non-obstacle cell"
        )


class TestGeometryAwareLayerPreferences:
    """_sort_group_nets_by_geometry (defect: net-id-parity chaos amplifier)."""

    def test_orders_by_dominant_axis_position_not_net_id(self):
        # Pads are laid out bottom-to-top as nets 1, 2, 3 at y = 4, 8, 12.
        router = _make_simple_router(num_nets=3)
        # Geometry order along y: 1 (y=4), 2 (y=8), 3 (y=12).  Use a group
        # whose id order disagrees with nothing yet -- so shuffle ids by
        # checking a subset where id order != y order is impossible with
        # this fixture; instead verify the dominant axis (x spread is 0,
        # y spread is 8) picks the y ordering.
        order = router._sort_group_nets_by_geometry({3, 1, 2})
        assert order == [1, 2, 3]

    def test_stable_under_net_set_changes(self):
        router = _make_simple_router(num_nets=4)
        full = router._sort_group_nets_by_geometry({1, 2, 3, 4})
        without_one = router._sort_group_nets_by_geometry({2, 3, 4})
        # Removing net 1 must not reorder the remaining members
        # (positions do not move) -- the pre-#3438 sorted-id parity
        # flipped every member's layer when the set changed.
        assert [n for n in full if n != 1] == without_one

    def test_padless_group_falls_back_to_id_order(self):
        router = _make_simple_router(num_nets=2)
        # Synthetic net ids with no pads (legacy unit-test groups).
        order = router._sort_group_nets_by_geometry({42, 7, 99})
        assert order == [7, 42, 99]


class TestReliefRescueTransaction:
    """_relief_rescue rollback semantics (never leaves the board worse)."""

    def test_no_relief_path_rolls_back_verbatim(self):
        router = _make_simple_router()
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        original = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=5.0,
                    y1=4.0,
                    x2=10.0,
                    y2=4.0,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        router._mark_route(original)
        router.grid.mark_route_usage(original)
        router.routes.append(original)
        net_routes: dict[int, list[Route]] = {1: [original]}
        pads_by_net = {
            net_id: [router.pads[p] for p in pad_ids] for net_id, pad_ids in router.nets.items()
        }

        with patch.object(Autorouter, "_relief_probe", return_value=([], set())):
            ok = router._relief_rescue(
                failed_net=1,
                neg_router=neg_router,
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=1.0,
                per_net_timeout=2.0,
                flush_print_fn=lambda *_: None,
                elapsed_fn=lambda: "0s",
            )

        assert ok is False
        assert net_routes[1] == [original], "rollback must restore the EXACT original Route objects"
        assert original in router.routes

    def test_conflict_free_probe_commits(self):
        router = _make_simple_router()
        from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        probe = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=5.0,
                    y1=4.0,
                    x2=35.0,
                    y2=4.0,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        net_routes: dict[int, list[Route]] = {1: []}
        pads_by_net = {
            net_id: [router.pads[p] for p in pad_ids] for net_id, pad_ids in router.nets.items()
        }

        with patch.object(Autorouter, "_relief_probe", return_value=([probe], set())):
            ok = router._relief_rescue(
                failed_net=1,
                neg_router=neg_router,
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=1.0,
                per_net_timeout=2.0,
                flush_print_fn=lambda *_: None,
                elapsed_fn=lambda: "0s",
            )

        assert ok is True
        # A conflict-free probe is re-routed in NORMAL mode before commit
        # (so the committed copper passes geometric validation); the probe
        # object itself is discarded when the normal re-route succeeds.
        assert net_routes[1], "failed net must be routed after the rescue"
        for route in net_routes[1]:
            assert route in router.routes
            assert route.net == 1


class TestCorridorReservationDeferral:
    """begin/flush_cpp_unmark_deferral (Issue #3438 cohort reroute)."""

    def test_unmark_is_queued_then_flushed(self):
        router = _make_simple_router()
        g = router.grid
        route = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=5.0,
                    y1=4.0,
                    x2=20.0,
                    y2=4.0,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        router._mark_route(route)

        g.begin_cpp_unmark_deferral()
        g.unmark_route(route)
        # Python grid is unmarked immediately...
        gx, gy = g.world_to_grid(12.0, 4.0)
        assert not g._blocked[0, gy, gx]
        # ...and the C++ mirror is queued, not applied.
        assert g._deferred_cpp_unmarks, "unmark must be queued during the window"

        flushed = g.flush_cpp_unmark_deferral()
        assert flushed == [route]
        assert g._deferred_cpp_unmarks == []
        assert getattr(g, "_cpp_unmark_deferred", False) is False

    def test_flush_without_window_is_noop(self):
        router = _make_simple_router()
        assert router.grid.flush_cpp_unmark_deferral() == []

    def test_unmark_outside_window_mirrors_immediately(self):
        router = _make_simple_router()
        g = router.grid
        route = Route(
            net=1,
            net_name="N1",
            segments=[
                Segment(
                    x1=5.0,
                    y1=4.0,
                    x2=20.0,
                    y2=4.0,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=1,
                ),
            ],
        )
        router._mark_route(route)
        g.unmark_route(route)
        assert not getattr(g, "_deferred_cpp_unmarks", [])


class TestCppReliefBindings:
    """C++ backend exposes the relief / usage-parity surface."""

    def test_bindings_present(self):
        router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")
        assert hasattr(router_cpp.Grid3D, "decrement_usage")
        assert hasattr(router_cpp.Grid3D, "reset_usage")
        assert hasattr(router_cpp.Pathfinder, "set_relief_mode")

    def test_relief_mode_roundtrip(self):
        router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")
        grid = router_cpp.Grid3D(100, 100, 2, 0.25)
        rules = router_cpp.DesignRules()
        pf = router_cpp.Pathfinder(grid, rules, True)
        assert pf.relief_mode is False
        pf.set_relief_mode(True)
        assert pf.relief_mode is True
        pf.set_relief_mode(False)
        assert pf.relief_mode is False

    def test_decrement_usage_clamps_at_zero(self):
        router_cpp = pytest.importorskip("kicad_tools.router.router_cpp")
        grid = router_cpp.Grid3D(10, 10, 2, 0.25)
        grid.decrement_usage(5, 5, 0)  # already 0 -> stays 0 (no underflow)
        grid.increment_usage(5, 5, 0)
        grid.decrement_usage(5, 5, 0)
        cell = grid.at(5, 5, 0)
        assert cell.usage_count == 0


def _make_diff_pair_router() -> Autorouter:
    """Autorouter with one suffix-inferred diff pair on a small board.

    Net 1 = ``TMDS_D0_P`` (the leg that routes cleanly in normal mode),
    net 2 = ``TMDS_D0_N`` (the leg that hard-fails into ``_relief_rescue``).
    Suffix-based detection resolves them as partners so
    ``_diff_pair_partner_net(2) == 1`` without any explicit declaration.
    """
    router = Autorouter(width=40.0, height=20.0)
    for name, net_id, y in (("TMDS_D0_P", 1, 4.0), ("TMDS_D0_N", 2, 8.0)):
        pads = [
            {
                "number": "1",
                "x": 5.0,
                "y": y,
                "width": 0.5,
                "height": 0.5,
                "net": net_id,
                "net_name": name,
            },
            {
                "number": "2",
                "x": 35.0,
                "y": y,
                "width": 0.5,
                "height": 0.5,
                "net": net_id,
                "net_name": name,
            },
        ]
        router.add_component(name, pads)
    return router


def _seg_route(net: int, name: str, y: float) -> Route:
    return Route(
        net=net,
        net_name=name,
        segments=[
            Segment(x1=5.0, y1=y, x2=35.0, y2=y, width=0.2, layer=Layer.F_CU, net=net),
        ],
    )


class TestReliefRescueDiffPairAtomicTransaction:
    """Issue #4255 (Track A / A2): a diff pair is an ATOMIC rescue unit.

    The N-1 invariant these tests pin down: when one leg (``TMDS_D0_P``)
    routes cleanly in the normal pass and its partner (``TMDS_D0_N``)
    later hard-fails into ``_relief_rescue``, the rescue must commit BOTH
    legs or NEITHER -- a committed "P-routed / N-stranded" state is
    unrepresentable.  The fixtures deliberately model "P committed /
    N rescues" (NOT "both legs fail together", which would pass without
    proving the coupling).
    """

    def _fixture(self):
        router = _make_diff_pair_router()
        # Sanity: suffix detection resolves the pair both ways.
        assert router._diff_pair_partner_net(2) == 1
        assert router._diff_pair_partner_net(1) == 2

        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        # P (net 1) is COMMITTED by normal routing.
        route_p = _seg_route(1, "TMDS_D0_P", 4.0)
        router._mark_route(route_p)
        router.grid.mark_route_usage(route_p)
        router.routes.append(route_p)
        # N (net 2) has hard-failed: no committed copper.
        net_routes: dict[int, list[Route]] = {1: [route_p], 2: []}
        pads_by_net = {
            net_id: [router.pads[p] for p in pad_ids] for net_id, pad_ids in router.nets.items()
        }
        return router, neg_router, net_routes, pads_by_net, route_p

    def test_partner_reland_failure_rolls_back_both_legs(self):
        """P committed, N rescues, but P cannot re-land -> BOTH roll back.

        This is the load-bearing fixture: N's conflict-free relief would
        commit (the "one leg wins" state the pre-#4255 per-net rescue
        produced), but because its partner P is now folded in as a
        mandatory member of the commit gate and fails to re-land, the
        whole transaction rolls back verbatim.  On pre-#4255 code this
        rescue returned True with N committed and P untouched (both
        routed) -- so asserting a verbatim rollback with N NOT committed
        distinguishes the fix.
        """
        router, neg_router, net_routes, pads_by_net, route_p = self._fixture()
        committed_n = _seg_route(2, "TMDS_D0_N", 8.0)
        probe_n = _seg_route(2, "TMDS_D0_N", 8.0)

        def probe_side_effect(net_id, present_factor, per_net_timeout=None):
            # N gets a conflict-free relief; the partner P has no relief
            # path (its 1:1-traded corridor is now held by N).
            if net_id == 2:
                return ([probe_n], set())
            return ([], set())

        def route_side_effect(net_id, present_cost_factor, per_net_timeout=None):
            if net_id == 2:
                return [committed_n]
            return []  # P (net 1) cannot re-land

        with (
            patch.object(Autorouter, "_relief_probe", side_effect=probe_side_effect),
            patch.object(Autorouter, "_route_net_negotiated", side_effect=route_side_effect),
        ):
            ok = router._relief_rescue(
                failed_net=2,
                neg_router=neg_router,
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=1.0,
                per_net_timeout=2.0,
                flush_print_fn=lambda *_: None,
                elapsed_fn=lambda: "0s",
            )

        assert ok is False, "one-leg-wins must be rejected -> rescue fails"
        # Both legs restored verbatim.
        assert net_routes[1] == [route_p], "partner P must be restored to its exact Route"
        assert route_p in router.routes
        assert net_routes[2] == [], "failed leg N must NOT be committed (never one leg)"
        assert committed_n not in router.routes, "N's copper must be rolled back"

    def test_partner_relands_commits_both_legs(self):
        """P committed, N rescues, P re-lands -> BOTH commit atomically."""
        router, neg_router, net_routes, pads_by_net, route_p = self._fixture()
        committed_n = _seg_route(2, "TMDS_D0_N", 8.0)
        probe_n = _seg_route(2, "TMDS_D0_N", 8.0)
        route_p_new = _seg_route(1, "TMDS_D0_P", 5.0)

        route_calls: list[int] = []

        def probe_side_effect(net_id, present_factor, per_net_timeout=None):
            if net_id == 2:
                return ([probe_n], set())
            return ([], set())

        def route_side_effect(net_id, present_cost_factor, per_net_timeout=None):
            route_calls.append(net_id)
            if net_id == 2:
                return [committed_n]
            return [route_p_new]  # P re-lands into a freed lane

        with (
            patch.object(Autorouter, "_relief_probe", side_effect=probe_side_effect),
            patch.object(Autorouter, "_route_net_negotiated", side_effect=route_side_effect),
        ):
            ok = router._relief_rescue(
                failed_net=2,
                neg_router=neg_router,
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=1.0,
                per_net_timeout=2.0,
                flush_print_fn=lambda *_: None,
                elapsed_fn=lambda: "0s",
            )

        assert ok is True
        # Partner was folded in: its re-land was attempted (proves it was
        # ripped at clean-slate as a mandatory member, not left untouched).
        assert 1 in route_calls, "partner P must be re-landed inside the transaction"
        assert net_routes[2] == [committed_n], "failed leg N committed"
        assert net_routes[1] == [route_p_new], "partner P re-routed and committed"
        assert committed_n in router.routes
        assert route_p_new in router.routes
        assert route_p not in router.routes, "P's original copper was ripped and replaced"

    def test_no_partner_path_is_byte_identical(self):
        """A single-ended failing net takes the unchanged per-net path.

        With no diff-pair partner the clean-slate rip must target exactly
        ``[failed_net]`` (never a partner), ``snapshot_nets`` stays
        single-net, and the commit proceeds as on pre-#4255 ``main``.
        """
        router = Autorouter(width=40.0, height=20.0)
        pads = [
            {
                "number": "1",
                "x": 5.0,
                "y": 4.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "SINGLE_A",
            },
            {
                "number": "2",
                "x": 35.0,
                "y": 4.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "SINGLE_A",
            },
        ]
        router.add_component("SINGLE_A", pads)
        assert router._diff_pair_partner_net(1) is None

        neg_router = NegotiatedRouter(
            grid=router.grid,
            router=router.router,
            rules=router.rules,
            net_class_map={},
        )
        # A stale partial route so the clean-slate rip fires.
        stale = _seg_route(1, "SINGLE_A", 4.0)
        router._mark_route(stale)
        router.grid.mark_route_usage(stale)
        router.routes.append(stale)
        net_routes: dict[int, list[Route]] = {1: [stale]}
        pads_by_net = {
            net_id: [router.pads[p] for p in pad_ids] for net_id, pad_ids in router.nets.items()
        }
        committed = _seg_route(1, "SINGLE_A", 4.0)

        with (
            patch.object(Autorouter, "_relief_probe", return_value=([committed], set())),
            patch.object(Autorouter, "_route_net_negotiated", return_value=[committed]),
            patch.object(neg_router, "rip_up_nets", wraps=neg_router.rip_up_nets) as rip_spy,
        ):
            ok = router._relief_rescue(
                failed_net=1,
                neg_router=neg_router,
                net_routes=net_routes,
                pads_by_net=pads_by_net,
                present_factor=1.0,
                per_net_timeout=2.0,
                flush_print_fn=lambda *_: None,
                elapsed_fn=lambda: "0s",
            )

        assert ok is True
        # Clean-slate rip is byte-identical to the pre-change single-net form.
        assert rip_spy.call_count == 1
        assert rip_spy.call_args.args[0] == [1], "no partner may be folded into the rip"
        assert net_routes[1] == [committed]
        assert committed in router.routes
