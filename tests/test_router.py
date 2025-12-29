"""Tests for router modules (primitives, rules, layers, heuristics)."""

import pytest

from kicad_tools.router.primitives import (
    Point, GridCell, Via, Segment, Route, Pad, Obstacle
)
from kicad_tools.router.rules import (
    DesignRules, NetClassRouting, create_net_class_map,
    NET_CLASS_POWER, NET_CLASS_CLOCK, NET_CLASS_DEFAULT, DEFAULT_NET_CLASS_MAP
)
from kicad_tools.router.layers import (
    Layer, LayerType, LayerDefinition, LayerStack, ViaType, ViaDefinition, ViaRules
)
from kicad_tools.router.heuristics import (
    HeuristicContext, ManhattanHeuristic, DirectionBiasHeuristic,
    CongestionAwareHeuristic, WeightedCongestionHeuristic, GreedyHeuristic,
    DEFAULT_HEURISTIC
)


class TestPoint:
    """Tests for Point class."""

    def test_point_creation(self):
        """Test creating a point."""
        pt = Point(10.0, 20.0, Layer.F_CU)
        assert pt.x == 10.0
        assert pt.y == 20.0
        assert pt.layer == Layer.F_CU

    def test_point_default_layer(self):
        """Test default layer is F_CU."""
        pt = Point(0, 0)
        assert pt.layer == Layer.F_CU

    def test_point_hash(self):
        """Test point hashing."""
        pt1 = Point(10.0, 20.0, Layer.F_CU)
        pt2 = Point(10.0, 20.0, Layer.F_CU)
        assert hash(pt1) == hash(pt2)

    def test_point_equality(self):
        """Test point equality."""
        pt1 = Point(10.0, 20.0, Layer.F_CU)
        pt2 = Point(10.0, 20.0, Layer.F_CU)
        pt3 = Point(10.0, 20.0, Layer.B_CU)
        assert pt1 == pt2
        assert pt1 != pt3

    def test_point_inequality_beyond_tolerance(self):
        """Test point inequality beyond 4 decimal place tolerance."""
        pt1 = Point(10.0001, 20.0001, Layer.F_CU)
        pt2 = Point(10.0002, 20.0002, Layer.F_CU)
        # These differ at 4 decimal places, so should not be equal
        assert pt1 != pt2

    def test_point_equality_non_point(self):
        """Test point equality with non-Point."""
        pt = Point(0, 0)
        assert pt.__eq__("not a point") == NotImplemented

    def test_point_grid_key(self):
        """Test grid key generation."""
        pt = Point(1.0, 2.0, Layer.F_CU)
        key = pt.grid_key(0.1)
        assert key == (10, 20, Layer.F_CU.value)

    def test_point_distance_same_layer(self):
        """Test Manhattan distance on same layer."""
        pt1 = Point(0, 0, Layer.F_CU)
        pt2 = Point(3, 4, Layer.F_CU)
        # Manhattan distance = |3| + |4| = 7
        assert pt1.distance_to(pt2) == 7.0

    def test_point_distance_different_layer(self):
        """Test distance with layer change."""
        pt1 = Point(0, 0, Layer.F_CU)
        pt2 = Point(0, 0, Layer.B_CU)
        # Layer distance includes via cost estimate
        dist = pt1.distance_to(pt2)
        assert dist > 0


class TestGridCell:
    """Tests for GridCell class."""

    def test_grid_cell_creation(self):
        """Test creating a grid cell."""
        cell = GridCell(5, 10, 0)
        assert cell.x == 5
        assert cell.y == 10
        assert cell.layer == 0
        assert cell.blocked is False
        assert cell.net == 0
        assert cell.cost == 1.0

    def test_grid_cell_with_options(self):
        """Test creating grid cell with options."""
        cell = GridCell(5, 10, 1, blocked=True, net=5, cost=2.0)
        assert cell.blocked is True
        assert cell.net == 5
        assert cell.cost == 2.0

    def test_grid_cell_congestion_fields(self):
        """Test congestion tracking fields."""
        cell = GridCell(0, 0, 0, usage_count=3, history_cost=0.5, is_obstacle=True)
        assert cell.usage_count == 3
        assert cell.history_cost == 0.5
        assert cell.is_obstacle is True


class TestVia:
    """Tests for Via class."""

    def test_via_creation(self):
        """Test creating a via."""
        via = Via(
            x=10.0, y=20.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1, net_name="GND"
        )
        assert via.x == 10.0
        assert via.y == 20.0
        assert via.drill == 0.3
        assert via.diameter == 0.6
        assert via.layers == (Layer.F_CU, Layer.B_CU)

    def test_via_to_sexp(self):
        """Test via S-expression generation."""
        via = Via(
            x=10.0, y=20.0, drill=0.3, diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU), net=1
        )
        sexp = via.to_sexp()
        assert "(via" in sexp
        assert "10.0000" in sexp
        assert "20.0000" in sexp
        assert "0.6" in sexp
        assert "0.3" in sexp
        assert "F.Cu" in sexp
        assert "B.Cu" in sexp


class TestSegment:
    """Tests for Segment class."""

    def test_segment_creation(self):
        """Test creating a segment."""
        seg = Segment(
            x1=0, y1=0, x2=10, y2=0, width=0.2,
            layer=Layer.F_CU, net=1, net_name="VCC"
        )
        assert seg.x1 == 0
        assert seg.x2 == 10
        assert seg.width == 0.2

    def test_segment_to_sexp(self):
        """Test segment S-expression generation."""
        seg = Segment(
            x1=0, y1=0, x2=10, y2=0, width=0.2,
            layer=Layer.F_CU, net=1
        )
        sexp = seg.to_sexp()
        assert "(segment" in sexp
        assert "(start" in sexp
        assert "(end" in sexp
        assert "0.2" in sexp
        assert "F.Cu" in sexp


class TestRoute:
    """Tests for Route class."""

    def test_route_creation(self):
        """Test creating a route."""
        route = Route(net=1, net_name="GND")
        assert route.net == 1
        assert route.net_name == "GND"
        assert route.segments == []
        assert route.vias == []

    def test_route_with_segments(self):
        """Test route with segments and vias."""
        route = Route(net=1, net_name="GND")
        route.segments.append(Segment(0, 0, 10, 0, 0.2, Layer.F_CU))
        route.vias.append(Via(10, 0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU)))

        assert len(route.segments) == 1
        assert len(route.vias) == 1

    def test_route_to_sexp(self):
        """Test route S-expression generation."""
        route = Route(net=1, net_name="GND")
        route.segments.append(Segment(0, 0, 10, 0, 0.2, Layer.F_CU, net=1))
        sexp = route.to_sexp()
        assert "(segment" in sexp


class TestPad:
    """Tests for Pad class."""

    def test_pad_creation(self):
        """Test creating a pad."""
        pad = Pad(
            x=10, y=20, width=1.0, height=1.0,
            net=1, net_name="VCC", layer=Layer.F_CU, ref="U1"
        )
        assert pad.x == 10
        assert pad.y == 20
        assert pad.width == 1.0
        assert pad.ref == "U1"

    def test_pad_through_hole(self):
        """Test through-hole pad."""
        pad = Pad(
            x=0, y=0, width=2.0, height=2.0,
            net=1, net_name="GND", through_hole=True, drill=1.0
        )
        assert pad.through_hole is True
        assert pad.drill == 1.0


class TestObstacle:
    """Tests for Obstacle class."""

    def test_obstacle_creation(self):
        """Test creating an obstacle."""
        obs = Obstacle(x=10, y=20, width=5, height=10, layer=Layer.F_CU, clearance=0.2)
        assert obs.x == 10
        assert obs.y == 20
        assert obs.width == 5
        assert obs.height == 10
        assert obs.clearance == 0.2


class TestDesignRules:
    """Tests for DesignRules class."""

    def test_design_rules_defaults(self):
        """Test default design rules."""
        rules = DesignRules()
        assert rules.trace_width == 0.2
        assert rules.trace_clearance == 0.2
        assert rules.via_drill == 0.35
        assert rules.via_diameter == 0.7
        assert rules.grid_resolution == 0.1

    def test_design_rules_custom(self):
        """Test custom design rules."""
        rules = DesignRules(
            trace_width=0.15,
            trace_clearance=0.15,
            via_drill=0.25
        )
        assert rules.trace_width == 0.15
        assert rules.via_drill == 0.25

    def test_design_rules_costs(self):
        """Test A* costs in design rules."""
        rules = DesignRules()
        assert rules.cost_straight == 1.0
        assert rules.cost_diagonal == pytest.approx(1.414, rel=0.01)
        assert rules.cost_turn == 5.0
        assert rules.cost_via == 10.0


class TestNetClassRouting:
    """Tests for NetClassRouting class."""

    def test_net_class_defaults(self):
        """Test default net class."""
        nc = NetClassRouting(name="Test")
        assert nc.name == "Test"
        assert nc.priority == 5
        assert nc.trace_width == 0.2
        assert nc.cost_multiplier == 1.0

    def test_net_class_power(self):
        """Test power net class preset."""
        assert NET_CLASS_POWER.name == "Power"
        assert NET_CLASS_POWER.priority == 1  # Highest priority
        assert NET_CLASS_POWER.trace_width == 0.5  # Wider traces

    def test_net_class_clock(self):
        """Test clock net class preset."""
        assert NET_CLASS_CLOCK.name == "Clock"
        assert NET_CLASS_CLOCK.length_critical is True


class TestCreateNetClassMap:
    """Tests for create_net_class_map function."""

    def test_create_empty_map(self):
        """Test creating empty map."""
        net_map = create_net_class_map()
        assert net_map == {}

    def test_create_power_nets(self):
        """Test creating map with power nets."""
        net_map = create_net_class_map(power_nets=["+5V", "GND"])
        assert "+5V" in net_map
        assert "GND" in net_map
        assert net_map["+5V"] == NET_CLASS_POWER

    def test_create_clock_nets(self):
        """Test creating map with clock nets."""
        net_map = create_net_class_map(clock_nets=["CLK", "MCLK"])
        assert "CLK" in net_map
        assert net_map["CLK"].name == "Clock"

    def test_default_net_class_map(self):
        """Test default net class map."""
        assert "+5V" in DEFAULT_NET_CLASS_MAP
        assert "GND" in DEFAULT_NET_CLASS_MAP
        assert "CLK" in DEFAULT_NET_CLASS_MAP


class TestLayer:
    """Tests for Layer enum."""

    def test_layer_values(self):
        """Test layer values."""
        assert Layer.F_CU.value == 0
        assert Layer.B_CU.value == 5

    def test_layer_kicad_names(self):
        """Test KiCad layer names."""
        assert Layer.F_CU.kicad_name == "F.Cu"
        assert Layer.B_CU.kicad_name == "B.Cu"
        assert Layer.IN1_CU.kicad_name == "In1.Cu"

    def test_layer_is_outer(self):
        """Test outer layer detection."""
        assert Layer.F_CU.is_outer is True
        assert Layer.B_CU.is_outer is True
        assert Layer.IN1_CU.is_outer is False


class TestLayerType:
    """Tests for LayerType enum."""

    def test_layer_types(self):
        """Test layer type values."""
        assert LayerType.SIGNAL.value == "signal"
        assert LayerType.PLANE.value == "plane"
        assert LayerType.MIXED.value == "mixed"


class TestLayerDefinition:
    """Tests for LayerDefinition class."""

    def test_layer_definition_creation(self):
        """Test creating layer definition."""
        layer_def = LayerDefinition(
            name="F.Cu", index=0, layer_type=LayerType.SIGNAL, is_outer=True
        )
        assert layer_def.name == "F.Cu"
        assert layer_def.index == 0
        assert layer_def.is_outer is True

    def test_layer_definition_layer_enum(self):
        """Test getting layer enum."""
        layer_def = LayerDefinition("F.Cu", 0, LayerType.SIGNAL)
        assert layer_def.layer_enum == Layer.F_CU

    def test_layer_definition_is_routable(self):
        """Test routable check."""
        signal = LayerDefinition("F.Cu", 0, LayerType.SIGNAL)
        plane = LayerDefinition("In1.Cu", 1, LayerType.PLANE)
        mixed = LayerDefinition("B.Cu", 3, LayerType.MIXED)

        assert signal.is_routable is True
        assert plane.is_routable is False
        assert mixed.is_routable is True


class TestLayerStack:
    """Tests for LayerStack class."""

    def test_two_layer_stack(self):
        """Test 2-layer stack preset."""
        stack = LayerStack.two_layer()
        assert stack.num_layers == 2
        assert stack.name == "2-Layer"

    def test_four_layer_stack(self):
        """Test 4-layer stack preset."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert stack.num_layers == 4
        assert len(stack.signal_layers) == 2
        assert len(stack.plane_layers) == 2

    def test_six_layer_stack(self):
        """Test 6-layer stack preset."""
        stack = LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        assert stack.num_layers == 6
        assert len(stack.signal_layers) == 4

    def test_layer_stack_validation(self):
        """Test layer stack validation."""
        # Non-sequential indices should raise
        with pytest.raises(ValueError, match="sequential"):
            LayerStack([
                LayerDefinition("F.Cu", 0, LayerType.SIGNAL),
                LayerDefinition("B.Cu", 5, LayerType.SIGNAL),  # Gap in indices
            ])

    def test_get_layer(self):
        """Test getting layer by index."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer(0)
        assert layer is not None
        assert layer.name == "F.Cu"
        assert stack.get_layer(99) is None

    def test_get_layer_by_name(self):
        """Test getting layer by name."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer_by_name("F.Cu")
        assert layer is not None
        assert layer.index == 0
        assert stack.get_layer_by_name("missing") is None

    def test_layer_enum_to_index(self):
        """Test mapping layer enum to index."""
        stack = LayerStack.two_layer()
        idx = stack.layer_enum_to_index(Layer.F_CU)
        assert idx == 0

    def test_index_to_layer_enum(self):
        """Test mapping index to layer enum."""
        stack = LayerStack.two_layer()
        layer = stack.index_to_layer_enum(0)
        assert layer == Layer.F_CU

    def test_get_routable_indices(self):
        """Test getting routable layer indices."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        indices = stack.get_routable_indices()
        assert 0 in indices  # F.Cu
        assert 3 in indices  # B.Cu
        assert 1 not in indices  # GND plane

    def test_is_plane_layer(self):
        """Test plane layer check."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert stack.is_plane_layer(1) is True  # GND plane
        assert stack.is_plane_layer(0) is False  # Signal

    def test_layer_stack_repr(self):
        """Test layer stack string representation."""
        stack = LayerStack.two_layer()
        s = repr(stack)
        assert "LayerStack" in s
        assert "2-Layer" in s


class TestViaType:
    """Tests for ViaType enum."""

    def test_via_types(self):
        """Test via type values."""
        assert ViaType.THROUGH.value == "through"
        assert ViaType.BLIND_TOP.value == "blind_top"
        assert ViaType.BURIED.value == "buried"
        assert ViaType.MICRO.value == "micro"


class TestViaDefinition:
    """Tests for ViaDefinition class."""

    def test_via_definition_creation(self):
        """Test creating via definition."""
        via_def = ViaDefinition(
            via_type=ViaType.THROUGH,
            drill_mm=0.3,
            annular_ring_mm=0.15,
            start_layer=0,
            end_layer=5
        )
        assert via_def.drill_mm == 0.3
        assert via_def.annular_ring_mm == 0.15

    def test_via_diameter(self):
        """Test via diameter calculation."""
        via_def = ViaDefinition(ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15)
        assert via_def.diameter == 0.6  # 0.3 + 2*0.15

    def test_via_spans_layer(self):
        """Test layer spanning check."""
        via_def = ViaDefinition(
            ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15,
            start_layer=0, end_layer=3
        )
        assert via_def.spans_layer(0, 4) is True
        assert via_def.spans_layer(2, 4) is True
        assert via_def.spans_layer(5, 6) is False

    def test_via_blocks_layer(self):
        """Test via blocking check."""
        via_def = ViaDefinition(
            ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15,
            start_layer=0, end_layer=-1  # -1 = bottom
        )
        assert via_def.blocks_layer(0, 4) is True
        assert via_def.blocks_layer(3, 4) is True


class TestViaRules:
    """Tests for ViaRules class."""

    def test_via_rules_defaults(self):
        """Test default via rules."""
        rules = ViaRules()
        assert rules.allow_blind is False
        assert rules.allow_buried is False
        assert rules.through_via is not None

    def test_via_rules_standard_2layer(self):
        """Test 2-layer via rules."""
        rules = ViaRules.standard_2layer()
        assert rules.through_via.start_layer == 0
        assert rules.through_via.end_layer == 1

    def test_via_rules_standard_4layer(self):
        """Test 4-layer via rules."""
        rules = ViaRules.standard_4layer()
        assert rules.through_via.end_layer == 3

    def test_via_rules_hdi(self):
        """Test HDI via rules."""
        rules = ViaRules.hdi_4layer()
        assert rules.allow_blind is True
        assert rules.allow_micro is True
        assert rules.blind_via is not None
        assert rules.micro_via is not None

    def test_get_available_vias(self):
        """Test getting available vias."""
        rules = ViaRules.standard_4layer()
        vias = rules.get_available_vias(4)
        assert len(vias) == 1  # Only through via

        rules_hdi = ViaRules.hdi_4layer()
        vias_hdi = rules_hdi.get_available_vias(4)
        assert len(vias_hdi) == 3  # Through, blind, micro

    def test_get_best_via(self):
        """Test getting best via for layer pair."""
        rules = ViaRules.hdi_4layer()
        best = rules.get_best_via(0, 3, 4)
        assert best is not None
        # Should get micro via (lowest cost) if it spans the layers
        # Actually micro only spans 0-1, so through via

    def test_get_best_via_no_match(self):
        """Test when no via spans the layers."""
        rules = ViaRules()
        rules.through_via = ViaDefinition(
            ViaType.THROUGH, 0.3, 0.15, start_layer=0, end_layer=1
        )
        best = rules.get_best_via(0, 5, 6)  # Request 0->5 but via only goes 0->1
        assert best is None


class TestHeuristics:
    """Tests for heuristic classes."""

    def test_manhattan_heuristic(self):
        """Test Manhattan distance heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules
        )
        heuristic = ManhattanHeuristic()

        # Distance from (0,0) to (10,10) = 20 * cost_straight
        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == 20.0

    def test_manhattan_heuristic_name(self):
        """Test heuristic name."""
        h = ManhattanHeuristic()
        assert h.name == "Manhattan"

    def test_direction_bias_heuristic(self):
        """Test direction bias heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=0, goal_layer=0, rules=rules
        )
        heuristic = DirectionBiasHeuristic(turn_penalty_factor=0.5)

        # Moving in goal direction
        estimate_aligned = heuristic.estimate(0, 0, 0, (1, 0), context)
        # Moving perpendicular
        estimate_perpendicular = heuristic.estimate(0, 0, 0, (0, 1), context)

        # Perpendicular should have higher cost
        assert estimate_perpendicular > estimate_aligned

    def test_direction_bias_heuristic_name(self):
        """Test direction bias name."""
        h = DirectionBiasHeuristic(turn_penalty_factor=0.5)
        assert "DirectionBias" in h.name
        assert "0.5" in h.name

    def test_congestion_aware_heuristic(self):
        """Test congestion-aware heuristic."""
        rules = DesignRules()

        def get_congestion_cost(x, y, layer):
            return 0.5  # Constant congestion

        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules,
            get_congestion_cost=get_congestion_cost
        )
        heuristic = CongestionAwareHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should include congestion cost
        assert estimate > 20.0

    def test_congestion_aware_heuristic_name(self):
        """Test congestion-aware name."""
        h = CongestionAwareHeuristic()
        assert h.name == "CongestionAware"

    def test_weighted_congestion_heuristic(self):
        """Test weighted congestion heuristic."""
        rules = DesignRules()

        def get_congestion_cost(x, y, layer):
            return 1.0

        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules,
            get_congestion_cost=get_congestion_cost
        )
        heuristic = WeightedCongestionHeuristic(num_samples=3, congestion_multiplier=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate > 20.0

    def test_weighted_congestion_heuristic_name(self):
        """Test weighted congestion name."""
        h = WeightedCongestionHeuristic(congestion_multiplier=2.0)
        assert "WeightedCongestion" in h.name
        assert "2.0" in h.name

    def test_greedy_heuristic(self):
        """Test greedy heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules
        )
        heuristic = GreedyHeuristic(greed_factor=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should be 2x Manhattan distance
        assert estimate == 40.0

    def test_greedy_heuristic_name(self):
        """Test greedy heuristic name."""
        h = GreedyHeuristic(greed_factor=2.0)
        assert "Greedy" in h.name
        assert "2.0" in h.name

    def test_default_heuristic(self):
        """Test default heuristic."""
        assert DEFAULT_HEURISTIC is not None
        assert isinstance(DEFAULT_HEURISTIC, CongestionAwareHeuristic)

    def test_heuristic_with_cost_multiplier(self):
        """Test heuristic with net class cost multiplier."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules,
            cost_multiplier=0.5  # Power net priority
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == 10.0  # 20 * 0.5

    def test_heuristic_layer_change(self):
        """Test heuristic with layer change."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=0, goal_y=0, goal_layer=1, rules=rules
        )
        heuristic = ManhattanHeuristic()

        # At goal position but different layer
        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == rules.cost_via  # Layer change cost


# =============================================================================
# RoutingGrid Tests
# =============================================================================

from kicad_tools.router.grid import RoutingGrid


class TestRoutingGrid:
    """Tests for RoutingGrid class."""

    def test_grid_creation(self):
        """Test creating a routing grid."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules)

        assert grid.width == 10.0
        assert grid.height == 10.0
        assert grid.resolution == 0.5
        assert grid.cols == 21  # (10 / 0.5) + 1
        assert grid.rows == 21

    def test_grid_with_origin(self):
        """Test grid with custom origin."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=100, origin_y=50)

        assert grid.origin_x == 100
        assert grid.origin_y == 50

    def test_world_to_grid(self):
        """Test world to grid coordinate conversion."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=0, origin_y=0)

        gx, gy = grid.world_to_grid(2.5, 3.5)
        assert gx == 5
        assert gy == 7

    def test_grid_to_world(self):
        """Test grid to world coordinate conversion."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=0, origin_y=0)

        x, y = grid.grid_to_world(5, 7)
        assert x == 2.5
        assert y == 3.5

    def test_world_to_grid_with_origin(self):
        """Test coordinate conversion with origin offset."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules, origin_x=100, origin_y=50)

        gx, gy = grid.world_to_grid(102.5, 53.5)
        assert gx == 5
        assert gy == 7

    def test_layer_stack_default(self):
        """Test default layer stack is 2-layer."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        assert grid.num_layers == 2
        assert grid.layers == 2  # Alias

    def test_layer_stack_custom(self):
        """Test custom layer stack."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        assert grid.num_layers == 4

    def test_layer_to_index(self):
        """Test layer enum to grid index mapping."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        idx = grid.layer_to_index(Layer.F_CU.value)
        assert idx == 0

    def test_index_to_layer(self):
        """Test grid index to layer enum mapping."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        layer_value = grid.index_to_layer(0)
        assert layer_value == Layer.F_CU.value

    def test_layer_to_index_invalid(self):
        """Test invalid layer value raises."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        with pytest.raises(ValueError):
            grid.layer_to_index(999)

    def test_index_to_layer_invalid(self):
        """Test invalid grid index raises."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        with pytest.raises(ValueError):
            grid.index_to_layer(999)

    def test_get_routable_indices(self):
        """Test getting routable layer indices."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        indices = grid.get_routable_indices()
        assert 0 in indices  # F.Cu
        assert 3 in indices  # B.Cu

    def test_is_plane_layer(self):
        """Test plane layer check."""
        rules = DesignRules()
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        grid = RoutingGrid(10.0, 10.0, rules, layer_stack=stack)

        assert grid.is_plane_layer(1) is True  # GND
        assert grid.is_plane_layer(0) is False  # F.Cu signal

    def test_congestion_tracking(self):
        """Test congestion tracking."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        # Initial congestion should be 0
        congestion = grid.get_congestion(5, 5, 0)
        assert congestion == 0.0

    def test_congestion_map(self):
        """Test congestion statistics."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        stats = grid.get_congestion_map()
        assert "max_congestion" in stats
        assert "avg_congestion" in stats
        assert "congested_regions" in stats
        assert stats["max_congestion"] == 0.0

    def test_add_obstacle(self):
        """Test adding obstacle to grid."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        obs = Obstacle(5.0, 5.0, 1.0, 1.0, Layer.F_CU)
        grid.add_obstacle(obs)

        # The obstacle should block some cells
        # Check center of obstacle region
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_add_pad(self):
        """Test adding pad to grid."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=1, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        # Pad should be added (verify grid was modified)
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        # Cell should be assigned to net
        assert cell.net == 1

    def test_grid_bounds_clamping(self):
        """Test coordinate clamping at grid boundaries."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(10.0, 10.0, rules)

        # Coordinates beyond grid should be clamped
        gx, gy = grid.world_to_grid(-10.0, -10.0)
        assert gx == 0
        assert gy == 0

        gx, gy = grid.world_to_grid(100.0, 100.0)
        assert gx == grid.cols - 1
        assert gy == grid.rows - 1


# =============================================================================
# Autorouter Tests
# =============================================================================

from kicad_tools.router.core import Autorouter


class TestAutorouter:
    """Tests for Autorouter class."""

    def test_autorouter_creation(self):
        """Test creating an autorouter."""
        router = Autorouter(width=50.0, height=50.0)

        assert router.grid is not None
        assert router.router is not None
        assert router.pads == {}
        assert router.nets == {}
        assert router.routes == []

    def test_autorouter_with_origin(self):
        """Test autorouter with custom origin."""
        router = Autorouter(width=50.0, height=50.0, origin_x=100, origin_y=100)

        assert router.grid.origin_x == 100
        assert router.grid.origin_y == 100

    def test_autorouter_with_rules(self):
        """Test autorouter with custom rules."""
        rules = DesignRules(trace_width=0.3, trace_clearance=0.2)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.2

    def test_autorouter_with_layer_stack(self):
        """Test autorouter with custom layer stack."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        router = Autorouter(width=50.0, height=50.0, layer_stack=stack)

        assert router.grid.num_layers == 4

    def test_add_component(self):
        """Test adding component pads."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "width": 0.5, "height": 0.5, "net": 1, "net_name": "VCC"},
            {"number": "2", "x": 12.0, "y": 10.0, "width": 0.5, "height": 0.5, "net": 2, "net_name": "GND"},
        ]
        router.add_component("U1", pads)

        assert ("U1", "1") in router.pads
        assert ("U1", "2") in router.pads
        assert 1 in router.nets
        assert 2 in router.nets

    def test_add_component_tracks_net_names(self):
        """Test that net names are tracked."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ]
        router.add_component("U1", pads)

        assert router.net_names[1] == "VCC"

    def test_add_component_no_net(self):
        """Test adding pad with no net (net=0)."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 0},
        ]
        router.add_component("U1", pads)

        assert ("U1", "1") in router.pads
        assert 0 not in router.nets  # Net 0 not tracked

    def test_add_obstacle(self):
        """Test adding obstacle."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_obstacle(25.0, 25.0, 5.0, 5.0, Layer.F_CU)

        # Verify grid was updated
        gx, gy = router.grid.world_to_grid(25.0, 25.0)
        cell = router.grid.grid[0][gy][gx]
        assert cell.blocked is True

    def test_get_statistics_empty(self):
        """Test statistics for empty router."""
        router = Autorouter(width=50.0, height=50.0)
        stats = router.get_statistics()

        assert stats["routes"] == 0
        assert stats["segments"] == 0
        assert stats["vias"] == 0

    def test_add_multiple_components(self):
        """Test adding multiple components."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
            {"number": "2", "x": 12.0, "y": 10.0, "net": 2},
        ])
        router.add_component("R1", [
            {"number": "1", "x": 20.0, "y": 10.0, "net": 1},
            {"number": "2", "x": 22.0, "y": 10.0, "net": 3},
        ])

        assert len(router.pads) == 4
        assert len(router.nets[1]) == 2  # Two pads on net 1

    def test_net_class_map(self):
        """Test net class map usage."""
        net_classes = create_net_class_map(
            power_nets=["VCC", "GND"],
            clock_nets=["CLK", "MCLK"],
        )
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        assert "VCC" in router.net_class_map
        assert "CLK" in router.net_class_map

    def test_through_hole_pad(self):
        """Test through-hole pad handling."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "through_hole": True, "drill": 0.8},
        ]
        router.add_component("J1", pads)

        pad = router.pads[("J1", "1")]
        assert pad.through_hole is True
        assert pad.drill == 0.8

    def test_pad_defaults(self):
        """Test pad default values."""
        router = Autorouter(width=50.0, height=50.0)

        pads = [
            {"number": "1", "x": 10.0, "y": 10.0},
        ]
        router.add_component("U1", pads)

        pad = router.pads[("U1", "1")]
        assert pad.width == 0.5  # Default
        assert pad.height == 0.5  # Default
        assert pad.net == 0  # Default
        assert pad.layer == Layer.F_CU  # Default

    def test_route_net_empty(self):
        """Test routing non-existent net."""
        router = Autorouter(width=50.0, height=50.0)
        routes = router.route_net(999)
        assert routes == []

    def test_route_net_single_pad(self):
        """Test routing net with only one pad (no route needed)."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])
        routes = router.route_net(1)
        assert routes == []

    def test_route_net_two_pads(self):
        """Test routing between two pads."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add two pads on the same net, reasonably close
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])
        router.add_component("R1", [
            {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])

        routes = router.route_net(1, use_mst=False)
        assert len(routes) >= 1

    def test_route_net_mst(self):
        """Test MST-based routing for multi-pad nets."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add three pads on the same net
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])
        router.add_component("U2", [
            {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])
        router.add_component("U3", [
            {"number": "1", "x": 15.0, "y": 20.0, "net": 1, "net_name": "VCC"},
        ])

        routes = router.route_net(1, use_mst=True)
        # MST should produce N-1 routes for N pads
        assert len(routes) >= 1

    def test_get_net_priority_with_net_class(self):
        """Test net priority calculation."""
        net_classes = create_net_class_map(power_nets=["VCC"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])

        priority, pad_count = router._get_net_priority(1)
        assert priority == 1  # Power net has highest priority
        assert pad_count == 1

    def test_get_net_priority_default(self):
        """Test net priority for unknown nets."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "UNKNOWN"},
        ])

        priority, pad_count = router._get_net_priority(1)
        assert priority == 10  # Default low priority

    def test_route_all_empty(self):
        """Test route_all with no nets."""
        router = Autorouter(width=50.0, height=50.0)
        routes = router.route_all()
        assert routes == []

    def test_route_all_skips_net_zero(self):
        """Test that route_all skips net 0."""
        router = Autorouter(width=50.0, height=50.0)
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 0},  # No net
        ])
        routes = router.route_all()
        assert routes == []

    def test_to_sexp_empty(self):
        """Test sexp generation for empty router."""
        router = Autorouter(width=50.0, height=50.0)
        sexp = router.to_sexp()
        assert sexp == ""

    def test_evaluate_solution_empty(self):
        """Test solution evaluation with no routes."""
        router = Autorouter(width=50.0, height=50.0)
        score = router._evaluate_solution([])
        assert score == 0.0

    def test_evaluate_solution_with_routes(self):
        """Test solution evaluation with routes."""
        router = Autorouter(width=50.0, height=50.0)

        # Add a net so total_nets > 0
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
            {"number": "2", "x": 20.0, "y": 10.0, "net": 1},
        ])

        route = Route(net=1, net_name="test")
        route.segments.append(Segment(10, 10, 20, 10, 0.2, Layer.F_CU, net=1))

        score = router._evaluate_solution([route])
        assert score > 0

    def test_reset_for_new_trial(self):
        """Test router reset for monte carlo trials."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add pads
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
        ])

        # Add a fake route
        router.routes.append(Route(net=1, net_name="VCC"))

        # Reset
        router._reset_for_new_trial()

        # Pads should still be there, routes should be cleared
        assert ("U1", "1") in router.pads
        assert router.routes == []

    def test_shuffle_within_tiers(self):
        """Test net shuffling preserves priority tiers."""
        net_classes = create_net_class_map(power_nets=["VCC", "GND"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        # Add power nets (priority 1) and signal nets (priority 10)
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            {"number": "2", "x": 12.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            {"number": "3", "x": 14.0, "y": 10.0, "net": 2, "net_name": "GND"},
            {"number": "4", "x": 16.0, "y": 10.0, "net": 2, "net_name": "GND"},
        ])
        router.add_component("U2", [
            {"number": "1", "x": 20.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
            {"number": "2", "x": 22.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
        ])

        net_order = [1, 2, 3]  # VCC, GND, SIG1
        shuffled = router._shuffle_within_tiers(net_order)

        # Power nets should come before signal nets
        assert set(shuffled[:2]) == {1, 2}  # Power nets first
        assert shuffled[2] == 3  # Signal net last

    def test_create_intra_ic_routes(self):
        """Test intra-IC route creation for same-component pins."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add component with two pins on the same net, close together
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            {"number": "2", "x": 11.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 1mm apart
        ])

        pads = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads)

        # Should create a direct route between these close pins
        assert len(routes) == 1
        assert len(connected) == 2

    def test_create_intra_ic_routes_too_far(self):
        """Test that intra-IC routes not created for distant pins."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add component with two pins on the same net, far apart
        router.add_component("U1", [
            {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 10mm apart
        ])

        pads = router.nets[1]
        routes, connected = router._create_intra_ic_routes(1, pads)

        # Too far apart, no intra-IC route
        assert len(routes) == 0
        assert len(connected) == 0

    def test_get_statistics_with_routes(self):
        """Test statistics with actual routes."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add a route manually
        route = Route(net=1, net_name="test")
        route.segments.append(Segment(10, 10, 20, 10, 0.2, Layer.F_CU, net=1))
        route.vias.append(Via(15, 10, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1))
        router.routes.append(route)

        stats = router.get_statistics()

        assert stats["routes"] == 1
        assert stats["segments"] == 1
        assert stats["vias"] == 1
        assert stats["total_length_mm"] == 10.0  # 20-10 = 10mm


# =============================================================================
# RoutingResult Tests
# =============================================================================

from kicad_tools.router.core import RoutingResult, AdaptiveAutorouter


class TestRoutingResult:
    """Tests for RoutingResult dataclass."""

    def test_routing_result_creation(self):
        """Test creating a routing result."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=8,
            overflow=2,
            converged=False,
            iterations_used=5,
            statistics={"routes": 8},
        )

        assert result.layer_count == 2
        assert result.nets_requested == 10
        assert result.nets_routed == 8
        assert result.converged is False

    def test_routing_result_success_rate(self):
        """Test success rate calculation."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[], layer_count=2, layer_stack=stack,
            nets_requested=10, nets_routed=8, overflow=0,
            converged=True, iterations_used=1, statistics={},
        )

        assert result.success_rate == 0.8

    def test_routing_result_success_rate_zero_nets(self):
        """Test success rate with zero nets."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[], layer_count=2, layer_stack=stack,
            nets_requested=0, nets_routed=0, overflow=0,
            converged=True, iterations_used=1, statistics={},
        )

        assert result.success_rate == 1.0

    def test_routing_result_str(self):
        """Test string representation."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[], layer_count=2, layer_stack=stack,
            nets_requested=10, nets_routed=10, overflow=0,
            converged=True, iterations_used=1, statistics={},
        )

        s = str(result)
        assert "CONVERGED" in s
        assert "2L" in s
        assert "10/10" in s


# =============================================================================
# Router (Pathfinder) Tests
# =============================================================================

from kicad_tools.router.pathfinder import Router, AStarNode


class TestAStarNode:
    """Tests for AStarNode dataclass."""

    def test_astar_node_creation(self):
        """Test creating an A* node."""
        node = AStarNode(f_score=10.0, g_score=5.0, x=3, y=4, layer=0)

        assert node.f_score == 10.0
        assert node.g_score == 5.0
        assert node.x == 3
        assert node.y == 4
        assert node.layer == 0
        assert node.parent is None
        assert node.via_from_parent is False

    def test_astar_node_ordering(self):
        """Test node ordering by f_score."""
        node1 = AStarNode(f_score=10.0, g_score=5.0, x=0, y=0, layer=0)
        node2 = AStarNode(f_score=5.0, g_score=3.0, x=1, y=1, layer=0)

        # Lower f_score should come first
        assert node2 < node1

    def test_astar_node_with_parent(self):
        """Test node with parent reference."""
        parent = AStarNode(f_score=5.0, g_score=2.0, x=0, y=0, layer=0)
        child = AStarNode(f_score=10.0, g_score=5.0, x=1, y=0, layer=0, parent=parent)

        assert child.parent is parent

    def test_astar_node_via(self):
        """Test node representing a via transition."""
        node = AStarNode(
            f_score=15.0, g_score=10.0, x=5, y=5, layer=1,
            parent=None, via_from_parent=True
        )

        assert node.via_from_parent is True

    def test_astar_node_direction(self):
        """Test node direction tracking."""
        node = AStarNode(
            f_score=10.0, g_score=5.0, x=1, y=0, layer=0,
            direction=(1, 0)  # Moving right
        )

        assert node.direction == (1, 0)


class TestRouterPathfinder:
    """Tests for Router (pathfinder) class."""

    def test_router_creation(self):
        """Test creating a router."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        assert router.grid is grid
        assert router.rules is rules
        assert router.heuristic is not None

    def test_router_with_custom_heuristic(self):
        """Test router with custom heuristic."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        heuristic = ManhattanHeuristic()
        router = Router(grid, rules, heuristic=heuristic)

        assert router.heuristic is heuristic

    def test_router_get_net_class(self):
        """Test getting net class for a net."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        net_classes = create_net_class_map(power_nets=["VCC"])
        router = Router(grid, rules, net_class_map=net_classes)

        nc = router._get_net_class("VCC")
        assert nc is not None
        assert nc.name == "Power"

        nc_unknown = router._get_net_class("UNKNOWN")
        assert nc_unknown is None

    def test_router_route_simple(self):
        """Test simple routing between two pads."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Create two pads on the same layer
        start_pad = Pad(x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)
        end_pad = Pad(x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)

        # Add pads to grid
        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None
        assert route.net == 1
        assert len(route.segments) > 0

    def test_router_route_with_weight(self):
        """Test weighted A* routing."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        start_pad = Pad(x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)
        end_pad = Pad(x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        # Higher weight = faster but potentially suboptimal
        route = router.route(start_pad, end_pad, weight=2.0)

        assert route is not None

    def test_router_route_blocked(self):
        """Test routing around obstacles."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Create obstacle between pads
        obstacle = Obstacle(x=25.0, y=25.0, width=5.0, height=20.0, layer=Layer.F_CU)
        grid.add_obstacle(obstacle)

        start_pad = Pad(x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)
        end_pad = Pad(x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        # Should still find a route (going around)
        assert route is not None

    def test_router_is_trace_blocked(self):
        """Test trace blocking check."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].is_obstacle = True

        # Check that trace is blocked at that location
        blocked = router._is_trace_blocked(10, 10, 0, net=1)
        assert blocked is True

    def test_router_is_trace_blocked_same_net(self):
        """Test trace not blocked by same net cells."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell with same net (e.g., a pad)
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].net = 1
        grid.grid[0][10][10].is_obstacle = False

        # Should not be blocked for same net
        blocked = router._is_trace_blocked(10, 10, 0, net=1)
        assert blocked is False

    def test_router_is_via_blocked(self):
        """Test via blocking check."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Block a cell with obstacle
        grid.grid[0][10][10].blocked = True
        grid.grid[0][10][10].is_obstacle = True

        blocked = router._is_via_blocked(10, 10, 0, net=1)
        assert blocked is True

    def test_router_get_congestion_cost(self):
        """Test congestion cost calculation."""
        rules = DesignRules(grid_resolution=0.5, congestion_threshold=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Without congestion
        cost = router._get_congestion_cost(10, 10, 0)
        assert cost == 0.0

    def test_router_reconstructs_route_correctly(self):
        """Test route reconstruction from A* nodes."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        start_pad = Pad(x=10.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)
        end_pad = Pad(x=15.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU)

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None
        # Check route properties
        assert route.net == 1
        assert route.net_name == "test"
        # Should have segments connecting the pads
        if route.segments:
            first_seg = route.segments[0]
            assert first_seg.layer == Layer.F_CU

    def test_router_through_hole_pads(self):
        """Test routing with through-hole pads."""
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Through-hole pads can be accessed from any layer
        start_pad = Pad(
            x=10.0, y=25.0, width=1.0, height=1.0, net=1, net_name="test",
            layer=Layer.F_CU, through_hole=True, drill=0.8
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=1.0, height=1.0, net=1, net_name="test",
            layer=Layer.F_CU, through_hole=True, drill=0.8
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router.route(start_pad, end_pad)

        assert route is not None


# =============================================================================
# AdaptiveAutorouter Tests
# =============================================================================


class TestAdaptiveAutorouter:
    """Tests for AdaptiveAutorouter class."""

    def test_adaptive_autorouter_creation(self):
        """Test creating adaptive autorouter."""
        components = [
            {
                "ref": "U1", "x": 10.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ]
            }
        ]
        net_map = {"VCC": 1, "GND": 2}

        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=components,
            net_map=net_map,
        )

        assert adaptive.width == 50.0
        assert adaptive.height == 50.0
        assert adaptive.max_layers == 6

    def test_adaptive_autorouter_skip_nets(self):
        """Test skip nets parameter."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=components,
            net_map=net_map,
            skip_nets=["GND", "VCC"],
        )

        assert "GND" in adaptive.skip_nets
        assert "VCC" in adaptive.skip_nets

    def test_adaptive_autorouter_max_layers(self):
        """Test max layers limit."""
        components = []
        net_map = {}

        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=components,
            net_map=net_map,
            max_layers=4,
        )

        assert adaptive.max_layers == 4

    def test_adaptive_layer_stacks(self):
        """Test layer stack progression."""
        assert len(AdaptiveAutorouter.LAYER_STACKS) == 3
        assert AdaptiveAutorouter.LAYER_STACKS[0].num_layers == 2
        assert AdaptiveAutorouter.LAYER_STACKS[1].num_layers == 4
        assert AdaptiveAutorouter.LAYER_STACKS[2].num_layers == 6

    def test_adaptive_to_sexp_no_route(self):
        """Test to_sexp before routing raises error."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.to_sexp()

    def test_adaptive_get_routes_no_route(self):
        """Test get_routes before routing raises error."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.get_routes()

    def test_adaptive_layer_count_no_route(self):
        """Test layer_count before routing returns 0."""
        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=[],
            net_map={},
        )

        assert adaptive.layer_count == 0

    def test_adaptive_check_convergence(self):
        """Test convergence checking."""
        components = [
            {
                "ref": "U1", "x": 10.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ]
            }
        ]
        net_map = {"NET1": 1}

        adaptive = AdaptiveAutorouter(
            width=50.0, height=50.0,
            components=components,
            net_map=net_map,
            verbose=False,
        )

        # Create a router to test convergence check
        stack = LayerStack.two_layer()
        router = adaptive._create_autorouter(stack)

        # No routes yet, no overflow
        converged = adaptive._check_convergence(router, overflow=0)
        # Should not converge because no nets are routed
        assert converged is False


# =============================================================================
# Router I/O Tests
# =============================================================================

from kicad_tools.router.io import route_pcb, load_pcb_for_routing


class TestRoutePcb:
    """Tests for route_pcb function."""

    def test_route_pcb_basic(self):
        """Test basic route_pcb function."""
        components = [
            {
                "ref": "U1", "x": 10.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ]
            },
            {
                "ref": "R1", "x": 30.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ]
            },
        ]
        net_map = {"VCC": 1, "GND": 2}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        # Should return some routing data
        assert isinstance(sexp, str)
        assert isinstance(stats, dict)
        assert "routes" in stats
        assert "segments" in stats
        assert "vias" in stats

    def test_route_pcb_with_rotation(self):
        """Test route_pcb with rotated components."""
        components = [
            {
                "ref": "U1", "x": 15.0, "y": 15.0, "rotation": 90,
                "pads": [
                    {"number": "1", "x": 0.0, "y": -2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 0.0, "y": 2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ]
            },
            {
                "ref": "R1", "x": 35.0, "y": 15.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ]
            },
        ]
        net_map = {"SIG": 1, "GND": 2}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        assert isinstance(sexp, str)
        assert stats["routes"] >= 0

    def test_route_pcb_skip_nets(self):
        """Test route_pcb with skip_nets parameter."""
        components = [
            {
                "ref": "U1", "x": 10.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                    {"number": "3", "x": 4.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ]
            },
            {
                "ref": "R1", "x": 30.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ]
            },
        ]
        net_map = {"VCC": 1, "GND": 2, "SIG": 3}

        # Skip VCC and GND (power/ground planes)
        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
            skip_nets=["VCC", "GND"],
        )

        assert isinstance(sexp, str)

    def test_route_pcb_with_origin(self):
        """Test route_pcb with custom origin."""
        components = [
            {
                "ref": "U1", "x": 110.0, "y": 60.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ]
            },
        ]
        net_map = {"NET1": 1}

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
            origin_x=100.0,
            origin_y=50.0,
        )

        assert isinstance(sexp, str)

    def test_route_pcb_assigns_new_net_numbers(self):
        """Test that route_pcb assigns net numbers for unknown nets."""
        components = [
            {
                "ref": "U1", "x": 10.0, "y": 10.0, "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NEW_NET"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NEW_NET"},
                ]
            },
        ]
        net_map = {}  # Empty net map

        sexp, stats = route_pcb(
            board_width=50.0,
            board_height=50.0,
            components=components,
            net_map=net_map,
        )

        # Net map should have been updated
        assert "NEW_NET" in net_map


class TestLoadPcbForRouting:
    """Tests for load_pcb_for_routing function."""

    def test_load_pcb_basic(self, routing_test_pcb):
        """Test loading a PCB file for routing."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        assert router is not None
        assert isinstance(net_map, dict)
        assert len(net_map) > 0
        # Check expected nets
        assert "NET1" in net_map
        assert "GND" in net_map
        assert "+3.3V" in net_map

    def test_load_pcb_dimensions(self, routing_test_pcb):
        """Test that board dimensions are parsed correctly."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # gr_rect defines edge cuts from (100,100) to (150,140)
        assert router.grid.width == 50.0  # 150 - 100
        assert router.grid.height == 40.0  # 140 - 100
        assert router.grid.origin_x == 100.0
        assert router.grid.origin_y == 100.0

    def test_load_pcb_with_skip_nets(self, routing_test_pcb):
        """Test loading PCB with skip_nets."""
        router, net_map = load_pcb_for_routing(
            str(routing_test_pcb),
            skip_nets=["GND", "+3.3V"]
        )

        assert router is not None
        # Skipped nets should still be in net_map
        assert "GND" in net_map

    def test_load_pcb_with_netlist_override(self, routing_test_pcb):
        """Test loading PCB with netlist overrides."""
        netlist = {
            "R1.1": "OVERRIDE_NET",
        }

        router, net_map = load_pcb_for_routing(
            str(routing_test_pcb),
            netlist=netlist,
        )

        # Override net should be in net_map
        assert "OVERRIDE_NET" in net_map

    def test_load_pcb_with_custom_rules(self, routing_test_pcb):
        """Test loading PCB with custom design rules."""
        rules = DesignRules(
            trace_width=0.3,
            trace_clearance=0.25,
            grid_resolution=0.5,
        )

        router, net_map = load_pcb_for_routing(
            str(routing_test_pcb),
            rules=rules,
        )

        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.25

    def test_load_pcb_components_added(self, routing_test_pcb):
        """Test that components are added to router."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # Check that pads were added
        assert len(router.pads) > 0

        # Check that nets were registered
        assert len(router.nets) > 0

    def test_load_pcb_through_hole_detection(self, routing_test_pcb):
        """Test that through-hole pads are detected correctly."""
        router, net_map = load_pcb_for_routing(str(routing_test_pcb))

        # J1 has through-hole pads
        # Look for through-hole pads
        has_through_hole = any(pad.through_hole for pad in router.pads.values())
        assert has_through_hole

    def test_load_pcb_default_dimensions(self, tmp_path):
        """Test default dimensions when no edge cuts present."""
        # Create a PCB without gr_rect
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "NET1")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 80)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
    (pad "2" smd rect (at 5 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)
"""
        pcb_file = tmp_path / "no_edge.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should use default HAT dimensions
        assert router.grid.width == 65.0
        assert router.grid.height == 56.0
