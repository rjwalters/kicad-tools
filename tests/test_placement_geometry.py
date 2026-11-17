"""Tests for placement geometry constraint detectors.

Covers:
- compute_overlap: pairwise AABB overlap with rotation and side awareness
- compute_boundary_violation: out-of-bounds area computation
"""

from __future__ import annotations

import pytest

from kicad_tools.placement.cost import BoardOutline
from kicad_tools.placement.geometry import (
    _aabb,
    _overlap_area,
    compute_boundary_violation,
    compute_overlap,
)
from kicad_tools.placement.vector import ComponentDef, PlacedComponent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comp(
    ref: str = "U1",
    x: float = 0.0,
    y: float = 0.0,
    rotation: float = 0.0,
    side: int = 0,
) -> PlacedComponent:
    """Create a PlacedComponent with minimal boilerplate."""
    return PlacedComponent(reference=ref, x=x, y=y, rotation=rotation, side=side)


def _cdef(
    ref: str = "U1",
    width: float = 2.0,
    height: float = 2.0,
) -> ComponentDef:
    """Create a ComponentDef with minimal boilerplate."""
    return ComponentDef(reference=ref, width=width, height=height)


# ---------------------------------------------------------------------------
# _aabb tests
# ---------------------------------------------------------------------------


class TestAABB:
    """Tests for the internal _aabb helper."""

    def test_no_rotation(self) -> None:
        comp = _comp(x=5.0, y=5.0, rotation=0.0)
        cdef = _cdef(width=4.0, height=2.0)
        box = _aabb(comp, cdef)
        assert box == pytest.approx((3.0, 4.0, 7.0, 6.0))

    def test_rotation_90(self) -> None:
        """90 deg rotation swaps width and height."""
        comp = _comp(x=5.0, y=5.0, rotation=90.0)
        cdef = _cdef(width=4.0, height=2.0)
        box = _aabb(comp, cdef)
        # After swap: half_w=1.0 (was height/2), half_h=2.0 (was width/2)
        assert box == pytest.approx((4.0, 3.0, 6.0, 7.0))

    def test_rotation_180(self) -> None:
        """180 deg rotation does not swap dimensions."""
        comp = _comp(x=5.0, y=5.0, rotation=180.0)
        cdef = _cdef(width=4.0, height=2.0)
        box = _aabb(comp, cdef)
        assert box == pytest.approx((3.0, 4.0, 7.0, 6.0))

    def test_rotation_270(self) -> None:
        """270 deg rotation swaps width and height."""
        comp = _comp(x=5.0, y=5.0, rotation=270.0)
        cdef = _cdef(width=4.0, height=2.0)
        box = _aabb(comp, cdef)
        assert box == pytest.approx((4.0, 3.0, 6.0, 7.0))

    def test_square_component_unaffected_by_rotation(self) -> None:
        """Square components have the same AABB regardless of rotation."""
        cdef = _cdef(width=4.0, height=4.0)
        for rot in (0.0, 90.0, 180.0, 270.0):
            comp = _comp(x=0.0, y=0.0, rotation=rot)
            box = _aabb(comp, cdef)
            assert box == pytest.approx((-2.0, -2.0, 2.0, 2.0)), f"Failed at rotation={rot}"


# ---------------------------------------------------------------------------
# _overlap_area tests
# ---------------------------------------------------------------------------


class TestOverlapArea:
    """Tests for the internal _overlap_area helper."""

    def test_no_overlap(self) -> None:
        assert _overlap_area((0, 0, 1, 1), (2, 2, 3, 3)) == pytest.approx(0.0)

    def test_touching_edge(self) -> None:
        """Boxes sharing an edge have zero overlap area."""
        assert _overlap_area((0, 0, 1, 1), (1, 0, 2, 1)) == pytest.approx(0.0)

    def test_partial_overlap(self) -> None:
        assert _overlap_area((0, 0, 2, 2), (1, 1, 3, 3)) == pytest.approx(1.0)

    def test_full_containment(self) -> None:
        """Smaller box fully inside larger box."""
        assert _overlap_area((0, 0, 4, 4), (1, 1, 2, 2)) == pytest.approx(1.0)

    def test_identical_boxes(self) -> None:
        assert _overlap_area((0, 0, 2, 2), (0, 0, 2, 2)) == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# compute_overlap tests
# ---------------------------------------------------------------------------


class TestComputeOverlap:
    """Tests for the public compute_overlap function."""

    def test_no_components(self) -> None:
        assert compute_overlap([], []) == pytest.approx(0.0)

    def test_single_component(self) -> None:
        """A single component cannot overlap with anything."""
        placements = [_comp("U1", x=0, y=0)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_overlap(placements, defs) == pytest.approx(0.0)

    def test_non_overlapping_pair(self) -> None:
        placements = [
            _comp("U1", x=0, y=0),
            _comp("U2", x=10, y=0),
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        assert compute_overlap(placements, defs) == pytest.approx(0.0)

    def test_touching_pair(self) -> None:
        """Components exactly touching (edge contact) have zero overlap."""
        placements = [
            _comp("U1", x=0, y=0),
            _comp("U2", x=2, y=0),  # right edge of U1 = left edge of U2
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        assert compute_overlap(placements, defs) == pytest.approx(0.0)

    def test_overlapping_pair(self) -> None:
        """Two 2x2 components centered 1mm apart overlap by 1mm^2."""
        placements = [
            _comp("U1", x=0, y=0),
            _comp("U2", x=1, y=0),
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        # U1: [-1, -1, 1, 1], U2: [0, -1, 2, 1]
        # x overlap: min(1,2) - max(-1,0) = 1 - 0 = 1
        # y overlap: min(1,1) - max(-1,-1) = 1 - (-1) = 2
        # area = 1 * 2 = 2
        assert compute_overlap(placements, defs) == pytest.approx(2.0)

    def test_identical_positions(self) -> None:
        """Two identical components at the same position fully overlap."""
        placements = [
            _comp("U1", x=5, y=5),
            _comp("U2", x=5, y=5),
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        assert compute_overlap(placements, defs) == pytest.approx(4.0)

    def test_front_back_no_overlap(self) -> None:
        """Components on different board sides do not overlap."""
        placements = [
            _comp("U1", x=5, y=5, side=0),  # front
            _comp("U2", x=5, y=5, side=1),  # back
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        assert compute_overlap(placements, defs) == pytest.approx(0.0)

    def test_same_side_overlap(self) -> None:
        """Components on the same side DO overlap."""
        placements = [
            _comp("U1", x=5, y=5, side=1),
            _comp("U2", x=5, y=5, side=1),
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        assert compute_overlap(placements, defs) == pytest.approx(4.0)

    def test_rotation_affects_overlap(self) -> None:
        """Rotation changes the AABB and thus the overlap."""
        # Two rectangular 4x2 components side by side at y=0
        # Without rotation, placed 3mm apart in X:
        #   U1 at x=0: AABB [-2, -1, 2, 1]
        #   U2 at x=3: AABB [1, -1, 5, 1]
        #   x overlap = min(2,5) - max(-2,1) = 2-1 = 1, y overlap = 2
        #   overlap area = 2

        placements_no_rot = [
            _comp("U1", x=0, y=0, rotation=0),
            _comp("U2", x=3, y=0, rotation=0),
        ]
        defs = [
            _cdef("U1", width=4, height=2),
            _cdef("U2", width=4, height=2),
        ]
        overlap_no_rot = compute_overlap(placements_no_rot, defs)
        assert overlap_no_rot == pytest.approx(2.0)

        # Now rotate U2 by 90 degrees: its AABB becomes 2x4 instead of 4x2
        #   U2 at x=3, rot=90: AABB [2, -2, 4, 2]
        #   x overlap = min(2,4) - max(-2,2) = 2-2 = 0
        #   No overlap!
        placements_rot = [
            _comp("U1", x=0, y=0, rotation=0),
            _comp("U2", x=3, y=0, rotation=90),
        ]
        overlap_rot = compute_overlap(placements_rot, defs)
        assert overlap_rot == pytest.approx(0.0)

    def test_three_components_pairwise(self) -> None:
        """Overlap is summed across all overlapping pairs."""
        # Three 2x2 components in a line, each 1mm apart
        placements = [
            _comp("U1", x=0, y=0),
            _comp("U2", x=1, y=0),
            _comp("U3", x=2, y=0),
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
            _cdef("U3", width=2, height=2),
        ]
        # U1 [-1,-1,1,1], U2 [0,-1,2,1], U3 [1,-1,3,1]
        # U1-U2: x=1*y=2 = 2
        # U2-U3: x=1*y=2 = 2
        # U1-U3: x=min(1,3)-max(-1,1)=0 -> 0
        assert compute_overlap(placements, defs) == pytest.approx(4.0)

    def test_mismatched_lengths_raises(self) -> None:
        with pytest.raises(ValueError, match="placements has 2.*component_defs has 1"):
            compute_overlap(
                [_comp("U1"), _comp("U2")],
                [_cdef("U1")],
            )


# ---------------------------------------------------------------------------
# compute_boundary_violation tests
# ---------------------------------------------------------------------------


class TestComputeBoundaryViolation:
    """Tests for the public compute_boundary_violation function."""

    @pytest.fixture()
    def board(self) -> BoardOutline:
        """A 10x10 board from (0,0) to (10,10)."""
        return BoardOutline(min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)

    def test_no_components(self, board: BoardOutline) -> None:
        assert compute_boundary_violation([], [], board) == pytest.approx(0.0)

    def test_fully_inside(self, board: BoardOutline) -> None:
        """Component fully within the board has zero violation."""
        placements = [_comp("U1", x=5, y=5)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(0.0)

    def test_touching_edge(self, board: BoardOutline) -> None:
        """Component exactly touching the board edge has zero violation."""
        # 2x2 component centered at (1, 1): AABB [0, 0, 2, 2]
        placements = [_comp("U1", x=1, y=1)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(0.0)

    def test_partially_outside_left(self, board: BoardOutline) -> None:
        """Component extending past the left edge."""
        # 2x2 component at x=0, y=5: AABB [-1, 4, 1, 6]
        # Inside: [0,4,1,6] = 1*2 = 2
        # Total area = 2*2 = 4
        # Violation = 4 - 2 = 2
        placements = [_comp("U1", x=0, y=5)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(2.0)

    def test_partially_outside_right(self, board: BoardOutline) -> None:
        """Component extending past the right edge."""
        # 2x2 component at x=10, y=5: AABB [9, 4, 11, 6]
        placements = [_comp("U1", x=10, y=5)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(2.0)

    def test_partially_outside_top(self, board: BoardOutline) -> None:
        """Component extending past the top edge."""
        placements = [_comp("U1", x=5, y=0)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(2.0)

    def test_partially_outside_bottom(self, board: BoardOutline) -> None:
        """Component extending past the bottom edge."""
        placements = [_comp("U1", x=5, y=10)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(2.0)

    def test_corner_violation(self, board: BoardOutline) -> None:
        """Component at a corner extends past two edges."""
        # 2x2 at (0,0): AABB [-1, -1, 1, 1]
        # Inside: [0,0,1,1] = 1*1 = 1
        # Total = 4
        # Violation = 4 - 1 = 3
        placements = [_comp("U1", x=0, y=0)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(3.0)

    def test_fully_outside(self, board: BoardOutline) -> None:
        """Component entirely outside the board."""
        # 2x2 at (-5, -5): AABB [-6, -6, -4, -4], entirely outside [0,0,10,10]
        placements = [_comp("U1", x=-5, y=-5)]
        defs = [_cdef("U1", width=2, height=2)]
        # Inside area = 0, Total = 4
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(4.0)

    def test_rotation_affects_boundary(self, board: BoardOutline) -> None:
        """Rotation changes bounding box and thus boundary violation."""
        # 6x2 component at board right edge (x=9, y=5)
        # No rotation: AABB [6, 4, 12, 6], inside [6,4,10,6] = 4*2=8, total=6*2=12
        #   violation = 12 - 8 = 4
        placements_no_rot = [_comp("U1", x=9, y=5, rotation=0)]
        defs = [_cdef("U1", width=6, height=2)]
        v_no_rot = compute_boundary_violation(placements_no_rot, defs, board)
        assert v_no_rot == pytest.approx(4.0)

        # 90 deg rotation: AABB becomes 2x6, [8, 2, 10, 8], fully inside
        placements_rot = [_comp("U1", x=9, y=5, rotation=90)]
        v_rot = compute_boundary_violation(placements_rot, defs, board)
        assert v_rot == pytest.approx(0.0)

    def test_violation_scales_with_overhang(self, board: BoardOutline) -> None:
        """More overhang produces more violation area."""
        defs = [_cdef("U1", width=4, height=4)]

        # Barely outside: x=-0.5, y=5 -> AABB [-2.5, 3, 1.5, 7]
        # Inside: [0, 3, 1.5, 7] = 1.5*4=6, total=4*4=16, violation=10
        v1 = compute_boundary_violation([_comp("U1", x=-0.5, y=5)], defs, board)

        # More outside: x=-2, y=5 -> AABB [-4, 3, 0, 7]
        # Inside: [0, 3, 0, 7] = 0*4=0, total=16, violation=16
        v2 = compute_boundary_violation([_comp("U1", x=-2, y=5)], defs, board)

        assert v2 > v1

    def test_multiple_components(self, board: BoardOutline) -> None:
        """Total violation is summed across components."""
        placements = [
            _comp("U1", x=0, y=5),  # left edge violation
            _comp("U2", x=10, y=5),  # right edge violation
        ]
        defs = [
            _cdef("U1", width=2, height=2),
            _cdef("U2", width=2, height=2),
        ]
        # Each contributes 2.0 mm^2 violation
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(4.0)

    def test_mismatched_lengths_raises(self, board: BoardOutline) -> None:
        with pytest.raises(ValueError, match="placements has 2.*component_defs has 1"):
            compute_boundary_violation(
                [_comp("U1"), _comp("U2")],
                [_cdef("U1")],
                board,
            )

    def test_back_side_same_boundary_rules(self, board: BoardOutline) -> None:
        """Back-side components are subject to the same boundary check."""
        placements = [_comp("U1", x=0, y=5, side=1)]
        defs = [_cdef("U1", width=2, height=2)]
        assert compute_boundary_violation(placements, defs, board) == pytest.approx(2.0)
