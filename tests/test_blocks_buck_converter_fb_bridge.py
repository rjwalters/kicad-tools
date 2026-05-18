"""Regression test for issue #3011: FB-pin label-on-stub coordinate bridge.

Board-05 placed C2 (a bulk-cap on the VMOTOR rail) at ``x=95.25`` with a
long vertical wire reaching the VMOTOR rail at ``y=25.4``.  Independently,
the ``BuckConverter`` for U1 (LM2596S-5) emitted a label-on-stub for
``pin_nets={"FB": "+5V"}`` at world coordinate ``(95.25, 97.79)`` -- which
is on the C2 vertical wire.  KiCad fuses the FB label's net into the wire's
net, silently bridging VMOTOR <-> +5V.

After the fix, when the FB-stub endpoint would land on a foreign wire,
``BuckConverter`` diverts FB to a direct L-shaped wire to the output-cap
node (``self._vout_node``) instead of emitting a label-on-stub.  The +5V
net label is published externally (by ``connect_to_rails``) on the actual
output-cap node, so FB still ends up on the +5V net -- but via real copper
rather than a coordinate collision.

This test reproduces the exact board-05 geometry with a real ``Schematic``
and verifies that no label lands on the pre-existing VMOTOR wire.  It is
expected to fail on ``main`` (pre-fix) and pass on this branch.
"""

from __future__ import annotations

from kicad_tools.schematic.blocks import BuckConverter
from kicad_tools.schematic.models.schematic import Schematic


# Board-05 coordinates (verbatim from boards/05-bldc-motor-controller/design.py).
BUCK_X = 80.01
BUCK_Y = 100.33
C2_X = 95.25         # X_POWER_IN (25) + 70
RAIL_VMOTOR_Y = 25.4
C2_Y = 114.3

# FB stub geometry on the LM2596S-5 symbol: FB pin at relative (12.7, 2.54).
# With a 2.54 mm stub to the right, the label would land at (95.25, 97.79).
FB_STUB_ENDPOINT = (95.25, 97.79)


def _make_schematic() -> Schematic:
    return Schematic(title="test")


class TestBuckConverterFbBridge:
    """Verify FB pin does not bridge to a foreign wire via label-on-stub."""

    def test_no_label_on_pre_existing_vmotor_wire(self) -> None:
        """The board-05 collision case: FB-stub label MUST NOT land on C2 wire.

        Drive the exact board-05 geometry: place a foreign vertical wire at
        ``x=95.25`` from ``y=25.4`` to past the FB-stub Y (97.79).  Then
        instantiate the buck.  The fix routes FB to ``_vout_node`` instead
        of placing a label at ``(95.25, 97.79)``, so no label sits on the
        foreign wire.
        """
        sch = _make_schematic()

        # Foreign wire mimicking board-05 C2's top-pin-to-VMOTOR wire.  Goes
        # well past the FB stub endpoint Y so the collision is unambiguous.
        sch.add_wire(
            (C2_X, RAIL_VMOTOR_Y),
            (C2_X, C2_Y + 1.27),  # past the FB stub Y at 97.79
            warn_on_collision=False,
        )

        # Add the buck with the board-05 ``pin_nets`` mapping.
        BuckConverter(
            sch,
            x=BUCK_X,
            y=BUCK_Y,
            ref="U1",
            value="LM2596-5.0",
            regulator_symbol="Regulator_Switching:LM2596S-5",
            pin_nets={
                "VIN": "VMOTOR",
                "GND": "GND",
                "FB": "+5V",
            },
        )

        # The bug: a ``+5V`` label was being emitted at (95.25, 97.79),
        # exactly on the foreign VMOTOR wire, bridging VMOTOR <-> +5V.
        # After the fix, FB is wired directly to the output-cap node and
        # no label exists at that coordinate.
        for lbl in sch.labels:
            assert (lbl.x, lbl.y) != FB_STUB_ENDPOINT, (
                f"Regression of #3011: label {lbl.text!r} placed at "
                f"{FB_STUB_ENDPOINT}, which is on the foreign VMOTOR wire."
            )

    def test_no_collision_case_still_emits_label(self) -> None:
        """Without the foreign wire, FB still emits a label-on-stub (back-compat).

        The diverting heuristic only kicks in when there's a collision.
        On a clean schematic the original stub-and-label behavior is
        preserved so the rest of the test suite stays green.
        """
        sch = _make_schematic()
        BuckConverter(
            sch,
            x=BUCK_X,
            y=BUCK_Y,
            ref="U1",
            value="LM2596-5.0",
            regulator_symbol="Regulator_Switching:LM2596S-5",
            pin_nets={"FB": "+5V"},
        )

        plus5v_labels = [lbl for lbl in sch.labels if lbl.text == "+5V"]
        assert len(plus5v_labels) == 1, (
            "Without a foreign wire, FB should still emit exactly one '+5V' "
            "label on its stub endpoint."
        )

    def test_fb_pin_remains_on_vout_net(self) -> None:
        """After diverting, the FB pin is still electrically VOUT.

        The fix wires FB to ``_vout_node`` (the output-cap pin 1).  Verify
        that the wires emitted by the block include a path from FB to the
        output cap, i.e. there exist two wire segments forming an L from
        the FB pin to ``_vout_node``.
        """
        sch = _make_schematic()
        # Foreign wire to trigger the divert
        sch.add_wire(
            (C2_X, RAIL_VMOTOR_Y),
            (C2_X, C2_Y + 1.27),
            warn_on_collision=False,
        )
        block = BuckConverter(
            sch,
            x=BUCK_X,
            y=BUCK_Y,
            ref="U1",
            value="LM2596-5.0",
            regulator_symbol="Regulator_Switching:LM2596S-5",
            pin_nets={"FB": "+5V"},
        )

        fb_pos = block.regulator.pin_position("FB")
        vout_node = block._vout_node

        # Look for an L-shaped path: FB -> (vout_x, fb_y) -> vout_node.
        # That requires a wire ending at (vout_x, fb_y) and another starting
        # there going to vout_node.
        corner = (vout_node[0], fb_pos[1])

        def _wire_endpoints():
            for w in sch.wires:
                yield (w.x1, w.y1), (w.x2, w.y2)

        has_fb_to_corner = any(
            ({a, b} == {fb_pos, corner}) for a, b in _wire_endpoints()
        )
        has_corner_to_vout = any(
            ({a, b} == {corner, vout_node}) for a, b in _wire_endpoints()
        )
        assert has_fb_to_corner, (
            f"Expected a wire from FB pin {fb_pos} to corner {corner}; "
            f"got wires: {list(_wire_endpoints())}"
        )
        assert has_corner_to_vout, (
            f"Expected a wire from corner {corner} to vout_node {vout_node}; "
            f"got wires: {list(_wire_endpoints())}"
        )
