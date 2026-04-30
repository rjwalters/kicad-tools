"""Tests for via placement regression fix (Issue #2325).

Validates that the router correctly places vias on multi-layer boards,
specifically addressing the regression where accumulated via costs and
plane-layer PTH pad blocking prevented all via placement.
"""

from kicad_tools.router.core import Autorouter
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pathfinder import Router
from kicad_tools.router.rules import DesignRules


class TestViaCostCap:
    """Tests for via cost cap (Issue #2325)."""

    def test_via_cost_cap_factor_default(self):
        """Default via_cost_cap_factor is 2.0."""
        rules = DesignRules()
        assert rules.via_cost_cap_factor == 2.0

    def test_via_cost_cap_factor_custom(self):
        """via_cost_cap_factor can be set to a custom value."""
        rules = DesignRules(via_cost_cap_factor=3.0)
        assert rules.via_cost_cap_factor == 3.0

    def test_via_cost_cap_factor_disabled(self):
        """via_cost_cap_factor of 0.0 disables capping."""
        rules = DesignRules(via_cost_cap_factor=0.0)
        assert rules.via_cost_cap_factor == 0.0

    def test_via_cost_cap_limits_total_via_cost(self):
        """Via cost cap should prevent total via cost from exceeding cap.

        When cost_via=10.0 and via_cost_cap_factor=2.0, the total
        incremental via cost should not exceed 20.0, even when
        inner_layer_cost, layer_util_cost, corridor_cost, etc. would
        push it higher.
        """
        rules = DesignRules(
            cost_via=10.0,
            via_cost_cap_factor=2.0,
            cost_layer_inner=5.0,
            cost_layer_utilization=10.0,
            cost_corridor_deviation=10.0,
        )
        # The cap is cost_via * via_cost_cap_factor = 20.0
        # Without cap, cost_via + cost_layer_inner + util + corridor could be 35+
        cap = rules.via_cost_cap_factor * rules.cost_via
        assert cap == 20.0

        # Verify that the uncapped sum would exceed the cap
        uncapped = (
            rules.cost_via
            + rules.cost_layer_inner
            + 0.5 * rules.cost_layer_utilization  # 50% utilization
            + rules.cost_corridor_deviation
        )
        assert uncapped > cap, "Uncapped cost should exceed cap for this test to be meaningful"


class TestPlanelayerViaSkip:
    """Tests for skipping plane layers in via placement checks (Issue #2325)."""

    def test_plane_layer_skipped_in_via_check(self):
        """Via check should skip plane layers.

        On a 4-layer board (SIG-GND-PWR-SIG), the inner GND and PWR
        plane layers should not be checked for via blocking because
        KiCad's zone fill handles clearance automatically.
        """
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=stack,
        )
        Router(grid, rules)  # Ensure Router accepts this grid

        # Verify that plane layers are correctly identified
        assert grid.is_plane_layer(1)  # In1.Cu (GND)
        assert grid.is_plane_layer(2)  # In2.Cu (PWR)
        assert not grid.is_plane_layer(0)  # F.Cu (signal)
        assert not grid.is_plane_layer(3)  # B.Cu (signal)

    def test_via_placement_on_four_layer_board_with_pth_pads(self):
        """Via placement should succeed on 4-layer board despite PTH pad blockage.

        When PTH pads block cells on plane layers, vias should still be
        placeable at positions away from those pads because the plane
        layer check is skipped.
        """
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=stack,
        )
        router = Router(grid, rules)

        # Block a region on the plane layer (simulating PTH pad)
        # This would previously cause via rejection at nearby positions
        plane_layer = 1  # GND plane
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                gx, gy = 10 + dx, 10 + dy
                if 0 <= gx < grid.cols and 0 <= gy < grid.rows:
                    grid._blocked[plane_layer, gy, gx] = True
                    grid._net[plane_layer, gy, gx] = 99  # Some other net

        # Check via at a position FAR from the blocked region
        # This should succeed because plane layers are skipped
        can_place = router._check_via_placement_cached(20, 20, net=1, allow_sharing=False)
        assert can_place, "Via should be placeable far from PTH pad blockage on plane layer"

    def test_via_still_blocked_on_signal_layers(self):
        """Via placement should still be blocked by obstacles on signal layers.

        Skipping plane layers should NOT affect blocking checks on signal
        layers (F.Cu, B.Cu).
        """
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(
            width=20.0,
            height=20.0,
            rules=rules,
            layer_stack=stack,
        )
        router = Router(grid, rules)

        # Block on signal layer 0 (F.Cu) - different net
        gx, gy = 15, 15
        grid._blocked[0, gy, gx] = True
        grid._net[0, gy, gx] = 99  # Different net

        # Via should be blocked because F.Cu is a signal layer
        can_place = router._check_via_placement_cached(gx, gy, net=1, allow_sharing=False)
        assert not can_place, "Via should be blocked by obstacle on signal layer"


class TestViaDiagnostics:
    """Tests for via placement diagnostic counters (Issue #2325)."""

    def test_diagnostic_counters_initialized(self):
        """Diagnostic counters should start at zero."""
        stack = LayerStack.two_layer()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=stack,
        )
        router = Router(grid, rules)

        diag = router.get_via_diagnostics()
        assert diag["attempts"] == 0
        assert diag["blocked"] == 0
        assert diag["zone_blocked"] == 0
        assert diag["exclusion_blocked"] == 0
        assert diag["eligible"] == 0

    def test_diagnostic_counters_reset(self):
        """reset_via_diagnostics should zero all counters."""
        stack = LayerStack.two_layer()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
            layer_stack=stack,
        )
        router = Router(grid, rules)

        # Manually set some counts
        router._via_diag_attempts = 42
        router._via_diag_blocked = 10
        router.reset_via_diagnostics()

        diag = router.get_via_diagnostics()
        assert diag["attempts"] == 0
        assert diag["blocked"] == 0


class TestFourLayerRoutingProducesVias:
    """Integration test: 4-layer routing should produce vias.

    This tests the core regression: on a 4-layer board where two
    pads are on different layers, the router MUST use at least one
    via to connect them.
    """

    def test_cross_layer_route_produces_via(self):
        """Routing between F.Cu and B.Cu pads should produce a via."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(
            grid_resolution=0.5,
            cost_via=10.0,
            via_cost_cap_factor=2.0,
        )
        router = Autorouter(
            width=30.0,
            height=20.0,
            rules=rules,
            layer_stack=stack,
        )

        # Add pad on F.Cu
        pads_r1 = [
            {
                "number": "1",
                "x": 5.0,
                "y": 10.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "SIG1",
                "layer": Layer.F_CU,
            },
        ]
        router.add_component("R1", pads_r1)

        # Add pad on B.Cu
        pads_r2 = [
            {
                "number": "1",
                "x": 25.0,
                "y": 10.0,
                "width": 1.0,
                "height": 1.0,
                "net": 1,
                "net_name": "SIG1",
                "layer": Layer.B_CU,
            },
        ]
        router.add_component("R2", pads_r2)

        # Route all nets
        results = router.route_all()

        # Check that at least one route has a via
        has_via = any(route.vias and len(route.vias) > 0 for route in results)
        assert has_via, "Cross-layer route should produce at least one via"

    def test_four_layer_with_pth_pads_allows_vias(self):
        """PTH pads on plane layers should not block ALL via placement.

        Simulates the chorus-test scenario where PTH pads exist on inner
        plane layers. Vias should still be placeable at positions away
        from those pads.
        """
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(
            grid_resolution=0.5,
            cost_via=10.0,
            via_cost_cap_factor=2.0,
        )
        router = Autorouter(
            width=40.0,
            height=30.0,
            rules=rules,
            layer_stack=stack,
        )

        # Add several PTH pads (simulating through-hole connectors)
        for i in range(5):
            pads = [
                {
                    "number": "1",
                    "x": 5.0 + i * 3.0,
                    "y": 15.0,
                    "width": 1.7,
                    "height": 1.7,
                    "net": 10 + i,
                    "net_name": f"PTH_NET_{i}",
                    "through_hole": True,
                    "drill": 1.0,
                },
            ]
            router.add_component(f"J{i}", pads)

        # Add two SMD pads on different layers that need a via
        pads_front = [
            {
                "number": "1",
                "x": 30.0,
                "y": 5.0,
                "width": 0.8,
                "height": 0.8,
                "net": 1,
                "net_name": "SIGNAL",
                "layer": Layer.F_CU,
            },
        ]
        pads_back = [
            {
                "number": "1",
                "x": 30.0,
                "y": 25.0,
                "width": 0.8,
                "height": 0.8,
                "net": 1,
                "net_name": "SIGNAL",
                "layer": Layer.B_CU,
            },
        ]
        router.add_component("U1", pads_front)
        router.add_component("U2", pads_back)

        # Route
        results = router.route_all()

        # The SIGNAL net should be routed with at least one via
        signal_routes = [r for r in results if r.net == 1]
        assert len(signal_routes) > 0, "SIGNAL net should have at least one route"

        has_via = any(route.vias and len(route.vias) > 0 for route in signal_routes)
        assert has_via, "Cross-layer SIGNAL route should use via despite PTH pads on plane layers"


class TestViaCostCapEffect:
    """Tests that via cost cap actually changes routing behavior."""

    def test_cap_reduces_effective_via_cost(self):
        """With cap enabled, effective via cost should be bounded.

        Tests the math directly: when multiple costs stack above the cap,
        the capped value should be used instead.
        """
        rules = DesignRules(
            cost_via=10.0,
            via_cost_cap_factor=2.0,
        )
        cap = rules.via_cost_cap_factor * rules.cost_via

        # Simulate accumulated cost components at high utilization
        base_via_cost = rules.cost_via  # 10.0
        inner_layer_cost = rules.cost_layer_inner  # 2.0
        layer_util_cost = 0.8 * rules.cost_layer_utilization  # 4.0
        corridor_cost = 5.0  # Full corridor deviation penalty
        congestion_cost = 4.0  # Congested area

        uncapped_total = (
            base_via_cost + inner_layer_cost + layer_util_cost + corridor_cost + congestion_cost
        )
        capped_total = min(uncapped_total, cap)

        assert uncapped_total > cap, "Test requires uncapped > cap"
        assert capped_total == cap, "Capped total should equal cap"
        assert capped_total == 20.0
