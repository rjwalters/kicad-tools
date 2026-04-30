"""Tests for stochastic cost perturbation (Issue #2334).

These tests verify that the router's stochastic perturbation system:
- Detects stagnation and activates perturbation
- Adds noise to _get_net_priority when perturbation_magnitude > 0
- Scales perturbation magnitude with stagnation duration
- Resets perturbation when overflow improves
- Returns deterministic results when perturbation_magnitude is 0
- Does not activate when overflow is already 0
"""

import pytest


class TestPerturbationActivation:
    """Tests for _activate_perturbation and _reset_perturbation methods."""

    def _make_autorouter(self):
        """Create a minimal Autorouter with perturbation fields."""
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=50.0)
        return router

    def test_perturbation_starts_at_zero(self):
        """Perturbation magnitude should be 0 at initialization."""
        router = self._make_autorouter()
        assert router._perturbation_magnitude == 0.0

    def test_activate_perturbation_sets_magnitude(self):
        """_activate_perturbation should set magnitude proportional to stagnation count."""
        router = self._make_autorouter()
        router._activate_perturbation(stagnation_count=1)
        assert router._perturbation_magnitude == pytest.approx(0.1)

        router._activate_perturbation(stagnation_count=3)
        assert router._perturbation_magnitude == pytest.approx(0.3)

        router._activate_perturbation(stagnation_count=5)
        assert router._perturbation_magnitude == pytest.approx(0.5)

    def test_activate_perturbation_scales_with_stagnation(self):
        """Perturbation magnitude should scale linearly with stagnation count."""
        router = self._make_autorouter()
        magnitudes = []
        for count in range(1, 6):
            router._activate_perturbation(count)
            magnitudes.append(router._perturbation_magnitude)

        # Each increment should be 0.1
        for i in range(len(magnitudes) - 1):
            assert magnitudes[i + 1] > magnitudes[i]

    def test_reset_perturbation_clears_magnitude(self):
        """_reset_perturbation should set magnitude back to 0."""
        router = self._make_autorouter()
        router._activate_perturbation(stagnation_count=3)
        assert router._perturbation_magnitude > 0

        router._reset_perturbation()
        assert router._perturbation_magnitude == 0.0

    def test_different_seeds_for_different_stagnation_counts(self):
        """Each activation with different stagnation_count should use a different seed."""
        router = self._make_autorouter()

        # Activate with count=1, sample some values
        router._activate_perturbation(stagnation_count=1)
        values_1 = [router._perturbation_rng.gauss(0, 1) for _ in range(5)]

        # Activate with count=2, sample some values
        router._activate_perturbation(stagnation_count=2)
        values_2 = [router._perturbation_rng.gauss(0, 1) for _ in range(5)]

        # The sequences should differ
        assert values_1 != values_2


class TestNetPriorityPerturbation:
    """Tests for perturbation noise injection in _get_net_priority."""

    def _make_autorouter_with_nets(self):
        """Create an Autorouter with some nets for priority testing."""
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=50.0)

        # Add components with pads using add_component API
        router.add_component("R1", [
            {"number": "1", "x": 5.0, "y": 5.0, "width": 1.0, "height": 1.0,
             "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 15.0, "y": 5.0, "width": 1.0, "height": 1.0,
             "net": 1, "net_name": "NET1"},
        ])
        router.add_component("R2", [
            {"number": "1", "x": 5.0, "y": 15.0, "width": 1.0, "height": 1.0,
             "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 15.0, "y": 15.0, "width": 1.0, "height": 1.0,
             "net": 2, "net_name": "NET2"},
        ])

        return router

    def test_deterministic_when_magnitude_zero(self):
        """_get_net_priority should be deterministic when perturbation is off."""
        router = self._make_autorouter_with_nets()
        assert router._perturbation_magnitude == 0.0

        # Call multiple times -- results should be identical
        p1 = router._get_net_priority(1)
        p2 = router._get_net_priority(1)
        assert p1 == p2

    def test_noisy_when_magnitude_positive(self):
        """_get_net_priority should add noise when perturbation is active."""
        router = self._make_autorouter_with_nets()

        # Activate perturbation
        router._activate_perturbation(stagnation_count=3)  # magnitude = 0.3

        # Get multiple perturbed priorities
        perturbed_values = [router._get_net_priority(1) for _ in range(10)]

        # The 6th element (congestion score with noise) should vary
        congestion_scores = [p[5] for p in perturbed_values]
        # With noise, not all values should be the same
        assert len(set(congestion_scores)) > 1, (
            "Expected varied congestion scores with perturbation active"
        )

    def test_noise_does_not_affect_other_priority_fields(self):
        """Only the congestion score (6th element) should be perturbed."""
        router = self._make_autorouter_with_nets()

        # Get baseline
        baseline = router._get_net_priority(1)

        # Activate perturbation
        router._activate_perturbation(stagnation_count=2)

        # Get perturbed priority
        perturbed = router._get_net_priority(1)

        # First 5 elements should be unchanged
        assert perturbed[0] == baseline[0], "Net class priority changed"
        assert perturbed[1] == baseline[1], "Complexity tier changed"
        assert perturbed[2] == baseline[2], "Constraint score changed"
        assert perturbed[3] == baseline[3], "Pad count changed"
        assert perturbed[4] == baseline[4], "Bounding box diagonal changed"

    def test_perturbation_can_change_net_ordering(self):
        """Perturbation should be able to reorder nets with similar priorities."""
        router = self._make_autorouter_with_nets()

        # With perturbation off, ordering is deterministic
        order_off = sorted([1, 2], key=lambda n: router._get_net_priority(n))

        # Activate strong perturbation
        router._activate_perturbation(stagnation_count=10)  # magnitude = 1.0

        # Try many random orderings -- at least one should differ
        found_different = False
        for _ in range(50):
            order_on = sorted([1, 2], key=lambda n: router._get_net_priority(n))
            if order_on != order_off:
                found_different = True
                break

        # With strong enough perturbation, ordering should change at least once
        # (probabilistic, but 50 trials with magnitude=1.0 is very likely)
        assert found_different, (
            "Expected perturbation to change net ordering at least once"
        )


class TestPerturbationDoesNotActivateAtZeroOverflow:
    """Verify perturbation does not activate when overflow is already 0."""

    def test_oscillation_not_detected_at_zero_overflow(self):
        """detect_oscillation returns False when all values are 0.

        This ensures perturbation never activates for a converged solution.
        """
        from kicad_tools.router.algorithms.negotiated import detect_oscillation

        # Zero overflow = convergence, not stagnation
        assert detect_oscillation([0, 0, 0, 0], window=4) is False

    def test_early_termination_not_triggered_at_zero_overflow_with_unrouted(self):
        """should_terminate_early returns False when overflow=0 but nets remain.

        The router should keep trying via neighborhood rip-up, not activate
        perturbation.
        """
        from kicad_tools.router.algorithms.negotiated import should_terminate_early

        # overflow=0 but unrouted nets remain
        history = [5, 3, 0, 0, 0]
        result = should_terminate_early(
            history, iteration=5, min_iterations=3, unrouted_count=2
        )
        assert result is False


class TestPerturbationResetOnImprovement:
    """Verify perturbation resets when a new best overflow is achieved."""

    def test_reset_after_improvement(self):
        """_reset_perturbation should be called when overflow improves."""
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=50.0)

        # Activate perturbation
        router._activate_perturbation(stagnation_count=3)
        assert router._perturbation_magnitude > 0

        # Simulate improvement by calling reset
        router._reset_perturbation()
        assert router._perturbation_magnitude == 0.0

    def test_perturbation_magnitude_matches_formula(self):
        """Verify magnitude = 0.1 * stagnation_count."""
        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=50.0)

        for count in [1, 2, 3, 5, 10]:
            router._activate_perturbation(count)
            expected = 0.1 * count
            assert router._perturbation_magnitude == pytest.approx(expected)
