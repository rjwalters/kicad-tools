"""Geometric constraint detectors for placement optimization.

Provides hard-constraint detectors that operate on :class:`PlacedComponent`
objects from the placement vector module.  Both functions return ``0.0`` for
valid (feasible) placements, making them suitable as penalty terms in a cost
function.

Two detectors are provided:

* **compute_overlap** -- total pairwise overlap area (mm^2) between
  component bounding boxes.  Components on opposite board sides (front vs
  back) are excluded from overlap checks.

* **compute_boundary_violation** -- total out-of-bounds area (mm^2) for
  components extending beyond the board outline.

Both functions account for component rotation when computing axis-aligned
bounding boxes (AABBs).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from .cost import BoardOutline
from .vector import ComponentDef, PlacedComponent

if TYPE_CHECKING:
    from kicad_tools.pcb.board_geometry import BoardGeometry

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _aabb(
    comp: PlacedComponent,
    comp_def: ComponentDef,
) -> tuple[float, float, float, float]:
    """Compute the axis-aligned bounding box for a placed component.

    Rotation is accounted for: 90 and 270 degree rotations swap the
    width and height of the component.

    Args:
        comp: Placed component with position and rotation.
        comp_def: Static component definition with width/height.

    Returns:
        Tuple of ``(min_x, min_y, max_x, max_y)`` in board coordinates.
    """
    rot_idx = int(round(comp.rotation / 90.0)) % 4

    if rot_idx in (1, 3):
        # 90 or 270 degrees: width and height swap
        half_w = comp_def.height / 2.0
        half_h = comp_def.width / 2.0
    else:
        half_w = comp_def.width / 2.0
        half_h = comp_def.height / 2.0

    return (
        comp.x - half_w,
        comp.y - half_h,
        comp.x + half_w,
        comp.y + half_h,
    )


def _overlap_area(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> float:
    """Compute the overlap area of two axis-aligned bounding boxes.

    Args:
        box_a: ``(min_x, min_y, max_x, max_y)``
        box_b: ``(min_x, min_y, max_x, max_y)``

    Returns:
        Overlap area in mm^2.  Zero if boxes do not overlap.
    """
    x_overlap = max(0.0, min(box_a[2], box_b[2]) - max(box_a[0], box_b[0]))
    y_overlap = max(0.0, min(box_a[3], box_b[3]) - max(box_a[1], box_b[1]))
    return x_overlap * y_overlap


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_overlap(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
) -> float:
    """Compute total pairwise overlap area between component bounding boxes.

    Uses axis-aligned bounding boxes (AABBs) derived from each component's
    width, height, position, and rotation.  Components on opposite board
    sides (front=0, back=1) do not overlap each other.

    Complexity is O(N^2) pairwise, which is acceptable for boards with
    fewer than ~100 components.

    Args:
        placements: Placed components with positions, rotations, and sides.
        component_defs: Static component definitions (same order as
            *placements*).

    Returns:
        Total overlap area in mm^2.  Returns ``0.0`` when no components
        overlap.

    Raises:
        ValueError: If *placements* and *component_defs* have different
            lengths.
    """
    n = len(placements)
    if n != len(component_defs):
        raise ValueError(f"placements has {n} items but component_defs has {len(component_defs)}")

    if n < 2:
        return 0.0

    # Pre-compute AABBs and sides
    boxes: list[tuple[float, float, float, float]] = []
    sides: list[int] = []
    for comp, comp_def in zip(placements, component_defs, strict=True):
        boxes.append(_aabb(comp, comp_def))
        sides.append(comp.side)

    total = 0.0
    for i in range(n):
        for j in range(i + 1, n):
            # Components on different sides cannot overlap
            if sides[i] != sides[j]:
                continue
            total += _overlap_area(boxes[i], boxes[j])

    return total


def compute_boundary_violation(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
    board: BoardOutline,
) -> float:
    """Compute total out-of-bounds area for components beyond the board edge.

    For each component whose AABB extends beyond the board outline, the
    area of the out-of-bounds region is accumulated.  A component fully
    inside the board contributes zero.

    Each edge is checked independently.  A component hanging off a corner
    contributes the L-shaped out-of-bounds area (not double-counted --
    the corner rectangle is counted once via the product of per-edge
    violations for each independent rectangle region).

    Args:
        placements: Placed components with positions, rotations, and sides.
        component_defs: Static component definitions (same order as
            *placements*).
        board: Rectangular board outline.

    Returns:
        Total out-of-bounds area in mm^2.  Returns ``0.0`` when all
        components are fully within the board.

    Raises:
        ValueError: If *placements* and *component_defs* have different
            lengths.
    """
    n = len(placements)
    if n != len(component_defs):
        raise ValueError(f"placements has {n} items but component_defs has {len(component_defs)}")

    total = 0.0

    for comp, comp_def in zip(placements, component_defs, strict=True):
        box_min_x, box_min_y, box_max_x, box_max_y = _aabb(comp, comp_def)

        # Component AABB dimensions
        comp_w = box_max_x - box_min_x
        comp_h = box_max_y - box_min_y

        # Clamp the component AABB to the board extents to find the
        # portion that is inside the board.
        inside_min_x = max(box_min_x, board.min_x)
        inside_min_y = max(box_min_y, board.min_y)
        inside_max_x = min(box_max_x, board.max_x)
        inside_max_y = min(box_max_y, board.max_y)

        inside_w = max(0.0, inside_max_x - inside_min_x)
        inside_h = max(0.0, inside_max_y - inside_min_y)

        inside_area = inside_w * inside_h
        total_area = comp_w * comp_h

        # Out-of-bounds area = total component area - area inside board
        violation = total_area - inside_area
        if violation > 0.0:
            total += violation

    return total


def extract_board_outline(pcb: Any) -> BoardOutline:
    """Extract board outline from Edge.Cuts graphic lines.

    Edge.Cuts coordinates are in sheet-absolute space, but footprint
    positions on ``SchemaPCB`` are board-relative (the origin is already
    subtracted).  We must convert the outline to the same board-relative
    coordinate system so the optimizer bounds match component positions.

    Args:
        pcb: Parsed PCB schema object with ``graphic_lines``,
            ``board_origin``, and ``footprints`` attributes.

    Returns:
        A :class:`BoardOutline` representing the bounding rectangle
        of the Edge.Cuts outline, or a fallback based on footprint
        positions / default dimensions.
    """
    xs: list[float] = []
    ys: list[float] = []

    for line in pcb.graphic_lines:
        if line.layer == "Edge.Cuts":
            xs.extend([line.start[0], line.end[0]])
            ys.extend([line.start[1], line.end[1]])

    if xs and ys:
        # Convert from sheet-absolute to board-relative coordinates.
        ox, oy = pcb.board_origin
        xs = [x - ox for x in xs]
        ys = [y - oy for y in ys]
        return BoardOutline(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))

    # Fallback: use footprint bounding box with margin
    for fp in pcb.footprints:
        xs.append(fp.position[0])
        ys.append(fp.position[1])

    if xs and ys:
        margin = 10.0
        return BoardOutline(
            min_x=min(xs) - margin,
            min_y=min(ys) - margin,
            max_x=max(xs) + margin,
            max_y=max(ys) + margin,
        )

    # Default fallback
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=100.0, max_y=100.0)


def compute_boundary_violation_shapely(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
    board_geometry: BoardGeometry,
) -> float:
    """Compute total out-of-bounds area using Shapely polygon containment.

    Like :func:`compute_boundary_violation`, but uses a Shapely-backed
    :class:`~kicad_tools.pcb.board_geometry.BoardGeometry` for accurate
    non-rectangular board shapes.

    Args:
        placements: Placed components with positions, rotations, and sides.
        component_defs: Static component definitions (same order as
            *placements*).
        board_geometry: Shapely-based board geometry.

    Returns:
        Total out-of-bounds area in mm^2.  Returns ``0.0`` when all
        components are fully within the board.

    Raises:
        ValueError: If *placements* and *component_defs* have different
            lengths.
    """
    n = len(placements)
    if n != len(component_defs):
        raise ValueError(f"placements has {n} items but component_defs has {len(component_defs)}")

    total = 0.0

    for comp, comp_def in zip(placements, component_defs, strict=True):
        box_min_x, box_min_y, box_max_x, box_max_y = _aabb(comp, comp_def)
        violation = board_geometry.compute_boundary_violation(
            box_min_x, box_min_y, box_max_x, box_max_y
        )
        if violation > 0.0:
            total += violation

    return total
