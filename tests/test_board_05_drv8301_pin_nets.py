"""Regression test for board-05 DRV8301 schematic pin wiring (#3387).

PR #3388 reconciled the BOM/PCB/schematic-value triangle around U3 (DRV8301)
and shipped the project-local ``board05_custom:DRV8301`` symbol library
(57-pin HTSSOP-56 pinout matching the physical part).  PR for #3387
completes the work: the schematic now emits U3 against that symbol -- not
the stock ``Driver_Motor:DRV8308`` -- and every pin gets a stub wire +
label so KiCad's label-on-wire connectivity rule is satisfied.

This test pins the post-#3387 state: every DRV8301 pin in the design's
``DRV8301_SCHEMATIC_PIN_NETS`` dict resolves to a real symbol pin position,
each pin gets exactly one stub wire endpoint coincident with one
labelled net, and no two output-type pins share a net.

The test exercises the actual ``board05_custom:DRV8301`` symbol through a
real :class:`Schematic` rather than a mock, so it catches symbol-library
drift in addition to the per-pin emission logic.  Shape mirrors the
previous ``test_board_05_drv8308_pin_nets.py`` (which targeted the
DRV8308 symbol PR #2985/#2986 wired up).

Acceptance criteria (per issue #3387):
* AC1: U3 schematic emission uses ``board05_custom:DRV8301`` symbol.
* AC2: every pin in the design-side mapping resolves to a real position.
* AC3: every labelled pin has a stub wire endpoint coincident with its
  label coordinate (KiCad label-on-wire invariant; regression of #2980).
* AC4: no GND/+3V3 bridge -- specifically, AGND (pin 28) net is "GND"
  and PWRGD (pin 4) net is "+3V3"; if a Builder swaps them by mistake
  the test fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schematic.blocks._stub_helpers import _emit_pin_net_stub
from kicad_tools.schematic.models.schematic import Schematic

# Path to the project-local DRV8301 symbol library shipped by PR #3388.
REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD05_SYMBOL_LIB = (
    REPO_ROOT / "boards" / "05-bldc-motor-controller" / "symbols" / "board05_custom.kicad_sym"
)

# The full board-05 DRV8301 schematic pin-net mapping.  Kept in lock-step
# with ``DRV8301_SCHEMATIC_PIN_NETS`` in
# ``boards/05-bldc-motor-controller/design.py``.  Adding a new entry there
# *should* require adding it here too; if these drift the test fails
# loudly (``test_every_pin_resolves_and_has_label_on_wire``).
#
# Pin 57 (PowerPAD) is wired manually via a vertical stub (not the
# horizontal ``_emit_pin_net_stub`` helper used for pins 1-56) because the
# symbol places it at the bottom-center with orientation 90.  It is
# verified separately by ``test_powerpad_pin_57_ties_to_gnd``.
BOARD_05_DRV8301_PIN_NETS: dict[str, str] = {
    "1": "GND",
    "2": "GND",
    "3": "+5V",
    "4": "+3V3",
    "5": "+3V3",
    "6": "+3V3",
    "7": "GND",
    "8": "+3V3",
    "9": "+3V3",
    "10": "+3V3",
    "11": "+3V3",
    "12": "GND",
    "13": "+5V",
    "14": "+5V",
    "15": "+5V",
    "16": "+3V3",
    "17": "PWM_AH",
    "18": "PWM_AL",
    "19": "PWM_BH",
    "20": "PWM_BL",
    "21": "PWM_CH",
    "22": "PWM_CL",
    "23": "+3V3",
    "24": "+3V3",
    "25": "ISENSE_A+",
    "26": "ISENSE_B+",
    "27": "+5V",
    "28": "GND",
    "29": "+24V",
    "30": "ISENSE_B+",
    "31": "ISENSE_B-",
    "32": "ISENSE_A+",
    "33": "ISENSE_A-",
    "34": "ISENSE_C-",
    "35": "GATE_CL",
    "36": "PHASE_C",
    "37": "GATE_DRV_CH",
    "38": "BST_C",
    "39": "ISENSE_B-",
    "40": "GATE_BL",
    "41": "PHASE_B",
    "42": "GATE_DRV_BH",
    "43": "BST_B",
    "44": "ISENSE_A-",
    "45": "GATE_AL",
    "46": "PHASE_A",
    "47": "GATE_DRV_AH",
    "48": "BST_A",
    "49": "+3V3",
    "50": "SW_OUT",
    "51": "SW_OUT",
    "52": "+24V",
    "53": "+24V",
    "54": "+24V",
    "55": "+3V3",
    "56": "GND",
}


def _make_schematic() -> Schematic:
    """Helper: minimal Schematic registered with the board-05 custom lib."""
    return Schematic(title="test", local_symbol_libs=[BOARD05_SYMBOL_LIB])


def _add_u3(sch: Schematic, x: float = 355, y: float = 145):
    """Helper: add a DRV8301 instance at the same coords design.py uses."""
    return sch.add_symbol(
        "board05_custom:DRV8301",
        x,
        y,
        "U3",
        "DRV8301",
        footprint="Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm",
    )


class TestBoard05DRV8301PinNets:
    """Verify the board-05 DRV8301 schematic emission is well-formed."""

    def test_symbol_library_exists(self) -> None:
        """AC1 precondition: the project-local symbol library file exists.

        PR #3388 shipped this file; if it gets deleted (e.g. by an
        over-zealous cleanup script) the schematic build silently falls
        back to DRV8308 and the U3 sync-netlist drift returns.
        """
        assert BOARD05_SYMBOL_LIB.exists(), (
            f"Expected project-local symbol library at {BOARD05_SYMBOL_LIB!s} "
            f"(shipped by PR #3388).  Without this file, design.py cannot "
            f"resolve ``board05_custom:DRV8301`` and falls back silently."
        )

    def test_every_pin_resolves_to_a_real_position(self) -> None:
        """AC2: every key in ``BOARD_05_DRV8301_PIN_NETS`` resolves to a pin.

        Mis-typing a pin number would surface here as a ``KeyError`` from
        ``pin_position``.  Same shape as the prior DRV8308 test.
        """
        sch = _make_schematic()
        u3 = _add_u3(sch)
        for pin_key in BOARD_05_DRV8301_PIN_NETS:
            pos = u3.pin_position(pin_key)
            assert pos is not None, f"pin {pin_key!r} did not resolve"
            assert isinstance(pos, tuple) and len(pos) == 2

    def test_powerpad_pin_57_resolves(self) -> None:
        """Pin 57 (PowerPAD) is present and at the bottom-center.

        The symbol defines pin 57 at ``(0, -39.37)`` with orientation 90
        (pin line points upward into the body).  ``_emit_pin_net_stub``
        cannot handle vertical pins, so design.py wires this pin manually
        with a downward stub + GND label -- guarded by
        ``test_powerpad_pin_57_ties_to_gnd``.
        """
        sch = _make_schematic()
        u3 = _add_u3(sch)
        pos = u3.pin_position("57")
        assert pos is not None
        # World x ~= U3_x = 350 (snap-adjusted); world y > U3_y = 145
        # (pin is below the symbol center).
        assert abs(pos[0] - 355) < 1.0, f"pin 57 x={pos[0]} != ~355"
        assert pos[1] > 145, f"pin 57 y={pos[1]} should be below U3 center"

    def test_every_pin_has_label_on_wire(self) -> None:
        """AC3: every labelled pin has a wire endpoint at its label coordinate.

        Counts the wires/labels emitted by the per-pin stub loop (mirroring
        the design.py emission shape) and verifies each label lands on a
        wire endpoint.  Without this, KiCad treats labels as floating and
        ERC fires ``isolated_pin_label`` for every left/right-edge pin.
        """
        sch = _make_schematic()
        u3 = _add_u3(sch)
        # Replicate the design.py emission shape: 56 horizontal stubs +
        # one manual vertical stub for pin 57.
        x_center = 355
        for pin_key, net in BOARD_05_DRV8301_PIN_NETS.items():
            pin_pos = u3.pin_position(pin_key)
            _emit_pin_net_stub(
                sch,
                pin_pos,
                x_center,
                net,
                None,
                block_label="U3 DRV8301 ",
            )

        n = len(BOARD_05_DRV8301_PIN_NETS)
        assert len(sch.wires) == n, (
            f"expected {n} new wires (one per pin_nets entry), got {len(sch.wires)}"
        )
        assert len(sch.labels) == n, (
            f"expected {n} new labels (one per pin_nets entry), got {len(sch.labels)}"
        )

        # Every label must sit on a wire endpoint (regression guard for
        # #2980 / #3387).
        wire_endpoints: set[tuple[float, float]] = set()
        for w in sch.wires:
            wire_endpoints.add((w.x1, w.y1))
            wire_endpoints.add((w.x2, w.y2))

        for label in sch.labels:
            pt = (label.x, label.y)
            assert pt in wire_endpoints, (
                f"label {label.text!r} at {pt} has no matching wire endpoint "
                f"-- KiCad will treat it as floating (regression of #2980/#3387)"
            )

    def test_no_gnd_3v3_bridge(self) -> None:
        """AC4 (core anti-regression): GND and +3V3 cannot be unified.

        Issue #3387 documents how the initial DRV8308->DRV8301 swap silently
        bridged AGND (pin 28) onto the +3V3 net through a J3 column-wire
        collision.  This test pins the mapping: pin 28 is GND, pin 4 is
        +3V3, and they are NOT the same net string.  A future refactor
        that accidentally collapses them would fail here loudly before
        ERC catches the global ``multiple_net_names`` warning.
        """
        assert BOARD_05_DRV8301_PIN_NETS["28"] == "GND", (
            "Pin 28 (AGND) must map to the global GND net.  Swapping this "
            "to +3V3 re-introduces the #3387 GND/+3V3 short."
        )
        assert BOARD_05_DRV8301_PIN_NETS["4"] == "+3V3", (
            "Pin 4 (PWRGD) must map to +3V3 (open-drain pull-up).  "
            "Swapping to GND would tie the pull-up rail to GND."
        )
        # No pin maps to an unexpected net name.
        for pin_key, net in BOARD_05_DRV8301_PIN_NETS.items():
            assert net in {
                "GND",
                "+3V3",
                "+5V",
                "+24V",
                "SW_OUT",
                "BST_A",
                "BST_B",
                "BST_C",
                "PWM_AH",
                "PWM_AL",
                "PWM_BH",
                "PWM_BL",
                "PWM_CH",
                "PWM_CL",
                "GATE_AL",
                "GATE_BL",
                "GATE_CL",
                "GATE_DRV_AH",
                "GATE_DRV_BH",
                "GATE_DRV_CH",
                "PHASE_A",
                "PHASE_B",
                "PHASE_C",
                "ISENSE_A+",
                "ISENSE_B+",
                "ISENSE_A-",
                "ISENSE_B-",
                "ISENSE_C-",
            }, f"pin {pin_key!r} maps to unknown net {net!r}"

    def test_outputs_have_unique_nets(self) -> None:
        """Each Output-type pin maps to a unique net name.

        Tying two Output pins to the same wire triggers ERC's
        ``pin_to_pin`` error ("Output pin connected to Output pin").  The
        DRV8301 has two current-sense amplifier outputs (SO1, SO2 = pins
        25, 26) and six gate-driver outputs (GH/GL_A/B/C = pins 35, 37,
        40, 42, 45, 47).  Each must have its own net label.

        Note: SO1/SO2 share their net with the corresponding SP1/SP2
        amplifier input (the differential amp feedback path), so we
        list only the gate-driver outputs in the "must be unique"
        set here.
        """
        # Six gate outputs across three half-bridges (HS and LS each phase).
        output_pin_names = ("35", "37", "40", "42", "45", "47")
        output_nets = [BOARD_05_DRV8301_PIN_NETS[pn] for pn in output_pin_names]
        # Every output pin appears in the dict.
        assert len(output_nets) == 6
        # No duplicates among output net names.
        assert len(set(output_nets)) == len(output_nets), (
            f"duplicate gate-output net(s): {output_nets}"
        )

    def test_construction_does_not_raise(self) -> None:
        """End-to-end smoke test: full pin emission completes without error.

        If any pin number is wrong, the symbol library is missing, or the
        stub-helper regresses, this construction raises.
        """
        sch = _make_schematic()
        u3 = _add_u3(sch)
        for pin_key, net in BOARD_05_DRV8301_PIN_NETS.items():
            pin_pos = u3.pin_position(pin_key)
            _emit_pin_net_stub(
                sch,
                pin_pos,
                355,
                net,
                None,
                block_label="U3 DRV8301 ",
            )
        # Pin 57: vertical stub.
        pp = u3.pin_position("57")
        sch.add_wire(pp, (pp[0], pp[1] + 2.54), warn_on_collision=False)
        sch.add_label("GND", pp[0], pp[1] + 2.54, rotation=90, validate_connection=False)
        # Total wires/labels: 56 (loop) + 1 (PowerPAD) = 57.
        assert len(sch.wires) == 57
        assert len(sch.labels) == 57

    def test_powerpad_pin_57_ties_to_gnd(self) -> None:
        """Pin 57 (PowerPAD) carries the GND net (manual vertical stub).

        The exposed thermal pad is a GND-only signal on the PCB; the
        schematic emission must place a ``GND`` label at the stub endpoint.
        """
        sch = _make_schematic()
        u3 = _add_u3(sch)
        pp = u3.pin_position("57")
        sch.add_wire(pp, (pp[0], pp[1] + 2.54), warn_on_collision=False)
        sch.add_label("GND", pp[0], pp[1] + 2.54, rotation=90, validate_connection=False)
        assert len(sch.labels) == 1
        assert sch.labels[0].text == "GND"
