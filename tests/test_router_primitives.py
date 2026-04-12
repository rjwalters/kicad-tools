"""Tests for router/primitives.py module."""

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import GridCell, Obstacle, Pad, Point, Route, Segment, Via
from kicad_tools.router.rules import (
    DEFAULT_NET_CLASS_MAP,
    NET_CLASS_CLOCK,
    NET_CLASS_POWER,
    DesignRules,
    NetClassRouting,
    create_net_class_map,
)


class TestSegmentStartEndProperties:
    """Tests for Segment.start and Segment.end properties.

    These properties provide convenient tuple access to segment endpoints,
    enabling cleaner code when working with segment coordinates.
    """

    def test_start_property_returns_tuple(self):
        """Test that start property returns (x1, y1) tuple."""
        seg = Segment(x1=10.0, y1=20.0, x2=30.0, y2=40.0, width=0.2, layer=Layer.F_CU)
        assert seg.start == (10.0, 20.0)
        assert isinstance(seg.start, tuple)

    def test_end_property_returns_tuple(self):
        """Test that end property returns (x2, y2) tuple."""
        seg = Segment(x1=10.0, y1=20.0, x2=30.0, y2=40.0, width=0.2, layer=Layer.F_CU)
        assert seg.end == (30.0, 40.0)
        assert isinstance(seg.end, tuple)

    def test_start_and_end_different(self):
        """Test that start and end return different values for non-zero-length segment."""
        seg = Segment(x1=0.0, y1=0.0, x2=100.0, y2=50.0, width=0.2, layer=Layer.F_CU)
        assert seg.start != seg.end
        assert seg.start == (0.0, 0.0)
        assert seg.end == (100.0, 50.0)

    def test_zero_length_segment(self):
        """Test start and end for zero-length segment (same point)."""
        seg = Segment(x1=5.0, y1=5.0, x2=5.0, y2=5.0, width=0.2, layer=Layer.F_CU)
        assert seg.start == seg.end == (5.0, 5.0)

    def test_negative_coordinates(self):
        """Test start and end with negative coordinates."""
        seg = Segment(x1=-10.0, y1=-20.0, x2=-5.0, y2=-15.0, width=0.2, layer=Layer.F_CU)
        assert seg.start == (-10.0, -20.0)
        assert seg.end == (-5.0, -15.0)

    def test_tuple_indexing(self):
        """Test that tuple properties can be indexed."""
        seg = Segment(x1=1.5, y1=2.5, x2=3.5, y2=4.5, width=0.2, layer=Layer.F_CU)
        # Index into start
        assert seg.start[0] == 1.5
        assert seg.start[1] == 2.5
        # Index into end
        assert seg.end[0] == 3.5
        assert seg.end[1] == 4.5

    def test_tuple_unpacking(self):
        """Test that tuple properties can be unpacked."""
        seg = Segment(x1=1.0, y1=2.0, x2=3.0, y2=4.0, width=0.2, layer=Layer.F_CU)
        # Unpack start
        x1, y1 = seg.start
        assert x1 == 1.0
        assert y1 == 2.0
        # Unpack end
        x2, y2 = seg.end
        assert x2 == 3.0
        assert y2 == 4.0

    def test_properties_consistent_with_attributes(self):
        """Test that properties are consistent with x1/y1/x2/y2 attributes."""
        seg = Segment(x1=100.0, y1=200.0, x2=300.0, y2=400.0, width=0.3, layer=Layer.B_CU)
        # Properties should match the underlying attributes
        assert seg.start[0] == seg.x1
        assert seg.start[1] == seg.y1
        assert seg.end[0] == seg.x2
        assert seg.end[1] == seg.y2

    def test_length_calculation_using_properties(self):
        """Test that segment length can be calculated using start/end properties."""
        seg = Segment(x1=0.0, y1=0.0, x2=3.0, y2=4.0, width=0.2, layer=Layer.F_CU)
        # 3-4-5 triangle should have length 5
        dx = seg.end[0] - seg.start[0]
        dy = seg.end[1] - seg.start[1]
        length = (dx**2 + dy**2) ** 0.5
        assert length == 5.0


class TestRouteValidateLayerTransitions:
    """Tests for Route.validate_layer_transitions() method.

    This method ensures that when consecutive segments are on different layers,
    there is a via at the transition point to make the route electrically valid.
    """

    def test_no_segments_returns_zero(self):
        """Test that empty route returns 0 vias inserted."""
        route = Route(net=1, net_name="TEST")
        assert route.validate_layer_transitions() == 0

    def test_single_segment_returns_zero(self):
        """Test that route with single segment returns 0 vias inserted."""
        route = Route(net=1, net_name="TEST")
        route.segments.append(Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU))
        assert route.validate_layer_transitions() == 0
        assert len(route.vias) == 0

    def test_same_layer_segments_no_via_needed(self):
        """Test that consecutive segments on same layer don't need vias."""
        route = Route(net=1, net_name="TEST")
        route.segments.extend(
            [
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=20.0, y1=0.0, x2=30.0, y2=0.0, width=0.2, layer=Layer.F_CU),
            ]
        )
        assert route.validate_layer_transitions() == 0
        assert len(route.vias) == 0

    def test_layer_transition_inserts_via(self):
        """Test that layer transition without via causes via insertion."""
        route = Route(net=1, net_name="NET1")
        # F.Cu segment ending at (10, 5)
        route.segments.append(Segment(x1=0.0, y1=0.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.F_CU))
        # B.Cu segment starting at (10, 5) - layer change with no via!
        route.segments.append(
            Segment(x1=10.0, y1=5.0, x2=20.0, y2=5.0, width=0.2, layer=Layer.B_CU)
        )

        # Should insert 1 via
        inserted = route.validate_layer_transitions()
        assert inserted == 1
        assert len(route.vias) == 1

        # Via should be at transition point
        via = route.vias[0]
        assert abs(via.x - 10.0) < 0.01
        assert abs(via.y - 5.0) < 0.01
        assert via.layers == (Layer.F_CU, Layer.B_CU)
        assert via.net == 1
        assert via.net_name == "NET1"

    def test_layer_transition_with_existing_via_no_duplicate(self):
        """Test that existing via at transition point prevents duplicate."""
        route = Route(net=1, net_name="TEST")
        route.segments.extend(
            [
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=5.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=10.0, y1=5.0, x2=20.0, y2=5.0, width=0.2, layer=Layer.B_CU),
            ]
        )
        # Pre-existing via at transition point
        route.vias.append(
            Via(
                x=10.0,
                y=5.0,
                drill=0.35,
                diameter=0.7,
                layers=(Layer.F_CU, Layer.B_CU),
                net=1,
            )
        )

        # Should not insert any vias
        inserted = route.validate_layer_transitions()
        assert inserted == 0
        assert len(route.vias) == 1  # Still just the original via

    def test_multiple_layer_transitions(self):
        """Test multiple layer transitions in a route."""
        route = Route(net=2, net_name="MULTI")
        route.segments.extend(
            [
                # F.Cu → transition at (10, 0)
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                # B.Cu → transition at (20, 0)
                Segment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, width=0.2, layer=Layer.B_CU),
                # F.Cu again
                Segment(x1=20.0, y1=0.0, x2=30.0, y2=0.0, width=0.2, layer=Layer.F_CU),
            ]
        )

        # Should insert 2 vias
        inserted = route.validate_layer_transitions()
        assert inserted == 2
        assert len(route.vias) == 2

        # Check via positions
        via_positions = sorted([(v.x, v.y) for v in route.vias])
        assert abs(via_positions[0][0] - 10.0) < 0.01
        assert abs(via_positions[1][0] - 20.0) < 0.01

    def test_custom_via_parameters(self):
        """Test that custom via drill and diameter are used."""
        route = Route(net=1, net_name="TEST")
        route.segments.extend(
            [
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, width=0.2, layer=Layer.B_CU),
            ]
        )

        # Use custom via parameters
        inserted = route.validate_layer_transitions(via_drill=0.5, via_diameter=1.0)
        assert inserted == 1

        via = route.vias[0]
        assert via.drill == 0.5
        assert via.diameter == 1.0

    def test_via_at_nearby_position_not_duplicate(self):
        """Test that via at nearby but different position doesn't prevent insertion."""
        route = Route(net=1, net_name="TEST")
        route.segments.extend(
            [
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, width=0.2, layer=Layer.B_CU),
            ]
        )
        # Via at nearby but different position (more than 0.01mm away)
        route.vias.append(
            Via(
                x=10.02,  # 0.02mm away - should not count as same position
                y=0.0,
                drill=0.35,
                diameter=0.7,
                layers=(Layer.F_CU, Layer.B_CU),
            )
        )

        # Should still insert a via at exact transition point
        inserted = route.validate_layer_transitions()
        assert inserted == 1
        assert len(route.vias) == 2

    def test_inner_layer_transitions(self):
        """Test transitions involving inner layers (4-layer board)."""
        route = Route(net=1, net_name="INNER")
        route.segments.extend(
            [
                # F.Cu → In1.Cu
                Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, width=0.2, layer=Layer.F_CU),
                Segment(x1=10.0, y1=0.0, x2=20.0, y2=0.0, width=0.2, layer=Layer.IN1_CU),
                # In1.Cu → B.Cu
                Segment(x1=20.0, y1=0.0, x2=30.0, y2=0.0, width=0.2, layer=Layer.B_CU),
            ]
        )

        inserted = route.validate_layer_transitions()
        assert inserted == 2

        # Check layer pairs are correct
        layers_pairs = sorted([v.layers for v in route.vias], key=lambda x: x[0].value)
        assert layers_pairs[0] == (Layer.F_CU, Layer.IN1_CU)
        assert layers_pairs[1] == (Layer.IN1_CU, Layer.B_CU)

    def test_issue_713_scenario(self):
        """Test the exact scenario from issue #713.

        From the issue: Net 2 (LED_ANODE) has:
        - F.Cu segment ending at (114.8, 111.3)
        - B.Cu segment starting at (114.8, 111.3)
        - No via between them
        """
        route = Route(net=2, net_name="LED_ANODE")
        # Segments from the issue
        route.segments.extend(
            [
                # F.Cu segment ending at (114.8, 111.3)
                Segment(x1=112.5, y1=109.0, x2=114.8, y2=111.3, width=0.2, layer=Layer.F_CU, net=2),
                # B.Cu segment starting at (114.8, 111.3)
                Segment(x1=114.8, y1=111.3, x2=119.5, y2=111.3, width=0.2, layer=Layer.B_CU, net=2),
            ]
        )

        # Before validation: no vias
        assert len(route.vias) == 0

        # Validate should insert via
        inserted = route.validate_layer_transitions()
        assert inserted == 1

        # Via should be at transition point
        via = route.vias[0]
        assert abs(via.x - 114.8) < 0.01
        assert abs(via.y - 111.3) < 0.01
        assert via.layers == (Layer.F_CU, Layer.B_CU)
        assert via.net == 2


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

    def test_crossing_penalty_default(self):
        """Test that crossing_penalty defaults to 0.0 for backward compatibility."""
        rules = DesignRules()
        assert rules.crossing_penalty == 0.0

    def test_crossing_penalty_custom(self):
        """Test setting a custom crossing_penalty value."""
        rules = DesignRules(crossing_penalty=5.0)
        assert rules.crossing_penalty == 5.0


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
