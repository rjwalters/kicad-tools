"""Monte Carlo multi-start routing algorithm.

This module provides randomized net ordering to escape local minima
caused by unfortunate routing order decisions. Includes both the core
MonteCarloRouter class and the orchestration function for running
multi-trial Monte Carlo routing.
"""

from __future__ import annotations

import math
import os
import random
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

        Args:
            net_order: Original net order
            get_priority: Function that takes net_id and returns priority tuple
                (e.g., (priority, pad_count, distance))

        Returns:
            New net order with shuffled tiers
        """
        # Group by priority tier (use first element of priority tuple)
        tiers: dict[int, list[int]] = {}
        for net in net_order:
            priority_tuple = get_priority(net)
            tier = priority_tuple[0]  # First element is the net class priority
            if tier not in tiers:
                tiers[tier] = []
            tiers[tier].append(net)

        # Shuffle within each tier and reassemble
        result: list[int] = []
        for priority in sorted(tiers.keys()):
            tier_nets = tiers[priority].copy()
            random.shuffle(tier_nets)
            result.extend(tier_nets)

        return result

    def evaluate_solution(self, routes: list[Route]) -> float:
        """Score a routing solution (higher = better).

        Scoring prioritizes:
        1. Completion rate (primary - weighted heavily)
        2. Lower via count (secondary)
        3. Shorter total length (tertiary)

        Args:
            routes: List of routes in the solution

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
        # Penalize vias and length slightly
        return completion_rate * 1000 - total_vias * 0.1 - total_length * 0.01


def run_monte_carlo(
    autorouter,
    num_trials: int = 10,
    use_negotiated: bool = False,
    seed: int | None = None,
    verbose: bool = True,
    progress_callback: ProgressCallback | None = None,
    num_workers: int | None = None,
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

    Returns:
        List of routes from the best trial
    """
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

    base_order = sorted(autorouter.nets.keys(), key=lambda n: autorouter._get_net_priority(n))
    base_order = [n for n in base_order if n != 0]

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
            if progress_callback is not None:
                if not progress_callback(
                    trial / num_trials, f"Trial {trial + 1}/{num_trials}", True
                ):
                    break

            random.seed(base_seed + trial)
            autorouter._reset_for_new_trial()
            net_order = (
                base_order.copy()
                if trial == 0
                else autorouter._shuffle_within_tiers(base_order)
            )
            routes = (
                autorouter.route_all_negotiated()
                if use_negotiated
                else autorouter.route_all(net_order)
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

    if verbose:
        print(f"\n  Best: Trial {best_trial + 1} (score={best_score:.2f})")

    autorouter.routes = best_routes if best_routes else []
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

    Returns:
        Tuple of (best_routes, best_score, best_trial)
    """
    # Import here to avoid circular imports - the trial runner is in core.py
    from ..core import _run_monte_carlo_trial

    # Serialize current state for workers
    base_config = autorouter._serialize_for_parallel()

    # Create configs for each trial
    trial_configs = []
    for trial in range(num_trials):
        config = base_config.copy()
        config.update(
            {
                "trial_num": trial,
                "seed": base_seed + trial,
                "base_order": base_order,
                "use_negotiated": use_negotiated,
            }
        )
        trial_configs.append(config)

    best_routes: list[Route] | None = None
    best_score = float("-inf")
    best_trial = -1
    completed = 0

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        # Submit all tasks
        futures = {
            executor.submit(_run_monte_carlo_trial, config): config["trial_num"]
            for config in trial_configs
        }

        # Process results as they complete
        for future in as_completed(futures):
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

    return best_routes, best_score, best_trial
