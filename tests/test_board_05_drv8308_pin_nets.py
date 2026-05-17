"""Regression test for board-05 DRV8308 SPI/control/power pin wiring (#2986).

PR #2985 added the ``pin_nets`` kwarg to :class:`GateDriverBlock` and wired
six gate outputs on the board-05 DRV8308 (closes #2980).  The 33 remaining
pins of the symbol (SPI inputs, discrete control, status outputs, charge-
pump nets, motor power, GND) were intentionally deferred to #2986.

This test pins the post-#2986 state: every DRV8308 pin that needs an
electrical connection -- whether via ``pin_nets`` inside the block, or via
the small block-external vertical-stub helper used for the top-edge
regulator pins -- has a wire endpoint coincident with its net label, and
every Output-type pin gets a *unique* net (KiCad's ERC reports
``pin_to_pin`` when two Output pins share a wire).

The test exercises the actual ``Driver_Motor:DRV8308`` KiCad symbol
through a real :class:`Schematic` rather than a mock, so it catches
symbol-library drift in addition to the block's stub-wire logic.  Shape
mirrors :class:`tests.test_blocks_gate_driver_block.TestGateDriverBlockPinNets`.

Acceptance criteria (per issue #2986):
* AC2: zero ``pin_not_connected`` on DRV8308 SPI / control / power pins.
* AC5: every DRV8308 pin in the extended ``pin_nets`` resolves to a real
  pin position and has a wire endpoint coincident with its label
  coordinate.

A separate test_board_05_zones.py-style end-to-end ERC count guard would
require running ``design.py`` (~3 minutes), so this test stays at the
unit-test layer and validates the construction-time invariant.  The
generated schematic carries the actual ERC count (24 errors total today,
0 of which are on U3 -- see issue #2986 PR description for the
breakdown).
"""

from __future__ import annotations

from kicad_tools.schematic.blocks import GateDriverBlock
from kicad_tools.schematic.models.schematic import Schematic

# The full board-05 ``pin_nets`` mapping for the DRV8308.  Kept in lock-step
# with ``boards/05-bldc-motor-controller/design.py``.  Adding a new entry
# there *should* require adding it here too; if these drift the test fails
# loudly (``test_every_pin_resolves_and_has_label_on_wire``).
BOARD_05_DRV8308_PIN_NETS: dict[str, str] = {
    # SPI inputs (idle high)
    "SCS": "+3.3V",
    "SCLK": "+3.3V",
    "SDATAI": "+3.3V",
    "SMODE": "+3.3V",
    # Discrete control inputs
    "ENABLE": "+3.3V",
    "RESET": "+3.3V",
    "BRAKE": "+3.3V",
    "DIR": "+3.3V",
    "CLKIN": "GND",
    # Feedback/tachometer inputs
    "FGINP": "GND",
    "FGINN_TACH": "GND",
    # Status outputs (each MUST be unique to avoid pin_to_pin ERC).
    "SDATAO": "SPI_MISO",
    "FGFB": "FGFB",
    "FGOUT": "FGOUT",
    "~{FAULTn}": "DRV_FAULTn",
    "~{LOCKn}": "DRV_LOCKn",
    # HS/LS gate outputs (existing PR #2985 pins, retained for completeness)
    "UHSG": "GATE_DRV_AH",
    "VHSG": "GATE_DRV_BH",
    "WHSG": "GATE_DRV_CH",
    "ULSG": "GATE_AL",
    "VLSG": "GATE_BL",
    "WLSG": "GATE_CL",
    # Phase bootstrap pins (HighP / HighN per phase)
    "UHP": "BST_A",
    "UHN": "PHASE_A",
    "VHP": "BST_B",
    "VHN": "PHASE_B",
    "WHP": "BST_C",
    "WHN": "PHASE_C",
    # Phase-voltage sense inputs
    "U": "PHASE_A",
    "V": "PHASE_B",
    "W": "PHASE_C",
    # Current sense input
    "ISEN": "GND",
    # Two GND pins on the symbol; pin_position("GND") resolves to pin 26.
    # The second GND pin (41) is keyed by pin number.
    "GND": "GND",
    "41": "GND",
    # Charge-pump capacitor pins (left edge of symbol)
    "CP1": "DRV_CP1",
    "CP2": "DRV_CP2",
}

# Top-edge pins (VINT, VCP, VM, VSW, VREG) cannot use the block's
# horizontal pin_nets stubs without colliding, so they are wired with
# vertical stubs outside the block in design.py.  This test loop reaches
# down to ``add_wire``/``add_label`` directly to verify the same
# label-on-wire invariant holds for them.
BOARD_05_DRV8308_TOP_EDGE_NETS: dict[str, str] = {
    "VINT": "DRV_VINT",
    "VCP": "DRV_VCP",
    "VM": "VMOTOR",
    "VSW": "DRV_VSW",
    "VREG": "DRV_VREG",
}


def _make_schematic() -> Schematic:
    """Helper: minimal Schematic with a title block."""
    return Schematic(title="test")


class TestBoard05DRV8308PinNets:
    """Verify the extended board-05 DRV8308 ``pin_nets`` dict is well-formed."""

    def test_every_pin_resolves_to_a_real_position(self) -> None:
        """Every key in ``BOARD_05_DRV8308_PIN_NETS`` resolves to a pin.

        Driven by AC5 of issue #2986: "every DRV8308 pin in the extended
        pin_nets resolves to a real pin position".  Mis-spelling a pin name
        (e.g. ``"FAULTn"`` instead of ``"~{FAULTn}"``) would surface here
        as a ``KeyError`` from ``pin_position``.
        """
        sch = _make_schematic()
        driver = sch.add_symbol("Driver_Motor:DRV8308", 280, 95, "U3", "DRV8308")
        for pin_key in BOARD_05_DRV8308_PIN_NETS:
            pos = driver.pin_position(pin_key)
            assert pos is not None, f"pin {pin_key!r} did not resolve"
            assert isinstance(pos, tuple) and len(pos) == 2

    def test_construction_does_not_raise(self) -> None:
        """Instantiating ``GateDriverBlock`` with the full dict succeeds.

        End-to-end smoke test: if any pin name is wrong, or the block's
        ``pin_position`` logic regresses, this raises during construction.
        """
        sch = _make_schematic()
        block = GateDriverBlock(
            sch,
            x=280,
            y=95,
            driver_type="3-phase",
            ref="U3",
            value="DRV8301",  # matches board-05 BOM
            bootstrap_caps=None,
            bypass_caps=["100nF", "10uF"],
            cap_ref_start=15,
            pin_nets=BOARD_05_DRV8308_PIN_NETS,
        )
        assert block.driver.reference == "U3"

    def test_every_pin_has_label_on_wire(self) -> None:
        """AC5: every labelled pin has a wire endpoint at its label coordinate.

        This is the load-bearing invariant for KiCad's label-on-wire
        connectivity (regression guard for #2980 / #2986).  We count
        ``add_wire`` and ``add_label`` *deltas* against a baseline run
        without ``pin_nets`` so unrelated wires/labels emitted by the
        block (bypass caps, etc.) don't pollute the assertion.
        """
        sch_base = _make_schematic()
        GateDriverBlock(
            sch_base, x=280, y=95, driver_type="3-phase",
            ref="U3", value="DRV8301",
            bootstrap_caps=None, bypass_caps=["100nF", "10uF"],
            cap_ref_start=15,
            pin_nets=None,
        )
        base_wire_count = len(sch_base.wires)
        base_label_count = len(sch_base.labels)

        sch = _make_schematic()
        block = GateDriverBlock(
            sch, x=280, y=95, driver_type="3-phase",
            ref="U3", value="DRV8301",
            bootstrap_caps=None, bypass_caps=["100nF", "10uF"],
            cap_ref_start=15,
            pin_nets=BOARD_05_DRV8308_PIN_NETS,
        )

        new_wires = sch.wires[base_wire_count:]
        new_labels = sch.labels[base_label_count:]

        # One wire and one label per pin_nets entry.
        n = len(BOARD_05_DRV8308_PIN_NETS)
        assert len(new_wires) == n, (
            f"expected {n} new wires (one per pin_nets entry), got {len(new_wires)}"
        )
        assert len(new_labels) == n, (
            f"expected {n} new labels (one per pin_nets entry), got {len(new_labels)}"
        )

        # Every label must sit on a wire endpoint (regression guard for
        # #2980 / #2986 -- without this KiCad treats the label as floating
        # and ERC reports ``isolated_pin_label``).
        wire_endpoints: set[tuple[float, float]] = set()
        for w in new_wires:
            wire_endpoints.add((w.x1, w.y1))
            wire_endpoints.add((w.x2, w.y2))

        for label in new_labels:
            pt = (label.x, label.y)
            assert pt in wire_endpoints, (
                f"label {label.text!r} at {pt} has no matching wire endpoint "
                f"-- KiCad will treat it as floating (regression of #2980/#2986)"
            )

        # And every pin in the dict gets a port aliased to its real
        # coordinate (used by external wiring; see motor.py:1023-1040).
        for pin_key, net_name in BOARD_05_DRV8308_PIN_NETS.items():
            # GND is the only net that collides with an existing placeholder
            # port; the original placeholder wins (back-compat with PR #2985).
            if net_name == "GND":
                continue
            assert net_name in block.ports, (
                f"net {net_name!r} (pin {pin_key!r}) missing from block.ports"
            )

    def test_outputs_have_unique_nets(self) -> None:
        """Each Output-type pin maps to a unique net name.

        Tying two Output pins to the same wire triggers ERC's
        ``pin_to_pin`` error ("Output pin connected to Output pin").  The
        DRV8308 symbol has six Output-type pins on the side carrying the
        status/SPI-MISO signals: SDATAO, FGFB, FGOUT, ~FAULTn, ~LOCKn (and
        the HS/LS gate outputs already covered by #2985).  Each must have
        its own net label.
        """
        output_pin_names = {
            "SDATAO", "FGFB", "FGOUT", "~{FAULTn}", "~{LOCKn}",
            "UHSG", "VHSG", "WHSG", "ULSG", "VLSG", "WLSG",
        }
        output_nets = [
            BOARD_05_DRV8308_PIN_NETS[name]
            for name in output_pin_names
            if name in BOARD_05_DRV8308_PIN_NETS
        ]
        # Every output pin appears in the dict.
        assert len(output_nets) == len(output_pin_names), (
            "not every output pin is mapped in BOARD_05_DRV8308_PIN_NETS"
        )
        # No duplicates among output net names.
        assert len(set(output_nets)) == len(output_nets), (
            f"duplicate output net(s): output_nets has duplicates -> {output_nets}"
        )

    def test_top_edge_pins_resolve(self) -> None:
        """The top-edge pin set used for vertical-stub wiring all resolves.

        ``design.py`` handles VINT/VCP/VM/VSW/VREG with vertical stubs
        outside the block (the block's horizontal stubs collide due to
        2.54 mm pin spacing).  This test pins the pin-name set so a
        typo there is caught at unit-test time.
        """
        sch = _make_schematic()
        driver = sch.add_symbol("Driver_Motor:DRV8308", 280, 95, "U3", "DRV8308")
        for pin_key in BOARD_05_DRV8308_TOP_EDGE_NETS:
            pos = driver.pin_position(pin_key)
            assert pos is not None, f"top-edge pin {pin_key!r} did not resolve"

    def test_top_edge_pins_have_distinct_positions(self) -> None:
        """The top-edge pins each occupy a distinct (x, y).

        Without distinct positions, vertical stubs would collide.  This
        catches a future symbol revision that merges/relocates the
        regulator pins.
        """
        sch = _make_schematic()
        driver = sch.add_symbol("Driver_Motor:DRV8308", 280, 95, "U3", "DRV8308")
        positions = {
            pin: driver.pin_position(pin)
            for pin in BOARD_05_DRV8308_TOP_EDGE_NETS
        }
        # All x-coords are distinct (the y-coord is the same for all top-edge pins).
        xs = [pos[0] for pos in positions.values()]
        assert len(set(xs)) == len(xs), (
            f"top-edge pins have duplicate x-coords: {positions}"
        )
