"""Board outline wrapper for panelization.

Wraps :class:`~kicad_tools.pcb.board_geometry.BoardGeometry` to provide
panel-specific geometric operations -- tab placement computation,
partition line intersection, and outline boolean merging.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely

if TYPE_CHECKING:
    from shapely.geometry import Polygon as ShapelyPolygon


def _require_shapely() -> None:
    """Raise early if Shapely is not available."""
    if not has_shapely():
        raise ImportError(
            "Shapely is required for panelization.  "
            "Install it with:  pip install kicad-tools[geometry]"
        )


class Substrate:
    """Board outline wrapper for panelization geometry.

    Wraps a :class:`BoardGeometry` and provides helpers for computing
    tab positions along board edges.

    Attributes:
        geometry: The underlying board geometry.
    """

    def __init__(self, geometry: BoardGeometry) -> None:
        _require_shapely()
        self.geometry = geometry

    @classmethod
    def from_pcb(cls, pcb: object) -> Substrate:
        """Build a Substrate from a parsed PCB.

        Args:
            pcb: A parsed PCB instance (schema.pcb.PCB).
        """
        geom = BoardGeometry.from_pcb(pcb)  # type: ignore[arg-type]
        return cls(geom)

    @classmethod
    def from_bounds(
        cls, min_x: float, min_y: float, max_x: float, max_y: float
    ) -> Substrate:
        """Build a rectangular Substrate from bounding-box limits."""
        geom = BoardGeometry.from_bounds(min_x, min_y, max_x, max_y)
        return cls(geom)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return (min_x, min_y, max_x, max_y) bounding box."""
        return self.geometry.bounds

    @property
    def width(self) -> float:
        """Board width in mm."""
        b = self.bounds
        return b[2] - b[0]

    @property
    def height(self) -> float:
        """Board height in mm."""
        b = self.bounds
        return b[3] - b[1]

    def tab_positions_on_edge(
        self,
        edge: str,
        count: int,
        board_offset_x: float = 0.0,
        board_offset_y: float = 0.0,
    ) -> list[tuple[float, float]]:
        """Compute evenly-spaced tab center positions along a board edge.

        Args:
            edge: One of "top", "bottom", "left", "right".
            count: Number of tabs.
            board_offset_x: X offset of the board instance in panel coords.
            board_offset_y: Y offset of the board instance in panel coords.

        Returns:
            List of (x, y) tab center positions in panel coordinates.
        """
        min_x, min_y, max_x, max_y = self.bounds
        positions: list[tuple[float, float]] = []

        if edge in ("top", "bottom"):
            y = min_y if edge == "top" else max_y
            span = max_x - min_x
            for i in range(count):
                x = min_x + span * (i + 1) / (count + 1)
                positions.append(
                    (x + board_offset_x, y + board_offset_y)
                )
        elif edge in ("left", "right"):
            x = min_x if edge == "left" else max_x
            span = max_y - min_y
            for i in range(count):
                y = min_y + span * (i + 1) / (count + 1)
                positions.append(
                    (x + board_offset_x, y + board_offset_y)
                )

        return positions

    def make_tab_polygon(
        self,
        center_x: float,
        center_y: float,
        width: float,
        height: float,
    ) -> BoardGeometry:
        """Create a rectangular tab polygon centered at (center_x, center_y).

        Args:
            center_x: Tab center X in panel coordinates.
            center_y: Tab center Y in panel coordinates.
            width: Tab width (along the edge) in mm.
            height: Tab height (perpendicular to edge) in mm.

        Returns:
            A BoardGeometry representing the tab rectangle.
        """
        half_w = width / 2.0
        half_h = height / 2.0
        return BoardGeometry.from_bounds(
            center_x - half_w,
            center_y - half_h,
            center_x + half_w,
            center_y + half_h,
        )
