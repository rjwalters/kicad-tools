"""PlacementVector type and encode/decode for optimization algorithms.

Encodes a complete board placement as a flat numeric array suitable for
optimization algorithms. Each component occupies 4 entries:
    [x, y, rot, side]

where:
    x, y   -- continuous position in mm (within board bounds)
    rot    -- discrete rotation index {0, 1, 2, 3} => {0, 90, 180, 270} degrees
    side   -- binary {0, 1} => {front, back}

Block-aware encoding
--------------------
When ``block_groups`` are provided, components belonging to a block share a
single ``[bx, by, brot]`` triple in the vector instead of individual
``[x, y, rot, side]`` entries.  This reduces the search-space dimensionality
from ``4*N`` to ``4*N_free + 3*N_blocks`` and lets the optimizer move
blocks as rigid units.

Usage:
    vector = encode(placements)
    placements = decode(vector, component_defs)
    bnd = bounds(board, component_defs)

    # Block-aware:
    vector = encode_with_blocks(placements, block_groups)
    placements = decode_with_blocks(vector, component_defs, block_groups)
    bnd = bounds_with_blocks(board, component_defs, block_groups)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from numpy.typing import NDArray

from .cost import BoardOutline

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIELDS_PER_COMPONENT = 4
"""Number of fields per component in the flat vector: x, y, rot, side."""

ROTATION_STEPS = (0.0, 90.0, 180.0, 270.0)
"""Discrete rotation values in degrees, indexed 0-3."""


# ---------------------------------------------------------------------------
# Supporting data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PadDef:
    """Definition of a pad in local component coordinates.

    Attributes:
        name: Pad name/number (e.g. "1", "A1").
        local_x: X offset from component origin in mm.
        local_y: Y offset from component origin in mm.
        size_x: Pad width in mm.
        size_y: Pad height in mm.
    """

    name: str
    local_x: float
    local_y: float
    size_x: float = 0.5
    size_y: float = 0.5


@dataclass(frozen=True)
class ComponentDef:
    """Static definition of a component to be placed.

    This captures the *identity* and *geometry* of a component, but not its
    current position.  Position is encoded in the :class:`PlacementVector`.

    Attributes:
        reference: Component reference designator (e.g. "U1", "R3").
        pads: Pad definitions in local component coordinates.
        width: Bounding box width in mm (used for bounds calculation).
        height: Bounding box height in mm (used for bounds calculation).
    """

    reference: str
    pads: tuple[PadDef, ...] = ()
    width: float = 1.0
    height: float = 1.0


@dataclass(frozen=True)
class RelativeOffset:
    """Position of a component relative to its block origin.

    Attributes:
        reference: Component reference designator.
        dx: X offset from block origin in mm.
        dy: Y offset from block origin in mm.
        rotation: Component rotation relative to block rotation in degrees.
        side: Board side (0=front, 1=back).
    """

    reference: str
    dx: float
    dy: float
    rotation: float = 0.0
    side: int = 0


@dataclass(frozen=True)
class BlockGroupDef:
    """Definition of a block group for reduced-dimensionality encoding.

    Maps a block identifier to its member components and their relative
    offsets from the block origin. During encoding the block is represented
    as 3 fields ``[bx, by, brot]`` instead of ``4*M`` fields for *M* members.

    Attributes:
        block_id: Unique identifier for the block.
        members: Relative offsets for each member component.
    """

    block_id: str
    members: tuple[RelativeOffset, ...] = field(default_factory=tuple)

    @property
    def member_refs(self) -> frozenset[str]:
        """Set of component reference designators in this block."""
        return frozenset(m.reference for m in self.members)


FIELDS_PER_BLOCK = 3
"""Number of fields per block in the flat vector: bx, by, brot."""


@dataclass(frozen=True)
class TransformedPad:
    """A pad with its absolute board coordinates after placement transform.

    Attributes:
        name: Pad name/number.
        x: Absolute X position in mm.
        y: Absolute Y position in mm.
        size_x: Pad width in mm (swapped for 90/270 rotation).
        size_y: Pad height in mm (swapped for 90/270 rotation).
    """

    name: str
    x: float
    y: float
    size_x: float
    size_y: float


@dataclass(frozen=True)
class PlacedComponent:
    """A component with its resolved position and transformed pads.

    Attributes:
        reference: Component reference designator.
        x: X position in mm.
        y: Y position in mm.
        rotation: Rotation in degrees (0, 90, 180, 270).
        side: Board side -- 0 for front, 1 for back.
        pads: Transformed pad coordinates in absolute board space.
    """

    reference: str
    x: float
    y: float
    rotation: float
    side: int
    pads: tuple[TransformedPad, ...] = ()


@dataclass(frozen=True)
class DimensionBound:
    """Bound for a single dimension of the placement vector.

    Attributes:
        lower: Lower bound (inclusive).
        upper: Upper bound (inclusive).
        is_discrete: True for rotation and side dimensions.
    """

    lower: float
    upper: float
    is_discrete: bool = False


@dataclass(frozen=True)
class PlacementBounds:
    """Per-dimension bounds for the entire placement vector.

    Attributes:
        lower: Array of lower bounds, shape (4N,).
        upper: Array of upper bounds, shape (4N,).
        discrete_mask: Boolean array, True for discrete dimensions, shape (4N,).
    """

    lower: NDArray[np.float64]
    upper: NDArray[np.float64]
    discrete_mask: NDArray[np.bool_]


# ---------------------------------------------------------------------------
# PlacementVector
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacementVector:
    """Flat numeric encoding of a complete board placement.

    Wraps a numpy array of length 4N where N is the number of components.
    Layout: ``[x0, y0, rot0, side0, x1, y1, rot1, side1, ...]``

    Attributes:
        data: The underlying flat numpy array.
    """

    data: NDArray[np.float64]

    @property
    def num_components(self) -> int:
        """Number of components encoded in this vector."""
        return len(self.data) // FIELDS_PER_COMPONENT

    def component_slice(self, index: int) -> NDArray[np.float64]:
        """Return the (x, y, rot, side) slice for component *index*."""
        start = index * FIELDS_PER_COMPONENT
        return self.data[start : start + FIELDS_PER_COMPONENT]

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PlacementVector):
            return NotImplemented
        return np.array_equal(self.data, other.data)

    def __repr__(self) -> str:
        n = self.num_components
        return f"PlacementVector(n_components={n}, data={self.data!r})"


# ---------------------------------------------------------------------------
# Pad coordinate transforms
# ---------------------------------------------------------------------------


def _transform_pad(
    pad: PadDef,
    comp_x: float,
    comp_y: float,
    rotation_deg: float,
    side: int,
) -> TransformedPad:
    """Transform a pad from local to absolute coordinates.

    The transform order is:
    1. Mirror across the local Y axis if side == 1 (back).
    2. Rotate by *rotation_deg* around the local origin.
    3. Translate by (comp_x, comp_y).

    Args:
        pad: Pad definition in local coordinates.
        comp_x: Component X position in mm.
        comp_y: Component Y position in mm.
        rotation_deg: Rotation in degrees (0, 90, 180, 270).
        side: 0 for front, 1 for back.

    Returns:
        Transformed pad in absolute board coordinates.
    """
    lx = pad.local_x
    ly = pad.local_y
    sx = pad.size_x
    sy = pad.size_y

    # Step 1: mirror across Y axis for back side
    if side == 1:
        lx = -lx

    # Step 2: rotate around local origin
    rot_idx = int(round(rotation_deg / 90.0)) % 4
    if rot_idx == 0:
        rx, ry = lx, ly
        out_sx, out_sy = sx, sy
    elif rot_idx == 1:  # 90 degrees CCW
        rx, ry = -ly, lx
        out_sx, out_sy = sy, sx
    elif rot_idx == 2:  # 180 degrees
        rx, ry = -lx, -ly
        out_sx, out_sy = sx, sy
    else:  # 270 degrees CCW (= 90 CW)
        rx, ry = ly, -lx
        out_sx, out_sy = sy, sx

    # Step 3: translate
    abs_x = comp_x + rx
    abs_y = comp_y + ry

    return TransformedPad(
        name=pad.name,
        x=abs_x,
        y=abs_y,
        size_x=out_sx,
        size_y=out_sy,
    )


# ---------------------------------------------------------------------------
# Encode / Decode
# ---------------------------------------------------------------------------


def encode(placements: Sequence[PlacedComponent]) -> PlacementVector:
    """Convert a list of placed components to a flat placement vector.

    Args:
        placements: Ordered sequence of placed components.

    Returns:
        A :class:`PlacementVector` encoding the positions.
    """
    n = len(placements)
    data = np.empty(n * FIELDS_PER_COMPONENT, dtype=np.float64)

    for i, p in enumerate(placements):
        base = i * FIELDS_PER_COMPONENT
        # Convert rotation degrees to index
        rot_idx = int(round(p.rotation / 90.0)) % 4
        data[base] = p.x
        data[base + 1] = p.y
        data[base + 2] = float(rot_idx)
        data[base + 3] = float(p.side)

    return PlacementVector(data=data)


def decode(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
) -> list[PlacedComponent]:
    """Convert a flat placement vector to a list of placed components.

    For each component, the pads from its :class:`ComponentDef` are
    transformed (mirrored, rotated, translated) to absolute board coordinates.

    Args:
        vector: Flat placement vector of length 4N.
        components: Component definitions (must be same length N and same
            order as used when encoding).

    Returns:
        List of :class:`PlacedComponent` with transformed pad coordinates.

    Raises:
        ValueError: If vector length does not match component count.
    """
    n = vector.num_components
    if n != len(components):
        raise ValueError(
            f"Vector encodes {n} components but {len(components)} component definitions provided"
        )

    result: list[PlacedComponent] = []
    for i, comp_def in enumerate(components):
        vals = vector.component_slice(i)
        x = float(vals[0])
        y = float(vals[1])
        rot_idx = int(round(float(vals[2]))) % 4
        side = int(round(float(vals[3])))
        rotation_deg = ROTATION_STEPS[rot_idx]

        # Transform pads
        transformed_pads = tuple(
            _transform_pad(pad, x, y, rotation_deg, side) for pad in comp_def.pads
        )

        result.append(
            PlacedComponent(
                reference=comp_def.reference,
                x=x,
                y=y,
                rotation=rotation_deg,
                side=side,
                pads=transformed_pads,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


def bounds(
    board: BoardOutline,
    components: Sequence[ComponentDef],
) -> PlacementBounds:
    """Compute per-dimension optimizer bounds for the placement vector.

    For each component:
    - x: ``[board.min_x + width/2, board.max_x - width/2]``
    - y: ``[board.min_y + height/2, board.max_y - height/2]``
    - rot: ``[0, 3]`` (discrete)
    - side: ``[0, 1]`` (discrete / binary)

    The x/y bounds account for component dimensions so the bounding box
    of each component stays within the board outline.

    Args:
        board: Board outline (axis-aligned bounding box).
        components: Component definitions.

    Returns:
        :class:`PlacementBounds` with lower, upper, and discrete_mask arrays.
    """
    n = len(components)
    total = n * FIELDS_PER_COMPONENT

    lower = np.empty(total, dtype=np.float64)
    upper = np.empty(total, dtype=np.float64)
    discrete_mask = np.zeros(total, dtype=np.bool_)

    for i, comp in enumerate(components):
        base = i * FIELDS_PER_COMPONENT
        half_w = comp.width / 2.0
        half_h = comp.height / 2.0

        # x bounds
        lower[base] = board.min_x + half_w
        upper[base] = board.max_x - half_w

        # y bounds
        lower[base + 1] = board.min_y + half_h
        upper[base + 1] = board.max_y - half_h

        # rot bounds (discrete: 0, 1, 2, 3)
        lower[base + 2] = 0.0
        upper[base + 2] = 3.0
        discrete_mask[base + 2] = True

        # side bounds (discrete: 0, 1)
        lower[base + 3] = 0.0
        upper[base + 3] = 1.0
        discrete_mask[base + 3] = True

    return PlacementBounds(lower=lower, upper=upper, discrete_mask=discrete_mask)


# ---------------------------------------------------------------------------
# Block-aware helpers
# ---------------------------------------------------------------------------


def _rotate_offset(dx: float, dy: float, rot_deg: float) -> tuple[float, float]:
    """Rotate an offset vector by *rot_deg* degrees counter-clockwise."""
    rot_idx = int(round(rot_deg / 90.0)) % 4
    if rot_idx == 0:
        return dx, dy
    elif rot_idx == 1:
        return -dy, dx
    elif rot_idx == 2:
        return -dx, -dy
    else:
        return dy, -dx


def _block_member_refs(block_groups: Sequence[BlockGroupDef]) -> frozenset[str]:
    """Return the set of all component references that belong to any block."""
    refs: set[str] = set()
    for bg in block_groups:
        for m in bg.members:
            refs.add(m.reference)
    return frozenset(refs)


def _split_free_and_block(
    components: Sequence[ComponentDef],
    block_groups: Sequence[BlockGroupDef],
) -> tuple[list[int], dict[str, int]]:
    """Return (free_indices, ref_to_comp_index) for the given components.

    ``free_indices`` lists component-array indices that are NOT in any block.
    ``ref_to_comp_index`` maps every component reference to its index in *components*.
    """
    ref_to_idx = {c.reference: i for i, c in enumerate(components)}
    blocked = _block_member_refs(block_groups)
    free_indices = [i for i, c in enumerate(components) if c.reference not in blocked]
    return free_indices, ref_to_idx


# ---------------------------------------------------------------------------
# Block-aware encode / decode
# ---------------------------------------------------------------------------


def encode_with_blocks(
    placements: Sequence[PlacedComponent],
    block_groups: Sequence[BlockGroupDef],
) -> PlacementVector:
    """Encode placements using reduced-dimensionality block representation.

    Free components get 4 fields each ``[x, y, rot, side]``.
    Each block gets 3 fields ``[bx, by, brot]``.

    The vector layout is::

        [free0_x, free0_y, free0_rot, free0_side,
         ...
         block0_bx, block0_by, block0_brot,
         block1_bx, block1_by, block1_brot,
         ...]

    Block origin and rotation are inferred from the first member's position
    and the block's relative offsets.

    Args:
        placements: Ordered sequence of placed components (all components,
            both free and block members).
        block_groups: Block group definitions.

    Returns:
        A reduced-dimensionality :class:`PlacementVector`.
    """
    pos_map: dict[str, PlacedComponent] = {p.reference: p for p in placements}
    blocked_refs = _block_member_refs(block_groups)

    # Free components in the order they appear in placements
    free = [p for p in placements if p.reference not in blocked_refs]
    n_free = len(free)
    n_blocks = len(block_groups)
    total = n_free * FIELDS_PER_COMPONENT + n_blocks * FIELDS_PER_BLOCK

    data = np.empty(total, dtype=np.float64)

    # Encode free components
    for i, p in enumerate(free):
        base = i * FIELDS_PER_COMPONENT
        rot_idx = int(round(p.rotation / 90.0)) % 4
        data[base] = p.x
        data[base + 1] = p.y
        data[base + 2] = float(rot_idx)
        data[base + 3] = float(p.side)

    # Encode blocks: infer block origin from first member
    block_offset = n_free * FIELDS_PER_COMPONENT
    for bi, bg in enumerate(block_groups):
        if not bg.members:
            data[block_offset] = 0.0
            data[block_offset + 1] = 0.0
            data[block_offset + 2] = 0.0
            block_offset += FIELDS_PER_BLOCK
            continue

        first = bg.members[0]
        placed = pos_map.get(first.reference)
        if placed is None:
            data[block_offset] = 0.0
            data[block_offset + 1] = 0.0
            data[block_offset + 2] = 0.0
            block_offset += FIELDS_PER_BLOCK
            continue

        # Infer block rotation from the first member
        block_rot_deg = (placed.rotation - first.rotation) % 360
        block_rot_idx = int(round(block_rot_deg / 90.0)) % 4
        block_rot = ROTATION_STEPS[block_rot_idx]

        # Infer block origin: placed_pos = origin + rotate(offset, block_rot)
        rdx, rdy = _rotate_offset(first.dx, first.dy, block_rot)
        bx = placed.x - rdx
        by = placed.y - rdy

        data[block_offset] = bx
        data[block_offset + 1] = by
        data[block_offset + 2] = float(block_rot_idx)
        block_offset += FIELDS_PER_BLOCK

    return PlacementVector(data=data)


def decode_with_blocks(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
    block_groups: Sequence[BlockGroupDef],
) -> list[PlacedComponent]:
    """Decode a block-aware placement vector to placed components.

    Returns one :class:`PlacedComponent` per entry in *components*, in the
    same order. Free-component positions come from the vector directly; block
    member positions are derived from the block origin/rotation and relative
    offsets.

    Args:
        vector: Reduced-dimensionality placement vector.
        components: Component definitions (all components, both free and block).
        block_groups: Block group definitions matching those used for encoding.

    Returns:
        List of :class:`PlacedComponent` with the same length and order as
        *components*.

    Raises:
        ValueError: If vector length does not match expected dimensionality.
    """
    blocked_refs = _block_member_refs(block_groups)
    free_indices, ref_to_idx = _split_free_and_block(components, block_groups)
    n_free = len(free_indices)
    n_blocks = len(block_groups)

    expected_len = n_free * FIELDS_PER_COMPONENT + n_blocks * FIELDS_PER_BLOCK
    if len(vector.data) != expected_len:
        raise ValueError(
            f"Vector length {len(vector.data)} does not match expected "
            f"{expected_len} (free={n_free}, blocks={n_blocks})"
        )

    comp_map = {c.reference: c for c in components}
    result_map: dict[str, PlacedComponent] = {}

    # Decode free components
    for fi, comp_idx in enumerate(free_indices):
        base = fi * FIELDS_PER_COMPONENT
        vals = vector.data[base : base + FIELDS_PER_COMPONENT]
        x = float(vals[0])
        y = float(vals[1])
        rot_idx = int(round(float(vals[2]))) % 4
        side = int(round(float(vals[3])))
        rotation_deg = ROTATION_STEPS[rot_idx]

        comp_def = components[comp_idx]
        transformed_pads = tuple(
            _transform_pad(pad, x, y, rotation_deg, side) for pad in comp_def.pads
        )
        result_map[comp_def.reference] = PlacedComponent(
            reference=comp_def.reference,
            x=x,
            y=y,
            rotation=rotation_deg,
            side=side,
            pads=transformed_pads,
        )

    # Decode block members
    block_offset = n_free * FIELDS_PER_COMPONENT
    for bg in block_groups:
        bx = float(vector.data[block_offset])
        by = float(vector.data[block_offset + 1])
        brot_idx = int(round(float(vector.data[block_offset + 2]))) % 4
        brot_deg = ROTATION_STEPS[brot_idx]
        block_offset += FIELDS_PER_BLOCK

        for m in bg.members:
            rdx, rdy = _rotate_offset(m.dx, m.dy, brot_deg)
            cx = bx + rdx
            cy = by + rdy
            crot = (brot_deg + m.rotation) % 360
            side = m.side

            comp_def = comp_map.get(m.reference)
            pads: tuple[TransformedPad, ...] = ()
            if comp_def is not None:
                pads = tuple(
                    _transform_pad(pad, cx, cy, crot, side) for pad in comp_def.pads
                )

            result_map[m.reference] = PlacedComponent(
                reference=m.reference,
                x=cx,
                y=cy,
                rotation=crot,
                side=side,
                pads=pads,
            )

    # Return in the same order as components
    return [result_map[c.reference] for c in components]


def bounds_with_blocks(
    board: BoardOutline,
    components: Sequence[ComponentDef],
    block_groups: Sequence[BlockGroupDef],
) -> PlacementBounds:
    """Compute per-dimension bounds for a block-aware placement vector.

    Free components get the same bounds as :func:`bounds`. Each block gets
    bounds ensuring that all its members stay within the board outline for
    any rotation.

    Args:
        board: Board outline.
        components: All component definitions.
        block_groups: Block group definitions.

    Returns:
        :class:`PlacementBounds` for the reduced-dimensionality vector.
    """
    blocked_refs = _block_member_refs(block_groups)
    free_indices, ref_to_idx = _split_free_and_block(components, block_groups)
    n_free = len(free_indices)
    n_blocks = len(block_groups)
    total = n_free * FIELDS_PER_COMPONENT + n_blocks * FIELDS_PER_BLOCK

    lower = np.empty(total, dtype=np.float64)
    upper = np.empty(total, dtype=np.float64)
    discrete_mask = np.zeros(total, dtype=np.bool_)

    # Free component bounds (same logic as bounds())
    for fi, comp_idx in enumerate(free_indices):
        comp = components[comp_idx]
        base = fi * FIELDS_PER_COMPONENT
        half_w = comp.width / 2.0
        half_h = comp.height / 2.0

        lower[base] = board.min_x + half_w
        upper[base] = board.max_x - half_w
        lower[base + 1] = board.min_y + half_h
        upper[base + 1] = board.max_y - half_h
        lower[base + 2] = 0.0
        upper[base + 2] = 3.0
        discrete_mask[base + 2] = True
        lower[base + 3] = 0.0
        upper[base + 3] = 1.0
        discrete_mask[base + 3] = True

    # Block bounds: block origin must keep all members within board for any rotation
    comp_map = {c.reference: c for c in components}
    block_base = n_free * FIELDS_PER_COMPONENT
    for bg in block_groups:
        # Compute worst-case radius: maximum distance any member corner can be
        # from the block origin across all 4 rotations
        max_reach = 0.0
        for m in bg.members:
            cdef = comp_map.get(m.reference)
            half_w = (cdef.width / 2.0) if cdef else 0.5
            half_h = (cdef.height / 2.0) if cdef else 0.5
            # For each rotation, member offset rotates; add component half-size
            for rot in ROTATION_STEPS:
                rdx, rdy = _rotate_offset(m.dx, m.dy, rot)
                reach_x = abs(rdx) + half_w
                reach_y = abs(rdy) + half_h
                max_reach = max(max_reach, reach_x, reach_y)

        lower[block_base] = board.min_x + max_reach
        upper[block_base] = board.max_x - max_reach
        lower[block_base + 1] = board.min_y + max_reach
        upper[block_base + 1] = board.max_y - max_reach
        lower[block_base + 2] = 0.0
        upper[block_base + 2] = 3.0
        discrete_mask[block_base + 2] = True
        block_base += FIELDS_PER_BLOCK

    return PlacementBounds(lower=lower, upper=upper, discrete_mask=discrete_mask)


# ---------------------------------------------------------------------------
# Block manipulation utilities
# ---------------------------------------------------------------------------


def move_block(
    vector: PlacementVector,
    block_index: int,
    dx: float,
    dy: float,
    n_free: int,
) -> PlacementVector:
    """Translate a block by (dx, dy) in a block-aware placement vector.

    Args:
        vector: Block-aware placement vector.
        block_index: Zero-based index of the block in the block_groups list.
        dx: Translation in X (mm).
        dy: Translation in Y (mm).
        n_free: Number of free (non-block) components.

    Returns:
        New :class:`PlacementVector` with the block moved.
    """
    new_data = vector.data.copy()
    base = n_free * FIELDS_PER_COMPONENT + block_index * FIELDS_PER_BLOCK
    new_data[base] += dx
    new_data[base + 1] += dy
    return PlacementVector(data=new_data)


def rotate_block(
    vector: PlacementVector,
    block_index: int,
    rotation_steps: int,
    n_free: int,
) -> PlacementVector:
    """Rotate a block by a number of 90-degree steps.

    Args:
        vector: Block-aware placement vector.
        block_index: Zero-based index of the block.
        rotation_steps: Number of 90-degree CCW steps to add (can be negative).
        n_free: Number of free (non-block) components.

    Returns:
        New :class:`PlacementVector` with the block rotated.
    """
    new_data = vector.data.copy()
    base = n_free * FIELDS_PER_COMPONENT + block_index * FIELDS_PER_BLOCK
    current = int(round(new_data[base + 2]))
    new_data[base + 2] = float((current + rotation_steps) % 4)
    return PlacementVector(data=new_data)


def swap_blocks(
    vector: PlacementVector,
    block_a: int,
    block_b: int,
    n_free: int,
) -> PlacementVector:
    """Swap the positions of two blocks (keeping their rotations).

    Args:
        vector: Block-aware placement vector.
        block_a: Zero-based index of the first block.
        block_b: Zero-based index of the second block.
        n_free: Number of free (non-block) components.

    Returns:
        New :class:`PlacementVector` with the two blocks' positions swapped.
    """
    new_data = vector.data.copy()
    base_a = n_free * FIELDS_PER_COMPONENT + block_a * FIELDS_PER_BLOCK
    base_b = n_free * FIELDS_PER_COMPONENT + block_b * FIELDS_PER_BLOCK

    # Swap x and y only (keep rotation)
    new_data[base_a], new_data[base_b] = new_data[base_b].copy(), new_data[base_a].copy()
    new_data[base_a + 1], new_data[base_b + 1] = (
        new_data[base_b + 1].copy(),
        new_data[base_a + 1].copy(),
    )
    return PlacementVector(data=new_data)
