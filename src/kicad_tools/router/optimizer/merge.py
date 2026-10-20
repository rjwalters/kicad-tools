"""Conservative collinear-merge pass shared by the routing save paths."""

from __future__ import annotations

import copy
import logging
import math

from ..primitives import Segment

logger = logging.getLogger(__name__)


def merge_collinear_routes(router) -> bool:
    """Collapse the router's per-grid-cell segments into straight runs.

    Issues #7120 / #7138: the router emits one segment per grid step, so a
    straight 9 mm run is ~90 collinear segments.  ``kct route`` runs the
    trace optimizer before saving; this applies the conservative subset of
    it (``merge_collinear`` only) to every route on ``router``, guarded by
    the connectivity invariant.

    On any failure ``router.routes`` is restored to its pre-call geometry and
    ``False`` is returned, so callers can save the raw routes.
    """
    # Imported at call time so the package-level names stay patchable.
    from ..connectivity_invariant import (
        enforce_connectivity_invariant,
        snapshot_connectivity,
    )
    from . import (
        OptimizationConfig,
        TraceOptimizer,
        make_collision_checker,
        optimize_routes_grid_synced,
    )

    original_routes = copy.deepcopy(router.routes)
    try:
        snapshot = snapshot_connectivity(router)
        optimizer = TraceOptimizer(
            config=OptimizationConfig(
                merge_collinear=True,
                eliminate_zigzags=False,
                compress_staircase=False,
                convert_45_corners=False,
                minimize_vias=False,
            ),
            collision_checker=make_collision_checker(router.grid),
        )
        optimize_routes_grid_synced(router, optimizer)
        enforce_connectivity_invariant(router, snapshot, "optimize", quiet=True)
        return True
    except Exception:
        logger.warning("Trace optimization failed; saving raw routes")
        router.routes = original_routes
        return False


def merge_collinear_same_width(
    router,
    net_names: set[str] | None = None,
    *,
    tolerance_mm: float = 1e-3,
    min_chain_mm: float = 0.0,
) -> int:
    """Fuse each route's collinear same-width segment chains, copper unchanged.

    Issue #7548: the router emits one segment per grid step and a width ramp
    one per width step, so a pair leg can be hundreds of 0.05 mm segments.  The
    skew tuner hosts a serpentine on a single segment of at least
    ``SerpentineConfig.min_segment_length``, so such a leg has no host and the
    pair is declined with ``no_suitable_segment`` although its copper holds
    straight runs of tens of mm.

    Unlike :func:`merge_collinear_routes` (whose merge takes the first
    segment's width), a chain is fused only when every segment has the same
    width and layer, every joint lies within ``tolerance_mm`` of the fused
    line, and no other segment, via or escape of the net ends on that joint.
    The fused segment therefore covers exactly the copper it replaces and the
    route's length is unchanged, so the routing grid needs no update.
    Escape routes and segments owned by a tuning pattern are left alone.

    ``min_chain_mm`` leaves shorter chains as they are.  ``ImpedanceRule``
    exempts segments under ``NECK_DOWN_EXEMPT_LENGTH_MM`` as fine-pitch
    breakout, so fusing a 1.3 mm breakout out of 0.05 mm steps would hand the
    rule a segment it then judges.  Set it above that scale to fuse only the
    trunk runs.

    Args:
        router: The :class:`~kicad_tools.router.core.Autorouter`.
        net_names: Limit to routes of these nets; ``None`` is every route.
        tolerance_mm: Largest deviation of a joint from the fused line.
        min_chain_mm: Shortest chain (summed segment length) worth fusing.

    Returns:
        The number of segments removed.
    """
    removed = 0
    for route in router.routes:
        if route.is_escape or len(route.segments) < 2:
            continue
        if net_names is not None and route.net_name not in net_names:
            continue

        # A joint that anything else of the net also ends on is a junction.
        ends: dict[tuple[float, float], int] = {}
        for other in router.routes:
            if other.net != route.net:
                continue
            for s in other.segments:
                for p in ((s.x1, s.y1), (s.x2, s.y2)):
                    key = (round(p[0], 4), round(p[1], 4))
                    ends[key] = ends.get(key, 0) + 1
            for v in other.vias:
                key = (round(v.x, 4), round(v.y, 4))
                ends[key] = ends.get(key, 0) + 2

        def mergeable(a, b) -> bool:
            if a.tuning_pattern is not None or b.tuning_pattern is not None:
                return False
            if a.layer != b.layer or abs(a.width - b.width) > 1e-6:
                return False
            if abs(a.x2 - b.x1) > 1e-4 or abs(a.y2 - b.y1) > 1e-4:
                return False
            return ends.get((round(a.x2, 4), round(a.y2, 4)), 0) <= 2

        merged: list = []
        chain = [route.segments[0]]

        def flush() -> None:
            nonlocal removed
            first, last = chain[0], chain[-1]
            if len(chain) == 1 or (
                sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in chain) < min_chain_mm
            ):
                merged.extend(chain)
                return
            merged.append(
                Segment(
                    x1=first.x1,
                    y1=first.y1,
                    x2=last.x2,
                    y2=last.y2,
                    width=first.width,
                    layer=first.layer,
                    net=first.net,
                    net_name=first.net_name,
                )
            )
            removed += len(chain) - 1

        for seg in route.segments[1:]:
            trial = [*chain, seg]
            if mergeable(chain[-1], seg) and _chain_is_straight(trial, tolerance_mm):
                chain.append(seg)
            else:
                flush()
                chain = [seg]
        flush()
        route.segments = merged
    return removed


def _chain_is_straight(chain, tolerance_mm: float) -> bool:
    """True when every joint of ``chain`` lies on the line first-start -> last-end.

    Joints must also advance monotonically along that line, strictly between
    the chain's start and end: a collinear fold-back would otherwise fuse and
    drop the copper it doubles back over (Issue #7605).
    """
    first, last = chain[0], chain[-1]
    dx, dy = last.x2 - first.x1, last.y2 - first.y1
    span = math.hypot(dx, dy)
    if span == 0:
        return False
    prev_dot = 0.0
    for s in chain[:-1]:
        if abs((s.x2 - first.x1) * dy - (s.y2 - first.y1) * dx) / span > tolerance_mm:
            return False
        dot = (s.x2 - first.x1) * dx + (s.y2 - first.y1) * dy
        if dot <= 0 or dot >= span * span or dot < prev_dot:
            return False
        prev_dot = dot
    return True
