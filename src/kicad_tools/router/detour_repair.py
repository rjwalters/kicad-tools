"""Targeted post-settle detour repair pass (Issue #9013).

The #9003 measurement (``docs/research/detour-ratio-fleet-measurement-9003.md``)
found 16/147 fleet connections (10.9%) routed above 1.25x their
alone-on-empty cost -- about 91.7 mm of upper-bound avoidable copper,
concentrated in the dense negotiated boards.  Nothing in the router
repairs a settled detour; this module is the conditional second half.

``--detour-repair`` (off by default) runs ONE bounded pass after the
negotiated loop settles, reusing the #9003 probe's alone-on-empty
records as the offender list.  Per offender net, worst ratio first:

1. Identify the placed wires actually blocking the offender's
   alone-board corridor with the existing frontier attribution
   (:meth:`NegotiatedRouter.find_blocking_nets_relaxed`, Issue #2274) --
   the relaxed path IS the corridor witness, and the committed nets
   sitting on it are the blockers.  No blockers means the detour is not
   copper-avoidable (keepout/pad geometry), so the net is skipped.
2. Lift the blockers plus the offender
   (:meth:`NegotiatedRouter.rip_up_nets`).
3. Route the offender first, onto its short corridor (present cost is
   low: the corridor is empty, so the shortest path is the cheapest).
4. Re-land every lifted net at a soft present cost (the
   ``sequential_ripup`` #5894 precedent).
5. Accept the rearrangement only if the whole-board
   :class:`~kicad_tools.router.checkpoint.RouteScore` is STRICTLY better
   (``is_better_than``, the #5945 lexicographic accept gate) AND the
   involved nets' copper strictly shrank.  The conjunction matters: the
   score is lexicographic, so a rearrangement that improves a
   higher-priority term (overflow, clearance) can still ADD copper --
   the fleet pass of 2026-11-17 caught board 05's CPL attempt with the
   score already accepting and involved copper 265.30 -> 265.80 mm,
   which is exactly what the issue's ground rule ("keep ... only if ...
   total copper (of the involved nets) shrank") forbids, so the copper
   comparison is enforced explicitly, not trusted to fall out of the
   sort key.  Any losing outcome rolls the attempt back to the exact
   pre-attempt copper (the #3470 transaction discipline shared with
   targeted rip-up).

Termination is structural: one attempt per offender net, no
re-queueing.  A repair that would strand any net is rolled back by the
gate, so the pass can only ever shorten copper -- never regress
connectivity.  Reuses only in-tree primitives and the behaviour
description; WeaveEngine (GPL-3.0) is ideas-only provenance.

Out of scope by the issue: ``redundant_copper_detour`` (#8218), the
``--report-detour-ratio`` probe contract (pinned by
``tests/test_detour_ratio_report_9003.py``), default-on enablement and
multi-round repair.  The pass fires on every settled-board path the real
``kct route`` dispatch reaches: the negotiated loop
(``Autorouter.route_all_negotiated``, which ``route_all_two_phase``'s
no-blocks fallback also lands in) and the block-aware two-phase tail
(``run_detour_repair_pass_from_autorouter``, the path dense boards --
the ones the measurement flagged -- actually take).  The one dispatched
loop it does NOT settle after is ``--ripup-strategy sequential-n1`` (the
KRT prototype replaces the negotiated loop wholesale); that combination
warns loudly at the dispatch site instead of silently dropping the flag
(the #5908 lesson).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.analysis.detour_ratio import (
    DETOUR_ENDPOINT_MATCH_TOLERANCE_MM,
    DETOUR_RATIO_THRESHOLD,
    endpoint_pair_distance,
    route_path_ends,
)
from kicad_tools.cli.progress import flush_print

from . import wall_deadline
from .checkpoint import RouteScore
from .observability import validate_net_connectivity

if TYPE_CHECKING:
    from .primitives import Pad, Route

#: Present-cost factor for the offender's own re-route.  The corridor is
#: empty (its occupants were just lifted), so the shortest path is the
#: cheapest one at any factor; a low factor keeps residual congestion
#: pricing from scaring the search off the corridor.
DETOUR_REPAIR_OFFENDER_COST = 1.0

#: Present-cost factor for a lifted net's re-land -- soft, not flat
#: forbidden (the ``sequential_ripup`` #5894 "soft cost on the ripped
#: corridor" precedent): the displaced net may still reclaim the
#: corridor at a premium if it is genuinely its best option, though the
#: accept gate then judges the whole arrangement.
DETOUR_REPAIR_LIFTED_COST = 5.0

__all__ = [
    "DETOUR_REPAIR_LIFTED_COST",
    "DETOUR_REPAIR_OFFENDER_COST",
    "RepairOutcome",
    "run_detour_repair_pass",
    "run_detour_repair_pass_from_autorouter",
]


@dataclass(frozen=True)
class RepairOutcome:
    """One offender net's repair result, for the per-offender log."""

    net: int
    net_name: str
    ratio: float
    verdict: str
    """``kept``, ``rolled back`` or ``skipped``."""
    reason: str = ""


@dataclass(frozen=True)
class _Offender:
    """One offender net: its worst connection and the drawn Route for it."""

    net: int
    net_name: str
    ratio: float
    alone_mm: float
    drawn_mm: float


def _route_mm(ar: Any, route: Any) -> float:
    """A route's copper length in mm under the kicad-cli-exact model."""
    model = ar._kicad_length_model(ar.grid.num_layers)
    return float(model.route_length(route))


def _find_offenders(
    ar: Any,
    net_routes: dict[int, list[Route]],
) -> tuple[list[_Offender], float]:
    """Pair the #9003 probe records against the settled routes.

    Returns the offender nets (worst ratio first, net id as the
    deterministic tie-break) and the total drawn copper of every
    offending connection in mm.  The pairing mirrors
    ``Autorouter.finalize_detour_probe_records`` (greedy nearest path
    ends under ``DETOUR_ENDPOINT_MATCH_TOLERANCE_MM``) but maps each
    record to its settled Route and net id, which the repair needs and
    the report does not.  A probe connection the settled route did not
    reproduce (``unmatched``) is not a repair candidate -- there is no
    drawn detour to shorten.
    """
    records = getattr(ar, "detour_probe_records", None)
    if not records:
        return [], 0.0
    by_net: dict[str, list[Any]] = {}
    for rec in records:
        if rec.probe_no_path or rec.alone_mm is None or rec.endpoints is None:
            continue
        by_net.setdefault(rec.net_name, []).append(rec)

    drawn_by_net: dict[str, list[Any]] = {}
    for route in ar.routes:
        if getattr(route, "is_escape", False) or not route.segments:
            continue
        drawn_by_net.setdefault(route.net_name, []).append(route)

    offenders_by_net: dict[int, _Offender] = {}
    copper_mm = 0.0
    for net_name, recs in by_net.items():
        drawn = drawn_by_net.get(net_name, [])
        candidates: list[tuple[float, int, int]] = []
        for pi, rec in enumerate(recs):
            for di, route in enumerate(drawn):
                ends = route_path_ends(route)
                if ends is None:
                    continue
                cost = endpoint_pair_distance(rec.endpoints, ends)
                if cost <= DETOUR_ENDPOINT_MATCH_TOLERANCE_MM:
                    candidates.append((cost, pi, di))
        candidates.sort()
        taken_p: set[int] = set()
        taken_d: set[int] = set()
        for _cost, pi, di in candidates:
            if pi in taken_p or di in taken_d:
                continue
            taken_p.add(pi)
            taken_d.add(di)
            rec = recs[pi]
            route = drawn[di]
            # The pass measures the drawn side itself: the record's
            # ``drawn_mm`` is filled only by the CLI's post-route report
            # finalize, which may not have run (and is stale here anyway --
            # earlier repairs in the same pass already changed the copper).
            drawn_mm = _route_mm(ar, route)
            alone_mm = float(rec.alone_mm or 0.0)
            ratio = drawn_mm / alone_mm if alone_mm > 0.0 else None
            if ratio is None or ratio <= DETOUR_RATIO_THRESHOLD:
                continue
            if not net_routes.get(route.net):
                # Not in the negotiated loop's committed set -- copper the
                # repair cannot rip (infrastructure fragment); leave it.
                continue
            copper_mm += drawn_mm
            existing = offenders_by_net.get(route.net)
            # One attempt per NET: keep the worst connection as its face.
            if existing is None or ratio > existing.ratio:
                offenders_by_net[route.net] = _Offender(
                    net=route.net,
                    net_name=net_name,
                    ratio=ratio,
                    alone_mm=alone_mm,
                    drawn_mm=drawn_mm,
                )
    offenders = sorted(offenders_by_net.values(), key=lambda o: (-o.ratio, o.net))
    return offenders, copper_mm


def _board_route_score(
    ar: Any,
    net_routes: dict[int, list[Route]],
    pads_by_net: dict[int, list[Pad]],
    neg_router: Any,
) -> RouteScore:
    """Score the whole board with the #5945 lexicographic RouteScore.

    Same metric sources ``route_all_negotiated``'s own best-state
    comparator uses: grid overflow, the three live clearance scans
    (seg-via #3002, via-seg #3020, seg-seg #3433), pad-to-pad
    connectivity (#3117) discounted by demotion-doomed overlaps
    (#3588).  The whole-board score subsumes an involved-nets-only
    copper comparison: nets-complete dominates wirelength in the sort
    key, so a rearrangement that strands any net can never win.
    """
    from .core import IterationMetrics

    clearance = ar.rules.trace_clearance
    extra = ar._collect_extra_routes_for_revalidation(net_routes)
    seg_via = neg_router.find_nets_with_segment_via_violations(
        net_routes, trace_clearance=clearance, extra_routes=extra
    )
    via_seg = neg_router.find_nets_with_via_segment_violations(
        net_routes, trace_clearance=clearance, extra_routes=extra
    )
    seg_seg = neg_router.find_nets_with_segment_segment_violations(
        net_routes, trace_clearance=clearance, extra_routes=extra
    )
    all_routes: list[Route] = []
    for routes in net_routes.values():
        all_routes.extend(routes)
    net_pads = {n: p for n, p in pads_by_net.items() if p and len(p) >= 2}
    conn = validate_net_connectivity(all_routes, net_pads)
    connected = {n for n, info in conn.items() if info.get("connected")}
    overlap_pairs = neg_router.find_segment_segment_violation_pairs(
        net_routes,
        trace_clearance=clearance,
        extra_routes=extra,
        copper_overlap_only=True,
    )
    overlapping = {n for pair in overlap_pairs for n in pair}
    metrics = IterationMetrics(
        iteration=-1,
        routed_count=sum(1 for routes in net_routes.values() if routes),
        overflow=int(ar.grid.get_total_overflow()),
        clearance_violations=len(seg_via) + len(via_seg) + len(seg_seg),
        nets_fully_connected=len(connected),
        demotable_connected=len(connected & overlapping),
    )
    return RouteScore.from_routes(list(ar.routes), metrics)


def _discard_partial(ar: Any, routes: list[Route]) -> None:
    """Unmark copper a search produced but the transaction never committed.

    ``_route_net_negotiated`` marks every Route it returns (incl. partial
    results) on the grid through its commit closure; the caller's commit
    (usage ledger + ``routes`` list) is separate.  A rolled-back partial
    must leave neither ledger touched by copper that was never committed.
    """
    for route in routes or []:
        ar.grid.unmark_route_usage(route)
        ar.grid.unmark_route(route)


def _commit(ar: Any, net_routes: dict[int, list[Route]], net: int, routes: list[Route]) -> None:
    """Commit a re-routed net exactly as the negotiated loop does."""
    net_routes[net] = routes
    for route in routes:
        ar.grid.mark_route_usage(route)
        ar.routes.append(route)
    if hasattr(ar.router, "update_layer_fill_ratios"):
        ar.router.update_layer_fill_ratios()


def _rollback(
    ar: Any,
    net_routes: dict[int, list[Route]],
    involved: list[int],
    original: dict[int, list[Route]],
    failures_before: int,
) -> None:
    """Restore ``involved`` nets to the exact pre-attempt copper (#3470).

    Unmarks whatever the attempt placed, then re-marks the original Route
    objects verbatim on both ledgers and the ``routes`` master list.  The
    re-land deliberately does NOT opt into the #5891 pad-access
    invariant: it restores already-committed copper, and refusing that
    would strand the very net the rollback is saving.
    """
    for net in involved:
        for route in list(net_routes.get(net, [])):
            ar.grid.unmark_route_usage(route)
            ar.grid.unmark_route(route)
            if route in ar.routes:
                ar.routes.remove(route)
        restored = list(original.get(net, []))
        net_routes[net] = restored
        for route in restored:
            ar._mark_route(route)
            ar.grid.mark_route_usage(route)
            if route not in ar.routes:
                ar.routes.append(route)
    del ar.routing_failures[failures_before:]
    if hasattr(ar.router, "update_layer_fill_ratios"):
        ar.router.update_layer_fill_ratios()


def _repair_net(
    ar: Any,
    offender: _Offender,
    net_routes: dict[int, list[Route]],
    pads_by_net: dict[int, list[Pad]],
    neg_router: Any,
    per_net_timeout: float | None,
) -> RepairOutcome:
    """One bounded repair attempt for one offender net (worst-first order).

    The attempt is a TRANSACTION: it commits only when the offender
    re-routes onto its corridor with zero failed edges, every lifted net
    re-lands, the whole-board score is strictly better, and the involved
    nets' copper strictly shrank (the score alone can accept on a
    higher-priority term while copper grows -- see the module docstring).
    Any other outcome restores the exact pre-attempt state.
    """
    net = offender.net
    scores = neg_router.find_blocking_nets_relaxed(
        [net], pads_by_net, per_net_timeout=per_net_timeout
    )
    blockers = sorted(b for b in scores if net_routes.get(b))
    if not blockers:
        return RepairOutcome(
            net,
            offender.net_name,
            offender.ratio,
            "skipped",
            "no committed copper on the alone-path corridor (detour is not avoidable)",
        )
    involved = [net, *blockers]
    original: dict[int, list[Route]] = {n: list(net_routes.get(n, [])) for n in involved}
    failures_before = len(ar.routing_failures)
    before = _board_route_score(ar, net_routes, pads_by_net, neg_router)

    def _restore() -> None:
        _rollback(ar, net_routes, involved, original, failures_before)

    neg_router.rip_up_nets(list(involved), net_routes, ar.routes)

    failed_edges: list[tuple[Pad, Pad]] = []
    routes = ar._route_net_negotiated(
        net,
        DETOUR_REPAIR_OFFENDER_COST,
        per_net_timeout=per_net_timeout,
        failure_callback=lambda src, tgt: failed_edges.append((src, tgt)),
    )
    if failed_edges or not routes:
        _discard_partial(ar, routes)
        _restore()
        return RepairOutcome(
            net,
            offender.net_name,
            offender.ratio,
            "rolled back",
            "offender did not re-route onto its corridor",
        )
    _commit(ar, net_routes, net, routes)

    for blocker in blockers:
        lifted = ar._route_net_negotiated(
            blocker,
            DETOUR_REPAIR_LIFTED_COST,
            per_net_timeout=per_net_timeout,
            failure_callback=lambda src, tgt: failed_edges.append((src, tgt)),
        )
        if failed_edges or not lifted:
            _discard_partial(ar, lifted)
            _restore()
            return RepairOutcome(
                net,
                offender.net_name,
                offender.ratio,
                "rolled back",
                f"lifted net {ar.net_names.get(blocker, blocker)} did not re-land",
            )
        _commit(ar, net_routes, blocker, lifted)

    after = _board_route_score(ar, net_routes, pads_by_net, neg_router)
    if not after.is_better_than(before):
        _restore()
        return RepairOutcome(
            net,
            offender.net_name,
            offender.ratio,
            "rolled back",
            "board score not strictly better "
            f"(before {before.describe()} vs after {after.describe()})",
        )
    involved_before_mm = sum(_route_mm(ar, r) for rs in original.values() for r in rs)
    involved_after_mm = sum(_route_mm(ar, r) for n in involved for r in net_routes.get(n, []))
    if involved_after_mm >= involved_before_mm:
        # The lexicographic gate can accept on a higher-priority term (less
        # overflow, fewer violations) while the rearrangement still ADDS
        # copper -- the issue's ground rule forbids keeping that.
        _restore()
        return RepairOutcome(
            net,
            offender.net_name,
            offender.ratio,
            "rolled back",
            "involved copper grew "
            f"{involved_before_mm:.2f}mm -> {involved_after_mm:.2f}mm "
            "(kept only when it shrinks)",
        )
    offender_before_mm = sum(_route_mm(ar, r) for r in original.get(net, []))
    offender_after_mm = sum(_route_mm(ar, r) for r in routes)
    return RepairOutcome(
        net,
        offender.net_name,
        offender.ratio,
        "kept",
        f"copper {offender_before_mm:.2f}mm -> {offender_after_mm:.2f}mm "
        f"(involved {involved_before_mm:.2f} -> {involved_after_mm:.2f}mm)",
    )


def run_detour_repair_pass_from_autorouter(
    ar: Any,
    *,
    per_net_timeout: float | None = None,
) -> list[RepairOutcome]:
    """The repair pass on a settle path that has no negotiated loop state.

    ``route_all_two_phase`` (every escape-routed board's detailed loop --
    the boards the #9003 measurement actually flagged) settles without an
    ``Autorouter.route_all_negotiated`` frame, so there is no loop-owned
    ``net_routes`` / ``pads_by_net`` / ``neg_router`` triple to hand the
    pass.  This adapter rebuilds the same shapes from Autorouter state:
    committed non-escape copper grouped per net (escape stubs stay
    rip-up-exempt exactly as in the negotiated loop, whose ``net_routes``
    never contains them), the escape-aware pad mapping the two-phase
    router itself uses, and a fresh :class:`NegotiatedRouter` over the
    live grid (the same construction the negotiated loop uses).
    """
    from .algorithms.negotiated import NegotiatedRouter

    neg_router = NegotiatedRouter(
        ar.grid,
        ar.router,
        ar.rules,
        ar.net_class_map,
        congestion_estimator=ar._ensure_congestion_estimator(),
    )
    net_routes: dict[int, list[Route]] = {}
    for route in ar.routes:
        if getattr(route, "is_escape", False) or not route.segments:
            continue
        net_routes.setdefault(route.net, []).append(route)
    pads_by_net: dict[int, list[Pad]] = {}
    for net, pads in ar.nets.items():
        if pads and len(pads) >= 2:
            pads_by_net[net] = [ar._escape_pad_overrides.get(p, ar.pads[p]) for p in pads]
    return run_detour_repair_pass(
        ar,
        net_routes=net_routes,
        pads_by_net=pads_by_net,
        neg_router=neg_router,
        per_net_timeout=per_net_timeout,
    )


def run_detour_repair_pass(
    ar: Any,
    *,
    net_routes: dict[int, list[Route]],
    pads_by_net: dict[int, list[Pad]],
    neg_router: Any,
    per_net_timeout: float | None = None,
) -> list[RepairOutcome]:
    """The post-settle detour repair pass (Issue #9013; ``--detour-repair``).

    Called once from ``route_all_negotiated`` after the loop settles,
    behind the opt-in ``router.detour_repair`` attribute.  Offenders come
    from the #9003 alone-on-empty probe records (the probe pre-pass runs
    whenever this flag is on, even without ``--report-detour-ratio``).
    Returns the per-offender outcomes; they are also stashed on
    ``ar.detour_repair_outcomes``.

    Bounded and safe by construction: one attempt per offender net, each
    transactional against the whole-board accept gate, and the whole
    pass stands down once the excursion wall deadline (if any) is spent.
    """
    outcomes: list[RepairOutcome] = []
    ar.detour_repair_outcomes = outcomes
    if not getattr(ar, "detour_repair", False):
        return outcomes
    offenders, copper_before = _find_offenders(ar, net_routes)
    if not offenders:
        flush_print(
            "  Detour repair: no connection above "
            f"{DETOUR_RATIO_THRESHOLD:.2f}x its alone-on-empty cost; nothing to do"
        )
        return outcomes
    if wall_deadline.expired():
        flush_print(
            "  Detour repair skipped for "
            f"{len(offenders)} offender net(s): excursion wall deadline spent"
        )
        outcomes.extend(
            RepairOutcome(o.net, o.net_name, o.ratio, "skipped", "wall deadline spent")
            for o in offenders
        )
        return outcomes

    flush_print(
        f"\n=== Detour Repair ===\n"
        f"  {len(offenders)} offender net(s) above {DETOUR_RATIO_THRESHOLD:.2f}x "
        f"alone-cost ({copper_before:.2f}mm); one attempt each, worst first"
    )
    kept = 0
    rolled_back = 0
    skipped = 0
    started = time.time()
    for idx, offender in enumerate(offenders, start=1):
        if wall_deadline.expired():
            outcome = RepairOutcome(
                offender.net, offender.net_name, offender.ratio, "skipped", "wall deadline spent"
            )
            skipped += 1
            outcomes.append(outcome)
            flush_print(f"  [{idx}/{len(offenders)}] {offender.net_name}: skipped (wall deadline)")
            continue
        t0 = time.time()
        outcome = _repair_net(ar, offender, net_routes, pads_by_net, neg_router, per_net_timeout)
        outcomes.append(outcome)
        if outcome.verdict == "kept":
            kept += 1
        elif outcome.verdict == "rolled back":
            rolled_back += 1
        else:
            skipped += 1
        flush_print(
            f"  [{idx}/{len(offenders)}] {outcome.net_name} "
            f"(ratio {outcome.ratio:.2f}x): {outcome.verdict}"
            f"{f' ({outcome.reason})' if outcome.reason else ''} "
            f"[{time.time() - t0:.1f}s]"
        )

    _, copper_after = _find_offenders(ar, net_routes)
    flush_print(
        f"  Detour repair: {kept} kept, {rolled_back} rolled back, {skipped} skipped "
        f"in {time.time() - started:.1f}s; offender copper "
        f"{copper_before:.2f}mm -> {copper_after:.2f}mm"
    )
    return outcomes
