"""PlacementVector type and encode/decode for optimization algorithms.

Encodes a complete board placement as a flat numeric array suitable for
optimization algorithms. Each component occupies 4 entries:
    [x, y, rot, side]

where:
    x, y   -- continuous position in mm (within board bounds)
    rot    -- discrete rotation index {0, 1, 2, 3} => {0, 90, 180, 270} degrees
    side   -- binary {0, 1} => {front, back}

Usage:
    vector = encode(placements)
    placements = decode(vector, component_defs)
    bnd = bounds(board, component_defs)
"""

from __future__ import annotations

from dataclasses import dataclass
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
