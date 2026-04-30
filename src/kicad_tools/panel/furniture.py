"""Panel furniture: tooling holes and fiducial marks.

Generates NPTH tooling holes and SMD fiducial pads on the panel frame
for pick-and-place alignment and mechanical fixturing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from kicad_tools.sexp.builders import fmt, uuid_node
from kicad_tools.sexp.parser import SExp

from .config import FiducialConfig, ToolingHoleConfig


@dataclass
class ToolingHole:
    """A tooling hole position.

    Attributes:
        x: Hole center X in panel coordinates (mm).
        y: Hole center Y in panel coordinates (mm).
        diameter: Drill diameter in mm.
    """

    x: float
    y: float
    diameter: float


@dataclass
class Fiducial:
    """A fiducial mark position.

    Attributes:
        x: Fiducial center X in panel coordinates (mm).
        y: Fiducial center Y in panel coordinates (mm).
        diameter: Copper pad diameter in mm.
        mask_margin: Solder mask opening margin in mm.
    """

    x: float
    y: float
    diameter: float
    mask_margin: float


def compute_tooling_holes(
    panel_bounds: tuple[float, float, float, float],
    config: ToolingHoleConfig,
) -> list[ToolingHole]:
    """Compute tooling hole positions on the panel frame.

    Places holes at corners of the panel according to the configured
    pattern (3-hole or 4-hole).

    Args:
        panel_bounds: (min_x, min_y, max_x, max_y) of the panel
            including frame.
        config: Tooling hole configuration.

    Returns:
        List of ToolingHole instances.
    """
    min_x, min_y, max_x, max_y = panel_bounds
    offset = config.offset
    holes: list[ToolingHole] = []

    # Bottom-left
    holes.append(ToolingHole(
        x=min_x + offset,
        y=max_y - offset,
        diameter=config.diameter,
    ))

    # Bottom-right
    holes.append(ToolingHole(
        x=max_x - offset,
        y=max_y - offset,
        diameter=config.diameter,
    ))

    # Top-left
    holes.append(ToolingHole(
        x=min_x + offset,
        y=min_y + offset,
        diameter=config.diameter,
    ))

    if config.pattern >= 4:
        # Top-right
        holes.append(ToolingHole(
            x=max_x - offset,
            y=min_y + offset,
            diameter=config.diameter,
        ))

    return holes


def tooling_hole_to_sexp(hole: ToolingHole) -> SExp:
    """Convert a ToolingHole to a KiCad footprint S-expression.

    Args:
        hole: The tooling hole.

    Returns:
        An SExp ``footprint`` node with an NPTH pad.
    """
    hole_uuid = str(uuid.uuid4())
    pad_uuid = str(uuid.uuid4())

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

    fp = SExp.list(
        "footprint",
        "Panel:ToolingHole",
        SExp.list("layer", "F.Cu"),
        uuid_node(hole_uuid),
        SExp.list("at", fmt(hole.x), fmt(hole.y)),
        SExp.list("attr", "board_only", "exclude_from_pos_files", "exclude_from_bom"),
        pad,
    )

    return fp


def compute_fiducials(
    panel_bounds: tuple[float, float, float, float],
    config: FiducialConfig,
) -> list[Fiducial]:
    """Compute fiducial mark positions on the panel frame.

    Places three fiducials in an L-pattern (bottom-left, bottom-right,
    top-left) for pick-and-place alignment.

    Args:
        panel_bounds: (min_x, min_y, max_x, max_y) of the panel
            including frame.
        config: Fiducial configuration.

    Returns:
        List of Fiducial instances.
    """
    min_x, min_y, max_x, max_y = panel_bounds
    offset = config.offset
    fiducials: list[Fiducial] = []

    # Bottom-left
    fiducials.append(Fiducial(
        x=min_x + offset,
        y=max_y - offset,
        diameter=config.diameter,
        mask_margin=config.mask_margin,
    ))

    # Bottom-right
    fiducials.append(Fiducial(
        x=max_x - offset,
        y=max_y - offset,
        diameter=config.diameter,
        mask_margin=config.mask_margin,
    ))

    # Top-left
    fiducials.append(Fiducial(
        x=min_x + offset,
        y=min_y + offset,
        diameter=config.diameter,
        mask_margin=config.mask_margin,
    ))

    return fiducials


def fiducial_to_sexp(fiducial: Fiducial) -> SExp:
    """Convert a Fiducial to a KiCad footprint S-expression.

    Generates a footprint with an SMD copper pad and appropriate
    solder mask opening for optical alignment.

    Args:
        fiducial: The fiducial mark.

    Returns:
        An SExp ``footprint`` node with an SMD fiducial pad.
    """
    fid_uuid = str(uuid.uuid4())
    pad_uuid = str(uuid.uuid4())

    pad = SExp.list(
        "pad",
        "1",
        "smd",
        "circle",
        SExp.list("at", 0, 0),
        SExp.list("size", fmt(fiducial.diameter), fmt(fiducial.diameter)),
        SExp.list("layers", "F.Cu", "F.Mask"),
        SExp.list("solder_mask_margin", fmt(fiducial.mask_margin)),
        uuid_node(pad_uuid),
    )

    fp = SExp.list(
        "footprint",
        "Panel:Fiducial",
        SExp.list("layer", "F.Cu"),
        uuid_node(fid_uuid),
        SExp.list("at", fmt(fiducial.x), fmt(fiducial.y)),
        SExp.list("attr", "board_only", "exclude_from_pos_files", "exclude_from_bom"),
        pad,
    )

    return fp
