"""Tests for propagation delay and timing analysis."""

import pytest

from kicad_tools.physics import (
    SPEED_OF_LIGHT,
    Stackup,
)
from kicad_tools.physics.timing import (
    DifferentialPairSkew,
    PropagationResult,
    TimingAnalyzer,
    TimingBudget,
)


class TestPropagationResult:
    """Tests for PropagationResult dataclass."""

    def test_basic_properties(self):
        """Test basic PropagationResult creation."""
        result = PropagationResult(
            delay_ps_per_mm=6.0,
            delay_ns_per_inch=0.15,
            velocity_m_per_s=1.5e8,
            velocity_percent_c=50.0,
        )

        assert result.delay_ps_per_mm == 6.0
        assert result.delay_ns_per_inch == 0.15
        assert result.velocity_m_per_s == 1.5e8
        assert result.velocity_percent_c == 50.0
        assert result.total_delay_ns == 0.0
        assert result.trace_length_mm == 0.0

    def test_repr_without_total_delay(self):
        """Test string representation without total delay."""
        result = PropagationResult(
            delay_ps_per_mm=6.0,
            delay_ns_per_inch=0.15,
            velocity_m_per_s=1.5e8,
            velocity_percent_c=50.0,
        )
        repr_str = repr(result)
        assert "6.00ps/mm" in repr_str
        assert "50.0%c" in repr_str
        assert "total=" not in repr_str

    def test_repr_with_total_delay(self):
        """Test string representation with total delay."""
        result = PropagationResult(
            delay_ps_per_mm=6.0,
            delay_ns_per_inch=0.15,
            velocity_m_per_s=1.5e8,
            velocity_percent_c=50.0,
            total_delay_ns=0.3,
            trace_length_mm=50.0,
        )
        repr_str = repr(result)
        assert "total=0.300ns" in repr_str


class TestTimingBudget:
    """Tests for TimingBudget dataclass."""

    def test_basic_properties(self):
        """Test basic TimingBudget creation."""
        budget = TimingBudget(
            net_name="DATA0",
            trace_length_mm=45.0,
            propagation_delay_ns=0.27,
        )

        assert budget.net_name == "DATA0"
        assert budget.trace_length_mm == 45.0
        assert budget.propagation_delay_ns == 0.27
        assert budget.target_delay_ns is None
        assert budget.skew_ns is None
        assert budget.within_budget is True

    def test_with_skew_within_budget(self):
        """Test TimingBudget with skew within budget."""
        budget = TimingBudget(
            net_name="DATA0",
            trace_length_mm=45.0,
            propagation_delay_ns=0.27,
            target_delay_ns=0.28,
            skew_ns=-0.01,
            within_budget=True,
        )

        repr_str = repr(budget)
        assert "DATA0" in repr_str
        assert "OK" in repr_str

    def test_with_skew_exceeding_budget(self):
        """Test TimingBudget with skew exceeding budget."""
        budget = TimingBudget(
            net_name="DATA0",
            trace_length_mm=45.0,
            propagation_delay_ns=0.27,
            target_delay_ns=0.30,
            skew_ns=-0.03,
            within_budget=False,
        )

        repr_str = repr(budget)
        assert "FAIL" in repr_str


class TestDifferentialPairSkew:
    """Tests for DifferentialPairSkew dataclass."""

    def test_within_spec(self):
        """Test differential pair skew within spec."""
        skew = DifferentialPairSkew(
            positive_net="USB_D+",
            negative_net="USB_D-",
            p_delay_ns=0.312,
            n_delay_ns=0.310,
            skew_ps=2.0,
            max_skew_ps=10.0,
            within_spec=True,
        )

        assert skew.within_spec is True
        assert skew.p_longer is True
        assert skew.recommendation is None
        assert "OK" in repr(skew)

    def test_exceeds_spec(self):
        """Test differential pair skew exceeding spec."""
        skew = DifferentialPairSkew(
            positive_net="USB_D+",
            negative_net="USB_D-",
            p_delay_ns=0.330,
            n_delay_ns=0.310,
            skew_ps=20.0,
            max_skew_ps=10.0,
            within_spec=False,
        )

        assert skew.within_spec is False
        assert skew.p_longer is True
        assert skew.recommendation is not None
        assert "P" in skew.recommendation
        assert "FAIL" in repr(skew)

    def test_n_longer(self):
        """Test when negative net is longer."""
        skew = DifferentialPairSkew(
            positive_net="USB_D+",
            negative_net="USB_D-",
            p_delay_ns=0.310,
            n_delay_ns=0.330,
            skew_ps=20.0,
            max_skew_ps=10.0,
            within_spec=False,
        )

        assert skew.p_longer is False
        assert "N" in skew.recommendation


class TestTimingAnalyzerPropagationDelay:
    """Tests for TimingAnalyzer.propagation_delay()."""

    def test_fr4_propagation_delay(self):
        """Verify typical FR4 propagation delay.

        For FR4 microstrip, typical propagation delay is ~140-180 ps/inch
        or ~5.5-7.1 ps/mm. This is based on eps_eff around 3.0-3.5.
        """
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")

        # Check ps/mm is in typical range
        assert 5.0 < result.delay_ps_per_mm < 8.0

        # Check ns/inch is in typical range (~0.14-0.18 ns/inch)
        assert 0.12 < result.delay_ns_per_inch < 0.20

    def test_velocity_percent_of_c(self):
        """Test velocity as percentage of speed of light.

        For FR4 with eps_eff ~3.0-3.5, velocity should be ~53-58% of c.
        """
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")

        # Should be significantly slower than light
        assert 40 < result.velocity_percent_c < 70

        # Verify calculation: v = c / sqrt(eps_eff)
        expected_v = result.velocity_m_per_s
        expected_percent = (expected_v / SPEED_OF_LIGHT) * 100
        assert result.velocity_percent_c == pytest.approx(expected_percent, rel=0.01)

    def test_stripline_slower_than_microstrip(self):
        """Test that stripline has slower propagation than microstrip.

        Stripline is fully embedded in dielectric, so it should be slower.
        """
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        microstrip = timing.propagation_delay(width_mm=0.2, layer="F.Cu", mode="microstrip")
        stripline = timing.propagation_delay(width_mm=0.2, layer="In1.Cu", mode="stripline")

        # Stripline should have more delay (slower velocity)
        assert stripline.delay_ps_per_mm > microstrip.delay_ps_per_mm
        assert stripline.velocity_m_per_s < microstrip.velocity_m_per_s

    def test_auto_mode_detection(self):
        """Test automatic mode detection for layers."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # F.Cu should auto-detect as microstrip
        outer_auto = timing.propagation_delay(width_mm=0.2, layer="F.Cu", mode="auto")
        outer_explicit = timing.propagation_delay(width_mm=0.2, layer="F.Cu", mode="microstrip")
        assert outer_auto.delay_ps_per_mm == pytest.approx(outer_explicit.delay_ps_per_mm, rel=0.01)

        # In1.Cu should auto-detect as stripline
        inner_auto = timing.propagation_delay(width_mm=0.2, layer="In1.Cu", mode="auto")
        inner_explicit = timing.propagation_delay(width_mm=0.2, layer="In1.Cu", mode="stripline")
        assert inner_auto.delay_ps_per_mm == pytest.approx(inner_explicit.delay_ps_per_mm, rel=0.01)

    def test_invalid_width(self):
        """Test error handling for invalid width."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.propagation_delay(width_mm=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            timing.propagation_delay(width_mm=-0.1, layer="F.Cu")

    def test_invalid_mode(self):
        """Test error handling for invalid mode."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="Invalid mode"):
            timing.propagation_delay(width_mm=0.2, layer="F.Cu", mode="invalid")


class TestTimingAnalyzerAnalyzeTrace:
    """Tests for TimingAnalyzer.analyze_trace()."""

    def test_total_delay_calculation(self):
        """Test total delay calculation for a trace."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.analyze_trace(
            trace_length_mm=50.0,
            width_mm=0.2,
            layer="F.Cu",
        )

        # Verify total delay = delay_ps_per_mm * length / 1000
        expected_total = result.delay_ps_per_mm * 50.0 / 1000
        assert result.total_delay_ns == pytest.approx(expected_total, rel=0.01)
        assert result.trace_length_mm == 50.0

    def test_longer_trace_more_delay(self):
        """Test that longer traces have more delay."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        short = timing.analyze_trace(trace_length_mm=25.0, width_mm=0.2, layer="F.Cu")
        long = timing.analyze_trace(trace_length_mm=100.0, width_mm=0.2, layer="F.Cu")

        assert long.total_delay_ns > short.total_delay_ns
        # Should scale linearly
        assert long.total_delay_ns == pytest.approx(4 * short.total_delay_ns, rel=0.01)

    def test_typical_trace_delay(self):
        """Test delay for typical trace lengths.

        A 50mm trace on FR4 microstrip should have ~0.3ns delay
        (6 ps/mm * 50mm = 300ps = 0.3ns)
        """
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.analyze_trace(
            trace_length_mm=50.0,
            width_mm=0.2,
            layer="F.Cu",
        )

        # Should be around 0.25-0.35ns for 50mm
        assert 0.2 < result.total_delay_ns < 0.4

    def test_invalid_length(self):
        """Test error handling for invalid length."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.analyze_trace(trace_length_mm=0, width_mm=0.2, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            timing.analyze_trace(trace_length_mm=-10, width_mm=0.2, layer="F.Cu")


class TestTimingAnalyzerLengthForDelay:
    """Tests for TimingAnalyzer.length_for_delay()."""

    def test_roundtrip_calculation(self):
        """Test that analyze_trace and length_for_delay are inverses."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # First calculate delay for a known length
        trace = timing.analyze_trace(trace_length_mm=75.0, width_mm=0.2, layer="F.Cu")

        # Then calculate length for that delay
        length = timing.length_for_delay(
            target_delay_ns=trace.total_delay_ns,
            width_mm=0.2,
            layer="F.Cu",
        )

        # Should get back original length
        assert length == pytest.approx(75.0, rel=0.01)

    def test_typical_delay_to_length(self):
        """Test length calculation for typical delays."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # For ~6ps/mm, 0.5ns delay needs ~83mm
        length = timing.length_for_delay(
            target_delay_ns=0.5,
            width_mm=0.2,
            layer="F.Cu",
        )

        # Should be in reasonable range
        assert 60 < length < 100

    def test_longer_delay_longer_length(self):
        """Test that longer delays require longer traces."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        length_short = timing.length_for_delay(target_delay_ns=0.1, width_mm=0.2, layer="F.Cu")
        length_long = timing.length_for_delay(target_delay_ns=0.5, width_mm=0.2, layer="F.Cu")

        assert length_long > length_short
        # Should scale linearly
        assert length_long == pytest.approx(5 * length_short, rel=0.01)

    def test_invalid_delay(self):
        """Test error handling for invalid delay."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.length_for_delay(target_delay_ns=0, width_mm=0.2, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            timing.length_for_delay(target_delay_ns=-0.1, width_mm=0.2, layer="F.Cu")


class TestTimingAnalyzerLengthMatching:
    """Tests for TimingAnalyzer.analyze_length_matching()."""

    def test_basic_length_matching(self):
        """Test basic length matching analysis."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        nets = [
            {"name": "DATA0", "length_mm": 45.0},
            {"name": "DATA1", "length_mm": 46.0},
            {"name": "DATA2", "length_mm": 44.5},
        ]

        results = timing.analyze_length_matching(
            nets=nets,
            width_mm=0.2,
            layer="F.Cu",
            max_skew_ns=0.1,
        )

        assert len(results) == 3
        for r in results:
            assert r.target_delay_ns is not None
            assert r.skew_ns is not None

    def test_all_within_budget(self):
        """Test nets that are all within budget."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # Very similar lengths should all be within 100ps budget
        nets = [
            {"name": "DATA0", "length_mm": 45.0},
            {"name": "DATA1", "length_mm": 45.5},
            {"name": "DATA2", "length_mm": 45.2},
        ]

        results = timing.analyze_length_matching(
            nets=nets,
            width_mm=0.2,
            layer="F.Cu",
            max_skew_ns=0.1,
        )

        assert all(r.within_budget for r in results)

    def test_some_exceeding_budget(self):
        """Test nets where some exceed budget."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # Large length difference will cause skew
        nets = [
            {"name": "DATA0", "length_mm": 40.0},
            {"name": "DATA1", "length_mm": 45.0},
            {"name": "DATA2", "length_mm": 60.0},  # Much longer
        ]

        results = timing.analyze_length_matching(
            nets=nets,
            width_mm=0.2,
            layer="F.Cu",
            max_skew_ns=0.05,  # Tight tolerance
        )

        # At least one should be out of budget
        assert not all(r.within_budget for r in results)

    def test_empty_nets_list(self):
        """Test with empty nets list."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        results = timing.analyze_length_matching(
            nets=[],
            width_mm=0.2,
            layer="F.Cu",
        )

        assert results == []

    def test_target_delay_is_average(self):
        """Test that target delay is the average of all nets."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        nets = [
            {"name": "DATA0", "length_mm": 40.0},
            {"name": "DATA1", "length_mm": 50.0},
            {"name": "DATA2", "length_mm": 60.0},
        ]

        results = timing.analyze_length_matching(
            nets=nets,
            width_mm=0.2,
            layer="F.Cu",
        )

        # All should have same target (the average)
        targets = [r.target_delay_ns for r in results]
        assert all(t == targets[0] for t in targets)

        # Target should be average of individual delays
        avg_delay = sum(r.propagation_delay_ns for r in results) / len(results)
        assert results[0].target_delay_ns == pytest.approx(avg_delay, rel=0.01)


class TestTimingAnalyzerDifferentialPairSkew:
    """Tests for TimingAnalyzer.analyze_differential_pair_skew()."""

    def test_within_usb2_spec(self):
        """Test differential pair within USB 2.0 spec (10ps)."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # Very similar lengths
        result = timing.analyze_differential_pair_skew(
            positive_length_mm=52.0,
            negative_length_mm=52.1,
            width_mm=0.15,
            layer="F.Cu",
            max_skew_ps=10.0,
        )

        # 0.1mm difference at ~6ps/mm = ~0.6ps skew, well within 10ps
        assert result.within_spec is True
        assert result.skew_ps < 10.0

    def test_exceeds_spec(self):
        """Test differential pair exceeding spec."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        # Large length difference
        result = timing.analyze_differential_pair_skew(
            positive_length_mm=50.0,
            negative_length_mm=55.0,
            width_mm=0.15,
            layer="F.Cu",
            max_skew_ps=10.0,
        )

        # 5mm difference at ~6ps/mm = ~30ps skew, exceeds 10ps
        assert result.within_spec is False
        assert result.skew_ps > 10.0

    def test_custom_net_names(self):
        """Test custom net names."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.analyze_differential_pair_skew(
            positive_length_mm=50.0,
            negative_length_mm=50.0,
            width_mm=0.15,
            layer="F.Cu",
            positive_net="HDMI_TX0+",
            negative_net="HDMI_TX0-",
        )

        assert result.positive_net == "HDMI_TX0+"
        assert result.negative_net == "HDMI_TX0-"

    def test_invalid_lengths(self):
        """Test error handling for invalid lengths."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.analyze_differential_pair_skew(
                positive_length_mm=0,
                negative_length_mm=50.0,
                width_mm=0.15,
                layer="F.Cu",
            )

        with pytest.raises(ValueError, match="positive"):
            timing.analyze_differential_pair_skew(
                positive_length_mm=50.0,
                negative_length_mm=-10.0,
                width_mm=0.15,
                layer="F.Cu",
            )


class TestTimingAnalyzerLengthDifferenceForSkew:
    """Tests for TimingAnalyzer.length_difference_for_skew()."""

    def test_usb2_skew_budget(self):
        """Test length difference for USB 2.0 skew budget (10ps)."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        max_diff = timing.length_difference_for_skew(
            max_skew_ps=10.0,
            width_mm=0.15,
            layer="F.Cu",
        )

        # For ~6ps/mm delay, 10ps allows ~1.7mm difference
        assert 1.0 < max_diff < 3.0

    def test_pcie_skew_budget(self):
        """Test length difference for PCIe skew budget (5ps)."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        max_diff_10ps = timing.length_difference_for_skew(
            max_skew_ps=10.0, width_mm=0.15, layer="F.Cu"
        )
        max_diff_5ps = timing.length_difference_for_skew(
            max_skew_ps=5.0, width_mm=0.15, layer="F.Cu"
        )

        # 5ps budget should allow half the length difference
        assert max_diff_5ps == pytest.approx(max_diff_10ps / 2, rel=0.01)

    def test_invalid_skew(self):
        """Test error handling for invalid skew."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.length_difference_for_skew(max_skew_ps=0, width_mm=0.15, layer="F.Cu")


class TestTimingAnalyzerSerpentineParameters:
    """Tests for TimingAnalyzer.serpentine_parameters()."""

    def test_basic_serpentine_calculation(self):
        """Test basic serpentine parameter calculation."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        params = timing.serpentine_parameters(
            target_extra_delay_ns=0.2,
            width_mm=0.2,
            spacing_mm=0.3,
            layer="F.Cu",
        )

        assert "extra_length_mm" in params
        assert "meander_amplitude_mm" in params
        assert "meander_pitch_mm" in params
        assert "num_meanders" in params

        # All should be positive
        assert params["extra_length_mm"] > 0
        assert params["meander_amplitude_mm"] > 0
        assert params["meander_pitch_mm"] > 0
        assert params["num_meanders"] > 0

    def test_more_delay_more_meanders(self):
        """Test that more delay requires more meanders."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        params_small = timing.serpentine_parameters(
            target_extra_delay_ns=0.1,
            width_mm=0.2,
            spacing_mm=0.3,
            layer="F.Cu",
        )

        params_large = timing.serpentine_parameters(
            target_extra_delay_ns=0.5,
            width_mm=0.2,
            spacing_mm=0.3,
            layer="F.Cu",
        )

        assert params_large["extra_length_mm"] > params_small["extra_length_mm"]
        assert params_large["num_meanders"] > params_small["num_meanders"]

    def test_amplitude_respects_spacing(self):
        """Test that meander amplitude respects spacing requirements."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        params = timing.serpentine_parameters(
            target_extra_delay_ns=0.2,
            width_mm=0.2,
            spacing_mm=0.3,
            layer="F.Cu",
        )

        # Amplitude should be at least 3x trace width for decoupling
        assert params["meander_amplitude_mm"] >= 3 * 0.2

    def test_invalid_delay(self):
        """Test error handling for invalid delay."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.serpentine_parameters(
                target_extra_delay_ns=0,
                width_mm=0.2,
                spacing_mm=0.3,
                layer="F.Cu",
            )

    def test_invalid_spacing(self):
        """Test error handling for invalid spacing."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        with pytest.raises(ValueError, match="positive"):
            timing.serpentine_parameters(
                target_extra_delay_ns=0.2,
                width_mm=0.2,
                spacing_mm=0,
                layer="F.Cu",
            )


class TestTimingAnalyzerIntegration:
    """Integration tests with different stackups."""

    def test_2layer_stackup(self):
        """Test timing analysis with 2-layer stackup."""
        stackup = Stackup.default_2layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.3, layer="F.Cu")
        assert result.delay_ps_per_mm > 0
        assert result.velocity_percent_c > 0

    def test_6layer_stackup(self):
        """Test timing analysis with 6-layer stackup."""
        stackup = Stackup.default_6layer()
        timing = TimingAnalyzer(stackup)

        # Outer layer
        outer = timing.propagation_delay(width_mm=0.2, layer="F.Cu")
        assert outer.delay_ps_per_mm > 0

        # Inner layer
        inner = timing.propagation_delay(width_mm=0.15, layer="In2.Cu")
        assert inner.delay_ps_per_mm > 0

    def test_oshpark_stackup(self):
        """Test timing analysis with OSH Park stackup."""
        stackup = Stackup.oshpark_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")

        # OSH Park has slightly different properties, but should be similar
        assert 5.0 < result.delay_ps_per_mm < 8.0


class TestTimingAnalyzerAccuracy:
    """Tests validating accuracy against expected values."""

    def test_typical_fr4_delay(self):
        """Verify typical FR4 delay is in expected range.

        Industry typical values:
        - Microstrip: ~140-180 ps/inch (~5.5-7.1 ps/mm)
        - Stripline: ~160-200 ps/inch (~6.3-7.9 ps/mm)
        """
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        microstrip = timing.propagation_delay(width_mm=0.2, layer="F.Cu")
        assert 5.0 < microstrip.delay_ps_per_mm < 7.5

        stripline = timing.propagation_delay(width_mm=0.15, layer="In1.Cu")
        assert 6.0 < stripline.delay_ps_per_mm < 8.5

    def test_velocity_consistency(self):
        """Test that velocity and delay are consistent."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")

        # delay = distance / velocity
        # For 1mm: delay_ps = 1e12 * 0.001 / v_p
        expected_delay = 1e12 * 0.001 / result.velocity_m_per_s
        assert result.delay_ps_per_mm == pytest.approx(expected_delay, rel=0.01)

    def test_unit_conversion_consistency(self):
        """Test that unit conversions are consistent."""
        stackup = Stackup.jlcpcb_4layer()
        timing = TimingAnalyzer(stackup)

        result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")

        # ns/inch should equal ps/mm * 25.4 / 1000
        expected_ns_per_inch = result.delay_ps_per_mm * 25.4 / 1000
        assert result.delay_ns_per_inch == pytest.approx(expected_ns_per_inch, rel=0.01)
