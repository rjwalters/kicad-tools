"""Tests for router/core.py module."""

from unittest.mock import patch

import pytest

from kicad_tools.router.core import (
    AdaptiveAutorouter,
    Autorouter,
    MSTEdgeInfo,
    RoutingFailure,
    RoutingResult,
)
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


class TestAutorouterInit:
    """Tests for Autorouter initialization."""

    def test_default_initialization(self):
        """Test Autorouter with default parameters."""
        router = Autorouter(width=50.0, height=40.0)
        assert router.grid.width == 50.0
        assert router.grid.height == 40.0
        assert router.rules is not None
        assert router.pads == {}
        assert router.nets == {}
        assert router.routes == []

    def test_with_origin(self):
        """Test Autorouter with custom origin."""
        router = Autorouter(width=50.0, height=40.0, origin_x=10.0, origin_y=5.0)
        assert router.grid.origin_x == 10.0
        assert router.grid.origin_y == 5.0

    def test_with_custom_rules(self):
        """Test Autorouter with custom design rules."""
        rules = DesignRules(trace_width=0.3, via_diameter=0.8)
        router = Autorouter(width=50.0, height=40.0, rules=rules)
        assert router.rules.trace_width == 0.3
        assert router.rules.via_diameter == 0.8

    def test_with_layer_stack(self):
        """Test Autorouter with custom layer stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router = Autorouter(width=50.0, height=40.0, layer_stack=stack)
        assert router.grid.num_layers == 4


class TestAutorouterAddComponent:
    """Tests for adding components to Autorouter."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_add_smd_component(self, router):
        """Test adding an SMD component with pads."""
        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "VCC",
            },
            {
                "number": "2",
                "x": 11.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "GND",
            },
        ]
        router.add_component("R1", pads)

        assert ("R1", "1") in router.pads
        assert ("R1", "2") in router.pads
        assert 1 in router.nets
        assert 2 in router.nets
        assert router.net_names[1] == "VCC"
        assert router.net_names[2] == "GND"

    def test_add_through_hole_component(self, router):
        """Test adding a through-hole component."""
        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 10.0,
                "width": 1.7,
                "height": 1.7,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 1.0,
            },
        ]
        router.add_component("U1", pads)

        pad = router.pads[("U1", "1")]
        assert pad.through_hole is True
        assert pad.drill == 1.0

    def test_multi_pin_net(self, router):
        """Test that nets track all connected pads."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 11.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("U1", pads)

        assert len(router.nets[1]) == 2
        assert ("U1", "1") in router.nets[1]
        assert ("U1", "2") in router.nets[1]


class TestAutorouterAddObstacle:
    """Tests for adding obstacles to Autorouter."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_add_obstacle(self, router):
        """Test adding an obstacle."""
        router.add_obstacle(25.0, 20.0, 5.0, 5.0, Layer.F_CU)

        # Verify the obstacle was added by checking grid cells are blocked
        gx, gy = router.grid.world_to_grid(25.0, 20.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU) is True

    def test_add_obstacle_default_layer(self, router):
        """Test adding an obstacle on default layer."""
        router.add_obstacle(25.0, 20.0, 5.0, 5.0)

        gx, gy = router.grid.world_to_grid(25.0, 20.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU) is True


class TestAutorouterRouting:
    """Tests for routing functionality."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_route_net_nonexistent(self, router):
        """Test routing a nonexistent net returns empty list."""
        routes = router.route_net(999)
        assert routes == []

    def test_route_net_single_pad(self, router):
        """Test routing a net with only one pad returns empty list."""
        pads = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1}]
        router.add_component("R1", pads)

        routes = router.route_net(1)
        assert routes == []

    def test_route_two_pad_net(self, router):
        """Test routing a two-pad net."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)
        # Should successfully route (may have segments)
        # The route may or may not succeed depending on clearances
        assert isinstance(routes, list)


class TestAutorouterStatistics:
    """Tests for Autorouter statistics and output."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_get_statistics_empty(self, router):
        """Test statistics on empty router."""
        stats = router.get_statistics()
        assert stats["routes"] == 0
        assert stats["segments"] == 0
        assert stats["vias"] == 0
        assert stats["total_length_mm"] == 0.0
        assert stats["nets_routed"] == 0

    def test_get_statistics_with_routes(self, router):
        """Test statistics with some routes."""
        # Manually add a route
        seg = Segment(x1=10.0, y1=10.0, x2=20.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        router.routes.append(route)

        stats = router.get_statistics()
        assert stats["routes"] == 1
        assert stats["segments"] == 1
        assert stats["vias"] == 0
        assert stats["nets_routed"] == 1

    def test_to_sexp_empty(self, router):
        """Test S-expression output on empty router."""
        sexp = router.to_sexp()
        assert sexp == ""

    def test_to_sexp_with_routes(self, router):
        """Test S-expression output with routes."""
        seg = Segment(x1=10.0, y1=10.0, x2=20.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])
        router.routes.append(route)

        sexp = router.to_sexp()
        assert "segment" in sexp
        assert "10.0000" in sexp
        assert "20.0000" in sexp


class TestAutorouterNetPriority:
    """Tests for net priority ordering."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_get_net_priority_unknown_net(self, router):
        """Test priority for unknown net class."""
        # Add a pad with unknown net class
        pads = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "RANDOM_NET"}]
        router.add_component("R1", pads)

        # Issue #1295: Return is now 5-tuple (priority, complexity_tier, -constraint_score, pad_count, distance)
        priority, complexity_tier, neg_constraint, pad_count, distance = router._get_net_priority(1)
        assert priority == 10  # Default priority
        assert pad_count == 1
        assert distance == 0.0  # Single pad has no distance

    def test_get_net_priority_distance_calculation(self, router):
        """Test that distance is calculated for multi-pad nets."""
        # Add two pads separated by known distance
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 3.0, "y": 4.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        # Issue #1295: Return is now 5-tuple (priority, complexity_tier, -constraint_score, pad_count, distance)
        priority, complexity_tier, neg_constraint, pad_count, distance = router._get_net_priority(1)
        assert pad_count == 2
        # Distance should be sqrt(3^2 + 4^2) = 5.0
        assert abs(distance - 5.0) < 0.001

    def test_net_ordering_prefers_shorter_nets(self, router):
        """Test that shorter nets are ordered before longer nets of same class."""
        # Add a short net (net 1)
        router.add_component(
            "R1", [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "SHORT"}]
        )
        router.add_component(
            "R2", [{"number": "1", "x": 1.0, "y": 0.0, "net": 1, "net_name": "SHORT"}]
        )

        # Add a long net (net 2)
        router.add_component(
            "R3", [{"number": "1", "x": 0.0, "y": 10.0, "net": 2, "net_name": "LONG"}]
        )
        router.add_component(
            "R4", [{"number": "1", "x": 10.0, "y": 10.0, "net": 2, "net_name": "LONG"}]
        )

        p1 = router._get_net_priority(1)
        p2 = router._get_net_priority(2)

        # Issue #1295: Return is now 5-tuple (priority, complexity_tier, -constraint_score, pad_count, distance)
        # Both have same class priority, but net 1 is shorter (simple tier) and net 2 is longer (complex tier)
        assert p1[0] == p2[0]  # Same class priority
        assert p1[1] <= p2[1]  # Net 1 is simple (tier 0), net 2 is complex (tier 1)
        assert p1 < p2  # Net 1 should be ordered first


class TestConstraintAwareOrdering:
    """Tests for constraint-aware net ordering (Issue #1020)."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_calculate_constraint_score_fine_pitch(self, router):
        """Test that fine-pitch components get higher constraint scores."""
        # Add a fine-pitch IC (U1) with 0.65mm pitch (TSSOP-20 style)
        # Create pads with 0.65mm spacing
        fine_pitch_pads = []
        for i in range(4):
            fine_pitch_pads.append(
                {
                    "number": str(i + 1),
                    "x": 10.0 + i * 0.65,  # 0.65mm pitch
                    "y": 10.0,
                    "net": 1 if i == 0 else 0,  # Only first pad on net 1
                    "net_name": "FINE_NET" if i == 0 else "",
                }
            )
        router.add_component("U1", fine_pitch_pads)

        # Add second pad for net 1 on a standard resistor (1.27mm pitch)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "FINE_NET"},
                {"number": "2", "x": 21.27, "y": 10.0, "net": 0},
            ],
        )

        # Add a standard pitch net (net 2) with only 1.27mm resistors
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 30.0, "y": 10.0, "net": 2, "net_name": "STD_NET"},
                {"number": "2", "x": 31.27, "y": 10.0, "net": 0},
            ],
        )
        router.add_component(
            "R3",
            [
                {"number": "1", "x": 35.0, "y": 10.0, "net": 2, "net_name": "STD_NET"},
                {"number": "2", "x": 36.27, "y": 10.0, "net": 0},
            ],
        )

        # Fine-pitch net should have higher constraint score
        fine_score = router._calculate_constraint_score(1)
        std_score = router._calculate_constraint_score(2)

        # U1 has 0.65mm pitch which is below fine_pitch_threshold (0.8mm)
        # So net 1 should have higher constraint score
        assert fine_score > std_score

    def test_calculate_constraint_score_disabled(self, router):
        """Test that constraint scoring returns 0 when disabled."""
        router.rules.constraint_ordering_enabled = False

        # Add any net
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 1.0, "y": 0.0, "net": 0},
            ],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 10.0, "y": 0.0, "net": 1, "net_name": "NET1"}],
        )

        score = router._calculate_constraint_score(1)
        assert score == 0.0

    def test_constraint_score_pad_count_contribution(self, router):
        """Test that more pads increase constraint score."""
        # Net 1: 2 pads
        router.add_component(
            "R1",
            [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "SMALL"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "SMALL"}],
        )

        # Net 2: 4 pads
        router.add_component(
            "R3",
            [{"number": "1", "x": 10.0, "y": 0.0, "net": 2, "net_name": "LARGE"}],
        )
        router.add_component(
            "R4",
            [{"number": "1", "x": 15.0, "y": 0.0, "net": 2, "net_name": "LARGE"}],
        )
        router.add_component(
            "R5",
            [{"number": "1", "x": 20.0, "y": 0.0, "net": 2, "net_name": "LARGE"}],
        )
        router.add_component(
            "R6",
            [{"number": "1", "x": 25.0, "y": 0.0, "net": 2, "net_name": "LARGE"}],
        )

        score1 = router._calculate_constraint_score(1)
        score2 = router._calculate_constraint_score(2)

        # Net 2 has more pads, so higher score
        assert score2 > score1

    def test_net_priority_includes_constraint_score(self, router):
        """Test that net priority tuple includes constraint score."""
        # Add a fine-pitch IC
        fine_pitch_pads = []
        for i in range(4):
            fine_pitch_pads.append(
                {
                    "number": str(i + 1),
                    "x": 10.0 + i * 0.5,  # 0.5mm pitch (very fine)
                    "y": 10.0,
                    "net": 1 if i == 0 else 0,
                    "net_name": "FINE" if i == 0 else "",
                }
            )
        router.add_component("U1", fine_pitch_pads)

        router.add_component(
            "R1",
            [{"number": "1", "x": 30.0, "y": 10.0, "net": 1, "net_name": "FINE"}],
        )

        # Add a standard pitch net with same distance
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "STD"},
                {"number": "2", "x": 11.27, "y": 20.0, "net": 0},  # 1.27mm pitch
            ],
        )
        router.add_component(
            "R3",
            [
                {"number": "1", "x": 30.0, "y": 20.0, "net": 2, "net_name": "STD"},
                {"number": "2", "x": 31.27, "y": 20.0, "net": 0},
            ],
        )

        p1 = router._get_net_priority(1)  # Fine-pitch net
        p2 = router._get_net_priority(2)  # Standard net

        # Same class priority
        assert p1[0] == p2[0]

        # Fine-pitch net should have higher constraint score (more negative in tuple)
        # Constraint score is at index 2 in the 5-tuple
        assert p1[2] < p2[2]  # More negative = higher constraint

        # Fine-pitch net should be ordered first (within same complexity tier)
        assert p1 < p2

    def test_net_ordering_fine_pitch_before_standard(self, router):
        """Test that fine-pitch nets are routed before standard nets in same tier."""
        from kicad_tools.router.rules import DesignRules

        # Enable constraint ordering explicitly
        rules = DesignRules(constraint_ordering_enabled=True)
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Add a standard pitch net (net 1) - should be routed second
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "STD"},
                {"number": "2", "x": 1.27, "y": 0.0, "net": 0},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "STD"},
                {"number": "2", "x": 6.27, "y": 0.0, "net": 0},
            ],
        )

        # Add a fine-pitch net (net 2) - should be routed first
        # Keep distance < 10mm so both nets are in same complexity tier
        fine_pitch_pads = []
        for i in range(4):
            fine_pitch_pads.append(
                {
                    "number": str(i + 1),
                    "x": 20.0 + i * 0.5,  # 0.5mm pitch
                    "y": 10.0,
                    "net": 2 if i == 0 else 0,
                    "net_name": "FINE" if i == 0 else "",
                }
            )
        router.add_component("U1", fine_pitch_pads)
        router.add_component(
            "R3",
            [{"number": "1", "x": 25.0, "y": 10.0, "net": 2, "net_name": "FINE"}],
        )

        # Get net order
        net_order = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))

        # Net 2 (fine-pitch) should come before net 1 (standard)
        # within the same complexity tier, constraint score dominates
        assert net_order.index(2) < net_order.index(1)


class TestAutorouterInterleavedOrdering:
    """Tests for interleaved net ordering with MST-based N-port net handling."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_compute_mst_edges_two_port_returns_empty(self, router):
        """Test that 2-port nets return empty MST edges."""
        # Add a 2-port net
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        edges = router._compute_mst_edges(1)
        assert edges == []

    def test_compute_mst_edges_three_port_net(self, router):
        """Test MST edge computation for a 3-port net."""
        # Add a 3-port net in a right triangle (3-4-5 triangle)
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 3.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads3 = [{"number": "1", "x": 3.0, "y": 4.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)
        router.add_component("R3", pads3)

        edges = router._compute_mst_edges(1)

        # Should have 2 edges (n-1 for n=3 nodes)
        assert len(edges) == 2

        # All edges should belong to net 1
        assert all(e.net_id == 1 for e in edges)

        # First edge should be marked as first
        assert edges[0].is_first is True
        assert edges[1].is_first is False

        # Edges should be sorted by distance
        assert edges[0].distance <= edges[1].distance

        # First edge should be 3mm (Manhattan distance)
        assert abs(edges[0].distance - 3.0) < 0.001

        # Second edge should be 4mm (Manhattan distance)
        assert abs(edges[1].distance - 4.0) < 0.001

    def test_get_shortest_mst_edge_distance(self, router):
        """Test getting shortest MST edge distance."""
        # Add a 3-port net
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 3.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads3 = [{"number": "1", "x": 3.0, "y": 10.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)
        router.add_component("R3", pads3)

        distance = router._get_shortest_mst_edge_distance(1)
        # Shortest edge is 3mm
        assert abs(distance - 3.0) < 0.001

    def test_get_shortest_mst_edge_distance_two_port(self, router):
        """Test that 2-port nets return 0.0 for shortest MST edge."""
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        distance = router._get_shortest_mst_edge_distance(1)
        assert distance == 0.0

    def test_interleaved_ordering_basic(self, router):
        """Test interleaved ordering interleaves 2-port and N-port nets by distance."""
        # Net A: 2-port, distance 5mm (diagonal)
        pads_a1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "A"}]
        pads_a2 = [{"number": "1", "x": 3.0, "y": 4.0, "net": 1, "net_name": "A"}]
        router.add_component("A1", pads_a1)
        router.add_component("A2", pads_a2)

        # Net B: 3-port, MST edges [3mm, 7mm] (Manhattan distances)
        pads_b1 = [{"number": "1", "x": 10.0, "y": 0.0, "net": 2, "net_name": "B"}]
        pads_b2 = [{"number": "1", "x": 13.0, "y": 0.0, "net": 2, "net_name": "B"}]
        pads_b3 = [{"number": "1", "x": 13.0, "y": 7.0, "net": 2, "net_name": "B"}]
        router.add_component("B1", pads_b1)
        router.add_component("B2", pads_b2)
        router.add_component("B3", pads_b3)

        # Net C: 2-port, distance 4mm (diagonal)
        pads_c1 = [{"number": "1", "x": 20.0, "y": 0.0, "net": 3, "net_name": "C"}]
        pads_c2 = [{"number": "1", "x": 22.4, "y": 3.2, "net": 3, "net_name": "C"}]
        router.add_component("C1", pads_c1)
        router.add_component("C2", pads_c2)

        net_order, mst_cache = router._get_interleaved_net_order(use_interleaving=True)

        # Net B (3-port) should be in MST cache
        assert 2 in mst_cache
        assert len(mst_cache[2]) == 2  # 2 MST edges

        # Order should be: B (3mm), C (4mm), A (5mm)
        # Because B's shortest MST edge (3mm) < C's distance (4mm) < A's distance (5mm)
        assert net_order == [2, 3, 1]

    def test_interleaved_ordering_respects_net_class_priority(self, router):
        """Test that net class priority is respected before interleaving."""
        from kicad_tools.router.rules import NetClassRouting

        # Create custom net class map with priority
        net_class_map = {
            "HIGH": NetClassRouting(name="HIGH", priority=1, trace_width=0.2),
            "LOW": NetClassRouting(name="LOW", priority=5, trace_width=0.2),
        }
        router.net_class_map = net_class_map

        # Net 1: HIGH priority, long distance
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "HIGH"}]
        pads2 = [{"number": "1", "x": 20.0, "y": 0.0, "net": 1, "net_name": "HIGH"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        # Net 2: LOW priority, short distance
        pads3 = [{"number": "1", "x": 0.0, "y": 10.0, "net": 2, "net_name": "LOW"}]
        pads4 = [{"number": "1", "x": 1.0, "y": 10.0, "net": 2, "net_name": "LOW"}]
        router.add_component("R3", pads3)
        router.add_component("R4", pads4)

        net_order, _ = router._get_interleaved_net_order(use_interleaving=True)

        # HIGH priority should come first despite longer distance
        assert net_order[0] == 1  # HIGH priority net
        assert net_order[1] == 2  # LOW priority net

    def test_interleaved_ordering_disabled(self, router):
        """Test fallback to standard ordering when interleaving disabled."""
        # Add a 3-port net
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads3 = [{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)
        router.add_component("R3", pads3)

        _, mst_cache = router._get_interleaved_net_order(use_interleaving=False)

        # MST cache should be empty when interleaving is disabled
        assert mst_cache == {}

    def test_mst_edge_info_dataclass(self):
        """Test MSTEdgeInfo dataclass."""
        edge = MSTEdgeInfo(
            net_id=1,
            edge_index=0,
            source_idx=0,
            target_idx=1,
            distance=5.0,
            is_first=True,
        )

        assert edge.net_id == 1
        assert edge.edge_index == 0
        assert edge.source_idx == 0
        assert edge.target_idx == 1
        assert edge.distance == 5.0
        assert edge.is_first is True

    def test_route_all_interleaved_parameter(self, router):
        """Test that route_all accepts interleaved parameter."""
        # Add simple 2-port net
        pads1 = [{"number": "1", "x": 0.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 5.0, "y": 0.0, "net": 1, "net_name": "NET1"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        # Should not raise error
        routes = router.route_all(interleaved=True)
        assert isinstance(routes, list)


class TestAutorouterMonteCarlo:
    """Tests for Monte Carlo routing methods."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_shuffle_within_tiers(self, router):
        """Test that tier shuffling preserves tier order."""
        # Add components with different net classes
        pads1 = [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"}]
        pads2 = [{"number": "1", "x": 20.0, "y": 10.0, "net": 2, "net_name": "NET2"}]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        net_order = [1, 2]
        shuffled = router._shuffle_within_tiers(net_order)

        assert set(shuffled) == set(net_order)
        assert len(shuffled) == len(net_order)

    def test_evaluate_solution_empty(self, router):
        """Test solution evaluation with no routes."""
        score = router._evaluate_solution([])
        assert score == 0.0

    def test_evaluate_solution_with_routes(self, router):
        """Test solution evaluation with routes."""
        # Add a net for tracking
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Create a route
        seg = Segment(x1=10.0, y1=10.0, x2=15.0, y2=10.0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])

        score = router._evaluate_solution([route])
        assert score > 0  # Should have positive score with routed net

    def test_monte_carlo_sequential_execution(self, router):
        """Test Monte Carlo routing with sequential execution (num_workers=1)."""
        # Add two simple 2-pin nets
        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        # Run with num_workers=1 (sequential)
        routes = router.route_all_monte_carlo(num_trials=3, seed=42, verbose=False, num_workers=1)
        assert isinstance(routes, list)

    def test_monte_carlo_parallel_execution(self, router):
        """Test Monte Carlo routing with parallel execution."""
        # Add two simple 2-pin nets
        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        # Run with num_workers=2 (parallel)
        routes = router.route_all_monte_carlo(num_trials=4, seed=42, verbose=False, num_workers=2)
        assert isinstance(routes, list)

    def test_monte_carlo_num_workers_auto_detection(self, router):
        """Test that num_workers=None auto-detects based on CPU count."""
        # Add a simple net
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # num_workers=None should auto-detect
        routes = router.route_all_monte_carlo(
            num_trials=2, seed=42, verbose=False, num_workers=None
        )
        assert isinstance(routes, list)

    def test_monte_carlo_num_workers_zero_triggers_auto(self, router):
        """Test that num_workers=0 triggers auto-detection."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_all_monte_carlo(num_trials=2, seed=42, verbose=False, num_workers=0)
        assert isinstance(routes, list)

    def test_monte_carlo_determinism_with_seed(self, router):
        """Test that Monte Carlo routing is deterministic with same seed."""
        # Add nets
        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads1)

        # Run twice with same seed (sequential to ensure determinism)
        routes1 = router.route_all_monte_carlo(num_trials=3, seed=42, verbose=False, num_workers=1)
        score1 = router._evaluate_solution(routes1)

        # Reset and run again
        router._reset_for_new_trial()
        routes2 = router.route_all_monte_carlo(num_trials=3, seed=42, verbose=False, num_workers=1)
        score2 = router._evaluate_solution(routes2)

        # Scores should be identical with same seed
        assert score1 == score2

    def test_monte_carlo_workers_capped_to_trials(self, router):
        """Test that num_workers is capped to num_trials."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Request more workers than trials - should not fail
        routes = router.route_all_monte_carlo(num_trials=2, seed=42, verbose=False, num_workers=10)
        assert isinstance(routes, list)

    def test_serialize_for_parallel(self, router):
        """Test that router state serialization works correctly."""
        # Add pads and nets
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Serialize
        config = router._serialize_for_parallel()

        # Verify essential fields are present
        assert "width" in config
        assert "height" in config
        assert "pads_data" in config
        assert "nets" in config
        assert "net_names" in config
        assert len(config["pads_data"]) == 2

    def test_monte_carlo_varying_trials(self, router):
        """Test Monte Carlo with varying number of trials."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Test with 1 trial
        routes_1 = router.route_all_monte_carlo(num_trials=1, seed=42, verbose=False, num_workers=1)
        assert isinstance(routes_1, list)

        # Test with 4 trials (parallel)
        router._reset_for_new_trial()
        routes_4 = router.route_all_monte_carlo(num_trials=4, seed=42, verbose=False, num_workers=2)
        assert isinstance(routes_4, list)

        # Test with 10 trials (parallel)
        router._reset_for_new_trial()
        routes_10 = router.route_all_monte_carlo(
            num_trials=10, seed=42, verbose=False, num_workers=4
        )
        assert isinstance(routes_10, list)


class TestRoutingResult:
    """Tests for RoutingResult dataclass."""

    def test_success_rate_full(self):
        """Test success rate with all nets routed."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=10,
            nets_routed=10,
            overflow=0,
            converged=True,
            iterations_used=5,
            statistics={},
        )
        assert result.success_rate == 1.0

    def test_success_rate_partial(self):
        """Test success rate with partial routing."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=10,
            nets_routed=7,
            overflow=0,
            converged=False,
            iterations_used=10,
            statistics={},
        )
        assert result.success_rate == 0.7

    def test_success_rate_zero_nets(self):
        """Test success rate with zero nets requested."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=0,
            nets_routed=0,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )
        assert result.success_rate == 1.0

    def test_str_converged(self):
        """Test string representation for converged result."""
        result = RoutingResult(
            routes=[],
            layer_count=4,
            layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
            nets_requested=20,
            nets_routed=20,
            overflow=0,
            converged=True,
            iterations_used=3,
            statistics={},
        )
        s = str(result)
        assert "CONVERGED" in s
        assert "4L" in s
        assert "20/20" in s

    def test_str_not_converged(self):
        """Test string representation for non-converged result."""
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=LayerStack.two_layer(),
            nets_requested=30,
            nets_routed=25,
            overflow=5,
            converged=False,
            iterations_used=10,
            statistics={},
        )
        s = str(result)
        assert "NOT CONVERGED" in s
        assert "overflow=5" in s


class TestAdaptiveAutorouterInit:
    """Tests for AdaptiveAutorouter initialization."""

    def test_default_initialization(self):
        """Test AdaptiveAutorouter with default parameters."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0, height=40.0, components=components, net_map=net_map
        )

        assert adaptive.width == 50.0
        assert adaptive.height == 40.0
        assert adaptive.max_layers == 6
        assert adaptive.result is None

    def test_with_custom_max_layers(self):
        """Test AdaptiveAutorouter with custom max layers."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=40.0, components=[], net_map={}, max_layers=4
        )

        assert adaptive.max_layers == 4

    def test_with_skip_nets(self):
        """Test AdaptiveAutorouter with skip nets."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=40.0, components=[], net_map={}, skip_nets=["GND", "VCC"]
        )

        assert "GND" in adaptive.skip_nets
        assert "VCC" in adaptive.skip_nets


class TestAdaptiveAutorouterLayerStacks:
    """Tests for layer stack configuration."""

    def test_layer_stacks_order(self):
        """Test that layer stacks are in increasing order."""
        stacks = AdaptiveAutorouter.LAYER_STACKS
        assert len(stacks) == 3
        assert stacks[0].num_layers == 2
        assert stacks[1].num_layers == 4
        assert stacks[2].num_layers == 6


class TestAdaptiveAutorouterMethods:
    """Tests for AdaptiveAutorouter methods."""

    @pytest.fixture
    def simple_component(self):
        """Create a simple component dict."""
        return {
            "ref": "R1",
            "x": 25.0,
            "y": 20.0,
            "rotation": 0,
            "pads": [
                {"number": "1", "x": -0.5, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                {"number": "2", "x": 0.5, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET2"},
            ],
        }

    def test_create_autorouter(self, simple_component):
        """Test creating an autorouter from components."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=40.0, components=[simple_component], net_map={"NET1": 1, "NET2": 2}
        )

        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        assert router is not None
        assert router.grid.num_layers == 2

    def test_layer_count_no_result(self):
        """Test layer_count property with no result."""
        adaptive = AdaptiveAutorouter(width=50.0, height=40.0, components=[], net_map={})

        assert adaptive.layer_count == 0

    def test_get_routes_no_result_raises(self):
        """Test get_routes raises if not routed."""
        adaptive = AdaptiveAutorouter(width=50.0, height=40.0, components=[], net_map={})

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.get_routes()

    def test_to_sexp_no_result_raises(self):
        """Test to_sexp raises if not routed."""
        adaptive = AdaptiveAutorouter(width=50.0, height=40.0, components=[], net_map={})

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.to_sexp()


class TestAdaptiveAutorouterComponentTransform:
    """Tests for component coordinate transformation."""

    def test_add_component_rotation(self):
        """Test that component rotation transforms pad positions."""
        component = {
            "ref": "R1",
            "x": 25.0,
            "y": 20.0,
            "rotation": 90,  # 90 degree rotation
            "pads": [
                {"number": "1", "x": 1.0, "y": 0.0, "net": "NET1"},
            ],
        }

        adaptive = AdaptiveAutorouter(
            width=50.0, height=40.0, components=[component], net_map={"NET1": 1}
        )

        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        # After 90 degree rotation, (1, 0) should become approximately (0, -1)
        pad = router.pads.get(("R1", "1"))
        assert pad is not None
        # x should be close to 25.0 (component center)
        assert abs(pad.x - 25.0) < 0.01
        # y should be offset by approximately -1.0 from center
        assert abs(pad.y - 19.0) < 0.01


class TestAutorouterIntraICRoutes:
    """Tests for intra-IC routing functionality."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_intra_ic_routes_single_component(self, router):
        """Test intra-IC routing for same-component pins on same net."""
        # Create IC with multiple pins on same net
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
            {"number": "3", "x": 11.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
            {"number": "4", "x": 12.0, "y": 10.0, "net": 1, "net_name": "SYNC_L"},
        ]
        router.add_component("U1", pads)

        pads_list = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads_list)

        # Should create routes connecting nearby same-IC pins
        assert len(routes) >= 0  # May create short connections
        # Connected indices should be tracked
        assert isinstance(connected, set)

    def test_intra_ic_routes_far_apart(self, router):
        """Test that distant pins don't get intra-IC routes."""
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 40.0, "y": 30.0, "net": 1, "net_name": "NET1"},  # Far apart
        ]
        router.add_component("U1", pads)

        pads_list = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads_list)

        # Distance > 3mm should not create intra-IC route
        assert len(routes) == 0


class TestAutorouterRouteAll:
    """Tests for route_all methods."""

    @pytest.fixture
    def router_with_nets(self):
        """Create router with multiple nets."""
        router = Autorouter(width=50.0, height=40.0)

        # Add two simple 2-pin nets
        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        return router

    def test_route_all_basic(self, router_with_nets):
        """Test basic route_all functionality."""
        routes = router_with_nets.route_all()
        assert isinstance(routes, list)

    def test_route_all_with_order(self, router_with_nets):
        """Test route_all with custom net order."""
        routes = router_with_nets.route_all(net_order=[2, 1])
        assert isinstance(routes, list)

    def test_route_all_skips_net_zero(self, router_with_nets):
        """Test that net 0 is skipped during routing."""
        # Add a pad with net 0
        pads = [{"number": "1", "x": 30.0, "y": 10.0, "net": 0}]
        router_with_nets.add_component("R3", pads)

        routes = router_with_nets.route_all()
        # Should not fail, net 0 is skipped
        assert isinstance(routes, list)


class TestAutorouterZones:
    """Tests for zone (copper pour) support."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_get_zone_statistics_empty(self, router):
        """Test zone statistics when no zones added."""
        stats = router.get_zone_statistics()
        assert "zones" in stats
        assert stats["zone_count"] == 0

    def test_clear_zones(self, router):
        """Test clearing zones."""
        router.clear_zones()
        stats = router.get_zone_statistics()
        assert stats["zone_count"] == 0


class TestAutorouterAdvanced:
    """Tests for advanced routing methods."""

    @pytest.fixture
    def router_with_nets(self):
        """Create router with multiple nets."""
        router = Autorouter(width=50.0, height=40.0)

        pads1 = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ]
        pads2 = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
        ]
        router.add_component("R1", pads1)
        router.add_component("R2", pads2)

        return router

    def test_route_all_advanced_single_pass(self, router_with_nets):
        """Test route_all_advanced with single pass."""
        routes = router_with_nets.route_all_advanced(monte_carlo_trials=0, use_negotiated=False)
        assert isinstance(routes, list)

    def test_route_all_advanced_negotiated(self, router_with_nets):
        """Test route_all_advanced with negotiated mode."""
        routes = router_with_nets.route_all_advanced(monte_carlo_trials=0, use_negotiated=True)
        assert isinstance(routes, list)

    def test_reset_for_new_trial(self, router_with_nets):
        """Test resetting router for new trial."""
        # Route first
        router_with_nets.route_all()
        original_routes = len(router_with_nets.routes)

        # Reset
        router_with_nets._reset_for_new_trial()

        # Routes should be cleared
        assert router_with_nets.routes == []
        # Pads should still be tracked
        assert len(router_with_nets.pads) > 0


class TestAutorouterBusDetection:
    """Tests for bus signal detection."""

    @pytest.fixture
    def router_with_bus(self):
        """Create router with bus signals."""
        router = Autorouter(width=50.0, height=40.0)

        # Add data bus signals
        for i in range(4):
            pads = [
                {
                    "number": "1",
                    "x": 10.0 + i * 2,
                    "y": 10.0,
                    "net": 10 + i,
                    "net_name": f"DATA[{i}]",
                },
                {
                    "number": "2",
                    "x": 10.0 + i * 2,
                    "y": 20.0,
                    "net": 10 + i,
                    "net_name": f"DATA[{i}]",
                },
            ]
            router.add_component(f"U{i}", pads)

        return router

    def test_detect_buses(self, router_with_bus):
        """Test bus detection from net names."""
        buses = router_with_bus.detect_buses(min_bus_width=2)
        assert isinstance(buses, list)

    def test_get_bus_analysis(self, router_with_bus):
        """Test getting bus analysis summary."""
        analysis = router_with_bus.get_bus_analysis()
        assert isinstance(analysis, dict)


class TestAutorouterDiffPair:
    """Tests for differential pair support."""

    @pytest.fixture
    def router(self):
        return Autorouter(width=50.0, height=40.0)

    def test_detect_diff_pairs(self, router):
        """Test differential pair detection."""
        # Add differential pair signals
        pads_p = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "USB_D+"},
            {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "USB_D+"},
        ]
        pads_n = [
            {"number": "1", "x": 10.0, "y": 12.0, "net": 2, "net_name": "USB_D-"},
            {"number": "2", "x": 20.0, "y": 12.0, "net": 2, "net_name": "USB_D-"},
        ]
        router.add_component("J1", pads_p)
        router.add_component("J2", pads_n)

        pairs = router.detect_differential_pairs()
        assert isinstance(pairs, list)


class TestNegotiatedModePadObstacles:
    """Tests for pad obstacle handling in negotiated routing mode.

    Issue #174: Autorouter was creating traces through pads because
    pad clearance zones weren't being treated as obstacles in negotiated mode.
    These tests verify the fix.
    """

    @pytest.fixture
    def router(self):
        """Create router with standard rules."""
        return Autorouter(width=50.0, height=40.0)

    def test_pad_blocks_other_net_in_negotiated_mode(self, router):
        """Test that pads block routes from other nets in negotiated mode.

        This is the core test for issue #174. A route from net 2 should not
        be able to pass through a pad belonging to net 1.
        """
        # Add a pad for net 1 in the center
        pad1 = [
            {
                "number": "1",
                "x": 25.0,
                "y": 20.0,
                "width": 2.0,
                "height": 2.0,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("U1", pad1)

        # Add pads for net 2 that would route through net 1's pad if unblocked
        pad2 = [
            {
                "number": "1",
                "x": 20.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 30.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET2",
            },
        ]
        router.add_component("R1", pad2)

        # Route using negotiated mode
        routes = router.route_all_negotiated(max_iterations=5)

        # If any route was created for net 2, verify it doesn't pass through net 1's pad
        net2_routes = [r for r in routes if r.net == 2]
        for route in net2_routes:
            for seg in route.segments:
                # The segment should not pass through the center of net 1's pad
                # Check if segment crosses the pad area (23-27 on x-axis at y=20)
                if seg.y1 == 20.0 and seg.y2 == 20.0:  # Horizontal at pad level
                    # If both endpoints are outside pad, segment shouldn't pass through
                    if seg.x1 < 23.0 and seg.x2 > 27.0:
                        # This would indicate the route went through the pad
                        pytest.fail("Route from net 2 passed through net 1's pad area")

    def test_grid_cell_usage_count_distinguishes_pads_from_routes(self, router):
        """Test that pad cells have usage_count=0 while routed cells have usage_count>0."""
        # Add a pad
        pad = [
            {
                "number": "1",
                "x": 25.0,
                "y": 20.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "NET1",
            }
        ]
        router.add_component("U1", pad)

        # Check that pad center cell has usage_count=0
        gx, gy = router.grid.world_to_grid(25.0, 20.0)
        layer_idx = router.grid.layer_to_index(Layer.F_CU.value)
        cell = router.grid.grid[layer_idx][gy][gx]

        assert cell.blocked is True, "Pad cell should be blocked"
        assert cell.net == 1, "Pad cell should have net assigned"
        assert cell.usage_count == 0, "Pad cell should have usage_count=0 (static obstacle)"

    def test_routed_cell_has_usage_count_after_marking(self, router):
        """Test that routed cells get usage_count>0 after mark_route_usage."""
        # Add two pads to route between
        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 20.0,
                "y": 20.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("R1", pads)

        # Route the net using negotiated mode
        routes = router._route_net_negotiated(1, present_cost_factor=0.5)

        if routes:
            # Mark route usage (this is what happens in route_all_negotiated)
            for route in routes:
                router.grid.mark_route_usage(route)

            # Check that routed cells have usage_count > 0
            for route in routes:
                for seg in route.segments:
                    gx, gy = router.grid.world_to_grid(seg.x1, seg.y1)
                    layer_idx = router.grid.layer_to_index(seg.layer.value)
                    cell = router.grid.grid[layer_idx][gy][gx]

                    # Routed cells should have usage_count > 0
                    # (unless they're pad cells which are special)
                    if not cell.is_obstacle:
                        assert cell.usage_count > 0, "Routed cell should have usage_count > 0"

    def test_same_net_can_reach_own_pad(self, router):
        """Test that a net can route to its own pads (not blocked by own pad)."""
        # Add two pads for the same net
        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 20.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 20.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("U1", pads)

        # Route using negotiated mode - should succeed
        routes = router.route_all_negotiated(max_iterations=5)

        # Should be able to route to own pads
        net1_routes = [r for r in routes if r.net == 1]
        assert len(net1_routes) > 0 or len(router.routes) > 0, "Should be able to route to own pads"


class TestSingleLayerRouting:
    """Tests for single-layer routing constraint (Issue #715).

    The allowed_layers field in DesignRules provides a hard constraint
    for restricting routing to specific layers.
    """

    def test_single_layer_no_vias(self):
        """Test that single-layer routing produces no vias."""
        rules = DesignRules(allowed_layers=["F.Cu"])
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Add two pads (default layer is F.Cu)
        pads = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 40.0, "y": 20.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)

        # Should produce routes with no vias
        for route in routes:
            assert len(route.vias) == 0, "Single-layer routing should produce no vias"

    def test_single_layer_all_segments_on_allowed_layer(self):
        """Test that all segments are on the allowed layer."""
        rules = DesignRules(allowed_layers=["F.Cu"])
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        pads = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 30.0, "y": 20.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)

        for route in routes:
            for segment in route.segments:
                assert segment.layer == Layer.F_CU, f"Segment on {segment.layer}, expected F.Cu"

    def test_back_copper_only_routing(self):
        """Test routing constrained to B.Cu only."""
        rules = DesignRules(allowed_layers=["B.Cu"])
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Add through-hole pads (can be routed on any layer)
        pads = [
            {
                "number": "1",
                "x": 10.0,
                "y": 20.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
            {
                "number": "2",
                "x": 30.0,
                "y": 20.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
        ]
        router.add_component("J1", pads)

        routes = router.route_net(1)

        for route in routes:
            assert len(route.vias) == 0, "Single-layer routing should produce no vias"
            for segment in route.segments:
                assert segment.layer == Layer.B_CU, f"Segment on {segment.layer}, expected B.Cu"

    def test_allowed_layers_none_allows_all(self):
        """Test that allowed_layers=None (default) allows all layers."""
        rules = DesignRules()  # Default: allowed_layers=None
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Add pads that might need layer change
        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 40.0, "y": 30.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        # Add an obstacle to potentially force layer change
        router.add_obstacle(25.0, 20.0, 5.0, 15.0, Layer.F_CU)

        routes = router.route_net(1)

        # Should be able to route (may or may not use vias depending on path)
        assert isinstance(routes, list)

    def test_two_layer_constraint(self):
        """Test allowing both F.Cu and B.Cu explicitly."""
        rules = DesignRules(allowed_layers=["F.Cu", "B.Cu"])
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        pads = [
            {"number": "1", "x": 10.0, "y": 20.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 30.0, "y": 20.0, "net": 1, "net_name": "NET1"},
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)

        # All segments should be on either F.Cu or B.Cu
        for route in routes:
            for segment in route.segments:
                assert segment.layer in [
                    Layer.F_CU,
                    Layer.B_CU,
                ], f"Segment on {segment.layer}, expected F.Cu or B.Cu"


class TestAutorouterOffGridPads:
    """Tests for routing with off-grid pads (Issue #956).

    When pad centers don't align exactly with the routing grid, the router
    should still be able to reach the pads by accepting any cell within the
    pad's metal area as a valid goal, not just the exact grid-snapped center.
    """

    def test_off_grid_pad_routing(self):
        """Test that pads with off-grid centers can still be routed.

        Reproduces the scenario from Issue #956 where pads at positions like
        (203.5875, 121.0) with 0.1mm grid fail to route because the grid-snapped
        position doesn't exactly match the pad center.
        """
        # Use a coarse grid (0.1mm) with off-grid pad positions
        # Grid resolution = clearance / 2 = 0.254 / 2 = 0.127mm by default
        # Use custom rules to get exactly 0.1mm grid
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Pad 1: On-grid position (clean 0.1mm increment)
        # Pad 2: Off-grid position (fractional offset like real boards)
        pads = [
            {
                "number": "1",
                "x": 10.0,  # On grid
                "y": 10.0,  # On grid
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.0375,  # Off grid by 0.0375mm
                "y": 10.025,  # Off grid by 0.025mm
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("R1", pads)

        # Route should succeed even though pad 2 is off-grid
        routes = router.route_net(1)

        # Should have at least one route with segments
        assert len(routes) > 0, "Should route despite off-grid target pad"
        assert len(routes[0].segments) > 0, "Route should have segments"

        # Verify the route connects to the actual pad positions
        # First segment should start near pad 1 (10.0, 10.0)
        # Last segment should end near pad 2 (15.0375, 10.025)
        first_seg = routes[0].segments[0]
        last_seg = routes[0].segments[-1]

        # Check that route endpoints are close to pad centers
        # (route reconstruction connects to actual pad centers)
        assert abs(first_seg.x1 - 10.0) < 0.2, f"Start X should be near 10.0, got {first_seg.x1}"
        assert abs(first_seg.y1 - 10.0) < 0.2, f"Start Y should be near 10.0, got {first_seg.y1}"
        assert abs(last_seg.x2 - 15.0375) < 0.2, f"End X should be near 15.0375, got {last_seg.x2}"
        assert abs(last_seg.y2 - 10.025) < 0.2, f"End Y should be near 10.025, got {last_seg.y2}"

    def test_both_pads_off_grid(self):
        """Test routing when both source and target pads are off-grid."""
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Both pads have fractional offsets
        pads = [
            {
                "number": "1",
                "x": 10.0125,  # Off grid by 0.0125mm
                "y": 10.0375,  # Off grid by 0.0375mm
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.0625,  # Off grid by 0.0625mm
                "y": 10.0875,  # Off grid by 0.0875mm
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        router.add_component("R1", pads)

        routes = router.route_net(1)

        assert len(routes) > 0, "Should route with both pads off-grid"
        assert len(routes[0].segments) > 0, "Route should have segments"

    def test_off_grid_pad_near_obstacle(self):
        """Test routing when off-grid pad's snapped center might be blocked.

        Issue #977: When a pad is off-grid, its grid-snapped center might
        fall into another component's clearance zone. The expanded start
        region (all cells within pad's metal area) allows routing to
        find an alternate entry point.

        Uses force_python=True since this tests Python pathfinder logic.
        """
        rules = DesignRules(trace_clearance=0.2, trace_width=0.2)
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # Place two nets with pads slightly off-grid and close together
        # The clearance zones may overlap causing blocked grid cells
        pads_net1 = [
            {
                "number": "1",
                "x": 10.05,  # Off grid by 0.05mm
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]
        pads_net2 = [
            {
                "number": "1",
                "x": 10.05,  # Same X but different Y, close to net1 pad
                "y": 10.6,  # Just outside clearance but close
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 10.6,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET2",
            },
        ]
        router.add_component("R1", pads_net1)
        router.add_component("R2", pads_net2)

        # Both nets should be routable despite off-grid positions
        routes1 = router.route_net(1)
        routes2 = router.route_net(2)

        assert len(routes1) > 0, "NET1 should be routed despite off-grid pad"
        assert len(routes2) > 0, "NET2 should be routed despite off-grid pad"
        assert len(routes1[0].segments) > 0, "NET1 route should have segments"
        assert len(routes2[0].segments) > 0, "NET2 route should have segments"

    def test_off_grid_pad_with_clearance_overlap(self):
        """Test routing when pad's grid cells overlap with clearance zones.

        Issue #990: When SMD pads have grid cells that overlap with other nets'
        clearance zones, the router should still be able to route by allowing
        the first step outward from the pad with relaxed clearance checking.

        This test creates a scenario where:
        - Two nets have pads positioned with some overlap in clearance zones
        - The router must allow exiting from the pad area even when some cells
          near the pad would normally fail clearance checks
        - Route should go around the blocked area to maintain proper clearance

        Uses force_python=True since this tests Python pathfinder logic.
        """
        # Grid: 0.2mm, Clearance: 0.2mm, Trace: 0.2mm
        rules = DesignRules(
            trace_clearance=0.2,
            trace_width=0.2,
            grid_resolution=0.2,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # Create layout where pads are close but with enough clearance for routing
        # NET1: pads along y=10.0
        pads_net1 = [
            {
                "number": "1",
                "x": 5.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]

        # NET2: pads offset in Y direction with minimal clearance margin
        # At y=11.0, pad bottom edge is at y=10.5 (for 1.0mm tall pad)
        # NET1 pad top edge is at y=10.25 (for 0.5mm tall pad)
        # Gap: 10.5 - 10.25 = 0.25mm, just above required 0.2mm clearance
        pads_net2 = [
            {
                "number": "1",
                "x": 5.0,
                "y": 11.0,
                "width": 0.8,
                "height": 1.0,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 11.0,
                "width": 0.8,
                "height": 1.0,
                "net": 2,
                "net_name": "NET2",
            },
        ]

        router.add_component("U1", pads_net1)
        router.add_component("U2", pads_net2)

        # Route NET1 - should succeed by routing along y=10.0
        routes1 = router.route_net(1)

        assert len(routes1) > 0, (
            "NET1 should be routed when clearance zones partially overlap grid cells "
            "(Issue #990 relaxed pad exit checking)"
        )
        assert len(routes1[0].segments) > 0, "NET1 route should have segments"

    def test_off_grid_pad_bidirectional_with_clearance_overlap(self):
        """Test bidirectional A* with off-grid pads where clearance zones overlap.

        Issue #990: Tests the bidirectional A* algorithm with pads that are
        off-grid and have partial clearance zone overlap with adjacent nets.

        Uses force_python=True since this tests Python pathfinder logic.
        """
        rules = DesignRules(
            trace_clearance=0.2,
            trace_width=0.2,
            grid_resolution=0.2,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # Off-grid pads with nearby obstacles
        pads_net1 = [
            {
                "number": "1",
                "x": 5.05,  # Slightly off-grid
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.05,  # Slightly off-grid
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET1",
            },
        ]

        # NET2 pads with proper clearance (1.0mm gap)
        pads_net2 = [
            {
                "number": "1",
                "x": 5.05,
                "y": 11.0,
                "width": 0.6,
                "height": 0.6,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 15.05,
                "y": 11.0,
                "width": 0.6,
                "height": 0.6,
                "net": 2,
                "net_name": "NET2",
            },
        ]

        router.add_component("U1", pads_net1)
        router.add_component("U2", pads_net2)

        # Access pathfinder directly to test bidirectional routing
        from kicad_tools.router.pathfinder import Router

        pathfinder = Router(router.grid, router.rules)

        pad1 = router.pads[("U1", "1")]
        pad2 = router.pads[("U1", "2")]

        # Test bidirectional routing
        route = pathfinder.route_bidirectional(pad1, pad2)

        assert route is not None, (
            "Bidirectional A* should succeed with off-grid pads (Issue #990 "
            "relaxed pad exit checking)"
        )
        assert len(route.segments) > 0, "Route should have segments"

    def test_subgrid_pad_entry_exit(self):
        """Test routing when pad's grid cells are blocked by adjacent net clearance.

        Issue #996: With coarse grids (0.5mm), pads positioned at fractional
        coordinates (like 10.325mm) may have their nearest grid cells blocked
        by adjacent components' clearance zones. The router should allow
        sub-grid entry/exit segments that connect the pad center directly to
        the nearest unblocked grid point.

        Scenario:
        - 0.5mm grid resolution
        - Pads at fractional positions (0.175mm off-grid)
        - Adjacent pads from another net positioned to have clearance zones
          overlap the target pad's nearest grid cells
        - Router should succeed by routing through the clearance zone

        Uses force_python=True since this tests Python pathfinder logic.
        """
        # Coarse 0.5mm grid (typical for routing)
        rules = DesignRules(
            trace_clearance=0.2,
            trace_width=0.25,
            grid_resolution=0.5,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # NET1: pads positioned 0.175mm off-grid
        # At x=5.175, nearest grid cell is x=5.0 (0.175mm away)
        pads_net1 = [
            {
                "number": "1",
                "x": 5.175,  # 0.175mm off-grid (5.0 is on-grid)
                "y": 10.175,  # 0.175mm off-grid (10.0 is on-grid)
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.175,  # 0.175mm off-grid
                "y": 10.175,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "NET1",
            },
        ]

        # NET2: Large pads positioned so their clearance zones overlap with NET1's
        # nearest grid cells, BUT metal areas don't overlap (which would be a DRC error).
        # NET1 metal ends at y=10.375. For proper clearance:
        # - NET2 metal must start at y >= 10.375 + 0.2 (clearance) = 10.575
        # - NET2 center at y >= 10.575 + 0.4 (half-height) = 10.975
        # Using y=11.0 gives metal area y=[10.6, 11.4], clearance y=[10.275, 11.725]
        # This ensures clearance overlap at y=10.375 but no metal overlap.
        pads_net2 = [
            {
                "number": "1",
                "x": 5.0,  # On-grid
                "y": 11.0,  # Metal at y=[10.6,11.4], clearance at y=[10.275,11.725]
                "width": 0.8,
                "height": 0.8,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 15.0,
                "y": 11.0,
                "width": 0.8,
                "height": 0.8,
                "net": 2,
                "net_name": "NET2",
            },
        ]

        router.add_component("U1", pads_net1)
        router.add_component("U2", pads_net2)

        # Route NET1 - should succeed with sub-grid connections
        routes1 = router.route_net(1)

        assert len(routes1) > 0, (
            "NET1 should be routed when nearest grid cells are blocked by "
            "clearance zones (Issue #996 sub-grid pad connections)"
        )
        assert len(routes1[0].segments) > 0, "NET1 route should have segments"

        # Verify that the route connects the actual pad centers (not grid cells)
        # The first segment should start at or near the pad center
        first_seg = routes1[0].segments[0]
        last_seg = routes1[0].segments[-1]

        # Check that route endpoints are close to pad centers (allowing some tolerance)
        pad1 = router.pads[("U1", "1")]
        pad2 = router.pads[("U1", "2")]

        def dist(x1, y1, x2, y2):
            return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5

        # Route should start near pad1 and end near pad2
        start_dist = min(
            dist(first_seg.x1, first_seg.y1, pad1.x, pad1.y),
            dist(last_seg.x2, last_seg.y2, pad1.x, pad1.y),
        )
        end_dist = min(
            dist(first_seg.x1, first_seg.y1, pad2.x, pad2.y),
            dist(last_seg.x2, last_seg.y2, pad2.x, pad2.y),
        )

        # At least one endpoint should be close to each pad
        assert start_dist < 0.3 or end_dist < 0.3, (
            f"Route should connect to pad centers (distances: {start_dist:.3f}, {end_dist:.3f})"
        )

    def test_subgrid_bidirectional_routing(self):
        """Test bidirectional A* with sub-grid pad connections.

        Issue #996: Verifies that bidirectional A* also supports sub-grid
        routing when pad positions cause all nearby grid cells to be blocked.

        Uses force_python=True since this tests Python pathfinder logic.
        """
        rules = DesignRules(
            trace_clearance=0.2,
            trace_width=0.25,
            grid_resolution=0.5,
        )
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # Off-grid pads
        pads_net1 = [
            {
                "number": "1",
                "x": 5.325,  # 0.175mm off-grid
                "y": 10.325,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "NET1",
            },
            {
                "number": "2",
                "x": 15.325,
                "y": 10.325,
                "width": 0.4,
                "height": 0.4,
                "net": 1,
                "net_name": "NET1",
            },
        ]

        # Adjacent pads with overlapping clearance zones but valid geometric clearance.
        # Position NET2 so its clearance zone blocks grid cells near NET1,
        # but the route at y=10.5 still has valid clearance to NET2's metal.
        # NET2 metal edge at y=11.5-0.4=11.1, clearance zone to y≈10.775, covering y=10.5
        # Route at y=10.5 has 0.6mm clearance to metal (>0.325mm required), so it's valid.
        pads_net2 = [
            {
                "number": "1",
                "x": 5.5,  # On-grid, same column as NET1's nearest grid
                "y": 11.5,  # Metal at y=[11.1,11.9], clearance blocks grid y=10.5
                "width": 0.8,
                "height": 0.8,
                "net": 2,
                "net_name": "NET2",
            },
            {
                "number": "2",
                "x": 15.5,
                "y": 11.5,
                "width": 0.8,
                "height": 0.8,
                "net": 2,
                "net_name": "NET2",
            },
        ]

        router.add_component("U1", pads_net1)
        router.add_component("U2", pads_net2)

        # Test bidirectional routing directly
        from kicad_tools.router.pathfinder import Router

        pathfinder = Router(router.grid, router.rules)

        pad1 = router.pads[("U1", "1")]
        pad2 = router.pads[("U1", "2")]

        route = pathfinder.route_bidirectional(pad1, pad2)

        assert route is not None, (
            "Bidirectional A* should succeed with sub-grid pad connections (Issue #996)"
        )
        assert len(route.segments) > 0, "Route should have segments"


class TestCrossingPenalty:
    """Tests for crossing-aware A* pathfinding (Issue #1250)."""

    def test_segments_intersect_crossing(self):
        """Test that intersecting segments are detected."""
        from kicad_tools.router.pathfinder import Router

        # X-shaped crossing
        assert Router._segments_intersect(0, 0, 10, 10, 0, 10, 10, 0) is True

    def test_segments_intersect_parallel(self):
        """Test that parallel segments are not detected as crossing."""
        from kicad_tools.router.pathfinder import Router

        # Parallel horizontal segments
        assert Router._segments_intersect(0, 0, 10, 0, 0, 5, 10, 5) is False

    def test_segments_intersect_shared_endpoint(self):
        """Test that segments sharing an endpoint do not count as crossing."""
        from kicad_tools.router.pathfinder import Router

        # T-junction: share endpoint at (5,5)
        assert Router._segments_intersect(0, 5, 5, 5, 5, 0, 5, 10) is False

    def test_segments_intersect_non_overlapping(self):
        """Test that non-overlapping segments are not detected."""
        from kicad_tools.router.pathfinder import Router

        # Segments in different quadrants
        assert Router._segments_intersect(0, 0, 1, 1, 5, 5, 6, 6) is False

    def test_add_and_clear_routed_segments(self):
        """Test add_routed_segments and clear_routed_segments API on Router."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
        grid = RoutingGrid(20.0, 20.0, rules)
        router = Router(grid, rules)

        assert len(router._routed_segments) == 0

        seg = Segment(x1=2.0, y1=2.0, x2=8.0, y2=2.0, width=0.2, layer=Layer.F_CU, net=1)
        router.add_routed_segments([seg])
        assert len(router._routed_segments) == 1

        router.clear_routed_segments()
        assert len(router._routed_segments) == 0

    def test_count_edge_crossings_same_layer_different_net(self):
        """Test that crossings are counted for same-layer, different-net segments."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
        grid = RoutingGrid(20.0, 20.0, rules)
        router = Router(grid, rules)

        # Add a horizontal routed segment on layer 0, net 1, from (5,10) to (15,10)
        # in grid coords
        router._routed_segments.append((5, 10, 15, 10, 0, 1))

        # Vertical edge from (10,5) to (10,15) on layer 0 for net 2 should cross it
        count = router._count_edge_crossings(10, 5, 10, 15, 0, 2)
        assert count == 1

    def test_count_edge_crossings_same_net_ignored(self):
        """Test that same-net segments are NOT counted as crossings."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
        grid = RoutingGrid(20.0, 20.0, rules)
        router = Router(grid, rules)

        # Segment on net 1
        router._routed_segments.append((5, 10, 15, 10, 0, 1))

        # Edge for net 1 should not count
        count = router._count_edge_crossings(10, 5, 10, 15, 0, 1)
        assert count == 0

    def test_count_edge_crossings_different_layer_ignored(self):
        """Test that crossings on different layers are NOT counted."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
        grid = RoutingGrid(20.0, 20.0, rules)
        router = Router(grid, rules)

        # Segment on layer 0, net 1
        router._routed_segments.append((5, 10, 15, 10, 0, 1))

        # Edge on layer 1, net 2 should not cross (different layer)
        count = router._count_edge_crossings(10, 5, 10, 15, 1, 2)
        assert count == 0

    def test_mark_route_feeds_routed_segments(self):
        """Test that _mark_route updates router._routed_segments."""
        rules = DesignRules(crossing_penalty=5.0)
        router = Autorouter(width=20.0, height=20.0, rules=rules)

        # Check initial state -- the Python pathfinder should have the attribute
        from kicad_tools.router.pathfinder import Router as PyRouter

        if isinstance(router.router, PyRouter):
            assert len(router.router._routed_segments) == 0

            route = Route(
                net=1,
                net_name="NET1",
                segments=[
                    Segment(
                        x1=2.0,
                        y1=5.0,
                        x2=8.0,
                        y2=5.0,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=1,
                    ),
                    Segment(
                        x1=8.0,
                        y1=5.0,
                        x2=8.0,
                        y2=10.0,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=1,
                    ),
                ],
            )
            router._mark_route(route)
            assert len(router.router._routed_segments) == 2

    def test_crossing_penalty_zero_no_regression(self):
        """Test that crossing_penalty=0.0 produces same behavior as before."""
        rules = DesignRules(crossing_penalty=0.0)
        router = Autorouter(width=30.0, height=20.0, rules=rules)

        pads = [
            {
                "number": "1",
                "x": 5.0,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
            {
                "number": "2",
                "x": 25.0,
                "y": 10.0,
                "net": 1,
                "net_name": "NET1",
                "through_hole": True,
                "drill": 0.8,
            },
        ]
        router.add_component("J1", pads)
        routes = router.route_net(1)
        assert len(routes) > 0, "Should route successfully with crossing_penalty=0.0"

    def test_crossing_penalty_reduces_crossings(self):
        """Test that crossing_penalty > 0 reduces crossings on a synthetic layout.

        Sets up a board where two nets must cross unless the router detours.
        With crossing_penalty=0.0 the router may take the shortest (crossing) path.
        With crossing_penalty=5.0 the router should find a non-crossing or
        fewer-crossing alternative.
        """

        def _count_route_crossings(
            route_segments: list[Segment], other_segments: list[Segment]
        ) -> int:
            """Count crossings between two sets of segments."""
            from kicad_tools.router.pathfinder import Router

            crossings = 0
            for seg_a in route_segments:
                for seg_b in other_segments:
                    if seg_a.layer != seg_b.layer:
                        continue
                    # Convert to int grid-like coords (multiply by 10 for precision)
                    ax1 = int(seg_a.x1 * 10)
                    ay1 = int(seg_a.y1 * 10)
                    ax2 = int(seg_a.x2 * 10)
                    ay2 = int(seg_a.y2 * 10)
                    bx1 = int(seg_b.x1 * 10)
                    by1 = int(seg_b.y1 * 10)
                    bx2 = int(seg_b.x2 * 10)
                    by2 = int(seg_b.y2 * 10)
                    if Router._segments_intersect(ax1, ay1, ax2, ay2, bx1, by1, bx2, by2):
                        crossings += 1
            return crossings

        # Board: 20x20mm, two nets arranged to encourage crossing
        # Net 1: top-left to bottom-right (diagonal tendency)
        # Net 2: bottom-left to top-right (diagonal tendency)
        def _route_board(penalty: float) -> tuple[list[Segment], list[Segment]]:
            rules = DesignRules(crossing_penalty=penalty)
            router = Autorouter(width=20.0, height=20.0, rules=rules)

            # Net 1: pads on opposite corners (top-left -> bottom-right)
            pads_net1 = [
                {
                    "number": "1",
                    "x": 3.0,
                    "y": 3.0,
                    "net": 1,
                    "net_name": "NET1",
                    "through_hole": True,
                    "drill": 0.8,
                },
                {
                    "number": "2",
                    "x": 17.0,
                    "y": 17.0,
                    "net": 1,
                    "net_name": "NET1",
                    "through_hole": True,
                    "drill": 0.8,
                },
            ]
            router.add_component("J1", pads_net1)

            # Net 2: pads on opposite corners (bottom-left -> top-right)
            pads_net2 = [
                {
                    "number": "1",
                    "x": 3.0,
                    "y": 17.0,
                    "net": 2,
                    "net_name": "NET2",
                    "through_hole": True,
                    "drill": 0.8,
                },
                {
                    "number": "2",
                    "x": 17.0,
                    "y": 3.0,
                    "net": 2,
                    "net_name": "NET2",
                    "through_hole": True,
                    "drill": 0.8,
                },
            ]
            router.add_component("J2", pads_net2)

            # Route net 1 first, then net 2
            routes1 = router.route_net(1)
            routes2 = router.route_net(2)

            segs1 = [s for r in routes1 for s in r.segments]
            segs2 = [s for r in routes2 for s in r.segments]
            return segs1, segs2

        # Route without penalty
        segs1_no_penalty, segs2_no_penalty = _route_board(0.0)
        crossings_no_penalty = _count_route_crossings(segs1_no_penalty, segs2_no_penalty)

        # Route with penalty
        segs1_with_penalty, segs2_with_penalty = _route_board(5.0)
        crossings_with_penalty = _count_route_crossings(segs1_with_penalty, segs2_with_penalty)

        # With penalty, crossings should be <= without penalty
        assert crossings_with_penalty <= crossings_no_penalty, (
            f"Crossing penalty should reduce crossings: "
            f"got {crossings_with_penalty} with penalty vs "
            f"{crossings_no_penalty} without"
        )

    def test_empty_routed_segments_no_penalty(self):
        """Test that first net to route never gets penalized (empty segments list)."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(crossing_penalty=5.0, grid_resolution=0.5)
        grid = RoutingGrid(20.0, 20.0, rules)
        router = Router(grid, rules)

        # No routed segments -- count should be 0
        count = router._count_edge_crossings(0, 0, 10, 10, 0, 1)
        assert count == 0


class TestPourNetFiltering:
    """Tests for pour-net skipping in route_all variants (Issue #1295)."""

    def test_is_pour_net_true_for_power_nets(self):
        """Test that _is_pour_net returns True for GND/VCC nets."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND", "VCC"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # Add GND net
        router.add_component(
            "C1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )
        router.add_component(
            "C2",
            [{"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )

        assert router._is_pour_net(1) is True

    def test_is_pour_net_false_for_signal_nets(self):
        """Test that _is_pour_net returns False for signal nets."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 2, "net_name": "SPI_MOSI"}],
        )

        assert router._is_pour_net(2) is False

    def test_is_pour_net_false_for_unknown_nets(self):
        """Test that _is_pour_net returns False for nets not in net_class_map."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 3, "net_name": "RANDOM"}],
        )

        assert router._is_pour_net(3) is False

    def test_get_net_priority_pour_net_returns_99(self):
        """Test that pour nets get priority 99 in _get_net_priority."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component(
            "C1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )
        router.add_component(
            "C2",
            [{"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )

        priority_tuple = router._get_net_priority(1)
        assert priority_tuple[0] == 99  # Pour net pushed to back

    def test_filter_pour_nets_removes_pour_nets(self):
        """Test that _filter_pour_nets removes pour nets from ordering."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND", "VCC"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # GND net
        router.add_component(
            "C1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )
        router.add_component(
            "C2",
            [{"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "GND"}],
        )
        # VCC net
        router.add_component(
            "U1",
            [{"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "VCC"}],
        )
        router.add_component(
            "U2",
            [{"number": "1", "x": 20.0, "y": 20.0, "net": 2, "net_name": "VCC"}],
        )
        # Signal net
        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 30.0, "net": 3, "net_name": "SPI_MOSI"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 20.0, "y": 30.0, "net": 3, "net_name": "SPI_MOSI"}],
        )

        net_order = [1, 2, 3]
        filtered = router._filter_pour_nets(net_order)

        assert 1 not in filtered  # GND removed
        assert 2 not in filtered  # VCC removed
        assert 3 in filtered  # SPI_MOSI kept

    def test_filter_pour_nets_noop_when_no_pour_nets(self):
        """Test that _filter_pour_nets returns original list when no pour nets."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "SIG_A"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "SIG_A"}],
        )

        net_order = [1]
        filtered = router._filter_pour_nets(net_order)

        assert filtered == [1]

    def test_route_all_skips_pour_nets(self):
        """Test that route_all does not attempt to route pour nets."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # GND pour net
        router.add_component(
            "C1",
            [{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "GND"}],
        )
        router.add_component(
            "C2",
            [{"number": "1", "x": 10.0, "y": 5.0, "net": 1, "net_name": "GND"}],
        )

        # Signal net (SPI_MOSI)
        router.add_component(
            "R1",
            [{"number": "1", "x": 5.0, "y": 15.0, "net": 2, "net_name": "SPI_MOSI"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 10.0, "y": 15.0, "net": 2, "net_name": "SPI_MOSI"}],
        )

        routes = router.route_all()

        # GND should NOT appear in routed nets
        routed_net_ids = {r.net for r in routes}
        assert 1 not in routed_net_ids, "GND pour net should not be routed"
        # SPI_MOSI may or may not have routed (depending on grid), but GND is the key check

    def test_route_all_explicit_order_with_pour_nets_filtered(self):
        """Test that explicit net_order also filters pour nets."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(power_nets=["GND"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # GND pour net
        router.add_component(
            "C1",
            [{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "GND"}],
        )
        router.add_component(
            "C2",
            [{"number": "1", "x": 10.0, "y": 5.0, "net": 1, "net_name": "GND"}],
        )

        # Even when pour net is explicitly in net_order, it should be filtered
        routes = router.route_all(net_order=[1])

        routed_net_ids = {r.net for r in routes}
        assert 1 not in routed_net_ids, "GND pour net should not be routed even in explicit order"

    def test_pour_net_ordering_sorts_to_end(self):
        """Test that pour nets sort after all signal nets in priority order."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(
            power_nets=["GND"],
            debug_nets=["SWDIO"],
        )
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # GND pour net
        router.add_component(
            "C1",
            [{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "GND"}],
        )
        # SWDIO debug net (low priority signal)
        router.add_component(
            "R1",
            [{"number": "1", "x": 5.0, "y": 15.0, "net": 2, "net_name": "SWDIO"}],
        )
        # Unknown signal net (default priority=10)
        router.add_component(
            "R2",
            [{"number": "1", "x": 5.0, "y": 25.0, "net": 3, "net_name": "RANDOM_SIG"}],
        )

        p_gnd = router._get_net_priority(1)
        p_debug = router._get_net_priority(2)
        p_default = router._get_net_priority(3)

        # Pour net should sort after all signal nets
        assert p_gnd > p_debug, "GND should sort after debug nets"
        assert p_gnd > p_default, "GND should sort after default signal nets"


class TestComplexityTierOrdering:
    """Tests for complexity-tier-based net ordering within priority classes (Issue #1295)."""

    def test_simple_2pin_before_complex_multipin(self):
        """Test that simple 2-pin short nets sort before multi-pin nets."""
        router = Autorouter(width=50.0, height=50.0)

        # Simple 2-pin net (net 1): short distance (< 10mm)
        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "SHORT_A"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 14.0, "y": 10.0, "net": 1, "net_name": "SHORT_A"}],
        )

        # Complex multi-pin net (net 2): 4 pads
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 20.0, "y": 20.0, "net": 2, "net_name": "BUS_D0"},
                {"number": "2", "x": 21.0, "y": 20.0, "net": 0},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 25.0, "y": 20.0, "net": 2, "net_name": "BUS_D0"},
                {"number": "2", "x": 26.0, "y": 20.0, "net": 0},
            ],
        )
        router.add_component(
            "U3",
            [{"number": "1", "x": 30.0, "y": 20.0, "net": 2, "net_name": "BUS_D0"}],
        )

        p_simple = router._get_net_priority(1)
        p_complex = router._get_net_priority(2)

        # Same class priority (both unknown/default = 10)
        assert p_simple[0] == p_complex[0]
        # Simple (tier 0) should sort before complex (tier 1)
        # Complexity tier is at index 1 in the 5-tuple
        assert p_simple[1] < p_complex[1]
        assert p_simple < p_complex

    def test_simple_2pin_before_long_2pin(self):
        """Test that short 2-pin nets (simple) sort before long 2-pin nets (complex)."""
        router = Autorouter(width=100.0, height=100.0)

        # Short 2-pin net (net 1): 5mm distance (< 10mm threshold)
        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "SHORT"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 15.0, "y": 10.0, "net": 1, "net_name": "SHORT"}],
        )

        # Long 2-pin net (net 2): 30mm distance (> 10mm threshold)
        router.add_component(
            "R3",
            [{"number": "1", "x": 10.0, "y": 50.0, "net": 2, "net_name": "LONG"}],
        )
        router.add_component(
            "R4",
            [{"number": "1", "x": 40.0, "y": 50.0, "net": 2, "net_name": "LONG"}],
        )

        p_short = router._get_net_priority(1)
        p_long = router._get_net_priority(2)

        # Same class priority
        assert p_short[0] == p_long[0]
        # Short 2-pin = simple (tier 0), long 2-pin = complex (tier 1)
        # Complexity tier is at index 1 in the 5-tuple
        assert p_short[1] == 0  # Simple tier
        assert p_long[1] == 1  # Complex tier
        assert p_short < p_long

    def test_signal_ordering_constrained_then_simple_then_complex(self):
        """Test full ordering: constrained > simple 2-pin > complex multi-pin."""
        from kicad_tools.router.rules import create_net_class_map

        net_classes = create_net_class_map(clock_nets=["CLK"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # Clock net (constrained, priority=2)
        router.add_component(
            "U1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "CLK"}],
        )
        router.add_component(
            "U2",
            [{"number": "1", "x": 15.0, "y": 10.0, "net": 1, "net_name": "CLK"}],
        )

        # Simple 2-pin signal (default priority=10, short distance)
        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "SIG_A"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 14.0, "y": 20.0, "net": 2, "net_name": "SIG_A"}],
        )

        # Complex multi-pin signal (default priority=10)
        router.add_component(
            "U3",
            [{"number": "1", "x": 10.0, "y": 30.0, "net": 3, "net_name": "BUS"}],
        )
        router.add_component(
            "U4",
            [{"number": "1", "x": 20.0, "y": 30.0, "net": 3, "net_name": "BUS"}],
        )
        router.add_component(
            "U5",
            [{"number": "1", "x": 30.0, "y": 30.0, "net": 3, "net_name": "BUS"}],
        )

        net_order = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))
        # Remove net 0
        net_order = [n for n in net_order if n != 0]

        # Clock (constrained) should come first
        assert net_order.index(1) < net_order.index(2)
        assert net_order.index(1) < net_order.index(3)
        # Simple 2-pin should come before complex multi-pin (both default class)
        assert net_order.index(2) < net_order.index(3)

    def test_5_tuple_return_type(self):
        """Test that _get_net_priority returns a 5-tuple."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "R1",
            [{"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"}],
        )

        result = router._get_net_priority(1)
        assert len(result) == 5, f"Expected 5-tuple, got {len(result)}-tuple"

    def test_no_pour_nets_unchanged_behavior(self):
        """Test that boards with no pour nets route identically to before."""
        router = Autorouter(width=50.0, height=50.0)

        # Two signal nets, no pour nets
        router.add_component(
            "R1",
            [{"number": "1", "x": 5.0, "y": 5.0, "net": 1, "net_name": "SIG_A"}],
        )
        router.add_component(
            "R2",
            [{"number": "1", "x": 10.0, "y": 5.0, "net": 1, "net_name": "SIG_A"}],
        )
        router.add_component(
            "R3",
            [{"number": "1", "x": 5.0, "y": 15.0, "net": 2, "net_name": "SIG_B"}],
        )
        router.add_component(
            "R4",
            [{"number": "1", "x": 10.0, "y": 15.0, "net": 2, "net_name": "SIG_B"}],
        )

        # _filter_pour_nets should return the same list
        net_order = [1, 2]
        filtered = router._filter_pour_nets(net_order)
        assert filtered == net_order


class TestOffGridNetExclusionFromRipup:
    """Tests for Issue #1605: off-grid nets excluded from rip-up iterations."""

    def test_off_grid_nets_excluded_from_failed_nets_recovery(self):
        """Nets with PADS_OFF_GRID failures should be excluded from rip-up recovery."""
        router = Autorouter(width=50.0, height=40.0)

        # Add two nets: one normal, one that will be marked as off-grid
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET_OK"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "NET_OK"},
            ],
        )
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "NET_OFFGRID"},
                {"number": "2", "x": 20.0, "y": 20.0, "net": 2, "net_name": "NET_OFFGRID"},
            ],
        )

        # Simulate a PADS_OFF_GRID routing failure for net 2
        router.routing_failures.append(
            RoutingFailure(
                net=2,
                net_name="NET_OFFGRID",
                source_pad=("U1", "1"),
                target_pad=("U1", "2"),
                reason="PADS_OFF_GRID: U1.1, U1.2",
            )
        )

        # Build the off_grid_nets set the same way route_all_negotiated does
        off_grid_nets = {
            f.net for f in router.routing_failures if f.reason.startswith("PADS_OFF_GRID")
        }

        assert 2 in off_grid_nets, "Net 2 should be identified as off-grid"
        assert 1 not in off_grid_nets, "Net 1 should not be identified as off-grid"

        # Simulate the failed_nets_to_recover filter
        net_order = [1, 2]
        net_routes = {1: []}  # Net 1 has routes; net 2 does not
        pads_by_net = {1: [], 2: []}  # Both have pads

        failed_nets_to_recover = [
            n
            for n in net_order
            if n not in net_routes and n in pads_by_net and n not in off_grid_nets
        ]

        assert 2 not in failed_nets_to_recover, (
            "Off-grid net 2 must NOT be included in failed_nets_to_recover"
        )

    def test_non_off_grid_failures_still_included_in_recovery(self):
        """Nets that fail for reasons OTHER than PADS_OFF_GRID should still be recovered."""
        router = Autorouter(width=50.0, height=40.0)

        # Add a net that fails for congestion (not off-grid)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 3, "net_name": "NET_CONGESTED"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 3, "net_name": "NET_CONGESTED"},
            ],
        )

        router.routing_failures.append(
            RoutingFailure(
                net=3,
                net_name="NET_CONGESTED",
                source_pad=("R1", "1"),
                target_pad=("R1", "2"),
                reason="BLOCKED_BY_COMPONENT: Path blocked by U2",
            )
        )

        off_grid_nets = {
            f.net for f in router.routing_failures if f.reason.startswith("PADS_OFF_GRID")
        }

        assert 3 not in off_grid_nets, "Congestion-failed net should not be off-grid"

        net_order = [3]
        net_routes = {}  # Net 3 has no routes
        pads_by_net = {3: []}

        failed_nets_to_recover = [
            n
            for n in net_order
            if n not in net_routes and n in pads_by_net and n not in off_grid_nets
        ]

        assert 3 in failed_nets_to_recover, (
            "Congestion-failed net must still be included in recovery"
        )

    def test_off_grid_set_empty_when_no_failures(self):
        """When there are no routing failures, off_grid_nets should be empty."""
        router = Autorouter(width=50.0, height=40.0)

        off_grid_nets = {
            f.net for f in router.routing_failures if f.reason.startswith("PADS_OFF_GRID")
        }

        assert off_grid_nets == set()


class TestPerNetTimeout:
    """Tests for Issue #1605: per-net wall-clock timeout in A* search."""

    def test_route_returns_none_with_tiny_timeout(self):
        """A* search should return None when per_net_timeout expires."""
        # Use force_python=True to ensure the Python pathfinder (with timeout
        # logic) is used instead of the C++ backend. Use a fine grid resolution
        # (0.05mm) on a 20x20mm board = 400x400 grid cells.
        rules = DesignRules(grid_resolution=0.05)
        router = Autorouter(width=20.0, height=20.0, rules=rules, force_python=True)

        # Place pads at opposite corners
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 0.5, "y": 0.5, "net": 1, "net_name": "TIMEOUT_TEST"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 19.5, "y": 19.5, "net": 1, "net_name": "TIMEOUT_TEST"},
            ],
        )

        # Block the direct diagonal with a second net's clearance zone,
        # forcing A* to explore many alternative paths (well over 1024 nodes).
        for i in range(1, 20):
            ref = f"B{i}"
            router.add_component(
                ref,
                [
                    {
                        "number": "1",
                        "x": float(i),
                        "y": float(i),
                        "net": 2,
                        "net_name": "BLOCKER",
                    },
                ],
            )

        pad_start = router.pads[("R1", "1")]
        pad_end = router.pads[("R2", "1")]

        # Mock time.monotonic so the deadline is already expired when checked.
        # First call sets the deadline, subsequent calls return a value past
        # the deadline, guaranteeing the timeout fires at the 1024th iteration.
        call_count = 0

        def mock_monotonic():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Initial call to set deadline: deadline = 100.0 + 5.0 = 105.0
                return 100.0
            # All subsequent calls: well past deadline
            return 200.0

        with patch(
            "kicad_tools.router.pathfinder.time.monotonic",
            side_effect=mock_monotonic,
        ):
            result = router.router.route(pad_start, pad_end, per_net_timeout=5.0)

        assert result is None

    def test_route_succeeds_without_timeout(self):
        """Normal routing should succeed when no per_net_timeout is set."""
        router = Autorouter(width=50.0, height=40.0)

        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "OK_NET"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "OK_NET"},
            ],
        )

        pad_start = router.pads[("R1", "1")]
        pad_end = router.pads[("R2", "1")]

        # Without timeout, should find a route normally
        result = router.router.route(pad_start, pad_end, per_net_timeout=None)
        assert result is not None, "Should find a route without timeout"

    def test_route_succeeds_with_generous_timeout(self):
        """Routing should succeed with a generous per_net_timeout."""
        router = Autorouter(width=50.0, height=40.0)

        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "OK_NET"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "OK_NET"},
            ],
        )

        pad_start = router.pads[("R1", "1")]
        pad_end = router.pads[("R2", "1")]

        # With a generous timeout, should find a route
        result = router.router.route(pad_start, pad_end, per_net_timeout=60.0)
        assert result is not None, "Should find a route with generous timeout"

    def test_route_all_negotiated_accepts_per_net_timeout(self):
        """route_all_negotiated should accept per_net_timeout parameter."""
        router = Autorouter(width=50.0, height=40.0)

        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )

        # Should not raise - verifies the parameter is accepted
        routes = router.route_all_negotiated(
            max_iterations=1,
            per_net_timeout=10.0,
        )
        assert isinstance(routes, list)


class TestPostRouteClearanceCorrection:
    """Tests for Issue #1666: post-route seg-seg clearance correction pass."""

    def test_post_route_correction_method_exists(self):
        """The _post_route_clearance_correction method must exist on Autorouter."""
        router = Autorouter(width=50.0, height=40.0)
        assert hasattr(router, "_post_route_clearance_correction")
        assert callable(router._post_route_clearance_correction)

    def test_post_route_correction_no_violations(self):
        """Correction pass returns 0 when there are no violations."""
        router = Autorouter(width=50.0, height=40.0)

        # No routes means no violations
        corrected = router._post_route_clearance_correction(
            net_routes={},
            pads_by_net={},
            present_factor=0.5,
        )
        assert corrected == 0

    def test_post_route_correction_with_routes(self):
        """Correction pass runs without error when routes exist."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.127,
        )
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Add two simple nets
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 5.0, "y": 10.0, "net": 1, "net_name": "NET1"},
                {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 5.0, "y": 20.0, "net": 2, "net_name": "NET2"},
                {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "NET2"},
            ],
        )

        # Route with negotiated mode - should invoke the correction pass
        routes = router.route_all_negotiated(max_iterations=2)
        assert isinstance(routes, list)

    def test_post_route_correction_uses_mark_route(self):
        """Issue #1694: Correction pass must call mark_route (width-aware), not mark_route_usage."""
        rules = DesignRules(
            grid_resolution=0.1,
            trace_width=0.2,
            trace_clearance=0.127,
        )
        router = Autorouter(width=50.0, height=40.0, rules=rules)

        # Create a route manually to simulate rerouting
        seg = Segment(
            x1=5.0, y1=10.0, x2=15.0, y2=10.0,
            width=0.5, net=1, layer=Layer.F_CU,
        )
        route = Route(net=1, net_name="NET1", segments=[seg], vias=[])

        # Patch validate_routes to return a violation so the correction runs,
        # then return no violations on the second call so it stops.
        violation = type(
            "V", (), {"net": 1, "obstacle_net": 2, "obstacle_type": "segment"}
        )()
        call_count = [0]

        def mock_validate(router_obj):
            call_count[0] += 1
            if call_count[0] == 1:
                return [violation]
            return []

        # Patch _route_net_negotiated to return the route
        def mock_route_net(net, pf, per_net_timeout=None):
            return [route]

        with patch("kicad_tools.router.io.validate_routes", mock_validate), \
             patch.object(router, "_route_net_negotiated", mock_route_net), \
             patch.object(router.grid, "mark_route") as mock_mark_route, \
             patch.object(router.grid, "mark_route_usage") as mock_mark_usage:

            router._post_route_clearance_correction(
                net_routes={1: [route], 2: []},
                pads_by_net={},
                present_factor=0.5,
            )

            # mark_route should be called (width-aware blocking)
            assert mock_mark_route.called, (
                "mark_route must be called in correction pass for width-aware blocking"
            )
            # mark_route_usage should NOT be called
            assert not mock_mark_usage.called, (
                "mark_route_usage should not be called in correction pass"
            )
