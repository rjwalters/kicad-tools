"""One-ended hybrid trunk starts for diff-pair segments (issue #6446, phase 2d).

A segment the planar search and the 2a fan-via gateway both declined often has
ONE terminal that cannot couple at all: no legal via site anywhere near it (a
BGA ball field walled by GND balls, a QFN under a keep-out) and no corridor
for the fat pair envelope.  The other end is fine.  A **hybrid** keeps the
coupled body for the part of the segment where it can exist:

* the coupled **trunk** runs from the easy end to a *trunk start* outside the
  hard part's pad field;
* each net reaches that start through its own single-ended **leg** (vias
  allowed), so the uncoupled copper is a short, bounded stub instead of the
  whole segment.

This module is the engine-agnostic half -- pure geometry, no engine, no
obstacle model.  It proposes trunk starts on a setback ladder measured from the
hard part's pad-field box; routing them, verifying the legs and ranking the
results is the engine's job (``LatticePathfinder._route_pair_hybrid``).

The setback ladder is the idea behind KiCadRoutingTools' ``_setback_ladder`` /
``_find_open_positions`` and its hybrid-leg join (``_route_hybrid_leg``) (MIT,
pinned ``64df3f58``; see ``kct ecosystem show kicadroutingtools`` and
``docs/research/kicad-routing-tools-comparison.md`` section 3).  No code is
reused from it; this is an independent implementation on our own lattice.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .gateway import HEADINGS
from .geometry import Pt, dist

#: Rect ``(x0, y0, x1, y1)``.
Rect = tuple[float, float, float, float]

#: Search bound (mm) on one hybrid leg when the protocol declares no
#: per-terminal uncoupled budget.  An engine search cap -- NOT a check limit
#: and not cited to any standard: it only stops the retry from accepting a
#: leg that wanders the board.  It is sized from the boards that need it: a
#: BGA breakout under grown class keep-outs (06rf's U2) needs 14-22 mm legs to
#: reach a trunk start clear of the ball field, which is still far shorter
#: than the 80-110 mm whole-segment uncoupled fallback the hybrid replaces.
HYBRID_MAX_LEG_MM = 25.0

#: Most trunk start POSITIONS tried per trunk layer (each in up to two P/N
#: orderings).  Bounds the retry cost of a declined segment: one fat A* per
#: ordering.
MAX_TRUNK_STARTS = 8

#: Most fat trunk searches per hard end of one segment (all layers, starts and
#: orderings together) -- the retry's hard cost bound.
MAX_TRUNK_SEARCHES = 16

#: Consecutive trunk searches that EXHAUST the A* on one layer before the layer
#: is given up (the easy end itself is probably the blocker, and a farther
#: start will not help).
MAX_CONSECUTIVE_TRUNK_MISSES = 4

#: Setback ladder (mm) measured from the pad-field box edge, nearest first.
TRUNK_SETBACKS_MM: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)

#: Headings tried per rung, best aligned with the other end first.
HEADINGS_PER_RUNG = 2

#: A heading must make at least this cosine with the direction to the other
#: end to be offered (the trunk then leaves toward the easy end).
MIN_HEADING_ALIGNMENT = 0.2

#: The trunk must be at least this many pair pitches long to count as coupled.
MIN_TRUNK_PITCHES = 2.0


@dataclass(frozen=True)
class TrunkStart:
    """One proposed trunk start (geometry only).

    ``site_p`` / ``site_n`` are where the P and N legs end and the coupled
    body begins, ``site_gap`` apart across the heading (a single-trace
    spacing, wider than the pair pitch, so each leg is legal next to the
    partner's trunk).  ``heading`` indexes
    :data:`~kicad_tools.router.pairplan.gateway.HEADINGS`: the direction the
    trunk leaves the hard part.  ``crossed`` is True when P sits on the
    opposite side of the pad pair's own axis, i.e. the legs have to swap.
    """

    heading: int
    setback: float
    site_p: Pt
    site_n: Pt
    crossed: bool

    def describe(self) -> str:
        """``setback ..mm @..deg`` for the log."""
        return f"setback {self.setback:.1f} mm @{self.heading * 45}deg"


def _ray_exit(origin: Pt, d: Pt, box: Rect) -> float:
    """Distance along unit ``d`` from ``origin`` to where it leaves ``box``.

    ``0.0`` when ``origin`` is not inside the box.
    """
    x0, y0, x1, y1 = box
    if not (x0 <= origin[0] <= x1 and y0 <= origin[1] <= y1):
        return 0.0
    t = math.inf
    for o, v, lo, hi in ((origin[0], d[0], x0, x1), (origin[1], d[1], y0, y1)):
        if v > 1e-12:
            t = min(t, (hi - o) / v)
        elif v < -1e-12:
            t = min(t, (lo - o) / v)
    return 0.0 if math.isinf(t) else max(t, 0.0)


def propose_trunk_starts(
    pad_p: Pt,
    pad_n: Pt,
    toward: Pt,
    field: Rect,
    *,
    site_gap: float,
    max_leg_mm: float,
    setbacks: tuple[float, ...] = TRUNK_SETBACKS_MM,
    cap: int = MAX_TRUNK_STARTS,
) -> list[TrunkStart]:
    """Trunk starts for a hard end, nearest setback first, capped.

    ``pad_p`` / ``pad_n`` are the hard end's pad centres, ``toward`` the other
    end's midpoint and ``field`` the hard part's pad-field box (pad extents
    included).  Candidates are ordered by setback (so the longest trunk comes
    first), then by alignment of the heading with ``toward``; each position
    yields the *uncrossed* ordering (P on the side of the pad pair's own
    axis) and then the *crossed* one.  A position whose straight line from
    the pad pair exceeds ``max_leg_mm`` is dropped (the legs can only be
    longer).  At most ``cap`` positions are returned, in up to two orderings.
    """
    mid = ((pad_p[0] + pad_n[0]) / 2.0, (pad_p[1] + pad_n[1]) / 2.0)
    to = (toward[0] - mid[0], toward[1] - mid[1])
    to_len = math.hypot(*to)
    aligned = sorted(
        (
            (
                (HEADINGS[i][0] * to[0] + HEADINGS[i][1] * to[1]) / to_len
                if to_len > 1e-9
                else 1.0,
                i,
            )
            for i in range(len(HEADINGS))
        ),
        key=lambda t: (-t[0], t[1]),
    )
    headings = [i for cos, i in aligned if cos >= MIN_HEADING_ALIGNMENT][:HEADINGS_PER_RUNG]
    axis = (pad_p[0] - pad_n[0], pad_p[1] - pad_n[1])

    out: list[TrunkStart] = []
    seen: set[tuple[float, float]] = set()
    positions = 0
    for setback in setbacks:
        for hi in headings:
            h = HEADINGS[hi]
            exit_t = _ray_exit(mid, h, field)
            centre = (mid[0] + h[0] * (exit_t + setback), mid[1] + h[1] * (exit_t + setback))
            if dist(centre, mid) > max_leg_mm + 1e-9:
                continue
            key = (round(centre[0], 4), round(centre[1], 4))
            if key in seen:
                continue
            seen.add(key)
            # +perp points to P's side of the pad pair's own axis.
            perp = (-h[1], h[0])
            if perp[0] * axis[0] + perp[1] * axis[1] < 0.0:
                perp = (-perp[0], -perp[1])
            half = site_gap / 2.0
            plus = (centre[0] + perp[0] * half, centre[1] + perp[1] * half)
            minus = (centre[0] - perp[0] * half, centre[1] - perp[1] * half)
            out.append(TrunkStart(hi, setback, plus, minus, crossed=False))
            out.append(TrunkStart(hi, setback, minus, plus, crossed=True))
            positions += 1
            if positions >= cap:
                return out
    return out
