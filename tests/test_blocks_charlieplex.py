"""Tests for the charlieplex matrix block factory."""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import (
    charlieplex_pairs_for_grid,
    create_charlieplex_matrix,
)
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# ---------------------------------------------------------------------------
# Mocked-schematic unit tests
# ---------------------------------------------------------------------------


def _mock_schematic() -> Mock:
    """Create a mocked Schematic exposing add_symbol/add_wire/add_global_label.

    Each ``add_symbol`` call returns a fresh symbol mock whose
    ``pin_position`` is keyed off the requested pin name and the requested
    (x, y) of the symbol — that way pin positions are unique per instance,
    which lets tests inspect distinct wires per LED/resistor.
    """
    sch = Mock()

    def make_symbol(*args, **kwargs):
        # Accept either positional or keyword form for x/y.
        if len(args) >= 3:
            x, y = args[1], args[2]
        else:
            x = kwargs.get("x", 0)
            y = kwargs.get("y", 0)
        sym = Mock()

        def pin_position(name):
            # LED pins: 1 = K (cathode), 2 = A (anode); offsets keep them
            # distinct from resistor pins on the same y.
            if name == "1":
                return (x - 2.54, y)
            if name == "2":
                return (x + 2.54, y)
            if name == "A":
                return (x + 2.54, y)
            if name == "K":
                return (x - 2.54, y)
            return (x, y)

        sym.pin_position.side_effect = pin_position
        return sym

    sch.add_symbol = Mock(side_effect=make_symbol)
    sch.add_wire = Mock()
    sch.add_global_label = Mock()
    return sch


# Board 02's exact charlieplex topology (3x3 grid, 4 pins).
BOARD_02_LED_PAIRS: list[tuple[int, int]] = [
    (0, 1),  # D1: A->B
    (1, 0),  # D2: B->A
    (0, 2),  # D3: A->C
    (2, 0),  # D4: C->A
    (0, 3),  # D5: A->D
    (3, 0),  # D6: D->A
    (1, 2),  # D7: B->C
    (2, 1),  # D8: C->B
    (1, 3),  # D9: B->D
]


class TestCharlieplexBoard02Topology:
    """Board 02 (3x3, 4-pin) reproducibility — the central correctness case."""

    def test_3x3_4pin_board02_topology(self):
        """Factory with board-02's pairs emits 9 LEDs, 4 resistors, 4 ports."""
        sch = _mock_schematic()
        cm = create_charlieplex_matrix(
            sch,
            x=152.4,
            y=50.8,
            pin_count=4,
            led_pairs=BOARD_02_LED_PAIRS,
            pin_labels=["A", "B", "C", "D"],
            resistor_value="330R",
            led_grid_cols=3,
        )

        # 9 LEDs (D1-D9), 4 resistors (R1-R4)
        led_refs = [f"D{i}" for i in range(1, 10)]
        r_refs = [f"R{i}" for i in range(1, 5)]
        for ref in led_refs + r_refs:
            assert ref in cm.components, f"missing component {ref}"
        assert len(cm.components) == 13

        # Ports: LINE_A..LINE_D
        for label in ("A", "B", "C", "D"):
            assert f"LINE_{label}" in cm.ports
        assert len(cm.ports) == 4

        # led_pairs/pin_labels stored on the block
        assert cm.led_pairs == BOARD_02_LED_PAIRS
        assert cm.pin_labels == ["A", "B", "C", "D"]
        assert cm.pin_count == 4

    def test_board02_global_labels_match_table(self):
        """Each LED's anode/cathode global labels match board 02's connection table."""
        sch = _mock_schematic()
        create_charlieplex_matrix(
            sch,
            x=152.4,
            y=50.8,
            pin_count=4,
            led_pairs=BOARD_02_LED_PAIRS,
            pin_labels=["A", "B", "C", "D"],
            led_grid_cols=3,
        )

        # Collect all global label names that were emitted.
        emitted_labels = [call.args[0] for call in sch.add_global_label.call_args_list]

        # Each LED contributes one anode-side NODE_<X> and one cathode-side
        # NODE_<Y> label.  The factory emits cathode label first (pin "1")
        # then anode label (pin "2") — so for each consecutive pair of
        # NODE_* labels we can assert equivalence with the board-02 table.
        label_letters = ["A", "B", "C", "D"]
        # Strip the resistor labels from the front (4 LINE_ + 4 NODE_).
        # Order of resistor-label emission per pin: LINE_<L> (left), NODE_<L> (right).
        expected_resistor_prefix = []
        for letter in label_letters:
            expected_resistor_prefix.append(f"LINE_{letter}")
            expected_resistor_prefix.append(f"NODE_{letter}")
        assert emitted_labels[: len(expected_resistor_prefix)] == expected_resistor_prefix

        # After the resistor labels come 9 LEDs * 2 NODE_* labels each.
        led_labels = emitted_labels[len(expected_resistor_prefix) :]
        assert len(led_labels) == 18

        for i, (anode_idx, cathode_idx) in enumerate(BOARD_02_LED_PAIRS):
            cathode_label = led_labels[2 * i]
            anode_label = led_labels[2 * i + 1]
            assert cathode_label == f"NODE_{label_letters[cathode_idx]}"
            assert anode_label == f"NODE_{label_letters[anode_idx]}"


class TestCharlieplexCapacityCases:
    """Other valid topologies."""

    def test_charlieplex_5x4_uses_all_20_pairs(self):
        """5 pins -> 20 LEDs (full N(N-1) population)."""
        sch = _mock_schematic()
        # Generate all 20 ordered pairs (i, j) for i != j in range(5).
        pairs = [(i, j) for i in range(5) for j in range(5) if i != j]
        assert len(pairs) == 20
        cm = create_charlieplex_matrix(
            sch,
            x=0,
            y=0,
            pin_count=5,
            led_pairs=pairs,
        )
        assert len([ref for ref in cm.components if ref.startswith("D")]) == 20
        assert len([ref for ref in cm.components if ref.startswith("R")]) == 5
        assert len(cm.ports) == 5

    def test_charlieplex_minimum_2pin(self):
        """Minimum charlieplex: 2 pins -> 2 LEDs."""
        sch = _mock_schematic()
        cm = create_charlieplex_matrix(
            sch,
            x=0,
            y=0,
            pin_count=2,
            led_pairs=[(0, 1), (1, 0)],
        )
        assert "D1" in cm.components and "D2" in cm.components
        assert "R1" in cm.components and "R2" in cm.components
        assert "LINE_A" in cm.ports and "LINE_B" in cm.ports


class TestCharlieplexValidation:
    """Invalid-input rejection."""

    def test_invalid_self_pair(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match="self-pair"):
            create_charlieplex_matrix(sch, x=0, y=0, pin_count=4, led_pairs=[(0, 0)])

    def test_invalid_index(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match="outside"):
            create_charlieplex_matrix(sch, x=0, y=0, pin_count=4, led_pairs=[(0, 5)])

    def test_negative_index(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match="outside"):
            create_charlieplex_matrix(sch, x=0, y=0, pin_count=4, led_pairs=[(-1, 2)])

    def test_duplicate_pair(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match="duplicate"):
            create_charlieplex_matrix(sch, x=0, y=0, pin_count=4, led_pairs=[(0, 1), (0, 1)])

    def test_pin_count_too_small(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match=r"pin_count must be >= 2"):
            create_charlieplex_matrix(sch, x=0, y=0, pin_count=1, led_pairs=[])

    def test_pin_labels_wrong_length(self):
        sch = _mock_schematic()
        with pytest.raises(ValueError, match="pin_labels"):
            create_charlieplex_matrix(
                sch,
                x=0,
                y=0,
                pin_count=4,
                led_pairs=[(0, 1)],
                pin_labels=["A", "B"],  # too short
            )


class TestCharlieplexPairsForGrid:
    """Tests for the convenience helper."""

    def test_3x3_grid_returns_9_pairs_4_pins(self):
        pairs, n = charlieplex_pairs_for_grid(3, 3)
        assert n == 4
        assert len(pairs) == 9
        # Must reproduce board 02's ordering verbatim.
        assert pairs == BOARD_02_LED_PAIRS

    def test_4x4_grid_needs_5_pins(self):
        # 4*(4-1) = 12 < 16, so smallest feasible N is 5.
        pairs, n = charlieplex_pairs_for_grid(4, 4)
        assert n == 5
        assert len(pairs) == 16
        # All pairs must be valid (i != j, in range, no duplicates)
        assert all(i != j for i, j in pairs)
        assert all(0 <= i < n and 0 <= j < n for i, j in pairs)
        assert len(set(pairs)) == 16

    def test_explicit_pin_count_too_small(self):
        with pytest.raises(ValueError, match="can drive at most"):
            charlieplex_pairs_for_grid(4, 4, pin_count=4)

    def test_explicit_pin_count_exact(self):
        pairs, n = charlieplex_pairs_for_grid(4, 4, pin_count=5)
        assert n == 5
        assert len(pairs) == 16

    def test_explicit_pin_count_larger_than_minimum(self):
        # With 6 pins we have 30 ordered-pair capacity; user can choose 16.
        pairs, n = charlieplex_pairs_for_grid(4, 4, pin_count=6)
        assert n == 6
        assert len(pairs) == 16

    def test_invalid_dimensions(self):
        with pytest.raises(ValueError):
            charlieplex_pairs_for_grid(0, 3)
        with pytest.raises(ValueError):
            charlieplex_pairs_for_grid(3, -1)


class TestCharlieplexCustomization:
    """Custom labels and ref offsets."""

    def test_custom_pin_labels(self):
        sch = _mock_schematic()
        cm = create_charlieplex_matrix(
            sch,
            x=0,
            y=0,
            pin_count=4,
            led_pairs=BOARD_02_LED_PAIRS,
            pin_labels=["X", "Y", "Z", "W"],
        )
        assert "LINE_X" in cm.ports
        assert "LINE_Y" in cm.ports
        assert "LINE_Z" in cm.ports
        assert "LINE_W" in cm.ports
        # Global labels emitted should include NODE_X..NODE_W.
        emitted = [c.args[0] for c in sch.add_global_label.call_args_list]
        assert "NODE_X" in emitted
        assert "NODE_W" in emitted
        # And not the default A/B/C/D.
        assert "NODE_A" not in emitted

    def test_resistor_ref_start_offset(self):
        sch = _mock_schematic()
        cm = create_charlieplex_matrix(
            sch,
            x=0,
            y=0,
            pin_count=4,
            led_pairs=[(0, 1)],
            resistor_ref_start=10,
        )
        # Resistors should be R10-R13, not R1-R4.
        for ref in ("R10", "R11", "R12", "R13"):
            assert ref in cm.components
        for ref in ("R1", "R2", "R3", "R4"):
            assert ref not in cm.components

    def test_led_ref_start_offset(self):
        sch = _mock_schematic()
        cm = create_charlieplex_matrix(
            sch,
            x=0,
            y=0,
            pin_count=2,
            led_pairs=[(0, 1), (1, 0)],
            led_ref_start=5,
        )
        assert "D5" in cm.components
        assert "D6" in cm.components
        assert "D1" not in cm.components


# ---------------------------------------------------------------------------
# Integration test: real Schematic
# ---------------------------------------------------------------------------


class TestCharlieplexIntegration:
    """End-to-end on a real Schematic."""

    def test_factory_realizes_into_real_schematic(self):
        """Factory writes valid components/wires/labels into a real Schematic."""
        sch = Schematic(title="Charlieplex test", snap_mode=SnapMode.AUTO, grid=1.27)
        cm = create_charlieplex_matrix(
            sch,
            x=152.4,
            y=50.8,
            pin_count=4,
            led_pairs=BOARD_02_LED_PAIRS,
            pin_labels=["A", "B", "C", "D"],
            resistor_value="330R",
            led_grid_cols=3,
        )

        # 9 LED + 4 resistor symbols
        assert len(sch.symbols) == 13

        # Wires: 4 resistors * 2 pin-stubs + 9 LEDs * 2 pin-stubs = 26
        assert len(sch.wires) == 26

        # Global labels: 4 LINE_ + 4 NODE_ from resistors + 18 NODE_ from LEDs = 26
        assert len(sch.global_labels) == 26

        # Block exposes 4 LINE_ ports and 13 components.
        assert set(cm.ports.keys()) == {"LINE_A", "LINE_B", "LINE_C", "LINE_D"}
        assert len(cm.components) == 13
