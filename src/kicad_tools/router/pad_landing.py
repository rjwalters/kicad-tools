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
straight step per cell of offset -- on a step that leaves the start pad or
reaches the end pad (see :func:`landing_cost`) with a cell more than one
grid cell from the pad centreline.
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

# A cell within one grid pitch of the centreline is on it, for the grid's
# purposes: the nearest row is up to half a cell off, and the row beside it
# up to one cell.  Charging those would only reshuffle routes for no gain
# (it reordered board 04's fan-out and split its +3.3V pour).  The small
# epsilon absorbs float noise.
LANDING_DEADBAND_CELLS = 1.0 + 1e-3

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
    box: tuple[int, int, int, int],
    ax: int,
    ay: int,
    bx: int,
    by: int,
    outward: bool,
    cost_turn: float,
    cost_straight: float,
) -> float:
    """Cost of the step ``(ax, ay) -> (bx, by)`` leaving or reaching a pad.

    ``box`` is the pad's metal box ``(gx1, gy1, gx2, gy2)``.  ``outward``
    selects the start pad (the step leaves it) over the end pad (the step
    reaches it).  The step is charged when one of its cells sits more than
    one grid cell off the pad centreline and it either crosses the pad's
    copper tip line inside the pad's lane, or crosses the metal box boundary
    while moving along the long axis.  The first catches a straight run on
    an off-centre row, a diagonal that jogs across the tip and a run that
    slips out of the box sideways at the tip column; the second catches a
    diagonal that crosses the centreline inside the pad.  A pure side step
    out of the box is not charged.
    """
    if geom.long_axis == AXIS_X:
        a_along, b_along = ax, bx
        a_off, b_off = abs(ay - geom.centre_gy), abs(by - geom.centre_gy)
    elif geom.long_axis == AXIS_Y:
        a_along, b_along = ay, by
        a_off, b_off = abs(ax - geom.centre_gx), abs(bx - geom.centre_gx)
    else:
        return 0.0
    off = max(a_off, b_off)
    if off <= LANDING_DEADBAND_CELLS:
        return 0.0
    a_tip = geom.tip_lo <= a_along <= geom.tip_hi
    b_tip = geom.tip_lo <= b_along <= geom.tip_hi
    crosses = (a_tip and not b_tip) if outward else (b_tip and not a_tip)
    crosses = crosses and min(a_off, b_off) <= geom.lane_half_cells + LANDING_DEADBAND_CELLS
    if not crosses and a_along != b_along:
        gx1, gy1, gx2, gy2 = box
        a_box = gx1 <= ax <= gx2 and gy1 <= ay <= gy2
        b_box = gx1 <= bx <= gx2 and gy1 <= by <= gy2
        crosses = (a_box and not b_box) if outward else (b_box and not a_box)
    if not crosses:
        return 0.0
    return 2.0 * cost_turn + cost_straight * off
