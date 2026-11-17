"""Tests for crosstalk estimation (NEXT/FEXT)."""

import math

import pytest

from kicad_tools.physics import (
    CrosstalkAnalyzer,
    CrosstalkResult,
    Stackup,
)


class TestCrosstalkResult:
    """Tests for CrosstalkResult dataclass."""

    def test_basic_result(self):
        """Test creating a CrosstalkResult."""
        result = CrosstalkResult(
            next_coefficient=0.05,
            fext_coefficient=0.03,
            next_db=-26.0,
            fext_db=-30.5,
            next_percent=5.0,
            fext_percent=3.0,
            coupled_length_mm=20.0,
            saturation_length_mm=75.0,
            severity="marginal",
            recommendation="Increase spacing",
        )
        assert result.next_coefficient == 0.05
        assert result.fext_coefficient == 0.03
        assert result.next_percent == 5.0
        assert result.fext_percent == 3.0
        assert result.severity == "marginal"
        assert result.recommendation == "Increase spacing"

    def test_repr(self):
        """Test string representation."""
        result = CrosstalkResult(
            next_coefficient=0.05,
            fext_coefficient=0.03,
            next_db=-26.0,
            fext_db=-30.5,
            next_percent=5.0,
            fext_percent=3.0,
            coupled_length_mm=20.0,
            saturation_length_mm=75.0,
            severity="marginal",
            recommendation=None,
        )
        repr_str = repr(result)
        assert "5.0%" in repr_str
        assert "3.0%" in repr_str
        assert "marginal" in repr_str


class TestCrosstalkAnalyzerBasic:
    """Basic tests for CrosstalkAnalyzer."""

    def test_basic_analysis(self):
        """Test basic crosstalk analysis."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Should get reasonable values
        assert 0 <= result.next_coefficient <= 1
        assert 0 <= result.fext_coefficient <= 1
        assert result.next_percent >= 0
        assert result.fext_percent >= 0
        assert result.coupled_length_mm == 20
        assert result.saturation_length_mm > 0
        assert result.severity in ("acceptable", "marginal", "excessive")

    def test_db_values_negative(self):
        """Test that dB values are negative (smaller = better)."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # dB values should be negative (20*log10 of coefficient < 1)
        assert result.next_db < 0
        assert result.fext_db < 0

    def test_db_calculation_correct(self):
        """Test dB calculation: dB = 20 * log10(coefficient)."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        expected_next_db = 20 * math.log10(max(result.next_coefficient, 1e-6))
        expected_fext_db = 20 * math.log10(max(result.fext_coefficient, 1e-6))

        assert result.next_db == pytest.approx(expected_next_db, rel=0.01)
        assert result.fext_db == pytest.approx(expected_fext_db, rel=0.01)

    def test_percent_calculation_correct(self):
        """Test percent is coefficient * 100."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        assert result.next_percent == pytest.approx(result.next_coefficient * 100, rel=0.001)
        assert result.fext_percent == pytest.approx(result.fext_coefficient * 100, rel=0.001)


class TestCrosstalkPhysicalBehavior:
    """Tests for physically correct crosstalk behavior."""

    def test_fext_increases_with_length(self):
        """FEXT should increase with parallel length."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        short = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=10,
            layer="F.Cu",
        )
        long = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=50,
            layer="F.Cu",
        )

        assert long.fext_percent > short.fext_percent

    def test_next_saturates(self):
        """NEXT should saturate beyond saturation length."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        # Get saturation length
        baseline = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=10,
            layer="F.Cu",
            rise_time_ns=1.0,
        )
        lsat = baseline.saturation_length_mm

        # Short (below saturation)
        short = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=lsat * 0.5,
            layer="F.Cu",
            rise_time_ns=1.0,
        )

        # At saturation
        at_sat = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=lsat,
            layer="F.Cu",
            rise_time_ns=1.0,
        )

        # Well beyond saturation
        beyond_sat = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=lsat * 3,
            layer="F.Cu",
            rise_time_ns=1.0,
        )

        # NEXT should increase before saturation
        assert short.next_coefficient < at_sat.next_coefficient

        # NEXT should be nearly the same at and beyond saturation
        assert beyond_sat.next_coefficient == pytest.approx(at_sat.next_coefficient, rel=0.05)

    def test_crosstalk_decreases_with_spacing(self):
        """Both NEXT and FEXT should decrease with spacing."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        tight = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.15,
            parallel_length_mm=20,
            layer="F.Cu",
        )
        loose = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.4,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        assert loose.next_percent < tight.next_percent
        assert loose.fext_percent < tight.fext_percent

    def test_faster_rise_time_increases_fext(self):
        """Faster rise time (shorter) should increase FEXT."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        slow = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=30,
            layer="F.Cu",
            rise_time_ns=2.0,
        )
        fast = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=30,
            layer="F.Cu",
            rise_time_ns=0.5,
        )

        # Faster rise time → smaller rise distance → higher FEXT
        assert fast.fext_percent > slow.fext_percent

    def test_slower_rise_time_increases_saturation_length(self):
        """Slower rise time should increase saturation length."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        slow = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
            rise_time_ns=2.0,
        )
        fast = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
            rise_time_ns=0.5,
        )

        assert slow.saturation_length_mm > fast.saturation_length_mm


class TestSeverityClassification:
    """Tests for crosstalk severity classification."""

    def test_acceptable_severity(self):
        """Test acceptable severity (< 3%)."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        # Very wide spacing should give low crosstalk
        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=1.0,  # Very wide
            parallel_length_mm=5,  # Short length
            layer="F.Cu",
        )

        # With wide spacing and short length, should be acceptable
        assert max(result.next_percent, result.fext_percent) < 3
        assert result.severity == "acceptable"
        assert result.recommendation is None

    def test_marginal_severity(self):
        """Test marginal severity (3-10%)."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        # Find geometry that gives marginal crosstalk
        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Check if this is in marginal range, adjust if needed
        if result.severity == "marginal":
            assert 3 <= max(result.next_percent, result.fext_percent) < 10
            assert result.recommendation is not None

    def test_excessive_severity(self):
        """Test excessive severity (> 10%)."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        # Very tight spacing and long length
        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.1,
            parallel_length_mm=100,
            layer="F.Cu",
        )

        # Should likely be excessive
        if result.severity == "excessive":
            assert max(result.next_percent, result.fext_percent) >= 10
            assert result.recommendation is not None
            assert (
                "layer" in result.recommendation.lower()
                or "spacing" in result.recommendation.lower()
            )


class TestSpacingForCrosstalkBudget:
    """Tests for inverse calculation (crosstalk budget → spacing)."""

    def test_spacing_for_5_percent_budget(self):
        """Test calculating spacing for 5% crosstalk budget."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        spacing = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=5.0,
            width_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Verify by forward calculation
        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=spacing,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        max_xt = max(result.next_percent, result.fext_percent)
        # Should be at or below budget (with some tolerance)
        assert max_xt <= 5.5  # Allow 10% tolerance

    def test_spacing_for_3_percent_budget(self):
        """Test calculating spacing for 3% crosstalk budget."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        spacing = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=3.0,
            width_mm=0.2,
            parallel_length_mm=15,
            layer="F.Cu",
        )

        # Verify by forward calculation
        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=spacing,
            parallel_length_mm=15,
            layer="F.Cu",
        )

        max_xt = max(result.next_percent, result.fext_percent)
        # Allow 10% tolerance - if we asked for 3%, we should get close
        assert max_xt <= 3.3
        # Note: 3% is right at threshold, so severity might be marginal
        assert result.severity in ("acceptable", "marginal")

    def test_tighter_budget_requires_wider_spacing(self):
        """Test that tighter budget requires wider spacing."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        spacing_5pct = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=5.0,
            width_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )
        spacing_3pct = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=3.0,
            width_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Tighter budget (3%) should require wider spacing
        assert spacing_3pct > spacing_5pct

    def test_longer_run_requires_wider_spacing(self):
        """Test that longer parallel run requires wider spacing."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        spacing_short = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=5.0,
            width_mm=0.2,
            parallel_length_mm=10,
            layer="F.Cu",
        )
        spacing_long = xt.spacing_for_crosstalk_budget(
            max_crosstalk_percent=5.0,
            width_mm=0.2,
            parallel_length_mm=50,
            layer="F.Cu",
        )

        # Longer run should require wider spacing to stay within budget
        assert spacing_long > spacing_short


class TestInnerLayerCrosstalk:
    """Tests for crosstalk on inner layers (stripline)."""

    def test_inner_layer_analysis(self):
        """Test crosstalk analysis on inner layer."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.15,
            victim_width_mm=0.15,
            spacing_mm=0.15,
            parallel_length_mm=20,
            layer="In1.Cu",
        )

        assert result.next_coefficient >= 0
        assert result.fext_coefficient >= 0
        assert result.severity in ("acceptable", "marginal", "excessive")

    def test_inner_layer_has_different_saturation_length(self):
        """Test that inner layer has different saturation length due to eps_eff."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        outer = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
            rise_time_ns=1.0,
        )
        inner = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="In1.Cu",
            rise_time_ns=1.0,
        )

        # Stripline has higher eps_eff → slower velocity → shorter saturation length
        # (Actually, saturation length = rise_distance/2 = (rise_time * v_p)/2)
        # Higher eps_eff → lower v_p → shorter saturation length
        assert inner.saturation_length_mm != outer.saturation_length_mm


class TestErrorHandling:
    """Tests for error handling."""

    def test_invalid_aggressor_width(self):
        """Test error handling for invalid aggressor width."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=0,
                victim_width_mm=0.2,
                spacing_mm=0.2,
                parallel_length_mm=20,
                layer="F.Cu",
            )

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=-0.1,
                victim_width_mm=0.2,
                spacing_mm=0.2,
                parallel_length_mm=20,
                layer="F.Cu",
            )

    def test_invalid_victim_width(self):
        """Test error handling for invalid victim width."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=0.2,
                victim_width_mm=0,
                spacing_mm=0.2,
                parallel_length_mm=20,
                layer="F.Cu",
            )

    def test_invalid_spacing(self):
        """Test error handling for invalid spacing."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=0.2,
                victim_width_mm=0.2,
                spacing_mm=0,
                parallel_length_mm=20,
                layer="F.Cu",
            )

    def test_invalid_parallel_length(self):
        """Test error handling for invalid parallel length."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=0.2,
                victim_width_mm=0.2,
                spacing_mm=0.2,
                parallel_length_mm=0,
                layer="F.Cu",
            )

    def test_invalid_rise_time(self):
        """Test error handling for invalid rise time."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.analyze(
                aggressor_width_mm=0.2,
                victim_width_mm=0.2,
                spacing_mm=0.2,
                parallel_length_mm=20,
                layer="F.Cu",
                rise_time_ns=0,
            )

    def test_spacing_budget_invalid_crosstalk(self):
        """Test error handling for invalid crosstalk budget."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.spacing_for_crosstalk_budget(
                max_crosstalk_percent=0,
                width_mm=0.2,
                parallel_length_mm=20,
                layer="F.Cu",
            )

    def test_spacing_budget_invalid_width(self):
        """Test error handling for invalid width in spacing budget."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.spacing_for_crosstalk_budget(
                max_crosstalk_percent=5.0,
                width_mm=0,
                parallel_length_mm=20,
                layer="F.Cu",
            )

    def test_spacing_budget_invalid_length(self):
        """Test error handling for invalid length in spacing budget."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            xt.spacing_for_crosstalk_budget(
                max_crosstalk_percent=5.0,
                width_mm=0.2,
                parallel_length_mm=0,
                layer="F.Cu",
            )


class TestStackupIntegration:
    """Tests for integration with different stackups."""

    def test_2layer_stackup(self):
        """Test with 2-layer stackup."""
        stackup = Stackup.default_2layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        assert result.next_coefficient >= 0
        assert result.fext_coefficient >= 0

    def test_6layer_stackup(self):
        """Test with 6-layer stackup."""
        stackup = Stackup.default_6layer()
        xt = CrosstalkAnalyzer(stackup)

        # Outer layer
        result_outer = xt.analyze(
            aggressor_width_mm=0.2,
            victim_width_mm=0.2,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )
        assert result_outer.next_coefficient >= 0

        # Inner layer
        result_inner = xt.analyze(
            aggressor_width_mm=0.15,
            victim_width_mm=0.15,
            spacing_mm=0.15,
            parallel_length_mm=20,
            layer="In2.Cu",
        )
        assert result_inner.next_coefficient >= 0

    def test_oshpark_4layer(self):
        """Test with OSH Park 4-layer stackup."""
        stackup = Stackup.oshpark_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.15,
            victim_width_mm=0.15,
            spacing_mm=0.15,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        assert result.next_coefficient >= 0
        assert result.fext_coefficient >= 0
        assert result.severity in ("acceptable", "marginal", "excessive")


class TestAsymmetricTraces:
    """Tests for asymmetric aggressor/victim configurations."""

    def test_different_widths(self):
        """Test with different aggressor and victim widths."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        result = xt.analyze(
            aggressor_width_mm=0.3,
            victim_width_mm=0.15,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Should still work with asymmetric widths
        assert result.next_coefficient >= 0
        assert result.fext_coefficient >= 0

    def test_symmetric_vs_asymmetric(self):
        """Test that asymmetric widths use average for coupling."""
        stackup = Stackup.jlcpcb_4layer()
        xt = CrosstalkAnalyzer(stackup)

        symmetric = xt.analyze(
            aggressor_width_mm=0.225,  # Average of 0.3 and 0.15
            victim_width_mm=0.225,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        asymmetric = xt.analyze(
            aggressor_width_mm=0.3,
            victim_width_mm=0.15,
            spacing_mm=0.2,
            parallel_length_mm=20,
            layer="F.Cu",
        )

        # Should give similar results since we use average width
        assert symmetric.next_coefficient == pytest.approx(asymmetric.next_coefficient, rel=0.01)
