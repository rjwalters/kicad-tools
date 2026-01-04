"""Tests for edge placement constraints."""

import pytest

from kicad_tools.optim import (
    BoardEdges,
    Component,
    Edge,
    EdgeConstraint,
    EdgeSide,
    Pin,
    PlacementConfig,
    PlacementOptimizer,
    Polygon,
    Vector2D,
)
from kicad_tools.optim.edge_placement import (
    _is_connector,
    _is_led,
    _is_mounting_hole,
    _is_switch,
    _is_test_point,
    compute_edge_force,
)


class TestEdgeSide:
    """Tests for EdgeSide enum."""

    def test_all_values(self):
        assert EdgeSide.TOP.value == "top"
        assert EdgeSide.BOTTOM.value == "bottom"
        assert EdgeSide.LEFT.value == "left"
        assert EdgeSide.RIGHT.value == "right"
        assert EdgeSide.ANY.value == "any"


class TestEdge:
    """Tests for Edge dataclass."""

    @pytest.fixture
    def horizontal_edge(self):
        return Edge(
            start=Vector2D(0, 0),
            end=Vector2D(100, 0),
            side=EdgeSide.TOP,
        )

    @pytest.fixture
    def vertical_edge(self):
        return Edge(
            start=Vector2D(0, 0),
            end=Vector2D(0, 100),
            side=EdgeSide.LEFT,
        )

    def test_length(self, horizontal_edge):
        assert horizontal_edge.length == 100.0

    def test_direction_horizontal(self, horizontal_edge):
        d = horizontal_edge.direction
        assert abs(d.x - 1.0) < 1e-10
        assert abs(d.y) < 1e-10

    def test_direction_vertical(self, vertical_edge):
        d = vertical_edge.direction
        assert abs(d.x) < 1e-10
        assert abs(d.y - 1.0) < 1e-10

    def test_normal_horizontal(self, horizontal_edge):
        n = horizontal_edge.normal
        # Normal should point away from edge (perpendicular)
        assert abs(n.magnitude() - 1.0) < 1e-10

    def test_project_point_on_edge(self, horizontal_edge):
        point = Vector2D(50, 10)
        pos, dist = horizontal_edge.project_point(point)
        assert abs(pos - 50.0) < 1e-10
        assert abs(dist - 10.0) < 1e-10

    def test_project_point_before_edge(self, horizontal_edge):
        point = Vector2D(-10, 5)
        pos, dist = horizontal_edge.project_point(point)
        # Position is clamped to edge start
        assert pos < 0 or pos == 0

    def test_project_point_after_edge(self, horizontal_edge):
        point = Vector2D(110, 5)
        pos, dist = horizontal_edge.project_point(point)
        # Position might extend beyond edge
        assert pos > 100


class TestEdgeConstraint:
    """Tests for EdgeConstraint dataclass."""

    def test_default_values(self):
        c = EdgeConstraint(reference="J1")
        assert c.reference == "J1"
        assert c.edge == "any"
        assert c.position is None
        assert c.slide is True
        assert c.corner_priority is False
        assert c.offset_mm == 0.0

    def test_with_edge(self):
        c = EdgeConstraint(reference="USB1", edge="top")
        assert c.edge == "top"
        assert c.edge_side == EdgeSide.TOP

    def test_with_position(self):
        c = EdgeConstraint(reference="J1", edge="left", position=15.0)
        assert c.position == 15.0

    def test_with_center_position(self):
        c = EdgeConstraint(reference="J1", edge="top", position="center")
        assert c.position == "center"

    def test_corner_priority(self):
        c = EdgeConstraint(reference="MH1", edge="any", corner_priority=True)
        assert c.corner_priority is True

    def test_invalid_edge_raises(self):
        with pytest.raises(ValueError, match="Invalid edge"):
            EdgeConstraint(reference="J1", edge="invalid")


class TestBoardEdges:
    """Tests for BoardEdges dataclass."""

    @pytest.fixture
    def board_edges(self):
        return BoardEdges.from_bounds(0, 0, 100, 80)

    def test_from_bounds(self, board_edges):
        assert board_edges.top is not None
        assert board_edges.bottom is not None
        assert board_edges.left is not None
        assert board_edges.right is not None

    def test_from_bounds_dimensions(self, board_edges):
        # Top edge should be at y=0
        assert board_edges.top.start.y == 0
        assert board_edges.top.end.y == 0
        # Bottom edge should be at y=80
        assert board_edges.bottom.start.y == 80
        assert board_edges.bottom.end.y == 80
        # Left edge should be at x=0
        assert board_edges.left.start.x == 0
        assert board_edges.left.end.x == 0
        # Right edge should be at x=100
        assert board_edges.right.start.x == 100
        assert board_edges.right.end.x == 100

    def test_from_polygon(self):
        polygon = Polygon.rectangle(50, 40, 100, 80)
        edges = BoardEdges.from_polygon(polygon)
        assert edges.top is not None
        assert edges.bottom is not None

    def test_get_edge(self, board_edges):
        assert board_edges.get_edge("top") == board_edges.top
        assert board_edges.get_edge(EdgeSide.BOTTOM) == board_edges.bottom

    def test_all_edges(self, board_edges):
        edges = board_edges.all_edges()
        assert len(edges) == 4
        assert board_edges.top in edges
        assert board_edges.bottom in edges

    def test_nearest_edge(self, board_edges):
        # Point near top edge
        point = Vector2D(50, 5)
        nearest = board_edges.nearest_edge(point)
        assert nearest.side == EdgeSide.TOP

        # Point near left edge
        point = Vector2D(5, 40)
        nearest = board_edges.nearest_edge(point)
        assert nearest.side == EdgeSide.LEFT

    def test_corners(self, board_edges):
        corners = board_edges.corners()
        assert len(corners) == 4

    def test_nearest_corner(self, board_edges):
        point = Vector2D(2, 2)  # Near top-left
        corner = board_edges.nearest_corner(point)
        assert corner.x == 0
        assert corner.y == 0


class TestComponentTypeDetection:
    """Tests for component type detection patterns."""

    def test_is_connector(self):
        assert _is_connector("J1") is True
        assert _is_connector("J12") is True
        assert _is_connector("P1") is True
        assert _is_connector("USB1") is True
        assert _is_connector("CON1") is True
        assert _is_connector("DC1") is True
        assert _is_connector("R1") is False
        assert _is_connector("C1") is False

    def test_is_connector_by_footprint(self):
        assert _is_connector("X1", "USB_C_Connector") is True
        assert _is_connector("X1", "Barrel_Jack") is True
        assert _is_connector("X1", "PinHeader_2x10") is True

    def test_is_mounting_hole(self):
        assert _is_mounting_hole("MH1") is True
        assert _is_mounting_hole("H1") is True
        assert _is_mounting_hole("H2") is True
        assert _is_mounting_hole("MOUNT1") is True
        assert _is_mounting_hole("R1") is False

    def test_is_mounting_hole_by_footprint(self):
        assert _is_mounting_hole("X1", "MountingHole_3.2mm") is True

    def test_is_test_point(self):
        assert _is_test_point("TP1") is True
        assert _is_test_point("TP") is True
        assert _is_test_point("TEST1") is True
        assert _is_test_point("R1") is False

    def test_is_switch(self):
        assert _is_switch("SW1") is True
        assert _is_switch("BTN1") is True
        assert _is_switch("S1") is True
        assert _is_switch("R1") is False

    def test_is_switch_by_footprint(self):
        assert _is_switch("X1", "SW_Tactile") is True
        assert _is_switch("X1", "Button_4x4mm") is True

    def test_is_led(self):
        assert _is_led("LED1") is True
        assert _is_led("D1") is True
        assert _is_led("R1") is False

    def test_is_led_by_footprint(self):
        assert _is_led("X1", "LED_0805") is True


class TestComputeEdgeForce:
    """Tests for edge force computation."""

    @pytest.fixture
    def component(self):
        return Component(
            ref="J1",
            x=50,
            y=20,  # 20mm from top edge
            width=10,
            height=5,
        )

    @pytest.fixture
    def board_edges(self):
        return BoardEdges.from_bounds(0, 0, 100, 80)

    def test_force_toward_edge(self, component, board_edges):
        constraint = EdgeConstraint(reference="J1", edge="top")
        force, is_at_edge = compute_edge_force(component, constraint, board_edges, stiffness=50.0)
        # Force should pull toward top edge (negative y in KiCad coordinates)
        # Since component is at y=20 and top edge is at y=0
        assert force.y < 0 or abs(force.y) < 1e-10
        assert is_at_edge is False  # 20mm away, not at edge

    def test_at_edge(self, board_edges):
        component = Component(ref="J1", x=50, y=0.5, width=10, height=5)
        constraint = EdgeConstraint(reference="J1", edge="top")
        force, is_at_edge = compute_edge_force(component, constraint, board_edges, stiffness=50.0)
        assert is_at_edge is True  # Within 1mm of edge

    def test_corner_priority_force(self, component, board_edges):
        constraint = EdgeConstraint(reference="J1", edge="any", corner_priority=True)
        force, _ = compute_edge_force(component, constraint, board_edges, stiffness=50.0)
        # Should have some force component toward nearest corner
        assert force.magnitude() > 0


class TestPlacementOptimizerEdgeConstraints:
    """Tests for edge constraints in PlacementOptimizer."""

    @pytest.fixture
    def optimizer(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        return PlacementOptimizer(board)

    def test_add_edge_constraint(self, optimizer):
        comp = Component(ref="J1", x=50, y=50)
        optimizer.add_component(comp)

        constraint = EdgeConstraint(reference="J1", edge="top")
        optimizer.add_edge_constraint(constraint)

        assert optimizer.get_edge_constraint("J1") == constraint
        assert comp.edge_constraint == constraint

    def test_add_multiple_edge_constraints(self, optimizer):
        comp1 = Component(ref="J1", x=20, y=20)
        comp2 = Component(ref="J2", x=80, y=60)
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)

        constraints = [
            EdgeConstraint(reference="J1", edge="left"),
            EdgeConstraint(reference="J2", edge="right"),
        ]
        optimizer.add_edge_constraints(constraints)

        assert len(optimizer.edge_constrained_components) == 2

    def test_edge_constrained_components(self, optimizer):
        comp1 = Component(ref="J1", x=20, y=20)
        comp2 = Component(ref="R1", x=50, y=50)  # No constraint
        comp3 = Component(ref="MH1", x=80, y=60)
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)
        optimizer.add_component(comp3)

        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))
        optimizer.add_edge_constraint(EdgeConstraint(reference="MH1", edge="any"))

        constrained = optimizer.edge_constrained_components
        refs = [c.ref for c in constrained]
        assert "J1" in refs
        assert "MH1" in refs
        assert "R1" not in refs

    def test_compute_forces_includes_edge_forces(self, optimizer):
        # Component not at edge
        comp = Component(ref="J1", x=50, y=40)  # Center of board
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        forces, _ = optimizer.compute_forces_and_torques()

        # Should have force on J1 toward edge
        assert "J1" in forces
        assert forces["J1"].magnitude() > 0

    def test_optimization_moves_to_edge(self, optimizer):
        # Start component in center
        comp = Component(ref="J1", x=50, y=40, width=5, height=3)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        initial_y = comp.y

        # Run optimization
        optimizer.run(iterations=100, dt=0.01)

        # Component should have moved toward top edge (y=0)
        assert comp.y < initial_y

    def test_fixed_component_ignores_edge_constraint(self, optimizer):
        comp = Component(ref="J1", x=50, y=40, fixed=True)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        initial_y = comp.y

        optimizer.run(iterations=50, dt=0.01)

        # Fixed component should not move
        assert comp.y == initial_y


class TestEdgeConstraintSliding:
    """Tests for sliding behavior along edges."""

    @pytest.fixture
    def optimizer_with_edge_components(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        opt = PlacementOptimizer(board)

        # Add two components on same edge
        comp1 = Component(
            ref="J1",
            x=30,
            y=5,  # Near top edge
            width=10,
            height=5,
            pins=[Pin(number="1", x=30, y=5, net=1)],
        )
        comp2 = Component(
            ref="J2",
            x=70,
            y=5,  # Near top edge
            width=10,
            height=5,
            pins=[Pin(number="1", x=70, y=5, net=1)],
        )

        opt.add_component(comp1)
        opt.add_component(comp2)

        # Both constrained to top edge with sliding enabled
        opt.add_edge_constraint(EdgeConstraint(reference="J1", edge="top", slide=True))
        opt.add_edge_constraint(EdgeConstraint(reference="J2", edge="top", slide=True))

        opt.create_springs_from_nets()

        return opt

    def test_edge_components_can_slide(self, optimizer_with_edge_components):
        opt = optimizer_with_edge_components
        j1 = opt.get_component("J1")
        j2 = opt.get_component("J2")

        initial_j1_x = j1.x
        initial_j2_x = j2.x

        # Spring between them should pull them together
        opt.run(iterations=50, dt=0.01)

        # Both should stay near top edge (small y)
        assert j1.y < 20
        assert j2.y < 20

        # They should have moved closer together (spring force)
        final_distance = abs(j2.x - j1.x)
        initial_distance = abs(initial_j2_x - initial_j1_x)
        assert final_distance < initial_distance


class TestEdgeConstraintConfig:
    """Tests for edge constraint configuration."""

    def test_edge_stiffness_in_config(self):
        config = PlacementConfig()
        assert hasattr(config, "edge_stiffness")
        assert config.edge_stiffness > 0

    def test_custom_edge_stiffness(self):
        config = PlacementConfig(edge_stiffness=100.0)
        assert config.edge_stiffness == 100.0
