"""Bounded terminal construction and qualification of uncommitted pair bodies."""

from __future__ import annotations

import copy
import itertools
import math
import time
from typing import TYPE_CHECKING

from .construction_validation import constructed_pair_geometry_issue
from .diffpair_detection import DetectedPair, DetectionSource
from .diffpair_length_tuning import tune_diff_pair_skew
from .match_group_length import MatchGroupTracker

if TYPE_CHECKING:
    from .body_planning import PairBody
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route


def complete_pair_body(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    body: PairBody,
    *,
    deadline: float,
    board_thickness_mm: float,
    num_copper_layers: int,
    allowed_via_sites: tuple[frozenset[tuple[int, int]], frozenset[tuple[int, int]]] | None = None,
    prefer_shortest_approach: bool = False,
) -> tuple[Route, Route] | None:
    """Try at most ten tails per half in each order within the shared deadline.

    Planned sites only restrict the ordinary layer-return search. All live
    copper and the uncommitted partner remain obstacles. Full geometry is
    checked before and after physical-length tuning; coupling and skew must
    meet the authored net class. This function never commits route occupancy.
    """
    if time.monotonic() >= deadline:
        return None
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None or not math.isfinite(board_thickness_mm) or board_thickness_mm <= 0:
        return None
    grid = finder.grid
    originals = (body.p_route, body.n_route)
    if any(not route.segments for route in originals):
        return None
    goals = (pads[1], pads[3])
    heads = tuple(
        router._virtual_pad_at(
            goal, *grid.grid_to_world(*point), grid.layer_to_index(route.segments[-1].layer.value)
        )
        for goal, point, route in zip(goals, (body.p_head, body.n_head), originals, strict=True)
    )
    intra = nc.effective_intra_pair_clearance()
    for first, second in ((0, 1), (1, 0)):
        if time.monotonic() >= deadline:
            return None
        with router._shadow_foreign_copper(*originals):
            tails = router._layer_return_tails(
                finder,
                heads[first],
                goals[first],
                originals[second],
                originals[first],
                deadline=deadline,
                prefer_shortest_approach=prefer_shortest_approach,
                allowed_via_sites=allowed_via_sites[first]
                if allowed_via_sites is not None
                else None,
            )
            for tail in itertools.islice(tails, 10):
                if time.monotonic() >= deadline:
                    return None
                first_route = copy.deepcopy(originals[first])
                first_route.segments.extend(tail.segments)
                first_route.vias.extend(tail.vias)
                with router._shadow_foreign_copper(first_route, originals[second]):
                    other_tails = router._layer_return_tails(
                        finder,
                        heads[second],
                        goals[second],
                        first_route,
                        originals[second],
                        deadline=deadline,
                        prefer_shortest_approach=prefer_shortest_approach,
                        allowed_via_sites=(
                            allowed_via_sites[second] if allowed_via_sites is not None else None
                        ),
                    )
                    for other_tail in itertools.islice(other_tails, 10):
                        if time.monotonic() >= deadline:
                            return None
                        candidate = list(copy.deepcopy(originals))
                        candidate[first] = copy.deepcopy(first_route)
                        candidate[second].segments.extend(other_tail.segments)
                        candidate[second].vias.extend(other_tail.vias)
                        if (
                            constructed_pair_geometry_issue(
                                router,
                                finder,
                                *candidate,
                                pads,
                                intra_pair_clearance=intra,
                                deadline=deadline,
                            )
                            is not None
                        ):
                            continue
                        corpus = {r.net: r for r in [*grid.routes, *router.autorouter.routes]}
                        corpus.update({r.net: r for r in candidate})
                        p, n, _ = tune_diff_pair_skew(
                            DetectedPair(pair=pair, source=DetectionSource.EXPLICIT),
                            corpus,
                            tolerance_mm=nc.skew_tolerance_mm,
                            intra_pair_clearance_mm=intra,
                            grid=grid,
                            board_thickness_mm=board_thickness_mm,
                            num_copper_layers=num_copper_layers,
                            blind_buried_supported=False,
                        )
                        if time.monotonic() >= deadline:
                            return None
                        lengths = [
                            MatchGroupTracker._measure_route_total(
                                r,
                                board_thickness_mm,
                                num_copper_layers,
                                blind_buried_supported=False,
                            )
                            for r in (p, n)
                        ]
                        if abs(lengths[0] - lengths[1]) > nc.skew_tolerance_mm:
                            continue
                        if (
                            min(
                                router._tail_coupled_fraction(p, n.segments),
                                router._tail_coupled_fraction(n, p.segments),
                            )
                            < nc.coupled_continuity_threshold
                        ):
                            continue
                        if (
                            constructed_pair_geometry_issue(
                                router,
                                finder,
                                p,
                                n,
                                pads,
                                intra_pair_clearance=intra,
                                deadline=deadline,
                            )
                            is None
                        ):
                            return p, n
    return None
