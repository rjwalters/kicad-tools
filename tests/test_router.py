"""Tests for router modules (primitives, rules, layers, heuristics)."""

import pytest

from kicad_tools.exceptions import RoutingError
from kicad_tools.router.heuristics import (
    DEFAULT_HEURISTIC,
    DIAGONAL_COST,
    CongestionAwareHeuristic,
    DirectionBiasHeuristic,
    GreedyHeuristic,
    HeuristicContext,
    ManhattanHeuristic,
    WeightedCongestionHeuristic,
    octile_distance,
)
from kicad_tools.router.layers import (
    Layer,
    LayerDefinition,
    LayerStack,
    LayerType,
    ViaDefinition,
    ViaRules,
    ViaType,
)
from kicad_tools.router.primitives import GridCell, Obstacle, Pad, Point, Route, Segment, Via
from kicad_tools.router.rules import (
    DEFAULT_NET_CLASS_MAP,
    NET_CLASS_CLOCK,
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
    create_net_class_map,
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

    def test_grid_cell_pad_ownership_fields(self):
        """Test pad ownership tracking fields."""
        cell = GridCell(0, 0, 0, pad_blocked=True, original_net=5)
        assert cell.pad_blocked is True
        assert cell.original_net == 5

    def test_grid_cell_pad_ownership_defaults(self):
        """Test pad ownership tracking field defaults."""
        cell = GridCell(0, 0, 0)
        assert cell.pad_blocked is False
        assert cell.original_net == 0


class TestVia:
    """Tests for Via class."""

    def test_via_creation(self):
        """Test creating a via."""
        via = Via(
            x=10.0,
            y=20.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=1,
            net_name="GND",
        )
        assert via.x == 10.0
        assert via.y == 20.0
        assert via.drill == 0.3
        assert via.diameter == 0.6
        assert via.layers == (Layer.F_CU, Layer.B_CU)

    def test_via_to_sexp(self):
        """Test via S-expression generation."""
        via = Via(x=10.0, y=20.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
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
        seg = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1, net_name="VCC")
        assert seg.x1 == 0
        assert seg.x2 == 10
        assert seg.width == 0.2

    def test_segment_to_sexp(self):
        """Test segment S-expression generation."""
        seg = Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1)
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
            x=10, y=20, width=1.0, height=1.0, net=1, net_name="VCC", layer=Layer.F_CU, ref="U1"
        )
        assert pad.x == 10
        assert pad.y == 20
        assert pad.width == 1.0
        assert pad.ref == "U1"

    def test_pad_through_hole(self):
        """Test through-hole pad."""
        pad = Pad(
            x=0, y=0, width=2.0, height=2.0, net=1, net_name="GND", through_hole=True, drill=1.0
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
        rules = DesignRules(trace_width=0.15, trace_clearance=0.15, via_drill=0.25)
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
        with pytest.raises(RoutingError, match="Invalid layer stack"):
            LayerStack(
                [
                    LayerDefinition("F.Cu", 0, LayerType.SIGNAL),
                    LayerDefinition("B.Cu", 5, LayerType.SIGNAL),  # Gap in indices
                ]
            )

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


class TestDetectLayerStack:
    """Tests for detect_layer_stack function."""

    def test_detect_2_layer_board(self):
        """Test detecting a 2-layer board."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 2
        assert "2-Layer" in stack.name

    def test_detect_4_layer_board_with_planes(self):
        """Test detecting a 4-layer board with inner planes."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (1 "In1.Cu" signal)
                (2 "In2.Cu" signal)
                (31 "B.Cu" signal)
            )
            (zone
                (net 1)
                (net_name "GND")
                (layer "In1.Cu")
            )
            (zone
                (net 2)
                (net_name "+3V3")
                (layer "In2.Cu")
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 4
        assert "4-Layer" in stack.name
        # Inner layers should be planes
        assert len(stack.plane_layers) == 2
        assert len(stack.signal_layers) == 2

    def test_detect_4_layer_board_no_zones(self):
        """Test detecting a 4-layer board without zones."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (1 "In1.Cu" signal)
                (2 "In2.Cu" signal)
                (31 "B.Cu" signal)
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 4
        # Without zones, should use signal configuration

    def test_detect_no_layers_fallback(self):
        """Test fallback when no layers section found."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = "(kicad_pcb )"
        stack = detect_layer_stack(pcb_text)
        # Should fall back to 2-layer
        assert stack.num_layers == 2


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
            via_type=ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15, start_layer=0, end_layer=5
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
            ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15, start_layer=0, end_layer=3
        )
        assert via_def.spans_layer(0, 4) is True
        assert via_def.spans_layer(2, 4) is True
        assert via_def.spans_layer(5, 6) is False

    def test_via_blocks_layer(self):
        """Test via blocking check."""
        via_def = ViaDefinition(
            ViaType.THROUGH,
            drill_mm=0.3,
            annular_ring_mm=0.15,
            start_layer=0,
            end_layer=-1,  # -1 = bottom
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
        rules.through_via = ViaDefinition(ViaType.THROUGH, 0.3, 0.15, start_layer=0, end_layer=1)
        best = rules.get_best_via(0, 5, 6)  # Request 0->5 but via only goes 0->1
        assert best is None


class TestHeuristics:
    """Tests for heuristic classes."""

    def test_manhattan_heuristic(self):
        """Test Manhattan distance heuristic."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            diagonal_routing=False,  # Use Manhattan distance
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
        context = HeuristicContext(goal_x=10, goal_y=0, goal_layer=0, rules=rules)
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
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            get_congestion_cost=get_congestion_cost,
            diagonal_routing=False,  # Use Manhattan distance for predictable base
        )
        heuristic = CongestionAwareHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should include congestion cost (base Manhattan = 20)
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
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            get_congestion_cost=get_congestion_cost,
            diagonal_routing=False,  # Use Manhattan distance for predictable base
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
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            diagonal_routing=False,  # Use Manhattan distance
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
            goal_x=10,
            goal_y=10,
            goal_layer=0,
            rules=rules,
            cost_multiplier=0.5,  # Power net priority
            diagonal_routing=False,  # Use Manhattan distance
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        assert estimate == 10.0  # 20 * 0.5

    def test_heuristic_layer_change(self):
        """Test heuristic with layer change."""
        rules = DesignRules()
        context = HeuristicContext(goal_x=0, goal_y=0, goal_layer=1, rules=rules)
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

        with pytest.raises(RoutingError):
            grid.layer_to_index(999)

    def test_index_to_layer_invalid(self):
        """Test invalid grid index raises."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        with pytest.raises(RoutingError):
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

    def test_add_pad_sets_pad_ownership(self):
        """Test that add_pad sets pad_blocked and original_net fields."""
        rules = DesignRules()
        grid = RoutingGrid(10.0, 10.0, rules)

        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=3, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]

        # Pad cells should be marked as pad-blocked with original_net set
        assert cell.pad_blocked is True
        assert cell.original_net == 3
        assert cell.net == 3

    def test_unmark_route_preserves_pad_cells(self):
        """Test that unmarking a route doesn't corrupt pad cells.

        This is the key bug fix from issue #294: when a route passes over a pad
        cell and then gets ripped up, the pad cell should remain blocked with
        its original net, not be cleared to net=0.
        """
        rules = DesignRules(trace_clearance=0.1, trace_width=0.2)
        grid = RoutingGrid(10.0, 10.0, rules)

        # Add a pad at (5, 5) with net=3
        pad = Pad(x=5.0, y=5.0, width=0.5, height=0.5, net=3, net_name="VCC", layer=Layer.F_CU)
        grid.add_pad(pad)

        # Verify pad cell state before marking route
        gx, gy = grid.world_to_grid(5.0, 5.0)
        cell = grid.grid[0][gy][gx]
        assert cell.pad_blocked is True
        assert cell.original_net == 3
        assert cell.net == 3
        assert cell.blocked is True

        # Create a route that passes through the pad area (same net)
        route = Route(net=3, net_name="VCC")
        route.segments.append(
            Segment(x1=4.0, y1=5.0, x2=6.0, y2=5.0, width=0.2, layer=Layer.F_CU, net=3)
        )

        # Mark the route
        grid.mark_route(route)

        # Cell should still be blocked with net=3
        assert cell.blocked is True
        assert cell.net == 3

        # Now unmark (rip-up) the route
        grid.unmark_route(route)

        # BUG FIX: Pad cell should STILL be blocked with its original net
        # Before the fix, this would have been cleared to blocked=False, net=0
        assert cell.blocked is True, "Pad cell should remain blocked after route rip-up"
        assert cell.net == 3, "Pad cell should retain its original net after route rip-up"
        assert cell.pad_blocked is True, "pad_blocked flag should be preserved"

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
                "x": 12.0,
                "y": 10.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "GND",
            },
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

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 12.0, "y": 10.0, "net": 2},
            ],
        )
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 22.0, "y": 10.0, "net": 3},
            ],
        )

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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        routes = router.route_net(1)
        assert routes == []

    def test_route_net_two_pads(self):
        """Test routing between two pads."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add two pads on the same net, reasonably close
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

        routes = router.route_net(1, use_mst=False)
        assert len(routes) >= 1

    def test_route_net_mst(self):
        """Test MST-based routing for multi-pad nets."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add three pads on the same net
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )
        router.add_component(
            "U3",
            [
                {"number": "1", "x": 15.0, "y": 20.0, "net": 1, "net_name": "VCC"},
            ],
        )

        routes = router.route_net(1, use_mst=True)
        # MST should produce N-1 routes for N pads
        assert len(routes) >= 1

    def test_get_net_priority_with_net_class(self):
        """Test net priority calculation."""
        net_classes = create_net_class_map(power_nets=["VCC"])
        router = Autorouter(width=50.0, height=50.0, net_class_map=net_classes)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

        priority, pad_count = router._get_net_priority(1)
        assert priority == 1  # Power net has highest priority
        assert pad_count == 1

    def test_get_net_priority_default(self):
        """Test net priority for unknown nets."""
        router = Autorouter(width=50.0, height=50.0)

        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "UNKNOWN"},
            ],
        )

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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 0},  # No net
            ],
        )
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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1},
            ],
        )

        route = Route(net=1, net_name="test")
        route.segments.append(Segment(10, 10, 20, 10, 0.2, Layer.F_CU, net=1))

        score = router._evaluate_solution([route])
        assert score > 0

    def test_reset_for_new_trial(self):
        """Test router reset for monte carlo trials."""
        rules = DesignRules(grid_resolution=0.5)
        router = Autorouter(width=50.0, height=50.0, rules=rules)

        # Add pads
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
            ],
        )

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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 12.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "3", "x": 14.0, "y": 10.0, "net": 2, "net_name": "GND"},
                {"number": "4", "x": 16.0, "y": 10.0, "net": 2, "net_name": "GND"},
            ],
        )
        router.add_component(
            "U2",
            [
                {"number": "1", "x": 20.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
                {"number": "2", "x": 22.0, "y": 10.0, "net": 3, "net_name": "SIG1"},
            ],
        )

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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 11.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 1mm apart
            ],
        )

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
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "VCC"},
                {"number": "2", "x": 20.0, "y": 10.0, "net": 1, "net_name": "VCC"},  # 10mm apart
            ],
        )

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

from kicad_tools.router.core import AdaptiveAutorouter, RoutingResult


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
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=8,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        assert result.success_rate == 0.8

    def test_routing_result_success_rate_zero_nets(self):
        """Test success rate with zero nets."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=0,
            nets_routed=0,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        assert result.success_rate == 1.0

    def test_routing_result_str(self):
        """Test string representation."""
        stack = LayerStack.two_layer()
        result = RoutingResult(
            routes=[],
            layer_count=2,
            layer_stack=stack,
            nets_requested=10,
            nets_routed=10,
            overflow=0,
            converged=True,
            iterations_used=1,
            statistics={},
        )

        s = str(result)
        assert "CONVERGED" in s
        assert "2L" in s
        assert "10/10" in s


# =============================================================================
# Router (Pathfinder) Tests
# =============================================================================

from kicad_tools.router.pathfinder import AStarNode, Router


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
            f_score=15.0, g_score=10.0, x=5, y=5, layer=1, parent=None, via_from_parent=True
        )

        assert node.via_from_parent is True

    def test_astar_node_direction(self):
        """Test node direction tracking."""
        node = AStarNode(
            f_score=10.0,
            g_score=5.0,
            x=1,
            y=0,
            layer=0,
            direction=(1, 0),  # Moving right
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
        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

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

        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

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

        start_pad = Pad(
            x=10.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=40.0, y=25.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

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

        start_pad = Pad(
            x=10.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=15.0, y=10.0, width=0.5, height=0.5, net=1, net_name="test", layer=Layer.F_CU
        )

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
            x=10.0,
            y=25.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="test",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
        )
        end_pad = Pad(
            x=40.0,
            y=25.0,
            width=1.0,
            height=1.0,
            net=1,
            net_name="test",
            layer=Layer.F_CU,
            through_hole=True,
            drill=0.8,
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
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            }
        ]
        net_map = {"VCC": 1, "GND": 2}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
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
            width=50.0,
            height=50.0,
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
            width=50.0,
            height=50.0,
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
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.to_sexp()

    def test_adaptive_get_routes_no_route(self):
        """Test get_routes before routing raises error."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        with pytest.raises(ValueError, match="No routing result"):
            adaptive.get_routes()

    def test_adaptive_layer_count_no_route(self):
        """Test layer_count before routing returns 0."""
        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
            components=[],
            net_map={},
        )

        assert adaptive.layer_count == 0

    def test_adaptive_check_convergence(self):
        """Test convergence checking."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ],
            }
        ]
        net_map = {"NET1": 1}

        adaptive = AdaptiveAutorouter(
            width=50.0,
            height=50.0,
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

from kicad_tools.router.io import load_pcb_for_routing, route_pcb


class TestRoutePcb:
    """Tests for route_pcb function."""

    def test_route_pcb_basic(self):
        """Test basic route_pcb function."""
        components = [
            {
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
            },
            {
                "ref": "R1",
                "x": 30.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
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
                "ref": "U1",
                "x": 15.0,
                "y": 15.0,
                "rotation": 90,
                "pads": [
                    {"number": "1", "x": 0.0, "y": -2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 0.0, "y": 2.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
            },
            {
                "ref": "R1",
                "x": 35.0,
                "y": 15.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                ],
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
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "GND"},
                    {"number": "3", "x": 4.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
            },
            {
                "ref": "R1",
                "x": 30.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "VCC"},
                    {"number": "2", "x": 2.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "SIG"},
                ],
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
                "ref": "U1",
                "x": 110.0,
                "y": 60.0,
                "rotation": 0,
                "pads": [
                    {"number": "1", "x": 0.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                    {"number": "2", "x": 5.0, "y": 0.0, "width": 0.5, "height": 0.5, "net": "NET1"},
                ],
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
                "ref": "U1",
                "x": 10.0,
                "y": 10.0,
                "rotation": 0,
                "pads": [
                    {
                        "number": "1",
                        "x": 0.0,
                        "y": 0.0,
                        "width": 0.5,
                        "height": 0.5,
                        "net": "NEW_NET",
                    },
                    {
                        "number": "2",
                        "x": 5.0,
                        "y": 0.0,
                        "width": 0.5,
                        "height": 0.5,
                        "net": "NEW_NET",
                    },
                ],
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
        router, net_map = load_pcb_for_routing(str(routing_test_pcb), skip_nets=["GND", "+3.3V"])

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

    def test_load_pcb_unquoted_pad_numbers(self, tmp_path):
        """Test parsing pads with unquoted numeric pad numbers (Issue #173).

        KiCad uses unquoted pad numbers for numeric pads:
            (pad 1 smd rect ...)
        But quoted for alphanumeric (BGA):
            (pad "A1" smd rect ...)
        """
        # Create a PCB with UNQUOTED pad numbers (real KiCad format)
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (net 3 "SIG")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "U1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 smd rect (at -1.905 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at -0.635 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 3 smd rect (at 0.635 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 3 "SIG"))
    (pad 4 smd rect (at 1.905 -2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 5 smd rect (at 1.905 2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 6 smd rect (at 0.635 2.475) (size 0.6 1.2) (layers "F.Cu") (net 3 "SIG"))
    (pad 7 smd rect (at -0.635 2.475) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
    (pad 8 smd rect (at -1.905 2.475) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x04"
    (layer "F.Cu")
    (at 140 120)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS"))
    (pad 1 thru_hole oval (at 0 0) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 1 "VCC"))
    (pad 2 thru_hole oval (at 0 2.54) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
    (pad 3 thru_hole oval (at 0 5.08) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 3 "SIG"))
    (pad 4 thru_hole oval (at 0 7.62) (size 1.7 1.7) (drill 1.0) (layers "*.Cu") (net 2 "GND"))
  )
)
"""
        pcb_file = tmp_path / "unquoted_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all nets
        assert "VCC" in net_map
        assert "GND" in net_map
        assert "SIG" in net_map

        # Should have found all 12 pads (8 from U1 + 4 from J1)
        assert len(router.pads) == 12

        # Check specific pads were parsed correctly
        pad_refs = {ref for ref, _ in router.pads.keys()}
        assert "U1" in pad_refs
        assert "J1" in pad_refs

        # Check pad numbers were parsed (should be strings "1", "2", etc.)
        pad_nums = {num for _, num in router.pads.keys()}
        assert "1" in pad_nums
        assert "8" in pad_nums

    def test_load_pcb_mixed_quoted_unquoted_pads(self, tmp_path):
        """Test parsing PCB with both quoted and unquoted pad numbers.

        This tests the case where a board has both:
        - Numeric pads: (pad 1 smd ...) - unquoted
        - BGA pads: (pad "A1" smd ...) - quoted
        """
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 160 150) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "VCC")
  (net 2 "GND")
  (footprint "Package_SO:SOIC-8"
    (layer "F.Cu")
    (at 110 120)
    (fp_text reference "U1" (at 0 0) (layer "F.SilkS"))
    (pad 1 smd rect (at 0 0) (size 0.6 1.2) (layers "F.Cu") (net 1 "VCC"))
    (pad 2 smd rect (at 1.27 0) (size 0.6 1.2) (layers "F.Cu") (net 2 "GND"))
  )
  (footprint "Package_BGA:BGA-4"
    (layer "F.Cu")
    (at 140 130)
    (fp_text reference "U2" (at 0 0) (layer "F.SilkS"))
    (pad "A1" smd circle (at -0.5 -0.5) (size 0.4 0.4) (layers "F.Cu") (net 1 "VCC"))
    (pad "A2" smd circle (at 0.5 -0.5) (size 0.4 0.4) (layers "F.Cu") (net 2 "GND"))
    (pad "B1" smd circle (at -0.5 0.5) (size 0.4 0.4) (layers "F.Cu") (net 2 "GND"))
    (pad "B2" smd circle (at 0.5 0.5) (size 0.4 0.4) (layers "F.Cu") (net 1 "VCC"))
  )
)
"""
        pcb_file = tmp_path / "mixed_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all 6 pads (2 from U1 + 4 from U2)
        assert len(router.pads) == 6

        # Check both numeric and alphanumeric pad numbers
        pad_keys = set(router.pads.keys())
        assert ("U1", "1") in pad_keys  # Unquoted numeric
        assert ("U1", "2") in pad_keys  # Unquoted numeric
        assert ("U2", "A1") in pad_keys  # Quoted alphanumeric
        assert ("U2", "B2") in pad_keys  # Quoted alphanumeric

    def test_load_pcb_multiline_pad_format(self, tmp_path):
        """Test parsing pads in KiCad 7+ multi-line format.

        KiCad 7+ formats pads across multiple lines:
            (pad "1" smd roundrect
              (at -0.9500 0.9000)
              (size 0.6000 1.1000)
              (roundrect_rratio 0.25)
              (layers "F.Cu" "F.Paste" "F.Mask")
              (net 2 "+5V")
              (uuid "...")
            )

        This is different from single-line format that older versions used.
        """
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "+5V")
  (net 2 "GND")
  (net 3 "SIG")
  (footprint "SOT-23-5"
    (layer "F.Cu")
    (uuid "test-uuid-1")
    (at 120 120 180)
    (fp_text reference "U1"
      (at 0 -2.05 0)
      (layer "F.SilkS")
    )
    (pad "1" smd roundrect
      (at -0.9500 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "+5V")
      (uuid "pad-uuid-1")
    )
    (pad "2" smd roundrect
      (at 0.0000 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "GND")
      (uuid "pad-uuid-2")
    )
    (pad "3" smd roundrect
      (at 0.9500 0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 3 "SIG")
      (uuid "pad-uuid-3")
    )
    (pad "4" smd roundrect
      (at 0.9500 -0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "GND")
      (uuid "pad-uuid-4")
    )
    (pad "5" smd roundrect
      (at -0.9500 -0.9000)
      (size 0.6000 1.1000)
      (roundrect_rratio 0.25)
      (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "+5V")
      (uuid "pad-uuid-5")
    )
  )
  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x03"
    (layer "F.Cu")
    (uuid "test-uuid-2")
    (at 140 120)
    (fp_text reference "J1"
      (at 0 -3)
      (layer "F.SilkS")
    )
    (pad "1" thru_hole oval
      (at 0 0)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 1 "+5V")
      (uuid "pad-uuid-j1-1")
    )
    (pad "2" thru_hole oval
      (at 0 2.54)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 2 "GND")
      (uuid "pad-uuid-j1-2")
    )
    (pad "3" thru_hole oval
      (at 0 5.08)
      (size 1.7 1.7)
      (drill 1.0)
      (layers "*.Cu")
      (net 3 "SIG")
      (uuid "pad-uuid-j1-3")
    )
  )
)
"""
        pcb_file = tmp_path / "multiline_pads.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(str(pcb_file))

        # Should have found all nets
        assert "+5V" in net_map
        assert "GND" in net_map
        assert "SIG" in net_map

        # Should have found all 8 pads (5 from U1 + 3 from J1)
        assert len(router.pads) == 8, f"Expected 8 pads, got {len(router.pads)}"

        # Check specific pads were parsed correctly
        pad_refs = {ref for ref, _ in router.pads.keys()}
        assert "U1" in pad_refs
        assert "J1" in pad_refs

        # Check all pad numbers from U1 were parsed
        u1_pads = {num for ref, num in router.pads.keys() if ref == "U1"}
        assert u1_pads == {"1", "2", "3", "4", "5"}

        # Check J1 pads
        j1_pads = {num for ref, num in router.pads.keys() if ref == "J1"}
        assert j1_pads == {"1", "2", "3"}

        # Verify nets were assigned correctly to pads
        # U1 pin 1 should be on +5V (net 1)
        pad_u1_1 = router.pads.get(("U1", "1"))
        assert pad_u1_1 is not None
        assert pad_u1_1.net == 1  # +5V


# =============================================================================
# DRC Compliance Tests (Issue #267 - Router DRC Compliance)
# =============================================================================

import warnings

from kicad_tools.router.io import (
    PCBDesignRules,
    parse_pcb_design_rules,
    validate_grid_resolution,
    validate_routes,
)


class TestParsePcbDesignRules:
    """Tests for parse_pcb_design_rules function."""

    def test_returns_defaults_for_empty_setup(self):
        """Test that defaults are returned when setup section is empty."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (setup
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_track_width == 0.2
        assert rules.min_via_diameter == 0.6
        assert rules.min_via_drill == 0.3
        assert rules.min_clearance == 0.2

    def test_returns_defaults_for_no_setup(self):
        """Test that defaults are returned when no setup section exists."""
        pcb_text = """(kicad_pcb
  (version 20240108)
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_track_width == 0.2
        assert rules.min_clearance == 0.2

    def test_parses_net_class_clearance(self):
        """Test parsing clearance from net class definition."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (setup)
  (net_class "Default" "Default net class"
    (clearance 0.15)
    (trace_width 0.25)
    (via_dia 0.8)
    (via_drill 0.4)
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        assert rules.min_clearance == 0.15
        assert rules.min_track_width == 0.25
        assert rules.min_via_diameter == 0.8
        assert rules.min_via_drill == 0.4

    def test_uses_minimum_from_multiple_net_classes(self):
        """Test that minimum values are used across multiple net classes."""
        pcb_text = """(kicad_pcb
  (version 20240108)
  (net_class "Default"
    (clearance 0.2)
    (trace_width 0.25)
  )
  (net_class "HighSpeed"
    (clearance 0.1)
    (trace_width 0.15)
  )
)"""
        rules = parse_pcb_design_rules(pcb_text)

        # Should use the minimum values
        assert rules.min_clearance == 0.1
        assert rules.min_track_width == 0.15

    def test_to_design_rules_conversion(self):
        """Test converting PCBDesignRules to DesignRules."""
        pcb_rules = PCBDesignRules(
            min_track_width=0.15,
            min_via_diameter=0.5,
            min_via_drill=0.25,
            min_clearance=0.1,
        )

        design_rules = pcb_rules.to_design_rules()

        assert design_rules.trace_width == 0.15
        assert design_rules.via_diameter == 0.5
        assert design_rules.via_drill == 0.25
        assert design_rules.trace_clearance == 0.1
        # Grid resolution should be clearance / 2 for DRC compliance
        assert design_rules.grid_resolution == 0.05

    def test_to_design_rules_custom_grid(self):
        """Test converting with custom grid resolution."""
        pcb_rules = PCBDesignRules(min_clearance=0.2)
        design_rules = pcb_rules.to_design_rules(grid_resolution=0.1)

        assert design_rules.grid_resolution == 0.1


class TestValidateGridResolution:
    """Tests for validate_grid_resolution function."""

    def test_no_warning_when_compliant(self):
        """Test no warnings when grid resolution is <= clearance/2."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.1, 0.2, warn=True)

            assert len(issues) == 0
            assert len(w) == 0

    def test_warning_when_resolution_exceeds_half_clearance(self):
        """Test warning when grid resolution > clearance/2."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.15, 0.2, warn=True)

            assert len(issues) == 1
            assert "may cause clearance violations" in issues[0]
            assert len(w) == 1

    def test_error_when_resolution_exceeds_clearance(self):
        """Test error message when grid resolution > clearance."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.3, 0.2, warn=True)

            assert len(issues) == 1
            assert "WILL cause DRC violations" in issues[0]
            assert len(w) == 1

    def test_warn_false_suppresses_warnings(self):
        """Test that warn=False suppresses warnings.warn() calls."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            issues = validate_grid_resolution(0.3, 0.2, warn=False)

            # Issues should still be returned
            assert len(issues) == 1
            # But no warning should be emitted
            assert len(w) == 0

    def test_exact_half_is_compliant(self):
        """Test that exactly clearance/2 is compliant (edge case)."""
        issues = validate_grid_resolution(0.1, 0.2, warn=False)
        assert len(issues) == 0


class TestValidateRoutes:
    """Tests for validate_routes function."""

    def test_no_violations_for_empty_routes(self):
        """Test no violations when router has no routes."""
        router = Autorouter(width=50, height=50)
        violations = validate_routes(router)
        assert len(violations) == 0

    def test_detects_clearance_violation(self):
        """Test detection of clearance violations between routes and pads."""
        rules = DesignRules(
            trace_width=0.2,
            trace_clearance=0.2,
            grid_resolution=0.1,
        )
        router = Autorouter(width=50, height=50, rules=rules)

        # Add two pads on different nets
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20, "y": 10, "width": 1.0, "height": 1.0, "net": 2},
            ],
        )

        # Manually add a route that passes very close to the second pad
        from kicad_tools.router.primitives import Route, Segment

        segment = Segment(x1=10, y1=10, x2=19.5, y2=10, layer=Layer.F_CU, width=0.2)
        route = Route(net=1, net_name="NET1", segments=[segment], vias=[])
        router.routes.append(route)

        violations = validate_routes(router)

        # Should detect the violation (route too close to pad on net 2)
        assert len(violations) >= 1
        assert violations[0].obstacle_type == "pad"
        assert violations[0].net == 1
        assert violations[0].obstacle_net == 2

    def test_no_violation_for_same_net_proximity(self):
        """Test no violation when route is near pad on same net."""
        rules = DesignRules(trace_clearance=0.2, grid_resolution=0.1)
        router = Autorouter(width=50, height=50, rules=rules)

        # Add two pads on the same net
        router.add_component(
            "U1",
            [
                {"number": "1", "x": 10, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
                {"number": "2", "x": 20, "y": 10, "width": 1.0, "height": 1.0, "net": 1},
            ],
        )

        # Add route connecting them (passes very close)
        from kicad_tools.router.primitives import Route, Segment

        segment = Segment(x1=10.5, y1=10, x2=19.5, y2=10, layer=Layer.F_CU, width=0.2)
        route = Route(net=1, net_name="NET1", segments=[segment], vias=[])
        router.routes.append(route)

        violations = validate_routes(router)

        # No violation - route is on same net as nearby pads
        assert len(violations) == 0


class TestLoadPcbForRoutingDrcCompliance:
    """Tests for DRC compliance features in load_pcb_for_routing."""

    def test_uses_pcb_rules_by_default(self, tmp_path):
        """Test that PCB design rules are used when available."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
    (via_dia 0.5)
    (via_drill 0.25)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), use_pcb_rules=True, validate_drc=False
        )

        # Should use rules from PCB
        assert router.rules.trace_clearance == 0.15
        assert router.rules.trace_width == 0.18
        assert router.rules.via_diameter == 0.5
        assert router.rules.via_drill == 0.25

    def test_use_pcb_rules_false_uses_defaults(self, tmp_path):
        """Test that use_pcb_rules=False ignores PCB design rules."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net 1 "NET1")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET1"))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        router, net_map = load_pcb_for_routing(
            str(pcb_file), use_pcb_rules=False, validate_drc=False
        )

        # Should use default rules, not PCB rules
        assert router.rules.trace_clearance == 0.2  # Default
        assert router.rules.grid_resolution == 0.1  # Default

    def test_validate_drc_emits_warning(self, tmp_path):
        """Test that validate_drc=True emits warnings for bad grid resolution."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with bad grid resolution
        rules = DesignRules(grid_resolution=0.25, trace_clearance=0.2)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            router, net_map = load_pcb_for_routing(str(pcb_file), rules=rules, validate_drc=True)

            # Should emit a warning
            assert len(w) >= 1
            assert "clearance" in str(w[0].message).lower()

    def test_validate_drc_false_no_warning(self, tmp_path):
        """Test that validate_drc=False suppresses warnings."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        # Use rules with bad grid resolution
        rules = DesignRules(grid_resolution=0.25, trace_clearance=0.2)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            router, net_map = load_pcb_for_routing(str(pcb_file), rules=rules, validate_drc=False)

            # Should NOT emit a warning
            assert len(w) == 0

    def test_custom_rules_override_pcb_rules(self, tmp_path):
        """Test that explicit rules parameter overrides PCB rules."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
  )
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
  (net 0 "")
  (net_class "Default"
    (clearance 0.15)
    (trace_width 0.18)
  )
  (footprint "Test"
    (layer "F.Cu")
    (at 120 120)
    (fp_text reference "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)"""
        pcb_file = tmp_path / "test_drc.kicad_pcb"
        pcb_file.write_text(pcb_content)

        custom_rules = DesignRules(
            trace_width=0.3,
            trace_clearance=0.25,
            grid_resolution=0.1,
        )

        router, net_map = load_pcb_for_routing(
            str(pcb_file), rules=custom_rules, use_pcb_rules=True, validate_drc=False
        )

        # Custom rules should be used, not PCB rules
        assert router.rules.trace_width == 0.3
        assert router.rules.trace_clearance == 0.25


# =============================================================================
# Net Class Setup and PCB Merge Tests (Issue #45 - KiCad 7+ Compatibility)
# =============================================================================

from kicad_tools.router.io import generate_netclass_setup, merge_routes_into_pcb


class TestGenerateNetclassSetup:
    """Tests for generate_netclass_setup function - KiCad 7+ compatibility."""

    def test_empty_returns_empty_string(self):
        """Test that no net classes returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules)
        assert result == ""

    def test_none_net_classes_returns_empty(self):
        """Test that None net_classes returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules, net_classes=None)
        assert result == ""

    def test_empty_dict_returns_empty(self):
        """Test that empty dict returns empty string."""
        rules = DesignRules()
        result = generate_netclass_setup(rules, net_classes={})
        assert result == ""

    def test_generates_net_class_assignments(self):
        """Test that net classes generate proper S-expressions."""
        rules = DesignRules()
        net_classes = {
            "Power": ["+5V", "GND"],
            "Signal": ["SDA", "SCL"],
        }
        result = generate_netclass_setup(rules, net_classes)

        # Should contain net_class assignments
        assert '(net_class "Power" "+5V")' in result
        assert '(net_class "Power" "GND")' in result
        assert '(net_class "Signal" "SDA")' in result
        assert '(net_class "Signal" "SCL")' in result

    def test_does_not_use_old_format(self):
        """Test that old KiCad 6 format is not used."""
        rules = DesignRules()
        net_classes = {"Power": ["+5V"]}
        result = generate_netclass_setup(rules, net_classes)

        # Should NOT contain old format
        assert "(net_settings" not in result
        assert "Default net class" not in result
        # Should not have nested net_class with clearance/trace_width
        assert "clearance" not in result.lower()
        assert "via_dia" not in result.lower()


class TestMergeRoutesIntoPcb:
    """Tests for merge_routes_into_pcb function."""

    def test_empty_routes_returns_original(self):
        """Test that empty routes returns original content."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)"
        result = merge_routes_into_pcb(pcb_content, "")
        assert result == pcb_content

    def test_inserts_routes_before_closing_paren(self):
        """Test that routes are inserted before final closing paren."""
        pcb_content = "(kicad_pcb\n  (version 20240108)\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "(segment" in result
        assert result.endswith(")\n")
        # Route should be before final paren
        assert result.index("segment") < result.rfind(")")

    def test_adds_autorouted_comment(self):
        """Test that autorouted comment is added."""
        pcb_content = "(kicad_pcb\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "; Autorouted traces" in result

    def test_handles_trailing_whitespace(self):
        """Test handling of trailing whitespace in PCB content."""
        pcb_content = "(kicad_pcb\n)   \n\n"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "(segment" in result
        assert result.strip().endswith(")")

    def test_preserves_original_content(self):
        """Test that original PCB content is preserved."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 1 "VCC")
  (footprint "Package_SO:SOIC-8")
)"""
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        assert "version 20240108" in result
        assert 'generator "test"' in result
        assert 'net 1 "VCC"' in result
        assert "Package_SO:SOIC-8" in result

    def test_does_not_add_net_settings(self):
        """Test that no net_settings block is added (KiCad 7+ compatibility)."""
        pcb_content = "(kicad_pcb\n)"
        route_sexp = "(segment (start 0 0) (end 10 10) (width 0.2))"

        result = merge_routes_into_pcb(pcb_content, route_sexp)

        # Should NOT contain old net_settings format
        assert "(net_settings" not in result
        assert "(net_class" not in result


class TestKicad7Compatibility:
    """Integration tests for KiCad 7+ compatibility (Issue #45)."""

    def test_routed_segments_are_self_contained(self):
        """Test that segments embed trace width, making net class metadata optional."""
        seg = Segment(x1=0, y1=0, x2=10, y2=0, width=0.25, layer=Layer.F_CU, net=1)
        sexp = seg.to_sexp()

        # Width should be embedded in segment
        assert "(width 0.25)" in sexp
        # No external net class reference needed
        assert "(net_class" not in sexp

    def test_routed_vias_are_self_contained(self):
        """Test that vias embed size and drill, making net class metadata optional."""
        via = Via(x=10.0, y=20.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        sexp = via.to_sexp()

        # Size and drill should be embedded
        assert "(size 0.6)" in sexp
        assert "(drill 0.3)" in sexp
        # No external net class reference needed
        assert "(net_class" not in sexp

    def test_route_sexp_has_no_net_settings(self):
        """Test that Route.to_sexp() doesn't generate net_settings."""
        route = Route(net=1, net_name="VCC")
        route.segments.append(Segment(0, 0, 10, 0, 0.2, Layer.F_CU, net=1))
        route.vias.append(Via(10, 0, 0.3, 0.6, (Layer.F_CU, Layer.B_CU), net=1))

        sexp = route.to_sexp()

        # Should not contain net_settings (old KiCad 6 format)
        assert "(net_settings" not in sexp
        # Should contain segments and vias with embedded parameters
        assert "(segment" in sexp
        assert "(via" in sexp


class TestDiagonalRouting:
    """Tests for diagonal (45°) routing support (Issue #59)."""

    def test_octile_distance_straight(self):
        """Test octile distance for orthogonal movement."""
        # Pure horizontal: 10 units
        assert octile_distance(10, 0) == 10.0
        # Pure vertical: 10 units
        assert octile_distance(0, 10) == 10.0

    def test_octile_distance_diagonal(self):
        """Test octile distance for pure diagonal movement."""
        # Pure diagonal: √2 * 10 ≈ 14.14
        distance = octile_distance(10, 10)
        expected = 10 * DIAGONAL_COST  # 10 diagonal moves
        assert abs(distance - expected) < 0.001

    def test_octile_distance_mixed(self):
        """Test octile distance for mixed movement."""
        # 10 horizontal, 5 vertical = 5 diagonal + 5 straight
        # = 5 * √2 + 5 = 5 * 1.414 + 5 ≈ 12.07
        distance = octile_distance(10, 5)
        expected = max(10, 5) + (DIAGONAL_COST - 1) * min(10, 5)
        assert abs(distance - expected) < 0.001

    def test_octile_distance_negative(self):
        """Test octile distance handles negative values."""
        assert octile_distance(-10, 0) == 10.0
        assert octile_distance(0, -10) == 10.0
        assert octile_distance(-10, -10) == octile_distance(10, 10)

    def test_diagonal_cost_value(self):
        """Test DIAGONAL_COST is √2."""
        import math

        assert abs(DIAGONAL_COST - math.sqrt(2)) < 0.001

    def test_router_diagonal_routing_default_enabled(self):
        """Test that diagonal routing is enabled by default."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules)

        # Should have 8 neighbors (4 orthogonal + 4 diagonal)
        assert len(router.neighbors_2d) == 8

    def test_router_diagonal_routing_disabled(self):
        """Test that diagonal routing can be disabled."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules, diagonal_routing=False)

        # Should only have 4 orthogonal neighbors
        assert len(router.neighbors_2d) == 4

    def test_router_diagonal_neighbors(self):
        """Test diagonal neighbor directions and costs."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(50.0, 50.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        # Extract diagonal moves (where both dx and dy are non-zero)
        diagonal_moves = [
            (dx, dy, dl, cost) for dx, dy, dl, cost in router.neighbors_2d if dx != 0 and dy != 0
        ]

        assert len(diagonal_moves) == 4
        # All diagonal moves should have cost ≈ 1.414
        for _dx, _dy, _dl, cost in diagonal_moves:
            assert abs(cost - DIAGONAL_COST) < 0.001

    def test_manhattan_heuristic_octile_with_diagonal(self):
        """Test Manhattan heuristic uses octile distance when diagonal enabled."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # With diagonal routing: octile distance = 10 * √2 ≈ 14.14
        expected = 10 * DIAGONAL_COST * rules.cost_straight
        assert abs(estimate - expected) < 0.01

    def test_manhattan_heuristic_manhattan_without_diagonal(self):
        """Test Manhattan heuristic uses Manhattan distance when diagonal disabled."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=False
        )
        heuristic = ManhattanHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Without diagonal: Manhattan distance = 20
        assert estimate == 20.0

    def test_heuristic_context_diagonal_default(self):
        """Test HeuristicContext defaults to diagonal_routing=True."""
        rules = DesignRules()
        context = HeuristicContext(goal_x=10, goal_y=10, goal_layer=0, rules=rules)
        assert context.diagonal_routing is True

    def test_congestion_aware_heuristic_with_diagonal(self):
        """Test CongestionAware heuristic uses octile distance."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = CongestionAwareHeuristic()

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Base cost should use octile distance
        expected_base = 10 * DIAGONAL_COST * rules.cost_straight
        # Estimate should be at least the base octile distance
        assert estimate >= expected_base - 0.01

    def test_greedy_heuristic_with_diagonal(self):
        """Test Greedy heuristic scales octile distance."""
        rules = DesignRules()
        context = HeuristicContext(
            goal_x=10, goal_y=10, goal_layer=0, rules=rules, diagonal_routing=True
        )
        heuristic = GreedyHeuristic(greed_factor=2.0)

        estimate = heuristic.estimate(0, 0, 0, (0, 0), context)
        # Should be 2x octile distance
        expected = 2.0 * 10 * DIAGONAL_COST * rules.cost_straight
        assert abs(estimate - expected) < 0.01

    def test_router_diagonal_corner_blocking_basic(self):
        """Test diagonal corner clearance checking."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        # Block a cell that would be in the corner of a diagonal move
        layer = 0
        grid.grid[layer][1][0].blocked = True  # Block cell at (0, 1)

        # Check diagonal move from (0, 0) to (1, 1)
        # Adjacent cells are (0, 1) and (1, 0) - (0, 1) is blocked
        is_blocked = router._is_diagonal_corner_blocked(0, 0, 1, 1, layer, net=1)
        assert is_blocked is True

    def test_router_diagonal_corner_clear(self):
        """Test diagonal move allowed when corners are clear."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        layer = 0
        # Don't block any cells

        # Check diagonal move from (2, 2) to (3, 3)
        # Adjacent cells are (2, 3) and (3, 2) - both should be clear
        is_blocked = router._is_diagonal_corner_blocked(2, 2, 1, 1, layer, net=1)
        assert is_blocked is False

    def test_router_orthogonal_not_checked(self):
        """Test orthogonal moves skip corner checking."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(10.0, 10.0, rules)
        router = Router(grid, rules, diagonal_routing=True)

        layer = 0
        # Orthogonal moves (dx=0 or dy=0) should always return False
        assert router._is_diagonal_corner_blocked(0, 0, 1, 0, layer, net=1) is False
        assert router._is_diagonal_corner_blocked(0, 0, 0, 1, layer, net=1) is False

    def test_router_route_uses_diagonal(self):
        """Test that routes can use diagonal moves for shorter paths."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=1.0)
        grid = RoutingGrid(20.0, 20.0, rules)
        router_diag = Router(grid, rules, diagonal_routing=True)

        # Create pads at diagonal positions
        start_pad = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_pad = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        grid.add_pad(start_pad)
        grid.add_pad(end_pad)

        route = router_diag.route(start_pad, end_pad)
        assert route is not None
        assert len(route.segments) > 0

    def test_router_diagonal_vs_orthogonal_path_length(self):
        """Test diagonal routing produces shorter paths than orthogonal."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules(grid_resolution=1.0)

        # Create two separate grids for fair comparison
        grid_diag = RoutingGrid(20.0, 20.0, rules)
        grid_orth = RoutingGrid(20.0, 20.0, rules)

        router_diag = Router(grid_diag, rules, diagonal_routing=True)
        router_orth = Router(grid_orth, rules, diagonal_routing=False)

        # Create pads at diagonal positions
        start_diag = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_diag = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        start_orth = Pad(
            x=2.0, y=2.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )
        end_orth = Pad(
            x=10.0, y=10.0, width=1.0, height=1.0, net=1, net_name="test", layer=Layer.F_CU
        )

        grid_diag.add_pad(start_diag)
        grid_diag.add_pad(end_diag)
        grid_orth.add_pad(start_orth)
        grid_orth.add_pad(end_orth)

        route_diag = router_diag.route(start_diag, end_diag)
        route_orth = router_orth.route(start_orth, end_orth)

        assert route_diag is not None
        assert route_orth is not None

        # Calculate total path length
        def total_length(route):
            length = 0.0
            for seg in route.segments:
                dx = seg.x2 - seg.x1
                dy = seg.y2 - seg.y1
                length += (dx**2 + dy**2) ** 0.5
            return length

        diag_length = total_length(route_diag)
        orth_length = total_length(route_orth)

        # Diagonal path should be shorter or equal (never longer)
        # Note: May be equal if path is already orthogonal
        assert diag_length <= orth_length + 0.01


# =============================================================================
# ZONE RULES AND ZONE-AWARE ROUTING TESTS
# =============================================================================


class TestZoneRules:
    """Tests for ZoneRules dataclass."""

    def test_zone_rules_defaults(self):
        """Test default values for ZoneRules."""
        from kicad_tools.router.rules import ZoneRules

        rules = ZoneRules()
        assert rules.clearance == 0.2
        assert rules.thermal_gap == 0.3
        assert rules.thermal_bridge_width == 0.3
        assert rules.thermal_spoke_count == 4
        assert rules.thermal_spoke_angle == 45.0
        assert rules.pth_connection == "thermal"
        assert rules.smd_connection == "thermal"
        assert rules.via_connection == "solid"
        assert rules.remove_islands is True
        assert rules.island_min_area == 0.5

    def test_zone_rules_custom(self):
        """Test custom ZoneRules values."""
        from kicad_tools.router.rules import ZoneRules

        rules = ZoneRules(
            clearance=0.3,
            thermal_gap=0.5,
            thermal_spoke_count=2,
            pth_connection="solid",
        )
        assert rules.clearance == 0.3
        assert rules.thermal_gap == 0.5
        assert rules.thermal_spoke_count == 2
        assert rules.pth_connection == "solid"


class TestDesignRulesZoneExtensions:
    """Tests for zone extensions to DesignRules."""

    def test_design_rules_has_zone_rules(self):
        """Test that DesignRules includes zone_rules."""
        from kicad_tools.router.rules import ZoneRules

        rules = DesignRules()
        assert hasattr(rules, "zone_rules")
        assert isinstance(rules.zone_rules, ZoneRules)

    def test_design_rules_zone_costs(self):
        """Test zone cost parameters in DesignRules."""
        rules = DesignRules()
        assert hasattr(rules, "cost_zone_same_net")
        assert hasattr(rules, "cost_zone_clearance")
        assert rules.cost_zone_same_net == 0.1  # Low cost for same-net zones
        assert rules.cost_zone_clearance == 2.0


class TestNetClassZoneExtensions:
    """Tests for zone extensions to NetClassRouting."""

    def test_net_class_has_zone_fields(self):
        """Test that NetClassRouting has zone-related fields."""
        nc = NetClassRouting(name="Test")
        assert hasattr(nc, "zone_priority")
        assert hasattr(nc, "zone_connection")
        assert hasattr(nc, "is_pour_net")

    def test_net_class_defaults(self):
        """Test default zone values for NetClassRouting."""
        nc = NetClassRouting(name="Test")
        assert nc.zone_priority == 0
        assert nc.zone_connection == "thermal"
        assert nc.is_pour_net is False

    def test_power_net_class_is_pour_net(self):
        """Test that NET_CLASS_POWER is marked as pour net."""
        assert NET_CLASS_POWER.is_pour_net is True
        assert NET_CLASS_POWER.zone_priority == 10
        assert NET_CLASS_POWER.zone_connection == "solid"


class TestZoneManager:
    """Tests for ZoneManager class."""

    def test_zone_manager_creation(self):
        """Test ZoneManager creation."""
        from kicad_tools.router import ZoneManager
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules()
        grid = RoutingGrid(50, 50, rules)
        manager = ZoneManager(grid, rules)

        assert manager.grid is grid
        assert manager.rules is rules
        assert manager.filled_zones == []

    def test_zone_manager_statistics_empty(self):
        """Test zone statistics with no zones."""
        from kicad_tools.router import ZoneManager
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules()
        grid = RoutingGrid(50, 50, rules)
        manager = ZoneManager(grid, rules)

        stats = manager.get_zone_statistics()
        assert stats["zone_count"] == 0
        assert stats["total_cells"] == 0
        assert stats["zones"] == []


class TestPathfinderZoneAwareness:
    """Tests for zone-aware routing in the pathfinder."""

    def test_zone_cell_detection(self):
        """Test detection of zone cells in pathfinder."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zones initially
        assert not router._is_zone_cell(5, 5, 0)

        # Mark a cell as zone
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        assert router._is_zone_cell(5, 5, 0)
        assert router._get_zone_net(5, 5, 0) == 1

    def test_zone_blocking_other_net(self):
        """Test that other-net zones block routing."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # Mark cell as zone for net 1
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        # Net 2 should be blocked by net 1 zone
        assert router._is_zone_blocked(5, 5, 0, net=2)
        # Net 1 should NOT be blocked by its own zone
        assert not router._is_zone_blocked(5, 5, 0, net=1)

    def test_zone_cost_same_net(self):
        """Test reduced cost for same-net zones."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zone - cost should be 0
        cost = router._get_zone_cost(5, 5, 0, net=1)
        assert cost == 0.0

        # Mark cell as zone for net 1
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 1

        # Same net - should have reduced cost (negative adjustment)
        cost = router._get_zone_cost(5, 5, 0, net=1)
        assert cost < 0

    def test_via_zone_blocking(self):
        """Test via placement blocked by other-net zones."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.pathfinder import Router

        rules = DesignRules()
        grid = RoutingGrid(20, 20, rules)
        router = Router(grid, rules)

        # No zones - via allowed
        assert router._can_place_via_in_zones(5, 5, net=1)

        # Add zone on layer 0 for net 2
        cell = grid.grid[0][5][5]
        cell.is_zone = True
        cell.net = 2

        # Net 1 via should be blocked (would pierce net 2 zone)
        assert not router._can_place_via_in_zones(5, 5, net=1)
        # Net 2 via should be allowed (through own zone)
        assert router._can_place_via_in_zones(5, 5, net=2)


# =============================================================================
# BOARD EDGE CLEARANCE TESTS (Issue #296)
# =============================================================================


class TestEdgeClearance:
    """Tests for board edge clearance functionality (Issue #296)."""

    def test_add_edge_keepout_blocks_cells(self):
        """Test that add_edge_keepout blocks cells near board edges."""
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules, origin_x=0, origin_y=0)

        # Define a simple rectangular board outline
        edge_segments = [
            ((0, 0), (20, 0)),  # Bottom edge
            ((20, 0), (20, 20)),  # Right edge
            ((20, 20), (0, 20)),  # Top edge
            ((0, 20), (0, 0)),  # Left edge
        ]

        # Apply 1mm edge clearance
        blocked_count = grid.add_edge_keepout(edge_segments, clearance=1.0)

        # Should have blocked some cells
        assert blocked_count > 0

        # Cells at the edge should be blocked (within 1mm = 2 grid cells)
        gx0, gy0 = grid.world_to_grid(0.5, 0.5)  # Near corner
        layer_idx = grid.get_routable_indices()[0]
        assert grid.grid[layer_idx][gy0][gx0].blocked is True

        # Cells in the center should NOT be blocked
        gx_center, gy_center = grid.world_to_grid(10, 10)
        assert grid.grid[layer_idx][gy_center][gx_center].blocked is False

    def test_add_edge_keepout_respects_clearance_distance(self):
        """Test that edge keepout uses correct clearance distance."""
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules(grid_resolution=0.25)
        grid = RoutingGrid(20, 20, rules, origin_x=0, origin_y=0)

        # Single horizontal edge segment at bottom
        edge_segments = [((0, 0), (20, 0))]

        # Apply 0.5mm edge clearance
        grid.add_edge_keepout(edge_segments, clearance=0.5)

        layer_idx = grid.get_routable_indices()[0]

        # Cell at 0.4mm from edge should be blocked (within 0.5mm clearance)
        gx, gy = grid.world_to_grid(10, 0.4)
        assert grid.grid[layer_idx][gy][gx].blocked is True

        # Cell at 1.0mm from edge should NOT be blocked
        gx, gy = grid.world_to_grid(10, 1.0)
        assert grid.grid[layer_idx][gy][gx].blocked is False

    def test_add_edge_keepout_no_clearance(self):
        """Test that zero clearance blocks no cells."""
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules)

        edge_segments = [((0, 0), (20, 0))]
        blocked_count = grid.add_edge_keepout(edge_segments, clearance=0.0)

        assert blocked_count == 0

    def test_add_edge_keepout_empty_segments(self):
        """Test that empty segment list blocks no cells."""
        from kicad_tools.router.grid import RoutingGrid

        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules)

        blocked_count = grid.add_edge_keepout([], clearance=1.0)

        assert blocked_count == 0

    def test_add_edge_keepout_all_layers(self):
        """Test that edge keepout applies to all routable layers."""
        from kicad_tools.router.grid import RoutingGrid
        from kicad_tools.router.layers import LayerStack

        # Use 4-layer board (signal-gnd-pwr-signal configuration)
        layer_stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        rules = DesignRules(grid_resolution=0.5)
        grid = RoutingGrid(20, 20, rules, layer_stack=layer_stack)

        edge_segments = [((0, 0), (20, 0))]
        grid.add_edge_keepout(edge_segments, clearance=1.0)

        # All routable layers should have cells blocked near edge
        routable_indices = grid.get_routable_indices()
        gx, gy = grid.world_to_grid(10, 0.5)

        for layer_idx in routable_indices:
            assert grid.grid[layer_idx][gy][gx].blocked is True


class TestExtractEdgeSegments:
    """Tests for extracting board edge segments from PCB files."""

    def test_extract_gr_rect_edge(self):
        """Test extracting edge segments from gr_rect element."""
        from kicad_tools.router.io import _extract_edge_segments

        pcb_text = """(kicad_pcb
  (gr_rect (start 100 100) (end 150 140) (layer "Edge.Cuts"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # gr_rect should produce 4 edge segments
        assert len(segments) == 4

        # Check that segments form a rectangle
        all_points = set()
        for (x1, y1), (x2, y2) in segments:
            all_points.add((x1, y1))
            all_points.add((x2, y2))

        # Should have 4 corner points
        assert (100, 100) in all_points
        assert (150, 100) in all_points
        assert (150, 140) in all_points
        assert (100, 140) in all_points

    def test_extract_gr_line_edges(self):
        """Test extracting edge segments from gr_line elements."""
        from kicad_tools.router.io import _extract_edge_segments

        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should extract 2 line segments
        assert len(segments) == 2
        assert ((0, 0), (50, 0)) in segments
        assert ((50, 0), (50, 50)) in segments

    def test_ignores_non_edge_cuts_layer(self):
        """Test that non-Edge.Cuts layers are ignored."""
        from kicad_tools.router.io import _extract_edge_segments

        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0) (layer "F.SilkS") (width 0.1))
  (gr_rect (start 0 0) (end 50 50) (layer "F.Cu"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should not extract any segments (not on Edge.Cuts)
        assert len(segments) == 0

    def test_extract_gr_rect_with_stroke_fill_attributes(self):
        """Test extracting edge from gr_rect with KiCad 7/8 stroke/fill attributes.

        KiCad 7+ includes stroke and fill attributes with nested parentheses.
        The regex must handle these nested structures correctly.
        See issue #318.
        """
        from kicad_tools.router.io import _extract_edge_segments

        # KiCad 7/8 format with stroke and fill (nested parentheses)
        pcb_text = """(kicad_pcb
  (gr_rect
    (start 0 0)
    (end 15 15)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)"""

        segments = _extract_edge_segments(pcb_text)

        # gr_rect should produce 4 edge segments
        assert len(segments) == 4

        # Check that segments form the expected rectangle
        all_points = set()
        for (x1, y1), (x2, y2) in segments:
            all_points.add((x1, y1))
            all_points.add((x2, y2))

        # Should have 4 corner points
        assert (0, 0) in all_points
        assert (15, 0) in all_points
        assert (15, 15) in all_points
        assert (0, 15) in all_points

    def test_extract_gr_line_with_stroke_attributes(self):
        """Test extracting edge from gr_line with KiCad 7/8 stroke attributes.

        See issue #318.
        """
        from kicad_tools.router.io import _extract_edge_segments

        # KiCad 7/8 format with stroke attribute
        pcb_text = """(kicad_pcb
  (gr_line (start 0 0) (end 50 0)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts"))
  (gr_line (start 50 0) (end 50 50)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts"))
)"""

        segments = _extract_edge_segments(pcb_text)

        # Should extract 2 line segments
        assert len(segments) == 2
        assert ((0, 0), (50, 0)) in segments
        assert ((50, 0), (50, 50)) in segments


class TestLoadPcbEdgeClearance:
    """Tests for edge_clearance parameter in load_pcb_for_routing."""

    def test_edge_clearance_applied(self, tmp_path):
        """Test that edge_clearance is applied when loading PCB."""
        from kicad_tools.router.io import load_pcb_for_routing

        # Create a minimal PCB file with Edge.Cuts rectangle
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
)""")

        # Load with edge clearance
        router, _ = load_pcb_for_routing(
            str(pcb_file),
            edge_clearance=1.0,
            validate_drc=False,
        )

        # Cells near edge should be blocked
        layer_idx = router.grid.get_routable_indices()[0]
        gx, gy = router.grid.world_to_grid(0.5, 0.5)
        assert router.grid.grid[layer_idx][gy][gx].blocked is True

    def test_no_edge_clearance_by_default(self, tmp_path):
        """Test that no edge clearance is applied by default."""
        from kicad_tools.router.io import load_pcb_for_routing

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))
)""")

        # Load without edge clearance (default)
        router, _ = load_pcb_for_routing(
            str(pcb_file),
            edge_clearance=None,
            validate_drc=False,
        )

        # Edge cells should NOT be blocked (no components yet)
        layer_idx = router.grid.get_routable_indices()[0]
        gx, gy = router.grid.world_to_grid(0.5, 0.5)
        assert router.grid.grid[layer_idx][gy][gx].blocked is False
