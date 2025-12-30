"""Tests for the optimization module."""

import math
import pytest

from kicad_tools.optim import (
    Component,
    FigureOfMerit,
    Keepout,
    Pin,
    PlacementConfig,
    PlacementOptimizer,
    Polygon,
    RoutingOptimizer,
    Spring,
    Vector2D,
)


class TestVector2D:
    """Tests for Vector2D dataclass."""

    def test_default_values(self):
        v = Vector2D()
        assert v.x == 0.0
        assert v.y == 0.0

    def test_initialization(self):
        v = Vector2D(3.0, 4.0)
        assert v.x == 3.0
        assert v.y == 4.0

    def test_addition(self):
        v1 = Vector2D(1.0, 2.0)
        v2 = Vector2D(3.0, 4.0)
        result = v1 + v2
        assert result.x == 4.0
        assert result.y == 6.0

    def test_subtraction(self):
        v1 = Vector2D(5.0, 7.0)
        v2 = Vector2D(2.0, 3.0)
        result = v1 - v2
        assert result.x == 3.0
        assert result.y == 4.0

    def test_scalar_multiplication(self):
        v = Vector2D(2.0, 3.0)
        result = v * 2.0
        assert result.x == 4.0
        assert result.y == 6.0

    def test_reverse_scalar_multiplication(self):
        v = Vector2D(2.0, 3.0)
        result = 2.0 * v
        assert result.x == 4.0
        assert result.y == 6.0

    def test_scalar_division(self):
        v = Vector2D(4.0, 6.0)
        result = v / 2.0
        assert result.x == 2.0
        assert result.y == 3.0

    def test_negation(self):
        v = Vector2D(3.0, -4.0)
        result = -v
        assert result.x == -3.0
        assert result.y == 4.0

    def test_dot_product(self):
        v1 = Vector2D(1.0, 2.0)
        v2 = Vector2D(3.0, 4.0)
        result = v1.dot(v2)
        assert result == 11.0  # 1*3 + 2*4

    def test_cross_product(self):
        v1 = Vector2D(1.0, 0.0)
        v2 = Vector2D(0.0, 1.0)
        result = v1.cross(v2)
        assert result == 1.0  # 1*1 - 0*0

    def test_magnitude(self):
        v = Vector2D(3.0, 4.0)
        assert v.magnitude() == 5.0

    def test_magnitude_squared(self):
        v = Vector2D(3.0, 4.0)
        assert v.magnitude_squared() == 25.0

    def test_normalized(self):
        v = Vector2D(3.0, 4.0)
        norm = v.normalized()
        assert abs(norm.x - 0.6) < 1e-10
        assert abs(norm.y - 0.8) < 1e-10
        assert abs(norm.magnitude() - 1.0) < 1e-10

    def test_normalized_zero_vector(self):
        v = Vector2D(0.0, 0.0)
        norm = v.normalized()
        assert norm.x == 0.0
        assert norm.y == 0.0

    def test_rotated_90_degrees(self):
        v = Vector2D(1.0, 0.0)
        rotated = v.rotated(90.0)
        assert abs(rotated.x) < 1e-10
        assert abs(rotated.y - 1.0) < 1e-10

    def test_rotated_180_degrees(self):
        v = Vector2D(1.0, 0.0)
        rotated = v.rotated(180.0)
        assert abs(rotated.x + 1.0) < 1e-10
        assert abs(rotated.y) < 1e-10

    def test_rotated_270_degrees(self):
        v = Vector2D(1.0, 0.0)
        rotated = v.rotated(270.0)
        assert abs(rotated.x) < 1e-10
        assert abs(rotated.y + 1.0) < 1e-10

    def test_perpendicular(self):
        v = Vector2D(1.0, 0.0)
        perp = v.perpendicular()
        assert perp.x == 0.0
        assert perp.y == 1.0
        # Perpendicular should be 90 degrees CCW
        assert v.dot(perp) == 0.0


class TestPolygon:
    """Tests for Polygon dataclass."""

    def test_default_empty(self):
        p = Polygon()
        assert p.vertices == []

    def test_rectangle(self):
        rect = Polygon.rectangle(0, 0, 4, 2)
        assert len(rect.vertices) == 4
        # Check corners
        xs = [v.x for v in rect.vertices]
        ys = [v.y for v in rect.vertices]
        assert min(xs) == -2.0
        assert max(xs) == 2.0
        assert min(ys) == -1.0
        assert max(ys) == 1.0

    def test_rectangle_offset(self):
        rect = Polygon.rectangle(10, 20, 4, 2)
        centroid = rect.centroid()
        assert abs(centroid.x - 10) < 1e-10
        assert abs(centroid.y - 20) < 1e-10

    def test_circle(self):
        circle = Polygon.circle(0, 0, 1.0, segments=8)
        assert len(circle.vertices) == 8
        # All vertices should be at distance 1 from origin
        for v in circle.vertices:
            dist = v.magnitude()
            assert abs(dist - 1.0) < 1e-10

    def test_from_footprint_bounds(self):
        fp = Polygon.from_footprint_bounds(0, 0, 2, 1, rotation=0)
        assert len(fp.vertices) == 4

    def test_from_footprint_bounds_rotated(self):
        fp = Polygon.from_footprint_bounds(0, 0, 2, 1, rotation=90)
        # After 90 degree rotation, what was width becomes height
        xs = [v.x for v in fp.vertices]
        ys = [v.y for v in fp.vertices]
        assert abs(max(xs) - min(xs) - 1.0) < 1e-10  # Originally height
        assert abs(max(ys) - min(ys) - 2.0) < 1e-10  # Originally width

    def test_edges(self):
        rect = Polygon.rectangle(0, 0, 2, 2)
        edges = list(rect.edges())
        assert len(edges) == 4
        # Each edge connects consecutive vertices
        for i, (start, end) in enumerate(edges):
            assert start == rect.vertices[i]
            assert end == rect.vertices[(i + 1) % 4]

    def test_centroid(self):
        rect = Polygon.rectangle(5, 10, 4, 2)
        c = rect.centroid()
        assert abs(c.x - 5) < 1e-10
        assert abs(c.y - 10) < 1e-10

    def test_centroid_empty(self):
        p = Polygon()
        c = p.centroid()
        assert c.x == 0.0
        assert c.y == 0.0

    def test_area_rectangle(self):
        rect = Polygon.rectangle(0, 0, 4, 3)
        # Area of 4x3 rectangle = 12
        assert abs(abs(rect.area()) - 12.0) < 1e-10

    def test_area_triangle(self):
        triangle = Polygon(vertices=[
            Vector2D(0, 0),
            Vector2D(4, 0),
            Vector2D(0, 3),
        ])
        # Area of right triangle = 0.5 * 4 * 3 = 6
        assert abs(abs(triangle.area()) - 6.0) < 1e-10

    def test_area_empty(self):
        p = Polygon()
        assert p.area() == 0.0

    def test_area_line(self):
        p = Polygon(vertices=[Vector2D(0, 0), Vector2D(1, 1)])
        assert p.area() == 0.0

    def test_perimeter_rectangle(self):
        rect = Polygon.rectangle(0, 0, 4, 3)
        # Perimeter = 2*(4+3) = 14
        assert abs(rect.perimeter() - 14.0) < 1e-10

    def test_contains_point_inside(self):
        rect = Polygon.rectangle(0, 0, 4, 4)
        assert rect.contains_point(Vector2D(0, 0)) is True
        assert rect.contains_point(Vector2D(1, 1)) is True

    def test_contains_point_outside(self):
        rect = Polygon.rectangle(0, 0, 4, 4)
        assert rect.contains_point(Vector2D(10, 10)) is False
        assert rect.contains_point(Vector2D(-10, 0)) is False

    def test_translate(self):
        rect = Polygon.rectangle(0, 0, 2, 2)
        translated = rect.translate(Vector2D(5, 10))
        c = translated.centroid()
        assert abs(c.x - 5) < 1e-10
        assert abs(c.y - 10) < 1e-10

    def test_rotate_around(self):
        rect = Polygon.rectangle(1, 0, 1, 1)
        # Rotate 180 degrees around origin
        rotated = rect.rotate_around(Vector2D(0, 0), 180)
        c = rotated.centroid()
        assert abs(c.x + 1) < 1e-10
        assert abs(c.y) < 1e-10


class TestComponent:
    """Tests for Component dataclass."""

    @pytest.fixture
    def simple_component(self):
        return Component(
            ref="U1",
            x=10.0,
            y=20.0,
            rotation=0.0,
            width=5.0,
            height=3.0,
            pins=[
                Pin(number="1", x=8.0, y=20.0, net=1, net_name="NET1"),
                Pin(number="2", x=12.0, y=20.0, net=2, net_name="GND"),
            ],
        )

    def test_default_values(self):
        comp = Component(ref="R1")
        assert comp.x == 0.0
        assert comp.y == 0.0
        assert comp.rotation == 0.0
        assert comp.width == 1.0
        assert comp.height == 1.0
        assert comp.fixed is False
        assert comp.mass == 1.0
        assert comp.vx == 0.0
        assert comp.vy == 0.0
        assert comp.angular_velocity == 0.0

    def test_position(self, simple_component):
        pos = simple_component.position()
        assert pos.x == 10.0
        assert pos.y == 20.0

    def test_velocity(self, simple_component):
        simple_component.vx = 1.0
        simple_component.vy = 2.0
        vel = simple_component.velocity()
        assert vel.x == 1.0
        assert vel.y == 2.0

    def test_outline(self, simple_component):
        outline = simple_component.outline()
        assert len(outline.vertices) == 4
        c = outline.centroid()
        assert abs(c.x - 10.0) < 1e-10
        assert abs(c.y - 20.0) < 1e-10

    def test_apply_force(self, simple_component):
        force = Vector2D(10.0, 0.0)
        dt = 0.1
        simple_component.apply_force(force, dt)
        # F = ma, so a = F/m = 10/1 = 10
        # v = v0 + a*dt = 0 + 10*0.1 = 1.0
        assert abs(simple_component.vx - 1.0) < 1e-10
        assert simple_component.vy == 0.0

    def test_apply_force_fixed(self, simple_component):
        simple_component.fixed = True
        force = Vector2D(10.0, 0.0)
        simple_component.apply_force(force, 0.1)
        # Fixed component should not move
        assert simple_component.vx == 0.0
        assert simple_component.vy == 0.0

    def test_apply_torque(self, simple_component):
        torque = 10.0
        dt = 0.1
        simple_component.apply_torque(torque, dt)
        # Should have some angular velocity now
        assert simple_component.angular_velocity != 0.0

    def test_apply_torque_fixed(self, simple_component):
        simple_component.fixed = True
        simple_component.apply_torque(10.0, 0.1)
        assert simple_component.angular_velocity == 0.0

    def test_update_position(self, simple_component):
        simple_component.vx = 1.0
        simple_component.vy = 2.0
        simple_component.angular_velocity = 10.0
        dt = 0.1
        simple_component.update_position(dt)
        assert abs(simple_component.x - 10.1) < 1e-10
        assert abs(simple_component.y - 20.2) < 1e-10
        assert simple_component.rotation > 0.0

    def test_update_position_fixed(self, simple_component):
        simple_component.fixed = True
        simple_component.vx = 1.0
        simple_component.vy = 2.0
        original_x = simple_component.x
        original_y = simple_component.y
        simple_component.update_position(0.1)
        assert simple_component.x == original_x
        assert simple_component.y == original_y

    def test_apply_damping(self, simple_component):
        simple_component.vx = 10.0
        simple_component.vy = 10.0
        simple_component.angular_velocity = 10.0
        simple_component.apply_damping(0.5, 0.5)
        assert simple_component.vx == 5.0
        assert simple_component.vy == 5.0
        assert simple_component.angular_velocity == 5.0

    def test_rotation_potential_torque_at_zero(self, simple_component):
        simple_component.rotation = 0.0
        torque = simple_component.compute_rotation_potential_torque(1.0)
        # At 0 degrees (a minimum), torque should be ~0
        assert abs(torque) < 1e-10

    def test_rotation_potential_torque_at_45(self, simple_component):
        simple_component.rotation = 45.0
        torque = simple_component.compute_rotation_potential_torque(1.0)
        # At 45 degrees (a maximum), torque should be non-zero
        assert torque != 0.0

    def test_rotation_potential_energy_at_zero(self, simple_component):
        simple_component.rotation = 0.0
        energy = simple_component.rotation_potential_energy(1.0)
        # At 0 degrees (a minimum), energy should be 0
        assert abs(energy) < 1e-10

    def test_rotation_potential_energy_at_45(self, simple_component):
        simple_component.rotation = 45.0
        energy = simple_component.rotation_potential_energy(1.0)
        # At 45 degrees (a maximum), energy should be positive
        assert energy > 0

    def test_update_pin_positions(self, simple_component):
        # Store original relative position
        original_dx = simple_component.pins[0].x - simple_component.x
        # Move component
        simple_component.x = 20.0
        simple_component.update_pin_positions()
        # Pin should have moved with component
        new_dx = simple_component.pins[0].x - simple_component.x
        assert abs(new_dx - original_dx) < 1e-10


class TestSpring:
    """Tests for Spring dataclass."""

    def test_default_values(self):
        spring = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="2",
        )
        assert spring.stiffness == 1.0
        assert spring.rest_length == 0.0
        assert spring.net == 0
        assert spring.net_name == ""


class TestKeeout:
    """Tests for Keepout dataclass."""

    def test_initialization(self):
        outline = Polygon.circle(0, 0, 5.0)
        keepout = Keepout(outline=outline, charge_multiplier=5.0, name="Mounting Hole")
        assert keepout.charge_multiplier == 5.0
        assert keepout.name == "Mounting Hole"


class TestPlacementConfig:
    """Tests for PlacementConfig dataclass."""

    def test_default_values(self):
        config = PlacementConfig()
        assert config.charge_density == 100.0
        assert config.min_distance == 0.5
        assert config.spring_stiffness == 10.0
        assert config.damping == 0.95
        assert config.max_velocity == 10.0


class TestPlacementOptimizer:
    """Tests for PlacementOptimizer."""

    @pytest.fixture
    def simple_optimizer(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        return PlacementOptimizer(board)

    @pytest.fixture
    def optimizer_with_components(self, simple_optimizer):
        comp1 = Component(
            ref="U1",
            x=30.0,
            y=40.0,
            width=10.0,
            height=8.0,
            pins=[
                Pin(number="1", x=25.0, y=40.0, net=1, net_name="NET1"),
                Pin(number="2", x=35.0, y=40.0, net=2, net_name="GND"),
            ],
        )
        comp2 = Component(
            ref="R1",
            x=70.0,
            y=60.0,
            width=4.0,
            height=2.0,
            pins=[
                Pin(number="1", x=68.0, y=60.0, net=1, net_name="NET1"),
                Pin(number="2", x=72.0, y=60.0, net=2, net_name="GND"),
            ],
        )
        simple_optimizer.add_component(comp1)
        simple_optimizer.add_component(comp2)
        simple_optimizer.create_springs_from_nets()
        return simple_optimizer

    def test_initialization(self, simple_optimizer):
        assert len(simple_optimizer.components) == 0
        assert len(simple_optimizer.springs) == 0
        assert len(simple_optimizer.keepouts) == 0
        assert simple_optimizer.config is not None

    def test_initialization_with_config(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = PlacementConfig(damping=0.8)
        opt = PlacementOptimizer(board, config)
        assert opt.config.damping == 0.8

    def test_add_component(self, simple_optimizer):
        comp = Component(ref="U1", x=50.0, y=50.0)
        simple_optimizer.add_component(comp)
        assert len(simple_optimizer.components) == 1
        assert simple_optimizer.get_component("U1") == comp

    def test_get_component_not_found(self, simple_optimizer):
        assert simple_optimizer.get_component("NONEXISTENT") is None

    def test_add_keepout(self, simple_optimizer):
        outline = Polygon.circle(50, 50, 5.0)
        keepout = simple_optimizer.add_keepout(outline, charge_multiplier=5.0, name="Hole")
        assert len(simple_optimizer.keepouts) == 1
        assert keepout.name == "Hole"

    def test_add_keepout_circle(self, simple_optimizer):
        keepout = simple_optimizer.add_keepout_circle(50, 50, 5.0, charge_multiplier=10.0, name="MH1")
        assert len(simple_optimizer.keepouts) == 1
        assert keepout.name == "MH1"

    def test_create_springs_from_nets(self, optimizer_with_components):
        # Should have created springs for NET1 and GND
        assert len(optimizer_with_components.springs) >= 2

    def test_is_power_net(self, simple_optimizer):
        assert simple_optimizer._is_power_net("VCC") is True
        assert simple_optimizer._is_power_net("GND") is True
        assert simple_optimizer._is_power_net("+3.3V") is True
        assert simple_optimizer._is_power_net("+5V") is True
        assert simple_optimizer._is_power_net("NET1") is False

    def test_is_clock_net(self, simple_optimizer):
        assert simple_optimizer._is_clock_net("CLK") is True
        assert simple_optimizer._is_clock_net("MCLK") is True
        assert simple_optimizer._is_clock_net("SPI_SCLK") is True
        assert simple_optimizer._is_clock_net("NET1") is False

    def test_compute_edge_to_point_force(self, simple_optimizer):
        point = Vector2D(5.0, 0.0)
        edge_start = Vector2D(0.0, -10.0)
        edge_end = Vector2D(0.0, 10.0)
        force = simple_optimizer.compute_edge_to_point_force(
            point, edge_start, edge_end, charge_density=100.0
        )
        # Force should point away from edge (positive x direction)
        assert force.x > 0
        assert abs(force.y) < 1e-10

    def test_compute_spring_force(self, optimizer_with_components):
        # Springs should exist
        assert len(optimizer_with_components.springs) > 0
        spring = optimizer_with_components.springs[0]
        force1, force2 = optimizer_with_components.compute_spring_force(spring)
        # Forces should be opposite
        assert abs(force1.x + force2.x) < 1e-10
        assert abs(force1.y + force2.y) < 1e-10

    def test_compute_spring_force_missing_component(self, simple_optimizer):
        spring = Spring(comp1_ref="MISSING", pin1_num="1", comp2_ref="ALSO_MISSING", pin2_num="1")
        force1, force2 = simple_optimizer.compute_spring_force(spring)
        assert force1.magnitude() == 0.0
        assert force2.magnitude() == 0.0

    def test_compute_boundary_force(self, simple_optimizer):
        # Point near edge should feel repulsion from edge
        point = Vector2D(5.0, 50.0)  # Near left edge
        force = simple_optimizer.compute_boundary_force(point)
        # Force should push toward center (positive x)
        assert force.x > 0

    def test_compute_forces(self, optimizer_with_components):
        forces = optimizer_with_components.compute_forces()
        assert "U1" in forces
        assert "R1" in forces
        # Forces should be Vector2D
        assert isinstance(forces["U1"], Vector2D)

    def test_compute_forces_and_torques(self, optimizer_with_components):
        forces, torques = optimizer_with_components.compute_forces_and_torques()
        assert "U1" in forces
        assert "U1" in torques

    def test_compute_energy(self, optimizer_with_components):
        energy = optimizer_with_components.compute_energy()
        # Energy should be non-negative
        assert energy >= 0

    def test_step(self, optimizer_with_components):
        initial_x = optimizer_with_components.components[0].x
        # Give component some velocity
        optimizer_with_components.components[0].vx = 1.0
        optimizer_with_components.step(0.1)
        # Position should have changed
        assert optimizer_with_components.components[0].x != initial_x

    def test_run_basic(self, optimizer_with_components):
        iterations = optimizer_with_components.run(iterations=10, dt=0.01)
        assert iterations <= 10

    def test_run_with_callback(self, optimizer_with_components):
        energies = []

        def callback(iteration, energy):
            energies.append(energy)

        optimizer_with_components.run(iterations=5, dt=0.01, callback=callback)
        assert len(energies) == 5

    def test_snap_rotations_to_90(self, optimizer_with_components):
        optimizer_with_components.components[0].rotation = 47.0
        optimizer_with_components.components[1].rotation = 92.0
        optimizer_with_components.snap_rotations_to_90()
        assert optimizer_with_components.components[0].rotation == 45.0 or \
               optimizer_with_components.components[0].rotation == 90.0 or \
               optimizer_with_components.components[0].rotation == 0.0
        # 47 rounds to 45 * not 90 degree slots - let me check the implementation
        # It snaps to 0, 90, 180, 270 - so 47 should snap to 0
        # Actually: round(47/90) = round(0.52) = 1, so 1*90 = 90
        # Let's just verify it's a multiple of 90
        assert optimizer_with_components.components[0].rotation % 90 == 0
        assert optimizer_with_components.components[1].rotation % 90 == 0

    def test_total_wire_length(self, optimizer_with_components):
        length = optimizer_with_components.total_wire_length()
        # Should be positive since components are not at same location
        assert length > 0

    def test_report(self, optimizer_with_components):
        report = optimizer_with_components.report()
        assert "Placement Optimizer Report" in report
        assert "U1" in report
        assert "R1" in report
        assert "Components:" in report

    def test_fixed_component_does_not_move(self, simple_optimizer):
        comp = Component(ref="U1", x=50.0, y=50.0, fixed=True)
        simple_optimizer.add_component(comp)
        initial_x = comp.x
        initial_y = comp.y
        simple_optimizer.run(iterations=10, dt=0.1)
        assert comp.x == initial_x
        assert comp.y == initial_y


class TestRoutingOptimizer:
    """Tests for RoutingOptimizer (placeholder)."""

    def test_not_implemented(self):
        with pytest.raises(NotImplementedError):
            RoutingOptimizer()


class TestFigureOfMerit:
    """Tests for FigureOfMerit."""

    def test_instantiation(self):
        # FigureOfMerit is a placeholder class
        fom = FigureOfMerit()
        assert fom is not None


class TestPin:
    """Tests for Pin dataclass."""

    def test_default_values(self):
        pin = Pin(number="1", x=0.0, y=0.0)
        assert pin.net == 0
        assert pin.net_name == ""

    def test_with_net(self):
        pin = Pin(number="1", x=5.0, y=10.0, net=1, net_name="VCC")
        assert pin.number == "1"
        assert pin.x == 5.0
        assert pin.y == 10.0
        assert pin.net == 1
        assert pin.net_name == "VCC"


class TestPlacementOptimizerIntegration:
    """Integration tests for PlacementOptimizer."""

    def test_optimization_reduces_wire_length(self):
        """Test that optimization tends to reduce total wire length."""
        board = Polygon.rectangle(50, 50, 100, 80)
        optimizer = PlacementOptimizer(board)

        # Two components connected by a net, placed far apart
        comp1 = Component(
            ref="U1",
            x=10.0,
            y=10.0,
            width=5.0,
            height=5.0,
            pins=[Pin(number="1", x=10.0, y=10.0, net=1, net_name="NET1")],
        )
        comp2 = Component(
            ref="R1",
            x=90.0,
            y=90.0,
            width=2.0,
            height=1.0,
            pins=[Pin(number="1", x=90.0, y=90.0, net=1, net_name="NET1")],
        )
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)
        optimizer.create_springs_from_nets()

        initial_length = optimizer.total_wire_length()

        # Run optimization
        optimizer.run(iterations=100, dt=0.01)

        final_length = optimizer.total_wire_length()

        # Wire length should decrease (spring pulls components together)
        assert final_length < initial_length

    def test_keepout_repels_components(self):
        """Test that keepout zones repel components."""
        board = Polygon.rectangle(50, 50, 100, 80)
        optimizer = PlacementOptimizer(board)

        # Component near a keepout
        comp = Component(ref="U1", x=52.0, y=50.0, width=5.0, height=5.0)
        optimizer.add_component(comp)

        # Keepout at center
        optimizer.add_keepout_circle(50.0, 50.0, 3.0, charge_multiplier=100.0)

        initial_x = comp.x

        # Run optimization
        optimizer.run(iterations=50, dt=0.01)

        # Component should have moved away from keepout (larger x)
        assert comp.x > initial_x

    def test_convergence_detection(self):
        """Test that simulation can converge early."""
        board = Polygon.rectangle(50, 50, 100, 80)
        config = PlacementConfig(
            energy_threshold=1000.0,  # Very high threshold
            velocity_threshold=100.0,  # Very high threshold
        )
        optimizer = PlacementOptimizer(board, config)

        # Add a single fixed component
        comp = Component(ref="U1", x=50.0, y=50.0, fixed=True)
        optimizer.add_component(comp)

        # Should converge immediately (no movable components)
        iterations = optimizer.run(iterations=1000, dt=0.01)
        assert iterations < 1000
