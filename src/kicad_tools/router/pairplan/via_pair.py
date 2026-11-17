"""Mid-trunk via pairs for coupled diff-pair runs (issue #7932, epic #6441 P3).

A **via pair** lets a coupled centreline change layer as ONE move: the pair
runs straight along a heading ``h`` on layer ``L`` to a centre point ``c``,
drops through two through vias placed symmetrically about the centreline,
perpendicular to ``h``, and carries on straight along ``h`` on layer ``L'``.
P's via sits on P's side of the travel direction, so the move never swaps
polarity (crossover / swap vias stay out of scope, #3508), and the two legs
jog out to their vias by the same amount, so the move adds no skew.

This module is the engine-free half -- pure geometry.  The engine supplies
legality (via clearance, fat-envelope checks, exact leg verification) and
decides where the move is offered.  Like the rest of
:mod:`kicad_tools.router.pairplan`, it imports no routing engine.

Geometry of one leg at a via pair (P on the left, ``+``):

* the leg arrives on ``L`` at offset ``+pitch/2`` from the centreline;
* ``jog`` before ``c`` it turns 45 degrees outward and runs straight to its
  via at ``c + left(h) * via_pitch / 2``, where ``jog = (via_pitch -
  pitch) / 2`` (so the jog is an exact 45-degree segment);
* on ``L'`` it leaves the via by the mirror-image jog and rejoins
  ``+pitch/2`` at ``jog`` past ``c``.

The collinear approach / via / collinear exit shape is the rule
KiCadRoutingTools documents for pair vias ("Via Placement" in its
``docs/differential-pairs.md``: ``D -> VIA -> D``, P/N vias perpendicular to
the travel direction, approach and exit jogs keeping P/N parallel), MIT; see
``kct ecosystem show kicadroutingtools``.  No code is reused from it; this is
an independent implementation for the heading-free lattice search, where the
straightness lives in the move rather than in the search state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .geometry import Pt, dist

#: Numerical slack (mm) for "is this offset segment long and straight enough
#: to carry the jog" -- far below any copper dimension.
_EPS = 1e-9


@dataclass(frozen=True)
class ViaPair:
    """One placed via pair: where it is and what it joins.

    ``centre`` is the centreline point the vias straddle; ``heading`` is the
    unit travel direction through it (the same on both layers); ``via_p`` /
    ``via_n`` are the two via sites; ``from_layer`` / ``to_layer`` are the
    layer indices of the runs before and after it.
    """

    centre: Pt
    heading: Pt
    via_p: Pt
    via_n: Pt
    from_layer: int
    to_layer: int


def left_of(heading: Pt) -> Pt:
    """The unit normal on the LEFT of ``heading`` (the ``offset > 0`` side)."""
    return (-heading[1], heading[0])


def via_pair_pitch(pitch: float, via_gap: float) -> float:
    """Centre-to-centre distance of a via pair's two vias.

    At least the cross-net via gap the engine requires, and never less than
    the pair pitch (the legs then run straight into their vias).
    """
    return max(via_gap, pitch)


def via_pair_jog(pitch: float, via_pitch: float) -> float:
    """Along-heading length of each leg's 45-degree jog out to its via."""
    return max(0.0, (via_pitch - pitch) / 2.0)


def via_pair_reach(pitch: float, via_pitch: float, via_radius: float, clearance: float) -> float:
    """Least straight run (mm) a via pair needs on EACH side of its centre.

    Half the via footprint along the heading plus clearance, or the jog,
    whichever is longer, plus half a pitch so the offset legs' mitre at the
    preceding (following) turn cannot eat into the jog.
    """
    return max(via_radius + clearance, via_pair_jog(pitch, via_pitch)) + pitch / 2.0


def via_pair_sites(centre: Pt, heading: Pt, via_pitch: float, *, p_left: bool) -> tuple[Pt, Pt]:
    """``(via_p, via_n)`` straddling ``centre`` perpendicular to ``heading``.

    ``p_left`` is the pair's side bit (P on the left of the travel
    direction, the :func:`~kicad_tools.router.lattice.coupled.side_bit`
    convention); P's via is on P's side.
    """
    n = left_of(heading)
    s = 1.0 if p_left else -1.0
    half = via_pitch / 2.0
    via_p = (centre[0] + s * n[0] * half, centre[1] + s * n[1] * half)
    via_n = (centre[0] - s * n[0] * half, centre[1] - s * n[1] * half)
    return via_p, via_n


def _along(a: Pt, b: Pt, heading: Pt) -> float | None:
    """Length of ``a -> b`` when it runs exactly along ``heading``, else ``None``."""
    length = dist(a, b)
    if length <= _EPS:
        return None
    d = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
    if abs(d[0] - heading[0]) > 1e-6 or abs(d[1] - heading[1]) > 1e-6:
        return None
    return length


def jog_into_via(leg: list[Pt], via: Pt, heading: Pt, jog: float) -> list[Pt] | None:
    """Re-end an offset leg that ARRIVES at a via pair at its via.

    ``leg`` ends at the offset of the via pair's centre; its last segment
    must run along ``heading`` and be longer than ``jog``.  The last vertex
    moves back by ``jog`` and the leg then runs 45 degrees out to ``via``.
    ``None`` when the last segment cannot carry the jog.
    """
    if len(leg) < 2:
        return None
    length = _along(leg[-2], leg[-1], heading)
    if length is None or length <= jog + _EPS:
        return None
    end = leg[-1]
    if jog <= _EPS:
        return [*leg[:-1], via] if dist(end, via) > _EPS else list(leg)
    turn = (end[0] - heading[0] * jog, end[1] - heading[1] * jog)
    return [*leg[:-1], turn, via]


def jog_out_of_via(leg: list[Pt], via: Pt, heading: Pt, jog: float) -> list[Pt] | None:
    """Re-start an offset leg that LEAVES a via pair at its via.

    The mirror of :func:`jog_into_via`: ``leg`` starts at the offset of the
    via pair's centre and its first segment must run along ``heading``.
    """
    if len(leg) < 2:
        return None
    length = _along(leg[0], leg[1], heading)
    if length is None or length <= jog + _EPS:
        return None
    start = leg[0]
    if jog <= _EPS:
        return [via, *leg[1:]] if dist(start, via) > _EPS else list(leg)
    turn = (start[0] + heading[0] * jog, start[1] + heading[1] * jog)
    return [via, turn, *leg[1:]]


def is_octilinear(a: Pt, b: Pt) -> bool:
    """Whether ``a -> b`` is a 0/45/90/135-degree segment (or degenerate)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if math.hypot(dx, dy) <= _EPS:
        return True
    return abs(dx) <= 1e-6 or abs(dy) <= 1e-6 or abs(abs(dx) - abs(dy)) <= 1e-6
