"""Balanced bends: source-side intra-pair skew avoidance at the pair's turns (#7649).

A coupled pair is emitted as two offsets of one centerline, ``+/- pitch/2``.
At a mitred joint that turns by ``theta`` the outer leg gains, and the inner
leg loses, ``pitch/2 * tan(theta/2)`` on each of the two segments the joint
joins, so the joint adds

    ``2 * pitch * tan(theta / 2)``

to the P-N length difference, signed by the turn's sense.  That is
``0.83 p`` for a 45-degree joint, ``2 p`` for a 90-degree one and ``4.83 p``
for a 135-degree one.  The same 90 degrees taken as two 45-degree joints
costs ``1.66 p``, and 135 degrees as three costs ``2.49 p``: a sharp joint
makes far more skew than the turning it does needs.  (A smooth arc of the
same angle, ``p * theta``, is the floor; 45-degree steps are within 6 % of
it.)  The net turning itself is fixed by the pad orientations at the two
ends, so this is the part of the skew a router can avoid without bumps:
take every turn that ADDS to the pair's skew in 45-degree steps.

Board 07rf's DQS centerline ended in a 135-degree joint: 2.04 mm of its
pre-tune skew on a 180-degree net turn whose 45-degree floor is 1.19 mm.

``pairplan`` stays engine-free: this module only rewrites a polyline.  The
caller re-verifies the rewritten centerline from scratch (the lattice
engine's pair finish gate), and keeps the original when it does not pass.
"""

from __future__ import annotations

import math

from .geometry import Pt, dist

#: Centerline step lengths tried for each 45-degree step that replaces part
#: of a sharp joint, in pair pitches, smallest (closest to the searched
#: path) first.  Below about half a pitch the inner leg's step collapses
#: (each 45-degree joint shortens it by ``0.41 * pitch / 2`` per side).
BEND_STEP_PITCHES: tuple[float, ...] = (1.0, 1.5, 2.0, 3.0)

#: A sharp joint is only rewritten when the segments either side keep at
#: least this much of their length (mm), so the first and last directions --
#: which fix the leg ends the pad attaches reach -- never change.
_MIN_KEPT_MM = 1e-3

_EPS = 1e-9


def _heading(a: Pt, b: Pt) -> int | None:
    """Octilinear heading of ``a -> b`` in 45-degree units (0..7), or ``None``.

    ``None`` for a zero-length or off-octilinear step: a polyline with one is
    left alone.
    """
    length = dist(a, b)
    if length <= _EPS:
        return None
    ang = math.atan2(b[1] - a[1], b[0] - a[0]) / (math.pi / 4.0)
    k = round(ang)
    if abs(ang - k) > 1e-6:
        return None
    return k % 8


def _turn(h_in: int, h_out: int) -> int:
    """Signed turn ``h_in -> h_out`` in 45-degree units, in ``[-3, 4]``."""
    return ((h_out - h_in + 3) % 8) - 3


def joint_skew(units: int, pitch: float) -> float:
    """Signed P-N length difference one mitred joint of ``units`` x 45 deg adds."""
    if units == 0:
        return 0.0
    return math.copysign(2.0 * pitch * math.tan(abs(units) * math.pi / 8.0), units)


def _turns(points: list[Pt]) -> list[int] | None:
    """Signed turn at each interior vertex, or ``None`` for a non-octilinear line."""
    heads: list[int] = []
    for a, b in zip(points, points[1:], strict=False):
        h = _heading(a, b)
        if h is None:
            return None
        heads.append(h)
    return [_turn(h1, h2) for h1, h2 in zip(heads, heads[1:], strict=False)]


def centerline_skew(points: list[Pt], pitch: float) -> float | None:
    """Signed offset-leg length difference of ``points``' joints (mm).

    The sign follows the turn sense (counter-clockwise in the coordinates
    given is positive); only the magnitude is meaningful to a caller that
    does not track which leg is which.  ``None`` for a polyline that is not
    octilinear or has a zero-length step.
    """
    turns = _turns(points)
    if turns is None:
        return None
    return sum(joint_skew(t, pitch) for t in turns)


def _unit(h: int) -> Pt:
    ang = (h % 8) * math.pi / 4.0
    return (math.cos(ang), math.sin(ang))


def _split(units: int) -> tuple[tuple[int, ...], ...]:
    """Ways to take a sharp joint of ``|units| >= 2`` in smaller joints.

    Most skew saved first: all 45-degree steps, then (for 135 degrees) one
    45-degree step next to a 90-degree joint, in both orders.
    """
    k = abs(units)
    if k == 2:
        return ((1, 1),)
    if k == 3:
        return ((1, 1, 1), (1, 2), (2, 1))
    return ()


def _replace_joint(
    h_in: int, h_out: int, parts: tuple[int, ...], sign: int, step: float
) -> tuple[float, float, list[Pt]] | None:
    """Offsets of a joint rewrite, relative to the joint vertex ``V``.

    The rewrite leaves the incoming segment ``back`` mm before ``V``, takes
    one ``step``-long segment at each intermediate heading, and rejoins the
    outgoing segment ``ahead`` mm after ``V``.  Returns ``(back, ahead,
    offsets)`` where ``offsets`` are the new vertices minus ``V`` (the last
    one lies ``ahead`` along the outgoing segment), or ``None`` when the
    geometry does not close.
    """
    d_in, d_out = _unit(h_in), _unit(h_out)
    heads: list[int] = []
    acc = h_in
    for part in parts[:-1]:
        acc = (acc + sign * part) % 8
        heads.append(acc)
    rx = sum(_unit(h)[0] for h in heads) * step
    ry = sum(_unit(h)[1] for h in heads) * step
    # back * d_in + ahead * d_out = R
    den = d_in[0] * d_out[1] - d_in[1] * d_out[0]
    if abs(den) <= _EPS:
        return None
    back = (rx * d_out[1] - ry * d_out[0]) / den
    ahead = (d_in[0] * ry - d_in[1] * rx) / den
    if back <= _EPS or ahead <= _EPS:
        return None
    pts: list[Pt] = [(-back * d_in[0], -back * d_in[1])]
    x, y = pts[0]
    for h in heads:
        u = _unit(h)
        x, y = x + u[0] * step, y + u[1] * step
        pts.append((x, y))
    return back, ahead, pts


def balance_bends(points: list[Pt], pitch: float, step: float) -> list[Pt] | None:
    """``points`` with its skew-adding sharp joints taken in 45-degree steps.

    Only joints whose turn has the same sense as the centerline's net joint
    skew are rewritten, largest saving first, and only while the magnitude of
    the net still drops: rewriting an opposing joint would ADD skew.  Each
    replaced joint gets ``step``-long (mm) intermediate segments.  A joint
    whose rewrite would eat more of a neighbouring segment than it has
    (keeping :data:`_MIN_KEPT_MM` of the first and last segment, whose
    directions fix the leg ends) is left sharp.

    Returns ``None`` when nothing changed (no sharp skew-adding joint, or none
    fits), so the caller can skip a re-verification.  The result has the same
    end points and end directions as ``points``; its net turning is the
    same; it is always shorter.
    """
    heads: list[int] = []
    for a, b in zip(points, points[1:], strict=False):
        h = _heading(a, b)
        if h is None:
            return None
        heads.append(h)
    if len(heads) < 2:
        return None
    turns = [_turn(h1, h2) for h1, h2 in zip(heads, heads[1:], strict=False)]
    net = sum(joint_skew(t, pitch) for t in turns)
    if abs(net) <= _EPS:
        return None
    sense = 1 if net > 0 else -1

    # Candidate joints: sharp, same sense as the net skew.  Joint j sits at
    # points[j + 1], between segments j and j + 1.
    lengths = [dist(a, b) for a, b in zip(points, points[1:], strict=False)]
    used_back = [0.0] * len(lengths)  # length taken from a segment's end
    used_fore = [0.0] * len(lengths)  # length taken from a segment's start
    order = sorted(
        (j for j, t in enumerate(turns) if abs(t) >= 2 and (t > 0) == (sense > 0)),
        key=lambda j: (-abs(turns[j]), j),
    )
    chosen: dict[int, list[Pt]] = {}
    for j in order:
        t = turns[j]
        for parts in _split(t):
            saving = abs(joint_skew(t, pitch)) - sum(abs(joint_skew(p, pitch)) for p in parts)
            if saving <= _EPS or abs(net - sense * saving) >= abs(net) - _EPS:
                continue
            fit = _replace_joint(heads[j], heads[j + 1], parts, 1 if t > 0 else -1, step)
            if fit is None:
                continue
            back, ahead, offsets = fit
            keep_in = _MIN_KEPT_MM if j == 0 else 0.0
            keep_out = _MIN_KEPT_MM if j + 1 == len(lengths) - 1 else 0.0
            if used_fore[j] + back > lengths[j] - keep_in - _EPS:
                continue
            if used_back[j + 1] + ahead > lengths[j + 1] - keep_out - _EPS:
                continue
            used_back[j] = back
            used_fore[j + 1] = ahead
            v = points[j + 1]
            chosen[j] = [(v[0] + ox, v[1] + oy) for ox, oy in offsets]
            net -= sense * saving
            break
    if not chosen:
        return None
    out: list[Pt] = [points[0]]
    for j in range(len(turns)):
        if j in chosen:
            out.extend(chosen[j])
        else:
            out.append(points[j + 1])
    out.append(points[-1])
    return out
