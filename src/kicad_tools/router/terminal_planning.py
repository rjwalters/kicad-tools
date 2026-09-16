"""Uncommitted, mutually clear landing-site plans for constructed pairs."""

from __future__ import annotations

import itertools
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

from .layers import Layer
from .primitives import Route, Via
from .via_clearance import drill_hole_to_hole_clear

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad


@dataclass(frozen=True)
class PairLanding:
    p_site: tuple[int, int]
    n_site: tuple[int, int]
    p_reservation: Route
    n_reservation: Route

    @property
    def allowed_sites(self):
        return frozenset({self.p_site}), frozenset({self.n_site})


def landing_proposals(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pads: tuple[Pad, Pad, Pad, Pad],
    *,
    outward: tuple[int, int],
    deadline: float,
) -> Iterator[PairLanding]:
    """Offer goal-side sites in a source-pitch frame, with all barrel guards.

    Candidates use the same four radii as the bounded layer-return search.
    Parallel pin rows first try equal diagonal offsets; perpendicular rows
    first try axial offsets, including staggered barrels for tight pitches.
    Geometry contains no net-name or board-coordinate special cases.
    """
    if time.monotonic() >= deadline:
        return
    grid, rules = finder.grid, finder.rules
    p_start, p_goal, n_start, n_goal = pads
    dx, dy = n_start.x - p_start.x, n_start.y - p_start.y
    if (abs(dx) < 1e-9) == (abs(dy) < 1e-9):
        return
    across = (int(math.copysign(1, dx)), 0) if abs(dx) >= 1e-9 else (0, int(math.copysign(1, dy)))
    if (
        sum(v * v for v in outward) != 1
        or sum(a * b for a, b in zip(across, outward, strict=True)) != 0
    ):
        return
    sign = 1 if (p_goal.x - p_start.x) * across[0] + (p_goal.y - p_start.y) * across[1] >= 0 else -1
    back = (-sign * across[0], -sign * across[1])
    goal_axis = (n_goal.x - p_goal.x, n_goal.y - p_goal.y)
    parallel = abs(goal_axis[0] * across[1] - goal_axis[1] * across[0]) < 1e-9
    diagonal = (back[0] + outward[0], back[1] + outward[1])
    directions = [diagonal, back, outward] if parallel else [back, diagonal, outward]
    directions += [(back[0] - outward[0], back[1] - outward[1]), (-outward[0], -outward[1])]
    radii = (0.6, 1.2, 1.8, 2.4)
    offsets = sorted(itertools.product(radii, repeat=2), key=lambda r: (sum(r), abs(r[0] - r[1])))
    foreign = [*grid.routes, *router.autorouter.routes]
    drills = router._collect_existing_drills() + [
        (v.x, v.y, v.drill) for r in grid.routes for v in r.vias
    ]
    seen = set()
    for direction in directions:
        for rp, rn in offsets:
            if time.monotonic() >= deadline:
                return
            sites = tuple(
                grid.world_to_grid(goal.x + direction[0] * radius, goal.y + direction[1] * radius)
                for goal, radius in zip((p_goal, n_goal), (rp, rn), strict=True)
            )
            if sites in seen:
                continue
            seen.add(sites)
            reservations: list[Route] = []
            for goal, site in zip((p_goal, n_goal), sites, strict=True):
                gx, gy = site
                if finder._is_via_blocked(
                    gx, gy, goal.net
                ) and not router._via_has_only_geometry_blockers(finder, gx, gy):
                    break
                x, y = grid.grid_to_world(gx, gy)
                via = Via(
                    x=x,
                    y=y,
                    diameter=rules.via_diameter,
                    drill=rules.via_drill,
                    layers=(
                        Layer(grid.index_to_layer(0)),
                        Layer(grid.index_to_layer(grid.num_layers - 1)),
                    ),
                    net=goal.net,
                    net_name=goal.net_name,
                )
                if (
                    grid.worst_via_pad_deficit(
                        via, exclude_net=-1, clearance_floor=rules.via_clearance
                    )[0]
                    > 1e-9
                ):
                    break
                if not drill_hole_to_hole_clear(x, y, via.drill, drills, rules.min_hole_to_hole):
                    break
                if not _barrel_clear(router, via, foreign + reservations):
                    break
                reservations.append(Route(net=goal.net, net_name=goal.net_name, vias=[via]))
            if len(reservations) == 2 and time.monotonic() < deadline:
                yield PairLanding(sites[0], sites[1], reservations[0], reservations[1])


def _barrel_clear(router, via, routes):
    rules = router.autorouter.rules
    for route in routes:
        for other in route.vias:
            distance = math.hypot(via.x - other.x, via.y - other.y)
            if distance < (via.drill + other.drill) / 2 + rules.min_hole_to_hole - 1e-9:
                return False
            if (
                route.net != via.net
                and distance < (via.diameter + other.diameter) / 2 + rules.via_clearance - 1e-9
            ):
                return False
        if route.net != via.net and any(
            router._point_segment_distance(via.x, via.y, s)
            < (via.diameter + s.width) / 2 + rules.via_clearance - 1e-9
            for s in route.segments
        ):
            return False
    return True


def select_landing_plan(
    router: DiffPairRouter,
    proposals: list[list[PairLanding]],
    *,
    deadline: float,
    max_choices: int,
) -> list[PairLanding] | None:
    """Bounded backtracking chooses compatible barrels without committing them.

    The caller supplies already validated per-pair proposals and must recheck
    final bodies against these reservations and the current live route corpus.
    The choice cap is shared across the entire search, including backtracking.
    """
    choices = 0
    selected: list[PairLanding] = []

    def search(index):
        nonlocal choices
        if time.monotonic() >= deadline:
            return False
        if index == len(proposals):
            return True
        if choices >= max_choices:
            return False
        for proposal in proposals[index]:
            if time.monotonic() >= deadline or choices >= max_choices:
                return False
            choices += 1
            existing = [r for p in selected for r in (p.p_reservation, p.n_reservation)]
            if not all(
                _barrel_clear(router, r.vias[0], existing)
                for r in (proposal.p_reservation, proposal.n_reservation)
            ):
                continue
            selected.append(proposal)
            if search(index + 1):
                return True
            selected.pop()
        return False

    return list(selected) if search(0) else None
