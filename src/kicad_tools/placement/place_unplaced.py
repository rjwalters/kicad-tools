"""Place unplaced components within the board outline.

Detects components at the origin or outside the board bounds and arranges
them in a grid within the board outline.  Used after ``sync-netlist`` to
move newly-added footprints into a reasonable initial layout.

Example::

    >>> from kicad_tools.placement.place_unplaced import place_unplaced
    >>> result = place_unplaced("board.kicad_pcb", dry_run=True)
    >>> for ref in result.placed_refs:
    ...     print(ref)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB, Footprint


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class PlaceUnplacedResult:
    """Result of placing unplaced components.

    Attributes:
        total_unplaced: Number of unplaced components detected.
        placed_count: Number of components successfully placed.
        overflow_count: Number of components that could not be placed
            because the available area was exhausted.
        placed_refs: References of placed components (in placement order).
        overflow_refs: References of components that could not be placed.
        board_bounds: Board bounding rectangle as ``(min_x, min_y, max_x, max_y)``.
        dry_run: Whether the run was a dry run (no PCB modifications).
    """

    total_unplaced: int = 0
    placed_count: int = 0
    overflow_count: int = 0
    placed_refs: list[str] = field(default_factory=list)
    overflow_refs: list[str] = field(default_factory=list)
    board_bounds: tuple[float, float, float, float] | None = None
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "total_unplaced": self.total_unplaced,
            "placed_count": self.placed_count,
            "overflow_count": self.overflow_count,
            "placed_refs": self.placed_refs,
            "overflow_refs": self.overflow_refs,
            "board_bounds": list(self.board_bounds) if self.board_bounds else None,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

ORIGIN_THRESHOLD = 0.1  # mm – matches validate/placement.py


def _get_board_bounds(pcb: PCB) -> tuple[float, float, float, float] | None:
    """Return board bounding rectangle in board-relative coordinates.

    Returns ``(min_x, min_y, max_x, max_y)`` or ``None`` when no
    ``Edge.Cuts`` outline is found.
    """
    outline = pcb.get_board_outline()
    if not outline:
        return None

    # get_board_outline() already returns board-relative coordinates.
    xs = [p[0] for p in outline]
    ys = [p[1] for p in outline]
    return (min(xs), min(ys), max(xs), max(ys))


def _is_at_origin_absolute(fp: Footprint, origin: tuple[float, float]) -> bool:
    """Check whether *fp* sits at the sheet-absolute origin.

    Footprint positions stored on the ``PCB`` object are board-relative.
    We convert back to sheet-absolute by adding ``origin`` and check
    proximity to ``(0, 0)``.
    """
    abs_x = fp.position[0] + origin[0]
    abs_y = fp.position[1] + origin[1]
    return abs(abs_x) < ORIGIN_THRESHOLD and abs(abs_y) < ORIGIN_THRESHOLD


def _is_outside_bounds(
    fp: Footprint,
    bounds: tuple[float, float, float, float],
) -> bool:
    """Check whether *fp* centre lies outside the board bounding rectangle.

    ``bounds`` is ``(min_x, min_y, max_x, max_y)`` in board-relative
    coordinates.
    """
    x, y = fp.position
    min_x, min_y, max_x, max_y = bounds
    return x < min_x or x > max_x or y < min_y or y > max_y


def _detect_unplaced(
    pcb: PCB,
    bounds: tuple[float, float, float, float],
) -> list[Footprint]:
    """Return footprints considered unplaced.

    A footprint is unplaced when it is either at the sheet-absolute origin
    or outside the board bounding rectangle.
    """
    origin = pcb.board_origin
    unplaced: list[Footprint] = []
    for fp in pcb.footprints:
        if _is_at_origin_absolute(fp, origin) or _is_outside_bounds(fp, bounds):
            unplaced.append(fp)
    return unplaced


# ---------------------------------------------------------------------------
# Footprint sizing helpers
# ---------------------------------------------------------------------------


def _footprint_size(fp: Footprint, default_margin: float = 0.5) -> tuple[float, float]:
    """Estimate footprint width and height from courtyard or pad extents.

    Checks courtyard graphics first; falls back to pad bounding box plus
    *default_margin* on each side.

    Returns ``(width, height)`` in mm.
    """
    # Try courtyard graphics
    cy_points: list[tuple[float, float]] = []
    for g in fp.graphics:
        layer = getattr(g, "layer", "")
        if "Courtyard" in layer:
            if hasattr(g, "start") and hasattr(g, "end"):
                cy_points.append(g.start)
                cy_points.append(g.end)

    if cy_points:
        xs = [p[0] for p in cy_points]
        ys = [p[1] for p in cy_points]
        return (max(xs) - min(xs), max(ys) - min(ys))

    # Fallback: pad bounding box + margin
    if fp.pads:
        xs: list[float] = []
        ys: list[float] = []
        for pad in fp.pads:
            px, py = pad.position
            hw, hh = pad.size[0] / 2, pad.size[1] / 2
            xs.extend([px - hw, px + hw])
            ys.extend([py - hh, py + hh])
        return (
            max(xs) - min(xs) + 2 * default_margin,
            max(ys) - min(ys) + 2 * default_margin,
        )

    # Ultimate fallback
    return (2.0, 2.0)


# ---------------------------------------------------------------------------
# Grid placement
# ---------------------------------------------------------------------------


@dataclass
class _GridCell:
    x: float
    y: float
    w: float
    h: float


def _build_grid(
    bounds: tuple[float, float, float, float],
    margin: float,
    spacing: float,
    cell_w: float,
    cell_h: float,
) -> list[_GridCell]:
    """Build a list of grid cells inside the placeable area."""
    min_x, min_y, max_x, max_y = bounds
    area_x = min_x + margin
    area_y = min_y + margin
    area_w = (max_x - min_x) - 2 * margin
    area_h = (max_y - min_y) - 2 * margin

    if area_w <= 0 or area_h <= 0:
        return []

    step_x = cell_w + spacing
    step_y = cell_h + spacing

    cells: list[_GridCell] = []
    y = area_y
    while y + cell_h <= area_y + area_h:
        x = area_x
        while x + cell_w <= area_x + area_w:
            cells.append(_GridCell(x=x, y=y, w=cell_w, h=cell_h))
            x += step_x
        y += step_y

    return cells


def _cell_occupied(
    cell: _GridCell,
    placed_positions: list[tuple[float, float, float, float]],
) -> bool:
    """Return ``True`` if *cell* overlaps any already-placed bounding box.

    Each item in *placed_positions* is ``(x, y, w, h)`` where ``(x, y)``
    is the centre.
    """
    cx = cell.x + cell.w / 2
    cy = cell.y + cell.h / 2
    for px, py, pw, ph in placed_positions:
        if (
            abs(cx - px) < (cell.w + pw) / 2
            and abs(cy - py) < (cell.h + ph) / 2
        ):
            return True
    return False


# ---------------------------------------------------------------------------
# Clustering helpers
# ---------------------------------------------------------------------------


def _cluster_footprints(
    footprints: list[Footprint],
    pcb: PCB,
) -> list[list[Footprint]]:
    """Group *footprints* by shared net connectivity.

    Components sharing at least one net are placed in the same cluster.
    Uses a simple union-find algorithm on net membership.
    """
    # Build a mapping from net number to footprint indices
    net_to_fps: dict[int, list[int]] = {}
    for idx, fp in enumerate(footprints):
        for pad in fp.pads:
            if pad.net_number > 0:
                net_to_fps.setdefault(pad.net_number, []).append(idx)

    # Union-find
    parent = list(range(len(footprints)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for indices in net_to_fps.values():
        for i in range(1, len(indices)):
            union(indices[0], indices[i])

    # Collect clusters
    clusters: dict[int, list[int]] = {}
    for idx in range(len(footprints)):
        root = find(idx)
        clusters.setdefault(root, []).append(idx)

    return [[footprints[i] for i in idxs] for idxs in clusters.values()]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def place_unplaced(
    pcb_path: str | Path,
    *,
    margin: float = 2.0,
    spacing: float = 2.0,
    cluster: bool = False,
    dry_run: bool = False,
    output_path: str | Path | None = None,
    quiet: bool = False,
) -> PlaceUnplacedResult:
    """Detect and grid-place unplaced components.

    Args:
        pcb_path: Path to ``.kicad_pcb`` file.
        margin: Inward margin from board edge in mm.
        spacing: Spacing between grid cells in mm.
        cluster: Group components by shared nets before placing.
        dry_run: When ``True`` report what would change without writing.
        output_path: Where to save the modified PCB (defaults to *pcb_path*).
        quiet: Suppress informational output.

    Returns:
        :class:`PlaceUnplacedResult` describing what was (or would be) done.
    """
    pcb_path = Path(pcb_path)
    pcb = PCB.load(str(pcb_path))

    # Determine board bounds
    bounds = _get_board_bounds(pcb)
    if bounds is None:
        raise ValueError(
            f"No Edge.Cuts board outline found in {pcb_path}. "
            "Cannot determine placement area."
        )

    # Detect unplaced components
    unplaced = _detect_unplaced(pcb, bounds)

    result = PlaceUnplacedResult(
        total_unplaced=len(unplaced),
        board_bounds=bounds,
        dry_run=dry_run,
    )

    if not unplaced:
        return result

    # Compute a representative cell size from the largest footprint dimension
    sizes = [_footprint_size(fp) for fp in unplaced]
    max_w = max(s[0] for s in sizes)
    max_h = max(s[1] for s in sizes)
    cell_w = max(max_w, 2.0)
    cell_h = max(max_h, 2.0)

    # Gather already-placed footprint positions (for overlap avoidance)
    placed_positions: list[tuple[float, float, float, float]] = []
    for fp in pcb.footprints:
        if fp in unplaced:
            continue
        fw, fh = _footprint_size(fp)
        placed_positions.append((fp.position[0], fp.position[1], fw, fh))

    # Build grid
    cells = _build_grid(bounds, margin, spacing, cell_w, cell_h)

    # Remove cells that overlap existing placements
    free_cells = [c for c in cells if not _cell_occupied(c, placed_positions)]

    # Optionally cluster
    if cluster:
        ordered: list[Footprint] = []
        for group in _cluster_footprints(unplaced, pcb):
            ordered.extend(group)
    else:
        ordered = list(unplaced)

    # Place components into free cells
    cell_idx = 0
    for fp in ordered:
        if cell_idx >= len(free_cells):
            result.overflow_refs.append(fp.reference)
            result.overflow_count += 1
            continue

        cell = free_cells[cell_idx]
        new_x = cell.x + cell.w / 2
        new_y = cell.y + cell.h / 2

        if not dry_run:
            pcb.update_footprint_position(fp.reference, new_x, new_y)

        result.placed_refs.append(fp.reference)
        result.placed_count += 1

        # Mark this position as occupied for future overlap checks
        fw, fh = _footprint_size(fp)
        placed_positions.append((new_x, new_y, fw, fh))
        cell_idx += 1

    # Save
    if not dry_run:
        save_path = Path(output_path) if output_path else pcb_path
        pcb.save(str(save_path))

    return result
