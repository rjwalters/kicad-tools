"""Tests for router/layers.py module."""

import pytest

from kicad_tools.exceptions import RoutingError
from kicad_tools.router.layers import (
    Layer,
    LayerDefinition,
    LayerStack,
    LayerType,
    ViaDefinition,
    ViaRules,
    ViaType,
)
from kicad_tools.router.rules import DesignRules


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

    def test_four_layer_all_signal_enums(self):
        """Test 4-layer ALL-SIG stack returns correct Layer enums."""
        stack = LayerStack.four_layer_all_signal()
        assert len(stack.layers) == 4

        expected = [
            ("F.Cu", 0, Layer.F_CU),
            ("In1.Cu", 1, Layer.IN1_CU),
            ("In2.Cu", 2, Layer.IN2_CU),
            ("B.Cu", 3, Layer.B_CU),
        ]

        for layer_def, (name, index, expected_enum) in zip(stack.layers, expected, strict=True):
            assert layer_def.name == name
            assert layer_def.index == index
            assert layer_def.layer_enum == expected_enum

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


class TestLayer:
    """Tests for Layer enum."""

    def test_layer_values(self):
        """Test layer values."""
        assert Layer.F_CU.value == 0
        assert Layer.B_CU.value == 5

    def test_layer_kicad_names(self):
        """Test KiCad layer names."""
        assert Layer.F_CU.kicad_name == "F.Cu"
        assert Layer.B_CU.kicad_name == "B.Cu"
        assert Layer.IN1_CU.kicad_name == "In1.Cu"

    def test_layer_is_outer(self):
        """Test outer layer detection."""
        assert Layer.F_CU.is_outer is True
        assert Layer.B_CU.is_outer is True
        assert Layer.IN1_CU.is_outer is False


class TestLayerType:
    """Tests for LayerType enum."""

    def test_layer_types(self):
        """Test layer type values."""
        assert LayerType.SIGNAL.value == "signal"
        assert LayerType.PLANE.value == "plane"
        assert LayerType.MIXED.value == "mixed"


class TestLayerDefinition:
    """Tests for LayerDefinition class."""

    def test_layer_definition_creation(self):
        """Test creating layer definition."""
        layer_def = LayerDefinition(
            name="F.Cu", index=0, layer_type=LayerType.SIGNAL, is_outer=True
        )
        assert layer_def.name == "F.Cu"
        assert layer_def.index == 0
        assert layer_def.is_outer is True

    def test_layer_definition_layer_enum(self):
        """Test getting layer enum."""
        layer_def = LayerDefinition("F.Cu", 0, LayerType.SIGNAL)
        assert layer_def.layer_enum == Layer.F_CU

    def test_layer_definition_is_routable(self):
        """Test routable check."""
        signal = LayerDefinition("F.Cu", 0, LayerType.SIGNAL)
        plane = LayerDefinition("In1.Cu", 1, LayerType.PLANE)
        mixed = LayerDefinition("B.Cu", 3, LayerType.MIXED)

        assert signal.is_routable is True
        assert plane.is_routable is False
        assert mixed.is_routable is True


class TestLayerStack:
    """Tests for LayerStack class."""

    def test_two_layer_stack(self):
        """Test 2-layer stack preset."""
        stack = LayerStack.two_layer()
        assert stack.num_layers == 2
        assert stack.name == "2-Layer"

    def test_four_layer_stack(self):
        """Test 4-layer stack preset."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert stack.num_layers == 4
        assert len(stack.signal_layers) == 2
        assert len(stack.plane_layers) == 2

    def test_four_layer_all_signal_stack(self):
        """Test 4-layer all-signal stack preset."""
        stack = LayerStack.four_layer_all_signal()
        assert stack.num_layers == 4
        assert stack.name == "4-Layer ALL-SIG"
        assert len(stack.signal_layers) == 4
        assert len(stack.plane_layers) == 0

    def test_six_layer_stack(self):
        """Test 6-layer stack preset."""
        stack = LayerStack.six_layer_sig_gnd_sig_sig_pwr_sig()
        assert stack.num_layers == 6
        assert len(stack.signal_layers) == 4

    def test_layer_stack_validation(self):
        """Test layer stack validation."""
        # Non-sequential indices should raise
        with pytest.raises(RoutingError, match="Invalid layer stack"):
            LayerStack(
                [
                    LayerDefinition("F.Cu", 0, LayerType.SIGNAL),
                    LayerDefinition("B.Cu", 5, LayerType.SIGNAL),  # Gap in indices
                ]
            )

    def test_get_layer(self):
        """Test getting layer by index."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer(0)
        assert layer is not None
        assert layer.name == "F.Cu"
        assert stack.get_layer(99) is None

    def test_get_layer_by_name(self):
        """Test getting layer by name."""
        stack = LayerStack.two_layer()
        layer = stack.get_layer_by_name("F.Cu")
        assert layer is not None
        assert layer.index == 0
        assert stack.get_layer_by_name("missing") is None

    def test_layer_enum_to_index(self):
        """Test mapping layer enum to index."""
        stack = LayerStack.two_layer()
        idx = stack.layer_enum_to_index(Layer.F_CU)
        assert idx == 0

    def test_index_to_layer_enum(self):
        """Test mapping index to layer enum."""
        stack = LayerStack.two_layer()
        layer = stack.index_to_layer_enum(0)
        assert layer == Layer.F_CU

    def test_get_routable_indices(self):
        """Test getting routable layer indices."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        indices = stack.get_routable_indices()
        assert 0 in indices  # F.Cu
        assert 3 in indices  # B.Cu
        assert 1 not in indices  # GND plane

    def test_is_plane_layer(self):
        """Test plane layer check."""
        stack = LayerStack.four_layer_sig_gnd_pwr_sig()
        assert stack.is_plane_layer(1) is True  # GND plane
        assert stack.is_plane_layer(0) is False  # Signal

    def test_layer_stack_repr(self):
        """Test layer stack string representation."""
        stack = LayerStack.two_layer()
        s = repr(stack)
        assert "LayerStack" in s
        assert "2-Layer" in s


class TestDetectLayerStack:
    """Tests for detect_layer_stack function."""

    def test_detect_2_layer_board(self):
        """Test detecting a 2-layer board."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (31 "B.Cu" signal)
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 2
        assert "2-Layer" in stack.name

    def test_detect_4_layer_board_with_planes(self):
        """Test detecting a 4-layer board with inner planes."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (1 "In1.Cu" signal)
                (2 "In2.Cu" signal)
                (31 "B.Cu" signal)
            )
            (zone
                (net 1)
                (net_name "GND")
                (layer "In1.Cu")
            )
            (zone
                (net 2)
                (net_name "+3V3")
                (layer "In2.Cu")
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 4
        assert "4-Layer" in stack.name
        # Inner layers should be planes
        assert len(stack.plane_layers) == 2
        assert len(stack.signal_layers) == 2

    def test_detect_4_layer_board_no_zones(self):
        """Test detecting a 4-layer board without zones."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = """
        (kicad_pcb
            (layers
                (0 "F.Cu" signal)
                (1 "In1.Cu" signal)
                (2 "In2.Cu" signal)
                (31 "B.Cu" signal)
            )
        )
        """
        stack = detect_layer_stack(pcb_text)
        assert stack.num_layers == 4
        # Without zones, should use signal configuration

    def test_detect_no_layers_fallback(self):
        """Test fallback when no layers section found."""
        from kicad_tools.router.io import detect_layer_stack

        pcb_text = "(kicad_pcb )"
        stack = detect_layer_stack(pcb_text)
        # Should fall back to 2-layer
        assert stack.num_layers == 2


class TestViaType:
    """Tests for ViaType enum."""

    def test_via_types(self):
        """Test via type values."""
        assert ViaType.THROUGH.value == "through"
        assert ViaType.BLIND_TOP.value == "blind_top"
        assert ViaType.BURIED.value == "buried"
        assert ViaType.MICRO.value == "micro"


class TestViaDefinition:
    """Tests for ViaDefinition class."""

    def test_via_definition_creation(self):
        """Test creating via definition."""
        via_def = ViaDefinition(
            via_type=ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15, start_layer=0, end_layer=5
        )
        assert via_def.drill_mm == 0.3
        assert via_def.annular_ring_mm == 0.15

    def test_via_diameter(self):
        """Test via diameter calculation."""
        via_def = ViaDefinition(ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15)
        assert via_def.diameter == 0.6  # 0.3 + 2*0.15

    def test_via_spans_layer(self):
        """Test layer spanning check."""
        via_def = ViaDefinition(
            ViaType.THROUGH, drill_mm=0.3, annular_ring_mm=0.15, start_layer=0, end_layer=3
        )
        assert via_def.spans_layer(0, 4) is True
        assert via_def.spans_layer(2, 4) is True
        assert via_def.spans_layer(5, 6) is False

    def test_via_blocks_layer(self):
        """Test via blocking check."""
        via_def = ViaDefinition(
            ViaType.THROUGH,
            drill_mm=0.3,
            annular_ring_mm=0.15,
            start_layer=0,
            end_layer=-1,  # -1 = bottom
        )
        assert via_def.blocks_layer(0, 4) is True
        assert via_def.blocks_layer(3, 4) is True


class TestViaRules:
    """Tests for ViaRules class."""

    def test_via_rules_defaults(self):
        """Test default via rules."""
        rules = ViaRules()
        assert rules.allow_blind is False
        assert rules.allow_buried is False
        assert rules.through_via is not None

    def test_via_rules_standard_2layer(self):
        """Test 2-layer via rules."""
        rules = ViaRules.standard_2layer()
        assert rules.through_via.start_layer == 0
        assert rules.through_via.end_layer == 1

    def test_via_rules_standard_4layer(self):
        """Test 4-layer via rules."""
        rules = ViaRules.standard_4layer()
        assert rules.through_via.end_layer == 3

    def test_via_rules_hdi(self):
        """Test HDI via rules."""
        rules = ViaRules.hdi_4layer()
        assert rules.allow_blind is True
        assert rules.allow_micro is True
        assert rules.blind_via is not None
        assert rules.micro_via is not None

    def test_get_available_vias(self):
        """Test getting available vias."""
        rules = ViaRules.standard_4layer()
        vias = rules.get_available_vias(4)
        assert len(vias) == 1  # Only through via

        rules_hdi = ViaRules.hdi_4layer()
        vias_hdi = rules_hdi.get_available_vias(4)
        assert len(vias_hdi) == 3  # Through, blind, micro

    def test_get_best_via(self):
        """Test getting best via for layer pair."""
        rules = ViaRules.hdi_4layer()
        best = rules.get_best_via(0, 3, 4)
        assert best is not None
        # Should get micro via (lowest cost) if it spans the layers
        # Actually micro only spans 0-1, so through via

    def test_get_best_via_no_match(self):
        """Test when no via spans the layers."""
        rules = ViaRules()
        rules.through_via = ViaDefinition(ViaType.THROUGH, 0.3, 0.15, start_layer=0, end_layer=1)
        best = rules.get_best_via(0, 5, 6)  # Request 0->5 but via only goes 0->1
        assert best is None


class TestFourLayerAllSignal:
    """Tests for the four_layer_all_signal() preset.

    Verifies that the 4-all stack makes all 4 copper layers routable
    signal layers, unlike the standard 4-layer (SIG-GND-PWR-SIG) which
    only provides 2 routable layers.
    """

    def test_all_layers_are_signal(self):
        """All 4 layers should have LayerType.SIGNAL."""
        stack = LayerStack.four_layer_all_signal()
        for layer in stack.layers:
            assert layer.layer_type == LayerType.SIGNAL, (
                f"{layer.name} should be SIGNAL, got {layer.layer_type}"
            )

    def test_no_plane_layers(self):
        """No layers should be PLANE or MIXED."""
        stack = LayerStack.four_layer_all_signal()
        assert len(stack.plane_layers) == 0

    def test_four_routable_layers(self):
        """All 4 layers should be routable."""
        stack = LayerStack.four_layer_all_signal()
        assert len(stack.signal_layers) == 4

    def test_routable_indices_returns_all_four(self):
        """get_routable_indices() must return [0, 1, 2, 3]."""
        stack = LayerStack.four_layer_all_signal()
        assert stack.get_routable_indices() == [0, 1, 2, 3]

    def test_outer_layers_correct(self):
        """F.Cu and B.Cu should be marked as outer layers."""
        stack = LayerStack.four_layer_all_signal()
        outer = stack.outer_layers
        assert len(outer) == 2
        assert outer[0].name == "F.Cu"
        assert outer[1].name == "B.Cu"

    def test_inner_layers_are_routable(self):
        """In1.Cu and In2.Cu should be inner routable layers."""
        stack = LayerStack.four_layer_all_signal()
        inner = stack.get_inner_layer_indices()
        assert inner == [1, 2]

    def test_no_plane_net_defined(self):
        """No layer should have a plane_net defined."""
        stack = LayerStack.four_layer_all_signal()
        for layer in stack.layers:
            assert layer.plane_net == "", (
                f"{layer.name} should have no plane_net, got '{layer.plane_net}'"
            )

    def test_standard_4layer_via_rules_work(self):
        """Standard 4-layer via rules should span all layers."""
        stack = LayerStack.four_layer_all_signal()
        rules = ViaRules.standard_4layer()
        # Through via should span 0 to 3
        assert rules.through_via.spans_layer(0, stack.num_layers)
        assert rules.through_via.spans_layer(1, stack.num_layers)
        assert rules.through_via.spans_layer(2, stack.num_layers)
        assert rules.through_via.spans_layer(3, stack.num_layers)

    def test_differs_from_sig_gnd_pwr_sig(self):
        """4-all must have more routable layers than standard 4-layer."""
        standard = LayerStack.four_layer_sig_gnd_pwr_sig()
        all_sig = LayerStack.four_layer_all_signal()
        assert len(all_sig.get_routable_indices()) > len(standard.get_routable_indices())
        # Standard has 2 routable layers, all-sig has 4
        assert standard.get_routable_indices() == [0, 3]
        assert all_sig.get_routable_indices() == [0, 1, 2, 3]

    def test_stack_name_and_description(self):
        """Verify name and description are set."""
        stack = LayerStack.four_layer_all_signal()
        assert stack.name == "4-Layer ALL-SIG"
        assert "signal" in stack.description.lower()

    def test_layer_names_match_kicad(self):
        """Layer names should match standard KiCad copper layer names."""
        stack = LayerStack.four_layer_all_signal()
        expected_names = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]
        actual_names = [layer.name for layer in stack.layers]
        assert actual_names == expected_names
