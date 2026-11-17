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


_OCTILINEAR = tuple((math.cos(k * math.pi / 4), math.sin(k * math.pi / 4)) for k in range(8))


def _layer_path(
    segments: list[Segment],
    layer: object,
    start: tuple[float, float],
    first: list[Segment],
    max_arc: float,
) -> list[tuple[Segment, tuple[float, float], tuple[float, float]]]:
    """Unbranched same-layer path continuing past ``first`` (any direction).

    Returns ``(segment, near_end, far_end)`` triples, starting with the
    ``first`` chain, and stops at a branch, a dead end or ``max_arc`` mm.
    """
    path: list[tuple[Segment, tuple[float, float], tuple[float, float]]] = []
    used: set[int] = set()
    cur = start
    arc = 0.0
    for seg in first:
        far = _other_end(seg, cur)
        assert far is not None
        path.append((seg, cur, far))
        used.add(id(seg))
        arc += math.hypot(far[0] - cur[0], far[1] - cur[1])
        cur = far
    while arc <= max_arc + _TOL:
        touching = [
            s
            for s in segments
            if s.layer == layer and id(s) not in used and _other_end(s, cur) is not None
        ]
        if len(touching) != 1 or touching[0].tuning_pattern is not None:
            break
        seg = touching[0]
        far = _other_end(seg, cur)
        assert far is not None
        path.append((seg, cur, far))
        used.add(id(seg))
        arc += math.hypot(far[0] - cur[0], far[1] - cur[1])
        cur = far
    return path


def _ray_hit(
    origin: tuple[float, float],
    d: tuple[float, float],
    p: tuple[float, float],
    q: tuple[float, float],
) -> float | None:
    """Parameter ``s`` in [0, 1] where the ray ``origin + t*d`` (t > 0) meets ``p..q``."""
    ex, ey = q[0] - p[0], q[1] - p[1]
    den = d[0] * ey - d[1] * ex
    if abs(den) <= 1e-12:
        return None
    wx, wy = p[0] - origin[0], p[1] - origin[1]
    t = (wx * ey - wy * ex) / den
    s = (wx * d[1] - wy * d[0]) / den
    if t <= _TOL or s < -1e-9 or s > 1.0 + 1e-9:
        return None
    return min(max(s, 0.0), 1.0)


def _segments_cross(a: Segment, b: Segment) -> bool:
    """True when the two segments share a point other than a common end."""
    (p, q), (r, t) = _ends(a), _ends(b)

    def orient(o: tuple[float, float], x: tuple[float, float], y: tuple[float, float]) -> float:
        return (x[0] - o[0]) * (y[1] - o[1]) - (x[1] - o[1]) * (y[0] - o[0])

    shared = [e for e in (r, t) if _near(e, p) or _near(e, q)]
    if shared:
        return False
    d1, d2 = orient(p, q, r), orient(p, q, t)
    d3, d4 = orient(r, t, p), orient(r, t, q)
    eps = 1e-9
    if ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    ):
        return True

    def on(o: tuple[float, float], x: tuple[float, float], y: tuple[float, float]) -> bool:
        return (
            abs(orient(o, x, y)) <= 1e-7
            and min(o[0], x[0]) - _TOL <= y[0] <= max(o[0], x[0]) + _TOL
            and min(o[1], x[1]) - _TOL <= y[1] <= max(o[1], x[1]) + _TOL
        )

    return on(p, q, r) or on(p, q, t) or on(r, t, p) or on(r, t, q)


def shortcut_via_hairpins(
    route: Route,
    seg_ok: Callable[[Segment], bool],
    anchors: list[tuple[float, float]] | None = None,
) -> int:
    """Drop the doubled copper of a hairpin whose via cannot move (Issue #7808).

    Where :func:`collapse_via_hairpins` finds no legal via site, the layer
    whose leg turns first still runs back over the other layer's cells from
    the via to its turn.  This replaces that doubled stretch -- and up to the
    same length again of the path past the turn -- with one octilinear
    segment straight from the via, so the via keeps its site and the copper
    no longer doubles back.  The longest shortcut ``seg_ok`` accepts wins.
    ``anchors`` are points (the net's pad centres) the removed copper must
    not pass through.  Returns the number of hairpins shortcut.  ``route``
    is mutated in place.
    """
    anchors = anchors or []
    done = 0
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
            len_a = _cumulative(at, chain_a)[-1]
            len_b = _cumulative(at, chain_b)[-1]
            # Reshape the layer whose leg turns first (the shorter one).
            if len_b > len_a + _TOL:
                continue
            overlap = len_b
            path = _layer_path(route.segments, b_layer, at, chain_b, 2.0 * overlap)
            n_hair = len(chain_b)
            if len(path) <= n_hair:
                continue
            other = [s for s in route.segments if not any(s is e[0] for e in path)]
            candidates: list[tuple[float, int, float, tuple[float, float]]] = []
            arc = overlap
            for idx in range(n_hair, len(path)):
                _seg, p, q = path[idx]
                seg_len = math.hypot(q[0] - p[0], q[1] - p[1])
                hits = {1.0}
                for d in _OCTILINEAR:
                    hit = _ray_hit(at, d, p, q)
                    if hit is not None:
                        hits.add(round(hit, 9))
                for frac in hits:
                    point = (p[0] + (q[0] - p[0]) * frac, p[1] + (q[1] - p[1]) * frac)
                    along = arc + seg_len * frac
                    if along > 2.0 * overlap + _TOL:
                        continue
                    dx, dy = point[0] - at[0], point[1] - at[1]
                    dist = math.hypot(dx, dy)
                    if dist <= _TOL or dist >= along - _TOL:
                        continue
                    # Octilinear, and not back along the doubled leg.
                    if not any(
                        abs(dx / dist - d[0]) <= 1e-6 and abs(dy / dist - d[1]) <= 1e-6
                        for d in _OCTILINEAR
                    ):
                        continue
                    if abs(dx / dist - u[0]) <= 1e-6 and abs(dy / dist - u[1]) <= 1e-6:
                        continue
                    candidates.append((along, idx, frac, point))
                arc += seg_len
            for _along, idx, frac, point in sorted(candidates, key=lambda c: -c[0]):
                removed = [e[0] for e in path[:idx]]
                if frac >= 1.0 - 1e-9:
                    removed.append(path[idx][0])
                removed_ids = {id(s) for s in removed}
                # Removed copper must not carry a branch, a via or a pad, and
                # the new end must not land on other copper's end (that is
                # the multi-end junction this pass exists to remove).
                fixed = [(v.x, v.y) for v in route.vias if v is not via] + list(anchors)
                same_layer = [pt for s in other if s.layer == b_layer for pt in _ends(s)]
                any_layer = [pt for s in other for pt in _ends(s)]
                inner = [e[2] for e in path[:idx]]
                if any(_near(k, pt) for k in fixed + same_layer for pt in inner):
                    continue
                if any(_near(k, point) for k in fixed + any_layer):
                    continue
                first = path[0][0]
                new = replace(first, x1=at[0], y1=at[1], x2=point[0], y2=point[1])
                survivors = [s for s in route.segments if id(s) not in removed_ids]
                if any(
                    s.layer == b_layer and s is not path[idx][0] and _segments_cross(new, s)
                    for s in survivors
                ):
                    continue
                if not seg_ok(new):
                    continue
                if frac < 1.0 - 1e-9:
                    seg = path[idx][0]
                    near = path[idx][1]
                    if _near((seg.x1, seg.y1), near):
                        seg.x1, seg.y1 = point
                    else:
                        seg.x2, seg.y2 = point
                route.segments[:] = survivors + [new]
                done += 1
                break
            else:
                continue
            break
    return done


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
