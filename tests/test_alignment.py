"""Tests for kicad_tools.optim.alignment module."""

import pytest

from kicad_tools.optim import (
    AlignmentConstraint,
    AlignmentType,
    Component,
    PlacementOptimizer,
    Polygon,
    align_components,
    align_to_reference,
    apply_alignment_constraints,
    distribute_components,
    snap_to_grid,
)


@pytest.fixture
def simple_optimizer() -> PlacementOptimizer:
    """Create a simple optimizer with test components."""
    board = Polygon.rectangle(50, 50, 100, 100)
    optimizer = PlacementOptimizer(board)

    # Add components in a grid-like pattern
    components = [
        Component(ref="R1", x=10.3, y=20.7, width=2, height=1),
        Component(ref="R2", x=15.8, y=20.2, width=2, height=1),
        Component(ref="R3", x=20.1, y=21.3, width=2, height=1),
        Component(ref="R4", x=25.6, y=19.9, width=2, height=1),
        Component(ref="C1", x=10.0, y=30.0, width=2, height=2),
        Component(ref="C2", x=20.0, y=35.0, width=2, height=2),
        Component(ref="C3", x=30.0, y=40.0, width=2, height=2),
        Component(ref="LED1", x=50.0, y=10.0, width=3, height=3),
        Component(ref="LED2", x=55.0, y=10.0, width=3, height=3),
        Component(ref="LED3", x=60.0, y=10.0, width=3, height=3),
        Component(ref="LED4", x=65.0, y=10.0, width=3, height=3),
        Component(ref="U1", x=40.0, y=50.0, width=10, height=10, fixed=True),
    ]

    for comp in components:
        optimizer.add_component(comp)

    return optimizer


class TestAlignmentType:
    """Tests for AlignmentType enum."""

    def test_all_types_defined(self):
        """Test that all alignment types are defined."""
        assert AlignmentType.GRID.value == "grid"
        assert AlignmentType.ROW.value == "row"
        assert AlignmentType.COLUMN.value == "column"
        assert AlignmentType.DISTRIBUTE.value == "distribute"
        assert AlignmentType.REFERENCE.value == "reference"


class TestAlignmentConstraint:
    """Tests for AlignmentConstraint dataclass."""

    def test_basic_constraint(self):
        """Test creating a basic constraint."""
        constraint = AlignmentConstraint(
            alignment_type=AlignmentType.GRID,
            components=["*"],
            parameters={"grid_mm": 0.5, "rotation_snap": 90},
        )

        assert constraint.alignment_type == AlignmentType.GRID
        assert constraint.components == ["*"]
        assert constraint.parameters["grid_mm"] == 0.5

    def test_matches_ref_exact(self):
        """Test exact reference matching."""
        constraint = AlignmentConstraint(
            alignment_type=AlignmentType.ROW,
            components=["R1", "R2", "R3"],
        )

        assert constraint.matches_ref("R1") is True
        assert constraint.matches_ref("R2") is True
        assert constraint.matches_ref("R4") is False
        assert constraint.matches_ref("C1") is False

    def test_matches_ref_pattern(self):
        """Test pattern matching."""
        constraint = AlignmentConstraint(
            alignment_type=AlignmentType.ROW,
            components=["R*", "LED*"],
        )

        assert constraint.matches_ref("R1") is True
        assert constraint.matches_ref("R123") is True
        assert constraint.matches_ref("LED1") is True
        assert constraint.matches_ref("C1") is False
        assert constraint.matches_ref("U1") is False


class TestSnapToGrid:
    """Tests for snap_to_grid function."""

    def test_snap_positions(self, simple_optimizer):
        """Test that positions snap to grid."""
        # R1 is at (10.3, 20.7) - should snap to (10.5, 20.5) on 0.5mm grid
        count = snap_to_grid(simple_optimizer, grid_mm=0.5, rotation_snap=None)

        # All non-fixed components should be snapped
        assert count >= 1

        # Check specific component
        r1 = simple_optimizer.get_component("R1")
        assert r1.x == pytest.approx(10.5, abs=0.01)
        assert r1.y == pytest.approx(20.5, abs=0.01)

    def test_snap_rotations(self, simple_optimizer):
        """Test that rotations snap to grid."""
        # Set a non-aligned rotation
        r1 = simple_optimizer.get_component("R1")
        r1.rotation = 47.5

        snap_to_grid(simple_optimizer, grid_mm=1.0, rotation_snap=90)

        # 47.5 rounds to 90 (closest 90 degree slot)
        assert r1.rotation == pytest.approx(90, abs=0.01)

    def test_fixed_components_not_moved(self, simple_optimizer):
        """Test that fixed components are not snapped."""
        u1 = simple_optimizer.get_component("U1")
        original_x, original_y = u1.x, u1.y

        snap_to_grid(simple_optimizer, grid_mm=0.5)

        assert u1.x == original_x
        assert u1.y == original_y

    def test_zero_grid_returns_zero(self, simple_optimizer):
        """Test that zero grid size does nothing."""
        count = snap_to_grid(simple_optimizer, grid_mm=0)
        assert count == 0

    def test_snap_1mm_grid(self, simple_optimizer):
        """Test snapping to 1mm grid."""
        snap_to_grid(simple_optimizer, grid_mm=1.0)

        r1 = simple_optimizer.get_component("R1")
        # 10.3 should snap to 10.0
        assert r1.x == pytest.approx(10.0, abs=0.01)


class TestAlignComponents:
    """Tests for align_components function."""

    def test_horizontal_alignment_center(self, simple_optimizer):
        """Test horizontal alignment to center."""
        refs = ["R1", "R2", "R3", "R4"]
        count = align_components(simple_optimizer, refs, axis="horizontal", reference="center")

        assert count == 4

        # All components should have the same Y (average of original Y values)
        r1 = simple_optimizer.get_component("R1")
        r2 = simple_optimizer.get_component("R2")
        r3 = simple_optimizer.get_component("R3")
        r4 = simple_optimizer.get_component("R4")

        # They should all be at the same Y
        assert r1.y == pytest.approx(r2.y, abs=0.01)
        assert r2.y == pytest.approx(r3.y, abs=0.01)
        assert r3.y == pytest.approx(r4.y, abs=0.01)

    def test_horizontal_alignment_top(self, simple_optimizer):
        """Test horizontal alignment to top edge."""
        refs = ["R1", "R2", "R3", "R4"]
        count = align_components(simple_optimizer, refs, axis="horizontal", reference="top")

        assert count == 4

        # All components should have the same top edge
        r1 = simple_optimizer.get_component("R1")
        r2 = simple_optimizer.get_component("R2")

        top1 = r1.y - r1.height / 2
        top2 = r2.y - r2.height / 2

        assert top1 == pytest.approx(top2, abs=0.01)

    def test_vertical_alignment_center(self, simple_optimizer):
        """Test vertical alignment to center."""
        refs = ["C1", "C2", "C3"]
        count = align_components(simple_optimizer, refs, axis="vertical", reference="center")

        assert count == 3

        c1 = simple_optimizer.get_component("C1")
        c2 = simple_optimizer.get_component("C2")
        c3 = simple_optimizer.get_component("C3")

        # They should all be at the same X
        assert c1.x == pytest.approx(c2.x, abs=0.01)
        assert c2.x == pytest.approx(c3.x, abs=0.01)

    def test_pattern_matching(self, simple_optimizer):
        """Test alignment with pattern matching."""
        count = align_components(simple_optimizer, ["R*"], axis="horizontal")

        assert count == 4  # R1, R2, R3, R4

    def test_single_component_returns_zero(self, simple_optimizer):
        """Test that single component alignment returns 0."""
        count = align_components(simple_optimizer, ["R1"], axis="horizontal")
        assert count == 0

    def test_fixed_components_not_aligned(self, simple_optimizer):
        """Test that fixed components are not aligned."""
        # U1 is fixed, C1 and C2 are movable
        u1 = simple_optimizer.get_component("U1")
        original_x, original_y = u1.x, u1.y

        count = align_components(simple_optimizer, ["U1", "C1", "C2"], axis="horizontal")

        # C1 and C2 should be aligned, U1 should not move
        assert count == 2
        assert u1.x == original_x
        assert u1.y == original_y


class TestDistributeComponents:
    """Tests for distribute_components function."""

    def test_distribute_evenly_horizontal(self, simple_optimizer):
        """Test even horizontal distribution."""
        refs = ["LED1", "LED2", "LED3", "LED4"]
        count = distribute_components(simple_optimizer, refs, axis="horizontal")

        assert count == 4

        led1 = simple_optimizer.get_component("LED1")
        led2 = simple_optimizer.get_component("LED2")
        led3 = simple_optimizer.get_component("LED3")
        led4 = simple_optimizer.get_component("LED4")

        # Components should be evenly spaced
        spacing_1_2 = led2.x - led1.x
        spacing_2_3 = led3.x - led2.x
        spacing_3_4 = led4.x - led3.x

        assert spacing_1_2 == pytest.approx(spacing_2_3, abs=0.01)
        assert spacing_2_3 == pytest.approx(spacing_3_4, abs=0.01)

    def test_distribute_with_fixed_spacing(self, simple_optimizer):
        """Test distribution with fixed spacing."""
        refs = ["LED1", "LED2", "LED3", "LED4"]
        count = distribute_components(simple_optimizer, refs, axis="horizontal", spacing_mm=10.0)

        assert count == 4

        led1 = simple_optimizer.get_component("LED1")
        led2 = simple_optimizer.get_component("LED2")
        led3 = simple_optimizer.get_component("LED3")
        led4 = simple_optimizer.get_component("LED4")

        # Components should be exactly 10mm apart
        assert led2.x - led1.x == pytest.approx(10.0, abs=0.01)
        assert led3.x - led2.x == pytest.approx(10.0, abs=0.01)
        assert led4.x - led3.x == pytest.approx(10.0, abs=0.01)

    def test_distribute_vertical(self, simple_optimizer):
        """Test vertical distribution."""
        refs = ["C1", "C2", "C3"]
        count = distribute_components(simple_optimizer, refs, axis="vertical")

        assert count == 3

        c1 = simple_optimizer.get_component("C1")
        c2 = simple_optimizer.get_component("C2")
        c3 = simple_optimizer.get_component("C3")

        # Components should be evenly spaced vertically
        spacing_1_2 = c2.y - c1.y
        spacing_2_3 = c3.y - c2.y

        assert spacing_1_2 == pytest.approx(spacing_2_3, abs=0.01)

    def test_single_component_returns_zero(self, simple_optimizer):
        """Test that single component distribution returns 0."""
        count = distribute_components(simple_optimizer, ["LED1"], axis="horizontal")
        assert count == 0


class TestAlignToReference:
    """Tests for align_to_reference function."""

    def test_align_to_left_edge(self, simple_optimizer):
        """Test aligning components to reference left edge."""
        count = align_to_reference(
            simple_optimizer,
            refs=["C1", "C2"],
            reference_ref="U1",
            edge="left",
        )

        assert count == 2

        u1 = simple_optimizer.get_component("U1")
        c1 = simple_optimizer.get_component("C1")
        c2 = simple_optimizer.get_component("C2")

        # Left edges should be aligned
        u1_left = u1.x - u1.width / 2
        c1_left = c1.x - c1.width / 2
        c2_left = c2.x - c2.width / 2

        assert c1_left == pytest.approx(u1_left, abs=0.01)
        assert c2_left == pytest.approx(u1_left, abs=0.01)

    def test_align_to_center_x(self, simple_optimizer):
        """Test aligning components to reference center X."""
        count = align_to_reference(
            simple_optimizer,
            refs=["C1", "C2"],
            reference_ref="U1",
            edge="center_x",
        )

        assert count == 2

        u1 = simple_optimizer.get_component("U1")
        c1 = simple_optimizer.get_component("C1")

        assert c1.x == pytest.approx(u1.x, abs=0.01)

    def test_reference_not_in_list(self, simple_optimizer):
        """Test that reference component is not moved."""
        u1 = simple_optimizer.get_component("U1")
        original_x, original_y = u1.x, u1.y

        align_to_reference(
            simple_optimizer,
            refs=["U1", "C1", "C2"],  # U1 is reference
            reference_ref="U1",
            edge="left",
        )

        # U1 should not have moved (it's the reference)
        assert u1.x == original_x
        assert u1.y == original_y

    def test_invalid_reference_returns_zero(self, simple_optimizer):
        """Test that invalid reference returns 0."""
        count = align_to_reference(
            simple_optimizer,
            refs=["C1", "C2"],
            reference_ref="NONEXISTENT",
            edge="left",
        )

        assert count == 0


class TestApplyAlignmentConstraints:
    """Tests for apply_alignment_constraints function."""

    def test_multiple_constraints(self, simple_optimizer):
        """Test applying multiple constraints."""
        constraints = [
            AlignmentConstraint(
                alignment_type=AlignmentType.GRID,
                components=["*"],
                parameters={"grid_mm": 0.5, "rotation_snap": 90},
            ),
            AlignmentConstraint(
                alignment_type=AlignmentType.ROW,
                components=["R1", "R2", "R3", "R4"],
                parameters={"tolerance_mm": 0.1, "reference": "center"},
            ),
        ]

        results = apply_alignment_constraints(simple_optimizer, constraints)

        assert "grid" in results
        assert "row" in results
        assert results["grid"] > 0
        assert results["row"] > 0

    def test_distribute_constraint(self, simple_optimizer):
        """Test applying distribute constraint."""
        constraints = [
            AlignmentConstraint(
                alignment_type=AlignmentType.DISTRIBUTE,
                components=["LED1", "LED2", "LED3", "LED4"],
                parameters={"axis": "horizontal", "spacing_mm": 10.0},
            ),
        ]

        results = apply_alignment_constraints(simple_optimizer, constraints)

        assert "distribute" in results
        assert results["distribute"] == 4

    def test_reference_constraint(self, simple_optimizer):
        """Test applying reference constraint."""
        constraints = [
            AlignmentConstraint(
                alignment_type=AlignmentType.REFERENCE,
                components=["C1", "C2"],
                parameters={"reference": "U1", "edge": "left"},
            ),
        ]

        results = apply_alignment_constraints(simple_optimizer, constraints)

        assert "reference" in results
        assert results["reference"] == 2

    def test_column_constraint(self, simple_optimizer):
        """Test applying column constraint."""
        constraints = [
            AlignmentConstraint(
                alignment_type=AlignmentType.COLUMN,
                components=["C1", "C2", "C3"],
                parameters={"tolerance_mm": 0.1, "reference": "center"},
            ),
        ]

        results = apply_alignment_constraints(simple_optimizer, constraints)

        assert "column" in results
        assert results["column"] == 3


class TestCLICommands:
    """Tests for CLI command integration."""

    def test_snap_command_help(self):
        """Test snap command help."""
        from kicad_tools.cli.placement_cmd import main

        # argparse exits with SystemExit(0) on --help
        with pytest.raises(SystemExit) as exc_info:
            main(["snap", "--help"])
        assert exc_info.value.code == 0

    def test_align_command_help(self):
        """Test align command help."""
        from kicad_tools.cli.placement_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["align", "--help"])
        assert exc_info.value.code == 0

    def test_distribute_command_help(self):
        """Test distribute command help."""
        from kicad_tools.cli.placement_cmd import main

        with pytest.raises(SystemExit) as exc_info:
            main(["distribute", "--help"])
        assert exc_info.value.code == 0
