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
        # Reflect the footprint kwarg (if any) onto the mock component
        # so tests can assert `.footprint` after construction -- mirrors
        # the real SymbolInstance.footprint attribute populated by
        # `Schematic.add_symbol`.  Empty string default matches the
        # production default for back-compat callers.  Without this hook,
        # ``cap.footprint`` resolves to a fresh ``Mock()`` auto-attr which
        # silently equals anything and lets bad assertions pass.
        comp.footprint = kwargs.get("footprint", "")
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

    def test_explicit_cap_footprint(self, mock_schematic):
        """Explicit cap_footprint sets .footprint on every bootstrap cap.

        Regression for issue #3017 -- bootstrap caps used to land in the
        schematic with footprint="" because BootstrapCapacitorArray didn't
        forward any footprint kwarg to add_symbol.  Mirrors PR #3016's
        ``test_gate_driver_explicit_bypass_cap_footprint``.
        """
        fp = "Capacitor_SMD:C_0805_2012Metric"
        block = create_bootstrap_capacitor_array(
            mock_schematic,
            x=0,
            y=0,
            phases=3,
            cap_footprint=fp,
        )

        assert len(block.caps) == 3
        for cap in block.caps:
            assert cap.footprint == fp

        # Every cap call to add_symbol must carry footprint=fp.
        assert len(mock_schematic.add_symbol.call_args_list) == 3
        for call in mock_schematic.add_symbol.call_args_list:
            assert call.kwargs.get("footprint") == fp

    def test_auto_footprint_forwarded(self, mock_schematic):
        """auto_footprint=True is forwarded to every bootstrap-cap add_symbol call."""
        block = create_bootstrap_capacitor_array(
            mock_schematic,
            x=0,
            y=0,
            phases=3,
            auto_footprint=True,
        )

        assert len(block.caps) == 3
        assert len(mock_schematic.add_symbol.call_args_list) == 3
        for call in mock_schematic.add_symbol.call_args_list:
            assert call.kwargs.get("auto_footprint") is True

    def test_default_back_compat(self, mock_schematic):
        """Default construction (no footprint kwargs) preserves back-compat.

        With no new kwargs supplied, add_symbol is called with
        auto_footprint=False and no explicit footprint kwarg (matches
        the pre-#3017 production behavior so existing callers see no
        change).
        """
        block = create_bootstrap_capacitor_array(mock_schematic, x=0, y=0, phases=3)

        assert len(block.caps) == 3
        for cap in block.caps:
            assert cap.footprint == ""

        assert len(mock_schematic.add_symbol.call_args_list) == 3
        for call in mock_schematic.add_symbol.call_args_list:
            # Default forwards auto_footprint=False, no explicit footprint kwarg.
            assert call.kwargs.get("auto_footprint") is False
            assert "footprint" not in call.kwargs


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

    def test_gate_driver_forwards_bootstrap_cap_footprint(self, mock_schematic):
        """``bootstrap_cap_footprint`` on GateDriverBlock flows to internal
        ``BootstrapCapacitorArray`` so its caps land with a real footprint.

        Regression for issue #3017 -- PR #3016 wired ``bypass_cap_footprint``
        / ``auto_footprint`` onto the bypass-cap loop but did not thread
        anything through to the internal ``BootstrapCapacitorArray``
        instantiation at ``motor.py:958-967``.  Asserting on every
        ``Device:C`` ``add_symbol`` call that targets a bootstrap-numbered
        ref (C1..C3 at the default ``cap_ref_start=1``) is the most direct
        way to confirm the forward without depending on call ordering.
        """
        fp = "Capacitor_SMD:C_0805_2012Metric"
        driver = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            driver_type="3-phase",
            value="DRV8301",
            bootstrap_caps="100nF",
            bootstrap_cap_footprint=fp,
        )

        # Three bootstrap caps must all carry the explicit footprint.
        assert len(driver.bootstrap_caps) == 3
        for cap in driver.bootstrap_caps:
            assert cap.footprint == fp

        # Every bootstrap cap call to add_symbol must carry footprint=fp.
        # Bootstrap caps use refs C{cap_ref_start}..C{cap_ref_start + phases - 1}
        # (default cap_ref_start=1 -> C1..C3).  Bypass caps follow at C4+ and
        # are addressed by the donor pattern in PR #3016.
        bootstrap_calls = [
            call for call in mock_schematic.add_symbol.call_args_list
            if call.args
            and call.args[0] == "Device:C"
            and call.args[3] in {"C1", "C2", "C3"}
        ]
        assert len(bootstrap_calls) == 3
        for call in bootstrap_calls:
            assert call.kwargs.get("footprint") == fp

    def test_gate_driver_bootstrap_footprint_falls_back_to_bypass(self, mock_schematic):
        """``bypass_cap_footprint`` alone propagates to bootstrap caps too.

        The fallback chain documented in ``GateDriverBlock.__init__``: when
        ``bootstrap_cap_footprint is None`` and ``bypass_cap_footprint`` is
        provided, the bootstrap array inherits the bypass footprint.  This
        matches the common board-05 reality where bootstrap and bypass caps
        share the same 0805 package.
        """
        fp = "Capacitor_SMD:C_0805_2012Metric"
        driver = GateDriverBlock(
            mock_schematic,
            x=100,
            y=100,
            driver_type="3-phase",
            value="DRV8301",
            bootstrap_caps="100nF",
            bypass_cap_footprint=fp,
            # bootstrap_cap_footprint omitted -> falls back to bypass_cap_footprint
        )

        assert len(driver.bootstrap_caps) == 3
        for cap in driver.bootstrap_caps:
            assert cap.footprint == fp
