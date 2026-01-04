"""Tests for the grouping constraints module."""

import tempfile
from pathlib import Path

import pytest

from kicad_tools.optim import (
    Component,
    ConstraintType,
    GroupingConstraint,
    PlacementOptimizer,
    Polygon,
    SpatialConstraint,
    expand_member_patterns,
    load_constraints_from_yaml,
    save_constraints_to_yaml,
    validate_grouping_constraints,
)


class TestSpatialConstraint:
    """Tests for SpatialConstraint dataclass."""

    def test_max_distance_factory(self):
        constraint = SpatialConstraint.max_distance(anchor="U1", radius_mm=10.0)
        assert constraint.constraint_type == ConstraintType.MAX_DISTANCE
        assert constraint.parameters["anchor"] == "U1"
        assert constraint.parameters["radius_mm"] == 10.0

    def test_alignment_factory(self):
        constraint = SpatialConstraint.alignment(axis="horizontal", tolerance_mm=0.25)
        assert constraint.constraint_type == ConstraintType.ALIGNMENT
        assert constraint.parameters["axis"] == "horizontal"
        assert constraint.parameters["tolerance_mm"] == 0.25

    def test_ordering_factory(self):
        constraint = SpatialConstraint.ordering(axis="horizontal", order=["LED1", "LED2", "LED3"])
        assert constraint.constraint_type == ConstraintType.ORDERING
        assert constraint.parameters["axis"] == "horizontal"
        assert constraint.parameters["order"] == ["LED1", "LED2", "LED3"]

    def test_within_box_factory(self):
        constraint = SpatialConstraint.within_box(x=10, y=20, width=30, height=40)
        assert constraint.constraint_type == ConstraintType.WITHIN_BOX
        assert constraint.parameters["x"] == 10
        assert constraint.parameters["y"] == 20
        assert constraint.parameters["width"] == 30
        assert constraint.parameters["height"] == 40

    def test_relative_position_factory(self):
        constraint = SpatialConstraint.relative_position(
            reference="U1", dx=5.0, dy=10.0, tolerance_mm=0.5
        )
        assert constraint.constraint_type == ConstraintType.RELATIVE_POSITION
        assert constraint.parameters["reference"] == "U1"
        assert constraint.parameters["dx"] == 5.0
        assert constraint.parameters["dy"] == 10.0
        assert constraint.parameters["tolerance_mm"] == 0.5


class TestGroupingConstraint:
    """Tests for GroupingConstraint dataclass."""

    def test_basic_creation(self):
        constraint = GroupingConstraint(
            name="status_leds",
            members=["LED1", "LED2", "LED3"],
            constraints=[SpatialConstraint.alignment(axis="horizontal")],
        )
        assert constraint.name == "status_leds"
        assert constraint.members == ["LED1", "LED2", "LED3"]
        assert len(constraint.constraints) == 1

    def test_get_resolved_members_exact(self):
        constraint = GroupingConstraint(
            name="test",
            members=["LED1", "LED2", "LED3"],
        )
        all_refs = ["LED1", "LED2", "LED3", "LED4", "R1", "R2"]
        resolved = constraint.get_resolved_members(all_refs)
        assert resolved == ["LED1", "LED2", "LED3"]

    def test_get_resolved_members_pattern(self):
        constraint = GroupingConstraint(
            name="test",
            members=["LED*"],
        )
        all_refs = ["LED1", "LED2", "LED3", "LED4", "R1", "R2"]
        resolved = constraint.get_resolved_members(all_refs)
        assert set(resolved) == {"LED1", "LED2", "LED3", "LED4"}

    def test_get_resolved_members_mixed(self):
        constraint = GroupingConstraint(
            name="test",
            members=["R1", "LED*"],
        )
        all_refs = ["LED1", "LED2", "R1", "R2"]
        resolved = constraint.get_resolved_members(all_refs)
        assert "R1" in resolved
        assert "LED1" in resolved
        assert "LED2" in resolved


class TestExpandMemberPatterns:
    """Tests for expand_member_patterns function."""

    def test_exact_match(self):
        result = expand_member_patterns(["LED1"], ["LED1", "LED2", "R1"])
        assert result == ["LED1"]

    def test_no_match(self):
        result = expand_member_patterns(["LED5"], ["LED1", "LED2", "R1"])
        assert result == []

    def test_glob_star(self):
        result = expand_member_patterns(["LED*"], ["LED1", "LED2", "LED10", "R1"])
        assert set(result) == {"LED1", "LED2", "LED10"}

    def test_glob_question(self):
        result = expand_member_patterns(["C1?"], ["C10", "C11", "C12", "C1", "C100"])
        assert set(result) == {"C10", "C11", "C12"}

    def test_glob_range(self):
        result = expand_member_patterns(["R[1-3]"], ["R1", "R2", "R3", "R4", "R5"])
        assert set(result) == {"R1", "R2", "R3"}

    def test_deduplication(self):
        result = expand_member_patterns(["LED1", "LED*"], ["LED1", "LED2"])
        # LED1 should appear only once
        assert result.count("LED1") == 1


class TestConstraintViolationDataclass:
    """Tests for ConstraintViolation dataclass."""

    def test_basic_creation(self):
        from kicad_tools.optim.constraints import ConstraintViolation as CV

        violation = CV(
            group_name="test_group",
            constraint_type=ConstraintType.ALIGNMENT,
            message="Components not aligned",
            components=["LED1", "LED2"],
            severity=0.5,
        )
        assert violation.group_name == "test_group"
        assert violation.constraint_type == ConstraintType.ALIGNMENT
        assert "LED1" in violation.components
        assert violation.severity == 0.5

    def test_str_representation(self):
        from kicad_tools.optim.constraints import ConstraintViolation as CV

        violation = CV(
            group_name="test_group",
            constraint_type=ConstraintType.ALIGNMENT,
            message="Components not aligned",
            components=["LED1", "LED2"],
        )
        s = str(violation)
        assert "test_group" in s
        assert "alignment" in s


class TestValidateMaxDistance:
    """Tests for max_distance constraint validation."""

    @pytest.fixture
    def components(self):
        return [
            Component(ref="U1", x=50.0, y=50.0),
            Component(ref="C1", x=55.0, y=50.0),  # 5mm from U1
            Component(ref="C2", x=60.0, y=50.0),  # 10mm from U1
            Component(ref="C3", x=70.0, y=50.0),  # 20mm from U1
        ]

    def test_all_within_radius(self, components):
        constraints = [
            GroupingConstraint(
                name="power_section",
                members=["U1", "C1", "C2"],
                constraints=[SpatialConstraint.max_distance(anchor="U1", radius_mm=15.0)],
            )
        ]
        violations = validate_grouping_constraints(components, constraints)
        assert len(violations) == 0

    def test_some_outside_radius(self, components):
        constraints = [
            GroupingConstraint(
                name="power_section",
                members=["U1", "C1", "C2", "C3"],
                constraints=[SpatialConstraint.max_distance(anchor="U1", radius_mm=15.0)],
            )
        ]
        violations = validate_grouping_constraints(components, constraints)
        assert len(violations) == 1
        assert "C3" in violations[0].components


class TestValidateAlignment:
    """Tests for alignment constraint validation."""

    @pytest.fixture
    def aligned_components(self):
        return [
            Component(ref="LED1", x=10.0, y=50.0),
            Component(ref="LED2", x=20.0, y=50.0),
            Component(ref="LED3", x=30.0, y=50.0),
        ]

    @pytest.fixture
    def misaligned_components(self):
        return [
            Component(ref="LED1", x=10.0, y=50.0),
            Component(ref="LED2", x=20.0, y=52.0),  # 2mm off
            Component(ref="LED3", x=30.0, y=50.0),
        ]

    def test_horizontal_alignment_satisfied(self, aligned_components):
        constraints = [
            GroupingConstraint(
                name="status_leds",
                members=["LED1", "LED2", "LED3"],
                constraints=[SpatialConstraint.alignment(axis="horizontal", tolerance_mm=0.5)],
            )
        ]
        violations = validate_grouping_constraints(aligned_components, constraints)
        assert len(violations) == 0

    def test_horizontal_alignment_violated(self, misaligned_components):
        constraints = [
            GroupingConstraint(
                name="status_leds",
                members=["LED1", "LED2", "LED3"],
                constraints=[SpatialConstraint.alignment(axis="horizontal", tolerance_mm=0.5)],
            )
        ]
        violations = validate_grouping_constraints(misaligned_components, constraints)
        assert len(violations) == 1
        assert violations[0].constraint_type == ConstraintType.ALIGNMENT


class TestValidateOrdering:
    """Tests for ordering constraint validation."""

    @pytest.fixture
    def ordered_components(self):
        return [
            Component(ref="LED1", x=10.0, y=50.0),
            Component(ref="LED2", x=20.0, y=50.0),
            Component(ref="LED3", x=30.0, y=50.0),
        ]

    @pytest.fixture
    def unordered_components(self):
        return [
            Component(ref="LED1", x=30.0, y=50.0),  # Wrong position
            Component(ref="LED2", x=20.0, y=50.0),
            Component(ref="LED3", x=10.0, y=50.0),  # Wrong position
        ]

    def test_ordering_satisfied(self, ordered_components):
        constraints = [
            GroupingConstraint(
                name="status_leds",
                members=["LED1", "LED2", "LED3"],
                constraints=[
                    SpatialConstraint.ordering(axis="horizontal", order=["LED1", "LED2", "LED3"])
                ],
            )
        ]
        violations = validate_grouping_constraints(ordered_components, constraints)
        assert len(violations) == 0

    def test_ordering_violated(self, unordered_components):
        constraints = [
            GroupingConstraint(
                name="status_leds",
                members=["LED1", "LED2", "LED3"],
                constraints=[
                    SpatialConstraint.ordering(axis="horizontal", order=["LED1", "LED2", "LED3"])
                ],
            )
        ]
        violations = validate_grouping_constraints(unordered_components, constraints)
        assert len(violations) == 1
        assert violations[0].constraint_type == ConstraintType.ORDERING


class TestValidateWithinBox:
    """Tests for within_box constraint validation."""

    @pytest.fixture
    def components_in_box(self):
        return [
            Component(ref="C1", x=15.0, y=15.0),
            Component(ref="C2", x=20.0, y=20.0),
        ]

    @pytest.fixture
    def components_outside_box(self):
        return [
            Component(ref="C1", x=15.0, y=15.0),  # Inside
            Component(ref="C2", x=50.0, y=50.0),  # Outside
        ]

    def test_all_within_box(self, components_in_box):
        constraints = [
            GroupingConstraint(
                name="power_section",
                members=["C1", "C2"],
                constraints=[SpatialConstraint.within_box(x=10, y=10, width=20, height=20)],
            )
        ]
        violations = validate_grouping_constraints(components_in_box, constraints)
        assert len(violations) == 0

    def test_some_outside_box(self, components_outside_box):
        constraints = [
            GroupingConstraint(
                name="power_section",
                members=["C1", "C2"],
                constraints=[SpatialConstraint.within_box(x=10, y=10, width=20, height=20)],
            )
        ]
        violations = validate_grouping_constraints(components_outside_box, constraints)
        assert len(violations) == 1
        assert "C2" in violations[0].components


class TestValidateRelativePosition:
    """Tests for relative_position constraint validation."""

    @pytest.fixture
    def components(self):
        return [
            Component(ref="U1", x=50.0, y=50.0),
            Component(ref="C1", x=55.0, y=50.0),  # 5mm to the right
        ]

    def test_relative_position_satisfied(self, components):
        constraints = [
            GroupingConstraint(
                name="decoupling",
                members=["U1", "C1"],
                constraints=[
                    SpatialConstraint.relative_position(
                        reference="U1", dx=5.0, dy=0.0, tolerance_mm=0.5
                    )
                ],
            )
        ]
        violations = validate_grouping_constraints(components, constraints)
        assert len(violations) == 0

    def test_relative_position_violated(self, components):
        constraints = [
            GroupingConstraint(
                name="decoupling",
                members=["U1", "C1"],
                constraints=[
                    SpatialConstraint.relative_position(
                        reference="U1", dx=10.0, dy=0.0, tolerance_mm=0.5
                    )
                ],
            )
        ]
        violations = validate_grouping_constraints(components, constraints)
        assert len(violations) == 1
        assert "C1" in violations[0].components


class TestConstraintLoader:
    """Tests for YAML constraint loading and saving."""

    def test_load_basic_yaml(self):
        yaml_content = """
groups:
  - name: status_leds
    members: ["LED1", "LED2", "LED3"]
    constraints:
      - type: alignment
        axis: horizontal
        tolerance_mm: 0.5
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            constraints = load_constraints_from_yaml(f.name)

        assert len(constraints) == 1
        assert constraints[0].name == "status_leds"
        assert constraints[0].members == ["LED1", "LED2", "LED3"]
        assert len(constraints[0].constraints) == 1
        assert constraints[0].constraints[0].constraint_type == ConstraintType.ALIGNMENT

        Path(f.name).unlink()

    def test_load_multiple_constraints(self):
        yaml_content = """
groups:
  - name: status_leds
    members: ["LED1", "LED2", "LED3", "LED4"]
    constraints:
      - type: alignment
        axis: horizontal
        tolerance_mm: 0.5
      - type: ordering
        axis: horizontal
        order: ["LED1", "LED2", "LED3", "LED4"]

  - name: power_section
    members: ["U2", "C10", "C11", "L1", "D1"]
    constraints:
      - type: within_box
        x: 0
        y: 0
        width: 25
        height: 20
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            constraints = load_constraints_from_yaml(f.name)

        assert len(constraints) == 2
        assert constraints[0].name == "status_leds"
        assert len(constraints[0].constraints) == 2
        assert constraints[1].name == "power_section"

        Path(f.name).unlink()

    def test_save_and_reload(self):
        original = [
            GroupingConstraint(
                name="test_group",
                members=["LED1", "LED2"],
                constraints=[
                    SpatialConstraint.alignment(axis="horizontal", tolerance_mm=0.5),
                    SpatialConstraint.max_distance(anchor="LED1", radius_mm=10.0),
                ],
            )
        ]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            save_constraints_to_yaml(original, f.name)
            loaded = load_constraints_from_yaml(f.name)

        assert len(loaded) == 1
        assert loaded[0].name == "test_group"
        assert len(loaded[0].constraints) == 2

        Path(f.name).unlink()

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            load_constraints_from_yaml("/nonexistent/path/to/file.yaml")

    def test_invalid_constraint_type(self):
        yaml_content = """
groups:
  - name: test
    members: ["LED1"]
    constraints:
      - type: invalid_type
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            with pytest.raises(ValueError, match="Unknown constraint type"):
                load_constraints_from_yaml(f.name)

        Path(f.name).unlink()


class TestPlacementOptimizerConstraints:
    """Tests for constraint integration in PlacementOptimizer."""

    @pytest.fixture
    def optimizer_with_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        optimizer = PlacementOptimizer(board)

        # Add some components
        optimizer.add_component(Component(ref="LED1", x=10.0, y=50.0))
        optimizer.add_component(Component(ref="LED2", x=20.0, y=50.0))
        optimizer.add_component(Component(ref="LED3", x=30.0, y=50.0))
        optimizer.add_component(Component(ref="U1", x=50.0, y=50.0))

        return optimizer

    def test_add_grouping_constraint(self, optimizer_with_components):
        constraint = GroupingConstraint(
            name="test",
            members=["LED1", "LED2"],
            constraints=[SpatialConstraint.alignment(axis="horizontal")],
        )
        optimizer_with_components.add_grouping_constraint(constraint)
        assert len(optimizer_with_components.grouping_constraints) == 1

    def test_add_multiple_constraints(self, optimizer_with_components):
        constraints = [
            GroupingConstraint(name="group1", members=["LED1", "LED2"]),
            GroupingConstraint(name="group2", members=["LED3", "U1"]),
        ]
        optimizer_with_components.add_grouping_constraints(constraints)
        assert len(optimizer_with_components.grouping_constraints) == 2

    def test_validate_constraints(self, optimizer_with_components):
        constraint = GroupingConstraint(
            name="leds",
            members=["LED1", "LED2", "LED3"],
            constraints=[SpatialConstraint.alignment(axis="horizontal", tolerance_mm=0.5)],
        )
        optimizer_with_components.add_grouping_constraint(constraint)
        violations = optimizer_with_components.validate_constraints()
        # All LEDs are at y=50, so should be aligned
        assert len(violations) == 0

    def test_constraint_forces_computed(self, optimizer_with_components):
        # Add a constraint that should create forces
        constraint = GroupingConstraint(
            name="leds",
            members=["LED1", "LED2", "LED3"],
            constraints=[SpatialConstraint.max_distance(anchor="LED1", radius_mm=5.0)],
        )
        optimizer_with_components.add_grouping_constraint(constraint)

        # LED3 is 20mm from LED1, constraint says max 5mm
        # So forces should be non-zero
        forces = optimizer_with_components.compute_constraint_forces()
        assert forces["LED3"].magnitude() > 0

    def test_optimization_respects_constraints(self, optimizer_with_components):
        # Put LED3 far from LED1
        optimizer_with_components.get_component("LED3").x = 60.0

        constraint = GroupingConstraint(
            name="leds",
            members=["LED1", "LED2", "LED3"],
            constraints=[SpatialConstraint.max_distance(anchor="LED1", radius_mm=15.0)],
        )
        optimizer_with_components.add_grouping_constraint(constraint)

        # Run optimization
        optimizer_with_components.run(iterations=100, dt=0.01)

        # LED3 should have moved closer to LED1
        led1 = optimizer_with_components.get_component("LED1")
        led3 = optimizer_with_components.get_component("LED3")
        final_dist = ((led3.x - led1.x) ** 2 + (led3.y - led1.y) ** 2) ** 0.5

        # Should be closer than initial 50mm
        assert final_dist < 50.0
