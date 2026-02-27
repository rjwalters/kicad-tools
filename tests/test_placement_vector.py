"""Tests for PlacementVector type, encode/decode, and bounds.

Covers:
- PlacementVector construction and properties
- Round-trip encode/decode identity
- Pad coordinate transforms for all 4 rotations x 2 sides (8 combos)
- bounds() with board outline and component dimensions
- A 3-component board scenario
"""

from __future__ import annotations

import numpy as np
import pytest

from kicad_tools.placement.cost import BoardOutline
from kicad_tools.placement.vector import (
    FIELDS_PER_COMPONENT,
    ROTATION_STEPS,
    ComponentDef,
    PadDef,
    PlacedComponent,
    PlacementBounds,
    PlacementVector,
    TransformedPad,
    bounds,
    decode,
    encode,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) < tol


def _make_single_pad_component(ref: str = "U1") -> ComponentDef:
    """Component with a single pad at local (1, 2)."""
    return ComponentDef(
        reference=ref,
        pads=(PadDef(name="1", local_x=1.0, local_y=2.0, size_x=0.6, size_y=0.8),),
        width=4.0,
        height=4.0,
    )


def _make_two_pad_component(ref: str = "R1") -> ComponentDef:
    """Resistor-like component with two pads at +/-1.5mm on X axis."""
    return ComponentDef(
        reference=ref,
        pads=(
            PadDef(name="1", local_x=-1.5, local_y=0.0, size_x=0.8, size_y=0.8),
            PadDef(name="2", local_x=1.5, local_y=0.0, size_x=0.8, size_y=0.8),
        ),
        width=4.0,
        height=2.0,
    )


# ---------------------------------------------------------------------------
# PlacementVector basics
# ---------------------------------------------------------------------------


class TestPlacementVector:
    def test_construction(self):
        data = np.array([10.0, 20.0, 0.0, 0.0], dtype=np.float64)
        vec = PlacementVector(data=data)
        assert vec.num_components == 1
        np.testing.assert_array_equal(vec.data, data)

    def test_num_components_multiple(self):
        data = np.zeros(12, dtype=np.float64)
        vec = PlacementVector(data=data)
        assert vec.num_components == 3

    def test_component_slice(self):
        data = np.array([1, 2, 3, 4, 5, 6, 7, 8], dtype=np.float64)
        vec = PlacementVector(data=data)
        np.testing.assert_array_equal(vec.component_slice(0), [1, 2, 3, 4])
        np.testing.assert_array_equal(vec.component_slice(1), [5, 6, 7, 8])

    def test_equality(self):
        a = PlacementVector(data=np.array([1.0, 2.0, 0.0, 0.0]))
        b = PlacementVector(data=np.array([1.0, 2.0, 0.0, 0.0]))
        c = PlacementVector(data=np.array([1.0, 2.0, 1.0, 0.0]))
        assert a == b
        assert a != c

    def test_repr(self):
        vec = PlacementVector(data=np.zeros(4))
        r = repr(vec)
        assert "PlacementVector" in r
        assert "n_components=1" in r


# ---------------------------------------------------------------------------
# Encode
# ---------------------------------------------------------------------------


class TestEncode:
    def test_single_component(self):
        placed = PlacedComponent(reference="U1", x=10.0, y=20.0, rotation=90.0, side=0)
        vec = encode([placed])
        assert vec.num_components == 1
        s = vec.component_slice(0)
        assert _close(s[0], 10.0)
        assert _close(s[1], 20.0)
        assert _close(s[2], 1.0)  # 90 / 90 = 1
        assert _close(s[3], 0.0)

    def test_rotation_encoding(self):
        """Rotation degrees map to indices 0-3."""
        for idx, deg in enumerate(ROTATION_STEPS):
            placed = PlacedComponent(reference="X1", x=0, y=0, rotation=deg, side=0)
            vec = encode([placed])
            assert _close(vec.data[2], float(idx)), f"rotation {deg} -> index {idx}"

    def test_side_encoding(self):
        front = PlacedComponent(reference="A1", x=0, y=0, rotation=0, side=0)
        back = PlacedComponent(reference="A1", x=0, y=0, rotation=0, side=1)
        assert _close(encode([front]).data[3], 0.0)
        assert _close(encode([back]).data[3], 1.0)

    def test_multiple_components(self):
        placements = [
            PlacedComponent(reference="U1", x=5, y=10, rotation=0, side=0),
            PlacedComponent(reference="R1", x=15, y=25, rotation=180, side=1),
        ]
        vec = encode(placements)
        assert vec.num_components == 2
        s0 = vec.component_slice(0)
        s1 = vec.component_slice(1)
        assert _close(s0[0], 5.0) and _close(s0[1], 10.0)
        assert _close(s1[0], 15.0) and _close(s1[1], 25.0)
        assert _close(s1[2], 2.0)  # 180 / 90 = 2
        assert _close(s1[3], 1.0)  # back


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------


class TestDecode:
    def test_single_component_no_pads(self):
        comp = ComponentDef(reference="U1")
        vec = PlacementVector(data=np.array([10.0, 20.0, 1.0, 0.0]))
        result = decode(vec, [comp])
        assert len(result) == 1
        p = result[0]
        assert p.reference == "U1"
        assert _close(p.x, 10.0)
        assert _close(p.y, 20.0)
        assert _close(p.rotation, 90.0)
        assert p.side == 0

    def test_mismatched_length_raises(self):
        comp = ComponentDef(reference="U1")
        vec = PlacementVector(data=np.zeros(8))  # 2 components
        with pytest.raises(ValueError, match="2 components but 1"):
            decode(vec, [comp])

    def test_pad_transform_identity(self):
        """Rotation=0, side=0 means pad position = component + local offset."""
        comp = _make_single_pad_component()
        vec = PlacementVector(data=np.array([10.0, 20.0, 0.0, 0.0]))
        result = decode(vec, [comp])
        pad = result[0].pads[0]
        assert _close(pad.x, 11.0)  # 10 + 1
        assert _close(pad.y, 22.0)  # 20 + 2
        assert _close(pad.size_x, 0.6)
        assert _close(pad.size_y, 0.8)


# ---------------------------------------------------------------------------
# Round-trip encode/decode
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_basic_round_trip(self):
        comp = _make_single_pad_component()
        original = PlacedComponent(
            reference="U1",
            x=10.0,
            y=20.0,
            rotation=0.0,
            side=0,
            pads=(TransformedPad(name="1", x=11.0, y=22.0, size_x=0.6, size_y=0.8),),
        )
        vec = encode([original])
        decoded = decode(vec, [comp])
        d = decoded[0]
        assert d.reference == original.reference
        assert _close(d.x, original.x)
        assert _close(d.y, original.y)
        assert _close(d.rotation, original.rotation)
        assert d.side == original.side

    def test_round_trip_all_rotations_front(self):
        comp = _make_single_pad_component()
        for deg in ROTATION_STEPS:
            original = PlacedComponent(reference="U1", x=5.0, y=10.0, rotation=deg, side=0)
            vec = encode([original])
            decoded = decode(vec, [comp])
            d = decoded[0]
            assert _close(d.rotation, deg), f"Failed round-trip for rotation {deg}"
            assert _close(d.x, 5.0)
            assert _close(d.y, 10.0)
            assert d.side == 0

    def test_round_trip_all_rotations_back(self):
        comp = _make_single_pad_component()
        for deg in ROTATION_STEPS:
            original = PlacedComponent(reference="U1", x=5.0, y=10.0, rotation=deg, side=1)
            vec = encode([original])
            decoded = decode(vec, [comp])
            d = decoded[0]
            assert _close(d.rotation, deg), f"Failed round-trip for rotation {deg} back"
            assert d.side == 1

    def test_round_trip_encode_decode_encode(self):
        """encode -> decode -> encode produces identical vector."""
        comp = _make_two_pad_component()
        original = PlacedComponent(reference="R1", x=7.0, y=3.0, rotation=270.0, side=1)
        vec1 = encode([original])
        decoded = decode(vec1, [comp])
        vec2 = encode(decoded)
        np.testing.assert_array_almost_equal(vec1.data, vec2.data)


# ---------------------------------------------------------------------------
# Pad transforms: all 8 combinations (4 rotations x 2 sides)
# ---------------------------------------------------------------------------


class TestPadTransforms:
    """Test pad at local (1, 2) with size (0.6, 0.8) placed at component (10, 20)."""

    @pytest.fixture
    def comp(self):
        return _make_single_pad_component()

    def _decode_pad(self, comp, rot_idx, side):
        vec = PlacementVector(data=np.array([10.0, 20.0, float(rot_idx), float(side)]))
        result = decode(vec, [comp])
        return result[0].pads[0]

    # --- Front side (side=0) ---

    def test_rot0_front(self, comp):
        pad = self._decode_pad(comp, 0, 0)
        assert _close(pad.x, 11.0)  # 10 + 1
        assert _close(pad.y, 22.0)  # 20 + 2
        assert _close(pad.size_x, 0.6)
        assert _close(pad.size_y, 0.8)

    def test_rot90_front(self, comp):
        pad = self._decode_pad(comp, 1, 0)
        # 90 CCW: (lx, ly) -> (-ly, lx) => (-2, 1)
        assert _close(pad.x, 8.0)  # 10 + (-2)
        assert _close(pad.y, 21.0)  # 20 + 1
        assert _close(pad.size_x, 0.8)  # swapped
        assert _close(pad.size_y, 0.6)

    def test_rot180_front(self, comp):
        pad = self._decode_pad(comp, 2, 0)
        # 180: (lx, ly) -> (-lx, -ly) => (-1, -2)
        assert _close(pad.x, 9.0)  # 10 + (-1)
        assert _close(pad.y, 18.0)  # 20 + (-2)
        assert _close(pad.size_x, 0.6)
        assert _close(pad.size_y, 0.8)

    def test_rot270_front(self, comp):
        pad = self._decode_pad(comp, 3, 0)
        # 270 CCW: (lx, ly) -> (ly, -lx) => (2, -1)
        assert _close(pad.x, 12.0)  # 10 + 2
        assert _close(pad.y, 19.0)  # 20 + (-1)
        assert _close(pad.size_x, 0.8)  # swapped
        assert _close(pad.size_y, 0.6)

    # --- Back side (side=1): mirror first, then rotate ---

    def test_rot0_back(self, comp):
        pad = self._decode_pad(comp, 0, 1)
        # Mirror: lx -> -lx => (-1, 2), then rot 0 => (-1, 2)
        assert _close(pad.x, 9.0)  # 10 + (-1)
        assert _close(pad.y, 22.0)  # 20 + 2
        assert _close(pad.size_x, 0.6)
        assert _close(pad.size_y, 0.8)

    def test_rot90_back(self, comp):
        pad = self._decode_pad(comp, 1, 1)
        # Mirror: (-1, 2), then 90 CCW: (-ly, lx) => (-2, -1)
        assert _close(pad.x, 8.0)  # 10 + (-2)
        assert _close(pad.y, 19.0)  # 20 + (-1)
        assert _close(pad.size_x, 0.8)
        assert _close(pad.size_y, 0.6)

    def test_rot180_back(self, comp):
        pad = self._decode_pad(comp, 2, 1)
        # Mirror: (-1, 2), then 180: (1, -2)
        assert _close(pad.x, 11.0)  # 10 + 1
        assert _close(pad.y, 18.0)  # 20 + (-2)
        assert _close(pad.size_x, 0.6)
        assert _close(pad.size_y, 0.8)

    def test_rot270_back(self, comp):
        pad = self._decode_pad(comp, 3, 1)
        # Mirror: (-1, 2), then 270 CCW: (ly, -lx) => (2, 1)
        assert _close(pad.x, 12.0)  # 10 + 2
        assert _close(pad.y, 21.0)  # 20 + 1
        assert _close(pad.size_x, 0.8)
        assert _close(pad.size_y, 0.6)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


class TestBounds:
    @pytest.fixture
    def board(self):
        return BoardOutline(min_x=0.0, min_y=0.0, max_x=100.0, max_y=80.0)

    def test_single_component_bounds(self, board):
        comp = ComponentDef(reference="U1", width=10.0, height=6.0)
        bnd = bounds(board, [comp])

        assert isinstance(bnd, PlacementBounds)
        assert len(bnd.lower) == 4
        assert len(bnd.upper) == 4
        assert len(bnd.discrete_mask) == 4

        # x: [0 + 5, 100 - 5] = [5, 95]
        assert _close(bnd.lower[0], 5.0)
        assert _close(bnd.upper[0], 95.0)

        # y: [0 + 3, 80 - 3] = [3, 77]
        assert _close(bnd.lower[1], 3.0)
        assert _close(bnd.upper[1], 77.0)

        # rot: [0, 3]
        assert _close(bnd.lower[2], 0.0)
        assert _close(bnd.upper[2], 3.0)
        assert bnd.discrete_mask[2] is np.True_

        # side: [0, 1]
        assert _close(bnd.lower[3], 0.0)
        assert _close(bnd.upper[3], 1.0)
        assert bnd.discrete_mask[3] is np.True_

        # x, y are continuous
        assert bnd.discrete_mask[0] is np.False_
        assert bnd.discrete_mask[1] is np.False_

    def test_multiple_component_bounds(self, board):
        comps = [
            ComponentDef(reference="U1", width=10.0, height=6.0),
            ComponentDef(reference="R1", width=4.0, height=2.0),
        ]
        bnd = bounds(board, comps)
        assert len(bnd.lower) == 8

        # U1 x: [5, 95]
        assert _close(bnd.lower[0], 5.0)
        assert _close(bnd.upper[0], 95.0)

        # R1 x: [2, 98]
        assert _close(bnd.lower[4], 2.0)
        assert _close(bnd.upper[4], 98.0)

        # R1 y: [1, 79]
        assert _close(bnd.lower[5], 1.0)
        assert _close(bnd.upper[5], 79.0)

    def test_bounds_discrete_mask_pattern(self, board):
        """Every 3rd and 4th element (0-indexed: 2,3, 6,7, ...) are discrete."""
        comps = [
            ComponentDef(reference="A"),
            ComponentDef(reference="B"),
            ComponentDef(reference="C"),
        ]
        bnd = bounds(board, comps)
        for i in range(3):
            base = i * FIELDS_PER_COMPONENT
            assert not bnd.discrete_mask[base]  # x
            assert not bnd.discrete_mask[base + 1]  # y
            assert bnd.discrete_mask[base + 2]  # rot
            assert bnd.discrete_mask[base + 3]  # side


# ---------------------------------------------------------------------------
# 3-component board scenario
# ---------------------------------------------------------------------------


class TestThreeComponentBoard:
    """Integration test with a realistic 3-component placement."""

    @pytest.fixture
    def board(self):
        return BoardOutline(min_x=0.0, min_y=0.0, max_x=50.0, max_y=40.0)

    @pytest.fixture
    def components(self):
        return [
            ComponentDef(
                reference="U1",
                pads=(
                    PadDef(name="1", local_x=-2.0, local_y=0.0),
                    PadDef(name="2", local_x=2.0, local_y=0.0),
                ),
                width=6.0,
                height=4.0,
            ),
            ComponentDef(
                reference="R1",
                pads=(
                    PadDef(name="1", local_x=-1.0, local_y=0.0),
                    PadDef(name="2", local_x=1.0, local_y=0.0),
                ),
                width=3.0,
                height=1.5,
            ),
            ComponentDef(
                reference="C1",
                pads=(
                    PadDef(name="1", local_x=0.0, local_y=-0.5),
                    PadDef(name="2", local_x=0.0, local_y=0.5),
                ),
                width=2.0,
                height=2.0,
            ),
        ]

    def test_encode_decode_round_trip(self, components):
        placements = [
            PlacedComponent(reference="U1", x=20.0, y=15.0, rotation=0.0, side=0),
            PlacedComponent(reference="R1", x=35.0, y=10.0, rotation=90.0, side=0),
            PlacedComponent(reference="C1", x=10.0, y=30.0, rotation=180.0, side=1),
        ]
        vec = encode(placements)
        assert vec.num_components == 3

        decoded = decode(vec, components)
        assert len(decoded) == 3

        for orig, dec in zip(placements, decoded, strict=True):
            assert dec.reference == orig.reference
            assert _close(dec.x, orig.x)
            assert _close(dec.y, orig.y)
            assert _close(dec.rotation, orig.rotation)
            assert dec.side == orig.side

    def test_decode_pad_count(self, components):
        vec = PlacementVector(
            data=np.array(
                [
                    20.0,
                    15.0,
                    0.0,
                    0.0,  # U1
                    35.0,
                    10.0,
                    1.0,
                    0.0,  # R1 at 90 deg
                    10.0,
                    30.0,
                    2.0,
                    1.0,  # C1 at 180 deg, back
                ]
            )
        )
        decoded = decode(vec, components)
        assert len(decoded[0].pads) == 2  # U1
        assert len(decoded[1].pads) == 2  # R1
        assert len(decoded[2].pads) == 2  # C1

    def test_u1_pads_at_identity(self, components):
        """U1 at (20,15) rot=0 side=0: pads at (18,15) and (22,15)."""
        vec = PlacementVector(
            data=np.array(
                [
                    20.0,
                    15.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                ]
            )
        )
        u1 = decode(vec, components)[0]
        p1, p2 = u1.pads
        assert _close(p1.x, 18.0) and _close(p1.y, 15.0)
        assert _close(p2.x, 22.0) and _close(p2.y, 15.0)

    def test_r1_pads_at_90deg(self, components):
        """R1 at (35,10) rot=90 side=0: pads rotated 90 CCW.

        Pad 1 local (-1, 0) -> 90 CCW -> (0, -1) -> abs (35, 9)
        Pad 2 local (1, 0)  -> 90 CCW -> (0, 1)  -> abs (35, 11)
        """
        vec = PlacementVector(
            data=np.array(
                [
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    35.0,
                    10.0,
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                ]
            )
        )
        r1 = decode(vec, components)[1]
        p1, p2 = r1.pads
        assert _close(p1.x, 35.0) and _close(p1.y, 9.0)
        assert _close(p2.x, 35.0) and _close(p2.y, 11.0)

    def test_c1_pads_at_180_back(self, components):
        """C1 at (10,30) rot=180 side=1 (back).

        Pad 1 local (0, -0.5) -> mirror X -> (0, -0.5)
                               -> 180     -> (0, 0.5)
                               -> abs     -> (10, 30.5)
        Pad 2 local (0, 0.5)  -> mirror X -> (0, 0.5)
                               -> 180     -> (0, -0.5)
                               -> abs     -> (10, 29.5)
        """
        vec = PlacementVector(
            data=np.array(
                [
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    10.0,
                    30.0,
                    2.0,
                    1.0,
                ]
            )
        )
        c1 = decode(vec, components)[2]
        p1, p2 = c1.pads
        assert _close(p1.x, 10.0) and _close(p1.y, 30.5)
        assert _close(p2.x, 10.0) and _close(p2.y, 29.5)

    def test_bounds_correct(self, board, components):
        bnd = bounds(board, components)
        assert len(bnd.lower) == 12

        # U1 (6x4): x in [3, 47], y in [2, 38]
        assert _close(bnd.lower[0], 3.0)
        assert _close(bnd.upper[0], 47.0)
        assert _close(bnd.lower[1], 2.0)
        assert _close(bnd.upper[1], 38.0)

        # R1 (3x1.5): x in [1.5, 48.5], y in [0.75, 39.25]
        assert _close(bnd.lower[4], 1.5)
        assert _close(bnd.upper[4], 48.5)
        assert _close(bnd.lower[5], 0.75)
        assert _close(bnd.upper[5], 39.25)

        # C1 (2x2): x in [1, 49], y in [1, 39]
        assert _close(bnd.lower[8], 1.0)
        assert _close(bnd.upper[8], 49.0)
        assert _close(bnd.lower[9], 1.0)
        assert _close(bnd.upper[9], 39.0)

    def test_vector_data_length(self, components):
        """Vector should be exactly 4*3=12 elements."""
        placements = [
            PlacedComponent(reference="U1", x=20, y=15, rotation=0, side=0),
            PlacedComponent(reference="R1", x=35, y=10, rotation=90, side=0),
            PlacedComponent(reference="C1", x=10, y=30, rotation=180, side=1),
        ]
        vec = encode(placements)
        assert len(vec.data) == 12
        assert vec.data.dtype == np.float64


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_placements(self):
        vec = encode([])
        assert vec.num_components == 0
        assert len(vec.data) == 0
        decoded = decode(vec, [])
        assert decoded == []

    def test_component_with_no_pads(self):
        comp = ComponentDef(reference="J1")
        placed = PlacedComponent(reference="J1", x=5, y=5, rotation=0, side=0)
        vec = encode([placed])
        decoded = decode(vec, [comp])
        assert decoded[0].pads == ()

    def test_rotation_wrapping(self):
        """Rotation index should wrap modulo 4."""
        comp = ComponentDef(reference="X1")
        # Manually create vector with rot index = 4 (should wrap to 0)
        vec = PlacementVector(data=np.array([0.0, 0.0, 4.0, 0.0]))
        decoded = decode(vec, [comp])
        assert _close(decoded[0].rotation, 0.0)

    def test_small_board_tight_bounds(self):
        """When a component nearly fills the board, bounds are very tight."""
        board = BoardOutline(min_x=0, min_y=0, max_x=10, max_y=10)
        comp = ComponentDef(reference="U1", width=9.0, height=9.0)
        bnd = bounds(board, [comp])
        # x: [4.5, 5.5], y: [4.5, 5.5]
        assert _close(bnd.lower[0], 4.5)
        assert _close(bnd.upper[0], 5.5)
        assert _close(bnd.lower[1], 4.5)
        assert _close(bnd.upper[1], 5.5)
