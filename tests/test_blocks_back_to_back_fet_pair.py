"""Tests for ``BackToBackFETPair`` (softstart rev B P1 — issue #3343).

The block represents two N-channel MOSFETs source-tied (common-source
/ Kelvin reference) with drains facing outward.  This is *not* a
half-bridge — body diodes block in both directions when off, which is
the load-bearing semantic distinction we test here.

Tests use a mocked ``Schematic`` so they exercise wiring + port
positions without producing real KiCad output.  Shape mirrors
``tests/test_blocks_half_bridge.py``.
"""

from unittest.mock import Mock

import pytest

from kicad_tools.schematic.blocks import BackToBackFETPair


@pytest.fixture
def mock_schematic():
    """Mock Schematic returning mock symbols with deterministic FET pins.

    ``Device:Q_NMOS`` convention: gate on the left, drain above, source
    below — the same convention used by ``test_blocks_half_bridge.py``.
    """
    sch = Mock()

    def create_mock_component(symbol, x, y, ref, *args, **kwargs):
        comp = Mock()
        comp.reference = ref
        comp.symbol = symbol
        comp.pin_position.side_effect = lambda name, _x=x, _y=y: {
            "D": (_x, _y - 10),
            "G": (_x - 10, _y),
            "S": (_x, _y + 10),
            "1": (_x, _y - 5),
            "2": (_x, _y + 5),
        }.get(name, (_x, _y))
        return comp

    sch.add_symbol = Mock(side_effect=create_mock_component)
    sch.add_wire = Mock()
    sch.add_junction = Mock()
    sch.add_label = Mock()
    return sch


class TestBackToBackFETPair:
    """Happy-path + topology invariants for the back-to-back FET pair."""

    def test_places_two_fets(self, mock_schematic):
        """Default construction places exactly two MOSFETs."""
        pair = BackToBackFETPair(
            mock_schematic,
            x=100,
            y=80,
            ref_a="Q1A",
            ref_b="Q1B",
        )
        # Two add_symbol calls for the FETs (any Kelvin-related junction
        # uses add_junction, not add_symbol).
        assert mock_schematic.add_symbol.call_count == 2

        # Both components must be registered with the requested refs.
        refs = [call.args[3] for call in mock_schematic.add_symbol.call_args_list]
        assert refs == ["Q1A", "Q1B"]

        # Component dictionary exposes both
        assert "Q_A" in pair.components
        assert "Q_B" in pair.components

    def test_default_part_is_irfb4110(self, mock_schematic):
        """Per softstart rev B BOM, default FET is IRFB4110."""
        BackToBackFETPair(mock_schematic, x=100, y=80)
        for call in mock_schematic.add_symbol.call_args_list:
            assert call.args[4] == "IRFB4110"

    def test_back_to_back_orientation_uses_180_rotation(self, mock_schematic):
        """FET B is rotated 180° so its source meets FET A's source.

        This is the load-bearing topology assertion: without the flip,
        the second FET's drain would meet the first's source (a
        half-bridge), and the body-diode protection vanishes.
        """
        BackToBackFETPair(mock_schematic, x=100, y=80)
        # add_symbol(symbol, x, y, ref, value, rotation=..., ...)
        rotations = [
            call.kwargs.get("rotation", 0) for call in mock_schematic.add_symbol.call_args_list
        ]
        assert rotations[0] == 0
        assert rotations[1] == 180

    def test_thermal_metadata_on_both_fets(self, mock_schematic):
        """Both FETs are tagged with thermal metadata for analyzer integration."""
        BackToBackFETPair(mock_schematic, x=100, y=80)
        for call in mock_schematic.add_symbol.call_args_list:
            props = call.kwargs.get("properties", {})
            assert "Thermal_Rth_JC" in props
            assert "Power_Dissipation" in props

    def test_exposes_required_ports(self, mock_schematic):
        """All five canonical ports must be present."""
        pair = BackToBackFETPair(mock_schematic, x=100, y=80)
        for port in ("DRAIN_A", "DRAIN_B", "GATE_A", "GATE_B", "SOURCE"):
            assert port in pair.ports, f"Missing port {port!r}"

    def test_kelvin_label_emits_when_requested(self, mock_schematic):
        """When ``kelvin_label`` is provided, exactly one label is added at SOURCE."""
        pair = BackToBackFETPair(
            mock_schematic,
            x=100,
            y=80,
            kelvin_label="SRC_POS",
        )
        labels = mock_schematic.add_label.call_args_list
        assert len(labels) == 1
        net_name, lx, ly = labels[0].args[0], labels[0].args[1], labels[0].args[2]
        assert net_name == "SRC_POS"
        # Label sits at the SOURCE port position.
        assert (lx, ly) == pair.port("SOURCE")

    def test_no_kelvin_label_by_default(self, mock_schematic):
        """Without ``kelvin_label`` no labels are emitted (back-compat)."""
        BackToBackFETPair(mock_schematic, x=100, y=80)
        assert mock_schematic.add_label.call_count == 0

    def test_source_junction_is_added(self, mock_schematic):
        """The common-source tie node always gets a junction marker."""
        BackToBackFETPair(mock_schematic, x=100, y=80)
        # At minimum the source-tie node + any Kelvin hint endpoint.
        assert mock_schematic.add_junction.call_count >= 1

    def test_metadata_includes_mosfet_value(self, mock_schematic):
        """Block metadata records the mosfet value for downstream tools."""
        pair = BackToBackFETPair(
            mock_schematic,
            x=100,
            y=80,
            mosfet_value="IRFB4110",
        )
        assert pair.mosfet_value == "IRFB4110"
        assert hasattr(pair, "kelvin_node")

    def test_all_emitted_wires_are_orthogonal(self):
        """Every wire segment must be horizontal or vertical (issue #3347).

        Diagonal wires are accepted by KiCad's schematic parser but cause
        unintended crossings, defeat the validator's orthogonal-only
        connectivity heuristics, and render as visually confusing slopes
        in the editor.  The block must therefore route the source tie
        (and any Kelvin hint stub) as Manhattan segments only.

        This test uses an asymmetric mock pin layout where the source
        sits offset in X from the symbol origin — mirroring the real
        ``Device:Q_NMOS`` geometry that produced the original diagonal
        bug.  Without the Z-route fix, ``a_source -> b_source`` would
        span both axes simultaneously.
        """
        sch = Mock()

        def create_mock_component(symbol, x, y, ref, *args, **kwargs):
            comp = Mock()
            comp.reference = ref
            rotation = kwargs.get("rotation", 0)
            # Emulate Device:Q_NMOS: source pin is offset +2.54 from the
            # symbol origin in library X.  With rotation=180 that flips
            # to -2.54 in schematic space — the same +/-5.08 mm separation
            # between the two source pins that produced the original
            # diagonal in softstart rev B.
            if rotation == 180:
                pins = {
                    "D": (x - 2.54, y + 5.08),
                    "G": (x + 5.08, y),
                    "S": (x - 2.54, y - 5.08),
                }
            else:
                pins = {
                    "D": (x + 2.54, y - 5.08),
                    "G": (x - 5.08, y),
                    "S": (x + 2.54, y + 5.08),
                }
            comp.pin_position.side_effect = lambda name, _p=pins: _p.get(name, (x, y))
            return comp

        sch.add_symbol = Mock(side_effect=create_mock_component)
        sch.add_wire = Mock()
        sch.add_junction = Mock()
        sch.add_label = Mock()

        BackToBackFETPair(sch, x=100, y=80, ref_a="Q1A", ref_b="Q1B")

        assert sch.add_wire.call_count >= 1, "Block must emit at least one wire"
        for call in sch.add_wire.call_args_list:
            (p1, p2), _kwargs = call.args, call.kwargs
            x1, y1 = p1
            x2, y2 = p2
            is_horizontal = y1 == y2
            is_vertical = x1 == x2
            assert is_horizontal or is_vertical, (
                f"Non-orthogonal wire segment emitted: ({x1}, {y1}) -> "
                f"({x2}, {y2}).  Wires must be horizontal (same y) or "
                f"vertical (same x); diagonals cause unintended crossings "
                f"and defeat validator heuristics (issue #3347)."
            )

    def test_source_tie_passes_through_kelvin_node(self, mock_schematic):
        """The Z-route's bend (Kelvin junction) sits at A-source X / mid Y.

        Locks the geometry guarantee callers rely on: the ``SOURCE`` port
        and any Kelvin hint stub extend from the same A-side bend that
        the previous diagonal implementation used, so this fix does not
        move the public port position.
        """
        pair = BackToBackFETPair(mock_schematic, x=100, y=80)

        # In the default mock, both sources share x=100 (symmetric pins),
        # so kelvin_node X equals a_source.x by construction.  Y is the
        # midpoint between a_source.y (90) and b_source.y (130).
        assert pair.kelvin_node[1] == pytest.approx(110.0)
        # Junction is added exactly at the Kelvin node.
        junction_calls = mock_schematic.add_junction.call_args_list
        assert any(call.args[:2] == pair.kelvin_node for call in junction_calls), (
            "Kelvin junction must sit at kelvin_node coordinates"
        )
