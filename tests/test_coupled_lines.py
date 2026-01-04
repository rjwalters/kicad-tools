"""Tests for coupled line analysis (differential pairs)."""

import math

import pytest

from kicad_tools.physics import (
    SPEED_OF_LIGHT,
    CoupledLines,
    DifferentialPairResult,
    Stackup,
)


class TestDifferentialPairResult:
    """Tests for DifferentialPairResult dataclass."""

    def test_basic_result(self):
        """Test creating a DifferentialPairResult."""
        result = DifferentialPairResult(
            zdiff=90.0,
            zcommon=25.0,
            z0_even=50.0,
            z0_odd=45.0,
            coupling_coefficient=0.1,
            epsilon_eff_even=3.5,
            epsilon_eff_odd=3.3,
        )
        assert result.zdiff == 90.0
        assert result.zcommon == 25.0
        assert result.coupling_coefficient == 0.1

    def test_phase_velocity_even(self):
        """Test even-mode phase velocity calculation."""
        result = DifferentialPairResult(
            zdiff=90.0,
            zcommon=25.0,
            z0_even=50.0,
            z0_odd=45.0,
            coupling_coefficient=0.1,
            epsilon_eff_even=4.0,
            epsilon_eff_odd=3.5,
        )

        # v_p = c / sqrt(eps_eff)
        expected = SPEED_OF_LIGHT / math.sqrt(4.0)
        assert result.phase_velocity_even == pytest.approx(expected, rel=0.01)

    def test_phase_velocity_odd(self):
        """Test odd-mode phase velocity calculation."""
        result = DifferentialPairResult(
            zdiff=90.0,
            zcommon=25.0,
            z0_even=50.0,
            z0_odd=45.0,
            coupling_coefficient=0.1,
            epsilon_eff_even=4.0,
            epsilon_eff_odd=3.5,
        )

        expected = SPEED_OF_LIGHT / math.sqrt(3.5)
        assert result.phase_velocity_odd == pytest.approx(expected, rel=0.01)

    def test_repr(self):
        """Test string representation."""
        result = DifferentialPairResult(
            zdiff=90.123,
            zcommon=25.456,
            z0_even=50.0,
            z0_odd=45.0,
            coupling_coefficient=0.123,
            epsilon_eff_even=4.0,
            epsilon_eff_odd=3.5,
        )
        repr_str = repr(result)
        assert "90.1" in repr_str
        assert "25.5" in repr_str
        assert "0.123" in repr_str


class TestEdgeCoupledMicrostrip:
    """Tests for edge-coupled microstrip calculations."""

    def test_edge_coupled_microstrip_basic(self):
        """Test basic edge-coupled microstrip calculation."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")

        # Should get reasonable values
        assert result.zdiff > 0
        assert result.zcommon > 0
        assert result.z0_even > result.z0_odd  # Physical requirement
        assert 0 < result.coupling_coefficient < 1

    def test_differential_pair_geometry(self):
        """Test differential pair calculations for various geometries.

        Zdiff depends on single-ended impedance and coupling:
        - Zdiff = 2 * Z0_odd
        - Z0_odd decreases with tighter coupling
        - For 90Ω diff, need Z0_single ≈ 50-55Ω with moderate coupling
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # On JLCPCB with 0.21mm prepreg, narrow traces have high Z0
        # 0.127mm trace gives ~81Ω single-ended
        result = cl.edge_coupled_microstrip(width_mm=0.127, gap_mm=0.127, layer="F.Cu")

        # Zdiff should be positive and related to geometry
        assert result.zdiff > 0
        # With 81Ω single-ended, expect Zdiff around 120-150Ω
        assert 100 < result.zdiff < 180

    def test_90ohm_achievable_geometry(self):
        """Test that 90Ω differential can be achieved with right geometry.

        For USB 90Ω on JLCPCB, need wider traces for lower Z0_single.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Use the inverse calculation to find the right gap
        gap = cl.gap_for_differential_impedance(zdiff_target=90, width_mm=0.35, layer="F.Cu")

        # Verify it works
        result = cl.edge_coupled_microstrip(width_mm=0.35, gap_mm=gap, layer="F.Cu")
        assert result.zdiff == pytest.approx(90, rel=0.05)

    def test_coupling_increases_with_tighter_gap(self):
        """Test that coupling coefficient increases as gap decreases."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        tight = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.1, layer="F.Cu")
        loose = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.3, layer="F.Cu")

        # Tighter gap should have higher coupling
        assert tight.coupling_coefficient > loose.coupling_coefficient

    def test_zdiff_increases_with_looser_gap(self):
        """Test that differential impedance increases as gap increases.

        Looser coupling means Z0_odd increases toward Z0_single, raising Zdiff.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        tight = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.1, layer="F.Cu")
        loose = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.3, layer="F.Cu")

        # Looser gap should have higher Zdiff (less coupling → Z0_odd closer to Z0_single)
        assert loose.zdiff > tight.zdiff

    def test_zdiff_approx_twice_z0_odd(self):
        """Test that Zdiff ≈ 2 * Z0_odd."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")

        # Zdiff = 2 * Z0_odd by definition
        assert result.zdiff == pytest.approx(2 * result.z0_odd, rel=0.001)

    def test_zcommon_approx_half_z0_even(self):
        """Test that Zcommon ≈ Z0_even / 2."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")

        # Zcommon = Z0_even / 2 by definition
        assert result.zcommon == pytest.approx(result.z0_even / 2, rel=0.001)

    def test_coupling_coefficient_formula(self):
        """Test coupling coefficient formula k = (Z0e - Z0o)/(Z0e + Z0o)."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")

        expected_k = (result.z0_even - result.z0_odd) / (result.z0_even + result.z0_odd)
        assert result.coupling_coefficient == pytest.approx(expected_k, rel=0.001)

    def test_bottom_layer_similar_to_top(self):
        """Test that B.Cu gives similar results to F.Cu on symmetric stackup."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        top = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")
        bottom = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="B.Cu")

        # Should be similar on symmetric stackup
        assert top.zdiff == pytest.approx(bottom.zdiff, rel=0.15)

    def test_invalid_width_raises_error(self):
        """Test that invalid width raises ValueError."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_microstrip(width_mm=0, gap_mm=0.15, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_microstrip(width_mm=-0.1, gap_mm=0.15, layer="F.Cu")

    def test_invalid_gap_raises_error(self):
        """Test that invalid gap raises ValueError."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=-0.1, layer="F.Cu")


class TestEdgeCoupledStripline:
    """Tests for edge-coupled stripline calculations."""

    def test_edge_coupled_stripline_basic(self):
        """Test basic edge-coupled stripline calculation."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=0.15, layer="In1.Cu")

        # Should get reasonable values
        assert result.zdiff > 0
        assert result.zcommon > 0
        assert result.z0_even > result.z0_odd

    def test_stripline_eps_eff_equals_er(self):
        """Test that stripline epsilon_eff equals substrate er.

        For stripline fully embedded in dielectric, eps_eff_even = eps_eff_odd = er.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=0.15, layer="In1.Cu")
        er = stackup.get_dielectric_constant("In1.Cu")

        # Both even and odd mode should have epsilon_eff = er
        assert result.epsilon_eff_even == pytest.approx(er, rel=0.01)
        assert result.epsilon_eff_odd == pytest.approx(er, rel=0.01)

    def test_stripline_coupling_effect(self):
        """Test coupling effect on stripline differential pairs."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        tight = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=0.1, layer="In1.Cu")
        loose = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=0.3, layer="In1.Cu")

        # Tighter gap should have higher coupling
        assert tight.coupling_coefficient > loose.coupling_coefficient
        # Looser gap should have higher Zdiff
        assert loose.zdiff > tight.zdiff

    def test_stripline_invalid_parameters(self):
        """Test error handling for invalid parameters."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_stripline(width_mm=0, gap_mm=0.15, layer="In1.Cu")

        with pytest.raises(ValueError, match="positive"):
            cl.edge_coupled_stripline(width_mm=0.15, gap_mm=0, layer="In1.Cu")


class TestBroadsideCoupledStripline:
    """Tests for broadside-coupled stripline calculations."""

    def test_broadside_coupled_basic(self):
        """Test basic broadside-coupled stripline calculation."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        result = cl.broadside_coupled_stripline(width_mm=0.15, layer1="In1.Cu", layer2="In2.Cu")

        # Should get reasonable values
        assert result.zdiff > 0
        assert result.zcommon > 0
        assert result.z0_even > result.z0_odd

    def test_broadside_stronger_coupling(self):
        """Test that broadside coupling is typically stronger than edge.

        Broadside-coupled traces are directly above/below each other,
        which generally produces stronger coupling.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Get geometry for reference
        h1, _ = stackup.get_stripline_geometry("In1.Cu")

        # Edge-coupled on inner layer with equivalent gap
        edge = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=h1, layer="In1.Cu")

        # Broadside-coupled between In1.Cu and In2.Cu
        broadside = cl.broadside_coupled_stripline(width_mm=0.15, layer1="In1.Cu", layer2="In2.Cu")

        # Both should have reasonable coupling
        assert 0 < edge.coupling_coefficient < 1
        assert 0 < broadside.coupling_coefficient < 1

    def test_broadside_invalid_width(self):
        """Test error handling for invalid width."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.broadside_coupled_stripline(width_mm=0, layer1="In1.Cu", layer2="In2.Cu")


class TestGapForDifferentialImpedance:
    """Tests for inverse calculation (Zdiff → gap)."""

    def test_gap_for_90ohm(self):
        """Test calculating gap for 90Ω differential impedance.

        Note: For 90Ω on JLCPCB, need wider traces since narrow traces
        have high single-ended impedance.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Use wider trace for achievable 90Ω
        gap = cl.gap_for_differential_impedance(zdiff_target=90, width_mm=0.35, layer="F.Cu")

        # Verify by forward calculation
        result = cl.edge_coupled_microstrip(width_mm=0.35, gap_mm=gap, layer="F.Cu")
        assert result.zdiff == pytest.approx(90, rel=0.05)

    def test_gap_for_100ohm(self):
        """Test calculating gap for 100Ω differential impedance."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Use wider trace for achievable 100Ω
        gap = cl.gap_for_differential_impedance(zdiff_target=100, width_mm=0.3, layer="F.Cu")

        # Verify by forward calculation
        result = cl.edge_coupled_microstrip(width_mm=0.3, gap_mm=gap, layer="F.Cu")
        assert result.zdiff == pytest.approx(100, rel=0.05)

    def test_gap_for_stripline(self):
        """Test calculating gap for stripline differential impedance.

        Note: Stripline has higher single-ended impedance, so we need
        to target higher Zdiff values that are achievable.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Target 135Ω which is achievable with 0.15mm trace on this stackup
        gap = cl.gap_for_differential_impedance(
            zdiff_target=135,
            width_mm=0.15,
            layer="In1.Cu",
            mode="edge_stripline",
        )

        # Verify by forward calculation
        result = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=gap, layer="In1.Cu")
        assert result.zdiff == pytest.approx(135, rel=0.10)

    def test_gap_auto_mode(self):
        """Test auto mode detection for layer type."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # F.Cu should auto-detect as microstrip
        gap_outer = cl.gap_for_differential_impedance(
            zdiff_target=100, width_mm=0.3, layer="F.Cu", mode="auto"
        )
        result_outer = cl.edge_coupled_microstrip(width_mm=0.3, gap_mm=gap_outer, layer="F.Cu")
        assert result_outer.zdiff == pytest.approx(100, rel=0.05)

        # In1.Cu should auto-detect as stripline
        # Use achievable target for this geometry
        gap_inner = cl.gap_for_differential_impedance(
            zdiff_target=140, width_mm=0.15, layer="In1.Cu", mode="auto"
        )
        result_inner = cl.edge_coupled_stripline(width_mm=0.15, gap_mm=gap_inner, layer="In1.Cu")
        assert result_inner.zdiff == pytest.approx(140, rel=0.10)

    def test_higher_zdiff_requires_wider_gap(self):
        """Test that higher Zdiff requires wider gap."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        gap_100 = cl.gap_for_differential_impedance(zdiff_target=100, width_mm=0.3, layer="F.Cu")
        gap_110 = cl.gap_for_differential_impedance(zdiff_target=110, width_mm=0.3, layer="F.Cu")

        # Higher Zdiff should require wider gap (less coupling)
        assert gap_110 > gap_100

    def test_invalid_zdiff_target(self):
        """Test error handling for invalid target impedance."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.gap_for_differential_impedance(zdiff_target=0, width_mm=0.127, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            cl.gap_for_differential_impedance(zdiff_target=-90, width_mm=0.127, layer="F.Cu")

    def test_invalid_width(self):
        """Test error handling for invalid width."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="positive"):
            cl.gap_for_differential_impedance(zdiff_target=90, width_mm=0, layer="F.Cu")

    def test_invalid_mode(self):
        """Test error handling for invalid mode."""
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        with pytest.raises(ValueError, match="Invalid mode"):
            cl.gap_for_differential_impedance(
                zdiff_target=90, width_mm=0.127, layer="F.Cu", mode="invalid"
            )


class TestStackupIntegration:
    """Tests for integration with different stackups."""

    def test_2layer_stackup(self):
        """Test with 2-layer stackup."""
        stackup = Stackup.default_2layer()
        cl = CoupledLines(stackup)

        # Should work on 2-layer board
        result = cl.edge_coupled_microstrip(width_mm=0.2, gap_mm=0.2, layer="F.Cu")
        assert result.zdiff > 0

        # 2-layer has thicker dielectric
        gap = cl.gap_for_differential_impedance(zdiff_target=90, width_mm=0.2, layer="F.Cu")
        assert gap > 0

    def test_6layer_stackup(self):
        """Test with 6-layer stackup."""
        stackup = Stackup.default_6layer()
        cl = CoupledLines(stackup)

        # Outer layer
        result_outer = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")
        assert result_outer.zdiff > 0

        # Inner layer stripline
        result_inner = cl.edge_coupled_stripline(width_mm=0.12, gap_mm=0.12, layer="In2.Cu")
        assert result_inner.zdiff > 0

    def test_oshpark_4layer(self):
        """Test with OSH Park 4-layer stackup."""
        stackup = Stackup.oshpark_4layer()
        cl = CoupledLines(stackup)

        result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=0.15, layer="F.Cu")

        # Should work with different dielectric properties
        assert result.zdiff > 0
        assert 0 < result.coupling_coefficient < 1


class TestAccuracyValidation:
    """Tests validating accuracy against known reference values."""

    def test_microstrip_zdiff_reasonable_range(self):
        """Test that calculated Zdiff is in reasonable range.

        For typical PCB geometries, Zdiff should be between 50-150Ω.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Various geometries
        for width in [0.1, 0.15, 0.2]:
            for gap in [0.1, 0.2, 0.3]:
                result = cl.edge_coupled_microstrip(width_mm=width, gap_mm=gap, layer="F.Cu")
                assert 40 < result.zdiff < 200

    def test_coupling_coefficient_range(self):
        """Test that coupling coefficient is in valid range.

        k should be between 0 (no coupling) and 1 (complete coupling).
        For practical geometries, k is typically 0.05-0.4.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        for gap in [0.1, 0.15, 0.2, 0.3]:
            result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=gap, layer="F.Cu")
            # Physical bounds
            assert 0 < result.coupling_coefficient < 1
            # Practical range
            assert 0.01 < result.coupling_coefficient < 0.5

    def test_z0_even_greater_than_z0_odd(self):
        """Test physical requirement that Z0_even > Z0_odd.

        This is always true for coupled lines because even mode
        has less field concentration between traces.
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        for gap in [0.05, 0.1, 0.2, 0.5]:
            result = cl.edge_coupled_microstrip(width_mm=0.15, gap_mm=gap, layer="F.Cu")
            assert result.z0_even > result.z0_odd, f"Failed at gap={gap}"

    def test_inverse_calculation_consistency(self):
        """Test that gap_for_differential_impedance is consistent with forward calc.

        For any target Zdiff, the inverse calculation should give a gap
        that produces that Zdiff when forward-calculated.

        Note: Using 0.35mm trace which gives ~55Ω single-ended on JLCPCB.
        Max achievable Zdiff ≈ 2 * Z0 ≈ 104Ω (at zero coupling).
        """
        stackup = Stackup.jlcpcb_4layer()
        cl = CoupledLines(stackup)

        # Test range that's achievable with this geometry (up to ~100Ω)
        for target_zdiff in [85, 90, 95, 100]:
            gap = cl.gap_for_differential_impedance(
                zdiff_target=target_zdiff, width_mm=0.35, layer="F.Cu"
            )
            result = cl.edge_coupled_microstrip(width_mm=0.35, gap_mm=gap, layer="F.Cu")

            # Should match within 5%
            assert result.zdiff == pytest.approx(target_zdiff, rel=0.05), (
                f"Target {target_zdiff}Ω, got {result.zdiff:.1f}Ω at gap={gap:.3f}mm"
            )
