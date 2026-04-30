"""Tests for PathFinder negotiated congestion cost model (Issue #2333).

Covers:
- EMA smoothing of per-cell present cost
- Exponential present cost escalation
- Congestion-ratio-based parameter auto-tuning
- Hotset-only rerouting mode
"""

import numpy as np
import pytest

from kicad_tools.router.algorithms.negotiated import (
    calculate_congestion_tuned_params,
    calculate_present_cost,
)


# =========================================================================
# Exponential present cost escalation
# =========================================================================


class TestExponentialPresentCost:
    """Tests for exponential present cost escalation mode."""

    def test_exponential_grows_faster_than_linear(self):
        """Exponential mode should produce higher costs at later iterations."""
        linear = calculate_present_cost(
            iteration=5, total_iterations=10, overflow_ratio=0.05, base_cost=0.5,
            exponential=False,
        )
        exp = calculate_present_cost(
            iteration=5, total_iterations=10, overflow_ratio=0.05, base_cost=0.5,
            exponential=True, pres_fac_mult=1.3,
        )
        # At iteration 5 with mult 1.3: 0.5 * 1.3^5 = 0.5 * 3.71 = 1.856
        assert exp > linear

    def test_exponential_at_iteration_zero(self):
        """At iteration 0, exponential should equal base cost (1.3^0 = 1)."""
        result = calculate_present_cost(
            iteration=0, total_iterations=10, overflow_ratio=0.0, base_cost=0.5,
            exponential=True, pres_fac_mult=1.3,
        )
        assert result == pytest.approx(0.5)

    def test_exponential_respects_cap(self):
        """Exponential cost should be capped at pres_fac_cap."""
        result = calculate_present_cost(
            iteration=100, total_iterations=100, overflow_ratio=0.0, base_cost=0.5,
            exponential=True, pres_fac_mult=1.3, pres_fac_cap=10.0,
        )
        assert result == pytest.approx(10.0)

    def test_exponential_formula(self):
        """Verify exact exponential formula: base * mult^iteration."""
        result = calculate_present_cost(
            iteration=3, total_iterations=10, overflow_ratio=0.0, base_cost=1.0,
            exponential=True, pres_fac_mult=2.0, pres_fac_cap=100.0,
        )
        # 1.0 * 2.0^3 = 8.0
        assert result == pytest.approx(8.0)

    def test_backward_compatible_linear_default(self):
        """Default (exponential=False) should produce same results as before."""
        result = calculate_present_cost(
            iteration=3, total_iterations=10, overflow_ratio=0.1, base_cost=0.5,
        )
        # progress_factor = 1 + 3/10 = 1.3
        # congestion_factor = 1 + min(0.2, 2.0) = 1.2
        # 0.5 * 1.3 * 1.2 = 0.78
        assert result == pytest.approx(0.78)


# =========================================================================
# Congestion-ratio-based auto-tuning
# =========================================================================


class TestCongestionAutoTune:
    """Tests for calculate_congestion_tuned_params."""

    def test_high_congestion_increases_params(self):
        """High congestion (>10%) should increase both parameters."""
        mult, hist = calculate_congestion_tuned_params(
            overflow_ratio=0.15, base_pres_fac_mult=1.3, base_history_increment=0.5,
        )
        # scale = 1 + min(0.15, 0.5) = 1.15
        # mult = 1 + (1.3 - 1) * 1.15 = 1 + 0.345 = 1.345
        assert mult == pytest.approx(1.345)
        # hist = 0.5 * 1.15 = 0.575
        assert hist == pytest.approx(0.575)

    def test_low_congestion_reduces_params(self):
        """Low congestion (<1%) should reduce both parameters."""
        mult, hist = calculate_congestion_tuned_params(
            overflow_ratio=0.005, base_pres_fac_mult=1.3, base_history_increment=0.5,
        )
        # scale = 0.7
        # mult = 1 + 0.3 * 0.7 = 1.21
        assert mult == pytest.approx(1.21)
        # hist = 0.5 * 0.7 = 0.35
        assert hist == pytest.approx(0.35)

    def test_moderate_congestion_unchanged(self):
        """Moderate congestion (1-10%) should use base parameters."""
        mult, hist = calculate_congestion_tuned_params(
            overflow_ratio=0.05, base_pres_fac_mult=1.3, base_history_increment=0.5,
        )
        assert mult == pytest.approx(1.3)
        assert hist == pytest.approx(0.5)

    def test_very_high_congestion_capped(self):
        """Very high congestion should cap the scale factor."""
        mult, hist = calculate_congestion_tuned_params(
            overflow_ratio=0.8, base_pres_fac_mult=1.3, base_history_increment=0.5,
        )
        # scale = 1 + min(0.8, 0.5) = 1.5
        assert mult == pytest.approx(1.0 + 0.3 * 1.5)
        assert hist == pytest.approx(0.5 * 1.5)

    def test_zero_congestion(self):
        """Zero congestion should use the low-congestion scale."""
        mult, hist = calculate_congestion_tuned_params(
            overflow_ratio=0.0, base_pres_fac_mult=1.3, base_history_increment=0.5,
        )
        # 0.0 < 0.01 -> scale = 0.7
        assert mult == pytest.approx(1.21)
        assert hist == pytest.approx(0.35)


# =========================================================================
# EMA smoothing of per-cell present cost
# =========================================================================


class TestEMASmoothing:
    """Tests for grid EMA present cost smoothing."""

    def _make_grid(self):
        """Create a minimal RoutingGrid for EMA testing."""
        from unittest.mock import MagicMock

        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid.__new__(RoutingGrid)
        grid._backend = np
        grid._backend_type = MagicMock()
        grid._backend_type.name = "CPU"
        # Use a property check that works
        from kicad_tools.router.grid import BackendType
        grid._backend_type = BackendType.CPU
        grid._usage_count = np.array([[[0, 1, 2]]], dtype=np.int16)
        grid._present_cost_ema = None
        return grid

    def test_first_call_initializes_ema(self):
        """First EMA update should set EMA to current present cost."""
        grid = self._make_grid()
        grid.update_present_cost_ema(present_cost_factor=2.0, alpha=0.6)

        assert grid._present_cost_ema is not None
        # Should equal present_cost_factor * usage_count
        expected = np.array([[[0.0, 2.0, 4.0]]], dtype=np.float32)
        np.testing.assert_array_almost_equal(grid._present_cost_ema, expected)

    def test_ema_smooths_between_old_and_new(self):
        """Subsequent EMA updates should blend old and new values."""
        grid = self._make_grid()
        # Initialize with factor=2.0 -> [0, 2, 4]
        grid.update_present_cost_ema(present_cost_factor=2.0, alpha=0.6)

        # Update with factor=10.0 -> new = [0, 10, 20]
        # EMA = 0.6 * [0, 10, 20] + 0.4 * [0, 2, 4] = [0, 6.8, 13.6]
        grid.update_present_cost_ema(present_cost_factor=10.0, alpha=0.6)

        expected = np.array([[[0.0, 6.8, 13.6]]], dtype=np.float32)
        np.testing.assert_array_almost_equal(grid._present_cost_ema, expected)

    def test_ema_with_alpha_one_uses_new_value(self):
        """alpha=1.0 should completely replace with new value."""
        grid = self._make_grid()
        grid.update_present_cost_ema(present_cost_factor=2.0, alpha=1.0)
        grid.update_present_cost_ema(present_cost_factor=5.0, alpha=1.0)

        expected = np.array([[[0.0, 5.0, 10.0]]], dtype=np.float32)
        np.testing.assert_array_almost_equal(grid._present_cost_ema, expected)

    def test_ema_with_alpha_zero_keeps_old_value(self):
        """alpha=0.0 should keep the previous EMA value."""
        grid = self._make_grid()
        grid.update_present_cost_ema(present_cost_factor=2.0, alpha=0.0)
        # First call initializes to [0, 2, 4] (copy, not blend)
        # Second call: 0 * new + 1.0 * old
        grid.update_present_cost_ema(present_cost_factor=100.0, alpha=0.0)

        expected = np.array([[[0.0, 2.0, 4.0]]], dtype=np.float32)
        np.testing.assert_array_almost_equal(grid._present_cost_ema, expected)

    def test_ema_not_allocated_by_default(self):
        """EMA array should be None until first update."""
        grid = self._make_grid()
        assert grid._present_cost_ema is None


# =========================================================================
# Hotset-only mode
# =========================================================================


class TestHotsetOnlyMode:
    """Tests for hotset_only flag skipping fallback strategies."""

    def test_hotset_only_parameter_accepted(self):
        """route_all_negotiated should accept hotset_only parameter."""
        from unittest.mock import MagicMock

        from kicad_tools.router.core import Autorouter

        # Just verify the parameter is accepted without TypeError
        router = Autorouter.__new__(Autorouter)
        # We can't easily call route_all_negotiated without full setup,
        # so just verify the method signature accepts the parameter
        import inspect
        sig = inspect.signature(router.route_all_negotiated)
        assert "hotset_only" in sig.parameters
        assert sig.parameters["hotset_only"].default is False

    def test_ema_smoothing_parameter_accepted(self):
        """route_all_negotiated should accept ema_smoothing parameter."""
        from kicad_tools.router.core import Autorouter
        import inspect
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert "ema_smoothing" in sig.parameters
        assert sig.parameters["ema_smoothing"].default is False

    def test_exponential_cost_parameter_accepted(self):
        """route_all_negotiated should accept exponential_cost parameter."""
        from kicad_tools.router.core import Autorouter
        import inspect
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert "exponential_cost" in sig.parameters
        assert sig.parameters["exponential_cost"].default is False

    def test_congestion_auto_tune_parameter_accepted(self):
        """route_all_negotiated should accept congestion_auto_tune parameter."""
        from kicad_tools.router.core import Autorouter
        import inspect
        sig = inspect.signature(Autorouter.route_all_negotiated)
        assert "congestion_auto_tune" in sig.parameters
        assert sig.parameters["congestion_auto_tune"].default is False


# =========================================================================
# A* cost lookup with EMA
# =========================================================================


class TestAStarEMAIntegration:
    """Tests for A* cost lookup using EMA present cost."""

    def test_negotiated_cost_uses_ema_when_available(self):
        """get_negotiated_cost should use EMA values when present."""
        from unittest.mock import MagicMock

        from kicad_tools.router.grid import RoutingGrid

        grid = RoutingGrid.__new__(RoutingGrid)
        grid._backend = np
        grid._backend_type = MagicMock()

        # Set up a 1-layer, 1x3 grid
        from kicad_tools.router.grid import BackendType
        grid._backend_type = BackendType.CPU
        grid.cols = 3
        grid.rows = 1
        grid.num_layers = 1
        grid._usage_count = np.array([[[0, 2, 1]]], dtype=np.int16)
        grid._history_cost = np.array([[[0.0, 1.0, 0.5]]], dtype=np.float32)
        grid._present_cost_ema = np.array([[[0.0, 5.0, 3.0]]], dtype=np.float32)
        grid._is_obstacle = np.array([[[False, False, False]]])
        grid._blocked = np.array([[[False, False, False]]])

        # Create minimal Cell mocks
        cell1 = MagicMock()
        cell1.is_obstacle = False
        cell1.usage_count = 2
        cell1.history_cost = 1.0

        cell2 = MagicMock()
        cell2.is_obstacle = False
        cell2.usage_count = 1
        cell2.history_cost = 0.5

        grid.grid = [[
            [MagicMock(is_obstacle=False), cell1, cell2],
        ]]

        # With EMA, present cost should use EMA value, not factor * usage
        cost = grid.get_negotiated_cost(1, 0, 0, present_cost_factor=100.0)
        # Should use EMA value (5.0) + history (1.0) = 6.0
        # NOT 100.0 * 2 + 1.0 = 201.0
        assert cost == pytest.approx(6.0)

    def test_negotiated_cost_without_ema(self):
        """get_negotiated_cost should use factor * usage when no EMA."""
        from unittest.mock import MagicMock

        from kicad_tools.router.grid import RoutingGrid, BackendType

        grid = RoutingGrid.__new__(RoutingGrid)
        grid._backend = np
        grid._backend_type = BackendType.CPU
        grid.cols = 3
        grid.rows = 1
        grid.num_layers = 1
        grid._present_cost_ema = None  # No EMA

        cell = MagicMock()
        cell.is_obstacle = False
        cell.usage_count = 2
        cell.history_cost = 1.0
        grid.grid = [[[MagicMock(), cell, MagicMock()]]]

        cost = grid.get_negotiated_cost(1, 0, 0, present_cost_factor=3.0)
        # factor * usage + history = 3.0 * 2 + 1.0 = 7.0
        assert cost == pytest.approx(7.0)
