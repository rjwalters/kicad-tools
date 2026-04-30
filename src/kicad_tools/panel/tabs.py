"""Tab generation for panelized boards.

Computes tab positions between board instances and between boards and
frame, then generates Edge.Cuts line segments for the tab outlines.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import TabConfig


@dataclass
class Tab:
    """A single breakaway tab.

    Attributes:
        x: Tab center X in panel coordinates (mm).
        y: Tab center Y in panel coordinates (mm).
        width: Tab width along the board edge (mm).
        height: Tab height perpendicular to the edge (mm).
        orientation: "horizontal" (tab spans along X) or
            "vertical" (tab spans along Y).
    """

    x: float
    y: float
    width: float
    height: float
    orientation: str  # "horizontal" or "vertical"

    @property
    def min_x(self) -> float:
        """Left edge of tab bounding box."""
        if self.orientation == "horizontal":
            return self.x - self.width / 2.0
        return self.x - self.height / 2.0

    @property
    def max_x(self) -> float:
        """Right edge of tab bounding box."""
        if self.orientation == "horizontal":
            return self.x + self.width / 2.0
        return self.x + self.height / 2.0

    @property
    def min_y(self) -> float:
        """Top edge of tab bounding box."""
        if self.orientation == "horizontal":
            return self.y - self.height / 2.0
        return self.y - self.width / 2.0

    @property
    def max_y(self) -> float:
        """Bottom edge of tab bounding box."""
        if self.orientation == "horizontal":
            return self.y + self.height / 2.0
        return self.y + self.width / 2.0


def compute_tabs_between_boards(
    board_a_bounds: tuple[float, float, float, float],
    board_b_bounds: tuple[float, float, float, float],
    config: TabConfig,
    orientation: str,
) -> list[Tab]:
    """Compute tabs connecting two adjacent boards.

    Args:
        board_a_bounds: (min_x, min_y, max_x, max_y) of the first board.
        board_b_bounds: (min_x, min_y, max_x, max_y) of the second board.
        config: Tab configuration.
        orientation: "horizontal" if boards are side-by-side (tabs span
            the vertical gap), "vertical" if boards are stacked (tabs
            span the horizontal gap).

    Returns:
        List of Tab instances.
    """
    tabs: list[Tab] = []

    if orientation == "horizontal":
        # Boards are side by side (A left, B right)
        gap_x = board_b_bounds[0] - board_a_bounds[2]
        gap_center_x = board_a_bounds[2] + gap_x / 2.0
        edge_min_y = max(board_a_bounds[1], board_b_bounds[1])
        edge_max_y = min(board_a_bounds[3], board_b_bounds[3])
        span = edge_max_y - edge_min_y

        count = config.count
        if config.spacing is not None and config.spacing > 0:
            count = max(1, int(span / config.spacing))

        for i in range(count):
            y = edge_min_y + span * (i + 1) / (count + 1)
            tabs.append(
                Tab(
                    x=gap_center_x,
                    y=y,
                    width=gap_x,
                    height=config.width,
                    orientation="vertical",
                )
            )
    else:
        # Boards are stacked (A top, B bottom)
        gap_y = board_b_bounds[1] - board_a_bounds[3]
        gap_center_y = board_a_bounds[3] + gap_y / 2.0
        edge_min_x = max(board_a_bounds[0], board_b_bounds[0])
        edge_max_x = min(board_a_bounds[2], board_b_bounds[2])
        span = edge_max_x - edge_min_x

        count = config.count
        if config.spacing is not None and config.spacing > 0:
            count = max(1, int(span / config.spacing))

        for i in range(count):
            x = edge_min_x + span * (i + 1) / (count + 1)
            tabs.append(
                Tab(
                    x=x,
                    y=gap_center_y,
                    width=config.width,
                    height=gap_y,
                    orientation="horizontal",
                )
            )

    return tabs


def compute_tabs_to_frame(
    board_bounds: tuple[float, float, float, float],
    frame_inner_bounds: tuple[float, float, float, float],
    config: TabConfig,
) -> list[Tab]:
    """Compute tabs connecting a board to the surrounding frame.

    Creates tabs on all four edges where the board is adjacent to the
    frame.

    Args:
        board_bounds: (min_x, min_y, max_x, max_y) of the board.
        frame_inner_bounds: (min_x, min_y, max_x, max_y) of the inner
            frame edge.
        config: Tab configuration.

    Returns:
        List of Tab instances.
    """
    tabs: list[Tab] = []
    bx0, by0, bx1, by1 = board_bounds
    fx0, fy0, fx1, fy1 = frame_inner_bounds
    board_w = bx1 - bx0
    board_h = by1 - by0

    count = config.count
    if config.spacing is not None and config.spacing > 0:
        count_h = max(1, int(board_w / config.spacing))
        count_v = max(1, int(board_h / config.spacing))
    else:
        count_h = count
        count_v = count

    # Top edge (board top to frame top)
    if abs(by0 - fy0) > 0.01:
        gap = by0 - fy0
        for i in range(count_h):
            x = bx0 + board_w * (i + 1) / (count_h + 1)
            tabs.append(
                Tab(
                    x=x,
                    y=fy0 + gap / 2.0,
                    width=config.width,
                    height=gap,
                    orientation="horizontal",
                )
            )

    # Bottom edge
    if abs(fy1 - by1) > 0.01:
        gap = fy1 - by1
        for i in range(count_h):
            x = bx0 + board_w * (i + 1) / (count_h + 1)
            tabs.append(
                Tab(
                    x=x,
                    y=by1 + gap / 2.0,
                    width=config.width,
                    height=gap,
                    orientation="horizontal",
                )
            )

    # Left edge
    if abs(bx0 - fx0) > 0.01:
        gap = bx0 - fx0
        for i in range(count_v):
            y = by0 + board_h * (i + 1) / (count_v + 1)
            tabs.append(
                Tab(
                    x=fx0 + gap / 2.0,
                    y=y,
                    width=gap,
                    height=config.width,
                    orientation="vertical",
                )
            )

    # Right edge
    if abs(fx1 - bx1) > 0.01:
        gap = fx1 - bx1
        for i in range(count_v):
            y = by0 + board_h * (i + 1) / (count_v + 1)
            tabs.append(
                Tab(
                    x=bx1 + gap / 2.0,
                    y=y,
                    width=gap,
                    height=config.width,
                    orientation="vertical",
                )
            )

    return tabs
