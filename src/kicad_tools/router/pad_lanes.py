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


# ---------------------------------------------------------------------------
# Escape fences (Issue #6761)
# ---------------------------------------------------------------------------
#
# The soft lane cost above shapes *where* a net crosses a neighbour's lane,
# but it is one more term in a sum: on board 05 PHASE_C still left U2.9 with
# a 45-degree F.Cu diagonal across U2.10's and U2.11's lanes and dropped its
# 0.8 mm via at their far end, leaving one exit pocket for two pads (GND_SENSE
# U2.10 or VM U2.11 stranded, run to run).  Pricing the via harder only moved
# the blocking via (#6422, #6426, #6415).
#
# An escape fence is the hard form of the same lane, scoped to the one place it
# is unambiguous: on a column whose pitch is below a via footprint of its nets
# (:func:`tight_fence_lanes`), a connection that *starts or ends on a pad of
# that column* may not put its trace centreline inside another-net lane of that
# same column (dilated by its own half width + clearance + the narrowest
# foreign trace's half width), and may not drop a via whose disc would reach
# such a lane's centreline -- within the lane band, and for vias also within
# one via footprint past the lane end.  The net therefore leaves its own pad
# straight out of the column (or changes layer inside its own lane) before it
# turns, and every neighbour keeps its own straight exit.
#
# Nets that do not terminate on the column are untouched, and so is a search
# the fences make impossible: :class:`~kicad_tools.router.cpp_backend.CppPathfinder`
# retries once without fences, so a fence can reshape a route but never lose a
# connection the unfenced search would find.  ``KCT_DISABLE_ESCAPE_FENCES=1``
# restores the pre-#6761 search byte-for-byte.

ESCAPE_FENCE_DISABLE_ENV = "KCT_DISABLE_ESCAPE_FENCES"

#: ``PadChannelBudget.layer`` value of a via-only fence (mirrors the C++
#: ``PAD_VIA_FENCE_LAYER``).
PAD_VIA_FENCE_LAYER = -2

#: Two pads share a column when their long-axis centre coordinates agree to
#: within this many mm (rounded to this many decimals in the column key).
_COLUMN_KEY_DECIMALS = 2


def escape_fences_disabled() -> bool:
    """True when ``KCT_DISABLE_ESCAPE_FENCES`` is set to a non-empty, non-zero value."""
    value = os.environ.get(ESCAPE_FENCE_DISABLE_ENV, "")
    return value.strip() not in ("", "0")


@dataclass(frozen=True)
class FenceLane:
    """One fine-pitch lane as the escape fences see it.

    ``column`` identifies the pad column (package, axis, outward direction
    and the pads' shared long-axis centre coordinate); ``layer`` is the grid
    layer index of the pad's copper.
    """

    column: tuple[str, str, int, float]
    lane: PadLane
    layer: int


def lane_column(lane: PadLane, pad: Pad) -> tuple[str, str, int, float]:
    """The column key of ``lane`` (``pad`` is the lane's own pad)."""
    coord = pad.x if lane.axis == "x" else pad.y
    return (
        str(lane.pad_key[0]),
        lane.axis,
        int(lane.direction),
        round(coord, _COLUMN_KEY_DECIMALS),
    )


def collect_fence_lanes(
    pads: Iterable[Pad],
    regions: Iterable[Any],
    layer_index: Any,
    length: float = LANE_LENGTH_MM,
) -> list[FenceLane]:
    """Every fine-pitch lane with its column key and pad layer index.

    ``layer_index`` maps a pad to its grid layer index (the autorouter passes
    ``grid.layer_to_index(pad.layer.value)``).
    """
    pad_list = list(pads)
    by_key = {p.key: p for p in pad_list}
    out: list[FenceLane] = []
    for lane in collect_fine_pitch_lanes(pad_list, regions, length):
        pad = by_key.get(lane.pad_key)
        if pad is None:
            continue
        out.append(FenceLane(lane_column(lane, pad), lane, int(layer_index(pad))))
    return out


def column_pitch(fence_lanes: Iterable[FenceLane]) -> float | None:
    """Smallest centre-to-centre spacing of the lanes of one column, or ``None``."""
    centres = sorted(
        ((fl.lane.y1 + fl.lane.y2) if fl.lane.axis == "x" else (fl.lane.x1 + fl.lane.x2)) * 0.5
        for fl in fence_lanes
    )
    gaps = [b - a for a, b in zip(centres, centres[1:], strict=False) if b - a > 1e-6]
    return min(gaps) if gaps else None


def tight_fence_lanes(
    fence_lanes: Iterable[FenceLane],
    via_diameter: Any,
    via_clearance: float,
) -> list[FenceLane]:
    """Keep the lanes of columns too tight for a via of their own nets.

    A column qualifies when its pitch is below the largest ``via_diameter(net)
    + via_clearance`` among its lanes' nets -- a lane narrower than a via
    footprint, so a layer change or a turn inside the band takes room a
    neighbour needs for its own exit.  Board 05: U2 (0.65 mm pitch, 0.6/0.8 mm
    vias) is fenced, U1 (0.8 mm pitch, 0.6 mm vias) is not.  Single-pad
    columns never qualify.
    """
    by_column: dict[tuple[str, str, int, float], list[FenceLane]] = {}
    for fl in fence_lanes:
        by_column.setdefault(fl.column, []).append(fl)
    kept: list[FenceLane] = []
    for lanes in by_column.values():
        pitch = column_pitch(lanes)
        if pitch is None:
            continue
        footprint = max(float(via_diameter(fl.lane.net)) for fl in lanes) + float(via_clearance)
        if pitch < footprint - 1e-9:
            kept.extend(lanes)
    return kept


def _cells_within(center: float, band: float, origin: float, res: float) -> tuple[int, int]:
    """Inclusive index range of cells whose centre is strictly within ``band`` of ``center``.

    Cell ``k``'s centre is ``origin + k * res`` (the grid convention
    :func:`lane_grid_rect` uses).  The range is empty (``lo > hi``) when no
    centre qualifies.
    """
    eps = 1e-9
    lo = math.floor((center - band - origin) / res + eps) + 1
    hi = math.ceil((center + band - origin) / res - eps) - 1
    return lo, hi


def escape_fence_rects(
    fence_lanes: Iterable[FenceLane],
    terminal_keys: Iterable[Any],
    net: int,
    grid: Any,
    *,
    trace_band_mm: float,
    via_band_mm: float,
) -> list[tuple[tuple[int, int, int, int], int, int]]:
    """Fence rectangles for one connection, as ``(rect, layer, source_net)``.

    ``terminal_keys`` are the connection's start and end pad keys.  For every
    column one of them belongs to, each lane of that column owned by another
    net yields:

    * a **trace fence** on the lane's pad layer -- the lane's along-axis cells
      by the cells whose centre lies strictly within ``trace_band_mm`` of the
      lane centreline -- and
    * a **via fence** (``layer == PAD_VIA_FENCE_LAYER``) -- the cells strictly
      within ``via_band_mm`` of the centreline, along the lane and one
      ``via_band_mm`` past its far end.

    ``trace_band_mm`` should be the routing net's half width + clearance + the
    narrowest foreign half width, ``via_band_mm`` its via radius + via
    clearance + the same foreign half width: then a cell outside the fence
    leaves a minimum-width straight exit along the neighbour's centreline.
    Lanes of ``net`` itself (including same-net neighbours) are never fenced.
    Rect bounds are inclusive grid indices, clipped to the grid.
    """
    lanes = list(fence_lanes)
    by_pad = {fl.lane.pad_key: fl.column for fl in lanes}
    columns = {by_pad[k] for k in terminal_keys if k in by_pad}
    if not columns:
        return []
    res = float(grid.resolution)
    ox = float(grid.origin_x)
    oy = float(grid.origin_y)
    cols = int(grid.cols)
    rows = int(grid.rows)

    def clip(gx1: int, gy1: int, gx2: int, gy2: int) -> tuple[int, int, int, int] | None:
        gx1, gy1 = max(0, gx1), max(0, gy1)
        gx2, gy2 = min(cols - 1, gx2), min(rows - 1, gy2)
        if gx1 > gx2 or gy1 > gy2:
            return None
        return gx1, gy1, gx2, gy2

    out: list[tuple[tuple[int, int, int, int], int, int]] = []
    for fl in lanes:
        lane = fl.lane
        if fl.column not in columns or lane.net == net:
            continue
        rect = lane_grid_rect(lane, grid)
        if rect is None:
            continue
        gx1, gy1, gx2, gy2 = rect
        if lane.axis == "x":
            centre = (lane.y1 + lane.y2) * 0.5
            t_lo, t_hi = _cells_within(centre, trace_band_mm, oy, res)
            v_lo, v_hi = _cells_within(centre, via_band_mm, oy, res)
            if lane.direction > 0:
                far = _cells_within(lane.x2, via_band_mm, ox, res)[1]
                v_along = (gx1, max(gx2, far))
            else:
                far = _cells_within(lane.x1, via_band_mm, ox, res)[0]
                v_along = (min(gx1, far), gx2)
            trace = clip(gx1, t_lo, gx2, t_hi)
            via = clip(v_along[0], v_lo, v_along[1], v_hi)
        else:
            centre = (lane.x1 + lane.x2) * 0.5
            t_lo, t_hi = _cells_within(centre, trace_band_mm, ox, res)
            v_lo, v_hi = _cells_within(centre, via_band_mm, ox, res)
            if lane.direction > 0:
                far = _cells_within(lane.y2, via_band_mm, oy, res)[1]
                v_along = (gy1, max(gy2, far))
            else:
                far = _cells_within(lane.y1, via_band_mm, oy, res)[0]
                v_along = (min(gy1, far), gy2)
            trace = clip(t_lo, gy1, t_hi, gy2)
            via = clip(v_lo, v_along[0], v_hi, v_along[1])
        if trace is not None:
            out.append((trace, fl.layer, int(lane.net)))
        if via is not None:
            out.append((via, PAD_VIA_FENCE_LAYER, int(lane.net)))
    return out


def build_escape_fence_budgets(
    fence_lanes: Iterable[FenceLane],
    terminal_keys: Iterable[Any],
    net: int,
    grid: Any,
    *,
    trace_band_mm: float,
    via_band_mm: float,
) -> list:
    """:func:`escape_fence_rects` as hard ``router_cpp.PadChannelBudget`` values.

    A trace fence carries an infinite ``overflow_penalty`` on its layer (the
    C++ A* refuses an infinitely priced cell); a via fence uses
    ``layer == PAD_VIA_FENCE_LAYER``.  Empty when the C++ backend is missing.
    """
    try:
        from .cpp_backend import router_cpp as _router_cpp
    except ImportError:
        return []
    if _router_cpp is None:
        return []
    budgets: list = []
    for rect, layer, source_net in escape_fence_rects(
        fence_lanes,
        terminal_keys,
        net,
        grid,
        trace_band_mm=trace_band_mm,
        via_band_mm=via_band_mm,
    ):
        budget = _router_cpp.PadChannelBudget()
        budget.gx1, budget.gy1, budget.gx2, budget.gy2 = rect
        budget.layer = int(layer)
        budget.capacity = 0
        budget.overflow_penalty = math.inf
        budget.origin_pad_ref_hash = 0
        budget.source_net = int(source_net)
        budgets.append(budget)
    return budgets
