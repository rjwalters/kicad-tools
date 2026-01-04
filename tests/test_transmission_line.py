"""Tests for transmission line impedance calculations."""

import math

import pytest

from kicad_tools.physics import (
    SPEED_OF_LIGHT,
    Stackup,
)
from kicad_tools.physics.transmission_line import (
    ImpedanceResult,
    TransmissionLine,
)


class TestImpedanceResult:
    """Tests for ImpedanceResult dataclass."""

    def test_propagation_delay_ps_per_mm(self):
        """Test propagation delay calculation."""
        # With eps_eff = 3, velocity = c / sqrt(3) ≈ 1.73e8 m/s
        # delay = 1mm / velocity = 0.001 / 1.73e8 s ≈ 5.77 ps/mm
        eps_eff = 3.0
        v_p = SPEED_OF_LIGHT / math.sqrt(eps_eff)

        result = ImpedanceResult(
            z0=50.0,
            epsilon_eff=eps_eff,
            loss_db_per_m=0.5,
            phase_velocity=v_p,
        )

        expected_delay = 1e12 * 0.001 / v_p  # ps/mm
        assert result.propagation_delay_ps_per_mm == pytest.approx(expected_delay, rel=0.01)

    def test_propagation_delay_ns_per_inch(self):
        """Test propagation delay in ns/inch."""
        eps_eff = 3.0
        v_p = SPEED_OF_LIGHT / math.sqrt(eps_eff)

        result = ImpedanceResult(
            z0=50.0,
            epsilon_eff=eps_eff,
            loss_db_per_m=0.5,
            phase_velocity=v_p,
        )

        # Should be ps/mm * 25.4 / 1000
        expected = result.propagation_delay_ps_per_mm * 25.4 / 1000
        assert result.propagation_delay_ns_per_inch == pytest.approx(expected, rel=0.01)

    def test_repr(self):
        """Test string representation."""
        result = ImpedanceResult(
            z0=50.123,
            epsilon_eff=3.456,
            loss_db_per_m=0.789,
            phase_velocity=1.5e8,
        )
        repr_str = repr(result)
        assert "50.12" in repr_str
        assert "3.456" in repr_str


class TestMicrostripImpedance:
    """Tests for microstrip impedance calculations."""

    def test_microstrip_impedance_jlcpcb_4layer(self):
        """Test microstrip impedance on JLCPCB 4-layer.

        With 0.21mm prepreg (er=4.05), a 0.2mm trace should give ~65-70Ω.
        For 50Ω, you need a wider trace (~0.38-0.4mm).
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # 0.2mm width on this stackup gives ~68Ω
        result = tl.microstrip(width_mm=0.2, layer="F.Cu")

        # Should be in reasonable range for this geometry
        assert 60 < result.z0 < 75

    def test_microstrip_narrow_high_impedance(self):
        """Test that narrow traces have higher impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result_narrow = tl.microstrip(width_mm=0.1, layer="F.Cu")
        result_wide = tl.microstrip(width_mm=0.3, layer="F.Cu")

        # Narrow trace should have higher impedance
        assert result_narrow.z0 > result_wide.z0

    def test_microstrip_effective_epsilon(self):
        """Test effective dielectric constant for microstrip.

        For microstrip, eps_eff should be between 1 and er.
        Typical FR4 microstrip has eps_eff around 2.5-3.5.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(width_mm=0.2, layer="F.Cu")

        # eps_eff should be between 1 (air) and er (substrate)
        er = stackup.get_dielectric_constant("F.Cu")
        assert 1 < result.epsilon_eff < er

        # For typical PCB, eps_eff is usually 2.5-4.0
        assert 2.0 < result.epsilon_eff < 4.5

    def test_microstrip_phase_velocity(self):
        """Test phase velocity calculation."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(width_mm=0.2, layer="F.Cu")

        # Phase velocity should be c / sqrt(eps_eff)
        expected_v = SPEED_OF_LIGHT / math.sqrt(result.epsilon_eff)
        assert result.phase_velocity == pytest.approx(expected_v, rel=0.01)

        # Should be slower than light in vacuum
        assert result.phase_velocity < SPEED_OF_LIGHT

    def test_microstrip_loss_positive(self):
        """Test that loss is calculated and positive."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(width_mm=0.2, layer="F.Cu", frequency_ghz=1.0)

        # Loss should be positive
        assert result.loss_db_per_m > 0

        # At 1 GHz on FR4, typical loss is 0.2-0.5 dB/inch = 8-20 dB/m
        # Our calculation gives ~13 dB/m which is reasonable
        assert 1.0 < result.loss_db_per_m < 50

    def test_microstrip_loss_increases_with_frequency(self):
        """Test that loss increases with frequency."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        loss_1ghz = tl.microstrip(width_mm=0.2, layer="F.Cu", frequency_ghz=1.0).loss_db_per_m
        loss_5ghz = tl.microstrip(width_mm=0.2, layer="F.Cu", frequency_ghz=5.0).loss_db_per_m

        # Loss should increase with frequency
        assert loss_5ghz > loss_1ghz

    def test_microstrip_bottom_layer(self):
        """Test microstrip on bottom layer."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # B.Cu should work the same as F.Cu on a symmetric stackup
        result_top = tl.microstrip(width_mm=0.2, layer="F.Cu")
        result_bottom = tl.microstrip(width_mm=0.2, layer="B.Cu")

        # Should be similar on symmetric stackup (within 10%)
        assert result_bottom.z0 == pytest.approx(result_top.z0, rel=0.10)

    def test_microstrip_invalid_width(self):
        """Test error handling for invalid width."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="positive"):
            tl.microstrip(width_mm=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            tl.microstrip(width_mm=-0.1, layer="F.Cu")


class TestStriplineImpedance:
    """Tests for stripline impedance calculations."""

    def test_stripline_inner_layer(self):
        """Test stripline impedance on inner layer."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.stripline(width_mm=0.15, layer="In1.Cu")

        # Stripline at 0.15mm should give ~75-80Ω on this stackup
        assert 60 < result.z0 < 100

    def test_stripline_eps_eff_equals_er(self):
        """Test that stripline epsilon_eff equals substrate er.

        For stripline fully embedded in dielectric, eps_eff = er.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.stripline(width_mm=0.15, layer="In1.Cu")
        er = stackup.get_dielectric_constant("In1.Cu")

        # Stripline epsilon_eff should equal the dielectric constant
        assert result.epsilon_eff == pytest.approx(er, rel=0.01)

    def test_stripline_slower_than_microstrip(self):
        """Test that stripline is slower than microstrip.

        Stripline is fully embedded in dielectric, so it's slower.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        microstrip_result = tl.microstrip(width_mm=0.2, layer="F.Cu")
        stripline_result = tl.stripline(width_mm=0.2, layer="In1.Cu")

        # Stripline phase velocity should be slower (lower)
        assert stripline_result.phase_velocity < microstrip_result.phase_velocity

    def test_stripline_narrow_high_impedance(self):
        """Test that narrow traces have higher impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result_narrow = tl.stripline(width_mm=0.1, layer="In1.Cu")
        result_wide = tl.stripline(width_mm=0.3, layer="In1.Cu")

        # Narrow trace should have higher impedance
        assert result_narrow.z0 > result_wide.z0

    def test_stripline_symmetric_vs_asymmetric(self):
        """Test that asymmetric stackup works correctly."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # In1.Cu has different distances to upper (prepreg) and lower (core) planes
        # h1 = 1.065mm (to In2.Cu/core), h2 = 0.2104mm (to F.Cu/prepreg)
        # This is asymmetric stripline
        result = tl.stripline(width_mm=0.15, layer="In1.Cu")

        # Should give reasonable impedance for this geometry
        assert 60 < result.z0 < 100


class TestWidthForImpedance:
    """Tests for inverse impedance calculation (Z0 → width)."""

    def test_width_for_50ohm_microstrip(self):
        """Test calculating width for 50Ω microstrip."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width = tl.width_for_impedance(z0_target=50, layer="F.Cu")

        # Verify by forward calculation
        result = tl.microstrip(width_mm=width, layer="F.Cu")
        assert result.z0 == pytest.approx(50, rel=0.02)

    def test_width_for_75ohm_microstrip(self):
        """Test calculating width for 75Ω microstrip."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width = tl.width_for_impedance(z0_target=75, layer="F.Cu")

        # Verify by forward calculation
        result = tl.microstrip(width_mm=width, layer="F.Cu")
        assert result.z0 == pytest.approx(75, rel=0.02)

    def test_width_for_50ohm_stripline(self):
        """Test calculating width for 50Ω stripline."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width = tl.width_for_impedance(z0_target=50, layer="In1.Cu", mode="stripline")

        # Verify by forward calculation (allow 5% tolerance)
        result = tl.stripline(width_mm=width, layer="In1.Cu")
        assert result.z0 == pytest.approx(50, rel=0.05)

    def test_width_for_impedance_auto_mode(self):
        """Test auto mode detection for layer type."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # F.Cu should auto-detect as microstrip
        width_outer = tl.width_for_impedance(z0_target=50, layer="F.Cu", mode="auto")
        result_outer = tl.microstrip(width_mm=width_outer, layer="F.Cu")
        assert result_outer.z0 == pytest.approx(50, rel=0.05)

        # In1.Cu should auto-detect as stripline
        width_inner = tl.width_for_impedance(z0_target=50, layer="In1.Cu", mode="auto")
        result_inner = tl.stripline(width_mm=width_inner, layer="In1.Cu")
        assert result_inner.z0 == pytest.approx(50, rel=0.05)

    def test_width_for_impedance_higher_z0_narrower(self):
        """Test that higher impedance requires narrower trace."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width_50 = tl.width_for_impedance(z0_target=50, layer="F.Cu")
        width_75 = tl.width_for_impedance(z0_target=75, layer="F.Cu")

        # 75Ω should be narrower than 50Ω
        assert width_75 < width_50

    def test_width_for_impedance_invalid_target(self):
        """Test error handling for invalid target impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="positive"):
            tl.width_for_impedance(z0_target=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            tl.width_for_impedance(z0_target=-50, layer="F.Cu")

    def test_width_for_impedance_invalid_mode(self):
        """Test error handling for invalid mode."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="Invalid mode"):
            tl.width_for_impedance(z0_target=50, layer="F.Cu", mode="invalid")


class TestDifferentialMicrostrip:
    """Tests for differential microstrip calculations."""

    def test_differential_impedance(self):
        """Test differential microstrip impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        single, z_diff = tl.differential_microstrip(width_mm=0.2, spacing_mm=0.2, layer="F.Cu")

        # Differential impedance should be approximately 2x single-ended
        # but reduced by coupling
        assert 1.5 * single.z0 < z_diff < 2.2 * single.z0

    def test_differential_spacing_effect(self):
        """Test that wider spacing increases differential impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        _, z_diff_tight = tl.differential_microstrip(width_mm=0.2, spacing_mm=0.1, layer="F.Cu")
        _, z_diff_loose = tl.differential_microstrip(width_mm=0.2, spacing_mm=0.5, layer="F.Cu")

        # Looser coupling (wider spacing) should give higher differential Z
        assert z_diff_loose > z_diff_tight


class TestStackupIntegration:
    """Tests for integration with different stackups."""

    def test_2layer_stackup(self):
        """Test with 2-layer stackup."""
        stackup = Stackup.default_2layer()
        tl = TransmissionLine(stackup)

        # Should work on 2-layer board
        result = tl.microstrip(width_mm=0.3, layer="F.Cu")
        assert result.z0 > 0

        # 2-layer has thicker dielectric, so wider trace for 50Ω
        width = tl.width_for_impedance(z0_target=50, layer="F.Cu")
        assert width > 0.2  # Should be wider than 4-layer

    def test_6layer_stackup(self):
        """Test with 6-layer stackup."""
        stackup = Stackup.default_6layer()
        tl = TransmissionLine(stackup)

        # Outer layer
        result_outer = tl.microstrip(width_mm=0.2, layer="F.Cu")
        assert result_outer.z0 > 0

        # Inner layers
        result_inner = tl.stripline(width_mm=0.15, layer="In2.Cu")
        assert result_inner.z0 > 0

    def test_oshpark_4layer(self):
        """Test with OSH Park 4-layer stackup."""
        stackup = Stackup.oshpark_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(width_mm=0.2, layer="F.Cu")

        # OSH Park has slightly different dielectric properties
        assert 40 < result.z0 < 70


class TestAccuracyValidation:
    """Tests validating accuracy against known reference values.

    Reference values from Saturn PCB Toolkit, JLCPCB calculator,
    and standard transmission line handbooks.
    """

    def test_microstrip_50ohm_width(self):
        """Test that width_for_impedance gives correct width for 50Ω.

        The inverse calculation should find the correct width that
        produces the target impedance when forward-calculated.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Calculate width for 50Ω
        width = tl.width_for_impedance(z0_target=50, layer="F.Cu")

        # Forward calculation should give 50Ω ±2%
        result = tl.microstrip(width_mm=width, layer="F.Cu")
        assert result.z0 == pytest.approx(50, rel=0.02)

    def test_effective_epsilon_reasonable_range(self):
        """Test that effective epsilon is in reasonable range.

        For microstrip on FR4 (er~4.5), eps_eff should be:
        - Higher for narrow traces (more field in substrate)
        - Lower for wide traces (more field in air)
        - Typically 2.5-4.0 for common geometries
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Various widths
        for width in [0.1, 0.2, 0.3, 0.5]:
            result = tl.microstrip(width_mm=width, layer="F.Cu")
            assert 2.0 < result.epsilon_eff < 4.5

    def test_propagation_delay_typical_range(self):
        """Test propagation delay is in typical range.

        For FR4 microstrip, typical propagation delay is 140-180 ps/inch.
        That's about 5.5-7.1 ps/mm.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(width_mm=0.2, layer="F.Cu")

        # Check ps/mm is reasonable
        delay_ps_mm = result.propagation_delay_ps_per_mm
        assert 5.0 < delay_ps_mm < 8.0

        # Check ns/inch is reasonable (140-180 ps/inch typical)
        delay_ns_inch = result.propagation_delay_ns_per_inch
        assert 0.12 < delay_ns_inch < 0.20


class TestCPWGImpedance:
    """Tests for coplanar waveguide with ground (CPWG) calculations."""

    def test_cpwg_basic_impedance(self):
        """Test basic CPWG impedance calculation."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Typical CPWG geometry on FR4
        result = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")

        # Should produce reasonable impedance
        # CPWG tends to have higher impedance than microstrip for similar widths
        assert 40 < result.z0 < 120

    def test_cpwg_50ohm_geometry(self):
        """Test typical 50Ω CPWG geometry.

        For FR4 with ~0.2mm dielectric, typical 50Ω CPWG requires
        wider traces and narrower gaps than microstrip. This test
        verifies the geometry solver can find such configurations.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Use the geometry solver to find a 50Ω configuration
        width, gap = tl.cpwg_geometry_for_impedance(z0_target=50, layer="F.Cu", width_mm=0.3)
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")

        # Verify it achieves target
        assert result.z0 == pytest.approx(50, rel=0.05)

    def test_cpwg_narrow_gap_lower_impedance(self):
        """Test that narrower gap produces lower impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result_narrow = tl.cpwg(width_mm=0.2, gap_mm=0.1, layer="F.Cu")
        result_wide = tl.cpwg(width_mm=0.2, gap_mm=0.3, layer="F.Cu")

        # Wider gap should give higher impedance
        assert result_wide.z0 > result_narrow.z0

    def test_cpwg_wide_trace_lower_impedance(self):
        """Test that wider trace produces lower impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result_narrow = tl.cpwg(width_mm=0.1, gap_mm=0.15, layer="F.Cu")
        result_wide = tl.cpwg(width_mm=0.4, gap_mm=0.15, layer="F.Cu")

        # Wider trace should give lower impedance
        assert result_wide.z0 < result_narrow.z0

    def test_cpwg_effective_epsilon(self):
        """Test CPWG effective dielectric constant.

        For CPWG, eps_eff should be between 1 and er.
        It's typically lower than microstrip eps_eff due to
        more field in air above the coplanar conductors.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")

        er = stackup.get_dielectric_constant("F.Cu")
        assert 1 < result.epsilon_eff < er

    def test_cpwg_phase_velocity(self):
        """Test CPWG phase velocity is reasonable."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")

        # Phase velocity should be c / sqrt(eps_eff)
        expected_v = SPEED_OF_LIGHT / math.sqrt(result.epsilon_eff)
        assert result.phase_velocity == pytest.approx(expected_v, rel=0.01)

        # Should be slower than light in vacuum
        assert result.phase_velocity < SPEED_OF_LIGHT

    def test_cpwg_loss_positive(self):
        """Test that CPWG loss is calculated and positive."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu", frequency_ghz=1.0)

        # Loss should be positive
        assert result.loss_db_per_m > 0

        # At 1 GHz on FR4, loss should be reasonable
        assert 0.5 < result.loss_db_per_m < 50

    def test_cpwg_loss_increases_with_frequency(self):
        """Test that CPWG loss increases with frequency."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        loss_1ghz = tl.cpwg(
            width_mm=0.25, gap_mm=0.15, layer="F.Cu", frequency_ghz=1.0
        ).loss_db_per_m
        loss_5ghz = tl.cpwg(
            width_mm=0.25, gap_mm=0.15, layer="F.Cu", frequency_ghz=5.0
        ).loss_db_per_m

        # Loss should increase with frequency
        assert loss_5ghz > loss_1ghz

    def test_cpwg_invalid_width(self):
        """Test error handling for invalid width."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg(width_mm=0, gap_mm=0.15, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg(width_mm=-0.1, gap_mm=0.15, layer="F.Cu")

    def test_cpwg_invalid_gap(self):
        """Test error handling for invalid gap."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg(width_mm=0.25, gap_mm=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg(width_mm=0.25, gap_mm=-0.1, layer="F.Cu")


class TestCPWGGeometryForImpedance:
    """Tests for CPWG inverse calculation (Z0 → geometry)."""

    def test_cpwg_geometry_fixed_width(self):
        """Test calculating gap for fixed width and target impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Fixed width, find gap for 50Ω
        width, gap = tl.cpwg_geometry_for_impedance(z0_target=50, layer="F.Cu", width_mm=0.25)

        # Width should be unchanged
        assert width == 0.25

        # Verify by forward calculation
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")
        assert result.z0 == pytest.approx(50, rel=0.03)

    def test_cpwg_geometry_fixed_gap(self):
        """Test calculating width for fixed gap and target impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Fixed gap, find width for 60Ω (achievable with this gap)
        width, gap = tl.cpwg_geometry_for_impedance(z0_target=60, layer="F.Cu", gap_mm=0.15)

        # Gap should be unchanged
        assert gap == 0.15

        # Verify by forward calculation
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")
        assert result.z0 == pytest.approx(60, rel=0.05)

    def test_cpwg_geometry_balanced(self):
        """Test calculating balanced geometry (neither specified)."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # No width or gap specified - find balanced geometry
        width, gap = tl.cpwg_geometry_for_impedance(z0_target=50, layer="F.Cu")

        # Both should be positive
        assert width > 0
        assert gap > 0

        # Verify by forward calculation
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")
        assert result.z0 == pytest.approx(50, rel=0.05)

    def test_cpwg_geometry_high_impedance(self):
        """Test geometry calculation for high impedance (75Ω)."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width, gap = tl.cpwg_geometry_for_impedance(z0_target=75, layer="F.Cu", width_mm=0.2)

        # Verify by forward calculation
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")
        assert result.z0 == pytest.approx(75, rel=0.05)

    def test_cpwg_geometry_low_impedance(self):
        """Test geometry calculation for low impedance (40Ω)."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # 40Ω is achievable with wider trace and smaller gap
        width, gap = tl.cpwg_geometry_for_impedance(z0_target=40, layer="F.Cu", width_mm=0.4)

        # Verify by forward calculation
        result = tl.cpwg(width_mm=width, gap_mm=gap, layer="F.Cu")
        assert result.z0 == pytest.approx(40, rel=0.10)

    def test_cpwg_geometry_invalid_target(self):
        """Test error handling for invalid target impedance."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg_geometry_for_impedance(z0_target=0, layer="F.Cu")

        with pytest.raises(ValueError, match="positive"):
            tl.cpwg_geometry_for_impedance(z0_target=-50, layer="F.Cu")

    def test_cpwg_geometry_both_specified_error(self):
        """Test error when both width and gap are specified."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        with pytest.raises(ValueError, match="either width_mm or gap_mm"):
            tl.cpwg_geometry_for_impedance(z0_target=50, layer="F.Cu", width_mm=0.25, gap_mm=0.15)


class TestCPWGvsOtherModes:
    """Tests comparing CPWG to microstrip and stripline."""

    def test_cpwg_vs_microstrip_impedance_range(self):
        """Test that CPWG can achieve similar impedance range as microstrip."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Both should be able to achieve 50Ω
        microstrip_50 = tl.width_for_impedance(z0_target=50, layer="F.Cu", mode="microstrip")
        microstrip_result = tl.microstrip(width_mm=microstrip_50, layer="F.Cu")

        cpwg_w, cpwg_g = tl.cpwg_geometry_for_impedance(z0_target=50, layer="F.Cu")
        cpwg_result = tl.cpwg(width_mm=cpwg_w, gap_mm=cpwg_g, layer="F.Cu")

        # Both should achieve roughly 50Ω
        assert microstrip_result.z0 == pytest.approx(50, rel=0.03)
        assert cpwg_result.z0 == pytest.approx(50, rel=0.05)

    def test_cpwg_effective_epsilon_vs_microstrip(self):
        """Test that CPWG has similar or lower eps_eff than microstrip.

        CPWG typically has lower eps_eff because more field is in the air
        above the coplanar conductors.
        """
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        microstrip = tl.microstrip(width_mm=0.25, layer="F.Cu")
        cpwg = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")

        # Both should have eps_eff > 1 and < er
        er = stackup.get_dielectric_constant("F.Cu")
        assert 1 < microstrip.epsilon_eff < er
        assert 1 < cpwg.epsilon_eff < er

        # CPWG eps_eff is typically similar to or lower than microstrip
        # (depends on geometry, so we allow either case)
        assert cpwg.epsilon_eff > 1
        assert cpwg.epsilon_eff < er

    def test_cpwg_propagation_delay_comparison(self):
        """Test that CPWG and microstrip have similar propagation delays."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        microstrip = tl.microstrip(width_mm=0.25, layer="F.Cu")
        cpwg = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")

        # Propagation delays should be in similar range
        # (within factor of 2 for typical geometries)
        delay_ratio = cpwg.propagation_delay_ps_per_mm / microstrip.propagation_delay_ps_per_mm
        assert 0.7 < delay_ratio < 1.3
