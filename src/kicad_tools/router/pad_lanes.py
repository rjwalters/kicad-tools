"""Neighbour entry-lane soft cost for fine-pitch pad columns (Issue #6382).

On a fine-pitch column (board 05's U2: 1.575 x 0.4 mm pads at 0.65 mm
pitch) a wide net that leaves its own pad straight and then turns 45
degrees crosses the *straight-out lanes* of the neighbouring pins within
1-2 mm of their copper tips.  When those neighbours route later they find
their pocket sealed (GND_SENSE U2.10 and VM U2.11 on board 05, stranded by
PHASE_C's diagonal and via).  Nothing in the grid A* knows about a foreign
pad's lane: the #5508 access invariant only sees a 0.5 mm exit stub, and
the #3143 per-pad channel budgets only run in the dense-package escape
pre-phase, which the default ``--grid auto`` adaptive branch never reaches.

This module builds one soft-cost rectangle per long-axis pad of every
detected fine-pitch package:

* **Along the long axis** from the copper tip outward (away from the
  package body) by :data:`LANE_LENGTH_MM`.
* **Across** the pad's short side, extended to the midpoint of the gap to
  the nearest same-column neighbour (capped by :data:`LANE_MAX_GAP_FILL_MM`).
  Adjacent lanes therefore *tile* the column's outward strip without
  overlapping (half-open cell bounds), so a neighbour's own straight escape
  -- whose centreline sits half a pitch from either boundary -- is never
  charged for the next lane, and a foreign via cannot dodge the cost by
  centring itself in the 0.25 mm inter-pad gap while its 0.6 mm disc blocks
  both lanes (measured on board 05: PHASE_C's via at 1.735 mm out, centred
  between U2.10 and U2.11, with pad-wide lanes).
* ``layer = -1`` so a foreign *via* landing in the lane is charged on its
  destination layer too (a through via in the lane blocks the pad's
  layer no matter which layers it connects).
* ``source_net = pad.net``; :class:`CppPathfinder` drops budgets whose
  source net equals the routing net, so a pad is never charged for its
  own lane.

The rectangles are expressed as ``router_cpp.PadChannelBudget`` values and
handed to :meth:`CppPathfinder.set_pad_lane_budgets`, a list kept separate
from the #3143 escape-channel budgets so neither setter clobbers the other.
The cost is soft: it never forbids a cell, it only makes crossing a
neighbour's lane close to its tip more expensive than crossing it further
out.  The Python pathfinder ignores budgets (same gap as #3143).

Escape hatch: ``KCT_DISABLE_PAD_LANES=1`` installs no lanes, restoring the
pre-#6382 cost function byte-for-byte.  ``KCT_PAD_LANE_LENGTH_MM`` and
``KCT_PAD_LANE_PENALTY`` override the length and the per-cell penalty
factor (x ``cost_straight``) for calibration sweeps without a code change.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .primitives import Pad

#: Lane length beyond the copper tip, in mm.  About three 0.65 mm pitches:
#: the board 05 blockers sit 1.0 mm (PHASE_C diagonal over U2.10), 1.67 mm
#: (over U2.11) and 1.73 mm (PHASE_C's via) from the tips.  One pitch would
#: not reach them.
LANE_LENGTH_MM = 2.0

#: Largest amount (mm) a lane is widened past the pad's short side toward a
#: same-column neighbour.  Enough to close a fine-pitch gap; keeps a coarse
#: (1.27 mm) pitch lane from swallowing a whole routing channel.
LANE_MAX_GAP_FILL_MM = 0.4

#: Two pads are in the same column when their long-axis centre coordinates
#: agree to within this many mm.
_SAME_COLUMN_TOL_MM = 0.05

#: Per-cell penalty as a multiple of ``rules.cost_straight``.  Same
#: calibration as the #3201 escape-channel budgets.
LANE_PENALTY_FACTOR = 0.5

#: Minimum long/short side ratio for a pad to have a defined long axis.
LANE_MIN_ASPECT = 1.2

#: Residual rotation tolerance (degrees) for a pad to count as cardinal.
#: ``Pad.width``/``height`` are already axis-swapped for ~90/270 rotations,
#: so only the residual matters.
_CARDINAL_TOLERANCE_DEG = 1.0

DISABLE_ENV = "KCT_DISABLE_PAD_LANES"
LENGTH_ENV = "KCT_PAD_LANE_LENGTH_MM"
PENALTY_ENV = "KCT_PAD_LANE_PENALTY"


@dataclass(frozen=True)
class PadLane:
    """A lane rectangle in world coordinates (mm).

    ``x1 <= x2`` and ``y1 <= y2``.  ``axis`` is ``"x"`` when the lane runs
    horizontally (pad long side along X) and ``direction`` is ``+1`` / ``-1``
    for the outward sense along that axis.
    """

    x1: float
    y1: float
    x2: float
    y2: float
    net: int
    pad_key: tuple[str, str]
    axis: str
    direction: int


def pad_lanes_disabled() -> bool:
    """True when ``KCT_DISABLE_PAD_LANES`` is set to a non-empty, non-zero value."""
    value = os.environ.get(DISABLE_ENV, "")
    return value.strip() not in ("", "0")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0.0:
        return default
    return value


def lane_length_mm() -> float:
    """Effective lane length (honours ``KCT_PAD_LANE_LENGTH_MM``)."""
    return _env_float(LENGTH_ENV, LANE_LENGTH_MM)


def lane_penalty_factor() -> float:
    """Effective penalty factor (honours ``KCT_PAD_LANE_PENALTY``)."""
    return _env_float(PENALTY_ENV, LANE_PENALTY_FACTOR)


def _is_cardinal(rotation: float) -> bool:
    residual = math.fmod(abs(rotation), 90.0)
    return min(residual, 90.0 - residual) <= _CARDINAL_TOLERANCE_DEG


def _lane_eligible(pad: Pad) -> bool:
    if pad.net == 0:
        return False  # NC pad: nothing ever routes out of it (#3201).
    if pad.steiner_point or pad.escape_terminal:
        return False  # Virtual / escape-terminal pads are not physical lands.
    if pad.through_hole:
        return False
    if pad.width <= 0.0 or pad.height <= 0.0:
        return False
    if not _is_cardinal(pad.rotation):
        return False
    long_side = max(pad.width, pad.height)
    short_side = min(pad.width, pad.height)
    return long_side >= LANE_MIN_ASPECT * short_side


def pad_lane(
    pad: Pad,
    body_center: tuple[float, float],
    length: float = LANE_LENGTH_MM,
    half_across: float | None = None,
) -> PadLane | None:
    """Return ``pad``'s outward lane, or ``None`` when it has none.

    The lane runs along the pad's long axis, from the copper tip on the
    side away from ``body_center`` outward by ``length``.  Across, it spans
    ``half_across`` either side of the pad centreline (default: half the
    pad's short side, i.e. exactly pad-wide).  Square, NC, virtual,
    escape-terminal, through-hole and non-cardinal pads get no lane, and
    neither does a pad whose long axis straddles the body centre (e.g. an
    exposed pad).
    """
    if length <= 0.0 or not _lane_eligible(pad):
        return None
    cx, cy = body_center
    if pad.width >= pad.height:
        hw = pad.height * 0.5 if half_across is None else half_across
        offset = pad.x - cx
        if abs(offset) < pad.width * 0.5:
            return None
        direction = 1 if offset > 0 else -1
        tip = pad.x + direction * pad.width * 0.5
        far = tip + direction * length
        return PadLane(
            x1=min(tip, far),
            y1=pad.y - hw,
            x2=max(tip, far),
            y2=pad.y + hw,
            net=pad.net,
            pad_key=pad.key,
            axis="x",
            direction=direction,
        )
    hw = pad.width * 0.5 if half_across is None else half_across
    offset = pad.y - cy
    if abs(offset) < pad.height * 0.5:
        return None
    direction = 1 if offset > 0 else -1
    tip = pad.y + direction * pad.height * 0.5
    far = tip + direction * length
    return PadLane(
        x1=pad.x - hw,
        y1=min(tip, far),
        x2=pad.x + hw,
        y2=max(tip, far),
        net=pad.net,
        pad_key=pad.key,
        axis="y",
        direction=direction,
    )


def compute_pad_lanes(
    pads: Iterable[Pad],
    length: float = LANE_LENGTH_MM,
) -> list[PadLane]:
    """Compute lanes for every eligible pad of one package.

    ``pads`` are the pads of a single component; the body centre is the
    centre of their bounding box.  Each lane is widened from the pad's
    short side to the midpoint of the gap to its nearest same-column
    neighbour (see :func:`lane_half_across`).
    """
    cluster = list(pads)
    if not cluster:
        return []
    xs = [p.x for p in cluster]
    ys = [p.y for p in cluster]
    center = ((min(xs) + max(xs)) * 0.5, (min(ys) + max(ys)) * 0.5)
    lanes: list[PadLane] = []
    for pad in cluster:
        lane = pad_lane(pad, center, length, lane_half_across(pad, cluster))
        if lane is not None:
            lanes.append(lane)
    return lanes


def lane_half_across(pad: Pad, cluster: Iterable[Pad]) -> float:
    """Half-width of ``pad``'s lane across its long axis.

    Half the pad's short side, extended to half the centre distance to the
    nearest neighbour in the same column (same long-axis orientation and
    long-axis centre coordinate), capped at ``short/2 +``
    :data:`LANE_MAX_GAP_FILL_MM`.  A pad with no same-column neighbour keeps
    a pad-wide lane.
    """
    horizontal = pad.width >= pad.height
    short_half = (pad.height if horizontal else pad.width) * 0.5
    nearest: float | None = None
    for other in cluster:
        if other is pad or (other.width >= other.height) != horizontal:
            continue
        if horizontal:
            same_column = abs(other.x - pad.x) <= _SAME_COLUMN_TOL_MM
            gap = abs(other.y - pad.y)
        else:
            same_column = abs(other.y - pad.y) <= _SAME_COLUMN_TOL_MM
            gap = abs(other.x - pad.x)
        if same_column and gap > 1e-6 and (nearest is None or gap < nearest):
            nearest = gap
    if nearest is None:
        return short_half
    return min(max(short_half, nearest * 0.5), short_half + LANE_MAX_GAP_FILL_MM)


def lane_grid_rect(lane: PadLane, grid: Any) -> tuple[int, int, int, int] | None:
    """Grid cells whose centres lie in the half-open box ``[x1, x2) x [y1, y2)``.

    Returned bounds are inclusive.  Returns ``None`` when the lane covers no
    cell centre or falls entirely off the grid.  Half-open containment means
    two lanes that share a boundary (adjacent pads of a column, which tile
    the strip) never both claim a cell centred exactly on it.
    """
    res = float(grid.resolution)
    ox = float(grid.origin_x)
    oy = float(grid.origin_y)
    eps = 1e-6
    gx1 = math.ceil((lane.x1 - ox) / res - eps)
    gx2 = math.ceil((lane.x2 - ox) / res - eps) - 1
    gy1 = math.ceil((lane.y1 - oy) / res - eps)
    gy2 = math.ceil((lane.y2 - oy) / res - eps) - 1
    gx1 = max(0, gx1)
    gy1 = max(0, gy1)
    gx2 = min(int(grid.cols) - 1, gx2)
    gy2 = min(int(grid.rows) - 1, gy2)
    if gx1 > gx2 or gy1 > gy2:
        return None
    return gx1, gy1, gx2, gy2


def collect_fine_pitch_lanes(
    pads: Iterable[Pad],
    regions: Iterable[Any],
    length: float = LANE_LENGTH_MM,
) -> list[PadLane]:
    """Lanes for every pad belonging to a detected fine-pitch region.

    ``regions`` are :class:`~kicad_tools.router.fine_pitch_escape.FinePitchRegion`
    instances (anything with ``pad_refs``).  Output order follows the
    region order and then the pad input order, so it is deterministic.
    """
    pad_list = list(pads)
    lanes: list[PadLane] = []
    for region in regions:
        refs = getattr(region, "pad_refs", None)
        if not refs:
            continue
        cluster = [p for p in pad_list if p.key in refs]
        lanes.extend(compute_pad_lanes(cluster, length))
    return lanes


def build_pad_lane_budgets(
    grid: Any,
    pads: Iterable[Pad],
    regions: Iterable[Any],
    cost_straight: float,
    *,
    length: float | None = None,
    penalty_factor: float | None = None,
) -> list:
    """Build ``router_cpp.PadChannelBudget`` lanes for the fine-pitch pads.

    Returns an empty list when the C++ backend is unavailable, when
    ``KCT_DISABLE_PAD_LANES`` is set, when no region is detected, or when
    the length or penalty resolves to zero.
    """
    if pad_lanes_disabled():
        return []
    try:
        from .cpp_backend import router_cpp as _router_cpp
    except ImportError:
        return []
    if _router_cpp is None:
        return []

    regions = list(regions)
    if not regions:
        return []
    if length is None:
        length = lane_length_mm()
    if penalty_factor is None:
        penalty_factor = lane_penalty_factor()
    penalty = float(cost_straight) * float(penalty_factor)
    if length <= 0.0 or penalty <= 0.0:
        return []

    budgets: list = []
    for lane in collect_fine_pitch_lanes(pads, regions, length):
        rect = lane_grid_rect(lane, grid)
        if rect is None:
            continue
        budget = _router_cpp.PadChannelBudget()
        budget.gx1, budget.gy1, budget.gx2, budget.gy2 = (int(v) for v in rect)
        budget.layer = -1
        budget.capacity = 0
        budget.overflow_penalty = penalty
        budget.origin_pad_ref_hash = 0
        budget.source_net = int(lane.net)
        budgets.append(budget)
    return budgets
