"""Preserve pad-local width tapers when emitting merged routing segments."""

from __future__ import annotations

import math


def taper_points(
    p0: tuple[float, float],
    p1: tuple[float, float],
    centers: list[tuple[float, float]],
    radius: float,
    resolution: float,
) -> list[tuple[float, float]]:
    """Split only the pad-local parts of a straight segment at grid resolution.

    Search reserves the net-class width. Emission may narrow that envelope
    near a fine-pitch pad, but must recover full width outside its taper disk.
    Circle intersections delimit that disk; sampling inside it represents the
    width profile without fragmenting the full-width corridor.
    """
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length2 = dx * dx + dy * dy
    if not centers or radius <= 0 or length2 == 0:
        return [p0, p1]
    length = math.sqrt(length2)
    cuts = {0.0, 1.0}
    for cx, cy in centers:
        ox, oy = p0[0] - cx, p0[1] - cy
        b = ox * dx + oy * dy
        discriminant = b * b - length2 * (ox * ox + oy * oy - radius * radius)
        if discriminant <= 0:
            continue
        root = math.sqrt(discriminant)
        lo = max(0.0, (-b - root) / length2)
        hi = min(1.0, (-b + root) / length2)
        if hi <= lo:
            continue
        count = max(1, math.ceil((hi - lo) * length / max(resolution, 1e-6)))
        cuts.update(lo + (hi - lo) * i / count for i in range(count + 1))
    return [(p0[0] + t * dx, p0[1] + t * dy) for t in sorted(cuts)]


def band_safe_width(
    a: tuple[float, float],
    b: tuple[float, float],
    width: float,
    base_width: float,
    own_net_bboxes: list[tuple[float, float, float, float]] | None,
    edge_mm: float,
) -> float:
    """Keep a tapered sub-segment out of the own-net pad slit band (#9019).

    The #1018 taper only ever NARROWS copper, and narrowing a leg that the
    search certified at ``base_width`` (touching its own pad, or clear of the
    fab ``pad_spacing`` detection edge) opens an air gap between the leg's
    edge and the pad: a leg that unioned with the pad corner at 0.2 mm lands
    in the ``(0, edge_mm)`` band at 0.1 mm -- the staircase of fab-view
    ``physical_copper_gap`` slits hugging a fine-pitch pad's corner.  When
    ``width`` leaves the segment in the band but ``base_width`` does not,
    keep ``base_width``; otherwise the taper width stands.

    Pure and backend-agnostic: the Python grid router and the C++ backend's
    route conversion both call it, so the two emit identical widths.
    ``own_net_bboxes`` are the segment's own-net, own-layer SMD pad boxes;
    ``edge_mm <= 0`` (stackup unresolved) or no pads leaves ``width`` as is.
    """
    if edge_mm <= 0 or not own_net_bboxes or width >= base_width:
        return width
    from .via_clearance import same_net_segment_pad_gap_acceptable

    if same_net_segment_pad_gap_acceptable(a, b, width / 2, own_net_bboxes, edge_mm):
        return width
    if same_net_segment_pad_gap_acceptable(a, b, base_width / 2, own_net_bboxes, edge_mm):
        return base_width
    return width


Piece = tuple[tuple[float, float], tuple[float, float], float]


def band_safe_taper(
    pieces: list[Piece],
    own_net_bboxes: list[tuple[float, float, float, float]] | None,
    edge_mm: float,
) -> list[Piece]:
    """Fuse taper sub-segments that still sit in the own-net slit band (#9019).

    ``pieces`` are the ``(start, end, width)`` sub-segments a route
    conversion is about to emit on ONE layer, in path order, each width
    already chosen by the #1018 taper and :func:`band_safe_width`.  Returns
    the sub-segments to emit; zero-length pieces are dropped.

    The taper fragments a leg into short pieces, and each piece is its own
    copper primitive.  A piece that ends just short of the pad the leg
    unions with -- the diagonal approach to a fine-pitch pad corner, U4.1
    on board 06 -- sits in ``(0, edge_mm)`` of that pad at EVERY legal
    width: its round end cap faces the pad edge across a slit, and the fab
    view grades it because that piece itself never touches the pad (the
    leg's union with the pad is through a LATER piece).  Neither width
    choice can fix it, so :func:`band_safe_width` leaves it as is.

    Such a piece is fused with its neighbours along the same straight run
    (consecutive, end-to-start contiguous, same direction) into one segment
    that reaches the pad or clears the edge, drawn at the NARROWEST width
    of the pieces it replaces: copper never grows beyond the taper profile,
    so clearance to other nets cannot get worse.  The smallest acceptable
    window wins (forward first on a tie); a piece with no acceptable window
    is emitted unchanged.  The fused segment must pass
    :func:`~kicad_tools.router.via_clearance.same_net_segment_pad_gap_acceptable`
    against every own-net pad box, so it cannot open a band elsewhere.

    Runs are found here rather than by the caller because the two backends
    hand the conversion different segmentations of the same path (Python
    merges collinear grid steps before tapering; the native adapter passes
    one segment per grid cell), and a fuse limited to one caller-side
    segment would differ between them.  Pure and backend-agnostic, like
    :func:`band_safe_width`.  ``edge_mm <= 0`` or no pads returns the
    pieces unchanged.
    """
    pieces = [p for p in pieces if p[0] != p[1]]
    if edge_mm <= 0 or not own_net_bboxes or len(pieces) < 2:
        return pieces
    out: list[Piece] = []
    run: list[Piece] = [pieces[0]]
    for piece in pieces[1:]:
        if _continues_run(run[-1], piece):
            run.append(piece)
        else:
            out.extend(_fuse_run(run, own_net_bboxes, edge_mm))
            run = [piece]
    out.extend(_fuse_run(run, own_net_bboxes, edge_mm))
    return out


def _continues_run(prev: Piece, piece: Piece) -> bool:
    """``piece`` starts where ``prev`` ends and heads the same way."""
    (ax, ay), (bx, by), _ = prev
    (cx, cy), (dx, dy), _ = piece
    if abs(bx - cx) > 1e-9 or abs(by - cy) > 1e-9:
        return False
    ux, uy = bx - ax, by - ay
    vx, vy = dx - cx, dy - cy
    lu, lv = math.hypot(ux, uy), math.hypot(vx, vy)
    if lu == 0 or lv == 0:
        return False
    return abs(ux * vy - uy * vx) <= 1e-9 * lu * lv and ux * vx + uy * vy > 0


def _fuse_run(
    pieces: list[Piece],
    own_net_bboxes: list[tuple[float, float, float, float]],
    edge_mm: float,
) -> list[Piece]:
    """:func:`band_safe_taper` over one straight run of pieces."""
    if len(pieces) < 2:
        return pieces
    from .via_clearance import same_net_segment_pad_gap_acceptable

    def ok(a: tuple[float, float], b: tuple[float, float], width: float) -> bool:
        return same_net_segment_pad_gap_acceptable(a, b, width / 2, own_net_bboxes, edge_mm)

    out: list[Piece] = []
    i = 0
    n = len(pieces)
    while i < n:
        a, b, w = pieces[i]
        if ok(a, b, w):
            out.append(pieces[i])
            i += 1
            continue
        best: tuple[int, str, float] | None = None  # (extra pieces, side, width)
        # Forward: fuse pieces i..i+k.
        narrow = w
        for hi in range(i + 1, n):
            narrow = min(narrow, pieces[hi][2])
            if ok(a, pieces[hi][1], narrow):
                best = (hi - i, "fwd", narrow)
                break
        # Backward: fuse the already-emitted tail out[-k:] with piece i.
        narrow = w
        for k in range(1, len(out) + 1):
            if best is not None and k >= best[0]:
                break
            narrow = min(narrow, out[-k][2])
            if ok(out[-k][0], b, narrow):
                best = (k, "back", narrow)
                break
        if best is None:
            out.append(pieces[i])
            i += 1
        elif best[1] == "fwd":
            out.append((a, pieces[i + best[0]][1], best[2]))
            i += best[0] + 1
        else:
            start = out[-best[0]][0]
            del out[-best[0] :]
            out.append((start, b, best[2]))
            i += 1
    return out
