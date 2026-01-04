"""Tests for physics stackup module."""

import pytest

from kicad_tools.physics import (
    COPPER_1OZ,
    COPPER_2OZ,
    COPPER_CONDUCTIVITY,
    COPPER_HALF_OZ,
    FR4_HIGH_TG,
    FR4_STANDARD,
    ROGERS_4350B,
    # Constants
    SPEED_OF_LIGHT,
    CopperWeight,
    # Materials
    LayerType,
    Stackup,
    StackupLayer,
    copper_thickness_from_oz,
    get_material,
    get_material_or_default,
)


class TestPhysicalConstants:
    """Tests for physical constants."""

    def test_speed_of_light(self):
        """Test speed of light constant."""
        assert SPEED_OF_LIGHT == 299792458  # m/s

    def test_copper_conductivity(self):
        """Test copper conductivity constant."""
        assert COPPER_CONDUCTIVITY == 5.8e7  # S/m


class TestCopperWeight:
    """Tests for copper weight calculations."""

    def test_half_oz_copper(self):
        """Test 0.5oz copper thickness."""
        assert COPPER_HALF_OZ.oz == 0.5
        assert COPPER_HALF_OZ.thickness_um == pytest.approx(17.5, rel=0.01)
        assert COPPER_HALF_OZ.thickness_mm == pytest.approx(0.0175, rel=0.01)

    def test_1oz_copper(self):
        """Test 1oz copper thickness."""
        assert COPPER_1OZ.oz == 1.0
        assert COPPER_1OZ.thickness_um == pytest.approx(35, rel=0.01)
        assert COPPER_1OZ.thickness_mm == pytest.approx(0.035, rel=0.01)

    def test_2oz_copper(self):
        """Test 2oz copper thickness."""
        assert COPPER_2OZ.oz == 2.0
        assert COPPER_2OZ.thickness_um == pytest.approx(70, rel=0.01)
        assert COPPER_2OZ.thickness_mm == pytest.approx(0.070, rel=0.01)

    def test_copper_thickness_from_oz(self):
        """Test copper thickness conversion function."""
        assert copper_thickness_from_oz(1.0) == pytest.approx(0.035, rel=0.01)
        assert copper_thickness_from_oz(0.5) == pytest.approx(0.0175, rel=0.01)
        assert copper_thickness_from_oz(2.0) == pytest.approx(0.070, rel=0.01)

    def test_custom_copper_weight(self):
        """Test creating custom copper weight."""
        custom = CopperWeight.from_oz(1.5)
        assert custom.oz == 1.5
        assert custom.thickness_um == pytest.approx(52.5, rel=0.01)


class TestDielectricMaterials:
    """Tests for dielectric material properties."""

    def test_fr4_standard(self):
        """Test standard FR4 properties."""
        assert FR4_STANDARD.name == "FR4"
        assert FR4_STANDARD.epsilon_r == pytest.approx(4.5, rel=0.1)
        assert FR4_STANDARD.loss_tangent == pytest.approx(0.02, rel=0.1)

    def test_fr4_high_tg(self):
        """Test high-Tg FR4 properties."""
        assert FR4_HIGH_TG.epsilon_r == pytest.approx(4.4, rel=0.1)
        assert FR4_HIGH_TG.loss_tangent < FR4_STANDARD.loss_tangent

    def test_rogers_4350b(self):
        """Test Rogers RO4350B properties."""
        assert ROGERS_4350B.epsilon_r == pytest.approx(3.48, rel=0.05)
        assert ROGERS_4350B.loss_tangent == pytest.approx(0.0037, rel=0.1)
        # Rogers should have lower loss than FR4
        assert ROGERS_4350B.loss_tangent < FR4_STANDARD.loss_tangent

    def test_get_material_case_insensitive(self):
        """Test material lookup is case-insensitive."""
        assert get_material("fr4") == FR4_STANDARD
        assert get_material("FR4") == FR4_STANDARD
        assert get_material("Fr4") == FR4_STANDARD

    def test_get_material_not_found(self):
        """Test material lookup returns None for unknown materials."""
        assert get_material("unknown_material") is None

    def test_get_material_or_default(self):
        """Test material lookup with default."""
        result = get_material_or_default("unknown")
        assert result == FR4_STANDARD

        result = get_material_or_default(None)
        assert result == FR4_STANDARD


class TestStackupLayer:
    """Tests for StackupLayer dataclass."""

    def test_copper_layer(self):
        """Test creating a copper layer."""
        layer = StackupLayer(
            name="F.Cu",
            layer_type=LayerType.COPPER,
            thickness_mm=0.035,
            material="copper",
            copper_weight_oz=1.0,
        )
        assert layer.is_copper is True
        assert layer.is_dielectric is False
        assert layer.is_signal_layer is True

    def test_dielectric_layer(self):
        """Test creating a dielectric layer."""
        layer = StackupLayer(
            name="prepreg 1",
            layer_type=LayerType.DIELECTRIC,
            thickness_mm=0.2,
            material="FR4",
            epsilon_r=4.5,
            loss_tangent=0.02,
        )
        assert layer.is_copper is False
        assert layer.is_dielectric is True
        assert layer.is_signal_layer is False

    def test_inner_layer_is_signal(self):
        """Test that inner copper layers are identified as signal layers."""
        layer = StackupLayer(
            name="In1.Cu",
            layer_type=LayerType.COPPER,
            thickness_mm=0.0175,
        )
        assert layer.is_signal_layer is True


class TestStackupPresets:
    """Tests for manufacturer stackup presets."""

    def test_default_2layer(self):
        """Test default 2-layer stackup."""
        stackup = Stackup.default_2layer()

        assert stackup.num_copper_layers == 2
        assert stackup.board_thickness_mm == pytest.approx(1.6, rel=0.1)
        assert len(stackup.layers) == 3  # F.Cu, core, B.Cu

        # Check layer order
        assert stackup.layers[0].name == "F.Cu"
        assert stackup.layers[1].name == "core"
        assert stackup.layers[2].name == "B.Cu"

        # Check copper thickness
        assert stackup.get_copper_thickness("F.Cu") == pytest.approx(0.035, rel=0.1)
        assert stackup.get_copper_thickness("B.Cu") == pytest.approx(0.035, rel=0.1)

    def test_default_2layer_custom_thickness(self):
        """Test 2-layer stackup with custom thickness."""
        stackup = Stackup.default_2layer(thickness_mm=1.0)
        assert stackup.board_thickness_mm == pytest.approx(1.0, rel=0.1)

    def test_jlcpcb_4layer(self):
        """Test JLCPCB 4-layer stackup."""
        stackup = Stackup.jlcpcb_4layer()

        assert stackup.num_copper_layers == 4
        assert stackup.board_thickness_mm == pytest.approx(1.6, rel=0.1)
        assert len(stackup.layers) == 7  # 4 copper + 3 dielectric

        # Check copper layer names
        copper_names = [l.name for l in stackup.copper_layers]
        assert copper_names == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]

        # Check outer layer copper weight (1oz)
        f_cu = stackup.get_layer("F.Cu")
        assert f_cu is not None
        assert f_cu.copper_weight_oz == 1.0

        # Check inner layer copper weight (0.5oz)
        in1_cu = stackup.get_layer("In1.Cu")
        assert in1_cu is not None
        assert in1_cu.copper_weight_oz == 0.5

        # Check dielectric constants
        prepreg1 = stackup.get_layer("prepreg 1")
        assert prepreg1 is not None
        assert prepreg1.epsilon_r == pytest.approx(4.05, rel=0.1)

    def test_oshpark_4layer(self):
        """Test OSH Park 4-layer stackup."""
        stackup = Stackup.oshpark_4layer()

        assert stackup.num_copper_layers == 4
        assert stackup.board_thickness_mm == pytest.approx(1.6, rel=0.1)
        assert stackup.copper_finish == "ENIG"

        # OSH Park uses FR408 which has lower loss
        prepreg1 = stackup.get_layer("prepreg 1")
        assert prepreg1 is not None
        assert prepreg1.loss_tangent < FR4_STANDARD.loss_tangent

    def test_default_6layer(self):
        """Test default 6-layer stackup."""
        stackup = Stackup.default_6layer()

        assert stackup.num_copper_layers == 6
        assert stackup.board_thickness_mm == pytest.approx(1.6, rel=0.1)

        # Check all copper layers exist
        copper_names = [l.name for l in stackup.copper_layers]
        assert "F.Cu" in copper_names
        assert "In1.Cu" in copper_names
        assert "In2.Cu" in copper_names
        assert "In3.Cu" in copper_names
        assert "In4.Cu" in copper_names
        assert "B.Cu" in copper_names


class TestStackupQueries:
    """Tests for stackup query methods."""

    def test_is_outer_layer(self):
        """Test identifying outer layers."""
        stackup = Stackup.jlcpcb_4layer()

        assert stackup.is_outer_layer("F.Cu") is True
        assert stackup.is_outer_layer("B.Cu") is True
        assert stackup.is_outer_layer("In1.Cu") is False
        assert stackup.is_outer_layer("In2.Cu") is False

    def test_get_dielectric_height_outer(self):
        """Test getting dielectric height for outer layer (microstrip)."""
        stackup = Stackup.jlcpcb_4layer()

        # F.Cu should have prepreg thickness to In1.Cu
        h = stackup.get_dielectric_height("F.Cu")
        assert h == pytest.approx(0.2104, rel=0.1)

    def test_get_dielectric_height_inner(self):
        """Test getting dielectric height for inner layer (stripline)."""
        stackup = Stackup.jlcpcb_4layer()

        # In1.Cu should return smaller of prepreg (0.21) or core (1.065)
        h = stackup.get_dielectric_height("In1.Cu")
        assert h == pytest.approx(0.2104, rel=0.1)  # prepreg is smaller

    def test_get_dielectric_constant_outer(self):
        """Test getting dielectric constant for outer layer."""
        stackup = Stackup.jlcpcb_4layer()

        er = stackup.get_dielectric_constant("F.Cu")
        assert er == pytest.approx(4.05, rel=0.1)  # prepreg er

    def test_get_dielectric_constant_inner(self):
        """Test getting dielectric constant for inner layer."""
        stackup = Stackup.jlcpcb_4layer()

        # In1.Cu is between prepreg (er=4.05) and core (er=4.6)
        er = stackup.get_dielectric_constant("In1.Cu")
        # Should be average
        expected = (4.05 + 4.6) / 2
        assert er == pytest.approx(expected, rel=0.1)

    def test_get_copper_thickness(self):
        """Test getting copper thickness."""
        stackup = Stackup.jlcpcb_4layer()

        # Outer layers are 1oz
        assert stackup.get_copper_thickness("F.Cu") == pytest.approx(0.035, rel=0.1)

        # Inner layers are 0.5oz
        assert stackup.get_copper_thickness("In1.Cu") == pytest.approx(0.0175, rel=0.1)

    def test_get_reference_plane_distance(self):
        """Test getting reference plane distance (alias for dielectric height)."""
        stackup = Stackup.jlcpcb_4layer()

        h = stackup.get_reference_plane_distance("F.Cu")
        assert h == stackup.get_dielectric_height("F.Cu")

    def test_get_loss_tangent(self):
        """Test getting loss tangent."""
        stackup = Stackup.jlcpcb_4layer()

        tan_d = stackup.get_loss_tangent("F.Cu")
        assert tan_d == pytest.approx(0.02, rel=0.1)

    def test_summary(self):
        """Test stackup summary."""
        stackup = Stackup.jlcpcb_4layer()

        summary = stackup.summary()
        assert summary["num_copper_layers"] == 4
        assert summary["board_thickness_mm"] == pytest.approx(1.6, rel=0.1)
        assert len(summary["layers"]) == 7


class TestStackupFromPCB:
    """Tests for parsing stackup from PCB files."""

    def test_from_pcb_no_stackup(self):
        """Test that PCB without explicit stackup gets default."""
        from kicad_tools.schema.pcb import PCB

        # Load a simple 2-layer board without stackup
        pcb = PCB.load("tests/fixtures/routing-diagnostic.kicad_pcb")
        stackup = Stackup.from_pcb(pcb)

        # Should get a 2-layer default
        assert stackup.num_copper_layers == 2
        assert stackup.board_thickness_mm == pytest.approx(1.6, rel=0.1)

    def test_repr(self):
        """Test string representation."""
        stackup = Stackup.jlcpcb_4layer()
        repr_str = repr(stackup)
        assert "4L" in repr_str
        assert "1.6mm" in repr_str
