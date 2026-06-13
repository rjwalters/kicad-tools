"""Mousebite and V-cut generation for panel separation.

Generates NPTH drill holes along tab center lines (mousebites) or
full-width score lines (V-cuts) for board separation after assembly.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass

from kicad_tools.sexp.builders import fmt, gr_line_node, uuid_node
from kicad_tools.sexp.parser import SExp

from .config import MousebiteConfig, VCutConfig
from .tabs import Tab


@dataclass
class MousebiteHole:
    """A single NPTH hole in a mousebite perforation line.

    Attributes:
        x: Hole center X in panel coordinates (mm).
        y: Hole center Y in panel coordinates (mm).
        diameter: Drill diameter in mm.
    """

    x: float
    y: float
    diameter: float


@dataclass
class VCutLine:
    """A single V-cut score line.

    Attributes:
        start_x: Start X coordinate (mm).
        start_y: Start Y coordinate (mm).
        end_x: End X coordinate (mm).
        end_y: End Y coordinate (mm).
    """

    start_x: float
    start_y: float
    end_x: float
    end_y: float


def generate_mousebite_holes(
    tab: Tab,
    config: MousebiteConfig,
) -> list[MousebiteHole]:
    """Generate NPTH holes along the center line of a tab.

    Holes are placed along the center of the tab perpendicular to the
    board edge, evenly spaced at the configured interval.

    Args:
        tab: The tab to perforate.
        config: Mousebite configuration.

    Returns:
        List of MousebiteHole instances.
    """
    holes: list[MousebiteHole] = []

    if tab.orientation == "horizontal":
        # Tab spans horizontally -- holes along horizontal center line
        line_y = tab.y
        line_start = tab.min_x + config.offset
        line_end = tab.max_x - config.offset
    else:
        # Tab spans vertically -- holes along vertical center line
        line_y = None  # type: ignore[assignment]
        line_start = tab.min_y + config.offset
        line_end = tab.max_y - config.offset

    length = line_end - line_start
    if length <= 0:
        return holes

    n_holes = max(1, int(math.floor(length / config.spacing)) + 1)
    actual_spacing = length / max(1, n_holes - 1) if n_holes > 1 else 0

    for i in range(n_holes):
        pos = line_start + i * actual_spacing
        if tab.orientation == "horizontal":
            holes.append(MousebiteHole(x=pos, y=line_y, diameter=config.diameter))
        else:
            holes.append(MousebiteHole(x=tab.x, y=pos, diameter=config.diameter))

    return holes


def mousebite_hole_to_sexp(hole: MousebiteHole) -> SExp:
    """Convert a MousebiteHole to a KiCad S-expression footprint.

    Generates a board-level ``footprint`` node containing a single NPTH
    pad. This matches how KiKit and other panelizers represent
    mousebite holes in KiCad files.

    Args:
        hole: The mousebite hole to convert.

    Returns:
        An SExp node representing the footprint.
    """
    hole_uuid = str(uuid.uuid4())
    pad_uuid = str(uuid.uuid4())

    # Build NPTH pad node
    pad = SExp.list(
        "pad",
        "",
        "np_thru_hole",
        "circle",
        SExp.list("at", 0, 0),
        SExp.list("size", fmt(hole.diameter), fmt(hole.diameter)),
        SExp.list("drill", fmt(hole.diameter)),
        SExp.list("layers", "*.Cu", "*.Mask"),
        uuid_node(pad_uuid),
    )

    # Build footprint wrapping the pad
    fp = SExp.list(
        "footprint",
        "Panel:Mousebite",
        SExp.list("layer", "F.Cu"),
        uuid_node(hole_uuid),
        SExp.list("at", fmt(hole.x), fmt(hole.y)),
        SExp.list("attr", "board_only", "exclude_from_pos_files", "exclude_from_bom"),
        pad,
    )

    return fp


def generate_vcut_lines(
    panel_bounds: tuple[float, float, float, float],
    cut_positions: list[float],
    orientation: str,
    config: VCutConfig,
) -> list[VCutLine]:
    """Generate V-cut score lines across the full panel dimension.

    V-cuts run the entire width or height of the panel.

    Args:
        panel_bounds: (min_x, min_y, max_x, max_y) of the panel.
        cut_positions: List of Y positions (for horizontal cuts) or
            X positions (for vertical cuts).
        orientation: "horizontal" or "vertical".
        config: V-cut configuration.

    Returns:
        List of VCutLine instances.
    """
    lines: list[VCutLine] = []
    min_x, min_y, max_x, max_y = panel_bounds

    for pos in cut_positions:
        if orientation == "horizontal":
            lines.append(
                VCutLine(
                    start_x=min_x,
                    start_y=pos,
                    end_x=max_x,
                    end_y=pos,
                )
            )
        else:
            lines.append(
                VCutLine(
                    start_x=pos,
                    start_y=min_y,
                    end_x=pos,
                    end_y=max_y,
                )
            )

    return lines


def vcut_line_to_sexp(line: VCutLine, config: VCutConfig) -> SExp:
    """Convert a VCutLine to a KiCad gr_line S-expression.

    Args:
        line: The V-cut line to convert.
        config: V-cut configuration for line width and layer.

    Returns:
        An SExp ``gr_line`` node.
    """
    return gr_line_node(
        line.start_x,
        line.start_y,
        line.end_x,
        line.end_y,
        layer=config.layer,
        width=config.line_width,
        uuid_str=str(uuid.uuid4()),
    )
