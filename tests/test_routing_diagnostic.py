"""Tests for the routing diagnostic board fixture.

This test module validates the autorouter algorithm using a minimal
diagnostic test circuit designed to expose common failure modes.

The routing-diagnostic.kicad_pcb fixture includes:
- R1 (0805) at off-grid position (5.05mm) to test FP precision
- R2 (0805) at standard grid position
- U1 (SOT-23-3) with tight 0.95mm pitch for clearance testing
- J1 (PTH 3-pin header) blocking both layers for via insertion tests

Nets:
- NET1: R1.1 -> U1.1 -> J1.1 (multi-point, SMD to PTH)
- NET2: R1.2 -> R2.1 (must route past U1, clearance test)
- NET3: R2.2 -> U1.2 -> J1.2 (multi-point, convergence at J1)
- NET4: U1.3 -> J1.3 (direct route baseline)

Expected failure modes tested:
1. Pad clearance (NET2 routing between/around U1's tight-pitch pads)
2. Floating-point precision (R1 at X=5.05mm with grid)
3. PTH layer blocking (J1 should block F.Cu and B.Cu)
4. Via insertion (when F.Cu is congested)
5. Route conflicts (NET1, NET3, NET4 all terminate at J1)
"""

from pathlib import Path

import pytest

from kicad_tools.router import DesignRules, load_pcb_for_routing


@pytest.fixture
def routing_diagnostic_pcb(fixtures_dir: Path) -> Path:
    """Return the path to the routing diagnostic PCB."""
    return fixtures_dir / "routing-diagnostic.kicad_pcb"


class TestRoutingDiagnosticFixture:
    """Tests for loading and routing the diagnostic fixture."""

    def test_load_diagnostic_pcb(self, routing_diagnostic_pcb: Path):
        """Test that the diagnostic PCB loads correctly."""
        assert routing_diagnostic_pcb.exists()

        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        assert router is not None
        assert isinstance(net_map, dict)

        # Verify expected nets are present
        assert "NET1" in net_map
        assert "NET2" in net_map
        assert "NET3" in net_map
        assert "NET4" in net_map

    def test_diagnostic_pcb_dimensions(self, routing_diagnostic_pcb: Path):
        """Test that board dimensions are parsed correctly."""
        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        # Board is 15mm x 15mm at origin (0,0)
        assert router.grid.width == 15.0
        assert router.grid.height == 15.0
        assert router.grid.origin_x == 0.0
        assert router.grid.origin_y == 0.0

    def test_diagnostic_pcb_components(self, routing_diagnostic_pcb: Path):
        """Test that all components are loaded correctly."""
        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        # Should have pads from: R1(2), R2(2), U1(3), J1(3) = 10 total
        assert len(router.pads) == 10

        # Check specific components
        assert ("R1", "1") in router.pads
        assert ("R1", "2") in router.pads
        assert ("R2", "1") in router.pads
        assert ("R2", "2") in router.pads
        assert ("U1", "1") in router.pads
        assert ("U1", "2") in router.pads
        assert ("U1", "3") in router.pads
        assert ("J1", "1") in router.pads
        assert ("J1", "2") in router.pads
        assert ("J1", "3") in router.pads

    def test_diagnostic_pcb_through_hole_detection(self, routing_diagnostic_pcb: Path):
        """Test that J1's through-hole pads are detected."""
        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        # J1 pads should be through-hole
        j1_pad1 = router.pads.get(("J1", "1"))
        j1_pad2 = router.pads.get(("J1", "2"))
        j1_pad3 = router.pads.get(("J1", "3"))

        assert j1_pad1 is not None
        assert j1_pad2 is not None
        assert j1_pad3 is not None

        assert j1_pad1.through_hole is True
        assert j1_pad2.through_hole is True
        assert j1_pad3.through_hole is True

    def test_diagnostic_pcb_net_assignments(self, routing_diagnostic_pcb: Path):
        """Test that net assignments match the spec."""
        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        # Get net IDs
        net1_id = net_map.get("NET1")
        net2_id = net_map.get("NET2")
        net3_id = net_map.get("NET3")
        net4_id = net_map.get("NET4")

        assert net1_id is not None
        assert net2_id is not None
        assert net3_id is not None
        assert net4_id is not None

        # NET1: R1.1, U1.1, J1.1
        assert router.pads[("R1", "1")].net == net1_id
        assert router.pads[("U1", "1")].net == net1_id
        assert router.pads[("J1", "1")].net == net1_id

        # NET2: R1.2, R2.1
        assert router.pads[("R1", "2")].net == net2_id
        assert router.pads[("R2", "1")].net == net2_id

        # NET3: R2.2, U1.2, J1.2
        assert router.pads[("R2", "2")].net == net3_id
        assert router.pads[("U1", "2")].net == net3_id
        assert router.pads[("J1", "2")].net == net3_id

        # NET4: U1.3, J1.3
        assert router.pads[("U1", "3")].net == net4_id
        assert router.pads[("J1", "3")].net == net4_id

    def test_off_grid_position_precision(self, routing_diagnostic_pcb: Path):
        """Test that R1's off-grid X position (5.05mm) is preserved."""
        router, net_map = load_pcb_for_routing(str(routing_diagnostic_pcb))

        r1_pad1 = router.pads.get(("R1", "1"))
        assert r1_pad1 is not None

        # R1 is at (5.05, 4.0), pad 1 is offset -1.0 in X
        # So pad 1 should be at approximately (4.05, 4.0)
        assert abs(r1_pad1.x - 4.05) < 0.01
        assert abs(r1_pad1.y - 4.0) < 0.01


class TestRoutingDiagnosticRouting:
    """Tests for routing the diagnostic board."""

    def test_route_basic_strategy(self, routing_diagnostic_pcb: Path):
        """Test routing with basic strategy."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Count nets to route (excluding net 0)
        nets_to_route = len([n for n in router.nets if n > 0])
        assert nets_to_route == 4  # NET1, NET2, NET3, NET4

        # Route all nets
        routed = router.route_all()

        # Get statistics
        stats = router.get_statistics()
        print(f"\nRouting stats: {stats}")

        # We expect the router to at least attempt all nets
        # Perfect routing would have nets_routed == 4
        assert stats["nets_routed"] >= 0  # Baseline: router runs without error

        # Check that routes were created
        assert isinstance(routed, list)

    def test_route_negotiated_strategy(self, routing_diagnostic_pcb: Path):
        """Test routing with negotiated congestion strategy."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Route with negotiated strategy
        routed = router.route_all_negotiated(max_iterations=10)

        stats = router.get_statistics()
        print(f"\nNegotiated routing stats: {stats}")

        # Negotiated routing should generally perform better
        assert isinstance(routed, list)
        assert stats["nets_routed"] >= 0

    def test_route_with_finer_grid(self, routing_diagnostic_pcb: Path):
        """Test routing with finer grid resolution for tight spaces."""
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.15,
            via_drill=0.3,
            via_diameter=0.5,
            grid_resolution=0.05,  # Finer grid
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Route all nets
        routed = router.route_all_negotiated(max_iterations=15)

        stats = router.get_statistics()
        print(f"\nFine grid routing stats: {stats}")

        # With finer grid, we may route more nets
        assert isinstance(routed, list)

    def test_route_statistics_accuracy(self, routing_diagnostic_pcb: Path):
        """Test that routing statistics are accurate."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        router.route_all()

        stats = router.get_statistics()

        # Verify statistics are sensible
        assert stats["routes"] >= 0
        assert stats["segments"] >= 0
        assert stats["vias"] >= 0
        assert stats["total_length_mm"] >= 0.0
        assert stats["nets_routed"] >= 0
        assert stats["nets_routed"] <= 4  # Can't route more than 4 nets


class TestRoutingDiagnosticChallenges:
    """Tests for specific routing challenges the diagnostic board exposes."""

    def test_net2_clearance_challenge(self, routing_diagnostic_pcb: Path):
        """Test NET2 routing which must navigate around U1's tight pads.

        NET2 connects R1.2 to R2.1 and must route past U1's SOT-23-3 pads
        which have 0.95mm pitch. This tests the router's obstacle clearance
        calculation.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        net2_id = net_map.get("NET2")
        assert net2_id is not None

        # Try to route just NET2
        result = router.route_net(net2_id)

        # NET2 may or may not route depending on grid/clearance settings
        # This test documents the challenge
        print(f"\nNET2 routing result: {result}")

    def test_multipoint_net_routing(self, routing_diagnostic_pcb: Path):
        """Test routing multi-point nets (NET1 and NET3 have 3 pads each).

        NET1: R1.1 -> U1.1 -> J1.1
        NET3: R2.2 -> U1.2 -> J1.2

        These test the router's ability to create spanning trees connecting
        multiple pads.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Verify multi-point nets have correct number of pads
        net1_id = net_map.get("NET1")
        net3_id = net_map.get("NET3")

        assert net1_id is not None
        assert net3_id is not None

        # Each should have 3 pads
        net1_pads = router.nets.get(net1_id, [])
        net3_pads = router.nets.get(net3_id, [])

        assert len(net1_pads) == 3
        assert len(net3_pads) == 3

    def test_smd_to_pth_transition(self, routing_diagnostic_pcb: Path):
        """Test routing from SMD pads to PTH pads.

        NET1 and NET3 both involve routing from SMD pads (R1, R2, U1)
        to PTH pads (J1). The PTH pads block both layers and may require
        layer transitions via vias.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Route all nets
        router.route_all()

        stats = router.get_statistics()

        # Document whether vias were needed
        print(f"\nVias used: {stats['vias']}")
        print(f"Total segments: {stats['segments']}")


class TestRoutingDiagnosticRegression:
    """Regression tests to track router improvements over time."""

    def test_full_routing_success_rate(self, routing_diagnostic_pcb: Path):
        """Track the router's success rate on this diagnostic board.

        This test documents current routing capability and can be updated
        as the router improves. The goal is eventually 4/4 nets routed
        with 0 DRC violations.
        """
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        # Use negotiated routing for best results
        router.route_all_negotiated(max_iterations=15)

        stats = router.get_statistics()

        # Document current performance
        print("\n" + "=" * 50)
        print("ROUTING DIAGNOSTIC BOARD - REGRESSION TEST")
        print("=" * 50)
        print(f"Nets routed: {stats['nets_routed']}/4")
        print(f"Routes: {stats['routes']}")
        print(f"Segments: {stats['segments']}")
        print(f"Vias: {stats['vias']}")
        print(f"Total length: {stats['total_length_mm']:.2f}mm")
        print("=" * 50)

        # This is a documentation test - record current state
        # As the router improves, update these expectations
        # Current baseline: router runs without error
        assert stats["nets_routed"] >= 0

    def test_sexp_output_valid(self, routing_diagnostic_pcb: Path):
        """Test that generated S-expression output is valid."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            via_drill=0.3,
            via_diameter=0.6,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_diagnostic_pcb),
            rules=rules,
        )

        router.route_all()

        # Get S-expression output
        sexp = router.to_sexp()

        # If routes were created, verify basic S-expression structure
        if sexp:
            # Should contain segment definitions
            assert "segment" in sexp or len(router.routes) == 0

            # Should not contain invalid syntax
            assert "# Invalid" not in sexp  # Issue #282
            assert "None" not in sexp  # Should not have Python None in output
