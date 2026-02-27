"""Bayesian Optimization placement strategy using Ax/BoTorch.

Implements the :class:`PlacementStrategy` interface with Bayesian Optimization
for sample-efficient placement search. Uses the Ax ``AxClient`` service API
for experiment management and BoTorch for the GP-based surrogate model and
acquisition functions.

Key features:
- Batch mode: 4-16 candidates per iteration via qExpectedImprovement (qEI)
- Latin Hypercube Sampling for initial design (5 x num_dimensions points)
- Mixed variable handling: continuous (x, y) + discrete (rotation, side)
  via Ax ChoiceParameter and RangeParameter types
- Input normalization handled internally by BoTorch
- Matern 5/2 kernel (BoTorch default GP kernel)
- Convergence detection via score plateau over sliding window
- State serialization for checkpoint/resume (replays observations on load)

Usage:
    from kicad_tools.placement.strategy import StrategyConfig
    from kicad_tools.placement.bo_strategy import BayesianOptStrategy

    strategy = BayesianOptStrategy()
    initial = strategy.initialize(bounds, StrategyConfig(seed=42))
    # ... ask/tell loop ...
    best_vec, best_score = strategy.best()
"""

from __future__ import annotations

import json
import logging
import warnings
from collections import deque
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from .strategy import PlacementStrategy, StrategyConfig
from .vector import PlacementBounds, PlacementVector

logger = logging.getLogger(__name__)

try:
    from ax.service.ax_client import AxClient
    from ax.service.utils.instantiation import ObjectiveProperties

    _HAS_AX = True
except ImportError:
    _HAS_AX = False


def _check_ax_available() -> None:
    """Raise ImportError if Ax/BoTorch are not installed."""
    if not _HAS_AX:
        raise ImportError(
            "The 'ax-platform' and 'botorch' packages are required for "
            "BayesianOptStrategy. Install them with: "
            "pip install 'kicad-tools[bayesian]'"
        )


def _latin_hypercube_sample(
    n_samples: int,
    lower: NDArray[np.float64],
    upper: NDArray[np.float64],
    discrete_mask: NDArray[np.bool_],
    rng: np.random.Generator,
) -> list[NDArray[np.float64]]:
    """Generate Latin Hypercube samples within bounds.

    For each dimension, divides the range into n_samples equal strata,
    places one sample per stratum, then shuffles across dimensions
    to produce a space-filling design.

    Discrete dimensions are rounded to the nearest integer.

    Args:
        n_samples: Number of samples to generate.
        lower: Lower bounds per dimension.
        upper: Upper bounds per dimension.
        discrete_mask: Boolean array, True for discrete dimensions.
        rng: NumPy random generator for reproducibility.

    Returns:
        List of sample arrays, each of shape (ndim,).
    """
    ndim = len(lower)
    samples = np.empty((n_samples, ndim), dtype=np.float64)

    for d in range(ndim):
        # Create stratified uniform samples in [0, 1)
        strata = (np.arange(n_samples) + rng.uniform(size=n_samples)) / n_samples
        # Shuffle to break correlation
        rng.shuffle(strata)
        # Scale to bounds
        samples[:, d] = lower[d] + strata * (upper[d] - lower[d])

    # Round discrete dimensions
    for d in range(ndim):
        if discrete_mask[d]:
            samples[:, d] = np.clip(
                np.round(samples[:, d]),
                lower[d],
                upper[d],
            )

    return [samples[i] for i in range(n_samples)]


class BayesianOptStrategy(PlacementStrategy):
    """Bayesian Optimization strategy using Ax/BoTorch.

    Uses Gaussian Process surrogate modeling with qExpectedImprovement
    acquisition for sample-efficient optimization. Particularly effective
    when each evaluation is expensive (e.g., involves trial routing at
    100ms-1s per evaluation), where BO's 10-50x sample efficiency over
    CMA-ES justifies the per-iteration overhead.

    The strategy uses two phases:
    1. Sobol quasi-random initialization (managed by Ax internally)
    2. BoTorch GP+qEI model-based batch suggestions

    The initial population returned by ``initialize()`` uses Latin Hypercube
    Sampling for a space-filling design (5 * ndim points by default).
    Subsequent ``suggest()`` calls use the Ax ``get_next_trial()`` API to
    generate model-based candidates.

    Attributes:
        _config: Strategy configuration.
        _bounds: Placement bounds.
        _batch_size: Number of candidates per batch suggestion.
        _generation: Current generation counter.
        _best_vector: Best placement vector found so far.
        _best_score: Score of the best placement vector.
        _score_history: Sliding window of best scores for convergence.
        _converged: Whether convergence has been detected.
        _all_vectors: All observed placement vectors (for save/load).
        _all_scores: All observed scores (for save/load).
        _n_initial: Number of initial LHS samples.
        _rng: NumPy random generator.
        _ax_client: Ax service client for experiment management.
    """

    def __init__(self) -> None:
        _check_ax_available()
        self._config: StrategyConfig | None = None
        self._bounds: PlacementBounds | None = None
        self._batch_size: int = 8
        self._generation: int = 0
        self._best_vector: PlacementVector | None = None
        self._best_score: float = float("inf")
        self._score_history: deque[float] = deque()
        self._converged: bool = False
        self._all_vectors: list[NDArray[np.float64]] = []
        self._all_scores: list[float] = []
        self._n_initial: int = 0
        self._rng: np.random.Generator = np.random.default_rng()
        self._ax_client: AxClient | None = None
        self._pending_candidates: list[PlacementVector] = []
        self._pending_trial_indices: list[int] = []
        self._initialized: bool = False

    def initialize(
        self,
        bounds: PlacementBounds,
        config: StrategyConfig,
    ) -> list[PlacementVector]:
        """Initialize the BO strategy and produce initial LHS population.

        Sets up the Ax experiment with:
        - Continuous RangeParameters for x, y dimensions
        - ChoiceParameters for rotation and side dimensions
        - Minimization of placement_cost objective
        - Sobol for initialization, then BoTorch GP+qEI

        Args:
            bounds: Per-dimension lower/upper bounds and discrete mask.
            config: Strategy configuration. Supports extra keys:
                - ``batch_size`` (int): Candidates per batch (default 8).
                - ``n_initial_factor`` (int): LHS samples = factor * ndim
                  (default 5).

        Returns:
            Initial population of placement vectors for external evaluation.
        """
        _check_ax_available()
        self._config = config
        self._bounds = bounds
        ndim = len(bounds.lower)

        # Configure batch size and initial samples
        self._batch_size = config.extra.get("batch_size", 8)
        n_initial_factor = config.extra.get("n_initial_factor", 5)
        self._n_initial = n_initial_factor * ndim

        # Set up RNG
        self._rng = np.random.default_rng(config.seed)

        # Reset state
        self._generation = 0
        self._best_vector = None
        self._best_score = float("inf")
        self._score_history = deque(maxlen=config.convergence_window)
        self._converged = False
        self._all_vectors = []
        self._all_scores = []
        self._pending_candidates = []
        self._pending_trial_indices = []

        # Build Ax parameter definitions
        parameters = []
        for d in range(ndim):
            if bounds.discrete_mask[d]:
                lo = int(bounds.lower[d])
                hi = int(bounds.upper[d])
                parameters.append(
                    {
                        "name": f"x{d}",
                        "type": "choice",
                        "values": list(range(lo, hi + 1)),
                        "value_type": "int",
                        "is_ordered": True,
                        "sort_values": True,
                    }
                )
            else:
                parameters.append(
                    {
                        "name": f"x{d}",
                        "type": "range",
                        "bounds": [float(bounds.lower[d]), float(bounds.upper[d])],
                        "value_type": "float",
                    }
                )

        # Create AxClient with logging suppressed
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self._ax_client = AxClient(
                random_seed=config.seed if config.seed is not None else 0,
                verbose_logging=False,
            )
            self._ax_client.create_experiment(
                name="placement_optimization",
                parameters=parameters,
                objectives={
                    "placement_cost": ObjectiveProperties(minimize=True),
                },
            )

        self._initialized = True

        # Generate initial LHS population
        return self._generate_lhs_initial()

    def _generate_lhs_initial(self) -> list[PlacementVector]:
        """Generate Latin Hypercube initial design.

        Returns:
            List of initial PlacementVectors.
        """
        assert self._bounds is not None
        samples = _latin_hypercube_sample(
            n_samples=self._n_initial,
            lower=self._bounds.lower,
            upper=self._bounds.upper,
            discrete_mask=self._bounds.discrete_mask,
            rng=self._rng,
        )
        vectors = [PlacementVector(data=s.copy()) for s in samples]
        self._pending_candidates = vectors
        self._pending_trial_indices = []  # LHS phase has no Ax trial indices
        return vectors

    def suggest(self, n: int) -> list[PlacementVector]:
        """Generate *n* candidate placement vectors.

        If still in the initial LHS phase (insufficient observations),
        returns additional LHS samples. Once enough data has been collected,
        uses the Ax/BoTorch model for GP+qEI-based suggestions.

        Args:
            n: Number of candidates to generate.

        Returns:
            List of *n* placement vectors.

        Raises:
            RuntimeError: If called before ``initialize()``.
        """
        if not self._initialized:
            raise RuntimeError("Must call initialize() before suggest()")
        assert self._bounds is not None
        assert self._ax_client is not None

        ndim = len(self._bounds.lower)
        candidates: list[PlacementVector] = []
        trial_indices: list[int] = []

        # Use Ax model-based generation
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                for _ in range(n):
                    params, trial_index = self._ax_client.get_next_trial()
                    data = np.array(
                        [params[f"x{d}"] for d in range(ndim)],
                        dtype=np.float64,
                    )
                    # Snap discrete dimensions
                    for d in range(ndim):
                        if self._bounds.discrete_mask[d]:
                            data[d] = np.clip(
                                np.round(data[d]),
                                self._bounds.lower[d],
                                self._bounds.upper[d],
                            )
                    candidates.append(PlacementVector(data=data))
                    trial_indices.append(trial_index)
        except Exception:
            # Fallback to LHS samples if model-based generation fails
            logger.debug("Ax generation failed, falling back to LHS samples")
            candidates = self._random_candidates(n)
            trial_indices = []

        self._pending_candidates = candidates
        self._pending_trial_indices = trial_indices
        return candidates

    def _random_candidates(self, n: int) -> list[PlacementVector]:
        """Generate random candidates as fallback.

        Args:
            n: Number of candidates.

        Returns:
            List of random PlacementVectors within bounds.
        """
        assert self._bounds is not None
        samples = _latin_hypercube_sample(
            n_samples=n,
            lower=self._bounds.lower,
            upper=self._bounds.upper,
            discrete_mask=self._bounds.discrete_mask,
            rng=self._rng,
        )
        return [PlacementVector(data=s.copy()) for s in samples]

    def observe(
        self,
        placements: list[PlacementVector],
        scores: list[float],
    ) -> None:
        """Feed evaluation results back to the optimizer.

        Records the observations in the Ax experiment so the GP model is
        updated for subsequent suggestions.

        Args:
            placements: Evaluated placement vectors (from last suggest).
            scores: Corresponding scalar scores (lower is better).

        Raises:
            RuntimeError: If called before ``initialize()``.
            ValueError: If lengths don't match or no pending candidates.
        """
        if not self._initialized:
            raise RuntimeError("Must call initialize() before observe()")
        assert self._config is not None
        assert self._ax_client is not None
        assert self._bounds is not None

        if len(placements) != len(scores):
            raise ValueError(f"Got {len(placements)} placements but {len(scores)} scores")

        if len(self._pending_candidates) != len(placements):
            raise ValueError(
                f"Got {len(placements)} placements but "
                f"{len(self._pending_candidates)} pending from last suggest()"
            )

        ndim = len(self._bounds.lower)

        # Report results to Ax
        if self._pending_trial_indices:
            # Model-based trials: complete them in Ax
            for trial_idx, score in zip(self._pending_trial_indices, scores, strict=True):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    self._ax_client.complete_trial(
                        trial_index=trial_idx,
                        raw_data={"placement_cost": score},
                    )
        else:
            # LHS phase: attach trials manually via attach_trial
            for placement, score in zip(placements, scores, strict=True):
                params = {f"x{d}": float(placement.data[d]) for d in range(ndim)}
                # Convert discrete dims to int for Ax
                for d in range(ndim):
                    if self._bounds.discrete_mask[d]:
                        params[f"x{d}"] = int(round(placement.data[d]))

                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    _params, trial_idx = self._ax_client.attach_trial(parameters=params)
                    self._ax_client.complete_trial(
                        trial_index=trial_idx,
                        raw_data={"placement_cost": score},
                    )

        self._pending_candidates = []
        self._pending_trial_indices = []

        # Store raw data for save/load
        for placement, score in zip(placements, scores, strict=True):
            self._all_vectors.append(placement.data.copy())
            self._all_scores.append(score)

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

        scores = list(self._score_history)
        oldest = scores[0]
        newest = scores[-1]

        if abs(oldest) < 1e-15:
            improvement = abs(oldest - newest)
        else:
            improvement = abs(oldest - newest) / abs(oldest)

        if improvement < self._config.convergence_threshold:
            self._converged = True

    def save_state(self, path: Path | str) -> None:
        """Persist optimizer state to a JSON file.

        Saves all observed data points, configuration, and bounds so the
        GP model can be rebuilt on load. The GP model itself is not
        serialized -- it is reconstructed from the observation history
        when ``load_state()`` is called.

        Args:
            path: File path to write state to.

        Raises:
            RuntimeError: If called before ``initialize()``.
        """
        if self._config is None or self._bounds is None:
            raise RuntimeError("Must call initialize() before save_state()")

        state = {
            "strategy": "bayesian_opt",
            "generation": self._generation,
            "batch_size": self._batch_size,
            "n_initial": self._n_initial,
            "best_score": self._best_score,
            "best_vector": (
                self._best_vector.data.tolist() if self._best_vector is not None else None
            ),
            "score_history": list(self._score_history),
            "converged": self._converged,
            "all_vectors": [v.tolist() for v in self._all_vectors],
            "all_scores": self._all_scores,
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
    def load_state(cls, path: Path | str) -> BayesianOptStrategy:
        """Restore a BO strategy from a previously saved state.

        Reconstructs the Ax experiment and replays all observed data
        to rebuild the GP model. The model is immediately ready for
        new suggestions.

        Args:
            path: File path to read state from.

        Returns:
            A BayesianOptStrategy instance ready to continue optimization.

        Raises:
            FileNotFoundError: If the state file does not exist.
            ValueError: If the state file is invalid.
        """
        _check_ax_available()
        path = Path(path)
        raw = json.loads(path.read_text())

        if raw.get("strategy") != "bayesian_opt":
            raise ValueError(f"Expected strategy 'bayesian_opt', got '{raw.get('strategy')}'")

        # Reconstruct config
        cfg_data = raw["config"]
        config_extra = dict(cfg_data.get("extra", {}))
        config_extra["batch_size"] = raw["batch_size"]
        config = StrategyConfig(
            max_iterations=cfg_data["max_iterations"],
            convergence_window=cfg_data["convergence_window"],
            convergence_threshold=cfg_data["convergence_threshold"],
            seed=cfg_data["seed"],
            extra=config_extra,
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
        instance.initialize(bounds, config)

        # Replay observations to rebuild the Ax experiment / GP model
        all_vectors = [np.array(v, dtype=np.float64) for v in raw["all_vectors"]]
        all_scores = raw["all_scores"]

        if all_vectors:
            # Replay in the same batch sizes used originally
            n_init = raw["n_initial"]
            batch_size = raw["batch_size"]

            # First batch is the LHS initial population
            first_batch = min(n_init, len(all_vectors))
            if first_batch > 0:
                batch_vecs = all_vectors[:first_batch]
                batch_scores = all_scores[:first_batch]
                placements = [PlacementVector(data=v.copy()) for v in batch_vecs]
                instance._pending_candidates = placements
                instance._pending_trial_indices = []
                instance.observe(placements, batch_scores)

            # Remaining batches use suggest/observe loop
            idx = first_batch
            while idx < len(all_vectors):
                end = min(idx + batch_size, len(all_vectors))
                batch_vecs = all_vectors[idx:end]
                batch_scores = all_scores[idx:end]
                n = len(batch_vecs)
                # We need to suggest to create Ax trials, then observe
                try:
                    instance.suggest(n)
                    # Replace suggested with actual historical vectors
                    actual = [PlacementVector(data=v.copy()) for v in batch_vecs]
                    # Complete the Ax trials with the actual scores
                    if instance._pending_trial_indices:
                        for trial_idx, score in zip(
                            instance._pending_trial_indices,
                            batch_scores,
                            strict=True,
                        ):
                            with warnings.catch_warnings():
                                warnings.simplefilter("ignore")
                                instance._ax_client.complete_trial(
                                    trial_index=trial_idx,
                                    raw_data={"placement_cost": score},
                                )
                        instance._pending_candidates = []
                        instance._pending_trial_indices = []
                        # Manually update tracking
                        for v, score in zip(batch_vecs, batch_scores, strict=True):
                            instance._all_vectors.append(v.copy())
                            instance._all_scores.append(score)
                            if score < instance._best_score:
                                instance._best_score = score
                                instance._best_vector = PlacementVector(data=v.copy())
                        instance._generation += 1
                        instance._score_history.append(instance._best_score)
                    else:
                        # Fallback: attach as manual trials
                        instance._pending_candidates = actual
                        instance._pending_trial_indices = []
                        instance.observe(actual, batch_scores)
                except Exception:
                    # If suggest fails during replay, attach manually
                    actual = [PlacementVector(data=v.copy()) for v in batch_vecs]
                    instance._pending_candidates = actual
                    instance._pending_trial_indices = []
                    instance.observe(actual, batch_scores)
                idx = end

        # Restore tracked state (overwrite what observe() computed)
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
