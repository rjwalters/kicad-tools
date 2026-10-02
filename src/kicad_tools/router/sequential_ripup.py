"""KRT-style sequential N+1 rip-up routing strategy (Issue #5894).

Part of #5787 (step 3), Epic #5784 Phase 3. An ALTERNATIVE outer control
loop to :meth:`kicad_tools.router.core.Autorouter.route_all_negotiated`'s
default whole-set PathFinder-style loop -- not an addition to it, and not a
bigger version of the rip-up helpers already in
:mod:`kicad_tools.router.algorithms.negotiated` (``rip_up_nets``,
``targeted_ripup``, ``neighborhood_ripup``, ``find_blocking_nets_relaxed``,
``escape_local_minimum``, ...). Those all operate *inside* the negotiated
loop's many-nets-at-once iteration. This module routes nets ONE AT A TIME,
in order, and only reaches for rip-up when a net's own attempt fails --
reusing the existing low-level primitives (grid mark/unmark via
:meth:`Autorouter._route_net_negotiated`, blocker attribution via
:class:`NegotiatedRouter.find_blocking_nets_relaxed`) rather than
re-deriving them, per the issue's own guidance.

Off by default: selected only via ``kct route --ripup-strategy
sequential-n1`` (see ``src/kicad_tools/cli/parser.py``), which sets
``Autorouter._ripup_strategy`` and is read once, unconditionally, at the top
of ``route_all_negotiated`` -- see that method's dispatch and
``cli/route_cmd.py:_apply_ripup_strategy`` for why the flag is plumbed as a
router attribute rather than a kwarg threaded through every one of
``route_all_negotiated``'s many internal call sites (the #5908 lesson from
step 2's ``--order-method`` silent no-op).

Algorithm per net (see ``docs/research/kct-route-ripup-experiment.md`` for
measurements and the honest list of approximations below):

1. Route the net directly at a HIGH present-cost factor (:data:`DEFAULT_HARD_COST`)
   -- effectively hard, since any already-occupied cell is prohibitively
   expensive to share. "Fully connected" means ZERO failed RSMT edges, read
   from ``_route_net_negotiated``'s ``failure_callback`` (#2425), not from a
   route-count comparison -- see :func:`_route_net` for why the obvious count
   proxy silently declares a partial net successful and so stops the rip-up
   escalation below from ever engaging.
2. On failure, **frontier-cell blocker attribution**: temporarily unblock
   every routed net's cells and re-run THIS net's own search
   (:meth:`NegotiatedRouter.find_blocking_nets_relaxed`, Issue #2274). The
   relaxed search's resulting path is the failing search's own frontier
   reaching the goal; the committed-net cells sitting on that path are the
   nets that were actually in its way -- not a straight-line guess.
3. **N+1 escalation with the slot-0 guard**: rip up the single
   highest-scoring blocker (N=1) and retry. If that still fails, escalate to
   the top 2, then top 3 (capped at ``max_escalation``). Only ALREADY-PLACED
   nets (i.e. ones this pass has committed at least one route for) are
   eligible rip-up targets -- a net not yet reached in the net order can
   never be ripped (ripping it would be a no-op that silently burns an
   escalation slot), which also structurally bounds how much of the board a
   single failing net can ever touch.
4. **Soft cost on the ripped corridor**: once the failed net re-routes
   through the freed cells, the displaced siblings are rerouted at a
   MODERATE present-cost factor (:data:`DEFAULT_SOFT_RIPUP_COST`), not the
   hard one -- they can still reclaim the corridor they just vacated if it
   is genuinely their best option, at a cost premium, rather than being
   flatly forbidden from it. Known approximation: ``present_cost_factor`` is
   a single global scalar in the underlying ``.route()`` API, so this is
   "moderate cost against ALL committed copper for this one reroute", not a
   true per-corridor cost overlay targeted at the specific cells the failed
   net just claimed. In practice that distinction rarely matters -- the
   failed net's new copper is exactly where the sibling most recently was --
   but it is not literally corridor-scoped.
5. Each escalation attempt is a TRANSACTION: it commits only if the failed
   net re-routes FULLY and every displaced sibling re-routes with at least
   as many routes as it had before (mirroring
   :meth:`NegotiatedRouter.targeted_ripup`'s own sibling-degradation proxy).
   Any other outcome rolls the whole attempt back to the pre-attempt
   snapshot and escalates.
6. **Whole-pass improvement gate**: after every net in the order has been
   attempted, the pass's (connections, vias) is compared against a second,
   cheap, no-rip-up baseline pass (hard single-attempt per net, no
   escalation at all -- what this strategy would produce with step 3 turned
   off). If the rip-up pass is not at least as good (same-or-more
   connections; no more vias at equal connections), the baseline is kept
   instead and the rip-up pass's copper is discarded. This is the
   "before/after improvement gate that reverts a worse run" from the issue.

Known limitations (prototype, not production parity with
``route_all_negotiated``):

* Net ordering reuses only a subset of the real loop's pre-processing
  (``_forced_net_order`` / priority sort, pour-net and net-0 filtering,
  pre-routed-net skipping, match-group interleaving) -- the matrix-topology,
  byte-lane and connector-sibling passes are not applied. Fine for this
  issue's target/no-regression boards (00/01/02/03/06a), which exercise
  none of those subsystems; not validated beyond them.
* No ``checkpoint_callback`` support yet -- accepted for signature
  compatibility with ``route_all_negotiated`` and silently ignored.
* The whole-pass improvement gate doubles the per-net routing work in the
  worst case (both passes always run when the gate is enabled). Measured
  cost is in the research doc; ``enable_improvement_gate=False`` skips the
  baseline pass entirely for a cheaper (ungated) measurement.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kicad_tools.cli.progress import flush_print

from .algorithms.negotiated import NegotiatedRouter

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from .core import Autorouter
    from .primitives import Pad, Route

#: First/retry attempt present-cost factor: high enough that sharing any
#: already-occupied cell is prohibitively expensive (effectively hard
#: routing). Harmless against cells a rip-up just freed -- those are zero
#: usage, so a high factor costs nothing there while still strongly
#: deterring overlap with every OTHER committed net.
DEFAULT_HARD_COST = 1000.0

#: Sibling-reroute present-cost factor after a successful rip-up retry --
#: see module docstring point 4 ("soft cost on the ripped corridor").
DEFAULT_SOFT_RIPUP_COST = 5.0

#: N+1 escalation cap (module docstring point 3).
DEFAULT_MAX_ESCALATION = 3


@dataclass(frozen=True)
class _RunMetrics:
    """Lexicographic comparison key for the improvement gate.

    More connections wins outright; a tie on connections is broken by fewer
    vias. Both counts are internal to this module (RSMT-edge-level route
    counts and ``Route.vias`` lengths) -- they are a cheap, self-consistent
    proxy for comparing two passes of the SAME strategy, not the externally
    measured ``measure_completion``/``measure_copper`` metrics the research
    harness uses on the final ``.kicad_pcb``.
    """

    connections: int
    vias: int

    def at_least_as_good_as(self, other: _RunMetrics) -> bool:
        if self.connections != other.connections:
            return self.connections > other.connections
        return self.vias <= other.vias


def _metrics(net_routes: dict[int, list[Route]]) -> _RunMetrics:
    connections = sum(len(routes) for routes in net_routes.values())
    vias = sum(len(route.vias) for routes in net_routes.values() for route in routes)
    return _RunMetrics(connections=connections, vias=vias)


def select_ripup_targets(
    blocker_scores: dict[int, int],
    committed_nets: set[int],
    escalation: int,
) -> list[int]:
    """Pick up to ``escalation`` rip-up targets, highest blocker score first.

    The **slot-0 guard** (Issue #5894): only nets already present in
    ``committed_nets`` -- nets this pass has placed at least one route for
    -- are eligible. A net not yet attempted (slot 0 of the net order, or
    any later net the sequential loop has not reached yet) can never be a
    rip-up target: ripping an unplaced net is a no-op that would silently
    let an "N+1" escalation consume a slot without displacing anything, and
    excluding it bounds how much of the board a single failing net can ever
    touch to "whatever has been placed so far", never "everything placed or
    not". Ties break on net id for determinism.

    Pure function -- no router/grid access -- so it is unit-testable in
    isolation (Issue #5894's test plan).
    """
    eligible = [n for n in blocker_scores if n in committed_nets]
    eligible.sort(key=lambda n: (-blocker_scores[n], n))
    return eligible[:escalation]


def is_improvement(candidate: tuple[int, int], baseline: tuple[int, int]) -> bool:
    """Whole-pass improvement gate predicate: ``(connections, vias)`` tuples.

    ``True`` means the candidate (the rip-up pass) is at least as good as
    the baseline (the no-rip-up pass) and should be KEPT; ``False`` means
    the baseline should be kept instead (Issue #5894's "improvement gate
    that reverts a worse run"). Pure function, unit-testable without a
    router -- see :func:`select_ripup_targets`.
    """
    return _RunMetrics(*candidate).at_least_as_good_as(_RunMetrics(*baseline))


@dataclass(frozen=True)
class _Config:
    per_net_timeout: float | None
    max_escalation: int
    hard_cost: float
    soft_ripup_cost: float


@dataclass
class _PassStats:
    """How much of the rip-up machinery actually fired during one pass.

    The whole-pass improvement gate can only report whether the rip-up pass
    beat the no-rip-up baseline; it cannot say *why* two passes tied. These
    counters can: a tie with ``escalations == 0`` means no net ever failed its
    direct attempt, while a tie with ``escalations > 0, commits == 0`` means
    every escalation was rolled back. The #5894 fleet comparison turns on that
    distinction (see ``docs/research/kct-route-ripup-experiment.md``), so the
    numbers are logged rather than left to be inferred from the metrics tie.
    """

    attempted: int = 0
    direct_success: int = 0
    escalations: int = 0
    retry_failed: int = 0
    sibling_rollbacks: int = 0
    ripup_commits: int = 0
    no_eligible_blocker: int = 0
    failed: int = 0

    def summary(self) -> str:
        return (
            f"{self.attempted} nets attempted, {self.direct_success} routed directly, "
            f"{self.failed} unrouted; rip-up: {self.escalations} escalation(s), "
            f"{self.ripup_commits} committed, {self.retry_failed} retry-failed, "
            f"{self.sibling_rollbacks} sibling-rollback(s), "
            f"{self.no_eligible_blocker} with no eligible blocker"
        )


# ---------------------------------------------------------------------------
# Net order / pad lookup -- see module docstring "Known limitations".
# ---------------------------------------------------------------------------


def _escape_infrastructure_nets(router: Autorouter) -> set[int]:
    """Nets whose only copper so far is dense-package escape infrastructure.

    ``generate_escape_routes`` appends its stubs to ``router.routes`` with
    ``is_escape`` left ``False`` (only the #1603 SUB-grid stubs set that flag),
    and remaps each escaped pad in ``router._escape_pad_overrides``. So the
    naive "any non-``is_escape`` route means pre-routed" test -- correct on the
    negotiated entry point, where the escape pre-phase has not run -- throws
    away most of the board on the escape/two-phase path: on board 03 it cut the
    net universe from 24 to 6 ("Nets to route: 6" against the CLI's "Nets to
    route: 24"), which is the dominant reason that board collapsed to 4/27 in
    the first #5894 fleet run.

    ``route_with_escape``'s own contract is explicit that these nets still need
    routing: "the main routing pipeline routes between escape endpoints
    (virtual pads), not original pad centers", so a net holding only escape
    copper is a net with a stub, not a finished net.
    """
    nets: set[int] = set()
    for pad_key in getattr(router, "_escape_pad_overrides", {}):
        pad = router.pads.get(pad_key)
        if pad is not None:
            nets.add(pad.net)
    return nets


def _build_net_order(router: Autorouter) -> list[int]:
    if router._forced_net_order is not None:
        net_order = list(router._forced_net_order)
    else:
        net_order = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))
    net_order = router._filter_pour_nets(net_order)
    net_order = [n for n in net_order if n != 0]
    # Issue #2464: a net already routed by a pre-pass (e.g. diff pairs, or
    # existing copper kept by --preserve-existing) is skipped -- but an
    # escape STUB is not a full route (Issue #3441), so it is not treated
    # as pre-routed; the real net-by-net attempt below still extends it.
    prerouted_nets = {r.net for r in router.routes if not getattr(r, "is_escape", False)}
    prerouted_nets -= _escape_infrastructure_nets(router)
    # Nets the pose-based coupled trunk pre-pass has claimed (#5786) must NOT
    # be re-routed as independent legs, even though the escape override above
    # would otherwise re-admit them -- same contract ``TwoPhaseRouter`` gets
    # through its ``get_claimed_nets`` hook.
    prerouted_nets |= set(getattr(router, "_pose_coupled_nets", set()) or set())
    net_order = [n for n in net_order if n not in prerouted_nets]
    return router._interleave_match_groups(net_order)


def _build_pads_by_net(router: Autorouter, net_order: list[int]) -> dict[int, list[Pad]]:
    pads_by_net: dict[int, list[Pad]] = {}
    for net in net_order:
        if net not in router.nets:
            continue
        pads_for_routing = router._region_filtered_pad_keys(net, router.nets[net])
        stub_targets = router._stub_target_pads(net)
        if len(pads_for_routing) + len(stub_targets) < 2:
            continue
        pads_by_net[net] = [
            router._escape_pad_overrides.get(p, router.pads[p]) for p in pads_for_routing
        ] + stub_targets
    return pads_by_net


# ---------------------------------------------------------------------------
# Grid commit / undo / restore -- mirrors
# ``NegotiatedRouter.targeted_ripup``'s transaction semantics exactly:
# a net reaches the grid through ``Autorouter._mark_route`` (inside
# ``_route_net_negotiated``'s per-edge callback) AND a separate, explicit
# ``grid.mark_route_usage`` from the caller (congestion bookkeeping) -- so
# undoing a COMMITTED net must reverse both (``unmark_route`` +
# ``unmark_route_usage``), while discarding a PARTIAL result that never
# reached ``_commit`` must reverse only the first (it never incremented
# usage).
# ---------------------------------------------------------------------------


def _route_net(
    router: Autorouter, net: int, present_cost_factor: float, per_net_timeout: float | None
) -> tuple[list[Route], int]:
    """Route one net; return its routes and the number of FAILED RSMT edges.

    The edge-failure count is the prototype's per-net success test, and it has
    to be: the obvious route-COUNT proxy (``len(routes) >= len(pads) - 1``)
    silently over-counts, because ``_route_net_negotiated`` prepends intra-IC
    and block-internal routes to its result before any inter-pad A* runs. On
    board 02 that proxy declared all 10 nets successful while the graded output
    was 31/36 connections, so the rip-up escalation -- the entire point of this
    strategy -- never once engaged (``0 escalation(s)`` in the pass stats). The
    ``failure_callback`` fires per failed/refused/timed-out edge (#2425), which
    is the signal the negotiated loop's own rip-up layer uses, so this predicate
    agrees with it by construction.
    """
    failures: list[tuple[Pad, Pad]] = []
    routes = router._route_net_negotiated(
        net,
        present_cost_factor,
        per_net_timeout=per_net_timeout,
        failure_callback=lambda src, dst: failures.append((src, dst)),
    )
    return routes, len(failures)


def _discard_partial(router: Autorouter, routes: list[Route]) -> None:
    """Undo a FAILED/partial result's already-``_mark_route``'d edges.

    These never went through :func:`_commit`, so ``grid.mark_route_usage``
    was never called on them -- only ``grid.unmark_route`` is needed, never
    ``unmark_route_usage`` (which would wrongly decrement a shared cell's
    congestion count for a route that never incremented it).
    """
    for route in routes:
        router.grid.unmark_route(route)


def _commit(
    router: Autorouter, net: int, routes: list[Route], net_routes: dict[int, list[Route]]
) -> None:
    net_routes[net] = routes
    for route in routes:
        router.grid.mark_route_usage(route)
        router.routes.append(route)


def _undo(router: Autorouter, net: int, net_routes: dict[int, list[Route]]) -> None:
    """Rip up a COMMITTED net -- mirrors ``NegotiatedRouter.rip_up_nets``."""
    for route in net_routes.get(net, []):
        router.grid.unmark_route_usage(route)
        router.grid.unmark_route(route)
        if route in router.routes:
            router.routes.remove(route)
    net_routes[net] = []


def _restore(
    router: Autorouter, net: int, routes: list[Route], net_routes: dict[int, list[Route]]
) -> None:
    """Re-commit a previously-undone net's EXACT Route objects (a rollback).

    Mirrors ``NegotiatedRouter.targeted_ripup``'s ``_rollback_to_snapshot``.

    ``_mark_route`` is called **without** ``enforce_pad_access``, matching
    every other re-land/rollback path (``core.py``'s ``mark_route`` closures at
    8901/12257/12438/12532/13214, which is what ``_rollback_to_snapshot``
    marks through).  Issue #5891's own contract reserves the commit-time
    pad-access gate for the single search-commit path and says rip-up re-land
    paths deliberately do not opt in: refusing restored copper that was
    already committed would strand the very net the rescue is for.  Not
    opting in is also what makes the discarded ``bool`` return safe here --
    it is unconditionally ``True`` unless the gate vetoes, so the three
    ledgers written below (blocking, congestion, ``router.routes``) cannot
    desync.
    """
    net_routes[net] = list(routes)
    for route in routes:
        router._mark_route(route)
        router.grid.mark_route_usage(route)
        router.routes.append(route)


# ---------------------------------------------------------------------------
# Frontier-cell blocker attribution (module docstring point 2).
# ---------------------------------------------------------------------------


def _frontier_blockers(
    router: Autorouter,
    net: int,
    pads: list[Pad],
    per_net_timeout: float | None,
) -> dict[int, int]:
    """Score already-routed nets by how much they block ``net``'s own search.

    Reuses :meth:`NegotiatedRouter.find_blocking_nets_relaxed` (Issue
    #2274) rather than re-deriving it: with every routed net's cells
    temporarily unblocked, it re-runs THIS net's own A* search between
    consecutive pads. The cells of the resulting relaxed path that were
    originally occupied by a routed net are that net's actual contribution
    to the failure -- the failing search's own frontier, not a Bresenham
    straight-line guess (the ``find_blocking_nets`` helper used elsewhere
    in ``negotiated.py``).
    """
    # ``Autorouter.router`` is annotated ``CppPathfinder | Router`` while
    # ``NegotiatedRouter`` asks for ``Router``; the two are duck-compatible for
    # every method the relaxed search calls (the C++ backend is a drop-in
    # pathfinder), and ``core.py``'s own ``NegotiatedRouter(self.grid,
    # self.router, ...)`` constructions pass the same union.
    neg_router = NegotiatedRouter(
        router.grid,
        router.router,  # type: ignore[arg-type]
        router.rules,
        router.net_class_map,
    )
    return neg_router.find_blocking_nets_relaxed(
        [net], {net: pads}, per_net_timeout=per_net_timeout
    )


# ---------------------------------------------------------------------------
# Per-net attempt with N+1 escalation (module docstring points 3-5).
# ---------------------------------------------------------------------------


def _attempt_net(
    router: Autorouter,
    net: int,
    pads: list[Pad],
    net_routes: dict[int, list[Route]],
    cfg: _Config,
    stats: _PassStats | None = None,
) -> bool:
    """Route ``net``, escalating rip-up on failure. Mutates ``net_routes``/grid."""
    if stats is None:
        stats = _PassStats()

    routes, edge_failures = _route_net(router, net, cfg.hard_cost, cfg.per_net_timeout)
    if routes and edge_failures == 0:
        _commit(router, net, routes, net_routes)
        stats.direct_success += 1
        return True
    if routes:
        _discard_partial(router, routes)

    committed_nets = {n for n, r in net_routes.items() if r}
    escalation = 1
    while escalation <= cfg.max_escalation:
        scores = _frontier_blockers(router, net, pads, cfg.per_net_timeout)
        targets = select_ripup_targets(scores, committed_nets, escalation)
        if not targets:
            stats.no_eligible_blocker += 1
            break
        stats.escalations += 1

        snapshot = {n: list(net_routes[n]) for n in targets}
        for n in targets:
            _undo(router, n, net_routes)

        routes, edge_failures = _route_net(router, net, cfg.hard_cost, cfg.per_net_timeout)
        if not routes or edge_failures:
            if routes:
                _discard_partial(router, routes)
            for n in targets:
                _restore(router, n, snapshot[n], net_routes)
            stats.retry_failed += 1
            escalation += 1
            continue

        _commit(router, net, routes, net_routes)

        ok = True
        placed_siblings: list[int] = []
        for n in targets:
            # The SIBLING test deliberately stays a route-count comparison
            # against its own pre-rip-up count, not the edge-failure predicate
            # used for the failed net: it asks "did this displaced net get at
            # least as much copper back as it had", which is exactly
            # ``targeted_ripup``'s sibling-degradation proxy. A sibling that
            # was already partial before the rip-up must be allowed to come
            # back equally partial, or no escalation could ever commit on a
            # board that starts with partial nets.
            s_routes, _ = _route_net(router, n, cfg.soft_ripup_cost, cfg.per_net_timeout)
            if s_routes and len(s_routes) >= len(snapshot[n]):
                _commit(router, n, s_routes, net_routes)
                placed_siblings.append(n)
            else:
                if s_routes:
                    _discard_partial(router, s_routes)
                ok = False
                break

        if ok:
            committed_nets.add(net)
            committed_nets.update(targets)
            stats.ripup_commits += 1
            return True
        stats.sibling_rollbacks += 1

        # Roll the whole attempt back: undo the failed net and any siblings
        # placed so far this escalation, restore every ripped net's ORIGINAL
        # geometry, and escalate.
        _undo(router, net, net_routes)
        for n in placed_siblings:
            _undo(router, n, net_routes)
        for n in targets:
            _restore(router, n, snapshot[n], net_routes)
        escalation += 1

    return bool(net_routes.get(net))


# ---------------------------------------------------------------------------
# Whole-pass driver + improvement gate (module docstring point 6).
# ---------------------------------------------------------------------------


def _run_pass(
    router: Autorouter,
    net_order: list[int],
    pads_by_net: dict[int, list[Pad]],
    cfg: _Config,
    *,
    use_ripup: bool,
    deadline: float | None = None,
    progress_callback: ProgressCallback | None = None,
    label: str = "sequential",
    stats: _PassStats | None = None,
) -> dict[int, list[Route]]:
    net_routes: dict[int, list[Route]] = {}
    if stats is None:
        stats = _PassStats()
    total = len(pads_by_net)
    done = 0
    for net in net_order:
        pads = pads_by_net.get(net)
        if not pads:
            continue
        if deadline is not None and time.time() >= deadline:
            flush_print(f"  [{label}] timeout reached -- stopping with {done}/{total} attempted")
            break
        stats.attempted += 1
        if use_ripup:
            if not _attempt_net(router, net, pads, net_routes, cfg, stats):
                stats.failed += 1
        else:
            routes, edge_failures = _route_net(router, net, cfg.hard_cost, cfg.per_net_timeout)
            if routes and edge_failures == 0:
                _commit(router, net, routes, net_routes)
                stats.direct_success += 1
            else:
                if routes:
                    _discard_partial(router, routes)
                stats.failed += 1
        done += 1
        if progress_callback is not None:
            frac = done / total if total else 1.0
            if not progress_callback(frac, f"[{label}] net {done}/{total}", True):
                break
    flush_print(f"  [{label}] {stats.summary()}")
    return net_routes


def route_all_sequential_ripup(
    router: Autorouter,
    *,
    timeout: float | None = None,
    per_net_timeout: float | None = None,
    seed: int | None = None,
    progress_callback: ProgressCallback | None = None,
    checkpoint_callback: object | None = None,
    max_escalation: int = DEFAULT_MAX_ESCALATION,
    hard_cost: float = DEFAULT_HARD_COST,
    soft_ripup_cost: float = DEFAULT_SOFT_RIPUP_COST,
    enable_improvement_gate: bool = True,
) -> list[Route]:
    """KRT-style sequential N+1 rip-up outer loop. See module docstring.

    Args:
        router: The loaded, already-prepared ``Autorouter`` (caller has run
            whatever pre-passes it needs -- escape routing, diff pairs,
            sub-grid prepass -- exactly as the negotiated loop's own call
            site does before dispatching here).
        timeout: Optional outer wall-clock budget in seconds, shared across
            BOTH the rip-up pass and (when the improvement gate is enabled)
            the baseline pass.
        per_net_timeout: Optional per-net wall-clock budget forwarded to
            every ``_route_net_negotiated`` / relaxed-attribution call.
        seed: Optional seed for the global ``random`` module, for parity
            with ``route_all_negotiated(seed=...)``. This strategy does not
            itself use randomness, but downstream MST/RSMT construction may.
        progress_callback: Optional ``(fraction, message, cancelable) ->
            bool`` callback; returning ``False`` stops the current pass
            early (partial result kept).
        checkpoint_callback: Accepted for signature compatibility with
            ``route_all_negotiated`` and NOT invoked -- see module
            docstring "Known limitations".
        max_escalation: N+1 escalation cap (module docstring point 3).
        hard_cost: Present-cost factor for a net's first attempt and its
            retry once blockers are ripped (module docstring point 1).
        soft_ripup_cost: Present-cost factor for a displaced sibling's
            reroute (module docstring point 4).
        enable_improvement_gate: Run the whole-pass before/after gate
            (module docstring point 6). Default ``True`` -- the acceptance
            criterion this issue ships with. ``False`` returns the rip-up
            pass's result directly (cheaper, for isolated measurement).

    Returns:
        The full, current ``router.routes`` list (every caller of
        ``route_all_negotiated`` either uses this return value or reads
        ``router.routes`` directly -- see the call sites in ``route_cmd.py``
        -- so this mirrors that method's own return contract).
    """
    if seed is not None:
        random.seed(seed)

    net_order = _build_net_order(router)
    pads_by_net = _build_pads_by_net(router, net_order)
    cfg = _Config(
        per_net_timeout=per_net_timeout,
        max_escalation=max_escalation,
        hard_cost=hard_cost,
        soft_ripup_cost=soft_ripup_cost,
    )

    flush_print("\n=== Sequential N+1 Rip-up Routing (prototype, Issue #5894) ===")
    flush_print(f"  Nets to route: {len(pads_by_net)}")
    flush_print(f"  Max escalation: {max_escalation}")
    if timeout:
        flush_print(f"  Timeout: {timeout}s (shared by the rip-up pass and the gate's baseline)")

    start = time.time()
    deadline = start + timeout if timeout is not None else None

    treatment_routes = _run_pass(
        router,
        net_order,
        pads_by_net,
        cfg,
        use_ripup=True,
        deadline=deadline,
        progress_callback=progress_callback,
        label="rip-up",
    )
    treatment_metrics = _metrics(treatment_routes)
    flush_print(
        f"  Rip-up pass: {treatment_metrics.connections} connections, "
        f"{treatment_metrics.vias} vias ({time.time() - start:.1f}s)"
    )

    if not enable_improvement_gate:
        return list(router.routes)

    # Whole-pass improvement gate: snapshot + rip up the treatment result to
    # get a clean grid, run the cheap no-rip-up baseline, and keep whichever
    # is better -- restoring the loser's geometry is never needed because
    # the LOSING pass's own copper is simply never re-applied.
    treatment_snapshot = {n: list(r) for n, r in treatment_routes.items() if r}
    for net in list(treatment_snapshot):
        _undo(router, net, treatment_routes)

    baseline_start = time.time()
    baseline_deadline = (
        baseline_start + max(timeout - (baseline_start - start), 0.0)
        if timeout is not None
        else None
    )
    baseline_routes = _run_pass(
        router,
        net_order,
        pads_by_net,
        cfg,
        use_ripup=False,
        deadline=baseline_deadline,
        label="no-rip-up baseline",
    )
    baseline_metrics = _metrics(baseline_routes)
    flush_print(
        f"  No-rip-up baseline: {baseline_metrics.connections} connections, "
        f"{baseline_metrics.vias} vias ({time.time() - baseline_start:.1f}s)"
    )

    if is_improvement(
        (treatment_metrics.connections, treatment_metrics.vias),
        (baseline_metrics.connections, baseline_metrics.vias),
    ):
        flush_print("  Improvement gate: rip-up pass kept (>= no-rip-up baseline)")
        for net in list(baseline_routes):
            if baseline_routes.get(net):
                _undo(router, net, baseline_routes)
        for net, routes in treatment_snapshot.items():
            _restore(router, net, routes, baseline_routes)
        return list(router.routes)

    flush_print("  Improvement gate: REVERTED to no-rip-up baseline (rip-up pass was not better)")
    return list(router.routes)
