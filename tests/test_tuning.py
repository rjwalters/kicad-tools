"""Tests for auto-tuning cost parameters."""

from kicad_tools.router import Autorouter, DesignRules
from kicad_tools.router.primitives import Pad
from kicad_tools.router.tuning import (
    COST_PROFILES,
    BoardCharacteristics,
    CostParams,
    CostProfile,
    analyze_board,
    create_adaptive_router,
    quick_tune,
    select_profile,
)


class TestCostParams:
    """Tests for CostParams dataclass."""

    def test_cost_params_creation(self):
        """Test creating cost parameters."""
        params = CostParams(via=10.0, turn=5.0, congestion=2.0)
        assert params.via == 10.0
        assert params.turn == 5.0
        assert params.congestion == 2.0

    def test_cost_params_defaults(self):
        """Test default cost parameters."""
        params = CostParams()
        assert params.via == 10.0
        assert params.turn == 5.0
        assert params.congestion == 2.0
        assert params.straight == 1.0
        assert params.diagonal == 1.414

    def test_cost_params_apply_to_rules(self):
        """Test applying cost params to design rules."""
        params = CostParams(via=15.0, turn=8.0, congestion=4.0)
        rules = DesignRules()
        new_rules = params.apply_to_rules(rules)

        assert new_rules.cost_via == 15.0
        assert new_rules.cost_turn == 8.0
        assert new_rules.cost_congestion == 4.0
        # Original rules unchanged
        assert rules.cost_via == 10.0

    def test_cost_params_scale(self):
        """Test scaling cost parameters."""
        params = CostParams(via=10.0, turn=5.0, congestion=2.0)
        scaled = params.scale(2.0)

        assert scaled.via == 20.0
        assert scaled.turn == 10.0
        assert scaled.congestion == 4.0


class TestCostProfiles:
    """Tests for preset cost profiles."""

    def test_all_profiles_exist(self):
        """Test that all defined profiles exist."""
        assert CostProfile.SPARSE in COST_PROFILES
        assert CostProfile.STANDARD in COST_PROFILES
        assert CostProfile.DENSE in COST_PROFILES
        assert CostProfile.MINIMIZE_VIAS in COST_PROFILES
        assert CostProfile.MINIMIZE_LENGTH in COST_PROFILES
        assert CostProfile.HIGH_SPEED in COST_PROFILES

    def test_profile_values(self):
        """Test that profile values are reasonable."""
        for profile, params in COST_PROFILES.items():
            assert params.via >= 1.0, f"{profile} via cost too low"
            assert params.turn >= 1.0, f"{profile} turn cost too low"
            assert params.congestion >= 1.0, f"{profile} congestion cost too low"
            assert params.straight > 0, f"{profile} straight cost must be positive"

    def test_minimize_vias_has_high_via_cost(self):
        """Test that minimize_vias profile has high via cost."""
        params = COST_PROFILES[CostProfile.MINIMIZE_VIAS]
        standard = COST_PROFILES[CostProfile.STANDARD]
        assert params.via > standard.via

    def test_high_speed_has_high_turn_cost(self):
        """Test that high_speed profile has high turn cost."""
        params = COST_PROFILES[CostProfile.HIGH_SPEED]
        standard = COST_PROFILES[CostProfile.STANDARD]
        assert params.turn > standard.turn


class TestBoardCharacteristics:
    """Tests for board analysis."""

    def test_analyze_empty_board(self):
        """Test analyzing empty board."""
        characteristics = analyze_board(
            nets={},
            pads={},
            board_width=100.0,
            board_height=100.0,
            layer_count=2,
        )
        assert characteristics.total_pads == 0
        assert characteristics.total_nets == 0
        assert characteristics.pin_density == 0.0

    def test_analyze_simple_board(self):
        """Test analyzing simple board."""
        pads = {
            ("U1", "1"): Pad(x=10, y=10, width=1, height=1, net=1, net_name="NET1"),
            ("U1", "2"): Pad(x=20, y=10, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "1"): Pad(x=50, y=50, width=1, height=1, net=2, net_name="NET2"),
            ("U2", "2"): Pad(x=60, y=50, width=1, height=1, net=2, net_name="NET2"),
        }
        nets = {
            1: [("U1", "1"), ("U1", "2")],
            2: [("U2", "1"), ("U2", "2")],
        }
        characteristics = analyze_board(
            nets=nets,
            pads=pads,
            board_width=100.0,
            board_height=100.0,
            layer_count=2,
        )

        assert characteristics.total_pads == 4
        assert characteristics.total_nets == 2
        assert characteristics.pin_density == 4 / 10000  # 4 pads / 10000 mm²
        assert characteristics.layer_count == 2

    def test_analyze_skips_net_zero(self):
        """Test that net 0 (unconnected) is skipped."""
        pads = {
            ("U1", "1"): Pad(x=10, y=10, width=1, height=1, net=0, net_name=""),
            ("U1", "2"): Pad(x=20, y=10, width=1, height=1, net=0, net_name=""),
            ("U2", "1"): Pad(x=50, y=50, width=1, height=1, net=1, net_name="NET1"),
            ("U2", "2"): Pad(x=60, y=50, width=1, height=1, net=1, net_name="NET1"),
        }
        nets = {
            0: [("U1", "1"), ("U1", "2")],
            1: [("U2", "1"), ("U2", "2")],
        }
        characteristics = analyze_board(
            nets=nets,
            pads=pads,
            board_width=100.0,
            board_height=100.0,
            layer_count=2,
        )

        # Only net 1 should be counted
        assert characteristics.total_nets == 1


class TestSelectProfile:
    """Tests for automatic profile selection."""

    def test_select_sparse_profile(self):
        """Test sparse profile selection for low density."""
        characteristics = BoardCharacteristics(
            pin_density=0.005,  # Very low density
            total_pads=10,
            total_nets=5,
        )
        profile = select_profile(characteristics)
        assert profile == CostProfile.SPARSE

    def test_select_standard_profile(self):
        """Test standard profile selection for medium density."""
        characteristics = BoardCharacteristics(
            pin_density=0.02,  # Medium density
            total_pads=100,
            total_nets=30,
        )
        profile = select_profile(characteristics)
        assert profile == CostProfile.STANDARD

    def test_select_dense_profile(self):
        """Test dense profile selection for high density."""
        characteristics = BoardCharacteristics(
            pin_density=0.1,  # High density
            total_pads=500,
            total_nets=100,
        )
        profile = select_profile(characteristics)
        assert profile == CostProfile.DENSE


class TestQuickTune:
    """Tests for heuristic-based tuning."""

    def test_quick_tune_sparse_board(self):
        """Test quick tuning for sparse board."""
        characteristics = BoardCharacteristics(
            pin_density=0.005,
            avg_net_span=10.0,
            layer_count=2,
        )
        params = quick_tune(characteristics)

        # Sparse board should have lower costs
        assert params.via < 15.0
        assert params.congestion < 5.0

    def test_quick_tune_dense_board(self):
        """Test quick tuning for dense board."""
        characteristics = BoardCharacteristics(
            pin_density=0.1,
            avg_net_span=20.0,
            layer_count=2,
        )
        params = quick_tune(characteristics)

        # Dense board should have higher costs
        assert params.via >= 10.0
        assert params.congestion >= 3.0

    def test_quick_tune_multi_layer(self):
        """Test that multi-layer boards have lower via costs."""
        characteristics_2layer = BoardCharacteristics(
            pin_density=0.05,
            avg_net_span=15.0,
            layer_count=2,
        )
        characteristics_4layer = BoardCharacteristics(
            pin_density=0.05,
            avg_net_span=15.0,
            layer_count=4,
        )

        params_2layer = quick_tune(characteristics_2layer)
        params_4layer = quick_tune(characteristics_4layer)

        # 4-layer should have lower via cost
        assert params_4layer.via < params_2layer.via

    def test_quick_tune_bounds(self):
        """Test that tuned values stay within bounds."""
        # Test with extreme values
        characteristics = BoardCharacteristics(
            pin_density=1.0,  # Very high
            avg_net_span=100.0,  # Very long
            layer_count=2,
        )
        params = quick_tune(characteristics)

        # Values should be clamped
        assert 3.0 <= params.via <= 30.0
        assert 2.0 <= params.turn <= 10.0
        assert 1.5 <= params.congestion <= 8.0


class TestAdaptiveRouter:
    """Tests for adaptive routing."""

    def test_create_adaptive_router(self):
        """Test creating an adaptive router function."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        adaptive_fn = create_adaptive_router(router, max_iterations=2)
        assert callable(adaptive_fn)


class TestIntegration:
    """Integration tests for tuning with Autorouter."""

    def test_route_all_tuned_quick(self):
        """Test route_all_tuned with quick method."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        routes = router.route_all_tuned(method="quick")
        assert isinstance(routes, list)

    def test_route_all_tuned_with_profile(self):
        """Test route_all_tuned with preset profile."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        routes = router.route_all_tuned(profile="standard")
        assert isinstance(routes, list)

    def test_route_all_tuned_with_profile_enum(self):
        """Test route_all_tuned with CostProfile enum."""
        rules = DesignRules(
            grid_resolution=0.5,
            trace_width=0.2,
            trace_clearance=0.2,
        )
        router = Autorouter(width=30, height=30, rules=rules)

        # Add simple net
        router.add_component(
            "U1",
            [
                {
                    "number": "1",
                    "x": 5,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
                {
                    "number": "2",
                    "x": 10,
                    "y": 5,
                    "width": 1,
                    "height": 1,
                    "net": 1,
                    "net_name": "NET1",
                },
            ],
        )

        routes = router.route_all_tuned(profile=CostProfile.DENSE)
        assert isinstance(routes, list)
