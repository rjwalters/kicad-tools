"""Tests for the edge-connector ORIENTATION constraint (issue #4450).

``compute_edge_force`` only ever *translated* an edge-constrained component
toward its board edge.  A USB-C receptacle can therefore end up flush at the
perimeter with its plug opening pointing at the board interior — placement the
edge-connector validation check (``ConflictType.EDGE_CONNECTOR_PLACEMENT``)
accepts but no cable can physically mate with.  These tests pin the rotational
half of the constraint: an edge constraint carrying a ``mating_face_offset_deg``
hint produces a torque that turns the mating face along the assigned edge's
outward normal, while a constraint without the hint stays translation-only.
"""

import math

import pytest

from kicad_tools.optim import (
    BoardEdges,
    Component,
    EdgeConstraint,
    PlacementConfig,
    PlacementOptimizer,
    Polygon,
)
from kicad_tools.optim.edge_placement import (
    compute_edge_orientation_torque,
    mating_face_direction,
    mating_face_misalignment_deg,
    resolve_target_edge,
)

# Mating-face hint for a KiCad ``Connector_USB`` receptacle: the plug opening
# points +Y (KiCad screen-south) at rotation 0.  Verified against
# ``USB_C_Receptacle_GCT_USB4105-xx-A_16P_TopMnt_Horizontal``: SMT tails at
# y = -3.68, body/courtyard extending to y = +4.18.
USB_C_MATING_FACE_DEG = 90.0


@pytest.fixture
def board_edges():
    """100 x 80 mm board with its top-left corner at the origin."""
    return BoardEdges.from_bounds(0, 0, 100, 80)


class TestMatingFaceDirection:
    """The local mating-face hint composed with the component rotation."""

    def test_zero_offset_zero_rotation_points_east(self):
        d = mating_face_direction(0.0, 0.0)
        assert d.x == pytest.approx(1.0)
        assert d.y == pytest.approx(0.0, abs=1e-12)

    def test_usb_c_hint_points_south_at_rotation_zero(self):
        d = mating_face_direction(USB_C_MATING_FACE_DEG, 0.0)
        assert d.x == pytest.approx(0.0, abs=1e-12)
        assert d.y == pytest.approx(1.0)

    def test_rotation_advances_the_local_hint(self):
        # 180 degrees of component rotation flips the mouth to face north.
        d = mating_face_direction(USB_C_MATING_FACE_DEG, 180.0)
        assert d.y == pytest.approx(-1.0)

    @pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0, 37.0])
    def test_matches_the_kicad_footprint_transform(self, rotation):
        """The hint frame is KiCad's, not standard CCW math.

        KiCad applies a footprint's orientation as a NEGATED angle (#3739).
        Rotating the local ``+Y`` mouth vector with the exact transform
        ``kicad_tools.geometry.courtyard`` (and KiCad itself) uses must give
        the same heading this helper reports, or the rotation the optimizer
        writes back to the board would point the real connector the wrong
        way.
        """
        rad = math.radians(-rotation)
        local_x, local_y = 0.0, 1.0  # +Y mouth at rotation 0
        expected = (
            local_x * math.cos(rad) - local_y * math.sin(rad),
            local_x * math.sin(rad) + local_y * math.cos(rad),
        )

        d = mating_face_direction(USB_C_MATING_FACE_DEG, rotation)
        assert d.x == pytest.approx(expected[0], abs=1e-12)
        assert d.y == pytest.approx(expected[1], abs=1e-12)


class TestMatingFaceMisalignment:
    """Signed misalignment between the mating face and the edge normal."""

    def test_none_without_hint(self, board_edges):
        comp = Component(ref="J1", x=50, y=5, width=10, height=5)
        constraint = EdgeConstraint(reference="J1", edge="top")
        assert mating_face_misalignment_deg(comp, constraint, board_edges) is None

    def test_zero_when_facing_outward(self, board_edges):
        # Top edge normal is -Y (north); a +Y mouth rotated 180 faces north.
        comp = Component(ref="J1", x=50, y=5, rotation=180.0, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        delta = mating_face_misalignment_deg(comp, constraint, board_edges)
        assert delta == pytest.approx(0.0, abs=1e-9)

    def test_antipodal_when_facing_the_interior(self, board_edges):
        # This is the board-03 defect: mouth at +Y (interior) on the top edge.
        comp = Component(ref="J1", x=50, y=5, rotation=0.0, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        delta = mating_face_misalignment_deg(comp, constraint, board_edges)
        assert abs(delta) == pytest.approx(180.0)

    def test_quarter_turn_on_left_edge(self, board_edges):
        # Left edge normal is -X (west); a +Y (south) mouth is 90 degrees
        # short of it.  A positive misalignment means the rotation has to
        # DECREASE (KiCad rotations subtract from a local heading), i.e. this
        # connector wants rotation 270.
        comp = Component(ref="J1", x=5, y=40, rotation=0.0, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="left", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        delta = mating_face_misalignment_deg(comp, constraint, board_edges)
        assert delta == pytest.approx(90.0)

    def test_any_edge_resolves_to_nearest(self, board_edges):
        comp = Component(ref="J1", x=95, y=40, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="any", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        assert resolve_target_edge(comp, constraint, board_edges) is board_edges.right
        # Right edge normal is +X; a +Y mouth is 90 degrees clockwise of it.
        delta = mating_face_misalignment_deg(comp, constraint, board_edges)
        assert delta == pytest.approx(-90.0)


class TestComputeEdgeOrientationTorque:
    """The torsion spring itself."""

    def test_no_hint_is_backward_compatible(self, board_edges):
        comp = Component(ref="J1", x=50, y=40, rotation=37.0, width=10, height=5)
        constraint = EdgeConstraint(reference="J1", edge="top")
        torque, aligned = compute_edge_orientation_torque(comp, constraint, board_edges)
        assert torque == 0.0
        assert aligned is True

    def test_zero_torque_when_already_aligned(self, board_edges):
        comp = Component(ref="J1", x=50, y=5, rotation=180.0, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        torque, aligned = compute_edge_orientation_torque(comp, constraint, board_edges)
        assert torque == pytest.approx(0.0, abs=1e-9)
        assert aligned is True

    def test_torque_reduces_misalignment(self, board_edges):
        """A small misalignment is driven toward zero, not away from it."""
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        for start in (150.0, 165.0, 195.0, 210.0):
            comp = Component(ref="J1", x=50, y=5, rotation=start, width=10, height=5)
            before = abs(mating_face_misalignment_deg(comp, constraint, board_edges))
            torque, aligned = compute_edge_orientation_torque(
                comp, constraint, board_edges, stiffness=20.0
            )
            assert not aligned
            # Nudge the component the way the torque points and re-measure.
            comp.rotation = start + math.copysign(1.0, torque)
            after = abs(mating_face_misalignment_deg(comp, constraint, board_edges))
            assert after < before, f"torque increased misalignment from rotation={start}"

    def test_torque_scales_with_stiffness(self, board_edges):
        comp = Component(ref="J1", x=50, y=5, rotation=150.0, width=10, height=5)
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        weak, _ = compute_edge_orientation_torque(comp, constraint, board_edges, stiffness=10.0)
        strong, _ = compute_edge_orientation_torque(comp, constraint, board_edges, stiffness=40.0)
        assert strong == pytest.approx(4.0 * weak)

    def test_back_facing_connector_gets_full_torque(self, board_edges):
        """The 180-degree case must NOT sit on a zero-torque saddle.

        A pure ``sin`` torsion spring vanishes at exactly 180 degrees of
        misalignment — which is the board-03 defect itself (mouth pointing at
        the board interior).  The saturated magnitude keeps full restoring
        torque through the back-facing half turn.
        """
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        comp = Component(ref="J1", x=50, y=5, rotation=0.0, width=10, height=5)
        torque, aligned = compute_edge_orientation_torque(
            comp, constraint, board_edges, stiffness=20.0
        )
        assert not aligned
        assert abs(torque) == pytest.approx(20.0)

    def test_magnitude_is_continuous_at_ninety_degrees(self, board_edges):
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        just_under = Component(ref="J1", x=50, y=5, rotation=180.0 - 89.9, width=10, height=5)
        just_over = Component(ref="J1", x=50, y=5, rotation=180.0 - 90.1, width=10, height=5)
        t1, _ = compute_edge_orientation_torque(just_under, constraint, board_edges, stiffness=20.0)
        t2, _ = compute_edge_orientation_torque(just_over, constraint, board_edges, stiffness=20.0)
        assert abs(abs(t1) - abs(t2)) < 0.01


class TestOptimizerEdgeOrientation:
    """The torque as wired into ``PlacementOptimizer``."""

    @pytest.fixture
    def optimizer(self):
        board = Polygon.rectangle(50, 40, 100, 80)
        return PlacementOptimizer(board)

    def test_config_exposes_orientation_stiffness(self):
        assert PlacementConfig().edge_orientation_stiffness > 0

    def test_no_orientation_torque_without_hint(self, optimizer):
        """Existing translation-only edge constraints are unchanged."""
        comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        _, torques = optimizer.compute_forces_and_torques()
        assert torques["J1"] == pytest.approx(0.0, abs=1e-9)

    def test_orientation_torque_present_with_hint(self, optimizer):
        comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(
            EdgeConstraint(reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG)
        )

        _, torques = optimizer.compute_forces_and_torques()
        assert abs(torques["J1"]) > 0.0

    def test_fixed_component_gets_no_orientation_torque(self, optimizer):
        comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5, fixed=True)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(
            EdgeConstraint(reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG)
        )

        _, torques = optimizer.compute_forces_and_torques()
        assert torques["J1"] == pytest.approx(0.0, abs=1e-9)

    def test_converges_to_edge_adjacent_and_outward_facing(self, optimizer):
        """The issue's headline regression test.

        A connector flagged as an edge connector, starting mid-board with its
        mating face pointing at the board interior, must end up on the
        perimeter *and* facing off-board after the standard
        run -> snap_to_grid pipeline.
        """
        comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5)
        optimizer.add_component(comp)
        constraint = EdgeConstraint(
            reference="J1", edge="top", mating_face_offset_deg=USB_C_MATING_FACE_DEG
        )
        optimizer.add_edge_constraint(constraint)

        start_y = comp.y
        optimizer.run(iterations=1000, dt=0.05)

        # Translation: pulled to the top edge (y = 0) until the board's own
        # boundary repulsion balances the edge spring.
        assert comp.y < 0.6 * start_y, f"connector did not migrate to the edge (y={comp.y})"
        assert comp.y < min(comp.x, 100 - comp.x, 80 - comp.y), "top edge is not the nearest edge"

        # Orientation: resolved to the cardinal slot facing off-board.
        optimizer.snap_rotations(90.0)
        assert comp.rotation == pytest.approx(180.0)

        delta = mating_face_misalignment_deg(comp, constraint, optimizer.board_edges)
        assert abs(delta) < 1e-6, f"mating face still {delta:.1f} deg off outward normal"

        face = mating_face_direction(USB_C_MATING_FACE_DEG, comp.rotation)
        assert face.y < 0, "mating face still points at the board interior"

    def test_snap_picks_outward_slot_on_every_edge(self, optimizer):
        """The outward slot is edge-specific, not a hardcoded 180."""
        # KiCad rotations subtract from a local heading (#3739), so a +Y mouth
        # points east at rotation 90 and west at rotation 270.
        expected = {"top": 180.0, "bottom": 0.0, "left": 270.0, "right": 90.0}
        for edge, want in expected.items():
            opt = PlacementOptimizer(Polygon.rectangle(50, 40, 100, 80))
            comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5)
            opt.add_component(comp)
            opt.add_edge_constraint(
                EdgeConstraint(
                    reference="J1", edge=edge, mating_face_offset_deg=USB_C_MATING_FACE_DEG
                )
            )
            opt.snap_rotations(90.0)
            assert comp.rotation == pytest.approx(want), f"{edge} edge snapped to {comp.rotation}"

    def test_snap_unchanged_without_hint(self, optimizer):
        """No regression: hintless components still snap to the NEAREST slot."""
        comp = Component(ref="J1", x=50, y=5, rotation=8.0, width=10, height=5)
        other = Component(ref="R1", x=20, y=20, rotation=97.0, width=2, height=1)
        optimizer.add_component(comp)
        optimizer.add_component(other)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        optimizer.snap_rotations(90.0)

        assert comp.rotation == pytest.approx(0.0)
        assert other.rotation == pytest.approx(90.0)

    def test_hintless_constraint_still_only_translates(self, optimizer):
        """No regression: without a hint the component's rotation is untouched
        by the edge system (only the pre-existing 90-degree wells act)."""
        comp = Component(ref="J1", x=50, y=40, rotation=0.0, width=10, height=5)
        optimizer.add_component(comp)
        optimizer.add_edge_constraint(EdgeConstraint(reference="J1", edge="top"))

        start_y = comp.y
        optimizer.run(iterations=1000, dt=0.05)

        assert comp.y < 0.6 * start_y
        assert comp.rotation == pytest.approx(0.0, abs=1e-6)


class TestDetectEdgeComponentsMatingFaces:
    """``detect_edge_components`` forwards caller-supplied mating-face hints."""

    def test_hints_attached_to_detected_connectors(self):
        from kicad_tools.optim.edge_placement import detect_edge_components

        class _FP:
            def __init__(self, reference, footprint_name):
                self.reference = reference
                self.footprint_name = footprint_name

        class _PCB:
            footprints = [
                _FP("J1", "Connector_USB:USB_C_Receptacle_GCT_USB4105"),
                _FP("J3", "Connector_PinHeader:PinHeader_1x04"),
                _FP("R1", "Resistor_SMD:R_0402_1005Metric"),
            ]

        constraints = detect_edge_components(_PCB(), mating_faces={"J1": USB_C_MATING_FACE_DEG})
        by_ref = {c.reference: c for c in constraints}

        assert by_ref["J1"].mating_face_offset_deg == USB_C_MATING_FACE_DEG
        assert by_ref["J3"].mating_face_offset_deg is None
        assert "R1" not in by_ref

    def test_default_is_no_hints(self):
        from kicad_tools.optim.edge_placement import detect_edge_components

        class _FP:
            reference = "J1"
            footprint_name = "Connector_USB:USB_C_Receptacle_GCT_USB4105"

        class _PCB:
            footprints = [_FP()]

        (constraint,) = detect_edge_components(_PCB())
        assert constraint.mating_face_offset_deg is None
