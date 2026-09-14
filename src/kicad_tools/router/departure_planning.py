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


#: Issue #5333: MIPI_DAT1's departure gap turned out NOT to be a single via
#: site the escape depth could dodge (a wider, budget-backed re-measurement
#: -- up to 8x the escape distance and 4x the native allowance -- still
#: stalled every proposal on its last step, at every depth, on every target
#: layer, in BOTH escape directions).  What actually distinguishes it from
#: its siblings (MIPI_CLK, MIPI_DAT0, TMDS_D0/D1/D2), which all resolve
#: exactly 6 of their 10-12 proposals, is which HALF resolves: every sibling
#: has one clean escape direction (the one ``departure_proposals`` sorts
#: first, toward the pair's own goal) and one blocked one; MIPI_DAT1 has
#: neither.  ``DIRECTION_TOWARD_GOAL`` / ``DIRECTION_AWAY_FROM_GOAL`` label
#: that split so the ledger states it directly instead of requiring a
#: one-off instrumented replay to notice it.
DIRECTION_TOWARD_GOAL = "toward_goal"
DIRECTION_AWAY_FROM_GOAL = "away_from_goal"


#: Issue #5333 (follow-up): the natural next step from the note above --
#: "widen the escape depth further, since +0..+8 wasn't enough" -- was
#: measured directly against Board07 (native-validating hand-built
#: departure prefixes, escape depth swept 0..+35 cells with the SAME
#: bend/spread geometry ``departure_proposals`` itself uses) and is a DEAD
#: END, not merely an unexplored parameter.  It is not a scarcity-of-sites
#: problem either: an independent grid-legality sweep
#: (``_is_via_blocked`` across a wide x/y window) found real, fully-clear
#: paired via sites in both directions -- roughly 13-31 cells straight out,
#: or >=26 cells laterally, past J1's own through-hole GND mechanical pads
#: (footprint pads ``M1``/``M2``, layers ``*.Cu`` -- i.e. copper on EVERY
#: layer, not a plane/pour antipad gap, ruling out that half of the
#: originally-filed hypothesis) and MIPI_CLK_N's already-committed copper.
#:
#: The reason NEITHER a deeper nor a wider fan can reach those otherwise-
#: legal sites: ``route_coupled``'s native search only relaxes its
#: symmetric-move spacing tolerance (``target_spacing`` +/- 1 cell,
#: otherwise) within ``effective_departure_radius`` Manhattan cells of the
#: start pads (diffpair_routing.py, ``effective_departure_radius = max(
#: effective_target_spacing, 6, start_spacing_delta * 2 + 4,
#: via_spread_delta * 2 + 4)``) -- measured at exactly 14 cells for
#: MIPI_DAT1 (``effective_target_spacing=3``, pad pitch 8 cells, so
#: ``start_spacing_delta=5`` dominates: ``5*2+4=14``).  Every departure
#: proposal ``departure_proposals`` builds is a SYMMETRIC straight run (P
#: and N move by the identical delta every step, holding the full pad
#: pitch) until its one narrowing point -- ``spread``, sized only for
#: via-hole legality -- immediately before the final via.  For MIPI_DAT1
#: that ``spread`` is 0 (via-pitch is already satisfied at pad pitch), so
#: the escape never narrows at all.  Once a forced symmetric step crosses
#: the 14-cell departure radius still holding the full 8-cell pitch against
#: a 3-cell target, the native search's own ``sym_spacing`` tolerance check
#: rejects it outright (measured directly: native-validated hand-built
#: prefixes reproducing ``departure_proposals``'s exact geometry, escape
#: depth swept 0/4/8/11/13/16/20 cells past baseline in +y and
#: 0/8/16/24/27/29/32/35 in -y, ALL fail identically once the forced escape
#: step first exceeds distance 14 from the start pad -- both directions
#: plateau at the SAME relative step regardless of how much further budget
#: or depth is granted, which is the fingerprint of a fixed-radius search
#: policy, not a foreign-copper collision).  Since the real legal via sites
#: sit well outside that 14-cell radius in every direction tried, no
#: geometric widening of the CURRENT (symmetric, non-narrowing) escape leg
#: -- deeper, wider, or both -- can ever reach them.
#:
#: Next step (not yet attempted): a departure candidate whose escape leg
#: narrows the P/N separation gradually (asymmetric per-step moves, the
#: same mechanism the free/unconstrained search already uses elsewhere)
#: toward ``effective_target_spacing`` as it travels outward, so the pitch
#: is already within native tolerance by the time it crosses the 14-cell
#: departure radius -- rather than staying at full pad pitch the whole way
#: and asking the native search to relax a boundary it does not relax past.
#: This changes proposal GEOMETRY, not any search/iteration budget or
#: legality/clearance check -- every existing native guard (copper,
#: clearance, trail, via-pitch) still applies to each step exactly as
#: before.


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

    ``direction_seen`` / ``direction_validated`` further split ``proposals_seen``
    / ``proposals_validated`` by :data:`DIRECTION_TOWARD_GOAL` /
    :data:`DIRECTION_AWAY_FROM_GOAL` (see the constants' docstring): a pair
    that clears every proposal in one direction and none in the other has a
    directional obstruction the escape-depth axis cannot dodge; a pair that
    clears zero in BOTH directions is starved a different way.  Written only;
    no branch reads it, so it cannot change which departures are offered.
    """

    deadline: float
    iterations_remaining: int
    iterations_used: int = 0
    proposals_seen: int = 0
    proposals_validated: int = 0
    reasons: Counter[str] = field(default_factory=Counter)
    native_rejections: Counter[str] = field(default_factory=Counter)
    direction_seen: Counter[str] = field(default_factory=Counter)
    direction_validated: Counter[str] = field(default_factory=Counter)

    def stage_summary(self) -> str:
        """One-line tally of where this pair's departure allowance went."""
        reasons = dict(sorted(self.reasons.items(), key=lambda kv: (-kv[1], kv[0])))
        rejections = dict(sorted(self.native_rejections.items(), key=lambda kv: (-kv[1], kv[0])))
        directions = {
            direction: f"{self.direction_validated[direction]}/{seen}"
            for direction, seen in sorted(self.direction_seen.items())
        }
        return (
            f"proposals={self.proposals_seen} "
            f"validated={self.proposals_validated} "
            f"departure_reasons={reasons} "
            f"departure_rejections={rejections} "
            f"departure_directions={directions}"
        )


@dataclass(frozen=True)
class DepartureProposal:
    prefix: tuple[JointStep, ...]
    across: tuple[int, int]
    outward: tuple[int, int]
    layer: int
    bend_steps: int
    #: :data:`DIRECTION_TOWARD_GOAL` if ``outward`` is the escape direction
    #: ``departure_proposals`` sorted first (the one that travels toward the
    #: pair's own goal); :data:`DIRECTION_AWAY_FROM_GOAL` otherwise.
    direction: str = DIRECTION_TOWARD_GOAL


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
    # Issue #5333 (MIPI_DAT1): the straight run above holds the pads' RAW
    # pitch all the way to the via -- correct when that pitch already sits
    # near the coupled target, but a pair whose pad pitch is much WIDER than
    # the target (and does not need the ``spread`` fan-out above because the
    # raw pitch already satisfies the mutual via pitch) never narrows at
    # all. Once such a straight run crosses ``effective_departure_radius``
    # cells from the start pads, the native search's spacing tolerance snaps
    # back to +-1 of the target and rejects it outright (see the
    # ``departure_planning`` module docstring's dated note for the full
    # measured mechanism). Offer an ADDITIONAL candidate below, per
    # direction/layer/bend, that narrows P and N toward the target spacing
    # right after any bend -- using the same one-cell-at-a-time asymmetric
    # moves the ``spread`` fan-out above already uses, just applied early
    # instead of only right before the via -- then continues the escape at
    # the now within-tolerance spacing. This only offers a different SHAPE;
    # every step, narrow or straight, is still checked against the same
    # copper/clearance/trail/via-pitch guards as every other proposal.
    narrow_target = max(finder.target_spacing_cells, finder.min_spacing_cells)
    narrow_amount = max(0, pitch - narrow_target)
    targets = [layer for layer in reversed(grid.get_routable_indices()) if layer != start_layer]
    if not targets and reasons is not None:
        # Every proposal ends on a paired via to another layer; a stack that
        # offers none cannot be departed from by construction.
        reasons["no_target_layer"] += 1
    for layer in targets:
        for direction_index, outward in enumerate(directions):
            direction = DIRECTION_TOWARD_GOAL if direction_index == 0 else DIRECTION_AWAY_FROM_GOAL
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
                yield DepartureProposal(
                    tuple(prefix), across, outward, layer, bend_steps, direction
                )

                if narrow_amount > 0:
                    # Reuse the already-built bend maneuver verbatim (it does
                    # not depend on the escape/spread geometry below it) and
                    # replace everything after it with a narrow-then-escape
                    # shape instead of the straight-then-spread one above.
                    bend_len = 3 * bend_steps if bend_steps else 0
                    narrow_prefix = list(prefix[:bend_len])
                    base_p = (px + ox * bend_steps, py + oy * bend_steps)
                    base_n = (nx + ox * bend_steps, ny + oy * bend_steps)
                    p_narrow, n_narrow = narrow_amount // 2, narrow_amount - narrow_amount // 2
                    narrow_prefix.extend(
                        (
                            base_p[0] + across[0] * j,
                            base_p[1] + across[1] * j,
                            start_layer,
                            *base_n,
                            start_layer,
                        )
                        for j in range(1, p_narrow + 1)
                    )
                    p_narrowed = (
                        base_p[0] + across[0] * p_narrow,
                        base_p[1] + across[1] * p_narrow,
                    )
                    narrow_prefix.extend(
                        (
                            *p_narrowed,
                            start_layer,
                            base_n[0] - across[0] * j,
                            base_n[1] - across[1] * j,
                            start_layer,
                        )
                        for j in range(1, n_narrow + 1)
                    )
                    n_narrowed = (
                        base_n[0] - across[0] * n_narrow,
                        base_n[1] - across[1] * n_narrow,
                    )
                    remaining = escape - bend_steps
                    narrow_prefix.extend(
                        (
                            p_narrowed[0] + ox * j,
                            p_narrowed[1] + oy * j,
                            start_layer,
                            n_narrowed[0] + ox * j,
                            n_narrowed[1] + oy * j,
                            start_layer,
                        )
                        for j in range(1, remaining + 1)
                    )
                    p_esc_n = (p_narrowed[0] + ox * remaining, p_narrowed[1] + oy * remaining)
                    n_esc_n = (n_narrowed[0] + ox * remaining, n_narrowed[1] + oy * remaining)
                    spread_n = max(0, math.ceil(finder._minimum_via_pitch_cells() - narrow_target))
                    p_spread_n, n_spread_n = spread_n // 2, spread_n - spread_n // 2
                    narrow_prefix.extend(
                        (
                            p_esc_n[0] - across[0] * j,
                            p_esc_n[1] - across[1] * j,
                            start_layer,
                            *n_esc_n,
                            start_layer,
                        )
                        for j in range(1, p_spread_n + 1)
                    )
                    p_via_n = (
                        p_esc_n[0] - across[0] * p_spread_n,
                        p_esc_n[1] - across[1] * p_spread_n,
                    )
                    narrow_prefix.extend(
                        (
                            *p_via_n,
                            start_layer,
                            n_esc_n[0] + across[0] * j,
                            n_esc_n[1] + across[1] * j,
                            start_layer,
                        )
                        for j in range(1, n_spread_n + 1)
                    )
                    n_via_n = (
                        n_esc_n[0] + across[0] * n_spread_n,
                        n_esc_n[1] + across[1] * n_spread_n,
                    )
                    narrow_prefix.append((*p_via_n, layer, *n_via_n, layer))
                    yield DepartureProposal(
                        tuple(narrow_prefix), across, outward, layer, bend_steps, direction
                    )


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
        budget.direction_seen[proposal.direction] += 1
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
        budget.direction_validated[proposal.direction] += 1
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
