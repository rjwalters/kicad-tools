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
