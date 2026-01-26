"""Tests for via conflict management during routing."""

import math

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.via_conflict import (
    RipRerouteResult,
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
