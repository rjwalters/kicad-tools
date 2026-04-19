"""Tests for block-aware placement constraints.

Covers:
- BlockGroupDef dataclass and properties
- Block-aware vector encoding: reduced dimensionality
- Block-aware vector decoding: relative positions maintained
- Block rotation at 90/180/270 degrees
- Block boundary violation cost
- Inter-block spacing violation cost
- Block move/rotate/swap utility functions
- Mixed optimization: free + block components
- Regression: no blocks => identical to standard behavior
- Edge case: single-component block
- Edge case: all components in blocks
- PCBLayout.to_block_groups() bridge
- PCBBlock.relative_offsets() helper
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.placement.cost import (
    BlockRegion,
    BoardOutline,
    ComponentPlacement,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    compute_block_boundary_violation,
    compute_inter_block_spacing_violation,
    evaluate_placement,
)
from kicad_tools.placement.vector import (
    FIELDS_PER_BLOCK,
    FIELDS_PER_COMPONENT,
    ROTATION_STEPS,
    BlockGroupDef,
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementVector,
    RelativeOffset,
    bounds,
    bounds_with_blocks,
    decode,
    decode_with_blocks,
    encode,
    encode_with_blocks,
    move_block,
    rotate_block,
    swap_blocks,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) < tol


def _make_board(w: float = 100.0, h: float = 100.0) -> BoardOutline:
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=w, max_y=h)


def _make_comp(ref: str, w: float = 2.0, h: float = 2.0) -> ComponentDef:
    return ComponentDef(reference=ref, width=w, height=h)


def _make_block_with_3_members() -> BlockGroupDef:
    """A block with U1 at origin, C1 at (3,0), C2 at (0,3)."""
    return BlockGroupDef(
        block_id="mcu_block",
        members=(
            RelativeOffset(reference="U1", dx=0.0, dy=0.0, rotation=0.0),
            RelativeOffset(reference="C1", dx=3.0, dy=0.0, rotation=0.0),
            RelativeOffset(reference="C2", dx=0.0, dy=3.0, rotation=0.0),
        ),
    )


# ---------------------------------------------------------------------------
# BlockGroupDef dataclass tests
# ---------------------------------------------------------------------------

class TestBlockGroupDef:
    def test_member_refs(self):
        bg = _make_block_with_3_members()
        assert bg.member_refs == frozenset({"U1", "C1", "C2"})

    def test_empty_block(self):
        bg = BlockGroupDef(block_id="empty")
        assert bg.member_refs == frozenset()
        assert len(bg.members) == 0


# ---------------------------------------------------------------------------
# Block vector encoding tests
# ---------------------------------------------------------------------------

class TestBlockVectorEncoding:
    def test_vector_length_reduced(self):
        """2 blocks (3 each) + 2 free => 4*2 + 3*2 = 14, not 4*8=32."""
        block1 = _make_block_with_3_members()
        block2 = BlockGroupDef(
            block_id="power_block",
            members=(
                RelativeOffset(reference="U2", dx=0.0, dy=0.0),
                RelativeOffset(reference="C3", dx=2.0, dy=0.0),
                RelativeOffset(reference="C4", dx=0.0, dy=2.0),
            ),
        )
        # Place all 8 components
        placements = [
            # Free components
            PlacedComponent(reference="R1", x=10.0, y=10.0, rotation=0.0, side=0),
            PlacedComponent(reference="R2", x=20.0, y=20.0, rotation=90.0, side=0),
            # Block 1 members at block origin (50, 50), rotation 0
            PlacedComponent(reference="U1", x=50.0, y=50.0, rotation=0.0, side=0),
            PlacedComponent(reference="C1", x=53.0, y=50.0, rotation=0.0, side=0),
            PlacedComponent(reference="C2", x=50.0, y=53.0, rotation=0.0, side=0),
            # Block 2 members at block origin (70, 70), rotation 0
            PlacedComponent(reference="U2", x=70.0, y=70.0, rotation=0.0, side=0),
            PlacedComponent(reference="C3", x=72.0, y=70.0, rotation=0.0, side=0),
            PlacedComponent(reference="C4", x=70.0, y=72.0, rotation=0.0, side=0),
        ]

        vec = encode_with_blocks(placements, [block1, block2])
        expected_len = 2 * FIELDS_PER_COMPONENT + 2 * FIELDS_PER_BLOCK  # 8 + 6 = 14
        assert len(vec.data) == expected_len

    def test_encode_decode_roundtrip(self):
        """Encode then decode should recover original positions."""
        block = _make_block_with_3_members()
        placements = [
            PlacedComponent(reference="R1", x=10.0, y=15.0, rotation=0.0, side=0),
            PlacedComponent(reference="U1", x=50.0, y=50.0, rotation=0.0, side=0),
            PlacedComponent(reference="C1", x=53.0, y=50.0, rotation=0.0, side=0),
            PlacedComponent(reference="C2", x=50.0, y=53.0, rotation=0.0, side=0),
        ]
        components = [
            _make_comp("R1"),
            _make_comp("U1", 4.0, 4.0),
            _make_comp("C1"),
            _make_comp("C2"),
        ]

        vec = encode_with_blocks(placements, [block])
        decoded = decode_with_blocks(vec, components, [block])

        assert len(decoded) == 4
        for orig, dec in zip(placements, decoded):
            assert orig.reference == dec.reference
            assert _close(orig.x, dec.x), f"{orig.reference}: x {orig.x} != {dec.x}"
            assert _close(orig.y, dec.y), f"{orig.reference}: y {orig.y} != {dec.y}"

    def test_decode_wrong_length_raises(self):
        """Decode with wrong vector length should raise ValueError."""
        block = _make_block_with_3_members()
        components = [_make_comp("R1"), _make_comp("U1"), _make_comp("C1"), _make_comp("C2")]
        # 1 free (4) + 1 block (3) = 7; give 10 instead
        bad_vec = PlacementVector(data=np.zeros(10, dtype=np.float64))
        with pytest.raises(ValueError, match="Vector length"):
            decode_with_blocks(bad_vec, components, [block])


# ---------------------------------------------------------------------------
# Block rotation tests
# ---------------------------------------------------------------------------

class TestBlockRotation:
    @pytest.mark.parametrize("rot_deg,rot_idx", [(0, 0), (90, 1), (180, 2), (270, 3)])
    def test_block_rotation_maintains_relative_positions(self, rot_deg, rot_idx):
        """Rotating a block should maintain relative distances between members."""
        block = BlockGroupDef(
            block_id="test_block",
            members=(
                RelativeOffset(reference="U1", dx=0.0, dy=0.0),
                RelativeOffset(reference="C1", dx=5.0, dy=0.0),
            ),
        )
        components = [_make_comp("U1", 4.0, 4.0), _make_comp("C1")]

        # Create vector: 0 free + 1 block
        # block at (50, 50) with given rotation
        data = np.array([50.0, 50.0, float(rot_idx)], dtype=np.float64)
        vec = PlacementVector(data=data)

        decoded = decode_with_blocks(vec, components, [block])
        u1 = decoded[0]
        c1 = decoded[1]

        # Distance between U1 and C1 should always be 5.0
        dist = ((u1.x - c1.x) ** 2 + (u1.y - c1.y) ** 2) ** 0.5
        assert _close(dist, 5.0), f"Distance {dist} != 5.0 at rotation {rot_deg}"

    def test_90_degree_rotation_specifics(self):
        """At 90 deg CCW, offset (5,0) becomes (0,5)."""
        block = BlockGroupDef(
            block_id="b",
            members=(
                RelativeOffset(reference="U1", dx=0.0, dy=0.0),
                RelativeOffset(reference="C1", dx=5.0, dy=0.0),
            ),
        )
        components = [_make_comp("U1"), _make_comp("C1")]

        # Block at origin (10, 10), rotation index 1 (90 deg)
        data = np.array([10.0, 10.0, 1.0], dtype=np.float64)
        vec = PlacementVector(data=data)
        decoded = decode_with_blocks(vec, components, [block])

        u1 = decoded[0]
        c1 = decoded[1]
        # U1 at (10, 10), C1 should be at (10-0, 10+5) = (10, 15)
        # _rotate_offset(5, 0, 90) = (-0, 5) = (0, 5)
        assert _close(u1.x, 10.0)
        assert _close(u1.y, 10.0)
        assert _close(c1.x, 10.0), f"C1.x = {c1.x}"
        assert _close(c1.y, 15.0), f"C1.y = {c1.y}"


# ---------------------------------------------------------------------------
# Block boundary violation tests
# ---------------------------------------------------------------------------

class TestBlockBoundaryViolation:
    def test_no_violation_when_inside(self):
        """Components inside their block region should have zero penalty."""
        placements = [
            ComponentPlacement(reference="U1", x=5.0, y=5.0),
            ComponentPlacement(reference="C1", x=7.0, y=5.0),
        ]
        regions = [BlockRegion(block_id="b1", min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)]
        membership = {"U1": "b1", "C1": "b1"}

        result = compute_block_boundary_violation(placements, regions, membership)
        assert result == 0.0

    def test_violation_outside_region(self):
        """Component partially outside should produce positive penalty."""
        placements = [
            ComponentPlacement(reference="U1", x=0.0, y=5.0),  # left edge at -0.5
        ]
        regions = [BlockRegion(block_id="b1", min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)]
        membership = {"U1": "b1"}

        result = compute_block_boundary_violation(placements, regions, membership)
        assert result > 0.0

    def test_no_regions_returns_zero(self):
        placements = [ComponentPlacement(reference="U1", x=5.0, y=5.0)]
        assert compute_block_boundary_violation(placements, [], {}) == 0.0

    def test_free_component_ignored(self):
        """Components not in block_membership should not contribute."""
        placements = [
            ComponentPlacement(reference="R1", x=-10.0, y=-10.0),  # far outside
        ]
        regions = [BlockRegion(block_id="b1", min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)]
        membership = {}  # R1 not in any block

        result = compute_block_boundary_violation(placements, regions, membership)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Inter-block spacing violation tests
# ---------------------------------------------------------------------------

class TestInterBlockSpacingViolation:
    def test_no_violation_when_far_apart(self):
        placements = [
            ComponentPlacement(reference="U1", x=5.0, y=5.0),
            ComponentPlacement(reference="U2", x=50.0, y=50.0),
        ]
        membership = {"U1": "b1", "U2": "b2"}
        result = compute_inter_block_spacing_violation(placements, membership, min_spacing=2.0)
        assert result == 0.0

    def test_violation_when_too_close(self):
        placements = [
            ComponentPlacement(reference="U1", x=5.0, y=5.0),
            ComponentPlacement(reference="U2", x=6.5, y=5.0),
        ]
        membership = {"U1": "b1", "U2": "b2"}
        # With default 1x1 sizes, blocks are 0.5mm apart (edge-to-edge)
        result = compute_inter_block_spacing_violation(placements, membership, min_spacing=2.0)
        assert result > 0.0

    def test_no_membership_returns_zero(self):
        placements = [ComponentPlacement(reference="U1", x=5.0, y=5.0)]
        result = compute_inter_block_spacing_violation(placements, {}, min_spacing=2.0)
        assert result == 0.0

    def test_zero_spacing_returns_zero(self):
        placements = [
            ComponentPlacement(reference="U1", x=5.0, y=5.0),
            ComponentPlacement(reference="U2", x=5.0, y=5.0),  # overlapping
        ]
        membership = {"U1": "b1", "U2": "b2"}
        result = compute_inter_block_spacing_violation(placements, membership, min_spacing=0.0)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Block move/rotate/swap utility tests
# ---------------------------------------------------------------------------

class TestBlockMoveOperations:
    def _make_simple_vec(self):
        """1 free component + 2 blocks => 4 + 3 + 3 = 10 fields."""
        data = np.array([
            # Free: R1 at (10, 20, rot=0, side=0)
            10.0, 20.0, 0.0, 0.0,
            # Block 0 at (30, 40, rot=0)
            30.0, 40.0, 0.0,
            # Block 1 at (60, 70, rot=1)
            60.0, 70.0, 1.0,
        ], dtype=np.float64)
        return PlacementVector(data=data)

    def test_move_block(self):
        vec = self._make_simple_vec()
        moved = move_block(vec, block_index=0, dx=5.0, dy=-3.0, n_free=1)
        assert _close(moved.data[4], 35.0)  # bx
        assert _close(moved.data[5], 37.0)  # by
        # Block 1 unchanged
        assert _close(moved.data[7], 60.0)
        assert _close(moved.data[8], 70.0)

    def test_rotate_block(self):
        vec = self._make_simple_vec()
        rotated = rotate_block(vec, block_index=1, rotation_steps=2, n_free=1)
        # Block 1 rotation was 1, +2 = 3
        assert _close(rotated.data[9], 3.0)
        # Block 0 rotation unchanged
        assert _close(rotated.data[6], 0.0)

    def test_rotate_block_wraps(self):
        vec = self._make_simple_vec()
        rotated = rotate_block(vec, block_index=1, rotation_steps=3, n_free=1)
        # 1 + 3 = 4 => 0 (mod 4)
        assert _close(rotated.data[9], 0.0)

    def test_swap_blocks(self):
        vec = self._make_simple_vec()
        swapped = swap_blocks(vec, block_a=0, block_b=1, n_free=1)
        # Block 0 now has Block 1's position
        assert _close(swapped.data[4], 60.0)
        assert _close(swapped.data[5], 70.0)
        # Block 1 now has Block 0's position
        assert _close(swapped.data[7], 30.0)
        assert _close(swapped.data[8], 40.0)
        # Rotations preserved
        assert _close(swapped.data[6], 0.0)  # Block 0 keeps its rotation
        assert _close(swapped.data[9], 1.0)  # Block 1 keeps its rotation


# ---------------------------------------------------------------------------
# Regression: no blocks => identical behavior
# ---------------------------------------------------------------------------

class TestNoBlocksRegression:
    def test_encode_no_blocks_matches_standard(self):
        """With no block groups, encode_with_blocks should match encode."""
        placements = [
            PlacedComponent(reference="R1", x=10.0, y=20.0, rotation=0.0, side=0),
            PlacedComponent(reference="R2", x=30.0, y=40.0, rotation=90.0, side=1),
        ]
        vec_standard = encode(placements)
        vec_block = encode_with_blocks(placements, [])
        np.testing.assert_array_almost_equal(vec_standard.data, vec_block.data)

    def test_decode_no_blocks_matches_standard(self):
        components = [_make_comp("R1"), _make_comp("R2")]
        placements = [
            PlacedComponent(reference="R1", x=10.0, y=20.0, rotation=0.0, side=0),
            PlacedComponent(reference="R2", x=30.0, y=40.0, rotation=90.0, side=1),
        ]
        vec = encode(placements)
        dec_standard = decode(vec, components)
        dec_block = decode_with_blocks(vec, components, [])

        for s, b in zip(dec_standard, dec_block):
            assert s.reference == b.reference
            assert _close(s.x, b.x)
            assert _close(s.y, b.y)

    def test_evaluate_placement_no_blocks(self):
        """evaluate_placement with no block args should behave identically."""
        placements = [
            ComponentPlacement(reference="R1", x=10.0, y=10.0),
            ComponentPlacement(reference="R2", x=20.0, y=20.0),
        ]
        nets = [Net(name="N1", pins=[("R1", "1"), ("R2", "1")])]
        rules = DesignRuleSet()
        board = _make_board()
        config = PlacementCostConfig()

        score_no_blocks = evaluate_placement(placements, nets, rules, board, config)
        score_with_none = evaluate_placement(
            placements, nets, rules, board, config,
            block_regions=None, block_membership=None,
        )
        assert _close(score_no_blocks.total, score_with_none.total)
        assert score_no_blocks.breakdown.block_boundary == 0.0
        assert score_no_blocks.breakdown.inter_block == 0.0


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_single_component_block(self):
        """A block with one component should behave like a free component."""
        block = BlockGroupDef(
            block_id="single",
            members=(RelativeOffset(reference="U1", dx=0.0, dy=0.0),),
        )
        components = [_make_comp("U1"), _make_comp("R1")]
        placements = [
            PlacedComponent(reference="U1", x=20.0, y=30.0, rotation=0.0, side=0),
            PlacedComponent(reference="R1", x=50.0, y=60.0, rotation=0.0, side=0),
        ]

        vec = encode_with_blocks(placements, [block])
        # 1 free (R1) * 4 + 1 block * 3 = 7
        assert len(vec.data) == 7

        decoded = decode_with_blocks(vec, components, [block])
        u1 = next(d for d in decoded if d.reference == "U1")
        r1 = next(d for d in decoded if d.reference == "R1")
        assert _close(u1.x, 20.0)
        assert _close(u1.y, 30.0)
        assert _close(r1.x, 50.0)
        assert _close(r1.y, 60.0)

    def test_all_components_in_blocks(self):
        """No free components: entire board in blocks."""
        block = BlockGroupDef(
            block_id="all",
            members=(
                RelativeOffset(reference="U1", dx=0.0, dy=0.0),
                RelativeOffset(reference="C1", dx=3.0, dy=0.0),
            ),
        )
        components = [_make_comp("U1"), _make_comp("C1")]
        placements = [
            PlacedComponent(reference="U1", x=40.0, y=40.0, rotation=0.0, side=0),
            PlacedComponent(reference="C1", x=43.0, y=40.0, rotation=0.0, side=0),
        ]

        vec = encode_with_blocks(placements, [block])
        # 0 free + 1 block * 3 = 3
        assert len(vec.data) == 3

        decoded = decode_with_blocks(vec, components, [block])
        assert len(decoded) == 2
        u1 = next(d for d in decoded if d.reference == "U1")
        c1 = next(d for d in decoded if d.reference == "C1")
        assert _close(u1.x, 40.0)
        assert _close(c1.x, 43.0)

    def test_empty_block_group(self):
        """A block with no members should not cause errors."""
        block = BlockGroupDef(block_id="empty")
        components = [_make_comp("R1")]
        placements = [
            PlacedComponent(reference="R1", x=10.0, y=10.0, rotation=0.0, side=0),
        ]

        vec = encode_with_blocks(placements, [block])
        # 1 free * 4 + 1 empty block * 3 = 7
        assert len(vec.data) == 7

        decoded = decode_with_blocks(vec, components, [block])
        assert len(decoded) == 1
        assert decoded[0].reference == "R1"


# ---------------------------------------------------------------------------
# Bounds tests
# ---------------------------------------------------------------------------

class TestBoundsWithBlocks:
    def test_bounds_length(self):
        board = _make_board()
        block = _make_block_with_3_members()
        components = [_make_comp("R1"), _make_comp("U1"), _make_comp("C1"), _make_comp("C2")]
        b = bounds_with_blocks(board, components, [block])
        # 1 free (R1) * 4 + 1 block * 3 = 7
        assert len(b.lower) == 7
        assert len(b.upper) == 7
        assert len(b.discrete_mask) == 7

    def test_block_rotation_is_discrete(self):
        board = _make_board()
        block = _make_block_with_3_members()
        components = [_make_comp("R1"), _make_comp("U1"), _make_comp("C1"), _make_comp("C2")]
        b = bounds_with_blocks(board, components, [block])
        # Block rotation at index 6 (4 free fields + 2 block xy fields)
        assert b.discrete_mask[6] == True  # noqa: E712

    def test_no_blocks_matches_standard(self):
        board = _make_board()
        components = [_make_comp("R1"), _make_comp("R2")]
        b_standard = bounds(board, components)
        b_block = bounds_with_blocks(board, components, [])
        np.testing.assert_array_almost_equal(b_standard.lower, b_block.lower)
        np.testing.assert_array_almost_equal(b_standard.upper, b_block.upper)


# ---------------------------------------------------------------------------
# evaluate_placement with block arguments
# ---------------------------------------------------------------------------

class TestEvaluatePlacementWithBlocks:
    def test_block_boundary_violation_in_score(self):
        """Block boundary violation should be reflected in the total score."""
        placements = [
            ComponentPlacement(reference="U1", x=-5.0, y=5.0),  # outside region
        ]
        regions = [BlockRegion(block_id="b1", min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)]
        membership = {"U1": "b1"}
        board = _make_board()
        nets: list[Net] = []
        rules = DesignRuleSet()

        score = evaluate_placement(
            placements, nets, rules, board,
            block_regions=regions, block_membership=membership,
        )
        assert score.breakdown.block_boundary > 0.0
        assert not score.is_feasible

    def test_feasible_with_blocks(self):
        """All components inside regions => feasible."""
        placements = [
            ComponentPlacement(reference="U1", x=5.0, y=5.0),
            ComponentPlacement(reference="R1", x=50.0, y=50.0),
        ]
        regions = [BlockRegion(block_id="b1", min_x=0.0, min_y=0.0, max_x=10.0, max_y=10.0)]
        membership = {"U1": "b1"}
        board = _make_board()
        nets: list[Net] = []
        rules = DesignRuleSet()

        score = evaluate_placement(
            placements, nets, rules, board,
            block_regions=regions, block_membership=membership,
        )
        assert score.breakdown.block_boundary == 0.0
        assert score.is_feasible


# ---------------------------------------------------------------------------
# PCBLayout.to_block_groups() bridge test
# ---------------------------------------------------------------------------

class TestPCBLayoutBridge:
    def test_to_block_groups(self):
        from kicad_tools.pcb.layout import PCBLayout
        from kicad_tools.pcb.blocks.base import PCBBlock

        layout = PCBLayout("test")
        block = PCBBlock("mcu")
        block.add_component("U1", "QFP-48", 0.0, 0.0)
        block.add_component("C1", "C_0805", 3.0, 0.0)
        layout.add_block(block, "mcu")

        groups = layout.to_block_groups()
        assert len(groups) == 1
        bg = groups[0]
        assert bg.block_id == "mcu"
        assert len(bg.members) == 2
        refs = {m.reference for m in bg.members}
        assert refs == {"U1", "C1"}

    def test_to_block_groups_empty_layout(self):
        from kicad_tools.pcb.layout import PCBLayout

        layout = PCBLayout("empty")
        groups = layout.to_block_groups()
        assert groups == []


# ---------------------------------------------------------------------------
# PCBBlock.relative_offsets() helper test
# ---------------------------------------------------------------------------

class TestPCBBlockRelativeOffsets:
    def test_relative_offsets(self):
        from kicad_tools.pcb.blocks.base import PCBBlock

        block = PCBBlock("test")
        block.add_component("U1", "QFP-48", 0.0, 0.0, rotation=0.0)
        block.add_component("C1", "C_0805", 3.0, 1.5, rotation=90.0)

        offsets = block.relative_offsets()
        assert len(offsets) == 2

        u1_offset = next(o for o in offsets if o.reference == "U1")
        c1_offset = next(o for o in offsets if o.reference == "C1")

        assert _close(u1_offset.dx, 0.0)
        assert _close(u1_offset.dy, 0.0)
        assert _close(c1_offset.dx, 3.0)
        assert _close(c1_offset.dy, 1.5)
        assert _close(c1_offset.rotation, 90.0)
