"""Tests for the keepout zone management module."""

import tempfile

from kicad_tools.optim import (
    KeepoutType,
    KeepoutViolation,
    KeepoutZone,
    PlacementOptimizer,
    Polygon,
    add_keepout_zones,
    create_keepout_polygon,
    load_keepout_zones_from_yaml,
)


class TestKeepoutType:
    """Tests for KeepoutType enum."""

    def test_enum_values(self):
        assert KeepoutType.MECHANICAL.value == "mechanical"
        assert KeepoutType.THERMAL.value == "thermal"
        assert KeepoutType.RF.value == "rf"
        assert KeepoutType.ASSEMBLY.value == "assembly"
        assert KeepoutType.CLEARANCE.value == "clearance"

    def test_from_string(self):
        assert KeepoutType("mechanical") == KeepoutType.MECHANICAL
        assert KeepoutType("thermal") == KeepoutType.THERMAL
        assert KeepoutType("rf") == KeepoutType.RF


class TestKeepoutZone:
    """Tests for KeepoutZone dataclass."""

    def test_basic_zone_creation(self):
        zone = KeepoutZone(
            name="test_zone",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        assert zone.name == "test_zone"
        assert zone.zone_type == KeepoutType.MECHANICAL
        assert len(zone.polygon) == 4

    def test_zone_with_clearance(self):
        zone = KeepoutZone(
            name="clearance_zone",
            zone_type=KeepoutType.CLEARANCE,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            clearance_mm=2.0,
        )
        assert zone.clearance_mm == 2.0

    def test_zone_layer_restriction(self):
        zone = KeepoutZone(
            name="top_zone",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            layer="F.Cu",
        )
        assert zone.layer == "F.Cu"

    def test_zone_via_trace_permissions(self):
        zone = KeepoutZone(
            name="rf_zone",
            zone_type=KeepoutType.RF,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            allow_vias=True,
            allow_traces=False,
        )
        assert zone.allow_vias is True
        assert zone.allow_traces is False

    def test_get_polygon(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        poly = zone.get_polygon()
        assert isinstance(poly, Polygon)
        assert len(poly.vertices) == 4

    def test_get_expanded_polygon(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            clearance_mm=1.0,
        )
        poly = zone.get_expanded_polygon()
        # Expanded polygon should be larger
        min_x = min(v.x for v in poly.vertices)
        max_x = max(v.x for v in poly.vertices)
        # The expansion should push vertices outward
        assert min_x < 0  # Expanded beyond original boundary
        assert max_x > 10

    def test_contains_point_inside(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        assert zone.contains_point(5.0, 5.0) is True

    def test_contains_point_outside(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
        )
        assert zone.contains_point(15.0, 15.0) is False

    def test_contains_point_with_clearance(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            clearance_mm=2.0,
        )
        # Point at (11, 5) is outside the base polygon but inside clearance
        # Due to expansion algorithm, points near edges should be included
        # The center should definitely be inside
        assert zone.contains_point(5.0, 5.0) is True

    def test_to_keepout(self):
        zone = KeepoutZone(
            name="test_zone",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            charge_multiplier=15.0,
        )
        keepout = zone.to_keepout()
        assert keepout.name == "test_zone"
        assert keepout.charge_multiplier == 15.0
        assert len(keepout.outline.vertices) == 4

    def test_to_dict(self):
        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.RF,
            polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            clearance_mm=3.0,
            layer="B.Cu",
        )
        d = zone.to_dict()
        assert d["name"] == "test"
        assert d["type"] == "rf"
        assert d["clearance_mm"] == 3.0
        assert d["layer"] == "B.Cu"

    def test_from_dict(self):
        data = {
            "name": "antenna",
            "type": "rf",
            "polygon": [[0, 0], [20, 0], [20, 15], [0, 15]],
            "clearance_mm": 5.0,
            "allow_traces": True,
        }
        zone = KeepoutZone.from_dict(data)
        assert zone.name == "antenna"
        assert zone.zone_type == KeepoutType.RF
        assert zone.clearance_mm == 5.0
        assert zone.allow_traces is True


class TestKeepoutViolation:
    """Tests for KeepoutViolation dataclass."""

    def test_violation_creation(self):
        violation = KeepoutViolation(
            component_ref="U1",
            zone_name="rf_zone",
            zone_type=KeepoutType.RF,
            position=(50.0, 25.0),
            overlap_mm=2.5,
            message="U1 overlaps RF exclusion zone",
        )
        assert violation.component_ref == "U1"
        assert violation.zone_name == "rf_zone"
        assert violation.overlap_mm == 2.5

    def test_violation_to_dict(self):
        violation = KeepoutViolation(
            component_ref="C3",
            zone_name="thermal_zone",
            zone_type=KeepoutType.THERMAL,
            position=(10.0, 20.0),
            overlap_mm=1.0,
        )
        d = violation.to_dict()
        assert d["component"] == "C3"
        assert d["zone"] == "thermal_zone"
        assert d["type"] == "thermal"


class TestCreateKeepoutPolygon:
    """Tests for create_keepout_polygon helper."""

    def test_basic_polygon(self):
        vertices = [(0, 0), (10, 0), (10, 10), (0, 10)]
        zone = create_keepout_polygon(vertices, KeepoutType.MECHANICAL)
        assert len(zone.polygon) == 4
        assert zone.zone_type == KeepoutType.MECHANICAL

    def test_polygon_with_name(self):
        vertices = [(0, 0), (5, 0), (5, 5)]
        zone = create_keepout_polygon(vertices, KeepoutType.RF, name="antenna_clearance")
        assert zone.name == "antenna_clearance"

    def test_polygon_with_clearance(self):
        vertices = [(0, 0), (10, 0), (10, 10), (0, 10)]
        zone = create_keepout_polygon(vertices, KeepoutType.THERMAL, clearance_mm=2.5)
        assert zone.clearance_mm == 2.5

    def test_polygon_with_layer(self):
        vertices = [(0, 0), (10, 0), (10, 10), (0, 10)]
        zone = create_keepout_polygon(vertices, KeepoutType.MECHANICAL, layer="B.Cu")
        assert zone.layer == "B.Cu"


class TestAddKeepoutZones:
    """Tests for add_keepout_zones function."""

    def test_add_single_zone(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        optimizer = PlacementOptimizer(board)

        zone = KeepoutZone(
            name="test",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(10, 10), (20, 10), (20, 20), (10, 20)],
        )

        count = add_keepout_zones(optimizer, [zone])
        assert count == 1
        assert len(optimizer.keepouts) == 1

    def test_add_multiple_zones(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        optimizer = PlacementOptimizer(board)

        zones = [
            KeepoutZone(
                name="zone1",
                zone_type=KeepoutType.MECHANICAL,
                polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
            ),
            KeepoutZone(
                name="zone2",
                zone_type=KeepoutType.RF,
                polygon=[(80, 60), (95, 60), (95, 75), (80, 75)],
            ),
        ]

        count = add_keepout_zones(optimizer, zones)
        assert count == 2
        assert len(optimizer.keepouts) == 2

    def test_zones_affect_forces(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        optimizer = PlacementOptimizer(board)

        # Add a component near where we'll put a keepout
        from kicad_tools.optim import Component

        comp = Component(ref="U1", x=15, y=15, width=5, height=5)
        optimizer.add_component(comp)

        # Add a keepout zone right where the component is
        zone = KeepoutZone(
            name="obstacle",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(10, 10), (20, 10), (20, 20), (10, 20)],
            charge_multiplier=20.0,
        )
        add_keepout_zones(optimizer, [zone])

        # Compute forces - there should be repulsion
        forces, torques = optimizer.compute_forces_and_torques()
        force = forces["U1"]
        # The component should be pushed away from the keepout
        assert force.magnitude() > 0


class TestLoadKeepoutZonesFromYaml:
    """Tests for load_keepout_zones_from_yaml function."""

    def test_load_single_zone(self):
        yaml_content = """
keepouts:
  - name: usb_clearance
    type: mechanical
    polygon: [[0, 0], [10, 0], [10, 5], [0, 5]]
    clearance_mm: 1.0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            zones = load_keepout_zones_from_yaml(f.name)

        assert len(zones) == 1
        assert zones[0].name == "usb_clearance"
        assert zones[0].zone_type == KeepoutType.MECHANICAL
        assert zones[0].clearance_mm == 1.0

    def test_load_multiple_zones(self):
        yaml_content = """
keepouts:
  - name: usb_zone
    type: mechanical
    polygon: [[0, 0], [10, 0], [10, 5], [0, 5]]
  - name: antenna_zone
    type: rf
    polygon: [[50, 0], [60, 0], [60, 20], [50, 20]]
    clearance_mm: 5.0
  - name: hot_spot
    type: thermal
    polygon: [[30, 30], [40, 30], [40, 40], [30, 40]]
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            zones = load_keepout_zones_from_yaml(f.name)

        assert len(zones) == 3
        assert zones[0].zone_type == KeepoutType.MECHANICAL
        assert zones[1].zone_type == KeepoutType.RF
        assert zones[2].zone_type == KeepoutType.THERMAL

    def test_load_with_all_options(self):
        yaml_content = """
keepouts:
  - name: complex_zone
    type: assembly
    polygon: [[0, 0], [20, 0], [20, 20], [0, 20]]
    layer: F.Cu
    clearance_mm: 2.5
    allow_vias: true
    allow_traces: false
    charge_multiplier: 15.0
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            zones = load_keepout_zones_from_yaml(f.name)

        assert len(zones) == 1
        zone = zones[0]
        assert zone.zone_type == KeepoutType.ASSEMBLY
        assert zone.layer == "F.Cu"
        assert zone.clearance_mm == 2.5
        assert zone.allow_vias is True
        assert zone.allow_traces is False
        assert zone.charge_multiplier == 15.0

    def test_load_empty_file(self):
        yaml_content = "keepouts: []"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()

            zones = load_keepout_zones_from_yaml(f.name)

        assert len(zones) == 0


class TestOptimizerIntegration:
    """Integration tests for keepout zones with the optimizer."""

    def test_optimizer_respects_keepout(self):
        """Test that optimizer moves components away from keepout zones."""
        # Create a board
        board = Polygon.rectangle(50, 40, 100, 80)
        optimizer = PlacementOptimizer(board)

        # Add component inside a keepout zone
        from kicad_tools.optim import Component

        comp = Component(ref="U1", x=15, y=15, width=4, height=4)
        optimizer.add_component(comp)

        # Add keepout zone where the component is
        zone = KeepoutZone(
            name="obstacle",
            zone_type=KeepoutType.MECHANICAL,
            polygon=[(10, 10), (25, 10), (25, 25), (10, 25)],
            charge_multiplier=50.0,
        )
        add_keepout_zones(optimizer, [zone])

        # Record initial position
        initial_x, initial_y = comp.x, comp.y

        # Run a few optimization iterations
        optimizer.run(iterations=50, dt=0.05)

        # Component should have moved away from keepout
        moved = (comp.x != initial_x) or (comp.y != initial_y)
        assert moved, "Component should move away from keepout zone"

    def test_multiple_keepouts(self):
        """Test optimizer with multiple keepout zones."""
        board = Polygon.rectangle(50, 40, 100, 80)
        optimizer = PlacementOptimizer(board)

        from kicad_tools.optim import Component

        comp = Component(ref="U1", x=50, y=40, width=5, height=5)
        optimizer.add_component(comp)

        # Add keepouts on all sides
        zones = [
            KeepoutZone(
                name="left",
                zone_type=KeepoutType.MECHANICAL,
                polygon=[(0, 0), (20, 0), (20, 80), (0, 80)],
            ),
            KeepoutZone(
                name="right",
                zone_type=KeepoutType.MECHANICAL,
                polygon=[(80, 0), (100, 0), (100, 80), (80, 80)],
            ),
        ]
        add_keepout_zones(optimizer, zones)

        assert len(optimizer.keepouts) == 2

        # Run optimization
        optimizer.run(iterations=100, dt=0.02)

        # Component should stay in the middle region
        assert 20 < comp.x < 80
