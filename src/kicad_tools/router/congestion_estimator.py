"""Pre-route congestion estimation using RUDY (Rectangular Uniform wire DensitY).

Provides a static congestion estimate from net geometry alone, before any
routing has taken place.  Each net's half-perimeter wirelength (HPWL) is
distributed uniformly across the coarse tiles its bounding box covers.
The resulting per-tile demand map identifies congestion hotspots that
should influence net ordering: nets passing through congested regions
benefit from being routed earlier when grid resources are plentiful.

Reference: Spindler & Johannes, "Fast and Accurate Routing Demand
Estimation for Efficient Routability-Driven Placement", DATE 2007.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .primitives import Pad


@dataclass
class TileGrid:
    """A uniform tile grid overlaid on the board area.

    Attributes:
        origin_x: X coordinate of the grid origin (mm).
        origin_y: Y coordinate of the grid origin (mm).
        tile_w: Width of each tile (mm).
        tile_h: Height of each tile (mm).
        cols: Number of tile columns.
        rows: Number of tile rows.
    """

    origin_x: float
    origin_y: float
    tile_w: float
    tile_h: float
    cols: int
    rows: int

    @classmethod
    def from_board(
        cls,
        origin_x: float,
        origin_y: float,
        width: float,
        height: float,
        target_tiles: int = 100,
    ) -> TileGrid:
        """Create a tile grid covering the board area.

        The grid chooses square-ish tiles so that the total tile count is
        close to *target_tiles* (default 100, i.e. ~10x10).

        Args:
            origin_x: Board origin X (mm).
            origin_y: Board origin Y (mm).
            width: Board width (mm).
            height: Board height (mm).
            target_tiles: Desired total number of tiles.

        Returns:
            A new ``TileGrid`` instance.
        """
        if width <= 0 or height <= 0:
            return cls(origin_x=origin_x, origin_y=origin_y,
                       tile_w=max(width, 1.0), tile_h=max(height, 1.0),
                       cols=1, rows=1)

        aspect = width / height
        cols = max(1, round(math.sqrt(target_tiles * aspect)))
        rows = max(1, round(target_tiles / cols))
        tile_w = width / cols
        tile_h = height / rows
        return cls(origin_x=origin_x, origin_y=origin_y,
                   tile_w=tile_w, tile_h=tile_h, cols=cols, rows=rows)

    def tile_at(self, x: float, y: float) -> tuple[int, int]:
        """Return (col, row) for the tile containing point (x, y).

        Points outside the grid are clamped to the nearest edge tile.
        """
        col = int((x - self.origin_x) / self.tile_w)
        row = int((y - self.origin_y) / self.tile_h)
        col = max(0, min(col, self.cols - 1))
        row = max(0, min(row, self.rows - 1))
        return (col, row)

    def tile_range(
        self, min_x: float, min_y: float, max_x: float, max_y: float,
    ) -> tuple[int, int, int, int]:
        """Return (col_lo, row_lo, col_hi, row_hi) covering a bounding box.

        Both endpoints are inclusive.
        """
        col_lo, row_lo = self.tile_at(min_x, min_y)
        col_hi, row_hi = self.tile_at(max_x, max_y)
        return (col_lo, row_lo, col_hi, row_hi)


@dataclass
class NetBBox:
    """Bounding box and HPWL for a single net."""

    net_id: int
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    pad_count: int

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def hpwl(self) -> float:
        """Half-perimeter wirelength in mm."""
        return self.width + self.height


@dataclass
class CongestionEstimator:
    """RUDY-based pre-route congestion estimator.

    Computes per-tile demand by distributing each net's HPWL uniformly
    across the tiles its bounding box covers.  The congestion score for
    a net is the average demand across the tiles in its bounding box,
    reflecting how contested the region is.

    Attributes:
        grid: The tile grid used for demand estimation.
        demand: 2-D demand array indexed ``[row][col]``.
        net_scores: Mapping of net ID to congestion score.
    """

    grid: TileGrid
    demand: list[list[float]] = field(default_factory=list)
    net_scores: dict[int, float] = field(default_factory=dict)

    @classmethod
    def from_nets(
        cls,
        nets: dict[int, list[tuple[str, str]]],
        pads: dict[tuple[str, str], Pad],
        board_origin_x: float,
        board_origin_y: float,
        board_width: float,
        board_height: float,
        target_tiles: int = 100,
        pour_net_ids: set[int] | None = None,
    ) -> CongestionEstimator:
        """Build a RUDY congestion estimate from net pad positions.

        Args:
            nets: Mapping of net ID to list of pad keys (ref, number).
            pads: Mapping of pad key to Pad object.
            board_origin_x: Board X origin (mm).
            board_origin_y: Board Y origin (mm).
            board_width: Board width (mm).
            board_height: Board height (mm).
            target_tiles: Target tile count for the grid (default 100).
            pour_net_ids: Set of net IDs that are pour nets (excluded from RUDY).

        Returns:
            A populated ``CongestionEstimator``.
        """
        tile_grid = TileGrid.from_board(
            board_origin_x, board_origin_y, board_width, board_height,
            target_tiles=target_tiles,
        )
        estimator = cls(grid=tile_grid)
        estimator.demand = [
            [0.0] * tile_grid.cols for _ in range(tile_grid.rows)
        ]

        pour_ids = pour_net_ids or set()

        # Phase 1: compute bounding boxes
        net_bboxes: list[NetBBox] = []
        for net_id, pad_keys in nets.items():
            if net_id == 0 or net_id in pour_ids:
                continue
            coords: list[tuple[float, float]] = []
            for key in pad_keys:
                pad = pads.get(key)
                if pad:
                    coords.append((pad.x, pad.y))
            if len(coords) < 2:
                # Single-pad or empty nets have zero HPWL
                estimator.net_scores[net_id] = 0.0
                continue

            min_x = min(c[0] for c in coords)
            max_x = max(c[0] for c in coords)
            min_y = min(c[1] for c in coords)
            max_y = max(c[1] for c in coords)
            net_bboxes.append(NetBBox(
                net_id=net_id,
                min_x=min_x, min_y=min_y,
                max_x=max_x, max_y=max_y,
                pad_count=len(coords),
            ))

        # Phase 2: distribute HPWL across tiles
        for bbox in net_bboxes:
            col_lo, row_lo, col_hi, row_hi = tile_grid.tile_range(
                bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y,
            )
            n_tiles = (col_hi - col_lo + 1) * (row_hi - row_lo + 1)
            if n_tiles <= 0:
                n_tiles = 1
            demand_per_tile = bbox.hpwl / n_tiles
            for r in range(row_lo, row_hi + 1):
                for c in range(col_lo, col_hi + 1):
                    estimator.demand[r][c] += demand_per_tile

        # Phase 3: compute per-net congestion score (average demand in bbox)
        for bbox in net_bboxes:
            col_lo, row_lo, col_hi, row_hi = tile_grid.tile_range(
                bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y,
            )
            n_tiles = (col_hi - col_lo + 1) * (row_hi - row_lo + 1)
            if n_tiles <= 0:
                estimator.net_scores[bbox.net_id] = 0.0
                continue
            total_demand = 0.0
            for r in range(row_lo, row_hi + 1):
                for c in range(col_lo, col_hi + 1):
                    total_demand += estimator.demand[r][c]
            estimator.net_scores[bbox.net_id] = total_demand / n_tiles

        return estimator

    def get_tile_demand(self, row: int, col: int) -> float:
        """Return the demand for a specific tile.

        Args:
            row: Tile row index.
            col: Tile column index.

        Returns:
            Demand value, or 0.0 if indices are out of range.
        """
        if 0 <= row < len(self.demand) and 0 <= col < len(self.demand[0]):
            return self.demand[row][col]
        return 0.0

    def get_net_congestion_score(self, net_id: int) -> float:
        """Return the congestion score for a net.

        Args:
            net_id: The net ID.

        Returns:
            Average RUDY demand across tiles in the net's bounding box,
            or 0.0 for unknown/excluded nets.
        """
        return self.net_scores.get(net_id, 0.0)

    def get_demand_grid(self) -> list[list[float]]:
        """Return the full demand grid for seeding global routing.

        Returns:
            2-D list of demand values indexed ``[row][col]``.
        """
        return self.demand

    def format_ascii_heatmap(self, max_width: int = 80) -> str:
        """Format the demand grid as an ASCII heatmap.

        Uses shading characters to represent demand intensity.

        Args:
            max_width: Maximum width in characters (default 80).

        Returns:
            Multi-line string with the heatmap.
        """
        if not self.demand or not self.demand[0]:
            return "(empty demand grid)"

        rows = len(self.demand)
        cols = len(self.demand[0])

        # Find max demand for normalisation
        max_demand = 0.0
        for row in self.demand:
            for val in row:
                if val > max_demand:
                    max_demand = val
        if max_demand == 0.0:
            max_demand = 1.0  # avoid division by zero

        # Determine cell width -- fit within max_width
        # Reserve 6 chars for row label "R00 |"
        label_width = 6
        available = max_width - label_width
        cell_w = max(1, available // cols)

        shades = " .:-=+*#@"

        lines: list[str] = []
        lines.append(f"RUDY Congestion Map ({cols}x{rows} tiles, max demand={max_demand:.2f})")
        lines.append(f"Legend: {' '.join(f'{shades[i]}={i}' for i in range(len(shades)))}")
        lines.append("")

        for r in range(rows):
            row_chars: list[str] = []
            for c in range(cols):
                normalised = self.demand[r][c] / max_demand
                idx = min(int(normalised * (len(shades) - 1)), len(shades) - 1)
                row_chars.append(shades[idx] * cell_w)
            label = f"R{r:02d} |"
            lines.append(f"{label}{''.join(row_chars)}")

        return "\n".join(lines)

    def format_json(self) -> dict:
        """Return the demand grid and net scores as a JSON-serialisable dict.

        Returns:
            Dict with keys ``grid``, ``net_scores``, ``tile_size``, ``dimensions``.
        """
        return {
            "dimensions": {
                "rows": self.grid.rows,
                "cols": self.grid.cols,
            },
            "tile_size": {
                "width_mm": round(self.grid.tile_w, 3),
                "height_mm": round(self.grid.tile_h, 3),
            },
            "grid": [
                [round(v, 4) for v in row] for row in self.demand
            ],
            "net_scores": {
                str(k): round(v, 4) for k, v in sorted(self.net_scores.items())
            },
        }
