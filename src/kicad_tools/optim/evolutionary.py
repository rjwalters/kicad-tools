"""
Evolutionary (genetic algorithm) placement optimizer.

Provides global optimization for component placement using:
- Population-based search with tournament selection
- Spatial partitioning crossover
- Gaussian mutation for positions and rotation perturbation
- Multi-objective fitness function (wire length, conflicts, routability)

Example::

    from kicad_tools.optim.evolutionary import EvolutionaryPlacementOptimizer
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    optimizer = EvolutionaryPlacementOptimizer.from_pcb(pcb)

    # Run evolutionary optimization
    best = optimizer.optimize(generations=100, population_size=50)

    # Or use hybrid mode: evolutionary + physics refinement
    physics_optimizer = optimizer.optimize_hybrid(generations=50)
    physics_optimizer.write_to_pcb(pcb)
    pcb.save("optimized.kicad_pcb")
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from kicad_tools.optim.components import Component, FunctionalCluster, Spring
from kicad_tools.optim.config import PlacementConfig
from kicad_tools.optim.geometry import Polygon, Vector2D
from kicad_tools.optim.placement import PlacementOptimizer

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "EvolutionaryPlacementOptimizer",
    "EvolutionaryConfig",
    "Individual",
]


@dataclass
class EvolutionaryConfig:
    """Configuration for the evolutionary placement optimizer."""

    # Population parameters
    population_size: int = 50
    generations: int = 100
    elitism: int = 5  # Keep top N individuals unchanged each generation

    # Genetic operator rates
    crossover_rate: float = 0.8
    mutation_rate: float = 0.1
    tournament_size: int = 3

    # Mutation parameters
    position_mutation_sigma: float = 1.0  # Standard deviation in mm for position mutations
    rotation_mutation_prob: float = 0.05  # Probability of rotating a component 90 degrees

    # Fitness weights (higher = more important)
    wire_length_weight: float = 0.1
    conflict_weight: float = 100.0
    routability_weight: float = 50.0
    boundary_violation_weight: float = 500.0  # Heavy penalty for components outside board

    # Grid snapping
    grid_snap: float = 0.127  # 5 mil grid (0 to disable)
    rotation_snap: float = 90.0  # Rotation grid in degrees

    # Convergence detection
    convergence_generations: int = 20  # Check convergence over this many generations
    convergence_threshold: float = 0.001  # Relative improvement threshold

    # Parallel processing
    parallel: bool = True
    max_workers: int | None = None  # None = use all available cores


@dataclass
class Individual:
    """
    A placement configuration (chromosome) in the genetic algorithm.

    Stores component positions and rotations as the genotype.
    Fitness is computed based on wire length, conflicts, and routability.
    """

    positions: dict[str, tuple[float, float]] = field(default_factory=dict)  # ref -> (x, y)
    rotations: dict[str, float] = field(default_factory=dict)  # ref -> rotation (degrees)
    fitness: float = 0.0

    def copy(self) -> Individual:
        """Create a deep copy of this individual."""
        return Individual(
            positions=dict(self.positions),
            rotations=dict(self.rotations),
            fitness=self.fitness,
        )


class EvolutionaryPlacementOptimizer:
    """
    Genetic algorithm for component placement optimization.

    Uses population-based search to explore the placement space globally,
    avoiding local minima that physics-based simulation might get stuck in.

    Key features:
    - Tournament selection for parent selection
    - Spatial partitioning crossover to preserve locality
    - Gaussian mutation for position exploration
    - Multi-objective fitness function
    - Optional parallel fitness evaluation
    """

    def __init__(
        self,
        board_outline: Polygon,
        config: EvolutionaryConfig | None = None,
    ):
        """
        Initialize the evolutionary optimizer.

        Args:
            board_outline: Polygon defining the board boundary
            config: Optimization parameters
        """
        self.board_outline = board_outline
        self.config = config or EvolutionaryConfig()
        self.components: list[Component] = []
        self.springs: list[Spring] = []
        self.clusters: list[FunctionalCluster] = []
        self._component_map: dict[str, Component] = {}
        self._board_bounds = self._compute_board_bounds()
        self._fitness_history: list[float] = []
        self._cluster_members: set[str] = set()  # Components that are in clusters

    def _compute_board_bounds(self) -> tuple[float, float, float, float]:
        """Compute bounding box of board outline (min_x, min_y, max_x, max_y)."""
        if not self.board_outline.vertices:
            return (0.0, 0.0, 100.0, 100.0)
        xs = [v.x for v in self.board_outline.vertices]
        ys = [v.y for v in self.board_outline.vertices]
        return (min(xs), min(ys), max(xs), max(ys))

    @classmethod
    def from_pcb(
        cls,
        pcb: PCB,
        config: EvolutionaryConfig | None = None,
        fixed_refs: list[str] | None = None,
        enable_clustering: bool = False,
    ) -> EvolutionaryPlacementOptimizer:
        """
        Create optimizer from a loaded PCB.

        Delegates to PlacementOptimizer.from_pcb() to reuse component
        and spring extraction logic, then copies the data.

        Args:
            pcb: Loaded PCB object
            config: Optimization parameters
            fixed_refs: List of reference designators for fixed components
            enable_clustering: If True, detect and preserve functional clusters
        """
        # Use PlacementOptimizer to extract components and nets
        physics_opt = PlacementOptimizer.from_pcb(
            pcb, fixed_refs=fixed_refs, enable_clustering=enable_clustering
        )

        optimizer = cls(physics_opt.board_outline, config)
        optimizer.components = physics_opt.components
        optimizer.springs = physics_opt.springs
        optimizer.clusters = physics_opt.clusters
        optimizer._component_map = physics_opt._component_map
        optimizer._update_cluster_members()

        return optimizer

    @classmethod
    def from_placement_optimizer(
        cls,
        physics_optimizer: PlacementOptimizer,
        config: EvolutionaryConfig | None = None,
    ) -> EvolutionaryPlacementOptimizer:
        """
        Create evolutionary optimizer from an existing PlacementOptimizer.

        Useful for hybrid optimization workflows.
        """
        optimizer = cls(physics_optimizer.board_outline, config)
        optimizer.components = physics_optimizer.components
        optimizer.springs = physics_optimizer.springs
        optimizer.clusters = physics_optimizer.clusters
        optimizer._component_map = physics_optimizer._component_map
        optimizer._update_cluster_members()

        return optimizer

    def _update_cluster_members(self):
        """Build set of all component refs that are part of clusters."""
        self._cluster_members = set()
        for cluster in self.clusters:
            self._cluster_members.add(cluster.anchor)
            self._cluster_members.update(cluster.members)

    def add_component(self, comp: Component):
        """Add a component to the optimizer."""
        self.components.append(comp)
        self._component_map[comp.ref] = comp

    def add_spring(self, spring: Spring):
        """Add a spring (net connection) to the optimizer."""
        self.springs.append(spring)

    def _get_movable_components(self) -> list[Component]:
        """Get list of components that can be moved (not fixed)."""
        return [c for c in self.components if not c.fixed]

    def _individual_from_current(self) -> Individual:
        """Create an individual from current component positions."""
        ind = Individual()
        for comp in self._get_movable_components():
            ind.positions[comp.ref] = (comp.x, comp.y)
            ind.rotations[comp.ref] = comp.rotation
        return ind

    def _apply_individual(self, ind: Individual):
        """Apply individual's positions/rotations to components."""
        for ref, (x, y) in ind.positions.items():
            comp = self._component_map.get(ref)
            if comp and not comp.fixed:
                comp.x = x
                comp.y = y
                comp.rotation = ind.rotations.get(ref, comp.rotation)
                comp.update_pin_positions()

    def _initialize_population(self, population_size: int) -> list[Individual]:
        """
        Create initial population of random placements.

        Generates individuals with random positions within board bounds
        and random 90-degree rotations.
        """
        population = []
        movable = self._get_movable_components()
        min_x, min_y, max_x, max_y = self._board_bounds

        # Add margin to keep components inside
        margin = 2.0
        min_x += margin
        min_y += margin
        max_x -= margin
        max_y -= margin

        # First individual preserves current placement
        population.append(self._individual_from_current())

        # Generate random individuals
        for _ in range(population_size - 1):
            ind = Individual()
            for comp in movable:
                # Random position within board bounds
                x = random.uniform(min_x, max_x)
                y = random.uniform(min_y, max_y)

                # Snap to grid if configured
                if self.config.grid_snap > 0:
                    x = round(x / self.config.grid_snap) * self.config.grid_snap
                    y = round(y / self.config.grid_snap) * self.config.grid_snap

                ind.positions[comp.ref] = (x, y)

                # Random 90-degree rotation
                rotation = random.choice([0.0, 90.0, 180.0, 270.0])
                ind.rotations[comp.ref] = rotation

            population.append(ind)

        return population

    def _crossover(self, parent1: Individual, parent2: Individual) -> Individual:
        """
        Crossover: exchange component subsets between parents.

        Uses spatial partitioning - components in left half from parent1,
        right half from parent2 (with some randomization).

        Cluster integrity is preserved: all members of a cluster are taken
        from the same parent as the cluster anchor.
        """
        child = Individual()
        min_x, _, max_x, _ = self._board_bounds
        mid_x = (min_x + max_x) / 2

        # Add some randomization to the partition line
        partition_x = mid_x + random.gauss(0, (max_x - min_x) * 0.1)

        # Determine which parent each cluster should come from (based on anchor position)
        cluster_parent: dict[str, Individual] = {}  # ref -> parent for cluster members
        for cluster in self.clusters:
            if cluster.anchor in parent1.positions:
                anchor_x, _ = parent1.positions[cluster.anchor]
                chosen_parent = parent1 if anchor_x < partition_x else parent2
                # All cluster members should come from same parent
                cluster_parent[cluster.anchor] = chosen_parent
                for member in cluster.members:
                    cluster_parent[member] = chosen_parent

        for ref in parent1.positions:
            # Check if this component is part of a cluster
            if ref in cluster_parent:
                chosen = cluster_parent[ref]
                child.positions[ref] = chosen.positions[ref]
                child.rotations[ref] = chosen.rotations[ref]
            else:
                # Use parent1's current position to decide partition
                p1_x, p1_y = parent1.positions[ref]

                if p1_x < partition_x:
                    # Take from parent1
                    child.positions[ref] = parent1.positions[ref]
                    child.rotations[ref] = parent1.rotations[ref]
                else:
                    # Take from parent2
                    child.positions[ref] = parent2.positions[ref]
                    child.rotations[ref] = parent2.rotations[ref]

        return child

    def _mutate(self, ind: Individual) -> Individual:
        """
        Mutation: random position/rotation perturbations.

        - Position: Gaussian noise with configured sigma
        - Rotation: 90-degree increment with low probability
        """
        min_x, min_y, max_x, max_y = self._board_bounds
        margin = 1.0

        for ref in ind.positions:
            # Position mutation
            if random.random() < self.config.mutation_rate:
                x, y = ind.positions[ref]
                x += random.gauss(0, self.config.position_mutation_sigma)
                y += random.gauss(0, self.config.position_mutation_sigma)

                # Clamp to board bounds
                x = max(min_x + margin, min(max_x - margin, x))
                y = max(min_y + margin, min(max_y - margin, y))

                # Snap to grid if configured
                if self.config.grid_snap > 0:
                    x = round(x / self.config.grid_snap) * self.config.grid_snap
                    y = round(y / self.config.grid_snap) * self.config.grid_snap

                ind.positions[ref] = (x, y)

            # Rotation mutation
            if random.random() < self.config.rotation_mutation_prob:
                current_rot = ind.rotations[ref]
                new_rot = (current_rot + 90) % 360
                ind.rotations[ref] = new_rot

        return ind

    def _evaluate_fitness(self, ind: Individual) -> float:
        """
        Multi-objective fitness combining wire length, conflicts, and routability.

        Higher fitness is better.
        """
        # Apply positions temporarily
        original_positions = {}
        for ref, (x, y) in ind.positions.items():
            comp = self._component_map.get(ref)
            if comp:
                original_positions[ref] = (comp.x, comp.y, comp.rotation)
                comp.x = x
                comp.y = y
                comp.rotation = ind.rotations.get(ref, comp.rotation)
                comp.update_pin_positions()

        try:
            # Calculate objectives
            wire_length = self._total_wire_length()
            conflicts = self._count_conflicts()
            boundary_violations = self._count_boundary_violations()
            routability = self._estimate_routability()

            # Weighted sum (higher = better)
            fitness = (
                1000.0
                - wire_length * self.config.wire_length_weight
                - conflicts * self.config.conflict_weight
                - boundary_violations * self.config.boundary_violation_weight
                + routability * self.config.routability_weight
            )

            return fitness

        finally:
            # Restore original positions
            for ref, (x, y, rot) in original_positions.items():
                comp = self._component_map.get(ref)
                if comp:
                    comp.x = x
                    comp.y = y
                    comp.rotation = rot
                    comp.update_pin_positions()

    def _total_wire_length(self) -> float:
        """Compute total wire length from spring connections."""
        total = 0.0
        for spring in self.springs:
            comp1 = self._component_map.get(spring.comp1_ref)
            comp2 = self._component_map.get(spring.comp2_ref)

            if not comp1 or not comp2:
                continue

            pin1 = next((p for p in comp1.pins if p.number == spring.pin1_num), None)
            pin2 = next((p for p in comp2.pins if p.number == spring.pin2_num), None)

            if not pin1 or not pin2:
                continue

            dx = pin2.x - pin1.x
            dy = pin2.y - pin1.y
            total += math.sqrt(dx * dx + dy * dy)

        return total

    def _count_conflicts(self) -> int:
        """
        Count component overlaps (courtyard conflicts).

        Uses axis-aligned bounding box overlap detection.
        """
        conflicts = 0
        n = len(self.components)

        for i in range(n):
            comp1 = self.components[i]
            # Get AABB for comp1
            hw1, hh1 = comp1.width / 2, comp1.height / 2

            for j in range(i + 1, n):
                comp2 = self.components[j]
                hw2, hh2 = comp2.width / 2, comp2.height / 2

                # AABB overlap check
                dx = abs(comp1.x - comp2.x)
                dy = abs(comp1.y - comp2.y)

                if dx < (hw1 + hw2) and dy < (hh1 + hh2):
                    conflicts += 1

        return conflicts

    def _count_boundary_violations(self) -> int:
        """Count components that are outside board boundary."""
        violations = 0
        for comp in self.components:
            if not self.board_outline.contains_point(Vector2D(comp.x, comp.y)):
                violations += 1
        return violations

    def _estimate_routability(self) -> float:
        """
        Estimate routability based on component spacing and wire congestion.

        Returns a score from 0-100 where higher is better.
        """
        if not self.components:
            return 100.0

        # Calculate average spacing between components
        n = len(self.components)
        if n < 2:
            return 100.0

        total_spacing = 0.0
        count = 0
        for i in range(n):
            for j in range(i + 1, n):
                dx = self.components[i].x - self.components[j].x
                dy = self.components[i].y - self.components[j].y
                dist = math.sqrt(dx * dx + dy * dy)
                total_spacing += dist
                count += 1

        avg_spacing = total_spacing / count if count > 0 else 0

        # More spacing = better routability (up to a point)
        # Normalize to ~0-100 range
        routability_score = min(100.0, avg_spacing * 5.0)

        return routability_score

    def _tournament_select(self, population: list[Individual]) -> Individual:
        """Select individual using tournament selection."""
        tournament = random.sample(population, min(self.config.tournament_size, len(population)))
        return max(tournament, key=lambda ind: ind.fitness)

    def _evolve(self, population: list[Individual]) -> list[Individual]:
        """
        Perform one generation of evolution.

        Uses elitism, tournament selection, crossover, and mutation.
        """
        # Sort by fitness (highest first)
        population.sort(key=lambda ind: ind.fitness, reverse=True)

        new_population = []

        # Elitism: keep top individuals unchanged
        for i in range(min(self.config.elitism, len(population))):
            new_population.append(population[i].copy())

        # Fill rest with offspring
        while len(new_population) < len(population):
            # Tournament selection for parents
            parent1 = self._tournament_select(population)
            parent2 = self._tournament_select(population)

            # Crossover
            if random.random() < self.config.crossover_rate:
                child = self._crossover(parent1, parent2)
            else:
                child = parent1.copy()

            # Mutation
            child = self._mutate(child)

            new_population.append(child)

        return new_population

    def _evaluate_population(self, population: list[Individual]):
        """
        Evaluate fitness for all individuals in population.

        Uses parallel processing if configured.
        """
        if self.config.parallel and len(population) > 4:
            # Parallel evaluation
            # Note: This requires picklable objects; for simplicity,
            # we'll evaluate sequentially in the current implementation
            # since Component objects have complex state.
            # A proper implementation would serialize the individual data.
            for ind in population:
                ind.fitness = self._evaluate_fitness(ind)
        else:
            # Sequential evaluation
            for ind in population:
                ind.fitness = self._evaluate_fitness(ind)

    def _check_convergence(self) -> bool:
        """
        Check if population has converged.

        Returns True if fitness improvement has plateaued.
        """
        if len(self._fitness_history) < self.config.convergence_generations:
            return False

        recent = self._fitness_history[-self.config.convergence_generations :]
        if recent[0] == 0:
            return False

        improvement = (recent[-1] - recent[0]) / abs(recent[0])
        return improvement < self.config.convergence_threshold

    def optimize(
        self,
        generations: int | None = None,
        population_size: int | None = None,
        callback: Callable[[int, Individual], None] | None = None,
    ) -> Individual:
        """
        Run evolutionary optimization.

        Args:
            generations: Number of generations (default: config.generations)
            population_size: Population size (default: config.population_size)
            callback: Optional function called each generation with (gen, best_individual)

        Returns:
            Best individual found
        """
        generations = generations or self.config.generations
        population_size = population_size or self.config.population_size

        # Initialize population
        population = self._initialize_population(population_size)
        self._fitness_history = []

        for gen in range(generations):
            # Evaluate fitness
            self._evaluate_population(population)

            # Sort by fitness (highest first)
            population.sort(key=lambda ind: ind.fitness, reverse=True)
            best = population[0]

            self._fitness_history.append(best.fitness)

            if callback:
                callback(gen, best)

            # Check convergence
            if self._check_convergence():
                break

            # Evolve to next generation (except on last iteration)
            if gen < generations - 1:
                population = self._evolve(population)

        # Return best individual
        return population[0]

    def optimize_hybrid(
        self,
        evolutionary_generations: int = 50,
        population_size: int = 30,
        physics_iterations: int = 500,
        physics_config: PlacementConfig | None = None,
        callback: Callable[[int, Individual], None] | None = None,
    ) -> PlacementOptimizer:
        """
        Hybrid optimization: evolutionary global search + physics local refinement.

        Phase 1: Run evolutionary algorithm to find globally good placement
        Phase 2: Use physics-based simulation to refine the best result

        Args:
            evolutionary_generations: Generations for evolutionary phase
            population_size: Population size for evolutionary phase
            physics_iterations: Iterations for physics refinement
            physics_config: Configuration for physics optimizer
            callback: Optional callback for evolutionary phase

        Returns:
            PlacementOptimizer with refined placement (call write_to_pcb to save)
        """
        # Phase 1: Evolutionary optimization
        best = self.optimize(
            generations=evolutionary_generations,
            population_size=population_size,
            callback=callback,
        )

        # Apply best individual to components
        self._apply_individual(best)

        # Phase 2: Create physics optimizer from current state
        physics_opt = PlacementOptimizer(self.board_outline, physics_config)
        physics_opt.components = self.components
        physics_opt.springs = self.springs
        physics_opt._component_map = self._component_map

        # Run physics simulation for local refinement
        physics_opt.run(iterations=physics_iterations)

        # Snap to grid
        if self.config.grid_snap > 0:
            physics_opt.snap_positions(self.config.grid_snap)
        if self.config.rotation_snap > 0:
            physics_opt.snap_rotations(self.config.rotation_snap)

        return physics_opt

    def write_to_pcb(self, pcb: PCB) -> int:
        """
        Write current component positions back to a PCB object.

        Args:
            pcb: PCB object to update

        Returns:
            Number of components successfully updated
        """
        updated = 0
        for comp in self.components:
            if pcb.update_footprint_position(comp.ref, comp.x, comp.y, comp.rotation):
                updated += 1
        return updated

    def report(self) -> str:
        """Generate a text report of current placement."""
        wire_length = self._total_wire_length()
        conflicts = self._count_conflicts()
        boundary_violations = self._count_boundary_violations()

        lines = [
            "Evolutionary Placement Optimizer Report",
            "=" * 45,
            f"Components: {len(self.components)}",
            f"Springs (net connections): {len(self.springs)}",
            f"Total wire length: {wire_length:.2f} mm",
            f"Conflicts (overlaps): {conflicts}",
            f"Boundary violations: {boundary_violations}",
            "",
            "Component Positions:",
            "-" * 45,
        ]

        for comp in sorted(self.components, key=lambda c: c.ref):
            fixed = " [fixed]" if comp.fixed else ""
            lines.append(
                f"  {comp.ref:8s}: ({comp.x:7.2f}, {comp.y:7.2f}) @ {comp.rotation:6.1f}°{fixed}"
            )

        return "\n".join(lines)
