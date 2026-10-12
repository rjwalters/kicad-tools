"""Monte Carlo multi-start routing algorithm.

This module provides randomized net ordering to escape local minima
caused by unfortunate routing order decisions. Includes both the core
MonteCarloRouter class and the orchestration function for running
multi-trial Monte Carlo routing.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import math
import os
import random
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from ..primitives import Route


class MonteCarloRouter:
    """Monte Carlo multi-start router.

    Tries multiple random net orderings within priority tiers,
    keeping the best result to escape local minima.
    """

    def __init__(self, total_nets: int):
        """Initialize the Monte Carlo router.

        Args:
            total_nets: Total number of nets (excluding net 0)
        """
        self.total_nets = total_nets

    def shuffle_within_tiers(
        self,
        net_order: list[int],
        get_priority: callable,
    ) -> list[int]:
        """Shuffle nets but preserve priority tier ordering.

        Groups by (net_class_priority, complexity_tier) so that simple 2-pin
        short nets and complex multi-pin/long-span nets within the same net
        class are not mixed together.

        Args:
            net_order: Original net order
            get_priority: Function that takes net_id and returns priority tuple
                where element 0 is net class priority and element 1 is
                complexity tier.

        Returns:
            New net order with shuffled tiers
        """
        # Group by (net_class_priority, complexity_tier)
        tiers: dict[tuple[int, int], list[int]] = {}
        for net in net_order:
            priority_tuple = get_priority(net)
            key = (priority_tuple[0], priority_tuple[1])
            if key not in tiers:
                tiers[key] = []
            tiers[key].append(net)

        # Shuffle within each tier and reassemble in sorted key order
        result: list[int] = []
        for key in sorted(tiers.keys()):
            tier_nets = tiers[key].copy()
            random.shuffle(tier_nets)
            result.extend(tier_nets)

        return result

    def shuffle_with_promotions(
        self,
        net_order: list[int],
        get_priority: callable,
        promotion_rate: float = 0.2,
    ) -> list[int]:
        """Shuffle within (priority, complexity_tier) groups with cross-tier promotions.

        Occasionally promotes tier-1 (complex/long-span) nets to tier-0
        (simple/short) position within the same net class priority, biasing
        exploration toward orderings where long-span nets get early corridor
        access.

        Cross-tier promotions never cross net class priority boundaries -- a
        debug net (priority 5) is never promoted ahead of a clock net
        (priority 2).

        Args:
            net_order: Original net order
            get_priority: Function that takes net_id and returns priority tuple
            promotion_rate: Probability of promoting each tier-1 net to tier 0
                within the same net class priority (0.0 = no promotions,
                1.0 = always promote)

        Returns:
            New net order with shuffled tiers and cross-tier promotions
        """
        # Group by (net_class_priority, complexity_tier)
        groups: dict[tuple[int, int], list[int]] = {}
        for net in net_order:
            pt = get_priority(net)
            key = (pt[0], pt[1])
            groups.setdefault(key, []).append(net)

        # Promote: move some tier-1 nets to tier-0 within same net class priority
        priorities = {k[0] for k in groups}
        for p in priorities:
            src_key = (p, 1)
            dst_key = (p, 0)
            if src_key not in groups:
                continue
            promoted = []
            remaining = []
            for net in groups[src_key]:
                if random.random() < promotion_rate:
                    promoted.append(net)
                else:
                    remaining.append(net)
            if promoted:
                groups.setdefault(dst_key, []).extend(promoted)
                groups[src_key] = remaining

        # Shuffle within each group and reassemble in sorted key order
        result: list[int] = []
        for key in sorted(groups.keys()):
            nets = groups[key].copy()
            random.shuffle(nets)
            result.extend(nets)
        return result

    def evaluate_solution(
        self, routes: list[Route], drc_violations: int = 0, drc_weight: float = 50.0
    ) -> float:
        """Score a routing solution (higher = better).

        Scoring prioritizes:
        1. Completion rate (primary - weighted heavily)
        2. DRC violation count (penalizes unmanufacturable solutions)
        3. Lower via count (secondary)
        4. Shorter total length (tertiary)

        At the default ``drc_weight`` of 50, each DRC violation cancels
        roughly one net completion (~50 points from ``completion_rate * 1000``
        for each net in a typical design).

        Args:
            routes: List of routes in the solution
            drc_violations: Number of DRC clearance violations in the solution
            drc_weight: Penalty per DRC violation (default 50.0)

        Returns:
            Solution score (higher is better)
        """
        if not routes:
            return 0.0

        routed_nets = len({r.net for r in routes})
        completion_rate = routed_nets / self.total_nets if self.total_nets > 0 else 0

        total_vias = sum(len(r.vias) for r in routes)
        total_length = sum(
            math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
        )

        # Completion rate is most important (1000x weight)
        # DRC violations penalized heavily to discourage unmanufacturable solutions
        # Penalize vias and length slightly
        return (
            completion_rate * 1000
            - drc_violations * drc_weight
            - total_vias * 0.1
            - total_length * 0.01
        )


# ---------------------------------------------------------------------------
# Issue #6343: best-trial publication for interrupted multi-start runs
# ---------------------------------------------------------------------------
#
# Monte Carlo and evolutionary routing keep their best trial in LOCAL variables
# (and, on the parallel path, the trials themselves run in worker processes),
# copying it into ``autorouter.routes`` only once the whole run finishes.  A
# deadline or Ctrl+C that unwinds mid-run therefore found ``router.routes``
# empty and the partial save wrote a board with no copper after 20 minutes of
# trials.  Each run now publishes every newly accepted best on the router as
# it is found; the CLI's partial save (``_save_partial_results``) restores it
# before serializing.  The attributes are cleared when the run finishes
# normally (its result is then in ``autorouter.routes``) or fails with an
# ordinary ``Exception`` (the caller carries on, so a stale best must never be
# substituted for a later stage's copper).  A ``BaseException`` unwind --
# ``RouteDeadlineExpired``, ``KeyboardInterrupt`` -- leaves them in place on
# purpose: that is exactly the case the save needs them for.


def clear_best_trial(autorouter) -> None:
    """Forget any best trial published by a previous multi-start run."""
    autorouter._best_trial_routes = None
    autorouter._best_trial_meta = None


def publish_best_trial(
    autorouter,
    routes: list[Route] | None,
    *,
    strategy: str,
    trial: int,
    score: float,
) -> None:
    """Record a newly accepted best trial on the router (issue #6343).

    Cheap by design: a shallow list copy, no grid replay.  The partial save
    only needs ``router.routes`` to serialize copper.
    """
    autorouter._best_trial_routes = list(routes) if routes else []
    autorouter._best_trial_meta = {"strategy": strategy, "trial": trial, "score": score}


def terminate_executor(executor: ProcessPoolExecutor) -> None:
    """Stop a process pool without waiting for running trials (issue #6343).

    ``shutdown(wait=True)`` -- what ``with ProcessPoolExecutor()`` does on
    exit -- joins workers that may each be minutes into a trial.  On a
    timeout or an interrupt the parent must return (or save) now, so cancel
    the queued trials and terminate the running ones.
    """
    terminate = getattr(executor, "terminate_workers", None)  # Python >= 3.14
    if terminate is not None:
        with contextlib.suppress(Exception):
            terminate()
        return
    # Older Pythons: snapshot the worker processes BEFORE ``shutdown``, which
    # drops the executor's reference to them.  Left running, they would also
    # be joined by ``concurrent.futures``' interpreter-exit hook.
    processes = list((getattr(executor, "_processes", None) or {}).values())
    with contextlib.suppress(Exception):
        executor.shutdown(wait=False, cancel_futures=True)
    for process in processes:
        with contextlib.suppress(Exception):
            process.terminate()


def deadline_from_timeout(timeout: float | None) -> float | None:
    """Monotonic deadline for an optional wall-clock budget in seconds."""
    return None if timeout is None else time.monotonic() + max(0.0, float(timeout))


def remaining_seconds(deadline: float | None) -> float | None:
    """Seconds left before ``deadline`` (never negative), or ``None``."""
    return None if deadline is None else max(0.0, deadline - time.monotonic())


# ---------------------------------------------------------------------------
# Issue #6345: route one multi-start trial in the order it was given
# ---------------------------------------------------------------------------
#
# ``Autorouter.route_all(net_order)`` re-sorts a caller-supplied order by
# ``_get_net_priority``, whose key is effectively unique per net, so every
# Monte Carlo shuffle and every evolutionary ``net_order`` gene was undone and
# all trials routed the same sequence (byte-identical results on board 05).
# ``route_all_negotiated()`` takes no order at all.  The one seam both entry
# points honour without the re-sort is ``_forced_net_order`` (#3897), so a
# trial installs its order there for the duration of the call.


def _initial_trial_order(autorouter) -> list[int]:
    """Base net order for a multi-start run (trial 0 / seed chromosome).

    Honours a user ``--order-method`` order (``_forced_net_order``) when one
    is installed, else the priority sort -- the same order a single default
    run would route.  Pour nets and net 0 are filtered out.
    """
    forced = getattr(autorouter, "_forced_net_order", None)
    if forced is not None:
        base_order = [n for n in forced if n in autorouter.nets]
    else:
        base_order = sorted(autorouter.nets.keys(), key=lambda n: autorouter._get_net_priority(n))
    # Issue #1295: Filter out pour nets before multi-start trials
    base_order = autorouter._filter_pour_nets(base_order)
    return [n for n in base_order if n != 0]


def route_trial(
    router,
    net_order: list[int] | None,
    *,
    use_negotiated: bool = False,
    negotiated_kwargs: dict | None = None,
    seed: int | None = None,
    timeout: float | None = None,
    per_net_timeout: float | None = None,
) -> list[Route]:
    """Route every net of ``router`` in exactly ``net_order`` (issue #6345).

    The order is installed as ``router._forced_net_order`` for the call, so
    neither ``route_all`` nor ``route_all_negotiated`` re-sorts it by priority
    (matrix layer preferences, match-group interleaving and byte-lane slots
    still apply, as for ``kct route --order-method``).  The previous forced
    order is restored afterwards so a parent router is never left forced.

    Args:
        router: The Autorouter to route on (already reset for this trial).
        net_order: Order the trial must route in.  ``None`` routes in the
            router's own default order (exactly what a single default run
            does, matrix-preference re-sort included) -- used for trial 0
            so the baseline stays faithful.
        use_negotiated: Run ``route_all_negotiated`` (rip-up / negotiation)
            instead of a single ``route_all`` pass.
        negotiated_kwargs: Extra keyword arguments for
            ``route_all_negotiated`` (iterations, patience, ...).  Must not
            contain ``seed``, ``timeout`` or ``per_net_timeout``.
        seed: Seed for the negotiated router's perturbation.
        timeout: Wall-clock budget for this trial in seconds, or ``None``.
        per_net_timeout: Per-net wall-clock cap in seconds, or ``None``.
    """
    previous = router._forced_net_order
    if net_order is not None:
        router._forced_net_order = list(net_order)
    try:
        routes: list[Route]
        if use_negotiated:
            routes = router.route_all_negotiated(
                **(negotiated_kwargs or {}),
                seed=seed,
                timeout=timeout,
                per_net_timeout=per_net_timeout,
            )
        else:
            routes = router.route_all(
                timeout=timeout,
                per_net_timeout=per_net_timeout,
                suppress_no_timeout_warning=True,
            )
        return routes
    finally:
        router._forced_net_order = previous


def baseline_trial_order(autorouter, order: list[int], base_order: list[int]) -> list[int] | None:
    """The order to force for a trial, or ``None`` for the router default.

    A trial whose order is the unperturbed base order routes with no forced
    order when the user installed none, so it is byte-identical to a single
    default run (the default path re-sorts after matrix-preference boosts,
    which a forced order skips).
    """
    if getattr(autorouter, "_forced_net_order", None) is None and list(order) == list(base_order):
        return None
    return list(order)


def trial_timeout(deadline: float | None, trials_left: int, workers: int = 1) -> float | None:
    """Per-trial wall-clock budget: the time left over the remaining waves.

    ``trials_left`` trials on ``workers`` processes take
    ``ceil(trials_left / workers)`` sequential waves; each trial gets an equal
    share of what is left so the last wave is not starved (#2794, #6345).

    The share keeps headroom (10%, at least 90 s, at most half) for the work a
    trial does past its routing budget -- the negotiated post-sweep (bounded
    at 60 s), solution scoring and returning the routes to the parent.  On
    board 05 four negotiated trials given the full share finished within
    seconds of the parent's deadline, which would have abandoned all of them.
    """
    left = remaining_seconds(deadline)
    if left is None:
        return None
    waves = max(1, math.ceil(max(1, trials_left) / max(1, workers)))
    share = left / waves
    headroom = min(0.5 * share, max(0.1 * share, 90.0))
    return max(1.0, share - headroom)


def run_monte_carlo(
    autorouter,
    num_trials: int = 10,
    use_negotiated: bool = False,
    seed: int | None = None,
    verbose: bool = True,
    progress_callback: ProgressCallback | None = None,
    num_workers: int | None = None,
    timeout: float | None = None,
    negotiated_kwargs: dict | None = None,
    per_net_timeout: float | None = None,
) -> list[Route]:
    """Run Monte Carlo multi-start routing on an Autorouter instance.

    Orchestrates multiple routing trials with randomized net orderings,
    keeping the best result. Supports both sequential and parallel execution.

    Args:
        autorouter: The Autorouter instance to run trials on
        num_trials: Number of routing trials to run
        use_negotiated: Whether to use negotiated congestion routing
        seed: Random seed for reproducibility
        verbose: Whether to print progress information
        progress_callback: Optional callback for progress updates
        num_workers: Number of parallel workers. None or 0 for auto-detection
            based on CPU count. 1 for sequential execution.
        timeout: Optional wall-clock budget in seconds (issue #6343).  When it
            runs out no further trial is started, trials still running in
            worker processes are abandoned, and the best trial completed so
            far is returned.  ``None`` (default) means no wall-clock limit.
            Each trial is also given an equal share of what is left (issue
            #6345) so a negotiated trial cannot eat the whole budget.
        negotiated_kwargs: Extra ``route_all_negotiated`` keyword arguments
            for negotiated trials (issue #6345).
        per_net_timeout: Per-net wall-clock cap forwarded to every trial.

    Each trial routes in its own order (issue #6345): trial 0 in the default
    order, later trials in a within-tier shuffle with promotions.

    Returns:
        List of routes from the best trial
    """
    clear_best_trial(autorouter)
    try:
        return _run_monte_carlo(
            autorouter,
            num_trials=num_trials,
            use_negotiated=use_negotiated,
            seed=seed,
            verbose=verbose,
            progress_callback=progress_callback,
            num_workers=num_workers,
            timeout=timeout,
            negotiated_kwargs=negotiated_kwargs,
            per_net_timeout=per_net_timeout,
        )
    except Exception:
        clear_best_trial(autorouter)
        raise


def _run_monte_carlo(
    autorouter,
    *,
    num_trials: int,
    use_negotiated: bool,
    seed: int | None,
    verbose: bool,
    progress_callback: ProgressCallback | None,
    num_workers: int | None,
    timeout: float | None,
    negotiated_kwargs: dict | None = None,
    per_net_timeout: float | None = None,
) -> list[Route]:
    deadline = deadline_from_timeout(timeout)
    base_seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    random.seed(base_seed)

    # Determine number of workers
    if num_workers is None or num_workers <= 0:
        num_workers = min(num_trials, os.cpu_count() or 4)
    num_workers = min(num_workers, num_trials)

    if verbose:
        print("\n=== Monte Carlo Multi-Start Routing ===")
        print(f"  Trials: {num_trials}, Negotiated: {use_negotiated}")
        if num_workers > 1:
            print(f"  Parallel workers: {num_workers}")
        if timeout is not None:
            print(f"  Timeout: {timeout:.1f}s")

    # Issue #6345: honour a user ``--order-method`` order as the base.
    base_order = _initial_trial_order(autorouter)

    best_routes: list[Route] | None = None
    best_score, best_trial = float("-inf"), -1

    # Use parallel execution if num_workers > 1
    if num_workers > 1:
        try:
            best_routes, best_score, best_trial = _run_parallel(
                autorouter=autorouter,
                num_trials=num_trials,
                use_negotiated=use_negotiated,
                base_seed=base_seed,
                base_order=base_order,
                num_workers=num_workers,
                verbose=verbose,
                progress_callback=progress_callback,
                deadline=deadline,
                negotiated_kwargs=negotiated_kwargs,
                per_net_timeout=per_net_timeout,
            )
        except Exception as e:
            if verbose:
                print(f"  ⚠ Parallel execution failed: {e}")
                print("  Falling back to sequential execution...")
            # Fall back to sequential execution
            num_workers = 1

    # Sequential execution (num_workers == 1 or fallback)
    if num_workers == 1:
        for trial in range(num_trials):
            if deadline is not None and time.monotonic() >= deadline:
                if verbose:
                    print(
                        f"  Timeout {timeout:.1f}s reached before trial {trial + 1}; "
                        f"returning best (score={best_score:.2f})"
                    )
                break
            if progress_callback is not None:
                if not progress_callback(
                    trial / num_trials, f"Trial {trial + 1}/{num_trials}", True
                ):
                    break

            random.seed(base_seed + trial)
            autorouter._reset_for_new_trial()
            if trial == 0:
                net_order = base_order.copy()
            else:
                # Increasing promotion rate over trials to broaden exploration
                promotion_rate = min(0.1 + 0.05 * trial, 0.5)
                net_order = autorouter._shuffle_within_tiers(
                    base_order, promotion_rate=promotion_rate
                )
            # Issue #6345: route in THIS trial's order (``route_all(order)``
            # re-sorted it back to the priority order, and the negotiated
            # call took no order at all, so every trial was identical).
            routes = route_trial(
                autorouter,
                baseline_trial_order(autorouter, net_order, base_order),
                use_negotiated=use_negotiated,
                negotiated_kwargs=negotiated_kwargs,
                seed=base_seed + trial,
                timeout=trial_timeout(deadline, num_trials - trial),
                per_net_timeout=per_net_timeout,
            )
            score = autorouter._evaluate_solution(routes)

            if verbose:
                status = "NEW BEST" if score > best_score else ""
                print(
                    f"  Trial {trial + 1}: {len({r.net for r in routes})}/{len(base_order)} nets, "
                    f"{sum(len(r.vias) for r in routes)} vias, score={score:.2f} {status}"
                )

            if score > best_score:
                best_score, best_routes, best_trial = score, routes.copy(), trial
                publish_best_trial(
                    autorouter, best_routes, strategy="monte-carlo", trial=trial, score=score
                )

    if verbose:
        print(f"\n  Best: Trial {best_trial + 1} (score={best_score:.2f})")

    # Issue #2634: The grid state at the end of the trial loop reflects whatever
    # the *last* trial left behind, not the *best* trial whose routes we are
    # about to surface.  ``_reset_for_new_trial`` rebuilds the grid and re-adds
    # pads but never re-marks routes; the parallel path skips this entirely
    # because all trials ran in worker processes.  Downstream consumers — the
    # layer-escalation overflow heuristic at ``route_cmd.py``, zone fills,
    # statistics — read grid state expecting it to match ``autorouter.routes``.
    # Rebuild the grid once more and replay the best routes onto it so that
    # invariant holds.  Note: ``_reset_for_new_trial`` clears ``autorouter.routes``,
    # so the assignment must happen *after* the reset.
    autorouter._reset_for_new_trial()
    autorouter.routes = best_routes if best_routes else []
    for route in autorouter.routes:
        autorouter.grid.mark_route_usage(route)
    # Issue #6343: the best trial now lives in ``autorouter.routes``; a later
    # stage owns them from here, so the partial save must not swap them back.
    clear_best_trial(autorouter)

    if progress_callback is not None:
        routed = len({r.net for r in autorouter.routes}) if autorouter.routes else 0
        progress_callback(
            1.0, f"Best: trial {best_trial + 1}, {routed}/{len(base_order)} nets", False
        )

    return autorouter.routes


def _run_parallel(
    autorouter,
    num_trials: int,
    use_negotiated: bool,
    base_seed: int,
    base_order: list[int],
    num_workers: int,
    verbose: bool,
    progress_callback: ProgressCallback | None,
    deadline: float | None = None,
    negotiated_kwargs: dict | None = None,
    per_net_timeout: float | None = None,
) -> tuple[list[Route] | None, float, int]:
    """Run Monte Carlo trials in parallel using ProcessPoolExecutor.

    Args:
        autorouter: The Autorouter instance (used for serialization)
        num_trials: Total number of trials to run
        use_negotiated: Whether to use negotiated routing
        base_seed: Base random seed
        base_order: Base net ordering
        num_workers: Number of parallel workers
        verbose: Whether to print progress
        progress_callback: Optional progress callback
        deadline: Optional ``time.monotonic()`` deadline (issue #6343).  When
            it passes, trials still running are abandoned and the best
            completed trial is returned.

    Returns:
        Tuple of (best_routes, best_score, best_trial)
    """
    # Import here to avoid circular imports - the trial runner is in core.py
    from ..core import _run_monte_carlo_trial

    # Serialize current state for workers
    base_config = autorouter._serialize_for_parallel()
    # Issue #6345: every trial gets an equal share of the budget, sized for
    # ``ceil(num_trials / num_workers)`` waves.
    per_trial_timeout = trial_timeout(deadline, num_trials, num_workers)
    # Trial 0 routes in the router's default order unless the user forced one.
    base_order_forced = getattr(autorouter, "_forced_net_order", None) is not None

    # Create configs for each trial
    trial_configs = []
    for trial in range(num_trials):
        config = base_config.copy()
        config.update(
            {
                "trial_num": trial,
                "seed": base_seed + trial,
                "base_order": base_order,
                "base_order_forced": base_order_forced,
                "use_negotiated": use_negotiated,
                "negotiated_kwargs": dict(negotiated_kwargs or {}),
                "trial_timeout": per_trial_timeout,
                "per_net_timeout": per_net_timeout,
            }
        )
        trial_configs.append(config)

    best_routes: list[Route] | None = None
    best_score = float("-inf")
    best_trial = -1
    completed = 0

    executor = ProcessPoolExecutor(max_workers=num_workers)
    try:
        # Submit all tasks
        futures = {
            executor.submit(_run_monte_carlo_trial, config): config["trial_num"]
            for config in trial_configs
        }

        # Process results as they complete.  Issue #6343: bounded by the
        # deadline so a timeout returns the best completed trial instead of
        # waiting minutes on the trials still running.
        try:
            for future in as_completed(futures, timeout=remaining_seconds(deadline)):
                trial_num = futures[future]
                try:
                    routes, score, _ = future.result()
                    completed += 1

                    if progress_callback is not None:
                        if not progress_callback(
                            completed / num_trials,
                            f"Completed {completed}/{num_trials} trials",
                            True,
                        ):
                            # Cancel remaining futures
                            for f in futures:
                                f.cancel()
                            break

                    is_new_best = score > best_score
                    if is_new_best:
                        best_score = score
                        best_routes = routes
                        best_trial = trial_num
                        publish_best_trial(
                            autorouter,
                            routes,
                            strategy="monte-carlo",
                            trial=trial_num,
                            score=score,
                        )

                    if verbose:
                        status = "NEW BEST" if is_new_best else ""
                        net_count = len({r.net for r in routes}) if routes else 0
                        via_count = sum(len(r.vias) for r in routes) if routes else 0
                        print(
                            f"  Trial {trial_num + 1}: {net_count}/{len(base_order)} nets, "
                            f"{via_count} vias, score={score:.2f} {status}"
                        )

                except Exception as e:
                    if verbose:
                        print(f"  Trial {trial_num + 1}: FAILED - {e}")
                    completed += 1
        except concurrent.futures.TimeoutError:
            if verbose:
                print(
                    f"  Timeout reached after {completed}/{num_trials} trials; "
                    f"abandoning the rest, returning best (score={best_score:.2f})"
                )
            terminate_executor(executor)
            return best_routes, best_score, best_trial
    except BaseException:
        # A deadline (``RouteDeadlineExpired``) or Ctrl+C unwinding through
        # here must not wait for the workers to finish their trials: that
        # join would eat the partial save's grace period.
        terminate_executor(executor)
        raise

    executor.shutdown(wait=True)
    return best_routes, best_score, best_trial
