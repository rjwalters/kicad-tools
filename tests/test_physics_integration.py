"""Tests for physics module integration with router, SI analyzer, and DRC.

These tests verify that the physics module is properly integrated with:
- Router (impedance-controlled routing)
- Signal integrity analyzer (crosstalk calculations)
- DRC rules (impedance validation)
- Diagnosis engine (impedance guidance)
"""

import pytest

from kicad_tools.physics import Stackup, TransmissionLine


class TestRouterPhysicsIntegration:
    """Tests for physics integration in the router."""

    @pytest.fixture
    def stackup(self):
        """Create a standard 4-layer stackup for testing."""
        return Stackup.jlcpcb_4layer()

    @pytest.fixture
    def autorouter_with_physics(self, stackup):
        """Create an autorouter with physics enabled."""
        from kicad_tools.router.core import Autorouter

        return Autorouter(
            width=100.0,
            height=100.0,
            stackup=stackup,
            physics_enabled=True,
        )

    @pytest.fixture
    def autorouter_without_physics(self):
        """Create an autorouter without physics."""
        from kicad_tools.router.core import Autorouter

        return Autorouter(
            width=100.0,
            height=100.0,
            physics_enabled=False,
        )

    def test_physics_available_with_stackup(self, autorouter_with_physics):
        """Test that physics is available when stackup is provided."""
        assert autorouter_with_physics.physics_available is True

    def test_physics_not_available_without_stackup(self, autorouter_without_physics):
        """Test that physics is not available without stackup."""
        assert autorouter_without_physics.physics_available is False

    def test_get_width_for_impedance(self, autorouter_with_physics):
        """Test calculating trace width for target impedance."""
        width = autorouter_with_physics.get_width_for_impedance(50.0, "F.Cu")
        assert width is not None
        assert 0.1 < width < 1.0  # Reasonable range for 50Ω on FR4

    def test_get_width_for_impedance_no_physics(self, autorouter_without_physics):
        """Test that width calculation returns None without physics."""
        width = autorouter_without_physics.get_width_for_impedance(50.0, "F.Cu")
        assert width is None

    def test_get_impedance_layer_widths(self, autorouter_with_physics):
        """Test calculating widths for multiple layers."""
        widths = autorouter_with_physics.get_impedance_layer_widths(50.0)

        assert "F.Cu" in widths
        assert "B.Cu" in widths
        # Inner layers may or may not be present depending on stackup
        for layer, width in widths.items():
            assert 0.05 < width < 1.5  # Reasonable range

    def test_impedance_width_varies_by_layer(self, autorouter_with_physics):
        """Test that impedance widths vary by layer type."""
        widths = autorouter_with_physics.get_impedance_layer_widths(50.0)

        # Outer layers (microstrip) typically need different width than inner (stripline)
        if "F.Cu" in widths and "In1.Cu" in widths:
            # They should be different (microstrip vs stripline)
            assert widths["F.Cu"] != widths["In1.Cu"]


class TestCrosstalkRiskDataclass:
    """Tests for CrosstalkRisk dataclass."""

    @pytest.fixture
    def crosstalk_risk_class(self):
        """Import CrosstalkRisk, skipping if dependencies unavailable."""
        pytest.importorskip("yaml")
        from kicad_tools.optim.signal_integrity import CrosstalkRisk

        return CrosstalkRisk

    def test_crosstalk_risk_structure(self, crosstalk_risk_class):
        """Test CrosstalkRisk dataclass structure."""
        risk = crosstalk_risk_class(
            aggressor_net="CLK",
            victim_net="ADC_IN",
            parallel_length_mm=10.0,
            spacing_mm=0.5,
            coupling_coefficient=0.05,
            next_percent=2.5,
            fext_percent=1.5,
            risk_level="acceptable",
            suggestion=None,
            calculated=False,
        )

        assert "CLK" in str(risk)
        assert "ADC_IN" in str(risk)
        assert "estimated" in str(risk)  # Not calculated

    def test_crosstalk_risk_calculated_flag(self, crosstalk_risk_class):
        """Test calculated flag affects string representation."""
        risk_heuristic = crosstalk_risk_class(
            aggressor_net="NET1",
            victim_net="NET2",
            parallel_length_mm=5.0,
            spacing_mm=0.3,
            coupling_coefficient=0.1,
            next_percent=5.0,
            fext_percent=3.0,
            risk_level="marginal",
            suggestion="Increase spacing",
            calculated=False,
        )

        risk_calculated = crosstalk_risk_class(
            aggressor_net="NET1",
            victim_net="NET2",
            parallel_length_mm=5.0,
            spacing_mm=0.3,
            coupling_coefficient=0.1,
            next_percent=5.0,
            fext_percent=3.0,
            risk_level="marginal",
            suggestion="Increase spacing",
            calculated=True,
        )

        assert "estimated" in str(risk_heuristic)
        assert "calculated" in str(risk_calculated)


class TestImpedanceGuideDataclass:
    """Tests for ImpedanceGuide dataclass."""

    def test_impedance_guide_structure(self):
        """Test ImpedanceGuide dataclass structure."""
        from kicad_tools.reasoning.diagnosis import ImpedanceGuide

        guide = ImpedanceGuide(
            target_z0=50.0,
            layer_widths={"F.Cu": 0.25, "B.Cu": 0.25},
            recommended_layer="F.Cu",
            notes=["Use outer layers for easier inspection"],
        )

        prompt = guide.to_prompt()
        assert "50" in prompt
        assert "F.Cu" in prompt
        assert "0.25" in prompt or "0.250" in prompt

    def test_impedance_guide_empty_widths(self):
        """Test ImpedanceGuide with no layer widths."""
        from kicad_tools.reasoning.diagnosis import ImpedanceGuide

        guide = ImpedanceGuide(
            target_z0=75.0,
            layer_widths={},
            recommended_layer=None,
            notes=[],
        )

        prompt = guide.to_prompt()
        assert "75" in prompt


class TestNetImpedanceSpec:
    """Tests for NetImpedanceSpec pattern matching."""

    @pytest.fixture
    def net_impedance_spec_class(self):
        """Import NetImpedanceSpec, skipping if dependencies unavailable."""
        pytest.importorskip("yaml")
        from kicad_tools.validate.rules.impedance import NetImpedanceSpec

        return NetImpedanceSpec

    def test_usb_pattern_matching(self, net_impedance_spec_class):
        """Test USB net pattern matching."""
        usb_spec = net_impedance_spec_class(r"USB.*D[PM\+\-]?", target_zdiff=90.0)
        assert usb_spec.matches("USB_DP")
        assert usb_spec.matches("USB_DM")
        assert usb_spec.matches("USB1_D")
        assert not usb_spec.matches("VCC")
        assert not usb_spec.matches("GND")

    def test_clock_pattern_matching(self, net_impedance_spec_class):
        """Test clock net pattern matching."""
        clk_spec = net_impedance_spec_class(r".*CLK.*", target_z0=50.0)
        assert clk_spec.matches("MCLK")
        assert clk_spec.matches("SYSCLK")
        assert clk_spec.matches("CLK_50M")
        assert not clk_spec.matches("DATA")
        assert not clk_spec.matches("VCC")

    def test_spec_tolerance(self, net_impedance_spec_class):
        """Test default tolerance value."""
        spec = net_impedance_spec_class(r".*", target_z0=50.0)
        assert spec.tolerance_percent == 10.0

        spec_custom = net_impedance_spec_class(r".*", target_z0=50.0, tolerance_percent=5.0)
        assert spec_custom.tolerance_percent == 5.0


class TestPhysicsModuleIntegration:
    """Tests for overall physics module integration."""

    def test_stackup_to_transmission_line(self):
        """Test creating transmission line from stackup."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        # Calculate microstrip for outer layer
        result = tl.microstrip(0.2, "F.Cu")
        assert 30 < result.z0 < 100  # Reasonable impedance range

    def test_stackup_to_width_calculation(self):
        """Test width calculation from stackup."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width = tl.width_for_impedance(50.0, "F.Cu")
        # Verify by calculating impedance for that width
        result = tl.microstrip(width, "F.Cu")
        assert abs(result.z0 - 50.0) < 5.0  # Within 10% tolerance

    def test_different_stackups_different_widths(self):
        """Test that different stackups give different width requirements."""
        stackup_jlc = Stackup.jlcpcb_4layer()
        stackup_osh = Stackup.oshpark_4layer()

        tl_jlc = TransmissionLine(stackup_jlc)
        tl_osh = TransmissionLine(stackup_osh)

        width_jlc = tl_jlc.width_for_impedance(50.0, "F.Cu")
        width_osh = tl_osh.width_for_impedance(50.0, "F.Cu")

        # Different stackups should require different widths
        # (due to different prepreg thickness and Er)
        assert width_jlc != width_osh

    def test_microstrip_vs_stripline_impedance(self):
        """Test impedance difference between microstrip and stripline."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        width = 0.15  # mm

        # Outer layer (microstrip)
        z0_outer = tl.microstrip(width, "F.Cu").z0

        # Inner layer (stripline) - use different width as stripline typically
        # needs narrower trace for same impedance
        z0_inner = tl.stripline(width, "In1.Cu").z0

        # Both should give valid impedance values
        assert 20 < z0_outer < 150
        assert 20 < z0_inner < 150

    def test_transmission_line_attributes(self):
        """Test TransmissionLineResult attributes."""
        stackup = Stackup.jlcpcb_4layer()
        tl = TransmissionLine(stackup)

        result = tl.microstrip(0.2, "F.Cu")

        # Check required attributes exist
        assert hasattr(result, "z0")
        assert hasattr(result, "epsilon_eff")
        assert hasattr(result, "loss_db_per_m")  # Loss in dB per meter

        # Check reasonable values
        assert result.z0 > 0
        assert result.epsilon_eff > 1.0  # Should be between 1 and Er
        assert result.loss_db_per_m >= 0
