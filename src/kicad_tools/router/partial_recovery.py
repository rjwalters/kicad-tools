"""Bounded geometric completion of saved coupled-search paths."""

from __future__ import annotations

import copy
import math
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from .construction_validation import constructed_pair_geometry_issue
from .diffpair_detection import DetectedPair, DetectionSource
from .diffpair_length_tuning import tune_diff_pair_skew
from .match_group_length import MatchGroupTracker
from .optimizer.consolidate import consolidate_segments

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route


def recover_partial_pair(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    *,
    deadline: float,
    board_thickness_mm: float,
    num_copper_layers: int,
) -> tuple[Route, Route] | None:
    """Complete a partial path without another search or occupancy mutation.

    Construction and validation share the caller's absolute deadline. Ordinary
    crossover vias are balanced on the other half before physical-length
    tuning; every accepted pair passes full geometry, skew and coupling gates.
    """
    path = getattr(finder, "last_best_cpp_path", None)
    if not path or time.monotonic() >= deadline:
        return None
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None or board_thickness_mm <= 0 or not math.isfinite(board_thickness_mm):
        return None
    grid = router.autorouter.grid
    expected_root = (
        *grid.world_to_grid(pads[0].x, pads[0].y),
        grid.layer_to_index(pads[0].layer.value),
        *grid.world_to_grid(pads[2].x, pads[2].y),
        grid.layer_to_index(pads[2].layer.value),
    )
    if tuple(path[0][:6]) != expected_root:
        return None
    intra = nc.effective_intra_pair_clearance()
    tolerance = nc.effective_skew_tolerance()
    threshold = nc.effective_coupled_continuity_threshold()
    original_pads = finder._cpp_reconstruct_pads
    finder._cpp_reconstruct_pads = pads
    try:
        for trim in (0, 4, 8, 12, 20, 30):
            if time.monotonic() >= deadline:
                return None
            partial = path[: len(path) - trim] if trim else path
            if len(partial) < 2:
                continue
            p, n = finder._reconstruct_coupled_routes_from_cpp_path(partial, partial=True)
            px, py, pl, nx, ny, nl, _ = partial[-1]
            heads = (
                router._virtual_pad_at(pads[0], *grid.grid_to_world(px, py), pl),
                router._virtual_pad_at(pads[2], *grid.grid_to_world(nx, ny), nl),
            )
            for crossing in (0, 1):
                if time.monotonic() >= deadline:
                    return None
                routes = copy.deepcopy([p, n])
                other = 1 - crossing
                goal = pads[1 if other == 0 else 3]
                with router._shadow_foreign_copper(*routes):
                    tail = router._synthesize_tail(
                        finder,
                        heads[other],
                        goal,
                        grid.layer_to_index(heads[other].layer.value),
                        partner_segments=routes[crossing].segments,
                        partner_clearance=router._pair_seg_clearance(finder, goal.net_name),
                        partner_vias=routes[crossing].vias,
                        prefer_shortest=True,
                    )
                if tail is None:
                    continue
                routes[other].segments.extend(tail.segments)
                with router._shadow_foreign_copper(*routes):
                    tail = router._synthesize_crossing_tail(
                        finder,
                        heads[crossing],
                        pads[1 if crossing == 0 else 3],
                        grid.layer_to_index(heads[crossing].layer.value),
                        routes[other].segments,
                        body_segments=routes[crossing].segments,
                        deadline=deadline,
                    )
                if tail is None:
                    continue
                routes[crossing].segments.extend(tail.segments)
                routes[crossing].vias.extend(tail.vias)
                for candidate in _balance_crossing(router, finder, routes, other, pads, deadline):
                    if time.monotonic() >= deadline:
                        return None
                    if constructed_pair_geometry_issue(
                        router,
                        finder,
                        candidate[0],
                        candidate[1],
                        pads,
                        intra_pair_clearance=intra,
                        deadline=deadline,
                    ):
                        continue
                    corpus = {r.net: r for r in router.autorouter.routes}
                    corpus.update({r.net: r for r in candidate})
                    tuned_p, tuned_n, _ = tune_diff_pair_skew(
                        DetectedPair(pair=pair, source=DetectionSource.EXPLICIT),
                        corpus,
                        tolerance_mm=tolerance,
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
                            r, board_thickness_mm, num_copper_layers, blind_buried_supported=False
                        )
                        for r in (tuned_p, tuned_n)
                    ]
                    if abs(lengths[0] - lengths[1]) > tolerance:
                        continue
                    if (
                        min(
                            router._tail_coupled_fraction(tuned_p, tuned_n.segments),
                            router._tail_coupled_fraction(tuned_n, tuned_p.segments),
                        )
                        < threshold
                    ):
                        continue
                    if (
                        constructed_pair_geometry_issue(
                            router,
                            finder,
                            tuned_p,
                            tuned_n,
                            pads,
                            intra_pair_clearance=intra,
                            deadline=deadline,
                        )
                        is None
                    ):
                        return tuned_p, tuned_n
    finally:
        finder._cpp_reconstruct_pads = original_pads
    return None


def _balance_crossing(router, finder, routes, target, pads, deadline):
    """Try two longest hosts and a fixed set of interior via windows."""
    originals = copy.deepcopy(routes)
    for route in originals:
        route.segments, _ = consolidate_segments(
            route.segments,
            protected_points=[(v.x, v.y) for v in route.vias] + [(p.x, p.y) for p in pads],
            tolerance=1e-9,
        )
    if len(originals[0].vias) == len(originals[1].vias):
        yield originals
        return
    if len(originals[1 - target].vias) - len(originals[target].vias) != 2:
        return
    indices = sorted(
        range(len(originals[target].segments)),
        key=lambda i: math.dist(
            originals[target].segments[i].start, originals[target].segments[i].end
        ),
        reverse=True,
    )[:2]
    for index in indices:
        for fraction in (0.25, 0.5, 0.75):
            for window in (0.8, 1.2):
                if time.monotonic() >= deadline:
                    return
                candidate = copy.deepcopy(originals)
                route, partner = candidate[target], candidate[1 - target]
                host = route.segments[index]
                length = math.dist(host.start, host.end)
                if length * fraction + window >= length:
                    continue
                ux, uy = (host.x2 - host.x1) / length, (host.y2 - host.y1) / length
                x1, y1 = host.x1 + ux * length * fraction, host.y1 + uy * length * fraction
                x2, y2 = x1 + ux * window, y1 + uy * window
                layer = finder.grid.layer_to_index(host.layer.value)
                head = router._virtual_pad_at(pads[0 if target == 0 else 2], x1, y1, layer)
                goal = replace(head, x=x2, y=y2)
                with router._shadow_foreign_copper(*candidate):
                    tail = router._synthesize_crossing_tail(
                        finder, head, goal, layer, partner.segments, deadline=deadline
                    )
                if tail is None:
                    continue
                route.segments[index : index + 1] = [
                    replace(host, x2=x1, y2=y1),
                    *tail.segments,
                    replace(host, x1=x2, y1=y2),
                ]
                route.vias.extend(tail.vias)
                yield candidate
