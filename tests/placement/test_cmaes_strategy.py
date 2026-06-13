"""Tests for CMAESStrategy convergence semantics.

These tests focus on the feasibility-gated convergence behaviour added
for issue #2821: the optimizer must not declare convergence while the
best-known placement is infeasible (signalled by ``best_score >=
INFEASIBILITY_OFFSET`` under :class:`CostMode.LEXICOGRAPHIC`).
"""

from __future__ import annotations

from kicad_tools.placement.cmaes_strategy import CMAESStrategy
from kicad_tools.placement.cost import INFEASIBILITY_OFFSET, BoardOutline
from kicad_tools.placement.strategy import StrategyConfig
from kicad_tools.placement.vector import ComponentDef, bounds


def _make_bounds():
    """Build placement bounds for a small synthetic board + components."""
    board = BoardOutline(min_x=0.0, min_y=0.0, max_x=50.0, max_y=50.0)
    components = [ComponentDef(reference=f"U{i + 1}", width=2.0, height=2.0) for i in range(3)]
    return bounds(board, components)


class TestFeasibilityGatedConvergence:
    """Issue #2821: convergence must be suppressed in the infeasible region."""

    def test_does_not_converge_while_infeasible(self):
        """Feeding plateaued infeasible scores must NOT flip ``converged``.

        Drive the strategy with synthetic ``observe()`` calls reporting
        identical infeasible scores (>= INFEASIBILITY_OFFSET). Even after
        ``convergence_window`` plateau-detection windows, ``converged``
        should remain False because the best-known score is still
        infeasible.
        """
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        # Score = INFEASIBILITY_OFFSET + small overlap penalty (matches
        # what _lexicographic_score produces for an infeasible placement).
        infeasible_score = INFEASIBILITY_OFFSET + 100.0

        # Run window + 10 generations of perfectly plateaued infeasible
        # scores. Without the feasibility gate, plateau detection would
        # flip _converged after the first ``convergence_window``
        # generations (around generation 5).
        for _ in range(config.convergence_window + 10):
            scores = [infeasible_score] * len(pop)
            strategy.observe(pop, scores)
            assert not strategy.converged, (
                "Strategy declared convergence while best-known score "
                f"({strategy.best()[1]}) is still infeasible "
                f">= {INFEASIBILITY_OFFSET}"
            )
            pop = strategy.suggest(6)

    def test_converges_once_feasible_and_plateau(self):
        """After a feasible score arrives and then plateaus, converge."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        # First, plateau in the infeasible region. No convergence yet.
        for _ in range(config.convergence_window + 2):
            scores = [INFEASIBILITY_OFFSET + 50.0] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(6)
        assert not strategy.converged

        # Now drop a single feasible score (well below the offset).
        # This becomes the new best, but score history still has older
        # infeasible best-scores in the window.
        scores = [42.0] + [INFEASIBILITY_OFFSET + 50.0] * (len(pop) - 1)
        strategy.observe(pop, scores)
        pop = strategy.suggest(6)

        # Now plateau at the feasible best for a full window.
        for _ in range(config.convergence_window + 2):
            scores = [INFEASIBILITY_OFFSET + 50.0] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(6)

        assert strategy.converged, (
            f"Strategy should converge after feasible best plus plateau (best={strategy.best()[1]})"
        )

    def test_score_just_below_offset_is_treated_as_feasible(self):
        """Boundary check: a best-score just under 1e12 counts as feasible.

        The feasibility gate uses ``best_score >= INFEASIBILITY_OFFSET``
        (1e12). A score of ``INFEASIBILITY_OFFSET - 1`` should not block
        convergence detection.
        """
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        feasible_score = INFEASIBILITY_OFFSET - 1.0
        for _ in range(config.convergence_window + 5):
            scores = [feasible_score] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(6)

        assert strategy.converged, (
            "A best-score just under INFEASIBILITY_OFFSET should be "
            "treated as feasible and allow plateau-convergence."
        )

    def test_score_at_exact_offset_blocks_convergence(self):
        """Boundary check: a best-score exactly at 1e12 blocks convergence."""
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        for _ in range(config.convergence_window + 5):
            scores = [float(INFEASIBILITY_OFFSET)] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(6)

        assert not strategy.converged, (
            "best_score == INFEASIBILITY_OFFSET should still be treated "
            "as infeasible (>= comparison)."
        )

    def test_weighted_sum_mode_unaffected(self):
        """Feasibility gate is a no-op for non-lexicographic scoring.

        The convergence check is purely score-based (it checks whether
        ``best_score >= INFEASIBILITY_OFFSET`` (1e12)). In ``WEIGHTED_SUM``
        mode the optimizer is operating with totally different score
        magnitudes, so the gate is inactive: small plateaued scores must
        still converge.
        """
        strategy = CMAESStrategy()
        b = _make_bounds()
        config = StrategyConfig(
            seed=42,
            convergence_window=5,
            convergence_threshold=1e-6,
            extra={"population_size": 6},
        )
        pop = strategy.initialize(b, config)

        # Small plateaued scores -- typical of WEIGHTED_SUM mode.
        for _ in range(config.convergence_window + 5):
            scores = [100.0] * len(pop)
            strategy.observe(pop, scores)
            pop = strategy.suggest(6)

        assert strategy.converged, (
            "Feasibility gate must not block convergence at small weighted-sum scores."
        )
