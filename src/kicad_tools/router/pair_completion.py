"""Bounded terminal construction and qualification of uncommitted pair bodies."""

from __future__ import annotations

import copy
import itertools
import math
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .construction_validation import constructed_pair_geometry_issue
from .diffpair_detection import DetectedPair, DetectionSource
from .diffpair_length_tuning import tune_diff_pair_skew
from .match_group_length import MatchGroupTracker

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .body_planning import PairBody
    from .diffpair_routing import CoupledPathfinder, DiffPairRouter
    from .primitives import Pad, Route

# Issue #5333: the full unrestricted retry in ``_tails_with_widen_fallback``
# is far more expensive than the fast single-site path -- it walks
# ``_layer_return_tails``'s whole radius x direction lattice with real
# ``_synthesize_tail`` cell searches per candidate. Measured on TMDS_D1
# (Board07, real congestion near U4's BGA-49 field): letting every
# geometrically-clear body widen without limit spent the pair's entire
# per-pair wall-clock window on the FIRST landing's site alone (landings=1
# of a possible 4, bodies=136 of a possible 400) -- so cheaper,
# genuinely-different landing candidates from
# :func:`terminal_planning.landing_proposals` never got a turn. This caps
# the COUNT of full-lattice retries, not the wall clock -- ``deadline``
# still bounds every individual search -- so the pair's UNCHANGED
# per-pair budget stays split across landings instead of being spent by one.
WIDEN_FALLBACK_ATTEMPTS: int = int(os.environ.get("KCT_WIDEN_FALLBACK_ATTEMPTS", "40"))


@dataclass
class WidenBudget:
    """Shared, pair-scoped cap on :func:`_tails_with_widen_fallback` retries.

    One instance is created per pair construction attempt and threaded
    through every landing and body so no single structurally-blocked
    landing can spend the whole pair's allowance on repeated full-lattice
    searches. ``spent`` is a diagnostic count only; nothing reads it to
    change behavior.
    """

    remaining: int = WIDEN_FALLBACK_ATTEMPTS
    spent: int = 0


def _tails_with_widen_fallback(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    head: Pad,
    goal: Pad,
    partner: Route,
    body: Route,
    *,
    deadline: float,
    prefer_shortest_approach: bool,
    reserved_routes: tuple[Route, ...],
    allowed_via_sites: frozenset[tuple[int, int]] | None,
    widen_budget: WidenBudget | None = None,
) -> Iterator[Route]:
    """Layer-return tails, widening to the full site lattice if the plan is dead.

    ``allowed_via_sites`` is normally a single pre-vetted, mutually-clear via
    cell from :func:`terminal_planning.landing_proposals` -- fast, because it
    skips re-deriving the site, but it only proves that ONE cell is clear of
    committed/reserved copper. It never proves a legal PLANAR approach
    (:meth:`DiffPairRouter._synthesize_tail`) actually reaches it through
    real congestion. Issue #5333's TMDS_D1 measurement: 212 geometrically
    clear bodies, both approach orderings, 848 ``no_tail`` rejections --
    EVERY completion attempt failed this way, meaning the single planned
    site near U4's BGA-49 field was never reachable, for any body shape.

    When the restricted search yields nothing at all, retry once with
    ``allowed_via_sites=None`` so :meth:`DiffPairRouter._layer_return_tails`
    runs its own full site lattice -- still gated by every existing
    clearance, hole-spacing, and partner-copper check; nothing here relaxes
    a rule. A leg whose planned site already works never reaches the
    fallback, so this costs nothing extra for pairs that already construct
    (MIPI_CLK, TMDS_D0). Both passes share the caller's existing deadline --
    no budget is extended. ``widen_budget``, if supplied, caps how many of
    these full-lattice retries a whole pair construction may spend (see
    :data:`WIDEN_FALLBACK_ATTEMPTS`); once exhausted, later calls fall back
    to the plain restricted search (still correct, just no longer widened).

    An explicitly EMPTY ``allowed_via_sites`` (as opposed to ``None``) means
    the caller already determined no candidate site exists at all and must
    stay a hard ``no_tail`` -- widening it would silently convert "terminal
    planning found zero legal barrels" into an unbounded per-attempt search
    and defeat the whole point of the earlier bounded stage.
    """
    if allowed_via_sites is None:
        yield from router._layer_return_tails(
            finder,
            head,
            goal,
            partner,
            body,
            deadline=deadline,
            prefer_shortest_approach=prefer_shortest_approach,
            allowed_via_sites=None,
            reserved_routes=reserved_routes,
        )
        return
    if not allowed_via_sites:
        return
    yielded = False
    for tail in router._layer_return_tails(
        finder,
        head,
        goal,
        partner,
        body,
        deadline=deadline,
        prefer_shortest_approach=prefer_shortest_approach,
        allowed_via_sites=allowed_via_sites,
        reserved_routes=reserved_routes,
    ):
        yielded = True
        yield tail
    if yielded:
        return
    if widen_budget is not None:
        if widen_budget.remaining <= 0:
            return
        widen_budget.remaining -= 1
        widen_budget.spent += 1
    yield from router._layer_return_tails(
        finder,
        head,
        goal,
        partner,
        body,
        deadline=deadline,
        prefer_shortest_approach=prefer_shortest_approach,
        allowed_via_sites=None,
        reserved_routes=reserved_routes,
    )


def qualify_constructed_pair(
    router: DiffPairRouter,
    finder: CoupledPathfinder,
    pair,
    pads: tuple[Pad, Pad, Pad, Pad],
    candidate: tuple[Route, Route],
    *,
    intra_pair_clearance: float | None = None,
    board_thickness_mm: float,
    num_copper_layers: int,
    deadline: float,
    reserved_routes: tuple[Route, ...] = (),
    reasons: Counter[str] | None = None,
) -> tuple[Route, Route] | None:
    """Apply the physical-skew / authored-coupling / post-tune geometry gate
    every constructed pair candidate must clear, or return ``None``.

    Shared by :func:`complete_pair_body`'s tail-completion loop (a candidate
    assembled from a departure body plus two synthesized layer-return tails)
    and :func:`pair_construction._corridor_guided_departures`'s raw native
    result (#5333: the corridor-guided search hands back a fully-assembled
    pair straight from the native joint search, bypassing tail synthesis --
    and therefore this gate -- entirely unless a caller applies it
    explicitly). Unlike the geometric-shape lattice's symmetric moves, a
    departure-prefix-seeded corridor search is NOT guaranteed to produce a
    length-matched pair by construction: measured on Board07's MIPI_DAT0
    (seed 42, native ABI 31), an unqualified raw corridor result differed in
    P/N physical length by 1.75mm against the net class's 0.05mm authored
    skew tolerance -- a real violation this function exists to catch before
    a caller can label the pair ``coupled-ok``.

    ``reasons``, if supplied, is the same diagnostic tally
    :func:`complete_pair_body` uses -- ``constructed_pair_geometry_issue``'s
    reason token for a colliding candidate, ``skew_tolerance`` /
    ``coupling_threshold`` for one that clears geometry but misses the
    authored net-class limit, or ``post_tune_<reason>`` if tuning itself
    reintroduces a collision.

    ``intra_pair_clearance``, if omitted, is derived from the net class's
    own ``effective_intra_pair_clearance()`` -- callers that already have it
    on hand (``complete_pair_body`` computes it once for its whole tail
    loop) may pass it through instead of paying that lookup again per
    candidate.
    """
    if reasons is None:
        reasons = Counter()
    grid = finder.grid
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None or not math.isfinite(board_thickness_mm) or board_thickness_mm <= 0:
        return None
    if intra_pair_clearance is None:
        intra_pair_clearance = nc.effective_intra_pair_clearance()
    issue = constructed_pair_geometry_issue(
        router,
        finder,
        candidate[0],
        candidate[1],
        pads,
        intra_pair_clearance=intra_pair_clearance,
        deadline=deadline,
        reserved_routes=reserved_routes,
    )
    if issue is not None:
        reasons[issue] += 1
        return None
    corpus = {r.net: r for r in [*grid.routes, *router.autorouter.routes]}
    # Several reservations can belong to one future net (departure copper and
    # landing barrel). Keep all of them in the tuner's per-net view without
    # mutating the caller's routes.
    reserved_nets: set[int] = set()
    for reservation in reserved_routes:
        if reservation.net not in reserved_nets:
            reserved_nets.add(reservation.net)
            if reservation.net not in corpus:
                corpus[reservation.net] = copy.deepcopy(reservation)
                continue
            corpus[reservation.net] = copy.deepcopy(corpus[reservation.net])
        corpus[reservation.net].segments.extend(reservation.segments)
        corpus[reservation.net].vias.extend(reservation.vias)
    corpus.update({r.net: r for r in candidate})
    p, n, _ = tune_diff_pair_skew(
        DetectedPair(pair=pair, source=DetectionSource.EXPLICIT),
        corpus,
        tolerance_mm=nc.effective_skew_tolerance(),
        intra_pair_clearance_mm=intra_pair_clearance,
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
        for r in (p, n)
    ]
    if abs(lengths[0] - lengths[1]) > nc.effective_skew_tolerance():
        reasons["skew_tolerance"] += 1
        return None
    if (
        min(
            router._tail_coupled_fraction(p, n.segments),
            router._tail_coupled_fraction(n, p.segments),
        )
        < nc.effective_coupled_continuity_threshold()
    ):
        reasons["coupling_threshold"] += 1
        return None
    final_issue = constructed_pair_geometry_issue(
        router,
        finder,
        p,
        n,
        pads,
        intra_pair_clearance=intra_pair_clearance,
        deadline=deadline,
        reserved_routes=reserved_routes,
    )
    if final_issue is not None:
        reasons[f"post_tune_{final_issue}"] += 1
        return None
    return p, n


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
    reserved_routes: tuple[Route, ...] = (),
    reasons: Counter[str] | None = None,
    widen_budget: WidenBudget | None = None,
) -> tuple[Route, Route] | None:
    """Try at most ten tails per half in each order within the shared deadline.

    Planned sites only restrict the ordinary layer-return search. All live
    copper, explicit future reservations and the uncommitted partner remain
    obstacles. Reservations are never added to either committed route list. Full geometry is
    checked before and after physical-length tuning; coupling and skew must
    meet the authored net class. This function never commits route occupancy.

    ``reasons`` is an optional diagnostic tally, never an input: it
    histograms why each attempted (tail, other_tail) candidate was rejected
    -- ``no_tail`` when the layer-return search offered nothing for a side,
    the ``constructed_pair_geometry_issue`` reason token for a colliding
    candidate, ``skew_tolerance`` / ``coupling_threshold`` for a candidate
    that qualifies geometrically but not physically, or the post-tune
    geometry reason if tuning itself introduces a collision.  A caller that
    reaches ``bodies=N geom_rejected=0`` with completions attempted still
    needs this to tell "no legal tail exists" apart from "every tail passes
    the pad-clearance gate but fails skew".

    ``widen_budget``, if supplied, is shared across every body this caller
    tries for the whole pair construction and caps how many full-lattice
    widen retries (see :func:`_tails_with_widen_fallback`) any of them may
    spend, so one structurally-blocked landing cannot exhaust the pair's
    wall clock before a different landing gets a turn.
    """
    if reasons is None:
        reasons = Counter()
    if time.monotonic() >= deadline:
        return None
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None or not math.isfinite(board_thickness_mm) or board_thickness_mm <= 0:
        return None
    grid = finder.grid
    reserved_routes = tuple(r for r in reserved_routes if r.net not in (pads[0].net, pads[2].net))
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
        with router._shadow_foreign_copper(*reserved_routes, *originals):
            tails = _tails_with_widen_fallback(
                router,
                finder,
                heads[first],
                goals[first],
                originals[second],
                originals[first],
                deadline=deadline,
                prefer_shortest_approach=prefer_shortest_approach,
                reserved_routes=reserved_routes,
                allowed_via_sites=allowed_via_sites[first]
                if allowed_via_sites is not None
                else None,
                widen_budget=widen_budget,
            )
            found_first_tail = False
            for tail in itertools.islice(tails, 10):
                if time.monotonic() >= deadline:
                    return None
                found_first_tail = True
                first_route = copy.deepcopy(originals[first])
                first_route.segments.extend(tail.segments)
                first_route.vias.extend(tail.vias)
                with router._shadow_foreign_copper(
                    *reserved_routes, first_route, originals[second]
                ):
                    other_tails = _tails_with_widen_fallback(
                        router,
                        finder,
                        heads[second],
                        goals[second],
                        first_route,
                        originals[second],
                        deadline=deadline,
                        prefer_shortest_approach=prefer_shortest_approach,
                        reserved_routes=reserved_routes,
                        allowed_via_sites=(
                            allowed_via_sites[second] if allowed_via_sites is not None else None
                        ),
                        widen_budget=widen_budget,
                    )
                    found_other_tail = False
                    for other_tail in itertools.islice(other_tails, 10):
                        if time.monotonic() >= deadline:
                            return None
                        found_other_tail = True
                        candidate = list(copy.deepcopy(originals))
                        candidate[first] = copy.deepcopy(first_route)
                        candidate[second].segments.extend(other_tail.segments)
                        candidate[second].vias.extend(other_tail.vias)
                        qualified = qualify_constructed_pair(
                            router,
                            finder,
                            pair,
                            pads,
                            (candidate[0], candidate[1]),
                            intra_pair_clearance=intra,
                            board_thickness_mm=board_thickness_mm,
                            num_copper_layers=num_copper_layers,
                            deadline=deadline,
                            reserved_routes=reserved_routes,
                            reasons=reasons,
                        )
                        if qualified is not None:
                            return qualified
                        if time.monotonic() >= deadline:
                            return None
                    if not found_other_tail:
                        reasons["no_tail"] += 1
            if not found_first_tail:
                reasons["no_tail"] += 1
    return None
