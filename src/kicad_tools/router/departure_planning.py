"""Bounded, native-validated departure proposals for geometric pair routing."""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder
    from .primitives import Pad, Route

JointStep = tuple[int, int, int, int, int, int]

#: Native rejection token every prefixed search necessarily reports in bulk:
#: at each step of the forced chain EVERY candidate but the required one is
#: pruned by the prefix constraint itself.  It is a property of the mechanism,
#: not evidence about the board, so the departure ledger drops it rather than
#: let it dominate the histogram that names the blocking guard (#5333).
PREFIX_CONSTRAINT_REJECTION = "departure_prefix"


@dataclass
class DepartureBudget:
    """The caller's shared search allowance, charged across every proposal.

    The counters after ``iterations_used`` are a diagnostic tally, never an
    input.  ``proposals_seen`` is how many escape shapes the geometry
    enumerator offered and ``proposals_validated`` how many survived native
    validation; ``reasons`` histograms why each of the others did not, and
    ``native_rejections`` accumulates the guard tokens the native search
    reported for the failed ones (minus the tautological prefix token).

    Issue #5333: without this, a pair reporting ``departures=0`` is
    indistinguishable between "the enumerator never offered a shape" (a
    structural gate: mismatched start layers, a diagonal pad pair, a single
    routable layer), "every shape was refused at its very first escape step"
    (pad-adjacent copper) and "every shape was refused at its last step" (the
    paired via).  Those are three different defects with three different
    fixes and the total spend cannot tell them apart.
    """

    deadline: float
    iterations_remaining: int
    iterations_used: int = 0
    proposals_seen: int = 0
    proposals_validated: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    native_rejections: Counter[str] = field(default_factory=Counter)

    def stage_summary(self) -> str:
        """One-line tally of where this pair's departure allowance went."""
        reasons = dict(sorted(self.reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        rejections = dict(sorted(self.native_rejections.items(), key=lambda kv: (-kv[1], kv[0])))
        return (
            f"proposals={self.proposals_seen} "
            f"validated={self.proposals_validated} "
            f"departure_reasons={reasons} "
            f"departure_rejections={rejections}"
        )


@dataclass(frozen=True)
class DepartureProposal:
    prefix: tuple[JointStep, ...]
    across: tuple[int, int]
    outward: tuple[int, int]
    layer: int
    bend_steps: int


@dataclass(frozen=True)
class ValidatedDeparture:
    proposal: DepartureProposal
    p_route: Route
    n_route: Route
    iterations: int


def departure_proposals(
    finder: CoupledPathfinder,
    pads: tuple[Pad, Pad, Pad, Pad],
    reasons: Counter[str] | None = None,
) -> Iterator[DepartureProposal]:
    """Enumerate two escape directions/shapes on each other routable layer.

    Source pitch defines an axis-aligned local frame. Geometry never names a
    net or assumes a board origin; native validation decides which proposals
    can actually leave the pads without colliding with copper or their trail.

    ``reasons`` optionally receives one token for each structural gate that
    makes this enumerator yield nothing at all, so an empty enumeration is
    never confused with a natively rejected one (#5333). It is written to,
    never read: no gate here depends on it.
    """
    grid = finder.grid
    p_start, p_goal, n_start, n_goal = pads
    if p_start.layer != n_start.layer:
        if reasons is not None:
            reasons["start_layers_differ"] += 1
        return
    px, py = grid.world_to_grid(p_start.x, p_start.y)
    nx, ny = grid.world_to_grid(n_start.x, n_start.y)
    dx, dy = nx - px, ny - py
    if (dx == 0) == (dy == 0):
        if reasons is not None:
            # Coincident cells or a diagonal pad pair: neither defines the
            # axis-aligned across/outward frame every proposal is built in.
            reasons["start_pads_not_axis_aligned"] += 1
        return
    across = ((1 if dx > 0 else -1), 0) if dx else (0, (1 if dy > 0 else -1))
    directions = [(across[1], -across[0]), (-across[1], across[0])]
    travel = (
        p_goal.x + n_goal.x - p_start.x - n_start.x,
        p_goal.y + n_goal.y - p_start.y - n_start.y,
    )
    directions.sort(key=lambda d: -(d[0] * travel[0] + d[1] * travel[1]))
    start_layer = grid.layer_to_index(p_start.layer.value)
    # Keep the ordinary barrel outside its own pad. Foreign clearance is
    # deliberately not waived here; every native step still checks it.
    extent = max(p_start.height, n_start.height) if across[0] else max(p_start.width, n_start.width)
    escape = max(
        1, math.ceil(max(0.8, extent / 2 + finder.rules.via_diameter / 2) / grid.resolution)
    )
    width = max(finder._get_trace_width_for_net(p.net_name) for p in (p_start, n_start))
    bend = math.ceil(width / grid.resolution) + 1
    bends = (
        (0, bend)
        if bend < escape and abs(dx) + abs(dy) - bend >= finder.min_spacing_cells
        else (0,)
    )
    # Issue #5333: a coupled via places BOTH barrels at the pair's current
    # separation, so a pad pitch below the mutual barrel pitch (via copper or
    # drill hole-to-hole, whichever dominates) cannot host the paired
    # transition at all -- the native step is rejected as ``via_pair_pitch``,
    # which is what confined every fine-pitch escape to its start layer.
    # Fan out symmetrically, one cell per step, immediately before the via and
    # after the escape has already cleared the pad row, exactly as a hand
    # layout necks out of a fine-pitch field. Each spread step is an ordinary
    # asymmetric move and is validated natively like every other step; the
    # barrels themselves still face every copper, drill and history guard.
    pitch = abs(dx) + abs(dy)
    spread = max(0, math.ceil(finder._minimum_via_pitch_cells() - pitch))
    p_spread, n_spread = spread // 2, spread - spread // 2
    targets = [layer for layer in reversed(grid.get_routable_indices()) if layer != start_layer]
    if not targets and reasons is not None:
        # Every proposal ends on a paired via to another layer; a stack that
        # offers none cannot be departed from by construction.
        reasons["no_target_layer"] += 1
    for layer in targets:
        for outward in directions:
            ox, oy = outward
            for bend_steps in bends:
                prefix: list[tuple[int, int, int, int, int, int]] = []
                if bend_steps:
                    prefix.extend(
                        (px, py, start_layer, nx - across[0] * j, ny - across[1] * j, start_layer)
                        for j in range(1, bend_steps + 1)
                    )
                    prefix.extend(
                        (
                            px + ox * j,
                            py + oy * j,
                            start_layer,
                            nx - across[0] * bend_steps + ox * j,
                            ny - across[1] * bend_steps + oy * j,
                            start_layer,
                        )
                        for j in range(1, bend_steps + 1)
                    )
                    prefix.extend(
                        (
                            px + ox * bend_steps,
                            py + oy * bend_steps,
                            start_layer,
                            nx - across[0] * (bend_steps - j) + ox * bend_steps,
                            ny - across[1] * (bend_steps - j) + oy * bend_steps,
                            start_layer,
                        )
                        for j in range(1, bend_steps + 1)
                    )
                prefix.extend(
                    (px + ox * j, py + oy * j, start_layer, nx + ox * j, ny + oy * j, start_layer)
                    for j in range(bend_steps + 1, escape + 1)
                )
                p_esc = (px + ox * escape, py + oy * escape)
                n_esc = (nx + ox * escape, ny + oy * escape)
                prefix.extend(
                    (
                        p_esc[0] - across[0] * j,
                        p_esc[1] - across[1] * j,
                        start_layer,
                        *n_esc,
                        start_layer,
                    )
                    for j in range(1, p_spread + 1)
                )
                p_via = (p_esc[0] - across[0] * p_spread, p_esc[1] - across[1] * p_spread)
                prefix.extend(
                    (
                        *p_via,
                        start_layer,
                        n_esc[0] + across[0] * j,
                        n_esc[1] + across[1] * j,
                        start_layer,
                    )
                    for j in range(1, n_spread + 1)
                )
                n_via = (n_esc[0] + across[0] * n_spread, n_esc[1] + across[1] * n_spread)
                prefix.append((*p_via, layer, *n_via, layer))
                yield DepartureProposal(tuple(prefix), across, outward, layer, bend_steps)


def validated_departures(
    finder: CoupledPathfinder,
    pads: tuple[Pad, Pad, Pad, Pad],
    budget: DepartureBudget,
) -> Iterator[ValidatedDeparture]:
    """Charge native attempts and yield only complete, verified prefixes.

    These are uncommitted route proposals. The caller must validate assembled
    geometry against the live board again before committing either half.

    Every proposal that does not survive leaves one token in ``budget.reasons``
    naming the stage that ended it, and contributes its native guard histogram
    to ``budget.native_rejections`` (#5333). The tally is written only; no
    branch below reads it, so it cannot change which departures are offered.
    """
    for proposal in departure_proposals(finder, pads, budget.reasons):
        budget.proposals_seen += 1
        steps = len(proposal.prefix)
        remaining_time = budget.deadline - time.monotonic()
        if remaining_time <= 0 or budget.iterations_remaining <= 0:
            budget.reasons["allowance_spent_before_attempt"] += 1
            return
        allowance = min(
            budget.iterations_remaining, len(proposal.prefix) + (6 if proposal.bend_steps else 4)
        )
        finder.route_coupled(
            *pads,
            departure_prefix=list(proposal.prefix),
            timeout_seconds=remaining_time,
            max_iterations_budget=allowance,
        )
        used = finder.last_iterations
        budget.iterations_used += used
        budget.iterations_remaining -= used
        if time.monotonic() >= budget.deadline or budget.iterations_remaining < 0:
            budget.reasons["allowance_spent_during_attempt"] += 1
            return
        if finder.last_rejections.get("departure_backend_unavailable"):
            budget.reasons["native_backend_unavailable"] += 1
            return
        path = finder.last_validated_departure_path
        if (
            len(path) != len(proposal.prefix) + 1
            or tuple(tuple(step[:6]) for step in path[1:]) != proposal.prefix
        ):
            budget.reasons[_stall_reason(finder, steps)] += 1
            budget.native_rejections.update(
                {
                    reason: count
                    for reason, count in finder.last_rejections.items()
                    if reason != PREFIX_CONSTRAINT_REJECTION
                }
            )
            continue
        p_route, n_route = finder._reconstruct_coupled_routes_from_cpp_path(path, partial=True)
        if time.monotonic() >= budget.deadline:
            budget.reasons["allowance_spent_during_attempt"] += 1
            return
        budget.proposals_validated += 1
        yield ValidatedDeparture(proposal, p_route, n_route, used)


def _stall_reason(finder: CoupledPathfinder, steps: int) -> str:
    """Name the required step a rejected proposal never got past.

    The native search reports how many required steps it actually expanded,
    so ``stalled_at_step_3_of_18`` says the pad row was left but the fourth
    step is illegal, while ``stalled_at_step_17_of_18`` says only the paired
    via itself is. The budget-bound variants keep an allowance exit from
    being read as a physical blockage -- the proposal may well be legal, the
    search simply was not allowed to finish proving it.
    """
    reached = min(getattr(finder, "last_departure_prefix_progress", 0), steps)
    if finder.last_iteration_limited:
        return f"iteration_limited_at_step_{reached}_of_{steps}"
    if finder.last_timeout_exceeded:
        return f"deadline_at_step_{reached}_of_{steps}"
    return f"stalled_at_step_{reached}_of_{steps}"
