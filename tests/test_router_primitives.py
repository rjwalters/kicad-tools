"""Tests for router/primitives.py module."""

from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via


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
