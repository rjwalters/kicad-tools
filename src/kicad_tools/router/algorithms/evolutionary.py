"""Evolutionary routing optimizer (GA-style selection, crossover, mutation).

Extends the Monte Carlo multi-start routing approach with population-based
search.  Instead of independent random trials, this module maintains a
population of *RoutingChromosomes* encoding per-net parameters (net ordering,
A* weight, preferred start layer, strategy flags) and evolves them using
tournament selection, order crossover (OX), and mutation operators.

The evolutionary loop reuses the same infrastructure as Monte Carlo routing
(``_serialize_for_parallel``, ``_run_monte_carlo_trial``-style workers) so
that each chromosome evaluation is a full ``route_all()`` call, and parallel
evaluation is supported via ``ProcessPoolExecutor``.

Example CLI usage::

    kct route board.kicad_pcb --strategy evolutionary --pop-size 20 --generations 10
"""

from __future__ import annotations

import concurrent.futures
import math
import os
import random
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .monte_carlo import (
    _initial_trial_order,
    baseline_trial_order,
    clear_best_trial,
    deadline_from_timeout,
    publish_best_trial,
    remaining_seconds,
    route_trial,
    terminate_executor,
    trial_timeout,
)

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

    from ..primitives import Route


# ---------------------------------------------------------------------------
# Chromosome
# ---------------------------------------------------------------------------


@dataclass
class RoutingChromosome:
    """Encodes per-net routing parameters for the evolutionary optimizer.

    Only ``net_order`` reaches the router (issue #6345).  The
    ``astar_weights``, ``preferred_layers`` and ``strategy_flags`` genes are
    evolved but currently **inert**: no evaluation path forwards them, because
    ``Autorouter`` has no per-net A* heuristic-weight hook and the per-net
    layer-preference hook (``net_class_map`` ``preferred_layers``) has no
    "no preference" value the seed chromosome could carry without changing the
    baseline.  Whether an evaluation uses negotiated routing is a run-wide
    choice (``use_negotiated``), not a per-net gene.  Plumbing the remaining
    genes is tracked as a follow-up to #6345.

    Attributes:
        net_order: Permutation of net IDs (routing sequence).
        astar_weights: Per-net A* heuristic weight multiplier (inert).
        preferred_layers: Per-net preferred start layer index (0 = F.Cu,
            1 = B.Cu, etc.) (inert).
        strategy_flags: Per-net boolean flags (inert).
        fitness: Cached fitness score (higher is better).
    """

    net_order: list[int] = field(default_factory=list)
    astar_weights: dict[int, float] = field(default_factory=dict)
    preferred_layers: dict[int, int] = field(default_factory=dict)
    strategy_flags: dict[int, bool] = field(default_factory=dict)
    fitness: float = float("-inf")

    def copy(self) -> RoutingChromosome:
        """Return a deep copy."""
        return RoutingChromosome(
            net_order=list(self.net_order),
            astar_weights=dict(self.astar_weights),
            preferred_layers=dict(self.preferred_layers),
            strategy_flags=dict(self.strategy_flags),
            fitness=self.fitness,
        )


# ---------------------------------------------------------------------------
# Genetic operators
# ---------------------------------------------------------------------------


def order_crossover(parent1: RoutingChromosome, parent2: RoutingChromosome) -> RoutingChromosome:
    """Order crossover (OX) for the net-order permutation.

    Selects a random sub-sequence from *parent1* and fills the remaining
    positions with elements from *parent2* in the order they appear,
    preserving the permutation property.

    Continuous and discrete genes use uniform crossover.
    """
    size = len(parent1.net_order)
    if size < 2:
        return parent1.copy()

    # --- permutation (OX) ---
    start = random.randint(0, size - 2)
    end = random.randint(start + 1, size - 1)

    child_order: list[int | None] = [None] * size
    child_order[start : end + 1] = parent1.net_order[start : end + 1]

    p2_remaining = [n for n in parent2.net_order if n not in child_order[start : end + 1]]
    idx = 0
    for i in range(size):
        if child_order[i] is None:
            child_order[i] = p2_remaining[idx]
            idx += 1

    child = RoutingChromosome(net_order=child_order)  # type: ignore[arg-type]

    # --- uniform crossover for continuous / discrete genes ---
    all_nets = set(parent1.astar_weights) | set(parent2.astar_weights)
    for net in all_nets:
        src = parent1 if random.random() < 0.5 else parent2
        if net in src.astar_weights:
            child.astar_weights[net] = src.astar_weights[net]
        if net in src.preferred_layers:
            child.preferred_layers[net] = src.preferred_layers[net]
        if net in src.strategy_flags:
            child.strategy_flags[net] = src.strategy_flags[net]

    return child


def mutate(chromosome: RoutingChromosome, mutation_rate: float = 0.1) -> RoutingChromosome:
    """Apply mutation operators to a chromosome (in-place, returns same ref).

    * **Swap mutation** on net order (swap two random positions).
    * **Gaussian perturbation** on A* weights.
    * **Random flip** for preferred layer and strategy flags.
    """
    # --- swap mutation on permutation ---
    if len(chromosome.net_order) >= 2 and random.random() < mutation_rate:
        i, j = random.sample(range(len(chromosome.net_order)), 2)
        chromosome.net_order[i], chromosome.net_order[j] = (
            chromosome.net_order[j],
            chromosome.net_order[i],
        )

    # --- Gaussian perturbation on A* weights ---
    for net in list(chromosome.astar_weights):
        if random.random() < mutation_rate:
            w = chromosome.astar_weights[net]
            w += random.gauss(0, 0.2)
            chromosome.astar_weights[net] = max(0.5, min(3.0, w))

    # --- random flip for preferred layer ---
    for net in list(chromosome.preferred_layers):
        if random.random() < mutation_rate * 0.5:
            chromosome.preferred_layers[net] = 1 - chromosome.preferred_layers[net]

    # --- random flip for strategy flags ---
    for net in list(chromosome.strategy_flags):
        if random.random() < mutation_rate * 0.3:
            chromosome.strategy_flags[net] = not chromosome.strategy_flags[net]

    return chromosome


def tournament_select(
    population: list[RoutingChromosome], tournament_size: int = 3
) -> RoutingChromosome:
    """Select an individual using tournament selection (replicates pattern
    from ``EvolutionaryPlacementOptimizer._tournament_select``).
    """
    tournament = random.sample(population, min(tournament_size, len(population)))
    return max(tournament, key=lambda c: c.fitness)


# ---------------------------------------------------------------------------
# Fitness evaluation helpers
# ---------------------------------------------------------------------------


def _score_routes(routes: list, total_nets: int) -> float:
    """Score a set of routes (mirrors ``MonteCarloRouter.evaluate_solution``).

    Adds a small DRC-clean bonus when 100 % completion is reached.
    """
    if not routes:
        return 0.0

    routed_nets = len({r.net for r in routes})
    completion_rate = routed_nets / total_nets if total_nets > 0 else 0

    total_vias = sum(len(r.vias) for r in routes)
    total_length = sum(
        math.sqrt((s.x2 - s.x1) ** 2 + (s.y2 - s.y1) ** 2) for r in routes for s in r.segments
    )

    score = completion_rate * 1000 - total_vias * 0.1 - total_length * 0.01

    # DRC-clean bonus: extra reward for 100 % completion
    if completion_rate >= 1.0:
        score += 50.0

    return score


# ---------------------------------------------------------------------------
# Worker function (module-level for pickling)
# ---------------------------------------------------------------------------


def _run_evolutionary_trial(config: dict) -> tuple[list, float, int]:
    """Evaluate a single chromosome in a worker process.

    Works identically to ``_run_monte_carlo_trial`` but uses the chromosome's
    net order directly instead of shuffling within tiers.

    Args:
        config: Serialized autorouter state plus chromosome data.

    Returns:
        Tuple of (routes, score, chromosome_index).
    """
    import random as _random

    from kicad_tools.router.core import (
        Autorouter,
        install_serialized_obstacles,
        worker_router_kwargs,
    )
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.rules import DesignRules

    chrom_idx = config["chrom_idx"]
    seed = config["seed"]
    # ``None`` = the router's default order (the unperturbed seed chromosome).
    net_order = config["net_order"]

    _random.seed(seed)

    # Recreate design rules
    rules_dict = config.get("rules_dict", {})
    rules = DesignRules(**rules_dict) if rules_dict else DesignRules()

    # Create new Autorouter instance
    router = Autorouter(
        width=config["width"],
        height=config["height"],
        origin_x=config["origin_x"],
        origin_y=config["origin_y"],
        rules=rules,
        net_class_map=config.get("net_class_map"),
        physics_enabled=False,
        # Issue #6343: same layer stack and search budgets as the parent.
        **worker_router_kwargs(config),
    )

    # Add pads from serialized data
    router._loaded_component_holes = config.get("component_holes")
    router.grid.install_component_hole_census(router._loaded_component_holes)

    for pad_data in config["pads_data"]:
        ref = pad_data["ref"]
        layer_data = pad_data["layer"]
        if isinstance(layer_data, int):
            pad_layer = Layer(layer_data)
        elif isinstance(layer_data, str):
            try:
                pad_layer = Layer.from_kicad_name(layer_data)
            except ValueError:
                pad_layer = Layer.F_CU
        elif isinstance(layer_data, Layer):
            pad_layer = layer_data
        else:
            pad_layer = Layer.F_CU

        from kicad_tools.router.primitives import Pad

        pin = str(pad_data["number"])
        pad = Pad(
            x=pad_data["x"],
            y=pad_data["y"],
            width=pad_data["width"],
            height=pad_data["height"],
            net=pad_data["net"],
            net_name=pad_data["net_name"],
            layer=pad_layer,
            ref=ref,
            component_id=pad_data.get("component_id", ref),
            terminal_id=pad_data.get("terminal_id", ""),
            pin=pin,
            through_hole=pad_data.get("through_hole", False),
            drill=pad_data.get("drill", 0.0),
            drill_size=pad_data.get("drill_size"),
            drill_rotation=pad_data.get("drill_rotation", 0.0),
            footprint_name=pad_data.get("footprint_name", ""),
            steiner_point=pad_data.get("steiner_point", False),
            # Issue #4910: ``pads_data`` is produced by ``Autorouter``'s
            # worker-config serializer, which carries the pad's residual
            # board rotation; dropping it here would silently rebuild every
            # rotated pad as axis-aligned in the worker process.
            rotation=pad_data.get("rotation", 0.0),
            shape=pad_data.get("shape", "rect"),
        )
        key = pad.key
        if key in router.pads and router.pads[key].net != pad.net:
            raise ValueError(f"Physical terminal {key!r} has conflicting nets")
        router.pads[key] = pad
        router.all_pads.append(pad)
        router.grid.add_pad(pad, pin_pitch=pad_data.get("pin_pitch"))

    # Restore nets and net_names
    router.nets = {
        int(k): list(dict.fromkeys((key[0], key[1]) for key in keys))
        for k, keys in config["nets"].items()
    }
    router.net_names = {int(k): v for k, v in config["net_names"].items()}

    # Restore pour-net overrides so _is_pour_net() returns correct results
    router._pour_nets_without_zones = set(config.get("pour_nets_without_zones", []))

    # Issue #5374: reinstall the board-edge keepout on the worker's own
    # grid.  ``_serialize_for_parallel`` carries it across the process
    # boundary via the "edge_segments" / "edge_clearance" config keys;
    # without reinstalling it here the worker's A* search can route
    # straight through cells the parent process would have blocked.
    edge_segments = config.get("edge_segments")
    edge_clearance = config.get("edge_clearance")
    if edge_segments is not None:
        router._edge_segments = edge_segments
        router._edge_clearance = edge_clearance
        if edge_segments and edge_clearance:
            router.grid.add_edge_keepout(edge_segments, edge_clearance)

    # Issue #5555: ``add_obstacle`` keepouts have the same problem -- they
    # only reach this worker through the "obstacles" key of the serialized
    # config, so replay them onto the worker's own grid before routing.
    install_serialized_obstacles(router, config)

    # Issue #6345: route in the chromosome's net order.  ``route_all(order)``
    # re-sorted it back to the priority order, so every chromosome routed the
    # same sequence and the whole population scored identically.
    routes = route_trial(
        router,
        net_order,
        use_negotiated=bool(config.get("use_negotiated", False)),
        negotiated_kwargs=config.get("negotiated_kwargs"),
        seed=seed,
        timeout=config.get("trial_timeout"),
        per_net_timeout=config.get("per_net_timeout"),
    )
    total_nets = len([n for n in router.nets if n != 0])
    score = _score_routes(routes, total_nets)

    return routes, score, chrom_idx


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


class EvolutionaryRoutingOptimizer:
    """Population-based evolutionary routing optimizer.

    Maintains a population of ``RoutingChromosome`` instances, each encoding
    net ordering plus per-net cost parameters.  Evolves the population over
    ``generations`` using tournament selection, OX crossover, and mutation,
    evaluating fitness via full ``route_all()`` calls.

    Parameters:
        pop_size: Population size.
        generations: Number of evolutionary generations.
        elitism: Number of top chromosomes to carry forward unchanged.
        crossover_rate: Probability of applying crossover.
        mutation_rate: Per-gene mutation probability.
        tournament_size: Tournament selection size.
    """

    def __init__(
        self,
        pop_size: int = 20,
        generations: int = 10,
        elitism: int = 2,
        crossover_rate: float = 0.8,
        mutation_rate: float = 0.15,
        tournament_size: int = 3,
    ):
        self.pop_size = pop_size
        self.generations = generations
        self.elitism = elitism
        self.crossover_rate = crossover_rate
        self.mutation_rate = mutation_rate
        self.tournament_size = tournament_size

    # ---- population initialisation ----

    def _init_population(
        self,
        base_order: list[int],
        get_priority: callable,
        seed: int,
    ) -> list[RoutingChromosome]:
        """Create initial population.

        The first chromosome uses the priority-sorted base order; the rest
        are random shuffles within priority tiers.
        """
        from ..algorithms.monte_carlo import MonteCarloRouter

        total_nets = len(base_order)
        mc = MonteCarloRouter(total_nets)

        population: list[RoutingChromosome] = []

        # Seed chromosome: deterministic baseline
        base_chrom = RoutingChromosome(net_order=list(base_order))
        for net in base_order:
            base_chrom.astar_weights[net] = 1.0
            base_chrom.preferred_layers[net] = 0
            base_chrom.strategy_flags[net] = False
        population.append(base_chrom)

        # Remaining: random shuffles with cross-tier promotions
        for i in range(1, self.pop_size):
            random.seed(seed + i)
            promotion_rate = min(0.1 + 0.05 * i, 0.5)
            order = mc.shuffle_with_promotions(
                base_order, get_priority, promotion_rate=promotion_rate
            )
            chrom = RoutingChromosome(net_order=order)
            for net in base_order:
                chrom.astar_weights[net] = max(0.5, min(3.0, random.gauss(1.0, 0.3)))
                chrom.preferred_layers[net] = random.choice([0, 1])
                chrom.strategy_flags[net] = False
            population.append(chrom)

        return population

    # ---- evolution step ----

    def _evolve(self, population: list[RoutingChromosome]) -> list[RoutingChromosome]:
        """Produce next generation via selection, crossover, mutation."""
        population.sort(key=lambda c: c.fitness, reverse=True)
        new_pop: list[RoutingChromosome] = []

        # Elitism
        for i in range(min(self.elitism, len(population))):
            new_pop.append(population[i].copy())

        # Fill remaining slots
        while len(new_pop) < len(population):
            p1 = tournament_select(population, self.tournament_size)
            p2 = tournament_select(population, self.tournament_size)

            if random.random() < self.crossover_rate:
                child = order_crossover(p1, p2)
            else:
                child = p1.copy()

            child = mutate(child, self.mutation_rate)
            new_pop.append(child)

        return new_pop


def run_evolutionary(
    autorouter,
    pop_size: int = 20,
    generations: int = 10,
    seed: int | None = None,
    verbose: bool = True,
    progress_callback: ProgressCallback | None = None,
    num_workers: int | None = None,
    timeout: float | None = None,
    use_negotiated: bool = False,
    negotiated_kwargs: dict | None = None,
    per_net_timeout: float | None = None,
) -> list[Route]:
    """Run evolutionary routing optimization on an Autorouter instance.

    Orchestrates a population of routing chromosomes over multiple
    generations, keeping the best result.  Supports both sequential and
    parallel evaluation.

    Args:
        autorouter: The Autorouter instance to run on.
        pop_size: Population size per generation.
        generations: Number of evolutionary generations.
        seed: Random seed for reproducibility.
        verbose: Whether to print progress.
        progress_callback: Optional callback for progress updates.
        num_workers: Number of parallel workers (None/0 = auto, 1 = sequential).
        timeout: Optional wall-clock budget in seconds.  If exceeded, no
            further chromosome is evaluated (issue #6343: checked between
            chromosomes too, and chromosomes still running in worker
            processes are abandoned) and the best result found so far is
            returned.  Default ``None`` means no wall-clock limit.  Each
            evaluation gets an equal share of what is left for the current
            generation (issue #6345).
        use_negotiated: Evaluate chromosomes with ``route_all_negotiated``
            rather than one ``route_all`` pass (issue #6345).
        negotiated_kwargs: Extra ``route_all_negotiated`` kwargs.
        per_net_timeout: Per-net wall-clock cap for every evaluation.

    Returns:
        List of routes from the best chromosome found.
    """
    clear_best_trial(autorouter)
    try:
        return _run_evolutionary(
            autorouter,
            pop_size=pop_size,
            generations=generations,
            seed=seed,
            verbose=verbose,
            progress_callback=progress_callback,
            num_workers=num_workers,
            timeout=timeout,
            use_negotiated=use_negotiated,
            negotiated_kwargs=negotiated_kwargs,
            per_net_timeout=per_net_timeout,
        )
    except Exception:
        clear_best_trial(autorouter)
        raise


def _run_evolutionary(
    autorouter,
    *,
    pop_size: int,
    generations: int,
    seed: int | None,
    verbose: bool,
    progress_callback: ProgressCallback | None,
    num_workers: int | None,
    timeout: float | None,
    use_negotiated: bool = False,
    negotiated_kwargs: dict | None = None,
    per_net_timeout: float | None = None,
) -> list[Route]:
    start_time = time.monotonic()
    deadline = deadline_from_timeout(timeout)

    base_seed = seed if seed is not None else random.randint(0, 2**31 - 1)
    random.seed(base_seed)

    # Determine worker count
    if num_workers is None or num_workers <= 0:
        num_workers = min(pop_size, os.cpu_count() or 4)
    num_workers = min(num_workers, pop_size)

    optimizer = EvolutionaryRoutingOptimizer(
        pop_size=pop_size,
        generations=generations,
    )

    if verbose:
        print("\n=== Evolutionary Routing Optimizer ===", flush=True)
        print(
            f"  Population: {pop_size}, Generations: {generations}, Negotiated: {use_negotiated}",
            flush=True,
        )
        if num_workers > 1:
            print(f"  Parallel workers: {num_workers}", flush=True)
        if timeout is not None:
            print(f"  Timeout: {timeout:.1f}s", flush=True)

    # Prepare base net order (same as Monte Carlo; issue #6345: honours a
    # user ``--order-method`` order)
    base_order = _initial_trial_order(autorouter)
    trial_kwargs = {
        "use_negotiated": use_negotiated,
        "negotiated_kwargs": dict(negotiated_kwargs or {}),
        "per_net_timeout": per_net_timeout,
    }

    total_nets = len(base_order)

    # Initialise population
    population = optimizer._init_population(base_order, autorouter._get_net_priority, base_seed)

    best_routes: list[Route] | None = None
    best_score = float("-inf")
    best_gen = -1

    total_evals = pop_size * generations

    # Issue #6343: publish each new best as soon as its chromosome finishes,
    # not after the whole generation -- on a large board one generation can
    # outlast the entire ``--timeout``.  The partial save restores it.
    best_seen = [float("-inf")]

    def _on_result(gen: int, idx: int, routes: list, score: float) -> None:
        if score > best_seen[0]:
            best_seen[0] = score
            publish_best_trial(
                autorouter,
                routes,
                strategy="evolutionary",
                trial=gen * pop_size + idx,
                score=score,
            )

    for gen in range(generations):
        # ----- wall-clock timeout check (Issue #2467) -----
        if timeout is not None and time.monotonic() - start_time >= timeout:
            if verbose:
                print(
                    f"  Timeout {timeout:.1f}s reached at gen {gen}; "
                    f"returning best (score={best_score:.2f})",
                    flush=True,
                )
            break

        # ----- evaluate population -----
        if num_workers > 1:
            routes_scores = _evaluate_parallel(
                autorouter,
                population,
                base_seed,
                gen,
                num_workers,
                deadline=deadline,
                on_result=_on_result,
                base_order=base_order,
                trial_kwargs=trial_kwargs,
            )
        else:
            routes_scores = _evaluate_sequential(
                autorouter,
                population,
                base_seed,
                gen,
                total_nets,
                deadline=deadline,
                on_result=_on_result,
                base_order=base_order,
                trial_kwargs=trial_kwargs,
            )

        # Issue #6343: an evaluation cut short by the deadline returns with
        # the unreached chromosomes scored ``-inf``.
        timed_out = deadline is not None and time.monotonic() >= deadline

        # Assign fitness and track best
        gen_best_score = float("-inf")
        gen_best_idx = 0
        for idx, (routes, score) in enumerate(routes_scores):
            population[idx].fitness = score
            if score > gen_best_score:
                gen_best_score = score
                gen_best_idx = idx

        if gen_best_score > best_score:
            best_score = gen_best_score
            best_routes = routes_scores[gen_best_idx][0]
            best_gen = gen

        if timed_out:
            if verbose:
                print(
                    f"  Timeout {timeout:.1f}s reached during gen {gen + 1}; "
                    f"returning best (score={best_score:.2f})",
                    flush=True,
                )
            break

        # Progress
        evals_done = (gen + 1) * pop_size
        if progress_callback is not None:
            if not progress_callback(
                evals_done / total_evals,
                f"Gen {gen + 1}/{generations} best={best_score:.2f}",
                True,
            ):
                break

        if verbose:
            avg_fitness = sum(c.fitness for c in population) / len(population)
            new_best = " NEW BEST" if gen == best_gen else ""
            print(
                f"  Gen {gen + 1}: best={gen_best_score:.2f} avg={avg_fitness:.2f}{new_best}",
                flush=True,
            )

        # Evolve (skip on last gen)
        if gen < generations - 1:
            population = optimizer._evolve(population)

    if verbose:
        print(f"\n  Best: Gen {best_gen + 1} (score={best_score:.2f})", flush=True)

    autorouter.routes = best_routes if best_routes else []
    # Issue #6343: the result is in ``autorouter.routes`` now; later stages own
    # it, so the partial save must not swap the published best back in.
    clear_best_trial(autorouter)
    if progress_callback is not None:
        routed = len({r.net for r in autorouter.routes}) if autorouter.routes else 0
        progress_callback(
            1.0,
            f"Best: gen {best_gen + 1}, {routed}/{total_nets} nets",
            False,
        )

    return autorouter.routes


# ---------------------------------------------------------------------------
# Evaluation helpers
# ---------------------------------------------------------------------------


_ResultCallback = Callable[[int, int, list, float], None]

# A chromosome not evaluated before the deadline: never selected as a best.
_UNEVALUATED: tuple[list, float] = ([], float("-inf"))


def _evaluate_sequential(
    autorouter,
    population: list[RoutingChromosome],
    base_seed: int,
    gen: int,
    total_nets: int,
    deadline: float | None = None,
    on_result: _ResultCallback | None = None,
    base_order: list[int] | None = None,
    trial_kwargs: dict | None = None,
) -> list[tuple[list, float]]:
    """Evaluate every chromosome sequentially.

    Chromosomes not reached before ``deadline`` score ``-inf`` (issue #6343).
    Each routes in its own ``net_order`` (issue #6345).
    """
    trial_kwargs = trial_kwargs or {}
    results: list[tuple[list, float]] = []
    for idx, chrom in enumerate(population):
        if deadline is not None and time.monotonic() >= deadline:
            results.extend([_UNEVALUATED] * (len(population) - idx))
            return results
        eval_seed = base_seed + gen * len(population) + idx
        random.seed(eval_seed)
        autorouter._reset_for_new_trial()
        order = (
            baseline_trial_order(autorouter, chrom.net_order, base_order)
            if base_order is not None
            else list(chrom.net_order)
        )
        routes = route_trial(
            autorouter,
            order,
            use_negotiated=bool(trial_kwargs.get("use_negotiated", False)),
            negotiated_kwargs=trial_kwargs.get("negotiated_kwargs"),
            seed=eval_seed,
            timeout=trial_timeout(deadline, len(population) - idx),
            per_net_timeout=trial_kwargs.get("per_net_timeout"),
        )
        score = _score_routes(routes, total_nets)
        results.append((routes, score))
        if on_result is not None:
            on_result(gen, idx, routes, score)
    return results


def _evaluate_parallel(
    autorouter,
    population: list[RoutingChromosome],
    base_seed: int,
    gen: int,
    num_workers: int,
    deadline: float | None = None,
    on_result: _ResultCallback | None = None,
    base_order: list[int] | None = None,
    trial_kwargs: dict | None = None,
) -> list[tuple[list, float]]:
    """Evaluate every chromosome in parallel using ProcessPoolExecutor.

    Issue #6343: waiting is bounded by ``deadline``; chromosomes still running
    then are abandoned (their workers terminated) and score ``-inf``.
    """
    base_config = autorouter._serialize_for_parallel()
    trial_kwargs = trial_kwargs or {}
    # Issue #6345: equal share of the time left, over this generation's waves.
    per_trial_timeout = trial_timeout(deadline, len(population), num_workers)

    trial_configs = []
    for idx, chrom in enumerate(population):
        config = base_config.copy()
        config.update(
            {
                "chrom_idx": idx,
                "seed": base_seed + gen * len(population) + idx,
                "net_order": (
                    baseline_trial_order(autorouter, chrom.net_order, base_order)
                    if base_order is not None
                    else list(chrom.net_order)
                ),
                "use_negotiated": bool(trial_kwargs.get("use_negotiated", False)),
                "negotiated_kwargs": dict(trial_kwargs.get("negotiated_kwargs") or {}),
                "trial_timeout": per_trial_timeout,
                "per_net_timeout": trial_kwargs.get("per_net_timeout"),
            }
        )
        trial_configs.append(config)

    # Pre-fill results list
    results: list[tuple[list, float] | None] = [None] * len(population)
    timed_out = False

    executor = ProcessPoolExecutor(max_workers=num_workers)
    try:
        futures = {
            executor.submit(_run_evolutionary_trial, cfg): cfg["chrom_idx"] for cfg in trial_configs
        }
        try:
            for future in as_completed(futures, timeout=remaining_seconds(deadline)):
                chrom_idx = futures[future]
                try:
                    routes, score, _ = future.result()
                    results[chrom_idx] = (routes, score)
                except Exception:
                    results[chrom_idx] = ([], 0.0)
                    continue
                if on_result is not None:
                    on_result(gen, chrom_idx, routes, score)
        except concurrent.futures.TimeoutError:
            timed_out = True
            terminate_executor(executor)
    except BaseException:
        # Deadline / Ctrl+C: do not join workers that are mid-trial.
        terminate_executor(executor)
        raise
    if not timed_out:
        executor.shutdown(wait=True)

    if timed_out:
        return [(r or _UNEVALUATED) for r in results]
    # Replace any remaining Nones (should not happen)
    return [(r or ([], 0.0)) for r in results]  # type: ignore[misc]
