"""Abstract base class for placement optimization strategies.

Defines the interface that all placement optimizers must implement. The
strategy pattern enables swapping between different optimization backends
(CMA-ES, simulated annealing, genetic algorithms, etc.) while keeping the
rest of the placement pipeline unchanged.

Usage:
    strategy = SomeConcreteStrategy()
    initial = strategy.initialize(bounds, config)
    for generation in range(max_gens):
        candidates = strategy.suggest(n=pop_size)
        scores = [evaluate(c) for c in candidates]
        strategy.observe(candidates, scores)
        if strategy.converged:
            break
    best_vector, best_score = strategy.best()
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .vector import PlacementBounds, PlacementVector


@dataclass(frozen=True)
class StrategyConfig:
    """Configuration shared by all placement strategies.

    Attributes:
        max_iterations: Maximum number of generations/iterations.
        convergence_window: Number of generations to check for score plateau.
        convergence_threshold: Minimum relative improvement to avoid plateau
            detection. If the best score improves by less than this fraction
            over the convergence window, the optimizer is considered converged.
        seed: Random seed for reproducibility. None for non-deterministic.
        extra: Strategy-specific configuration as a free-form dict.
    """

    max_iterations: int = 1000
    convergence_window: int = 50
    convergence_threshold: float = 1e-8
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PlacementStrategy(ABC):
    """Abstract interface for placement optimization strategies.

    Follows the ask-tell pattern: the optimizer *suggests* candidate
    solutions, the caller *evaluates* them externally, and *observes*
    the resulting scores back to the optimizer.

    Lifecycle:
        1. ``initialize(bounds, config)`` -- set up the optimizer and return
           an initial population of placement vectors.
        2. ``suggest(n)`` -- ask the optimizer for *n* new candidates.
        3. ``observe(placements, scores)`` -- feed evaluation results back.
        4. Repeat 2-3 until ``converged`` is True or max iterations reached.
        5. ``best()`` -- retrieve the best solution found.

    Persistence:
        ``save_state(path)`` and ``load_state(path)`` enable checkpointing
        and resuming long optimization runs.
    """

    @abstractmethod
    def initialize(
        self,
        bounds: PlacementBounds,
        config: StrategyConfig,
    ) -> list[PlacementVector]:
        """Set up the optimizer and produce an initial population.

        Args:
            bounds: Per-dimension lower/upper bounds and discrete mask.
            config: Strategy configuration.

        Returns:
            Initial population of placement vectors for evaluation.
        """

    @abstractmethod
    def suggest(self, n: int) -> list[PlacementVector]:
        """Generate *n* new candidate placement vectors.

        The caller is responsible for evaluating these candidates and
        calling :meth:`observe` with the results.

        Args:
            n: Number of candidates to generate.

        Returns:
            List of *n* placement vectors.
        """

    @abstractmethod
    def observe(
        self,
        placements: list[PlacementVector],
        scores: list[float],
    ) -> None:
        """Feed evaluation results back to the optimizer.

        Must be called with the same placements returned by the most
        recent :meth:`suggest` call (in the same order).

        Args:
            placements: The evaluated placement vectors.
            scores: Corresponding scalar scores (lower is better).
        """

    @abstractmethod
    def best(self) -> tuple[PlacementVector, float]:
        """Return the best solution found so far.

        Returns:
            Tuple of (best_placement_vector, best_score).

        Raises:
            RuntimeError: If called before any observation.
        """

    @property
    @abstractmethod
    def converged(self) -> bool:
        """Whether the optimizer has detected convergence.

        Convergence criteria are strategy-specific but typically involve
        detecting a score plateau over a sliding window.
        """

    @abstractmethod
    def save_state(self, path: Path | str) -> None:
        """Persist optimizer state to disk for later resumption.

        Args:
            path: File path to write state to.
        """

    @classmethod
    @abstractmethod
    def load_state(cls, path: Path | str) -> PlacementStrategy:
        """Restore an optimizer from a previously saved state.

        Args:
            path: File path to read state from.

        Returns:
            A fully-initialized strategy instance ready to continue
            optimization from where it left off.
        """
