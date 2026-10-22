"""Collapse via hairpins in a committed route (Issue #7782).

The joint coupled search can land a net's layer change past the point where
the leg turns: the leg runs along layer A to the via, and layer B returns over
the same cells to the turn.  The doubled copper puts A-layer and B-layer
segment ends on the same points, and :mod:`.kicad_length` resolves a junction
of three or more ends in file order -- so the measured length of the net
depends on segment order.

:func:`collapse_via_hairpins` moves such a via back to the turn (or, when the
turn is not legal, to the legal site nearest it) and deletes the doubled
copper on both layers.  It is pure geometry: whether a via may sit at a
candidate site is decided by the caller's ``via_ok`` callback, so the pass
never removes copper it cannot replace with a legal via.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

from .primitives import Route, Segment, Via

_TOL = 1e-4
_DIR_DOT = 1.0 - 1e-6


def _ends(seg: Segment) -> tuple[tuple[float, float], tuple[float, float]]:
    return (seg.x1, seg.y1), (seg.x2, seg.y2)


def _near(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= _TOL and abs(a[1] - b[1]) <= _TOL


def _other_end(seg: Segment, at: tuple[float, float]) -> tuple[float, float] | None:
    p, q = _ends(seg)
    if _near(p, at):
        return q
    if _near(q, at):
        return p
    return None


def _walk(
    segments: list[Segment],
    layer: object,
    start: tuple[float, float],
    u: tuple[float, float] | None,
) -> tuple[list[Segment], tuple[float, float]] | None:
    """Collinear same-layer chain leaving ``start``.

    With ``u`` ``None`` the first segment fixes the direction (and must be the
    only one on the layer at ``start``); the returned direction is its unit
    vector.  Otherwise the chain must leave along ``u``.
    """
    chain: list[Segment] = []
    cur = start
    direction = u
    while True:
        used = {id(s) for s in chain}
        touching = [s for s in segments if s.layer == layer and id(s) not in used]
        touching = [s for s in touching if _other_end(s, cur) is not None]
        if len(touching) != 1:
            if not chain:
                return None
            break
        seg = touching[0]
        far = _other_end(seg, cur)
        assert far is not None
        length = math.hypot(far[0] - cur[0], far[1] - cur[1])
        if length <= _TOL:
            break
        d = ((far[0] - cur[0]) / length, (far[1] - cur[1]) / length)
        if direction is None:
            direction = d
        elif d[0] * direction[0] + d[1] * direction[1] < _DIR_DOT:
            if not chain:
                return None
            break
        chain.append(seg)
        cur = far
    assert direction is not None
    return chain, direction


def _cumulative(start: tuple[float, float], chain: list[Segment]) -> list[float]:
    out: list[float] = []
    cur = start
    total = 0.0
    for seg in chain:
        far = _other_end(seg, cur)
        assert far is not None
        total += math.hypot(far[0] - cur[0], far[1] - cur[1])
        out.append(total)
        cur = far
    return out


def _trim(
    chain: list[Segment], start: tuple[float, float], t: float, new_start: tuple[float, float]
) -> list[Segment]:
    """Drop the first ``t`` mm of ``chain``; return the segments to delete."""
    dropped: list[Segment] = []
    cur = start
    remaining = t
    for seg in chain:
        far = _other_end(seg, cur)
        assert far is not None
        length = math.hypot(far[0] - cur[0], far[1] - cur[1])
        if length <= remaining + _TOL:
            dropped.append(seg)
            remaining -= length
            cur = far
            if remaining <= _TOL:
                break
            continue
        # This segment straddles the cut: its ``cur`` end moves to the cut.
        if _near((seg.x1, seg.y1), cur):
            seg.x1, seg.y1 = new_start
        else:
            seg.x2, seg.y2 = new_start
        break
    return dropped


def collapse_via_hairpins(
    route: Route,
    via_ok: Callable[[Via], bool],
) -> int:
    """Move hairpin vias of ``route`` back to the turn and drop the doubled copper.

    A hairpin is a via where a chain of collinear segments on one layer ends
    and a chain on another layer leaves over the same cells.  ``via_ok`` is
    asked about the via at each candidate site, nearest the turn first; the
    first accepted site wins and a via with no accepted site is left alone.
    Returns the number of vias moved.  ``route`` is mutated in place.
    """
    moved = 0
    for via in list(route.vias):
        if via.in_pad or via.is_micro or via.span_type is not None:
            continue
        at = (via.x, via.y)
        layers = {s.layer for s in route.segments if any(_near(p, at) for p in _ends(s))}
        if len(layers) != 2:
            continue
        la, lb = sorted(layers, key=lambda layer: layer.value)
        for a_layer, b_layer in ((la, lb), (lb, la)):
            walked_a = _walk(route.segments, a_layer, at, None)
            if walked_a is None:
                continue
            chain_a, u = walked_a
            walked_b = _walk(route.segments, b_layer, at, u)
            if walked_b is None:
                continue
            chain_b, _ = walked_b
            if any(s.tuning_pattern is not None for s in chain_a + chain_b):
                continue
            cum_a = _cumulative(at, chain_a)
            cum_b = _cumulative(at, chain_b)
            overlap = min(cum_a[-1], cum_b[-1])
            marks = sorted({round(c, 6) for c in cum_a + cum_b if c <= overlap + _TOL} | {overlap})
            span_ids = {id(s) for s in chain_a + chain_b}
            for t in reversed(marks):
                if t <= _TOL:
                    continue
                site = (at[0] + u[0] * t, at[1] + u[1] * t)
                if _branches_inside(route, span_ids, at, site):
                    continue
                if not via_ok(replace(via, x=site[0], y=site[1])):
                    continue
                dropped = _trim(chain_a, at, t, site) + _trim(chain_b, at, t, site)
                drop_ids = {id(s) for s in dropped}
                route.segments[:] = [s for s in route.segments if id(s) not in drop_ids]
                via.x, via.y = site
                moved += 1
                break
            else:
                continue
            break
    return moved


def _branches_inside(
    route: Route,
    span_ids: set[int],
    a: tuple[float, float],
    b: tuple[float, float],
) -> bool:
    """True when other copper of the route ends inside the span ``a``..``b``."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    for seg in route.segments:
        if id(seg) in span_ids:
            continue
        for p in _ends(seg):
            if _near(p, a):
                return True
            s = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / length**2
            if _TOL <= s * length <= length - _TOL:
                cx, cy = a[0] + s * (b[0] - a[0]), a[1] + s * (b[1] - a[1])
                if math.hypot(p[0] - cx, p[1] - cy) <= _TOL:
                    return True
    for other in route.vias:
        p = (other.x, other.y)
        if _near(p, a):
            continue
        s = ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / length**2
        if _TOL <= s * length <= length - _TOL:
            cx, cy = a[0] + s * (b[0] - a[0]), a[1] + s * (b[1] - a[1])
            if math.hypot(p[0] - cx, p[1] - cy) <= _TOL:
                return True
    return False


def merge_junction_runs(route: Route) -> int:
    """Join collinear same-layer segments that meet at a multi-layer junction.

    A hairpin the via cannot leave (no legal site nearer the turn) keeps its
    doubled copper, but the router chops each leg into grid-step segments, so
    every chop point carries one F.Cu and one B.Cu end -- the 3+-end junctions
    that make the measured length depend on segment order.  Joining the
    collinear pieces on each layer leaves the copper unchanged and the only
    shared ends at the via and the turn, each a plain two-end junction.
    Returns the number of segments removed.
    """
    removed = 0
    via_points = [(v.x, v.y) for v in route.vias]
    changed = True
    while changed:
        changed = False
        for seg in route.segments:
            if seg.tuning_pattern is not None:
                continue
            for end in _ends(seg):
                if any(_near(end, v) for v in via_points):
                    continue
                at_end = [s for s in route.segments if any(_near(p, end) for p in _ends(s))]
                if len(at_end) < 3 or len({s.layer for s in at_end}) < 2:
                    continue
                same = [s for s in at_end if s.layer == seg.layer]
                if len(same) != 2 or any(s.tuning_pattern is not None for s in same):
                    continue
                other = same[0] if same[1] is seg else same[1]
                a = _other_end(seg, end)
                b = _other_end(other, end)
                if a is None or b is None:
                    continue
                ux, uy = end[0] - a[0], end[1] - a[1]
                vx, vy = b[0] - end[0], b[1] - end[1]
                lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
                if lu <= _TOL or lv <= _TOL or ux * vx + uy * vy < lu * lv * _DIR_DOT:
                    continue
                seg.x1, seg.y1, seg.x2, seg.y2 = a[0], a[1], b[0], b[1]
                route.segments[:] = [s for s in route.segments if s is not other]
                removed += 1
                changed = True
                break
            if changed:
                break
    return removed
