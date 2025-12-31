"""Tests for the evolutionary placement optimizer."""

import pytest

from kicad_tools.optim import (
    Component,
    Pin,
    PlacementOptimizer,
    Polygon,
    Spring,
)
from kicad_tools.optim.evolutionary import (
    EvolutionaryConfig,
    EvolutionaryPlacementOptimizer,
    Individual,
)


class TestEvolutionaryConfig:
    """Tests for EvolutionaryConfig dataclass."""

    def test_default_values(self):
        config = EvolutionaryConfig()
        assert config.population_size == 50
        assert config.generations == 100
        assert config.elitism == 5
        assert config.crossover_rate == 0.8
        assert config.mutation_rate == 0.1
        assert config.tournament_size == 3
        assert config.position_mutation_sigma == 1.0
        assert config.rotation_mutation_prob == 0.05
        assert config.grid_snap == 0.127
        assert config.rotation_snap == 90.0
        assert config.parallel is True

    def test_custom_values(self):
        config = EvolutionaryConfig(
            population_size=100,
            generations=200,
            elitism=10,
            crossover_rate=0.9,
            mutation_rate=0.2,
        )
        assert config.population_size == 100
        assert config.generations == 200
        assert config.elitism == 10
        assert config.crossover_rate == 0.9
        assert config.mutation_rate == 0.2


class TestIndividual:
    """Tests for Individual dataclass."""

    def test_default_values(self):
        ind = Individual()
        assert ind.positions == {}
        assert ind.rotations == {}
        assert ind.fitness == 0.0

    def test_initialization(self):
        ind = Individual(
            positions={"U1": (10.0, 20.0), "R1": (30.0, 40.0)},
            rotations={"U1": 0.0, "R1": 90.0},
            fitness=100.0,
        )
        assert ind.positions["U1"] == (10.0, 20.0)
        assert ind.rotations["R1"] == 90.0
        assert ind.fitness == 100.0

    def test_copy(self):
        ind = Individual(
            positions={"U1": (10.0, 20.0)},
            rotations={"U1": 90.0},
            fitness=50.0,
        )
        copied = ind.copy()
        assert copied.positions == ind.positions
        assert copied.rotations == ind.rotations
        assert copied.fitness == ind.fitness
        # Verify it's a deep copy
        copied.positions["U1"] = (100.0, 200.0)
        assert ind.positions["U1"] == (10.0, 20.0)


class TestEvolutionaryPlacementOptimizer:
    """Tests for EvolutionaryPlacementOptimizer."""

    @pytest.fixture
    def simple_optimizer(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        return EvolutionaryPlacementOptimizer(board)

    @pytest.fixture
    def optimizer_with_components(self, simple_optimizer):
        comp1 = Component(
            ref="U1",
            x=30.0,
            y=40.0,
            width=10.0,
            height=8.0,
            pins=[
                Pin(number="1", x=25.0, y=40.0, net=1, net_name="NET1"),
                Pin(number="2", x=35.0, y=40.0, net=2, net_name="GND"),
            ],
        )
        comp2 = Component(
            ref="R1",
            x=70.0,
            y=60.0,
            width=4.0,
            height=2.0,
            pins=[
                Pin(number="1", x=68.0, y=60.0, net=1, net_name="NET1"),
                Pin(number="2", x=72.0, y=60.0, net=2, net_name="GND"),
            ],
        )
        simple_optimizer.add_component(comp1)
        simple_optimizer.add_component(comp2)

        # Create springs for the nets
        spring1 = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="1",
            stiffness=1.0,
            net=1,
            net_name="NET1",
        )
        spring2 = Spring(
            comp1_ref="U1",
            pin1_num="2",
            comp2_ref="R1",
            pin2_num="2",
            stiffness=1.0,
            net=2,
            net_name="GND",
        )
        simple_optimizer.add_spring(spring1)
        simple_optimizer.add_spring(spring2)

        return simple_optimizer

    def test_initialization(self, simple_optimizer):
        assert len(simple_optimizer.components) == 0
        assert len(simple_optimizer.springs) == 0
        assert simple_optimizer.config is not None

    def test_initialization_with_config(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(population_size=30)
        opt = EvolutionaryPlacementOptimizer(board, config)
        assert opt.config.population_size == 30

    def test_add_component(self, simple_optimizer):
        comp = Component(ref="U1", x=50.0, y=50.0)
        simple_optimizer.add_component(comp)
        assert len(simple_optimizer.components) == 1
        assert simple_optimizer._component_map["U1"] == comp

    def test_add_spring(self, simple_optimizer):
        spring = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="1",
        )
        simple_optimizer.add_spring(spring)
        assert len(simple_optimizer.springs) == 1


class TestEvolutionaryOperators:
    """Tests for genetic operators."""

    @pytest.fixture
    def optimizer_with_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            mutation_rate=1.0,  # Always mutate for testing
            position_mutation_sigma=5.0,
            rotation_mutation_prob=1.0,  # Always rotate for testing
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        comp1 = Component(ref="U1", x=30.0, y=40.0, width=5.0, height=5.0)
        comp2 = Component(ref="R1", x=70.0, y=60.0, width=3.0, height=2.0)
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)

        return optimizer

    def test_individual_from_current(self, optimizer_with_components):
        ind = optimizer_with_components._individual_from_current()
        assert "U1" in ind.positions
        assert "R1" in ind.positions
        assert ind.positions["U1"] == (30.0, 40.0)
        assert ind.positions["R1"] == (70.0, 60.0)

    def test_apply_individual(self, optimizer_with_components):
        ind = Individual(
            positions={"U1": (50.0, 50.0), "R1": (60.0, 60.0)},
            rotations={"U1": 90.0, "R1": 180.0},
        )
        optimizer_with_components._apply_individual(ind)
        assert optimizer_with_components.components[0].x == 50.0
        assert optimizer_with_components.components[0].y == 50.0
        assert optimizer_with_components.components[0].rotation == 90.0

    def test_initialize_population(self, optimizer_with_components):
        population = optimizer_with_components._initialize_population(10)
        assert len(population) == 10
        # First individual should be current placement
        assert population[0].positions["U1"] == (30.0, 40.0)
        # Other individuals should have random positions
        # (at least some should differ from original)
        different_count = sum(1 for ind in population[1:] if ind.positions["U1"] != (30.0, 40.0))
        assert different_count > 0

    def test_crossover(self, optimizer_with_components):
        parent1 = Individual(
            positions={"U1": (10.0, 10.0), "R1": (90.0, 90.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        parent2 = Individual(
            positions={"U1": (20.0, 20.0), "R1": (80.0, 80.0)},
            rotations={"U1": 90.0, "R1": 90.0},
        )
        child = optimizer_with_components._crossover(parent1, parent2)
        # Child should have positions from one or the other parent
        assert "U1" in child.positions
        assert "R1" in child.positions

    def test_mutate(self, optimizer_with_components):
        ind = Individual(
            positions={"U1": (50.0, 50.0), "R1": (50.0, 50.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        original_positions = dict(ind.positions)
        original_rotations = dict(ind.rotations)

        mutated = optimizer_with_components._mutate(ind)

        # With 100% mutation rate, positions should change
        positions_changed = any(
            mutated.positions[ref] != original_positions[ref] for ref in mutated.positions
        )
        rotations_changed = any(
            mutated.rotations[ref] != original_rotations[ref] for ref in mutated.rotations
        )
        assert positions_changed or rotations_changed

    def test_tournament_select(self, optimizer_with_components):
        population = [
            Individual(positions={"U1": (10.0, 10.0)}, rotations={"U1": 0.0}, fitness=10.0),
            Individual(positions={"U1": (20.0, 20.0)}, rotations={"U1": 0.0}, fitness=50.0),
            Individual(positions={"U1": (30.0, 30.0)}, rotations={"U1": 0.0}, fitness=30.0),
        ]
        # Tournament selection should prefer higher fitness
        selections = [optimizer_with_components._tournament_select(population) for _ in range(20)]
        # The individual with fitness 50 should be selected more often
        high_fitness_count = sum(1 for s in selections if s.fitness == 50.0)
        assert high_fitness_count > 5  # Should be selected fairly often


class TestFitnessEvaluation:
    """Tests for fitness evaluation."""

    @pytest.fixture
    def optimizer_with_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        optimizer = EvolutionaryPlacementOptimizer(board)

        comp1 = Component(
            ref="U1",
            x=30.0,
            y=40.0,
            width=5.0,
            height=5.0,
            pins=[Pin(number="1", x=30.0, y=40.0, net=1, net_name="NET1")],
        )
        comp2 = Component(
            ref="R1",
            x=70.0,
            y=60.0,
            width=3.0,
            height=2.0,
            pins=[Pin(number="1", x=70.0, y=60.0, net=1, net_name="NET1")],
        )
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)

        spring = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="1",
            stiffness=1.0,
            net=1,
            net_name="NET1",
        )
        optimizer.add_spring(spring)

        return optimizer

    def test_total_wire_length(self, optimizer_with_components):
        length = optimizer_with_components._total_wire_length()
        # Distance between (30, 40) and (70, 60) = sqrt(40^2 + 20^2) = sqrt(2000) ≈ 44.7
        assert 40 < length < 50

    def test_count_conflicts_no_overlap(self, optimizer_with_components):
        conflicts = optimizer_with_components._count_conflicts()
        assert conflicts == 0

    def test_count_conflicts_with_overlap(self, optimizer_with_components):
        # Move components to overlap
        optimizer_with_components.components[1].x = 32.0
        optimizer_with_components.components[1].y = 42.0
        conflicts = optimizer_with_components._count_conflicts()
        assert conflicts == 1

    def test_count_boundary_violations_inside(self, optimizer_with_components):
        violations = optimizer_with_components._count_boundary_violations()
        assert violations == 0

    def test_count_boundary_violations_outside(self, optimizer_with_components):
        # Move component outside board
        optimizer_with_components.components[0].x = -100.0
        violations = optimizer_with_components._count_boundary_violations()
        assert violations == 1

    def test_evaluate_fitness(self, optimizer_with_components):
        ind = optimizer_with_components._individual_from_current()
        fitness = optimizer_with_components._evaluate_fitness(ind)
        # Fitness should be a reasonable number
        assert fitness != 0.0

    def test_wire_length_affects_fitness(self, optimizer_with_components):
        # Test that shorter wire length leads to higher fitness
        # when routability effect is controlled (same spacing pattern)
        # We use a config that emphasizes wire length over routability
        optimizer_with_components.config.wire_length_weight = 10.0  # Much higher
        optimizer_with_components.config.routability_weight = 0.0  # Disable routability

        # Closer placement = higher fitness
        ind_close = Individual(
            positions={"U1": (45.0, 50.0), "R1": (55.0, 50.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )
        # Further apart = lower fitness (more wire length penalty)
        ind_far = Individual(
            positions={"U1": (25.0, 50.0), "R1": (75.0, 50.0)},
            rotations={"U1": 0.0, "R1": 0.0},
        )

        fitness_close = optimizer_with_components._evaluate_fitness(ind_close)
        fitness_far = optimizer_with_components._evaluate_fitness(ind_far)

        assert fitness_close > fitness_far


class TestEvolution:
    """Tests for evolution process."""

    @pytest.fixture
    def optimizer_with_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            population_size=10,
            elitism=2,
            crossover_rate=0.8,
            mutation_rate=0.1,
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        comp1 = Component(
            ref="U1",
            x=30.0,
            y=40.0,
            width=5.0,
            height=5.0,
            pins=[Pin(number="1", x=30.0, y=40.0, net=1, net_name="NET1")],
        )
        comp2 = Component(
            ref="R1",
            x=70.0,
            y=60.0,
            width=3.0,
            height=2.0,
            pins=[Pin(number="1", x=70.0, y=60.0, net=1, net_name="NET1")],
        )
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)

        spring = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="1",
            stiffness=1.0,
            net=1,
            net_name="NET1",
        )
        optimizer.add_spring(spring)

        return optimizer

    def test_evolve_preserves_elitism(self, optimizer_with_components):
        population = optimizer_with_components._initialize_population(10)
        optimizer_with_components._evaluate_population(population)
        population.sort(key=lambda ind: ind.fitness, reverse=True)

        # Store the elite individuals' positions (elitism=2)
        elite1_positions = dict(population[0].positions)
        elite2_positions = dict(population[1].positions)

        new_population = optimizer_with_components._evolve(population)

        # The elite individuals should be preserved in the new population
        # (by position, since they were copied)
        elite_positions_found = [
            ind.positions for ind in new_population if ind.positions == elite1_positions
        ]
        assert len(elite_positions_found) >= 1, "First elite individual not preserved"

        elite2_positions_found = [
            ind.positions for ind in new_population if ind.positions == elite2_positions
        ]
        assert len(elite2_positions_found) >= 1, "Second elite individual not preserved"

    def test_evolve_maintains_population_size(self, optimizer_with_components):
        population = optimizer_with_components._initialize_population(10)
        optimizer_with_components._evaluate_population(population)

        new_population = optimizer_with_components._evolve(population)
        assert len(new_population) == len(population)


class TestOptimization:
    """Integration tests for full optimization runs."""

    @pytest.fixture
    def optimizer_with_components(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            population_size=20,
            generations=10,
            elitism=2,
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        # Components placed far apart
        comp1 = Component(
            ref="U1",
            x=10.0,
            y=20.0,
            width=5.0,
            height=5.0,
            pins=[Pin(number="1", x=10.0, y=20.0, net=1, net_name="NET1")],
        )
        comp2 = Component(
            ref="R1",
            x=90.0,
            y=80.0,
            width=3.0,
            height=2.0,
            pins=[Pin(number="1", x=90.0, y=80.0, net=1, net_name="NET1")],
        )
        optimizer.add_component(comp1)
        optimizer.add_component(comp2)

        spring = Spring(
            comp1_ref="U1",
            pin1_num="1",
            comp2_ref="R1",
            pin2_num="1",
            stiffness=1.0,
            net=1,
            net_name="NET1",
        )
        optimizer.add_spring(spring)

        return optimizer

    def test_optimize_returns_individual(self, optimizer_with_components):
        best = optimizer_with_components.optimize(generations=5, population_size=10)
        assert isinstance(best, Individual)
        assert "U1" in best.positions
        assert "R1" in best.positions
        assert best.fitness != 0.0

    def test_optimize_with_callback(self, optimizer_with_components):
        generations_seen = []

        def callback(gen, best):
            generations_seen.append(gen)

        optimizer_with_components.optimize(generations=5, population_size=10, callback=callback)
        assert len(generations_seen) == 5
        assert generations_seen == [0, 1, 2, 3, 4]

    def test_optimize_improves_fitness(self, optimizer_with_components):
        initial_ind = optimizer_with_components._individual_from_current()
        initial_fitness = optimizer_with_components._evaluate_fitness(initial_ind)

        best = optimizer_with_components.optimize(generations=20, population_size=30)

        # Fitness should improve (or at least not get worse)
        assert best.fitness >= initial_fitness

    def test_optimize_hybrid_returns_physics_optimizer(self, optimizer_with_components):
        physics_opt = optimizer_with_components.optimize_hybrid(
            evolutionary_generations=5,
            population_size=10,
            physics_iterations=50,
        )
        assert isinstance(physics_opt, PlacementOptimizer)
        assert len(physics_opt.components) == 2

    def test_report(self, optimizer_with_components):
        report = optimizer_with_components.report()
        assert "Evolutionary Placement Optimizer Report" in report
        assert "U1" in report
        assert "R1" in report
        assert "Components:" in report
        assert "wire length" in report.lower()


class TestConvergence:
    """Tests for convergence detection."""

    def test_check_convergence_early(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            convergence_generations=5,
            convergence_threshold=0.001,
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        # Not enough history
        optimizer._fitness_history = [100.0, 101.0, 102.0]
        assert optimizer._check_convergence() is False

    def test_check_convergence_improving(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            convergence_generations=5,
            convergence_threshold=0.001,
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        # Still improving
        optimizer._fitness_history = [100.0, 110.0, 120.0, 130.0, 140.0]
        assert optimizer._check_convergence() is False

    def test_check_convergence_converged(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        config = EvolutionaryConfig(
            convergence_generations=5,
            convergence_threshold=0.01,  # 1% threshold
        )
        optimizer = EvolutionaryPlacementOptimizer(board, config)

        # Converged (less than 1% improvement over 5 generations)
        optimizer._fitness_history = [100.0, 100.1, 100.2, 100.3, 100.4]
        assert optimizer._check_convergence() is True


class TestFromPlacementOptimizer:
    """Tests for creating evolutionary optimizer from physics optimizer."""

    def test_from_placement_optimizer(self):
        board = Polygon.rectangle(50, 50, 100, 80)
        physics_opt = PlacementOptimizer(board)

        comp = Component(ref="U1", x=50.0, y=50.0, width=5.0, height=5.0)
        physics_opt.add_component(comp)
        physics_opt.create_springs_from_nets()

        evo_opt = EvolutionaryPlacementOptimizer.from_placement_optimizer(physics_opt)

        assert len(evo_opt.components) == 1
        assert evo_opt.components[0].ref == "U1"
        assert evo_opt.board_outline == physics_opt.board_outline
