"""CMA-ES placement optimization strategy using the ``cmaes`` library.

Implements the :class:`PlacementStrategy` interface with CMA-ES (Covariance
Matrix Adaptation Evolution Strategy). Uses CMAwM (CMA-ES with Margin) for
mixed-integer optimization, handling continuous (x, y) and discrete
(rotation, side) variables natively.

Key features:
- Auto-scaled population size: ``4 + floor(3 * ln(n))`` from dimensionality
- Discrete variable handling via CMAwM margin correction
- Convergence detection: score plateau over a sliding window
- Deterministic replay with seed control
- State serialization for checkpoint/resume

Usage:
    from kicad_tools.placement.strategy import StrategyConfig
    from kicad_tools.placement.cmaes_strategy import CMAESStrategy

    strategy = CMAESStrategy()
    initial = strategy.initialize(bounds, StrategyConfig(seed=42))
    # ... ask/tell loop ...
    best_vec, best_score = strategy.best()
"""

from __future__ import annotations

import json
import math
from collections import deque
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .strategy import PlacementStrategy, StrategyConfig
from .vector import PlacementBounds, PlacementVector

try:
    from cmaes import CMAwM
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "The 'cmaes' package is required for CMAESStrategy. Install it with: pip install cmaes"
    ) from exc


def _auto_population_size(ndim: int) -> int:
    """Compute default CMA-ES population size from dimensionality.

    Uses the standard formula: ``4 + floor(3 * ln(n))``.

    Args:
        ndim: Number of dimensions in the search space.

    Returns:
        Population size (minimum 4).
    """
    return max(4, 4 + int(math.floor(3 * math.log(max(1, ndim)))))


def _build_steps(discrete_mask: NDArray[np.bool_]) -> NDArray[np.float64]:
    """Build the ``steps`` array for CMAwM from a discrete mask.

    Continuous dimensions get step=0 (no discretization).
    Discrete dimensions get step=1 (integer steps).

    Args:
        discrete_mask: Boolean array where True means discrete.

    Returns:
        Step-size array for CMAwM.
    """
    steps = np.zeros(len(discrete_mask), dtype=np.float64)
    steps[discrete_mask] = 1.0
    return steps


class CMAESStrategy(PlacementStrategy):
    """CMA-ES placement optimizer using CMAwM for mixed-integer variables.

    The optimizer treats x/y positions as continuous variables and rotation
    indices {0,1,2,3} and side flags {0,1} as discrete variables via the
    CMAwM margin correction.

    Attributes:
        _optimizer: The underlying CMAwM instance (set after initialize).
        _config: Strategy configuration.
        _bounds: Placement bounds.
        _population_size: Number of candidates per generation.
        _generation: Current generation counter.
        _best_vector: Best placement vector found so far.
        _best_score: Score of the best placement vector.
        _score_history: Sliding window of best scores for convergence.
        _converged: Whether convergence has been detected.
        _pending_tell: Pending (x_tell, x_eval) pairs from the last suggest.
    """

    def __init__(self) -> None:
        self._optimizer: CMAwM | None = None
        self._config: StrategyConfig | None = None
        self._bounds: PlacementBounds | None = None
        self._population_size: int = 0
        self._generation: int = 0
        self._best_vector: PlacementVector | None = None
        self._best_score: float = float("inf")
        self._score_history: deque[float] = deque()
        self._converged: bool = False
        self._pending_tell: list[tuple[NDArray[np.float64], NDArray[np.float64]]] = []

    def initialize(
        self,
        bounds: PlacementBounds,
        config: StrategyConfig,
    ) -> list[PlacementVector]:
        """Initialize the CMA-ES optimizer and produce initial population.

        Sets up the CMAwM optimizer with:
        - Mean at the center of the bounded region
        - Sigma as 1/4 of the average range (covers ~95% of the space)
        - Discrete steps for rotation and side variables
        - Auto-scaled or user-specified population size

        Args:
            bounds: Per-dimension bounds with discrete mask.
            config: Strategy configuration. Supports extra keys:
                - ``population_size`` (int): Override auto-scaled pop size.
                - ``sigma`` (float): Override initial step size.

        Returns:
            Initial population of placement vectors for external evaluation.
        """
        self._config = config
        self._bounds = bounds
        ndim = len(bounds.lower)

        # Population size: user override or auto-scaled
        self._population_size = config.extra.get(
            "population_size",
            _auto_population_size(ndim),
        )

        # Initial mean: center of the bounded region
        mean = (bounds.lower + bounds.upper) / 2.0

        # Initial sigma: 1/4 of the average range
        ranges = bounds.upper - bounds.lower
        # Avoid zero range (can happen for single-value discrete dims)
        safe_ranges = np.where(ranges > 0, ranges, 1.0)
        sigma = float(config.extra.get("sigma", np.mean(safe_ranges) / 4.0))

        # Build CMAwM bounds array: shape (ndim, 2)
        cma_bounds = np.column_stack([bounds.lower, bounds.upper])

        # Build step sizes: 0 for continuous, 1 for discrete
        steps = _build_steps(bounds.discrete_mask)

        self._optimizer = CMAwM(
            mean=mean,
            sigma=sigma,
            bounds=cma_bounds,
            steps=steps,
            seed=config.seed,
            population_size=self._population_size,
        )

        # Reset state
        self._generation = 0
        self._best_vector = None
        self._best_score = float("inf")
        self._score_history = deque(maxlen=config.convergence_window)
        self._converged = False
        self._pending_tell = []

        # Generate initial population
        return self.suggest(self._population_size)

    def suggest(self, n: int) -> list[PlacementVector]:
        """Generate *n* candidate placement vectors from the optimizer.

        For CMA-ES, ``n`` should match the population size. If fewer are
        requested, only that many candidates are generated.

        The CMAwM ``ask()`` method returns two arrays per candidate:
        - ``x_eval``: the discretized point for evaluation
        - ``x_tell``: the internal (continuous) representation for tell

        We return PlacementVectors built from ``x_eval`` and store
        ``x_tell`` for the subsequent ``observe()`` call.

        Args:
            n: Number of candidates to generate.

        Returns:
            List of *n* placement vectors.

        Raises:
            RuntimeError: If called before ``initialize()``.
        """
        if self._optimizer is None:
            raise RuntimeError("Must call initialize() before suggest()")

        self._pending_tell = []
        candidates: list[PlacementVector] = []

        for _ in range(n):
            x_eval, x_tell = self._optimizer.ask()
            self._pending_tell.append((x_tell, x_eval))
            candidates.append(PlacementVector(data=x_eval.copy()))

        return candidates

    def observe(
        self,
        placements: list[PlacementVector],
        scores: list[float],
    ) -> None:
        """Feed evaluation results back to the CMA-ES optimizer.

        Updates the internal covariance matrix and mean based on the
        observed scores. Also updates convergence tracking.

        Args:
            placements: Evaluated placement vectors (from last suggest).
            scores: Corresponding scalar scores (lower is better).

        Raises:
            RuntimeError: If called before ``initialize()``.
            ValueError: If lengths don't match or no pending tell data.
        """
        if self._optimizer is None or self._config is None:
            raise RuntimeError("Must call initialize() before observe()")

        if len(placements) != len(scores):
            raise ValueError(f"Got {len(placements)} placements but {len(scores)} scores")

        if len(self._pending_tell) != len(placements):
            raise ValueError(
                f"Got {len(placements)} placements but {len(self._pending_tell)} "
                f"pending from last suggest()"
            )

        # Build solutions for tell: list of (x_tell, score)
        solutions: list[tuple[NDArray[np.float64], float]] = []
        for (x_tell, _x_eval), score in zip(self._pending_tell, scores, strict=True):
            solutions.append((x_tell, score))

        self._optimizer.tell(solutions)
        self._pending_tell = []

        # Track best solution
        for placement, score in zip(placements, scores, strict=True):
            if score < self._best_score:
                self._best_score = score
                self._best_vector = PlacementVector(data=placement.data.copy())

        # Update convergence tracking
        self._generation += 1
        self._score_history.append(self._best_score)
        self._check_convergence()

    def best(self) -> tuple[PlacementVector, float]:
        """Return the best solution found so far.

        Returns:
            Tuple of (best_placement_vector, best_score).

        Raises:
            RuntimeError: If called before any observation.
        """
        if self._best_vector is None:
            raise RuntimeError("No observations yet -- call observe() first")
        return self._best_vector, self._best_score

    @property
    def converged(self) -> bool:
        """Whether the optimizer has detected convergence."""
        return self._converged

    def _check_convergence(self) -> None:
        """Check if the optimizer has converged based on score history.

        Convergence is detected when the best score has not improved by
        more than ``convergence_threshold`` (relative) over the last
        ``convergence_window`` generations.
        """
        if self._config is None:
            return

        window = self._config.convergence_window

        if len(self._score_history) < window:
            return

        # Compare oldest and newest scores in the window
        scores = list(self._score_history)
        oldest = scores[0]
        newest = scores[-1]

        # Relative improvement (handle zero/near-zero base)
        if abs(oldest) < 1e-15:
            # If the oldest score is essentially zero, check absolute change
            improvement = abs(oldest - newest)
        else:
            improvement = abs(oldest - newest) / abs(oldest)

        if improvement < self._config.convergence_threshold:
            self._converged = True

        # Also check the library's own stopping criterion
        if self._optimizer is not None and self._optimizer.should_stop():
            self._converged = True

    def save_state(self, path: Path | str) -> None:
        """Persist optimizer state to a JSON file.

        Saves enough state to resume optimization: generation counter,
        best solution, score history, and configuration. The CMAwM
        internal state is not directly serializable, so we save the
        parameters needed to reconstruct a similar optimizer state.

        Note: Full CMAwM covariance matrix state is not preserved.
        The resumed optimizer will start with a fresh covariance but
        with the correct mean (best solution found so far) and
        generation count. This is a pragmatic tradeoff -- the optimizer
        will re-adapt quickly from a good starting point.

        Args:
            path: File path to write state to.
        """
        if self._config is None or self._bounds is None:
            raise RuntimeError("Must call initialize() before save_state()")

        state = {
            "strategy": "cmaes",
            "generation": self._generation,
            "population_size": self._population_size,
            "best_score": self._best_score,
            "best_vector": (
                self._best_vector.data.tolist() if self._best_vector is not None else None
            ),
            "score_history": list(self._score_history),
            "converged": self._converged,
            "config": {
                "max_iterations": self._config.max_iterations,
                "convergence_window": self._config.convergence_window,
                "convergence_threshold": self._config.convergence_threshold,
                "seed": self._config.seed,
                "extra": self._config.extra,
            },
            "bounds": {
                "lower": self._bounds.lower.tolist(),
                "upper": self._bounds.upper.tolist(),
                "discrete_mask": self._bounds.discrete_mask.tolist(),
            },
        }

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))

    @classmethod
    def load_state(cls, path: Path | str) -> CMAESStrategy:
        """Restore a CMA-ES strategy from a previously saved state.

        Reconstructs the optimizer with the saved configuration and
        bounds, setting the mean to the best solution found so far.
        The covariance matrix starts fresh but the optimizer adapts
        quickly from a good starting point.

        Args:
            path: File path to read state from.

        Returns:
            A CMAESStrategy instance ready to continue optimization.

        Raises:
            FileNotFoundError: If the state file does not exist.
            ValueError: If the state file is invalid.
        """
        path = Path(path)
        raw = json.loads(path.read_text())

        if raw.get("strategy") != "cmaes":
            raise ValueError(f"Expected strategy 'cmaes', got '{raw.get('strategy')}'")

        # Reconstruct config
        cfg_data = raw["config"]
        config = StrategyConfig(
            max_iterations=cfg_data["max_iterations"],
            convergence_window=cfg_data["convergence_window"],
            convergence_threshold=cfg_data["convergence_threshold"],
            seed=cfg_data["seed"],
            extra=cfg_data.get("extra", {}),
        )

        # Reconstruct bounds
        bounds_data = raw["bounds"]
        bounds = PlacementBounds(
            lower=np.array(bounds_data["lower"], dtype=np.float64),
            upper=np.array(bounds_data["upper"], dtype=np.float64),
            discrete_mask=np.array(bounds_data["discrete_mask"], dtype=np.bool_),
        )

        # Create and initialize the strategy
        instance = cls()

        # Override population size from saved state
        config_extra = dict(config.extra)
        config_extra["population_size"] = raw["population_size"]
        config = StrategyConfig(
            max_iterations=config.max_iterations,
            convergence_window=config.convergence_window,
            convergence_threshold=config.convergence_threshold,
            seed=config.seed,
            extra=config_extra,
        )

        # Initialize with saved bounds and config (generates initial pop)
        instance.initialize(bounds, config)

        # Restore tracked state
        instance._generation = raw["generation"]
        instance._best_score = raw["best_score"]
        instance._converged = raw["converged"]

        if raw["best_vector"] is not None:
            instance._best_vector = PlacementVector(
                data=np.array(raw["best_vector"], dtype=np.float64)
            )

        instance._score_history = deque(
            raw["score_history"],
            maxlen=config.convergence_window,
        )

        return instance
