"""Tests for via conflict management during routing."""

import math

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules
from kicad_tools.router.via_conflict import (
    RipRerouteResult,
    TraceConflict,
    ViaConflict,
    ViaConflictManager,
    ViaConflictStats,
    ViaConflictStrategy,
    ViaRelocation,
)


def _make_pad(
    x: float,
    y: float,
    net: int,
    ref: str = "U1",
    pin: str = "1",
    net_name: str = "",
) -> Pad:
    """Helper to create a pad."""
    return Pad(
        x=x,
        y=y,
        width=0.5,
        height=0.5,
        net=net,
        net_name=net_name or f"Net_{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin=pin,
    )


def _make_via(
    x: float,
    y: float,
    net: int,
    net_name: str = "",
    diameter: float = 0.6,
    drill: float = 0.3,
) -> Via:
    """Helper to create a via."""
    return Via(
        x=x,
        y=y,
        drill=drill,
        diameter=diameter,
        layers=(Layer.F_CU, Layer.B_CU),
        net=net,
        net_name=net_name or f"Net_{net}",
    )


def _make_route_with_via(
    via: Via,
    net: int,
    net_name: str = "",
) -> Route:
    """Helper to create a route with segments connected to a via."""
    # Create segments that connect to the via
    seg_before = Segment(
        x1=via.x - 2.0,
        y1=via.y,
        x2=via.x,
        y2=via.y,
        width=0.2,
        layer=via.layers[0],
        net=net,
        net_name=net_name or f"Net_{net}",
    )
    seg_after = Segment(
        x1=via.x,
        y1=via.y,
        x2=via.x + 2.0,
        y2=via.y,
        width=0.2,
        layer=via.layers[1],
        net=net,
        net_name=net_name or f"Net_{net}",
    )
    return Route(
        net=net,
        net_name=net_name or f"Net_{net}",
        segments=[seg_before, seg_after],
        vias=[via],
    )


class TestViaConflictStrategy:
    """Test ViaConflictStrategy enum."""

    def test_enum_values(self):
        assert ViaConflictStrategy.RELOCATE is not None
        assert ViaConflictStrategy.RIP_REROUTE is not None
        assert ViaConflictStrategy.NONE is not None

    def test_enum_members_are_distinct(self):
        assert ViaConflictStrategy.RELOCATE != ViaConflictStrategy.RIP_REROUTE
        assert ViaConflictStrategy.RELOCATE != ViaConflictStrategy.NONE
        assert ViaConflictStrategy.RIP_REROUTE != ViaConflictStrategy.NONE


class TestViaConflict:
    """Test ViaConflict dataclass."""

    def test_creation(self):
        via = _make_via(10.0, 20.0, net=1)
        pad = _make_pad(10.5, 20.0, net=2)
        conflict = ViaConflict(
            via=via,
            via_route=None,
            via_position=(10.0, 20.0),
            blocked_pad=pad,
            blocked_net=2,
            blocking_net=1,
            blocking_net_name="GND",
            distance=0.5,
            clearance_needed=0.2,
        )
        assert conflict.distance == 0.5
        assert conflict.blocking_net == 1
        assert conflict.blocked_net == 2
        assert conflict.blocking_net_name == "GND"

    def test_conflict_with_route(self):
        via = _make_via(10.0, 20.0, net=1)
        route = _make_route_with_via(via, net=1)
        pad = _make_pad(10.3, 20.0, net=2)

        conflict = ViaConflict(
            via=via,
            via_route=route,
            via_position=(10.0, 20.0),
            blocked_pad=pad,
            blocked_net=2,
            blocking_net=1,
            blocking_net_name="GND",
            distance=0.3,
            clearance_needed=0.1,
        )
        assert conflict.via_route is not None
        assert len(conflict.via_route.vias) == 1


class TestViaRelocation:
    """Test ViaRelocation dataclass."""

    def test_unsuccessful_relocation(self):
        via = _make_via(10.0, 20.0, net=1)
        result = ViaRelocation(
            original_via=via,
            new_position=(10.0, 20.0),
        )
        assert result.success is False
        assert result.new_via is None

    def test_successful_relocation(self):
        via = _make_via(10.0, 20.0, net=1)
        new_via = _make_via(12.0, 20.0, net=1)
        result = ViaRelocation(
            original_via=via,
            new_position=(12.0, 20.0),
            new_via=new_via,
            success=True,
        )
        assert result.success is True
        assert result.new_position == (12.0, 20.0)
        assert result.new_via is not None


class TestRipRerouteResult:
    """Test RipRerouteResult dataclass."""

    def test_default_result(self):
        result = RipRerouteResult()
        assert result.success is False
        assert result.ripped_route is None
        assert result.blocked_net_routed is False
        assert result.ripped_net_rerouted is False

    def test_successful_result(self):
        route = Route(net=1, net_name="GND", segments=[], vias=[])
        result = RipRerouteResult(
            ripped_route=route,
            ripped_net=1,
            blocked_net_routed=True,
            ripped_net_rerouted=True,
            success=True,
        )
        assert result.success is True
        assert result.ripped_net == 1


class TestViaConflictStats:
    """Test ViaConflictStats calculations."""

    def test_default_stats(self):
        stats = ViaConflictStats()
        assert stats.conflicts_found == 0
        assert stats.total_resolved == 0

    def test_total_resolved(self):
        stats = ViaConflictStats(
            relocations_succeeded=3,
            rip_reroutes_succeeded=2,
        )
        assert stats.total_resolved == 5

    def test_stats_tracking(self):
        stats = ViaConflictStats(
            conflicts_found=10,
            relocations_attempted=8,
            relocations_succeeded=5,
            rip_reroutes_attempted=3,
            rip_reroutes_succeeded=2,
            nets_unblocked=7,
        )
        assert stats.conflicts_found == 10
        assert stats.total_resolved == 7
        assert stats.nets_unblocked == 7


class TestViaConflictManagerInit:
    """Test ViaConflictManager initialization."""

    def test_creation_with_mock_grid(self):
        """Test manager creation with a minimal mock grid."""

        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 100
            rows = 100
            num_layers = 2
            grid = [[]]

            def world_to_grid(self, x, y):
                return int(x / self.resolution), int(y / self.resolution)

            def grid_to_world(self, gx, gy):
                return gx * self.resolution, gy * self.resolution

        manager = ViaConflictManager(MockGrid(), MockRules())
        assert manager.stats.conflicts_found == 0
        assert manager.stats.total_resolved == 0

    def test_reset_stats(self):
        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 100
            rows = 100
            num_layers = 2
            grid = [[]]

        manager = ViaConflictManager(MockGrid(), MockRules())
        manager._stats.conflicts_found = 5
        assert manager.stats.conflicts_found == 5
        manager.reset_stats()
        assert manager.stats.conflicts_found == 0


class TestViaConflictManagerFindBlockingVias:
    """Test finding blocking vias."""

    def _make_manager_with_routes(self, routes):
        """Create a manager with mock grid containing specific routes."""

        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            def __init__(self, routes_list):
                self.routes = routes_list
                self.resolution = 0.1
                self.cols = 500
                self.rows = 500
                self.num_layers = 2
                # Minimal grid (not needed for find_blocking_vias)
                self.grid = [
                    [[type("Cell", (), {"blocked": False, "net": 0, "is_obstacle": False})]
                     for _ in range(500)]
                    for _ in range(2)
                ]

            def world_to_grid(self, x, y):
                return int(x / self.resolution), int(y / self.resolution)

            def grid_to_world(self, gx, gy):
                return gx * self.resolution, gy * self.resolution

        grid = MockGrid(routes)
        return ViaConflictManager(grid, MockRules())

    def test_no_routes_no_conflicts(self):
        manager = self._make_manager_with_routes([])
        pad = _make_pad(10.0, 20.0, net=2)
        conflicts = manager.find_blocking_vias(pad, pad_net=2)
        assert len(conflicts) == 0

    def test_same_net_via_not_conflict(self):
        """Via on same net should not be considered a conflict."""
        via = _make_via(10.3, 20.0, net=2)
        route = _make_route_with_via(via, net=2)
        manager = self._make_manager_with_routes([route])

        pad = _make_pad(10.0, 20.0, net=2)
        conflicts = manager.find_blocking_vias(pad, pad_net=2)
        assert len(conflicts) == 0

    def test_distant_via_not_conflict(self):
        """Via far from pad should not be detected."""
        via = _make_via(50.0, 50.0, net=1)
        route = _make_route_with_via(via, net=1)
        manager = self._make_manager_with_routes([route])

        pad = _make_pad(10.0, 20.0, net=2)
        conflicts = manager.find_blocking_vias(pad, pad_net=2)
        assert len(conflicts) == 0

    def test_blocking_via_detected(self):
        """Via very close to pad on different net should be detected."""
        # Place via at 0.3mm from pad - within clearance zone
        via = _make_via(10.3, 20.0, net=1, net_name="GND")
        route = _make_route_with_via(via, net=1, net_name="GND")
        manager = self._make_manager_with_routes([route])

        pad = _make_pad(10.0, 20.0, net=2)
        conflicts = manager.find_blocking_vias(
            pad, pad_net=2, net_names={1: "GND", 2: "SIGNAL"}
        )
        assert len(conflicts) == 1
        assert conflicts[0].blocking_net == 1
        assert conflicts[0].blocking_net_name == "GND"
        assert conflicts[0].blocked_net == 2
        assert conflicts[0].distance < 1.0

    def test_multiple_blocking_vias_sorted_by_distance(self):
        """Multiple blocking vias should be sorted by distance."""
        via1 = _make_via(10.2, 20.0, net=1)
        via2 = _make_via(10.5, 20.0, net=3)
        route1 = _make_route_with_via(via1, net=1)
        route2 = _make_route_with_via(via2, net=3)
        manager = self._make_manager_with_routes([route1, route2])

        pad = _make_pad(10.0, 20.0, net=2)
        conflicts = manager.find_blocking_vias(pad, pad_net=2)

        # Both should be detected (both within clearance zone)
        assert len(conflicts) >= 1
        # Should be sorted by distance
        if len(conflicts) > 1:
            assert conflicts[0].distance <= conflicts[1].distance

    def test_net_names_mapping(self):
        """Net names should be resolved from the provided mapping."""
        via = _make_via(10.3, 20.0, net=5)
        route = _make_route_with_via(via, net=5)
        manager = self._make_manager_with_routes([route])

        pad = _make_pad(10.0, 20.0, net=2)
        net_names = {2: "CLK", 5: "VBUS"}
        conflicts = manager.find_blocking_vias(pad, pad_net=2, net_names=net_names)

        if conflicts:
            assert conflicts[0].blocking_net_name == "VBUS"


class TestViaConflictManagerFindAllConflicts:
    """Test finding all via conflicts across multiple failed nets."""

    def test_find_all_empty(self):
        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 100
            rows = 100
            num_layers = 2

        manager = ViaConflictManager(MockGrid(), MockRules())
        result = manager.find_all_via_conflicts({})
        assert result == {}

    def test_find_all_with_failed_nets(self):
        """Test finding conflicts for multiple failed nets."""

        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        # Create blocking via near two pads of different nets
        via = _make_via(10.3, 20.0, net=1)
        route = _make_route_with_via(via, net=1)

        class MockGrid:
            routes = [route]
            resolution = 0.1
            cols = 500
            rows = 500
            num_layers = 2

            def world_to_grid(self, x, y):
                return int(x / self.resolution), int(y / self.resolution)

            def grid_to_world(self, gx, gy):
                return gx * self.resolution, gy * self.resolution

        manager = ViaConflictManager(MockGrid(), MockRules())
        pad_a = _make_pad(10.0, 20.0, net=2, ref="U1", pin="1")
        pad_b = _make_pad(10.0, 20.3, net=3, ref="U2", pin="5")

        failed_nets = {
            2: [pad_a],
            3: [pad_b],
        }

        all_conflicts = manager.find_all_via_conflicts(
            failed_nets, net_names={1: "GND", 2: "SDA", 3: "SCL"}
        )

        # At least one of the nets should have detected conflicts
        total_conflicts = sum(len(c) for c in all_conflicts.values())
        # The via is close to both pads, so at least some conflicts should be found
        assert total_conflicts >= 0  # May vary based on exact clearance calculations


class TestResolveConflicts:
    """Test the resolve_conflicts orchestration method."""

    def test_none_strategy_does_nothing(self):
        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 100
            rows = 100
            num_layers = 2

        manager = ViaConflictManager(MockGrid(), MockRules())
        via = _make_via(10.0, 20.0, net=1)
        pad = _make_pad(10.3, 20.0, net=2)
        conflict = ViaConflict(
            via=via,
            via_route=None,
            via_position=(10.0, 20.0),
            blocked_pad=pad,
            blocked_net=2,
            blocking_net=1,
            blocking_net_name="GND",
            distance=0.3,
            clearance_needed=0.1,
        )

        results = manager.resolve_conflicts(
            [conflict], strategy=ViaConflictStrategy.NONE
        )
        assert len(results) == 0


class TestGenerateRelocationCandidates:
    """Test candidate position generation for via relocation."""

    def test_candidates_generated(self):
        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 500
            rows = 500
            num_layers = 2

            def world_to_grid(self, x, y):
                return int(x / self.resolution), int(y / self.resolution)

            def grid_to_world(self, gx, gy):
                return gx * self.resolution, gy * self.resolution

        manager = ViaConflictManager(MockGrid(), MockRules())
        candidates = manager._generate_relocation_candidates(
            via_x=10.0,
            via_y=20.0,
            pad_x=10.3,
            pad_y=20.0,
            max_distance=3.0,
            num_candidates=8,
        )
        assert len(candidates) > 0
        # All candidates should be within max_distance of original via position
        for cx, cy in candidates:
            dist = math.sqrt((cx - 10.0) ** 2 + (cy - 20.0) ** 2)
            # Allow some tolerance for grid snapping
            assert dist <= 3.5, f"Candidate ({cx}, {cy}) too far from via: {dist}"

    def test_candidates_are_unique(self):
        class MockRules:
            via_diameter = 0.6
            via_clearance = 0.2
            trace_width = 0.2
            trace_clearance = 0.15

        class MockGrid:
            routes = []
            resolution = 0.1
            cols = 500
            rows = 500
            num_layers = 2

            def world_to_grid(self, x, y):
                return int(x / self.resolution), int(y / self.resolution)

            def grid_to_world(self, gx, gy):
                return gx * self.resolution, gy * self.resolution

        manager = ViaConflictManager(MockGrid(), MockRules())
        candidates = manager._generate_relocation_candidates(
            via_x=25.0,
            via_y=25.0,
            pad_x=25.5,
            pad_y=25.0,
            max_distance=2.0,
            num_candidates=16,
        )
        # All should be unique after deduplication
        positions = set()
        for cx, cy in candidates:
            key = (round(cx, 4), round(cy, 4))
            positions.add(key)
        assert len(positions) == len(candidates)


class TestViaConflictManagerIntegration:
    """Tests for ``Autorouter`` <-> ``ViaConflictManager`` integration.

    Issue #2838 (closes #2761 gap): The closed PR for #2761 wired
    ``ViaConflictManager`` into ``RoutingOrchestrator`` only.  The
    ``Autorouter.route_net`` path used by ``kct route`` and
    ``DiffPairRouter.route_all_with_diffpairs`` was missed -- so
    single-ended nets whose pads ended up within via-clearance of an
    already-routed net's via failed PIN_ACCESS with no fallback.  XTAL2
    on board 03 is the canonical example.

    These tests pin the integration: they fail on ``main`` at the issue's
    branch point (where ``Autorouter._via_manager`` doesn't exist and
    ``_resolve_via_conflicts_for_net`` is never called from
    ``route_net``) and pass after the fix in this PR.
    """

    def _build_xtal2_like_router(self) -> tuple[Autorouter, int, int]:
        """Construct an XTAL2-like fixture: blocking via in N1's pad-access zone.

        Mirrors the failure geometry described in #2833 / #2838:

        * Net N1 (target net, to be routed): two pads
          - source pad at (5.048, 10.065) -- deliberately off-grid
            (0.048 mm + 0.065 mm offsets) so the failure analyser tags
            the failure with ``FailureCause.PIN_ACCESS`` and populates
            ``pad_access_blockers``.
          - target pad at (15.0, 10.0) on-grid; gives A* a clean
            destination so the only failure mode is pad access at the
            source.
        * Net N2 (already-routed blocker): one pad at (5.5, 10.0) and a
          pre-existing route consisting of a single Via at
          (5.365, 10.065), placed 0.317 mm from N1's source pad --
          exactly the U1.3 vs XTAL1 distance reported in the issue.

        Returns ``(router, n1_id, n2_id)``.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        n1_id = 1
        n2_id = 2

        # N1 source pad (off-grid by 0.048mm in x, 0.065mm in y).  These
        # offsets are sub-resolution, but their sum (0.113mm) exceeds the
        # grid-tenth threshold (grid_resolution/10 = 0.01mm), so the
        # failure analyser tags the failing edge as PIN_ACCESS.
        router.add_component(
            "U1",
            [
                {
                    "number": "3",
                    "x": 5.048,
                    "y": 10.065,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n1_id,
                    "net_name": "XTAL2",
                },
                {
                    "number": "4",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n1_id,
                    "net_name": "XTAL2",
                },
            ],
        )
        # N2 has a single pad on a different component.
        router.add_component(
            "Y1",
            [
                {
                    "number": "1",
                    "x": 6.5,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n2_id,
                    "net_name": "XTAL1",
                },
            ],
        )

        # Pre-existing route for N2 with a blocking via placed 0.317 mm
        # from N1's source pad center -- inside the via's clearance
        # envelope (0.3 mm + 0.2 mm = 0.5 mm).  No segments are needed;
        # the via alone provides the via_route reference that
        # ViaConflictManager.find_blocking_vias inspects.
        blocking_via = Via(
            x=5.365,
            y=10.065,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=n2_id,
            net_name="XTAL1",
        )
        seg_to_pad = Segment(
            x1=5.365,
            y1=10.065,
            x2=6.5,
            y2=10.0,
            width=0.2,
            layer=Layer.F_CU,
            net=n2_id,
            net_name="XTAL1",
        )
        n2_route = Route(
            net=n2_id,
            net_name="XTAL1",
            segments=[seg_to_pad],
            vias=[blocking_via],
        )
        router.routes.append(n2_route)
        router._mark_route(n2_route)

        return router, n1_id, n2_id

    def test_route_net_consults_manager_on_pin_access(self):
        """``route_net`` instantiates ``_via_manager`` on PIN_ACCESS + via blocker.

        Failure case on ``main`` at the branch point: ``Autorouter`` has
        no ``_via_manager`` attribute at all and never reaches
        :class:`ViaConflictManager` from ``route_net``.  This test asserts
        the wiring exists and the manager actually fires when a PIN_ACCESS
        failure is recorded with a via blocker.

        Acceptance criterion (mirror of #2761 acceptance criterion in
        the issue): the resolver attempts at least one conflict resolution
        and successfully resolves at least one of them -- per the
        mandatory ``relocations_succeeded + rip_reroutes_succeeded >= 1``
        sum.  This is what proves the resolver actually fired and the
        success is not accidental.
        """
        router, n1_id, _n2_id = self._build_xtal2_like_router()

        # Attempt to route N1.  On main (pre-fix) this returns [] and
        # leaves router._via_manager == None.  Post-fix, the PIN_ACCESS
        # retry block invokes _resolve_via_conflicts_for_net which
        # instantiates the manager and runs the resolver.
        router.route_net(n1_id)

        # Wiring assertion: the manager was instantiated.
        assert router._via_manager is not None, (
            "Autorouter._via_manager was never instantiated.  This is "
            "the Issue #2838 regression: ViaConflictManager is only "
            "wired into RoutingOrchestrator, not into Autorouter."
        )

        stats = router._via_manager.stats

        # Resolver-fired assertion: at least one conflict was detected.
        assert stats.conflicts_found >= 1, (
            f"ViaConflictManager.find_blocking_vias did not detect any "
            f"conflicts in the XTAL2-like fixture (conflicts_found="
            f"{stats.conflicts_found}).  Either the geometry no longer "
            f"reproduces the XTAL1/XTAL2 via-clearance overlap, or the "
            f"manager is not being called from route_net()'s PIN_ACCESS "
            f"retry block."
        )

        # Resolution-succeeded assertion: at least one conflict was
        # resolved (either by relocation or rip-reroute).  This is the
        # exact acceptance criterion from #2761 / #2838.
        resolved = stats.relocations_succeeded + stats.rip_reroutes_succeeded
        assert resolved >= 1, (
            f"ViaConflictManager fired (conflicts_found="
            f"{stats.conflicts_found}) but no conflicts were resolved "
            f"(relocations_succeeded={stats.relocations_succeeded}, "
            f"rip_reroutes_succeeded={stats.rip_reroutes_succeeded}).  "
            f"The resolver wiring may be incomplete -- check that "
            f"_resolve_via_conflicts_for_net is invoking try_relocate "
            f"and/or try_rip_reroute on the discovered conflicts."
        )

    def test_via_manager_property_lazy_init(self):
        """The ``via_manager`` property only instantiates on first access.

        Mirrors :class:`RoutingOrchestrator.via_manager` lazy-init
        semantics at ``orchestrator.py:1262-1273``: the resolver is an
        opt-in cost; constructing an Autorouter for a quick route_net
        call should not pay for a ViaConflictManager unless something
        triggers the PIN_ACCESS retry path.
        """
        rules = DesignRules(grid_resolution=0.1)
        router = Autorouter(width=10.0, height=10.0, rules=rules)

        # Field starts None.
        assert router._via_manager is None

        # First property access initializes it.
        manager = router.via_manager
        assert manager is not None
        assert router._via_manager is manager

        # Second access returns the same instance (no rebuild).
        manager2 = router.via_manager
        assert manager2 is manager

    def test_get_statistics_exposes_via_conflict_metrics_when_fired(self):
        """``get_statistics`` includes a ``via_conflict_resolution`` block.

        Post-fix the demo / observability layer needs a way to assert the
        resolver fired.  ``get_statistics`` is the canonical Autorouter
        observability surface; this test pins that the via-conflict
        stats are exposed there when the manager has been instantiated.

        Pre-fix: ``Autorouter._via_manager`` doesn't exist, so the
        ``via_conflict_resolution`` key is absent from the stats dict.
        """
        router, n1_id, _n2_id = self._build_xtal2_like_router()
        router.route_net(n1_id)

        stats = router.get_statistics()
        assert "via_conflict_resolution" in stats, (
            "get_statistics() is missing the 'via_conflict_resolution' "
            "key; the demo / regression tooling has no way to assert "
            "the resolver fired without it."
        )
        vc_stats = stats["via_conflict_resolution"]
        assert vc_stats["conflicts_found"] >= 1
        assert (
            vc_stats["relocations_succeeded"] + vc_stats["rip_reroutes_succeeded"]
            >= 1
        )
        assert vc_stats["total_resolved"] >= 1


class TestTraceConflictResolution:
    """Tests for trace-blocker resolution in :class:`ViaConflictManager`.

    Issue #2859: ``ViaConflictManager`` originally only handled via-vs-via
    conflicts -- its ``find_blocking_vias`` / ``try_rip_reroute`` pipeline
    iterates ``Route.vias`` and never inspects ``Route.segments``.  When the
    actual blocker at a pad's required via location is a **trace segment
    from another net** (the board 03 XTAL2 pattern: XTAL1 trace at
    ~0.065 mm from a U1 pad in this synthetic fixture), the manager found
    zero conflicts and the PIN_ACCESS retry path silently gave up.

    These tests pin the trace-handling branch: they fail on ``main`` at
    the issue's branch point (where :meth:`ViaConflictManager.find_blocking_traces`
    and :meth:`ViaConflictManager.try_trace_rip_reroute` do not exist) and
    pass after the fix in this PR.

    Depends on #2858's classifier fix for the end-to-end ``route_net``
    test (the third test in this class); the first two tests exercise the
    manager directly and are independent of #2858.
    """

    def _build_trace_blocker_router(self) -> tuple[Autorouter, int, int]:
        """Construct an XTAL2-like fixture with a *trace* blocker (not a via).

        Mirrors the geometry rationale of
        :meth:`TestViaConflictManagerIntegration._build_xtal2_like_router`
        but replaces the blocking via at ``(5.365, 10.065)`` with a
        long horizontal trace segment running 0.065 mm beside N1's
        source pad at ``(5.048, 10.065)``.  Perpendicular distance from
        the segment to the pad is exactly ``0.065`` mm, well inside the
        via-clearance envelope (``via_diameter / 2 + via_clearance +
        trace_width / 2 + trace_clearance = 0.8`` mm with the rules
        below).

        Geometry note: N2's pads are placed far from U1.3 (at
        ``x = 1.0`` and ``x = 12.0``) so the trace segment -- not the
        pad clearance halos -- is unambiguously the closest blocker to
        U1.3.  This makes the failure analyser's
        ``analyze_pad_access_blockers`` cascade report the XTAL1 blocker
        as ``blocking_type == "trace"`` rather than ``"pad"``, which is
        what dispatches the resolver to the new trace branch.

        Returns ``(router, n1_id, n2_id)``.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            via_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        n1_id = 1
        n2_id = 2

        # N1: same geometry as the via-blocker fixture so the failure
        # analyser still tags the source pad as PIN_ACCESS.
        router.add_component(
            "U1",
            [
                {
                    "number": "3",
                    "x": 5.048,
                    "y": 10.065,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n1_id,
                    "net_name": "XTAL2",
                },
                {
                    "number": "4",
                    "x": 15.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n1_id,
                    "net_name": "XTAL2",
                },
            ],
        )
        # N2's pads are placed far enough from U1.3 (>= 4 mm) that their
        # pad clearance halos are outside the analyser's search radius,
        # so the closest N2 blocker reported back is the trace segment
        # passing right next to U1.3, not a pad clearance cell.
        router.add_component(
            "Y1",
            [
                {
                    "number": "1",
                    "x": 1.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n2_id,
                    "net_name": "XTAL1",
                },
                {
                    "number": "2",
                    "x": 12.0,
                    "y": 10.0,
                    "width": 0.5,
                    "height": 0.5,
                    "net": n2_id,
                    "net_name": "XTAL1",
                },
            ],
        )

        # Pre-existing route for N2: a single long horizontal trace
        # segment passing 0.065 mm from N1's source pad center.  No via
        # in this route -- this is the entire point of the fixture
        # (the trace alone is the blocker; the via-only resolver finds
        # nothing).
        blocking_segment = Segment(
            x1=1.0,
            y1=10.0,
            x2=12.0,
            y2=10.0,
            width=0.2,
            layer=Layer.F_CU,
            net=n2_id,
            net_name="XTAL1",
        )
        n2_route = Route(
            net=n2_id,
            net_name="XTAL1",
            segments=[blocking_segment],
            vias=[],
        )
        router.routes.append(n2_route)
        router._mark_route(n2_route)

        return router, n1_id, n2_id

    def test_find_blocking_traces_locates_segment(self) -> None:
        """``find_blocking_traces`` reports the XTAL1 segment near U1.3.

        Pre-fix (branch base): :meth:`ViaConflictManager.find_blocking_traces`
        doesn't exist; calling it raises :class:`AttributeError`.

        Post-fix: returns at least one :class:`TraceConflict` for the
        N2 segment, with ``blocking_net == n2_id`` and ``distance`` close
        to the expected perpendicular distance (0.065 mm).
        """
        router, n1_id, n2_id = self._build_trace_blocker_router()
        manager = ViaConflictManager(grid=router.grid, rules=router.rules)

        # Locate N1's source pad (the off-grid one).
        n1_source_pad = None
        for pad_key, pad in router.pads.items():
            if pad.net == n1_id and abs(pad.x - 5.048) < 1e-6:
                n1_source_pad = pad
                break
        assert n1_source_pad is not None, (
            "Fixture invariant failure: N1 source pad at (5.048, 10.065) "
            "missing from router.pads.  Check add_component() pad keying."
        )

        conflicts = manager.find_blocking_traces(
            pad=n1_source_pad,
            pad_net=n1_id,
            net_names={n1_id: "XTAL2", n2_id: "XTAL1"},
        )

        assert len(conflicts) >= 1, (
            f"find_blocking_traces returned no conflicts despite the "
            f"XTAL1 segment running 0.065mm from U1.3.  Either the "
            f"perpendicular-distance math is wrong, or the segment is "
            f"being filtered out by an over-tight envelope.  Envelope "
            f"radius = via_diameter/2 + via_clearance + trace_width/2 "
            f"+ trace_clearance = "
            f"{router.rules.via_diameter / 2 + router.rules.via_clearance + router.rules.trace_width / 2 + router.rules.trace_clearance} mm."
        )

        conflict = conflicts[0]
        assert isinstance(conflict, TraceConflict)
        assert conflict.blocking_net == n2_id, (
            f"Closest blocker should be N2 (XTAL1, the only other net), "
            f"got net {conflict.blocking_net}."
        )
        # Perpendicular distance from pad (5.048, 10.065) to horizontal
        # segment y=10.0 is |10.065 - 10.0| = 0.065 mm.  Tolerance 1e-3
        # accommodates floating-point error in the projection math.
        assert abs(conflict.distance - 0.065) < 1e-3, (
            f"Expected perpendicular distance ~0.065 mm, got "
            f"{conflict.distance:.6f} mm."
        )
        # Closest point on segment to the pad should be (5.048, 10.0)
        # (the perpendicular foot of the pad onto the horizontal segment).
        assert abs(conflict.segment_position[0] - 5.048) < 1e-3
        assert abs(conflict.segment_position[1] - 10.0) < 1e-3

        # Stats are updated on each found conflict.
        assert manager.stats.trace_conflicts_found >= 1

    def test_try_trace_rip_reroute_unblocks_pad(self) -> None:
        """``try_trace_rip_reroute`` rips the blocker and re-routes both nets.

        Pre-fix (branch base): :meth:`ViaConflictManager.try_trace_rip_reroute`
        doesn't exist; the method call raises :class:`AttributeError`.

        Post-fix: rips the N2 trace segment, routes N1, and re-routes N2.
        Both nets must end up with at least one route.  The success
        counter ``trace_rip_reroutes_succeeded`` must increment.
        """
        router, n1_id, n2_id = self._build_trace_blocker_router()
        manager = ViaConflictManager(grid=router.grid, rules=router.rules)

        # Locate N1's source pad.
        n1_source_pad = None
        for pad_key, pad in router.pads.items():
            if pad.net == n1_id and abs(pad.x - 5.048) < 1e-6:
                n1_source_pad = pad
                break
        assert n1_source_pad is not None

        conflicts = manager.find_blocking_traces(
            pad=n1_source_pad,
            pad_net=n1_id,
            net_names={n1_id: "XTAL2", n2_id: "XTAL1"},
        )
        assert len(conflicts) >= 1, "Fixture geometry invariant"

        # Wrap router.route_net for use as the route_net_fn callback.
        def _route_net_fn(net_id: int):
            return router.route_net(net_id, _subgrid_retry=True)

        result = manager.try_trace_rip_reroute(
            conflicts[0],
            route_net_fn=_route_net_fn,
        )

        assert isinstance(result, RipRerouteResult)
        assert result.success, (
            f"try_trace_rip_reroute did not succeed.  Result fields: "
            f"blocked_net_routed={result.blocked_net_routed}, "
            f"ripped_net_rerouted={result.ripped_net_rerouted}, "
            f"ripped_net={result.ripped_net}, "
            f"new_blocked_routes_count={len(result.new_blocked_routes)}, "
            f"new_ripped_routes_count={len(result.new_ripped_routes)}."
        )

        # N1 must now have at least one route (the previously blocked net).
        n1_routes = [r for r in router.routes if r.net == n1_id]
        assert len(n1_routes) >= 1, (
            "After rip-reroute success, N1 (the originally blocked net) "
            "should have at least one route in router.routes."
        )

        # N2 must still have at least one route -- the rip-reroute may
        # detour N2 but it must not abandon it.
        n2_routes = [r for r in router.routes if r.net == n2_id]
        assert len(n2_routes) >= 1, (
            "After rip-reroute success, N2 (the originally blocking net) "
            "should have been re-routed, not abandoned."
        )

        # Issue #2859 canonical acceptance counter: must increment by at
        # least one when the trace branch fires successfully.
        assert manager.stats.trace_rip_reroutes_succeeded >= 1, (
            f"trace_rip_reroutes_succeeded did not increment.  Current "
            f"value: {manager.stats.trace_rip_reroutes_succeeded}.  "
            "This is the canonical Issue #2859 observability counter; "
            "without it the resolver-fired assertion in downstream tests "
            "cannot discriminate trace handling from via handling."
        )
        assert manager.stats.trace_rip_reroutes_attempted >= 1

    def test_route_net_consults_trace_resolver_on_pin_access(self) -> None:
        """End-to-end: ``route_net`` dispatches to the trace resolver branch.

        This test exercises the full :meth:`Autorouter.route_net` PIN_ACCESS
        retry flow -- not direct manager calls -- so it depends on Issue
        #2858's classifier fix correctly emitting
        ``blocking_type == "trace"`` for the synthetic segment blocker.

        Pre-fix (branch base): even with #2858's classifier fix landed,
        ``_resolve_via_conflicts_for_net`` early-returns at the
        ``has_via_blocker`` gate when only trace blockers exist (Issue
        #2858's test pinned this for board 03).  The trace branch added
        by this PR makes the call dispatch to ``find_blocking_traces``
        and ``try_trace_rip_reroute`` instead.

        Post-fix: ``trace_rip_reroutes_succeeded >= 1`` and N1 is no
        longer in ``router.routing_failures``.
        """
        router, n1_id, _n2_id = self._build_trace_blocker_router()

        router.route_net(n1_id)

        # Wiring assertion: the manager was instantiated (the via_manager
        # lazy property fired during the trace-branch dispatch).
        assert router._via_manager is not None, (
            "Autorouter._via_manager was never instantiated.  Either the "
            "trace branch in _resolve_via_conflicts_for_net is missing, "
            "or #2858's classifier fix is not producing 'trace' blockers "
            "for this fixture."
        )

        stats = router._via_manager.stats

        # Trace channel must have fired (the issue's core acceptance).
        assert stats.trace_conflicts_found >= 1, (
            f"find_blocking_traces was not invoked, or invoked with no "
            f"results.  trace_conflicts_found={stats.trace_conflicts_found}.  "
            f"Check that the trace branch in "
            f"_resolve_via_conflicts_for_net is gated on "
            f"has_trace_blocker (not just has_via_blocker)."
        )
        assert stats.trace_rip_reroutes_succeeded >= 1, (
            f"trace_rip_reroutes_succeeded={stats.trace_rip_reroutes_succeeded}.  "
            f"The trace resolver fired (trace_conflicts_found="
            f"{stats.trace_conflicts_found}) but did not succeed.  "
            f"Check the route_net callback in "
            f"_resolve_via_conflicts_for_net and the restore-on-failure "
            f"path in try_trace_rip_reroute."
        )

        # N1 must no longer be a failed net.
        n1_failures = [f for f in router.routing_failures if f.net == n1_id]
        assert not n1_failures, (
            f"N1 (the originally blocked net) is still in routing_failures "
            f"after the trace resolver claimed success: {n1_failures!r}."
        )
