"""Unit tests for the evolutionary routing optimizer.

Tests cover chromosome dataclass, genetic operators (OX crossover, swap
mutation, Gaussian weight perturbation, layer/strategy flip), tournament
selection, fitness scoring, and the end-to-end optimiser loop.
"""

from __future__ import annotations

import random

import pytest

from kicad_tools.router.algorithms.evolutionary import (
    EvolutionaryRoutingOptimizer,
    RoutingChromosome,
    _score_routes,
    mutate,
    order_crossover,
    tournament_select,
)


# ---------------------------------------------------------------------------
# Chromosome basics
# ---------------------------------------------------------------------------


class TestRoutingChromosome:
    def test_default_init(self):
        c = RoutingChromosome()
        assert c.net_order == []
        assert c.astar_weights == {}
        assert c.fitness == float("-inf")

    def test_copy_is_independent(self):
        c = RoutingChromosome(
            net_order=[1, 2, 3],
            astar_weights={1: 1.0, 2: 1.5},
            preferred_layers={1: 0, 2: 1},
            strategy_flags={1: True},
            fitness=42.0,
        )
        c2 = c.copy()
        assert c2.net_order == c.net_order
        assert c2.fitness == 42.0

        # Mutating the copy must not affect the original
        c2.net_order.append(99)
        c2.astar_weights[1] = 999.0
        assert 99 not in c.net_order
        assert c.astar_weights[1] == 1.0


# ---------------------------------------------------------------------------
# Order crossover
# ---------------------------------------------------------------------------


class TestOrderCrossover:
    def test_ox_preserves_permutation(self):
        """OX must produce a valid permutation containing every net exactly once."""
        random.seed(42)
        nets = list(range(1, 11))
        p1 = RoutingChromosome(
            net_order=list(nets),
            astar_weights={n: 1.0 for n in nets},
            preferred_layers={n: 0 for n in nets},
        )
        random.shuffle(nets)
        p2 = RoutingChromosome(
            net_order=list(nets),
            astar_weights={n: 1.5 for n in nets},
            preferred_layers={n: 1 for n in nets},
        )

        for _ in range(50):
            child = order_crossover(p1, p2)
            assert sorted(child.net_order) == list(range(1, 11))

    def test_ox_single_element(self):
        p1 = RoutingChromosome(net_order=[5])
        p2 = RoutingChromosome(net_order=[5])
        child = order_crossover(p1, p2)
        assert child.net_order == [5]

    def test_ox_inherits_continuous_genes(self):
        """Continuous and discrete genes should be present in the child."""
        random.seed(0)
        nets = [1, 2, 3]
        p1 = RoutingChromosome(
            net_order=nets,
            astar_weights={1: 0.8, 2: 1.0, 3: 1.2},
            preferred_layers={1: 0, 2: 0, 3: 0},
            strategy_flags={1: False, 2: True, 3: False},
        )
        p2 = RoutingChromosome(
            net_order=[3, 1, 2],
            astar_weights={1: 1.5, 2: 2.0, 3: 0.5},
            preferred_layers={1: 1, 2: 1, 3: 1},
            strategy_flags={1: True, 2: False, 3: True},
        )
        child = order_crossover(p1, p2)
        # Every net should have a weight, layer, and flag
        for net in nets:
            assert net in child.astar_weights
            assert net in child.preferred_layers
            assert net in child.strategy_flags


# ---------------------------------------------------------------------------
# Mutation
# ---------------------------------------------------------------------------


class TestMutation:
    def test_swap_mutation_preserves_permutation(self):
        """Swap mutation must not break the permutation property."""
        random.seed(7)
        nets = list(range(1, 21))
        c = RoutingChromosome(
            net_order=list(nets),
            astar_weights={n: 1.0 for n in nets},
            preferred_layers={n: 0 for n in nets},
            strategy_flags={n: False for n in nets},
        )
        for _ in range(100):
            mutate(c, mutation_rate=0.5)
            assert sorted(c.net_order) == nets

    def test_gaussian_weight_clamped(self):
        """A* weights must stay within [0.5, 3.0] after mutation."""
        random.seed(99)
        nets = list(range(1, 6))
        c = RoutingChromosome(
            net_order=nets,
            astar_weights={n: 1.0 for n in nets},
        )
        for _ in range(200):
            mutate(c, mutation_rate=1.0)
        for w in c.astar_weights.values():
            assert 0.5 <= w <= 3.0

    def test_mutation_rate_zero_no_change(self):
        """With mutation_rate=0 no changes should occur."""
        random.seed(0)
        c = RoutingChromosome(
            net_order=[1, 2, 3],
            astar_weights={1: 1.0, 2: 1.0, 3: 1.0},
            preferred_layers={1: 0, 2: 0, 3: 0},
            strategy_flags={1: False, 2: False, 3: False},
        )
        original = c.copy()
        mutate(c, mutation_rate=0.0)
        assert c.net_order == original.net_order
        assert c.astar_weights == original.astar_weights
        assert c.preferred_layers == original.preferred_layers
        assert c.strategy_flags == original.strategy_flags


# ---------------------------------------------------------------------------
# Tournament selection
# ---------------------------------------------------------------------------


class TestTournamentSelection:
    def test_selects_best_in_deterministic_case(self):
        """If tournament_size >= population, always returns the best."""
        pop = [
            RoutingChromosome(fitness=10.0),
            RoutingChromosome(fitness=50.0),
            RoutingChromosome(fitness=30.0),
        ]
        winner = tournament_select(pop, tournament_size=3)
        assert winner.fitness == 50.0

    def test_tournament_never_returns_none(self):
        pop = [RoutingChromosome(fitness=float("-inf"))]
        winner = tournament_select(pop, tournament_size=1)
        assert winner is not None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoreRoutes:
    def test_empty_routes(self):
        assert _score_routes([], 5) == 0.0

    def test_zero_nets(self):
        assert _score_routes([], 0) == 0.0

    def test_drc_bonus_at_full_completion(self):
        """100 % completion should include the DRC bonus."""

        class _FakeSegment:
            def __init__(self):
                self.x1 = self.y1 = 0.0
                self.x2 = self.y2 = 1.0

        class _FakeRoute:
            def __init__(self, net):
                self.net = net
                self.vias = []
                self.segments = [_FakeSegment()]

        routes = [_FakeRoute(1), _FakeRoute(2)]
        score_full = _score_routes(routes, total_nets=2)
        score_partial = _score_routes([_FakeRoute(1)], total_nets=2)

        # Full completion should be significantly better than half
        assert score_full > score_partial
        # DRC bonus adds 50
        assert score_full > 1000


# ---------------------------------------------------------------------------
# EvolutionaryRoutingOptimizer unit tests
# ---------------------------------------------------------------------------


class TestEvolutionaryRoutingOptimizer:
    def test_init_defaults(self):
        opt = EvolutionaryRoutingOptimizer()
        assert opt.pop_size == 20
        assert opt.generations == 10
        assert opt.elitism == 2

    def test_custom_params(self):
        opt = EvolutionaryRoutingOptimizer(
            pop_size=10, generations=5, elitism=1,
            crossover_rate=0.9, mutation_rate=0.2, tournament_size=4,
        )
        assert opt.pop_size == 10
        assert opt.tournament_size == 4

    def test_evolve_preserves_population_size(self):
        """_evolve must return the same number of individuals."""
        random.seed(123)
        opt = EvolutionaryRoutingOptimizer(pop_size=8, elitism=2)
        nets = list(range(1, 6))
        pop = []
        for i in range(8):
            c = RoutingChromosome(
                net_order=list(nets),
                astar_weights={n: 1.0 for n in nets},
                preferred_layers={n: 0 for n in nets},
                strategy_flags={n: False for n in nets},
                fitness=float(i),
            )
            pop.append(c)

        new_pop = opt._evolve(pop)
        assert len(new_pop) == 8

    def test_evolve_elitism_preserves_best(self):
        """The best individuals should be carried forward by elitism."""
        random.seed(456)
        opt = EvolutionaryRoutingOptimizer(pop_size=5, elitism=2)
        nets = [1, 2, 3]
        pop = []
        for i in range(5):
            c = RoutingChromosome(
                net_order=list(nets),
                astar_weights={n: 1.0 for n in nets},
                preferred_layers={n: 0 for n in nets},
                strategy_flags={n: False for n in nets},
                fitness=float(i * 10),
            )
            pop.append(c)

        new_pop = opt._evolve(pop)
        fitnesses = sorted([c.fitness for c in new_pop], reverse=True)
        # Top 2 from original (40.0 and 30.0) should still be present
        assert 40.0 in fitnesses
        assert 30.0 in fitnesses


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_generations(self):
        """EvolutionaryRoutingOptimizer with 0 generations should
        still be constructible (the run_evolutionary function
        handles the 0-generation case by returning the best of
        the initial population evaluation)."""
        opt = EvolutionaryRoutingOptimizer(pop_size=3, generations=0)
        assert opt.generations == 0
