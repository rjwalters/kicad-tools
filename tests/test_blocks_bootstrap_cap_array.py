"""Tests for BootstrapCapacitorArray block and create_bootstrap_capacitor_array factory."""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    BootstrapCapacitorArray,
    GateDriverBlock,
    create_bootstrap_capacitor_array,
)


@pytest.fixture
def mock_schematic():
    """Create mock schematic for tests.

    Same pattern as TestGateDriverBlockMocked in test_schematic_blocks.py.
    """
    sch = Mock()

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.ref = ref
        comp.value = args[0] if args else kwargs.get("value")
        comp.x = x
        comp.y = y
        comp.pin_position.side_effect = lambda name: {
            "1": (x, y - 5),
            "2": (x, y + 5),
        }.get(name, (x, y))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    sch.add_text = Mock()
    sch.wire_decoupling_cap = Mock()
    return sch


class TestBootstrapCapacitorArrayMocked:
    """Tests for BootstrapCapacitorArray with mocked schematic."""

    def test_default_3_phase(self, mock_schematic):
        """Default phases=3 creates 3 caps with default labels A/B/C, value 100nF."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0)

        assert block.phases == 3
        assert block.phase_labels == ["A", "B", "C"]
        assert len(block.caps) == 3
        # Verify each cap got the default value
        for cap in block.caps:
            assert cap.value == "100nF"
        # Verify component dict keys
        assert "C_BOOT_A" in block.components
        assert "C_BOOT_B" in block.components
        assert "C_BOOT_C" in block.components

    def test_phase_count_1(self, mock_schematic):
        """phases=1 creates exactly 1 cap, label A."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=1)

        assert block.phases == 1
        assert block.phase_labels == ["A"]
        assert len(block.caps) == 1
        assert "C_BOOT_A" in block.components

    def test_phase_count_2(self, mock_schematic):
        """phases=2 creates 2 caps, labels A, B."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=2)

        assert block.phases == 2
        assert block.phase_labels == ["A", "B"]
        assert len(block.caps) == 2
        assert "C_BOOT_A" in block.components
        assert "C_BOOT_B" in block.components

    def test_phase_count_6(self, mock_schematic):
        """phases=6 creates 6 caps with integer-style labels '0'..'5'."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=6)

        assert block.phases == 6
        assert block.phase_labels == ["0", "1", "2", "3", "4", "5"]
        assert len(block.caps) == 6
        for label in ["0", "1", "2", "3", "4", "5"]:
            assert f"C_BOOT_{label}" in block.components

    def test_custom_phase_labels(self, mock_schematic):
        """Custom phase_labels=['U','V','W'] yields C_BOOT_U/V/W keys."""
        block = create_bootstrap_capacitor_array(
            mock_schematic,
            x=0,
            y=0,
            phases=3,
            phase_labels=["U", "V", "W"],
        )

        assert block.phase_labels == ["U", "V", "W"]
        assert "C_BOOT_U" in block.components
        assert "C_BOOT_V" in block.components
        assert "C_BOOT_W" in block.components

    def test_custom_value(self, mock_schematic):
        """value='220nF' applied to all caps via add_symbol."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3, value="220nF")

        assert block.value == "220nF"
        # Verify all add_symbol calls received "220nF" as the value
        # (positional arg 4: symbol, x, y, ref, value)
        for call in mock_schematic.add_symbol.call_args_list:
            args, kwargs = call
            assert args[4] == "220nF"

    def test_cap_ref_start(self, mock_schematic):
        """cap_ref_start=12 yields refs C12, C13, C14."""
        create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3, cap_ref_start=12)

        # Pull the ref (positional arg 3) from each add_symbol call
        refs = [call.args[3] for call in mock_schematic.add_symbol.call_args_list]
        assert refs == ["C12", "C13", "C14"]

    def test_cap_ref_prefix(self, mock_schematic):
        """cap_ref_prefix='CB' yields refs CB1, CB2, CB3."""
        create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3, cap_ref_prefix="CB")

        refs = [call.args[3] for call in mock_schematic.add_symbol.call_args_list]
        assert refs == ["CB1", "CB2", "CB3"]

    def test_ports(self, mock_schematic):
        """Default 3-phase block exposes HIGH_A/B/C and PHASE_A/B/C ports."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0)

        for label in ["A", "B", "C"]:
            assert f"HIGH_{label}" in block.ports
            assert f"PHASE_{label}" in block.ports

    def test_phase_nets_validation(self, mock_schematic):
        """phase_nets length mismatch raises ValueError."""
        with pytest.raises(ValueError, match="phase_nets"):
            create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3, phase_nets=["X"])

    def test_high_nets_validation(self, mock_schematic):
        """high_nets length mismatch raises ValueError."""
        with pytest.raises(ValueError, match="high_nets"):
            create_bootstrap_capacitor_array(
                mock_schematic, x=0, y=0, phases=3, high_nets=["A", "B"]
            )

    def test_phase_labels_validation(self, mock_schematic):
        """phase_labels length mismatch raises ValueError."""
        with pytest.raises(ValueError, match="phase_labels"):
            create_bootstrap_capacitor_array(
                mock_schematic, x=0, y=0, phases=3, phase_labels=["A", "B"]
            )

    def test_invalid_phases(self, mock_schematic):
        """phases < 1 raises ValueError."""
        with pytest.raises(ValueError, match="phases"):
            create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=0)

    def test_cap_spacing(self, mock_schematic):
        """Caps are placed at x, x+spacing, x+2*spacing."""
        create_bootstrap_capacitor_array(mock_schematic, x=100, y=50, phases=3, cap_spacing=15)

        # Pull positional x argument (index 1) from each add_symbol call
        xs = [call.args[1] for call in mock_schematic.add_symbol.call_args_list]
        assert xs == [100, 115, 130]

        # All caps share the same y
        ys = [call.args[2] for call in mock_schematic.add_symbol.call_args_list]
        assert ys == [50, 50, 50]

    def test_high_nets_creates_labels(self, mock_schematic):
        """high_nets triggers add_label at each cap pin 1."""
        create_bootstrap_capacitor_array(
            mock_schematic,
            x=0,
            y=0,
            phases=3,
            high_nets=["BST_A", "BST_B", "BST_C"],
        )

        # Verify add_label was called with each net name
        label_names = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert "BST_A" in label_names
        assert "BST_B" in label_names
        assert "BST_C" in label_names

    def test_phase_nets_creates_labels(self, mock_schematic):
        """phase_nets triggers add_label at each cap pin 2."""
        create_bootstrap_capacitor_array(
            mock_schematic,
            x=0,
            y=0,
            phases=3,
            phase_nets=["PHASE_A", "PHASE_B", "PHASE_C"],
        )

        label_names = [call.args[0] for call in mock_schematic.add_label.call_args_list]
        assert "PHASE_A" in label_names
        assert "PHASE_B" in label_names
        assert "PHASE_C" in label_names

    def test_no_labels_when_nets_none(self, mock_schematic):
        """When neither high_nets nor phase_nets is provided, add_label is not called."""
        create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3)
        assert mock_schematic.add_label.call_count == 0

    def test_returns_bootstrap_array_instance(self, mock_schematic):
        """Factory returns a BootstrapCapacitorArray."""
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0)
        assert isinstance(block, BootstrapCapacitorArray)


class TestGateDriverBlockComposition:
    """Verify GateDriverBlock composes BootstrapCapacitorArray internally."""

    def test_gate_driver_uses_bootstrap_array(self, mock_schematic):
        """GateDriverBlock(bootstrap_caps='100nF') composes a BootstrapCapacitorArray.

        Back-compat: len(driver.bootstrap_caps) == 3 must still hold.
        """
        driver = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            driver_type="3-phase",
            value="DRV8301",
            bootstrap_caps="100nF",
        )

        # Composition: an internal _bootstrap_block attribute exists
        assert hasattr(driver, "_bootstrap_block")
        assert isinstance(driver._bootstrap_block, BootstrapCapacitorArray)

        # Back-compat with existing tests
        assert len(driver.bootstrap_caps) == 3

    def test_gate_driver_half_bridge_composition(self, mock_schematic):
        """Half-bridge gate driver composes a 1-phase BootstrapCapacitorArray."""
        driver = GateDriverBlock(mock_schematic, x=100, y=100, driver_type="half-bridge")

        assert hasattr(driver, "_bootstrap_block")
        assert isinstance(driver._bootstrap_block, BootstrapCapacitorArray)
        assert driver._bootstrap_block.phases == 1
        assert len(driver.bootstrap_caps) == 1
