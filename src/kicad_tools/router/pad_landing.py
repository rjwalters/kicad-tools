"""Pad centreline landing cost (Issue #6368).

The A* search seeds every cell of the start pad's (eroded) metal box and
accepts any cell of the end pad's metal box as a goal.  The off-grid tail
that ``reconstruct_path`` / ``_reconstruct_route`` draws between the pad
centre and that first/last grid cell is free: it carries no step or turn
cost.  An off-centre row therefore gets a free dogleg *inside* the pad,
while a centred landing that has to change rows outside the pad pays two
turns for the same jog.  The search consequently prefers the off-centre
row whenever the route continues on it, and the run leaves the pad tip
displaced towards a neighbour pin (board 05 U2: VCP at -0.125 mm,
SENSE_IN at +0.070 mm), consuming the neighbour's entry lane.

The landing cost charges exactly that skipped jog -- two turns plus one
straight step per cell of offset -- on the step that crosses the pad's tip
line (leaving the start pad, entering the end pad) inside the pad's lane
when either of its cells is more than half a cell from the pad centreline.
It is a soft cost only: every cell that is a legal seed or goal today stays
one, so a route that can only land off-centre still does.

``Pathfinder::landing_cost`` in ``cpp/src/pathfinder.cpp`` is the native
twin of :func:`landing_cost`; keep the two in step.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from .primitives import Pad

# Offsets up to half a cell are the nearest-row quantisation, not a choice.
# The small epsilon keeps a pad centred exactly between two rows free on
# both of them despite float noise.
LANDING_DEADBAND_CELLS = 0.5 + 1e-3

# A pad needs this aspect ratio before it has a long axis worth centring on.
# Near-square pads (0603 passives, THT lands) keep their old behaviour.
LONG_AXIS_MIN_ASPECT = 1.2

# ``long_axis`` encoding shared with the native ``PadBounds``.
AXIS_NONE = -1
AXIS_X = 0
AXIS_Y = 1


class LandingGeometry(NamedTuple):
    """The ``PadBounds`` landing fields, in grid units (Issue #6368)."""

    long_axis: int
    centre_gx: float
    centre_gy: float
    lane_half_cells: float
    tip_lo: int
    tip_hi: int


NO_LANDING = LandingGeometry(AXIS_NONE, 0.0, 0.0, 0.0, 0, 0)


def pad_long_axis(pad: Pad) -> int:
    """Return the board axis the pad is long along, or ``AXIS_NONE``.

    ``width``/``height`` are board-space for a pad whose residual rotation is
    a half-turn multiple (the parsers swap them for quarter turns); any other
    residual angle has no grid row to centre on.  Virtual pads (Steiner
    points, escape terminals) carry no pad tip.
    """
    if getattr(pad, "steiner_point", False) or getattr(pad, "escape_terminal", False):
        return AXIS_NONE
    width = float(getattr(pad, "width", 0.0) or 0.0)
    height = float(getattr(pad, "height", 0.0) or 0.0)
    if width <= 0.0 or height <= 0.0:
        return AXIS_NONE
    rot = float(getattr(pad, "rotation", 0.0) or 0.0) % 180.0
    if 1.0 <= rot <= 179.0:
        return AXIS_NONE
    if width >= height * LONG_AXIS_MIN_ASPECT:
        return AXIS_X
    if height >= width * LONG_AXIS_MIN_ASPECT:
        return AXIS_Y
    return AXIS_NONE


def pad_landing_geometry(pad: Pad, grid: Any) -> LandingGeometry:
    """Landing-cost inputs for ``pad`` on ``grid`` (cell ``g`` centred at ``g``)."""
    axis = pad_long_axis(pad)
    if axis == AXIS_NONE:
        return NO_LANDING
    res = grid.resolution
    cgx = (pad.x - grid.origin_x) / res
    cgy = (pad.y - grid.origin_y) / res
    if axis == AXIS_X:
        centre, half_len, short = cgx, pad.width / 2 / res, pad.height / res
    else:
        centre, half_len, short = cgy, pad.height / 2 / res, pad.width / res
    # Cells whose centre lies on the pad copper along the long axis; the
    # range ends are the pad's two tip lines.
    tip_lo = math.ceil(centre - half_len - 1e-6)
    tip_hi = math.floor(centre + half_len + 1e-6)
    # The lane: a run crossing the tip line within one pad short side of the
    # centreline overlaps the pad's half-width plus that of any trace narrow
    # enough to land on it, so it is leaving (or entering) this pad's lane,
    # not passing beside it.
    return LandingGeometry(axis, cgx, cgy, short, tip_lo, tip_hi)


def landing_cost(
    geom: LandingGeometry,
    ax: int,
    ay: int,
    bx: int,
    by: int,
    outward: bool,
    cost_turn: float,
    cost_straight: float,
) -> float:
    """Cost of the step ``(ax, ay) -> (bx, by)`` at a pad's tip line.

    The step is charged only when it crosses an end of the pad's copper
    along its long axis -- outward (on -> off the pad) for the start pad,
    inward for the end pad -- inside the pad's lane, and one of its two cells
    sits more than half a cell off the pad centreline.  Taking the larger
    offset charges a diagonal that jogs across the tip as well as a straight
    run on an off-centre row; keying on the copper tip line, not the eroded
    metal box edge, also charges a run that slips out of the box sideways
    and leaves along the pad.
    """
    if geom.long_axis == AXIS_X:
        a_along, b_along = ax, bx
        a_off, b_off = abs(ay - geom.centre_gy), abs(by - geom.centre_gy)
    elif geom.long_axis == AXIS_Y:
        a_along, b_along = ay, by
        a_off, b_off = abs(ax - geom.centre_gx), abs(bx - geom.centre_gx)
    else:
        return 0.0
    a_in = geom.tip_lo <= a_along <= geom.tip_hi
    b_in = geom.tip_lo <= b_along <= geom.tip_hi
    if outward:
        if not (a_in and not b_in):
            return 0.0
    elif not (b_in and not a_in):
        return 0.0
    if min(a_off, b_off) > geom.lane_half_cells + LANDING_DEADBAND_CELLS:
        return 0.0  # crossing the tip line beside the pad, not off it
    off = max(a_off, b_off)
    if off <= LANDING_DEADBAND_CELLS:
        return 0.0
    return 2.0 * cost_turn + cost_straight * off
