"""Clearance-aware 45-degree best-fit for the mesh router (issue #4268).

This is the one genuinely-new algorithm in the mesh vertical slice and the
biggest risk (the #3906 board-05 3-way-short is the standing proof that an
obstacle-*blind* octilinear fit ships a defect).  It generalises the
already-shipped ``SubGrid._try_candidates_with_clearance`` discipline
(``subgrid.py:1004-1030``) from "the pad-escape chord" to "each chord of the
funnel polyline":

  * split an off-angle chord into a 45-legal two-leg dogleg via
    :func:`kicad_tools.router.quantize.dogleg_points`, then
  * clearance-check EACH leg against the same obstacle model the straight
    chord used, and **reject the candidate if either leg collides** -- trying
    the other dogleg orientation, then subdividing to hug the geodesic more
    tightly, before finally failing the chord.

The returned polyline is guaranteed 45-legal (feeds the #3907 ``to_sexp``
choke without raising) AND clearance-clean leg-by-leg (never emits a bulge
into a keep-out).  A chord that cannot be made clear returns ``None`` -- the
route fails rather than shipping a short.
"""

from __future__ import annotations

from collections.abc import Callable

from ..quantize import dogleg_points, is_45_aligned
from .geometry import Pt

# Clearance predicate: True if a straight leg from a to b is obstacle-free.
ClearFn = Callable[[Pt, Pt], bool]

# Recursion cap on chord subdivision. Each level halves the dogleg bulge
# (bounded by min(|dx|,|dy|)); 8 levels shrink a full-board chord to <1um.
_MAX_SUBDIVISION = 8


def octilinear_fit(
    polyline: list[Pt], is_clear: ClearFn, *, max_subdivision: int = _MAX_SUBDIVISION
) -> list[Pt] | None:
    """Convert an arbitrary-angle geodesic into a clearance-clean 45-legal path.

    Returns the 45-legal vertex list (starting at ``polyline[0]``), or ``None``
    if any chord cannot be octilinearised without a leg entering an obstacle.
    """
    if len(polyline) < 2:
        return list(polyline)

    out: list[Pt] = [polyline[0]]
    for i in range(len(polyline) - 1):
        legs = _fit_chord(polyline[i], polyline[i + 1], is_clear, max_subdivision)
        if legs is None:
            return None
        out.extend(legs)
    return _dedupe(out)


def _fit_chord(a: Pt, b: Pt, is_clear: ClearFn, depth: int) -> list[Pt] | None:
    """Return the 45-legal points from ``a`` to ``b`` (EXCLUDING ``a``).

    Tries: straight (if already aligned) -> default dogleg -> flipped dogleg
    -> midpoint subdivision.  Every emitted leg is clearance-checked.
    """
    dx = b[0] - a[0]
    dy = b[1] - a[1]

    if is_45_aligned(dx, dy):
        # Already on a legal angle; only a clearance check remains.  A straight
        # aligned chord that collides cannot be repaired by doglegging (a
        # dogleg of an aligned chord is collinear), so this is a hard fail.
        return [b] if is_clear(a, b) else None

    # Off-angle: try both dogleg orientations (bulge on opposite sides).
    for axis_first in (False, True):
        pts = dogleg_points(a[0], a[1], b[0], b[1], axis_first=axis_first)
        if len(pts) != 3:
            continue
        mid = pts[1]
        if is_clear(a, mid) and is_clear(mid, b):
            return [mid, b]

    # Both doglegs bulge into an obstacle -> subdivide to hug the geodesic.
    if depth > 0:
        m = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        first = _fit_chord(a, m, is_clear, depth - 1)
        if first is not None:
            second = _fit_chord(m, b, is_clear, depth - 1)
            if second is not None:
                return first + second
    return None


def _dedupe(pts: list[Pt]) -> list[Pt]:
    """Drop consecutive duplicate vertices (zero-length legs)."""
    out: list[Pt] = []
    for p in pts:
        if not out or abs(out[-1][0] - p[0]) > 1e-9 or abs(out[-1][1] - p[1]) > 1e-9:
            out.append(p)
    return out
