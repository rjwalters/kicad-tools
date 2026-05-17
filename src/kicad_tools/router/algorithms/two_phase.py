"""Two-phase routing algorithm (Global + Detailed).

Phase 1 uses tile-based GlobalRouter with geometry-based edge capacity
and negotiated iteration to assign corridors for each net.
Phase 2 uses grid-based routing with corridor guidance.

Issue #2276: Replaced SparseRouter global phase with tile-based
GlobalRouter supporting per-layer capacity and negotiated congestion.
"""

from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING, Any, Callable

from kicad_tools.cli.progress import flush_print

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from ..grid import RoutingGrid
    from ..output import format_failed_nets_summary
    from ..pathfinder import Router
    from ..primitives import Pad, Route
    from ..rules import DesignRules
    from ..sparse import Corridor


class TwoPhaseRouter:
    """Two-phase global+detailed routing algorithm.

    Phase 1 (Global): Use tile-based GlobalRouter with geometry-based
    edge capacity estimation and negotiated congestion to assign
    corridors for each net.

    Phase 2 (Detailed): Use grid-based routing with corridor guidance.
    Routes prefer to stay within their assigned corridors but can exit
    with a cost penalty.
    """

    def __init__(
        self,
        grid: RoutingGrid,
        router: Router,
        rules: DesignRules,
        net_class_map: dict | None,
        nets: dict[int, list[tuple[str, str]]],
        net_names: dict[int, str],
        pads: dict[tuple[str, str], Pad],
        routes: list[Route],
        routing_failures: list,
        get_net_priority: callable,
        route_net: callable,
        route_net_with_corridor: callable,
        mark_route: callable,
        pour_nets_without_zones: set[str] | None = None,
        attempt_blocked_component_ripup: Callable[..., Any] | None = None,
        build_pads_by_net: Callable[..., Any] | None = None,
        get_partially_routed_nets: Callable[..., Any] | None = None,
        interleave_match_groups: Callable[[list[int]], list[int]] | None = None,
        apply_byte_lane_inner_priority: Callable[[list[int]], list[int]] | None = None,
    ):
        self.grid = grid
        self.router = router
        self.rules = rules
        self.net_class_map = net_class_map
        self.nets = nets
        self.net_names = net_names
        self.pads = pads
        self.routes = routes
        self.routing_failures = routing_failures
        self._get_net_priority = get_net_priority
        self._route_net = route_net
        self._route_net_with_corridor = route_net_with_corridor
        self._mark_route = mark_route
        self._pour_nets_without_zones = pour_nets_without_zones or set()
        # Issue #2914: Optional fairness pass that front-loads one
        # representative per match group on the priority-sorted
        # ``net_order``.  Threaded in from
        # :meth:`Autorouter._create_two_phase_router` so the two-phase
        # detailed-routing loop uses the same fairness contract as
        # :meth:`Autorouter.route_all_negotiated`.  When ``None`` (e.g.
        # unit tests that construct TwoPhaseRouter directly), the
        # routing order is identical to the pre-#2914 behaviour.
        self._interleave_match_groups = interleave_match_groups
        # Issue #2962: Optional inner-corner byte-lane priority bump.
        # Mirrors the ``_interleave_match_groups`` threading pattern.
        # When ``None`` (e.g. unit tests that construct TwoPhaseRouter
        # directly) the byte-lane reorder is skipped.
        self._apply_byte_lane_inner_priority = apply_byte_lane_inner_priority
        # Issue #2527: Optional hooks that let the detailed-routing stall path
        # invoke ``Autorouter._attempt_blocked_component_ripup_negotiated``.
        # When the initial pass leaves overflow=0 with unrouted/partial nets
        # (geometric / topology blocker, not congestion), the iteration loop
        # below would otherwise short-circuit and never engage the
        # destination-component sibling rip-up that PR #2523 wired into the
        # negotiated route_all path.  Threading these callables in allows the
        # two-phase path to share the same recovery mechanism.
        self._attempt_blocked_component_ripup = attempt_blocked_component_ripup
        self._build_pads_by_net = build_pads_by_net
        self._get_partially_routed_nets = get_partially_routed_nets

        # Issue #2597: Communicates the reason the negotiated outer loop in
        # ``_detailed_negotiated()`` exited.  Read by the progress-callback
        # status string in :class:`Autorouter` to distinguish ``"stagnated"``
        # from ``"timeout"`` and bare ``f"overflow={N}"``.  Possible values:
        #   - ``None`` — loop hasn't run yet, or two-phase path not taken.
        #   - ``"stagnated"`` — rip-up cohort stagnation detector tripped.
        #   - ``"timeout"`` — wall-clock budget exhausted.
        #   - ``"early_stop"`` — overflow-history early termination.
        #   - ``"converged"`` — overflow reached zero.
        #   - ``"max_iterations"`` — outer loop ran to ``max_iterations``.
        self.last_termination_reason: str | None = None

    def route_all(
        self,
        use_negotiated: bool = True,
        corridor_width_factor: float = 2.0,
        corridor_penalty: float | None = None,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        per_net_timeout: float | None = None,
        initial_routes: list[Route] | None = None,
        max_iterations: int = 20,
        patience: int = 2,
    ) -> list[Route]:
        """Route all nets using two-phase global+detailed routing.

        Args:
            use_negotiated: Use negotiated congestion routing in detailed phase
            corridor_width_factor: Corridor width as multiple of clearance (default: 2.0)
            corridor_penalty: Cost penalty for routing outside corridor.
                Defaults to ``self.rules.cost_corridor_deviation`` when *None*.
            progress_callback: Optional callback for progress updates
            timeout: Optional timeout in seconds
            per_net_timeout: Optional wall-clock timeout per A* search
            initial_routes: Pre-existing routes (e.g. escape routes) that
                should be seeded into the negotiated router's tracking dict
                so they participate in rip-up/reroute (Issue #2294).
            max_iterations: Maximum rip-up-and-reroute iterations for the
                Phase 2 detailed negotiated routing loop (default: 20).
            patience: Minimum number of non-improving iterations before
                early termination is considered (Issue #2317, default: 2).

        Returns:
            List of routes (may be partial if timeout reached or some nets fail)
        """
        from ..global_router import GlobalRouter
        from ..output import format_failed_nets_summary
        from ..region_graph import RegionGraph
        from ..sparse import Corridor

        if corridor_penalty is None:
            corridor_penalty = self.rules.cost_corridor_deviation

        start_time = time.time()

        print("\n=== Two-Phase Routing (Global + Detailed) ===")

        # Get nets to route in priority order
        net_order = sorted(self.nets.keys(), key=lambda n: self._get_net_priority(n))
        net_order = [n for n in net_order if n != 0]

        # Issue #1295: Filter out pour nets — they are connected via zone fills.
        # Issue #1841: Exclude pour nets without zones (they route as signals).
        pour_nets = []
        signal_nets = []
        for n in net_order:
            net_name = self.net_names.get(n, "")
            if net_name in self._pour_nets_without_zones:
                signal_nets.append(n)
                continue
            net_class = (self.net_class_map or {}).get(net_name)
            if net_class and net_class.is_pour_net:
                pour_nets.append(n)
            else:
                signal_nets.append(n)
        if pour_nets:
            pour_names = [self.net_names.get(n, f"Net {n}") for n in pour_nets]
            flush_print(
                f"  Skipping {len(pour_nets)} pour net(s) "
                f"(use zone fill instead): {pour_names}"
            )
        net_order = signal_nets

        # Filter out single-pad nets — they are trivially connected and
        # should not inflate the "nets routed" count.  This mirrors the
        # filter in core.py:1082.
        single_pad_nets = []
        multi_pad_nets = []
        for n in net_order:
            if len(self.nets.get(n, [])) < 2:
                single_pad_nets.append(n)
            else:
                multi_pad_nets.append(n)
        if single_pad_nets:
            flush_print(
                f"  Skipping {len(single_pad_nets)} single-pad net(s) "
                "(trivially connected)"
            )
        net_order = multi_pad_nets

        # Issue #2914: Front-load one representative per match group so
        # no group can be fully starved by the wall-clock budget.  Without
        # this, board 07 ADDR_BUS (priority class 2) was fully scheduled
        # after DDR / MIPI / HDMI (class 1) and the 600 s budget was
        # exhausted before A0..A7 received any "Routing net..." log line.
        # The helper is threaded in from
        # :meth:`Autorouter._create_two_phase_router` so it shares its
        # implementation (and detection-failure fallback) with the
        # negotiated-route path.  When unset (direct TwoPhaseRouter
        # construction in tests) the routing order is unchanged.
        if self._interleave_match_groups is not None:
            net_order = self._interleave_match_groups(net_order)

        # Issue #2962: Mirrored byte-lane detection hook (scaffolding only).
        # See the ``Autorouter._apply_byte_lane_inner_priority`` docstring
        # for the rationale and the R1/R2/R3 trace.  Applied after the
        # starvation-fairness pass so a future implementation that swaps
        # the helper body for a real reorder keeps the head-class ordering
        # exact; only within-class neighbour priorities would be adjusted.
        if self._apply_byte_lane_inner_priority is not None:
            net_order = self._apply_byte_lane_inner_priority(net_order)

        total_nets = len(net_order)

        if total_nets == 0:
            print("  No nets to route")
            return []

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        # =====================================================================
        # Phase 1: Tile-based Global Routing (Issue #2276)
        # =====================================================================
        print("\n--- Phase 1: Global Routing (tile-based) ---")
        if progress_callback is not None:
            if not progress_callback(0.0, "Phase 1: Global routing", True):
                return list(self.routes)

        # Compute routing pitch from design rules
        trace_pitch = self.rules.trace_width + self.rules.trace_clearance
        corridor_width = corridor_width_factor * self.rules.trace_clearance

        # Determine tile grid size: ~10x trace pitch per tile, minimum 3x3
        tile_size = max(trace_pitch * 10.0, 1.0)
        num_cols = max(3, int(self.grid.width / tile_size))
        num_rows = max(3, int(self.grid.height / tile_size))

        # Build tile-based region graph with geometry-based capacity
        region_graph = RegionGraph(
            board_width=self.grid.width,
            board_height=self.grid.height,
            origin_x=self.grid.origin_x,
            origin_y=self.grid.origin_y,
            num_cols=num_cols,
            num_rows=num_rows,
            trace_pitch=trace_pitch,
            num_layers=self.grid.num_layers,
        )

        # Register pads as obstacles for blockage-aware capacity
        pad_list = list(self.pads.values())
        region_graph.register_obstacles(pad_list)

        stats = region_graph.get_statistics()
        flush_print(
            f"  Tile grid: {num_cols}x{num_rows} "
            f"({stats['num_regions']} regions, {stats['num_edges']} edges, "
            f"pitch={trace_pitch:.3f}mm, layers={self.grid.num_layers})"
        )

        # Run global routing with negotiated iteration
        global_router = GlobalRouter(
            region_graph=region_graph,
            corridor_width=corridor_width,
            default_layer=0,
            negotiated=True,
            max_iterations=15,
            history_increment=1.0,
        )

        global_result = global_router.route_all(
            nets=self.nets,
            pad_dict=self.pads,
            net_order=net_order,
        )

        # Extract corridors from global routing result
        corridors: dict[int, Corridor] = {}
        for net_id, assign in global_result.assignments.items():
            corridors[net_id] = assign.corridor

        flush_print(
            f"  Global routing: {len(corridors)}/{total_nets} nets have corridors "
            f"({global_result.iterations} iterations, "
            f"overflow={global_result.final_overflow}, "
            f"{elapsed_str()})"
        )
        if global_result.failed_nets:
            flush_print(
                f"  {len(global_result.failed_nets)} nets failed global routing "
                f"(will attempt anyway)"
            )

        # =====================================================================
        # Phase 2: Detailed Routing with Corridor Guidance
        # =====================================================================
        print("\n--- Phase 2: Detailed Routing ---")
        if progress_callback is not None:
            if not progress_callback(0.3, "Phase 2: Detailed routing", True):
                return list(self.routes)

        # Set corridor preferences on the grid
        for net, corridor in corridors.items():
            self.grid.set_corridor_preference(corridor, net, corridor_penalty)

        # Route using negotiated or standard routing
        if use_negotiated:
            detailed_routes = self._detailed_negotiated(
                net_order=net_order,
                corridor_penalty=corridor_penalty,
                corridors=corridors,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
                per_net_timeout=per_net_timeout,
                initial_routes=initial_routes,
                max_iterations=max_iterations,
                patience=patience,
            )
        else:
            detailed_routes = self._detailed_standard(
                net_order=net_order,
                progress_callback=progress_callback,
                timeout=timeout,
                start_time=start_time,
            )

        # Clear corridor preferences (not needed after routing)
        self.grid.clear_all_corridor_preferences()

        # Summary — use connectivity-aware counting so we only report
        # nets where all pads are in the same connected component (#2352).
        nets_with_segments = len({r.net for r in detailed_routes})
        connected_nets = nets_with_segments  # fallback if pad data unavailable

        if self.pads and self.nets:
            from ..observability import validate_net_connectivity

            net_pads: dict[int, list] = {}
            for net_id, pad_keys in self.nets.items():
                pad_list = [self.pads[k] for k in pad_keys if k in self.pads]
                if pad_list:
                    net_pads[net_id] = pad_list
            connectivity = validate_net_connectivity(detailed_routes, net_pads)
            connected_nets = sum(
                1
                for info in connectivity.values()
                if info["connected"]
            )

        total_elapsed = time.time() - start_time
        print("\n=== Two-Phase Routing Complete ===")
        print(f"  Total nets: {total_nets}")
        print(f"  Global routing: {len(corridors)} corridors assigned")
        print(f"  Detailed routing: {connected_nets} nets routed")
        if connected_nets < nets_with_segments:
            print(
                f"    ({nets_with_segments - connected_nets} additional net(s) "
                "have partial routes)"
            )
        print(f"  Total time: {total_elapsed:.1f}s")

        # Print failed nets summary if any routes failed
        if self.routing_failures:
            failure_summary = format_failed_nets_summary(self.routing_failures)
            if failure_summary:
                print(failure_summary)

        if progress_callback is not None:
            # Issue #2597: Distinguish ``stagnated`` from ``timeout`` in the
            # final progress message so callers (and CI) can pick the right
            # next action — re-place vs. add budget.  Plain ``timeout`` was
            # ambiguous: did we run out of clock or hit a local minimum?
            status_suffix = ""
            if self.last_termination_reason == "stagnated":
                status_suffix = " (stagnated)"
            elif self.last_termination_reason == "timeout":
                status_suffix = " (timeout)"
            elif self.last_termination_reason == "converged":
                status_suffix = " (converged)"
            progress_callback(
                1.0,
                (
                    f"Complete: {connected_nets}/{total_nets} nets routed "
                    f"in {total_elapsed:.1f}s{status_suffix}"
                ),
                False,
            )

        return detailed_routes

    def _detailed_negotiated(
        self,
        net_order: list[int],
        corridor_penalty: float | None = None,
        corridors: dict | None = None,
        progress_callback: ProgressCallback | None = None,
        timeout: float | None = None,
        start_time: float = 0.0,
        per_net_timeout: float | None = None,
        initial_routes: list[Route] | None = None,
        max_iterations: int = 20,
        patience: int = 2,
    ) -> list[Route]:
        """Detailed routing phase using negotiated congestion routing.

        Args:
            initial_routes: Pre-existing routes (e.g. escape routes) to seed
                into ``net_routes`` so they participate in rip-up/reroute
                instead of being permanently reserved (Issue #2294).
            patience: Minimum number of non-improving iterations before
                early termination is considered (Issue #2317). Passed as
                ``min_iterations`` to ``should_terminate_early()``.
        """
        from ..algorithms import NegotiatedRouter
        from ..algorithms.negotiated import (
            detect_ripup_stagnation,
            should_terminate_early,
        )

        if corridor_penalty is None:
            corridor_penalty = self.rules.cost_corridor_deviation

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        def elapsed_str() -> str:
            return f"{time.time() - start_time:.1f}s"

        total_nets = len(net_order)

        # Use negotiated routing with corridor guidance
        neg_router = NegotiatedRouter(self.grid, self.router, self.rules, self.net_class_map)
        net_routes: dict[int, list[Route]] = {}
        present_factor = 0.5

        # Issue #2294: Seed pre-existing routes (e.g. escape routes from
        # Phase 1) into net_routes so the rip-up loop can displace them.
        # Also register their usage counts so unmark_route_usage works
        # correctly during rip-up.
        if initial_routes:
            for route in initial_routes:
                net_id = route.net
                if net_id not in net_routes:
                    net_routes[net_id] = []
                net_routes[net_id].append(route)
                self.grid.mark_route_usage(route)

        # Issue #2518: Single ``timed_out`` flag propagates across nested
        # loops so that hitting the wall-clock budget inside the per-net
        # inner loop short-circuits the outer iteration loop too.  Without
        # this, the inner ``break`` only exits one level and the iteration
        # body's overflow recompute / history snapshot still runs, then the
        # next iteration is started before the iteration-boundary check
        # fires — wasting one full ``len(nets_to_reroute) * per_net_timeout``
        # tail (~1080s in the chorus-test repro for issue #2518).
        timed_out = False

        # Initial routing pass
        for i, net in enumerate(net_order):
            if check_timeout():
                flush_print(
                    f"  ⚠ Timeout during detailed routing at net {i}/{total_nets} ({elapsed_str()})"
                )
                timed_out = True
                break

            net_name = self.net_names.get(net, f"Net {net}")
            pct = (i / total_nets * 100) if total_nets > 0 else 0
            flush_print(f"  [{pct:5.1f}%] Routing {net_name}... ({elapsed_str()})")

            routes = self._route_net_with_corridor(net, present_factor, per_net_timeout=per_net_timeout)
            if routes:
                net_routes[net] = routes
                for route in routes:
                    self.grid.mark_route_usage(route)
                    self.routes.append(route)

        overflow = self.grid.get_total_overflow()
        flush_print(f"  Initial pass: {len(net_routes)}/{total_nets} nets, overflow: {overflow}")

        # Issue #2527 / #2745: When the initial pass leaves one or more
        # multi-pad nets unrouted (or partially routed because an A* edge
        # into a dense IC was blocked by a sibling net), the destination-
        # component sibling rip-up (``_attempt_blocked_component
        # _ripup_negotiated``) is the recovery mechanism that can free
        # them.  Originally (#2527) the gate required ``overflow == 0``
        # because the iteration loop below was assumed to handle the
        # ``overflow > 0`` case via standard rip-up scheduling.
        #
        # Issue #2745: That assumption breaks on board 04-stm32-devboard.
        # The standard rip-up loop at ``two_phase.py`` below selects victim
        # nets via ``find_nets_through_overused_cells(net_routes, overused)``
        # which only sees nets with *placed segments*.  A net classified
        # ``blocked_path`` with **zero placed segments** (e.g. OSC_OUT on
        # board 04 — U2.6 pad couldn't escape WEST because OSC_IN already
        # occupied the corridor) is invisible to that scheduler, no matter
        # how many iterations run.  Meanwhile ``overflow == 1`` (from
        # OSC_IN's tight escape) gates this recovery off, so the failed
        # net is never re-evaluated — the 4L escalation replays the same
        # deterministic failure.
        #
        # Drop the ``overflow == 0`` gate.  Engage BLOCKED_BY_COMPONENT
        # recovery whenever ``stall_failed`` (fully unrouted or partially
        # routed multi-pad nets) is non-empty, regardless of overflow.
        # The per-net rip-up budget (``stall_budget = 3``) prevents thrash
        # on charlieplex-style boards where many sibling rip-ups would
        # otherwise be attempted.
        ripup_history: dict[int, int] = {}
        if (
            not timed_out
            and self._attempt_blocked_component_ripup is not None
            and self._build_pads_by_net is not None
        ):
            pads_by_net_local = self._build_pads_by_net(net_order)
            partial_failed: set[int] = set()
            if self._get_partially_routed_nets is not None:
                partial_failed = self._get_partially_routed_nets(
                    net_routes, pads_by_net_local
                )
            stall_failed = [
                n
                for n in net_order
                if (n not in net_routes or n in partial_failed)
                and n in pads_by_net_local
                and len(pads_by_net_local[n]) >= 2
            ]
            if stall_failed:
                flush_print(
                    f"  Initial pass stall (overflow={overflow}): "
                    f"{len(stall_failed)} net(s) unrouted -- engaging "
                    f"BLOCKED_BY_COMPONENT rip-up ({elapsed_str()})"
                )
                rescued_count = 0
                # Issue #2527: Use a per-net rip-up budget of at least 3 here
                # (matching the negotiated ``route_all`` default).  Connector-
                # adjacent escapes routinely need 2-3 rip-ups before they
                # converge, and this stall path runs at most once before the
                # iteration loop takes over.
                stall_budget = 3
                for failed_net in list(stall_failed):
                    if check_timeout():
                        timed_out = True
                        break
                    rescued = self._attempt_blocked_component_ripup(
                        failed_net=failed_net,
                        neg_router=neg_router,
                        net_routes=net_routes,
                        pads_by_net=pads_by_net_local,
                        ripup_history=ripup_history,
                        present_cost_factor=present_factor,
                        max_ripups_per_net=stall_budget,
                        per_net_timeout=per_net_timeout,
                    )
                    if rescued:
                        rescued_count += 1
                if rescued_count > 0:
                    flush_print(
                        f"  BLOCKED_BY_COMPONENT rip-up resolved "
                        f"{rescued_count}/{len(stall_failed)} net(s) "
                        f"({elapsed_str()})"
                    )
                    # Recompute overflow now that new routes may have been
                    # placed by the helper -- if rip-ups introduced overflow
                    # the iteration loop below will pick it up and converge.
                    overflow = self.grid.get_total_overflow()
                    flush_print(
                        f"  Post-recovery overflow: {overflow}"
                    )


        # Issue #2317: Track overflow history for early-stop detection.
        overflow_history: list[int] = [overflow]

        # Issue #2597: Track per-iteration rip-up cohort for stagnation
        # detection.  ``ripup_set_history[k]`` is the set of net IDs ripped
        # up at the start of outer iteration ``k+1`` (the initial pass is
        # not represented).  Combined with ``overflow_history`` this lets
        # us detect the chorus-test pattern where consecutive iterations
        # tear up the *same* nets and produce only marginal overflow
        # improvement — the existing ``should_terminate_early()`` heuristic
        # cannot see this because the overflow trajectory is strictly
        # decreasing.
        ripup_set_history: list[set[int]] = []

        # Issue #2597: stagnation flag returned to the caller via the
        # ``last_termination_reason`` attribute so the progress callback can
        # distinguish ``"stagnated"`` from ``"timeout"`` and bare
        # ``f"overflow={N}"``.
        stagnation_detected = False

        # Issue #2305: Track best routing state across iterations.
        # Overflow can oscillate during rip-up-and-reroute; if timeout or
        # iteration limit is hit during a high-overflow iteration we want to
        # return the best state observed, not the last one.
        best_overflow = overflow
        best_routes: list[Route] = copy.deepcopy(list(self.routes))
        best_net_routes: dict[int, list[Route]] = copy.deepcopy(net_routes)
        best_iteration = 0  # 0 = initial pass

        # Rip-up and reroute iterations if needed.
        # Issue #2518: skip the entire iteration loop if the initial pass
        # was cut short by the wall-clock budget — otherwise we burn another
        # ~iteration-budget of work after the budget is already exhausted.
        if overflow > 0 and not timed_out:
            history_increment = 1.0
            present_factor_increment = 0.5

            for iteration in range(1, max_iterations + 1):
                if check_timeout():
                    flush_print(f"  ⚠ Timeout at iteration {iteration} ({elapsed_str()})")
                    timed_out = True
                    break

                if progress_callback is not None:
                    progress = 0.3 + 0.6 * (iteration / max_iterations)
                    if not progress_callback(
                        progress, f"Iteration {iteration}/{max_iterations}", True
                    ):
                        break

                present_factor += present_factor_increment
                self.grid.update_history_costs(history_increment)

                # Issue #2288: Relax corridor constraint as iterations progress
                # so the detailed router can escape suboptimal global corridors.
                # Issue #2308: Decay rate and floor are now configurable via
                # DesignRules.corridor_decay_rate / corridor_decay_floor.
                if corridors:
                    effective_penalty = corridor_penalty * max(
                        self.rules.corridor_decay_floor,
                        1.0 - self.rules.corridor_decay_rate * iteration,
                    )
                    for net, corridor in corridors.items():
                        self.grid.set_corridor_preference(
                            corridor, net, effective_penalty
                        )

                overused = self.grid.find_overused_cells()
                nets_to_reroute = neg_router.find_nets_through_overused_cells(net_routes, overused)
                flush_print(
                    f"  Iteration {iteration}: ripping up {len(nets_to_reroute)} nets ({elapsed_str()})"
                )

                # Issue #2597: Snapshot the rip-up cohort *before* mutating
                # ``net_routes`` so the stagnation detector can compare the
                # current iteration's targets against the previous one.
                ripup_set_history.append(set(nets_to_reroute))

                neg_router.rip_up_nets(nets_to_reroute, net_routes, self.routes)

                for i, net in enumerate(nets_to_reroute):
                    if check_timeout():
                        # Issue #2518: set the propagating flag so the
                        # outer iteration loop exits immediately too,
                        # without running the post-loop bookkeeping
                        # (overflow recompute, history snapshot,
                        # convergence check).
                        flush_print(
                            f"    ⚠ Timeout during reroute at net "
                            f"{i}/{len(nets_to_reroute)} ({elapsed_str()})"
                        )
                        timed_out = True
                        break
                    net_name = self.net_names.get(net, f"Net {net}")
                    flush_print(
                        f"    Re-routing net {i + 1}/{len(nets_to_reroute)}: "
                        f"{net_name}... ({elapsed_str()})"
                    )
                    routes = self._route_net_with_corridor(net, present_factor, per_net_timeout=per_net_timeout)
                    if routes:
                        net_routes[net] = routes
                        for route in routes:
                            self.grid.mark_route_usage(route)
                            self.routes.append(route)

                # Issue #2518: short-circuit immediately if the per-net
                # inner loop tripped the budget.  Skip overflow recompute,
                # history snapshot, convergence/early-stop checks, and the
                # next iteration so partial-state restore can run.
                if timed_out:
                    flush_print(
                        f"  ⚠ Timeout at iteration {iteration} ({elapsed_str()})"
                    )
                    break

                overflow = self.grid.get_total_overflow()
                flush_print(f"  Iteration {iteration} complete: overflow={overflow}")

                # Issue #2317: Record overflow for early-stop detection.
                overflow_history.append(overflow)

                # Issue #2305: Snapshot state when overflow improves
                if overflow < best_overflow:
                    best_overflow = overflow
                    best_routes = copy.deepcopy(list(self.routes))
                    best_net_routes = copy.deepcopy(net_routes)
                    best_iteration = iteration

                if overflow == 0:
                    flush_print(f"  Converged at iteration {iteration}!")
                    break

                # Issue #2317: Early-stop when overflow regresses or
                # stagnates across iterations.  Reuses the battle-tested
                # ``should_terminate_early()`` from the negotiated router
                # (Issues #633, #1823, #2295, #2297).
                if should_terminate_early(
                    overflow_history, iteration, min_iterations=patience
                ):
                    flush_print(
                        f"  Early stop: overflow not improving "
                        f"(best={best_overflow})"
                    )
                    break

                # Issue #2597: Detect rip-up cohort stagnation that
                # ``should_terminate_early()`` cannot see.  When the same
                # nets get torn up across consecutive iterations and the
                # overflow needle barely moves, the next iteration is very
                # likely to repeat the same ~per-net-timeout × N seconds of
                # work without escaping the local minimum.  The chorus-test
                # pattern is ``ripup=[{A..F}, {A..F}], overflow=[30, 12, 10]``
                # — strictly decreasing overflow keeps the standard
                # heuristic silent, but iteration 3 is doomed to burn ~100 s
                # of wall-clock budget before producing the same six routes.
                if detect_ripup_stagnation(
                    ripup_set_history,
                    overflow_history,
                    overflow_delta_threshold=self.rules.stagnation_overflow_delta_threshold,
                    jaccard_threshold=self.rules.stagnation_jaccard_threshold,
                ):
                    prev_ov = overflow_history[-2]
                    curr_ov = overflow_history[-1]
                    if prev_ov > 0:
                        improvement_pct = (
                            100.0 * (prev_ov - curr_ov) / prev_ov
                        )
                    else:
                        improvement_pct = 0.0
                    flush_print(
                        f"  Stagnation detected: rip-up set unchanged, "
                        f"overflow plateau ({prev_ov} → {curr_ov}, "
                        f"{improvement_pct:.1f}%)"
                    )
                    stagnation_detected = True
                    break

        # Issue #2305: Restore best state if the final iteration is worse
        final_overflow = self.grid.get_total_overflow()
        if best_overflow < final_overflow:
            flush_print(
                f"  Restoring iteration {best_iteration} state "
                f"(overflow={best_overflow}) instead of final "
                f"(overflow={final_overflow})"
            )
            # Unmark all current routes from the grid
            for route in list(self.routes):
                self.grid.unmark_route_usage(route)
            # Replace with best-state routes
            self.routes.clear()
            self.routes.extend(best_routes)
            # Re-mark best routes on the grid
            for route in self.routes:
                self.grid.mark_route_usage(route)
            # Update net_routes to best state
            net_routes.clear()
            net_routes.update(best_net_routes)

        # Issue #2597: Surface the iteration-loop exit reason to the caller
        # so the progress-callback status string can distinguish between
        # ``"stagnated"`` (rip-up cohort stuck), ``"timeout"`` (wall clock
        # exhausted), and bare ``f"overflow={N}"`` (other early stop or
        # ``max_iterations`` reached).  ``stagnated`` takes precedence over
        # ``timeout`` because the stagnation detector breaks out *before*
        # iteration N+1 has a chance to trip the wall-clock check.
        effective_overflow = min(best_overflow, final_overflow)
        if stagnation_detected:
            self.last_termination_reason = "stagnated"
        elif effective_overflow == 0:
            self.last_termination_reason = "converged"
        elif timed_out:
            self.last_termination_reason = "timeout"
        else:
            # Loop hit ``max_iterations`` or ``should_terminate_early()``;
            # both fall under the catch-all ``f"overflow={N}"`` status in
            # the caller's progress message, so signal ``early_stop`` here.
            self.last_termination_reason = "early_stop"

        return list(self.routes)

    def _detailed_standard(
        self,
        net_order: list[int],
        progress_callback: ProgressCallback | None,
        timeout: float | None,
        start_time: float,
    ) -> list[Route]:
        """Detailed routing phase using standard routing (no negotiation)."""

        def check_timeout() -> bool:
            if timeout is None:
                return False
            return time.time() - start_time >= timeout

        total_nets = len(net_order)
        all_routes: list[Route] = []

        for i, net in enumerate(net_order):
            if check_timeout():
                print(f"  ⚠ Timeout at net {i}/{total_nets} ({time.time() - start_time:.1f}s)")
                break

            if progress_callback is not None:
                progress = 0.3 + 0.7 * (i / total_nets)
                net_name = self.net_names.get(net, f"Net {net}")
                if not progress_callback(progress, f"Routing {net_name}", True):
                    break

            routes = self._route_net(net)
            all_routes.extend(routes)

        return all_routes
