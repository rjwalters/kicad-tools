"""Tests for router/layers.py module."""

import pytest

from kicad_tools.router.layers import Layer, LayerDefinition, LayerStack, LayerType


class TestLayerFromKicadName:
    """Tests for Layer.from_kicad_name() classmethod."""

    def test_from_kicad_name_f_cu(self):
        """Test converting F.Cu to Layer enum."""
        assert Layer.from_kicad_name("F.Cu") == Layer.F_CU
        assert Layer.from_kicad_name("F.Cu").value == 0

    def test_from_kicad_name_b_cu(self):
        """Test converting B.Cu to Layer enum."""
        assert Layer.from_kicad_name("B.Cu") == Layer.B_CU
        assert Layer.from_kicad_name("B.Cu").value == 5

    def test_from_kicad_name_in1_cu(self):
        """Test converting In1.Cu to Layer enum."""
        assert Layer.from_kicad_name("In1.Cu") == Layer.IN1_CU
        assert Layer.from_kicad_name("In1.Cu").value == 1

    def test_from_kicad_name_in2_cu(self):
        """Test converting In2.Cu to Layer enum."""
        assert Layer.from_kicad_name("In2.Cu") == Layer.IN2_CU
        assert Layer.from_kicad_name("In2.Cu").value == 2

    def test_from_kicad_name_in3_cu(self):
        """Test converting In3.Cu to Layer enum."""
        assert Layer.from_kicad_name("In3.Cu") == Layer.IN3_CU
        assert Layer.from_kicad_name("In3.Cu").value == 3

    def test_from_kicad_name_in4_cu(self):
        """Test converting In4.Cu to Layer enum."""
        assert Layer.from_kicad_name("In4.Cu") == Layer.IN4_CU
        assert Layer.from_kicad_name("In4.Cu").value == 4

    def test_from_kicad_name_all_layers(self):
        """Test that all Layer enum members can be retrieved by name."""
        for layer in Layer:
            result = Layer.from_kicad_name(layer.kicad_name)
            assert result == layer
            assert result.value == layer.value

    def test_from_kicad_name_unknown_raises_value_error(self):
        """Test that unknown layer names raise ValueError."""
        with pytest.raises(ValueError, match="Unknown KiCad copper layer name"):
            Layer.from_kicad_name("Unknown.Cu")

    def test_from_kicad_name_non_copper_raises_value_error(self):
        """Test that non-copper layer names raise ValueError."""
        with pytest.raises(ValueError):
            Layer.from_kicad_name("F.SilkS")

    def test_from_kicad_name_empty_raises_value_error(self):
        """Test that empty string raises ValueError."""
        with pytest.raises(ValueError):
            Layer.from_kicad_name("")

    def test_from_kicad_name_case_sensitive(self):
        """Test that layer name matching is case-sensitive."""
        with pytest.raises(ValueError):
            Layer.from_kicad_name("f.cu")  # lowercase should fail
        with pytest.raises(ValueError):
            Layer.from_kicad_name("F.CU")  # wrong case should fail


class TestLayerDefinitionLayerEnum:
    """Tests for LayerDefinition.layer_enum property.

    This is critical for 4-layer boards where B.Cu has stack index 3
    but must return Layer.B_CU (value 5), not Layer.IN3_CU (value 3).
    """

    def test_layer_enum_f_cu_at_index_0(self):
        """Test F.Cu at index 0 returns Layer.F_CU."""
        layer_def = LayerDefinition("F.Cu", 0, LayerType.SIGNAL, is_outer=True)
        assert layer_def.layer_enum == Layer.F_CU
        assert layer_def.layer_enum.value == 0

    def test_layer_enum_b_cu_at_index_1_two_layer(self):
        """Test B.Cu at index 1 (2-layer stack) returns Layer.B_CU."""
        layer_def = LayerDefinition("B.Cu", 1, LayerType.SIGNAL, is_outer=True)
        assert layer_def.layer_enum == Layer.B_CU
        assert layer_def.layer_enum.value == 5

    def test_layer_enum_b_cu_at_index_3_four_layer(self):
        """Test B.Cu at index 3 (4-layer stack) returns Layer.B_CU.

        This was the original bug: index 3 was incorrectly returning
        Layer.IN3_CU (value 3) instead of Layer.B_CU (value 5).
        """
        layer_def = LayerDefinition("B.Cu", 3, LayerType.SIGNAL, is_outer=True)
        assert layer_def.layer_enum == Layer.B_CU
        assert layer_def.layer_enum.value == 5
        # Explicitly verify it's NOT the wrong layer
        assert layer_def.layer_enum != Layer.IN3_CU

    def test_layer_enum_b_cu_at_index_5_six_layer(self):
        """Test B.Cu at index 5 (6-layer stack) returns Layer.B_CU."""
        layer_def = LayerDefinition("B.Cu", 5, LayerType.SIGNAL, is_outer=True)
        assert layer_def.layer_enum == Layer.B_CU
        assert layer_def.layer_enum.value == 5

    def test_layer_enum_inner_layers(self):
        """Test inner layer definitions return correct Layer enums."""
        # In1.Cu at index 1
        in1 = LayerDefinition("In1.Cu", 1, LayerType.PLANE)
        assert in1.layer_enum == Layer.IN1_CU
        assert in1.layer_enum.value == 1

        # In2.Cu at index 2
        in2 = LayerDefinition("In2.Cu", 2, LayerType.PLANE)
        assert in2.layer_enum == Layer.IN2_CU
        assert in2.layer_enum.value == 2

    def test_layer_enum_maps_by_name_not_index(self):
        """Verify layer_enum maps by name, not by index."""
        # Create layer with mismatched name and index
        # B.Cu at index 3 should still return Layer.B_CU (value 5)
        layer_def = LayerDefinition("B.Cu", 3, LayerType.SIGNAL)
        assert layer_def.index == 3  # Index is 3
        assert layer_def.layer_enum.value == 5  # But enum value is 5
        assert layer_def.layer_enum == Layer.B_CU


class TestLayerStackLayerEnums:
    """Tests for layer_enum property across standard layer stack presets."""

    def test_two_layer_stack_enums(self):
        """Test 2-layer stack returns correct Layer enums."""
        stack = LayerStack.two_layer()
        assert len(stack.layers) == 2

        # F.Cu at index 0
        assert stack.layers[0].name == "F.Cu"
        assert stack.layers[0].index == 0
        assert stack.layers[0].layer_enum == Layer.F_CU

        # B.Cu at index 1
        assert stack.layers[1].name == "B.Cu"
        assert stack.layers[1].index == 1
        assert stack.layers[1].layer_enum == Layer.B_CU

    def test_four_layer_sig_gnd_pwr_sig_enums(self):
        """Test 4-layer SIG-GND-PWR-SIG stack returns correct Layer enums."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert len(stack.layers) == 4

        expected = [
            ("F.Cu", 0, Layer.F_CU),
            ("In1.Cu", 1, Layer.IN1_CU),
            ("In2.Cu", 2, Layer.IN2_CU),
            ("B.Cu", 3, Layer.B_CU),  # Critical: index 3 but Layer.B_CU (value 5)
        ]

        for layer_def, (name, index, expected_enum) in zip(stack.layers, expected, strict=True):
            assert layer_def.name == name
            assert layer_def.index == index
            assert layer_def.layer_enum == expected_enum

    def test_four_layer_sig_sig_gnd_pwr_enums(self):
        """Test 4-layer SIG-SIG-GND-PWR stack returns correct Layer enums."""
        stack = LayerStack.four_layer_sig_sig_gnd_pwr()
        assert len(stack.layers) == 4

        # B.Cu should still map to Layer.B_CU regardless of stack configuration
        b_cu = [l for l in stack.layers if l.name == "B.Cu"][0]
        assert b_cu.layer_enum == Layer.B_CU
        assert b_cu.layer_enum.value == 5

    def test_six_layer_stack_enums(self):
        """Test 6-layer stack returns correct Layer enums."""
        stack = LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        assert len(stack.layers) == 6

        expected = [
            ("F.Cu", 0, Layer.F_CU),
            ("In1.Cu", 1, Layer.IN1_CU),
            ("In2.Cu", 2, Layer.IN2_CU),
            ("In3.Cu", 3, Layer.IN3_CU),
            ("In4.Cu", 4, Layer.IN4_CU),
            ("B.Cu", 5, Layer.B_CU),
        ]

        for layer_def, (name, index, expected_enum) in zip(stack.layers, expected, strict=True):
            assert layer_def.name == name
            assert layer_def.index == index
            assert layer_def.layer_enum == expected_enum


class TestLayerKicadName:
    """Tests for Layer.kicad_name property."""

    def test_kicad_name_all_layers(self):
        """Test all Layer enum members have correct kicad_name."""
        expected = {
            Layer.F_CU: "F.Cu",
            Layer.IN1_CU: "In1.Cu",
            Layer.IN2_CU: "In2.Cu",
            Layer.IN3_CU: "In3.Cu",
            Layer.IN4_CU: "In4.Cu",
            Layer.B_CU: "B.Cu",
        }
        for layer, expected_name in expected.items():
            assert layer.kicad_name == expected_name

    def test_kicad_name_roundtrip(self):
        """Test that kicad_name and from_kicad_name are inverses."""
        for layer in Layer:
            roundtrip = Layer.from_kicad_name(layer.kicad_name)
            assert roundtrip == layer


class TestLayerIsOuter:
    """Tests for Layer.is_outer property."""

    def test_f_cu_is_outer(self):
        """Test F.Cu is an outer layer."""
        assert Layer.F_CU.is_outer is True

    def test_b_cu_is_outer(self):
        """Test B.Cu is an outer layer."""
        assert Layer.B_CU.is_outer is True

    def test_inner_layers_not_outer(self):
        """Test inner layers are not outer."""
        assert Layer.IN1_CU.is_outer is False
        assert Layer.IN2_CU.is_outer is False
        assert Layer.IN3_CU.is_outer is False
        assert Layer.IN4_CU.is_outer is False
