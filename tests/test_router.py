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
