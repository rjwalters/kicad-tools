"""Question-specific board images for agents (issue #6316).

``kct board-view`` and the ``board_view`` MCP tool both call
:func:`board_view`.  See :mod:`kicad_tools.boardview.model` for the coordinate
frames and the missing-link computation, and
:mod:`kicad_tools.boardview.render` for the drawing.
"""

from .model import (
    FRAME_BOARD,
    FRAME_SHEET,
    BoardGeometry,
    LinkReport,
    MissingLink,
    collect_geometry,
    missing_links,
)
from .render import (
    CAVEAT,
    MATPLOTLIB_HINT,
    MAX_VISION_API_PX,
    VIEWS,
    board_view,
    matplotlib_available,
)

__all__ = [
    "CAVEAT",
    "FRAME_BOARD",
    "FRAME_SHEET",
    "MATPLOTLIB_HINT",
    "MAX_VISION_API_PX",
    "VIEWS",
    "BoardGeometry",
    "LinkReport",
    "MissingLink",
    "board_view",
    "collect_geometry",
    "matplotlib_available",
    "missing_links",
]
