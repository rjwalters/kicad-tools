"""
Optimization workflow module.

Provides a high-level API for running placement optimizations,
extracting the core logic from CLI commands for reusability.

Example::

    from kicad_tools.optim.workflow import OptimizationWorkflow, OptimizationResult
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")
    workflow = OptimizationWorkflow(pcb)

    # Run force-directed optimization
    result = workflow.run(
        strategy="force-directed",
        iterations=1000,
        grid=0.5,
    )

    if result.success:
        print(f"Optimized {result.components_updated} components")
        print(f"Wire length: {result.wire_length_mm:.2f} mm")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from kicad_tools.optim.constraints import GroupingConstraint
    from kicad_tools.optim.evolutionary import EvolutionaryPlacementOptimizer
    from kicad_tools.optim.keepout import KeepoutZone
    from kicad_tools.optim.placement import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

__all__ = ["OptimizationResult", "OptimizationWorkflow"]


@dataclass
class RoutabilityInfo:
    """Routability check results."""

    rate: float
    nets: int
    problem_nets: int


@dataclass
class RoutabilityComparison:
    """Before/after routability comparison."""

    before: RoutabilityInfo
    after: RoutabilityInfo
    change: float


@dataclass
class OptimizationResult:
    """
    Result of a placement optimization run.

    Attributes:
        success: Whether optimization completed successfully.
        strategy: The optimization strategy used.
        components: Number of components in the optimization.
        iterations: Number of iterations/generations run.
        wire_length_mm: Total wire length after optimization.
        energy: System energy after optimization.
        components_updated: Number of components whose positions changed.
        constraint_violations: List of constraint violations (if any).
        routability: Routability comparison (if checked).
        output_path: Path where optimized PCB was saved (if saved).
        message: Human-readable status message.
    """

    success: bool = False
    strategy: str = ""
    components: int = 0
    iterations: int = 0
    wire_length_mm: float = 0.0
    energy: float = 0.0
    components_updated: int = 0
    constraint_violations: list[str] = field(default_factory=list)
    routability: RoutabilityComparison | None = None
    output_path: Path | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        result: dict[str, Any] = {
            "success": self.success,
            "strategy": self.strategy,
            "components": self.components,
            "iterations": self.iterations,
            "wire_length_mm": round(self.wire_length_mm, 2),
            "energy": round(self.energy, 4),
            "components_updated": self.components_updated,
            "constraint_violations": self.constraint_violations,
            "output_path": str(self.output_path) if self.output_path else None,
            "message": self.message,
        }
        if self.routability:
            result["routability"] = {
                "before": {
                    "rate": round(self.routability.before.rate, 3),
                    "nets": self.routability.before.nets,
                    "problem_nets": self.routability.before.problem_nets,
                },
                "after": {
                    "rate": round(self.routability.after.rate, 3),
                    "nets": self.routability.after.nets,
                    "problem_nets": self.routability.after.problem_nets,
                },
                "change": round(self.routability.change, 3),
            }
        else:
            result["routability"] = None
        return result


@dataclass
class WorkflowConfig:
    """Configuration for optimization workflow."""

    # Strategy selection
    strategy: str = "force-directed"

    # Common parameters
    grid: float = 0.0  # Grid snap in mm (0 = no snapping)
    iterations: int = 1000  # For force-directed and hybrid physics phase
    verbose: bool = False

    # Force-directed specific
    thermal: bool = False

    # Evolutionary specific
    generations: int = 100
    population: int = 50

    # Optional features
    enable_clustering: bool = False
    edge_detect: bool = False

    # Fixed components (list of reference designators)
    fixed_refs: list[str] = field(default_factory=list)


class OptimizationWorkflow:
    """
    High-level workflow for placement optimization.

    Encapsulates the logic for running different optimization strategies
    (force-directed, evolutionary, hybrid) with support for constraints,
    keepout zones, and routability checking.

    This class extracts core optimization logic from CLI commands to make
    it reusable from Python API without CLI dependencies.
    """

    def __init__(
        self,
        pcb: PCB,
        config: WorkflowConfig | None = None,
        constraints: list[GroupingConstraint] | None = None,
        keepout_zones: list[KeepoutZone] | None = None,
    ):
        """
        Initialize optimization workflow.

        Args:
            pcb: The PCB to optimize.
            config: Workflow configuration (uses defaults if None).
            constraints: Grouping constraints to apply.
            keepout_zones: Keepout zones to avoid.
        """
        self.pcb = pcb
        self.config = config or WorkflowConfig()
        self.constraints = constraints or []
        self.keepout_zones = keepout_zones or []

        # Will be set after optimization
        self._optimizer: PlacementOptimizer | EvolutionaryPlacementOptimizer | None = None

    @property
    def optimizer(
        self,
    ) -> PlacementOptimizer | EvolutionaryPlacementOptimizer | None:
        """Get the underlying optimizer (available after run())."""
        return self._optimizer

    def run(
        self,
        callback: Callable[[int, float], None] | None = None,
    ) -> OptimizationResult:
        """
        Run the placement optimization.

        Args:
            callback: Optional progress callback(iteration, energy/fitness).

        Returns:
            OptimizationResult with metrics and status.

        Raises:
            ValueError: If an unknown strategy is specified.
        """
        strategy = self.config.strategy

        if strategy == "force-directed":
            return self._run_force_directed(callback)
        elif strategy == "evolutionary":
            return self._run_evolutionary(callback)
        elif strategy == "hybrid":
            return self._run_hybrid(callback)
        else:
            return OptimizationResult(
                success=False,
                strategy=strategy,
                message=f"Unknown strategy: {strategy}",
            )

    def _run_force_directed(
        self,
        callback: Callable[[int, float], None] | None = None,
    ) -> OptimizationResult:
        """Run force-directed (physics-based) optimization."""
        from kicad_tools.optim import PlacementConfig, PlacementOptimizer, add_keepout_zones

        config = PlacementConfig(
            grid_size=self.config.grid if self.config.grid > 0 else 0.0,
            rotation_grid=90.0,
            thermal_enabled=self.config.thermal,
        )

        optimizer = PlacementOptimizer.from_pcb(
            self.pcb,
            config=config,
            fixed_refs=self.config.fixed_refs,
            enable_clustering=self.config.enable_clustering,
            edge_detect=self.config.edge_detect,
        )

        # Add constraints if loaded
        if self.constraints:
            optimizer.add_grouping_constraints(self.constraints)

        # Add keepout zones
        if self.keepout_zones:
            add_keepout_zones(optimizer, self.keepout_zones)

        # Run simulation
        iterations_run = optimizer.run(
            iterations=self.config.iterations,
            callback=callback,
        )

        # Snap to grid
        if self.config.grid > 0:
            optimizer.snap_to_grid(self.config.grid, 90.0)

        self._optimizer = optimizer

        # Get constraint violations
        violations: list[str] = []
        if self.constraints and hasattr(optimizer, "validate_constraints"):
            violations = optimizer.validate_constraints() or []

        return OptimizationResult(
            success=True,
            strategy="force-directed",
            components=len(optimizer.components),
            iterations=iterations_run,
            wire_length_mm=optimizer.total_wire_length(),
            energy=optimizer.compute_energy(),
            constraint_violations=violations,
            message="Optimization completed successfully",
        )

    def _run_evolutionary(
        self,
        callback: Callable[[int, float], None] | None = None,
    ) -> OptimizationResult:
        """Run evolutionary (genetic algorithm) optimization."""
        from kicad_tools.optim import EvolutionaryPlacementOptimizer, add_keepout_zones
        from kicad_tools.optim.evolutionary import EvolutionaryConfig

        config = EvolutionaryConfig(
            generations=self.config.generations,
            population_size=self.config.population,
            grid_snap=self.config.grid if self.config.grid > 0 else 0.127,
        )

        optimizer = EvolutionaryPlacementOptimizer.from_pcb(
            self.pcb,
            config=config,
            fixed_refs=self.config.fixed_refs,
            enable_clustering=self.config.enable_clustering,
        )

        # Add keepout zones
        if self.keepout_zones:
            add_keepout_zones(optimizer, self.keepout_zones)

        # Wrap callback for evolutionary interface
        evo_callback = None
        if callback:

            def evo_callback(gen: int, best: Any) -> None:
                callback(gen, best.fitness)

        # Run optimization
        best = optimizer.optimize(
            generations=self.config.generations,
            population_size=self.config.population,
            callback=evo_callback,
        )

        self._optimizer = optimizer

        return OptimizationResult(
            success=True,
            strategy="evolutionary",
            components=len(optimizer.components),
            iterations=self.config.generations,
            wire_length_mm=optimizer.total_wire_length(),
            energy=optimizer.compute_energy(),
            message=f"Best fitness: {best.fitness:.2f}",
        )

    def _run_hybrid(
        self,
        callback: Callable[[int, float], None] | None = None,
    ) -> OptimizationResult:
        """Run hybrid optimization (evolutionary + physics refinement)."""
        from kicad_tools.optim import (
            EvolutionaryPlacementOptimizer,
            PlacementConfig,
            add_keepout_zones,
        )
        from kicad_tools.optim.evolutionary import EvolutionaryConfig

        evo_config = EvolutionaryConfig(
            generations=self.config.generations,
            population_size=self.config.population,
            grid_snap=self.config.grid if self.config.grid > 0 else 0.127,
        )

        physics_config = PlacementConfig(
            grid_size=self.config.grid if self.config.grid > 0 else 0.0,
            rotation_grid=90.0,
        )

        evo_optimizer = EvolutionaryPlacementOptimizer.from_pcb(
            self.pcb,
            config=evo_config,
            fixed_refs=self.config.fixed_refs,
            enable_clustering=self.config.enable_clustering,
        )

        # Add keepout zones
        if self.keepout_zones:
            add_keepout_zones(evo_optimizer, self.keepout_zones)

        # Wrap callback for evolutionary interface
        evo_callback = None
        if callback:

            def evo_callback(gen: int, best: Any) -> None:
                callback(gen, best.fitness)

        # Run hybrid optimization
        physics_optimizer = evo_optimizer.optimize_hybrid(
            evolutionary_generations=self.config.generations,
            population_size=self.config.population,
            physics_iterations=self.config.iterations,
            physics_config=physics_config,
            callback=evo_callback,
        )

        self._optimizer = physics_optimizer

        return OptimizationResult(
            success=True,
            strategy="hybrid",
            components=len(physics_optimizer.components),
            iterations=self.config.generations + self.config.iterations,
            wire_length_mm=physics_optimizer.total_wire_length(),
            energy=physics_optimizer.compute_energy(),
            message="Hybrid optimization completed successfully",
        )

    def write_to_pcb(self) -> int:
        """
        Write optimized positions back to the PCB.

        Returns:
            Number of components updated.

        Raises:
            RuntimeError: If run() hasn't been called yet.
        """
        if self._optimizer is None:
            raise RuntimeError("Must call run() before write_to_pcb()")
        return self._optimizer.write_to_pcb(self.pcb)

    def save(self, output_path: Path | str) -> int:
        """
        Write optimized positions and save PCB to file.

        Args:
            output_path: Path to save the optimized PCB.

        Returns:
            Number of components updated.
        """
        updated = self.write_to_pcb()
        self.pcb.save(str(output_path))
        return updated

    def get_report(self) -> str:
        """
        Get optimization report.

        Returns:
            Human-readable report string.

        Raises:
            RuntimeError: If run() hasn't been called yet.
        """
        if self._optimizer is None:
            raise RuntimeError("Must call run() before get_report()")
        return self._optimizer.report()
