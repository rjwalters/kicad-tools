"""Regression tests for issue #3015: generic stub-endpoint collision detection.

Promotes the FB-pin collision check from PR #3014 to a module-level helper
(``_emit_pin_net_stub`` in ``kicad_tools.schematic.blocks._stub_helpers``)
and applies it to every block that has the silent-net-bridging timebomb:

  * ``LDOBlock`` (``power/regulators.py``)
  * ``GateDriverBlock`` (``motor.py``)
  * ``HalfBridge`` gate-net branches (``motor.py``)
  * ``DebugHeader`` (``interface/debug.py``)

The helper:

  1. Tries the symbol-center-aware primary side first.
  2. Auto-shifts to the opposite side if the primary endpoint would land
     on an existing foreign wire.
  3. Raises ``ValueError`` if both sides would collide.  Silent net
     bridging is unrecoverable at netlist time, so we fail loudly.

This file exercises all three behaviors on every affected block using a
real ``Schematic`` (not a mock) so the collision detection runs against
the real ``_find_wire_collisions_for_point`` primitive.

The ``BuckConverter`` FB-pin branch keeps its existing divert-to-VOUT
behavior (PR #3014); ``tests/test_blocks_buck_converter_fb_bridge.py``
remains the regression test for that special case.
"""

from __future__ import annotations

import pytest

from kicad_tools.schematic.blocks import (
    BuckConverter,
    DebugHeader,
    GateDriverBlock,
    HalfBridge,
    LDOBlock,
)
from kicad_tools.schematic.models.schematic import Schematic


STUB = 2.54


def _make_schematic() -> Schematic:
    return Schematic(title="test")


def _label_coords(sch: Schematic) -> set[tuple[float, float]]:
    return {(lbl.x, lbl.y) for lbl in sch.labels}


def _label_at(sch: Schematic, x: float, y: float) -> bool:
    """True iff a label exists exactly at (x, y) (within grid snap)."""
    for lbl in sch.labels:
        if abs(lbl.x - x) < 0.01 and abs(lbl.y - y) < 0.01:
            return True
    return False


# ---------------------------------------------------------------------------
# LDOBlock
# ---------------------------------------------------------------------------
class TestLDOPinNetsCollision:
    """Issue #3015 regression tests for ``LDOBlock`` pin_nets emission."""

    # Board-realistic AMS1117 LDO geometry.  The AMS1117 pin numbering is
    # 1=GND (bottom center), 2=VO (right), 3=VI (left).
    LDO_X = 100.0
    LDO_Y = 100.0

    def test_no_collision_emits_normal_label(self) -> None:
        """Baseline: no foreign wire, label lands on the primary-side stub."""
        sch = _make_schematic()
        LDOBlock(
            sch, x=self.LDO_X, y=self.LDO_Y,
            ref="U1",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
            pin_nets={"VI": "+5V"},
        )
        # VI is on the symbol's left edge so the primary stub goes left.
        plus5v_labels = [lbl for lbl in sch.labels if lbl.text == "+5V"]
        assert len(plus5v_labels) == 1, (
            f"Expected exactly one +5V label, got {len(plus5v_labels)}"
        )

    def test_auto_shift_on_one_side_collision(self) -> None:
        """A foreign wire on the primary side triggers auto-shift to the other side."""
        sch = _make_schematic()

        # Find the VI pin coordinate first by instantiating a probe LDO,
        # then build a real one on a fresh schematic with the foreign wire
        # placed exactly on the primary-side stub endpoint.
        probe = _make_schematic()
        probe_ldo = LDOBlock(
            probe, x=self.LDO_X, y=self.LDO_Y,
            ref="U99",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
        )
        vi_pos = probe_ldo.ldo.pin_position("VI")
        # VI is left of center; primary stub goes left.
        primary_x = vi_pos[0] - STUB
        fallback_x = vi_pos[0] + STUB

        # Build the real test schematic: pre-add a vertical wire crossing
        # the primary stub endpoint's interior.  The wire must extend a
        # distance above and below the endpoint so the point lies on the
        # *interior* (not the endpoint).
        sch.add_wire(
            (primary_x, vi_pos[1] - 10),
            (primary_x, vi_pos[1] + 10),
            warn_on_collision=False,
        )

        LDOBlock(
            sch, x=self.LDO_X, y=self.LDO_Y,
            ref="U1",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
            pin_nets={"VI": "+5V"},
        )

        # The label must NOT land on the foreign wire's coordinate; it
        # should land on the fallback side.
        assert not _label_at(sch, primary_x, vi_pos[1]), (
            f"Regression of #3015: +5V label landed on foreign wire at "
            f"({primary_x}, {vi_pos[1]})."
        )
        assert _label_at(sch, fallback_x, vi_pos[1]), (
            f"Expected auto-shift to fallback side at "
            f"({fallback_x}, {vi_pos[1]}); got labels at {_label_coords(sch)}."
        )

    def test_both_sides_collide_raises(self) -> None:
        """Both sides colliding raises ``ValueError`` (NOT silent bridge)."""
        sch = _make_schematic()

        # Probe to find pin coordinate.
        probe = _make_schematic()
        probe_ldo = LDOBlock(
            probe, x=self.LDO_X, y=self.LDO_Y,
            ref="U99",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
        )
        vi_pos = probe_ldo.ldo.pin_position("VI")
        primary_x = vi_pos[0] - STUB
        fallback_x = vi_pos[0] + STUB

        # Pre-add foreign wires on BOTH sides.
        sch.add_wire(
            (primary_x, vi_pos[1] - 10),
            (primary_x, vi_pos[1] + 10),
            warn_on_collision=False,
        )
        sch.add_wire(
            (fallback_x, vi_pos[1] - 10),
            (fallback_x, vi_pos[1] + 10),
            warn_on_collision=False,
        )

        with pytest.raises(ValueError) as exc_info:
            LDOBlock(
                sch, x=self.LDO_X, y=self.LDO_Y,
                ref="U1",
                ldo_symbol="Regulator_Linear:AMS1117-3.3",
                pin_nets={"VI": "+5V"},
            )
        msg = str(exc_info.value)
        assert "+5V" in msg, f"Expected net name in error: {msg!r}"
        assert "LDOBlock" in msg, f"Expected block label in error: {msg!r}"


# ---------------------------------------------------------------------------
# GateDriverBlock
# ---------------------------------------------------------------------------
class TestGateDriverPinNetsCollision:
    """Issue #3015 regression tests for ``GateDriverBlock`` pin_nets emission."""

    DRIVER_X = 200.0
    DRIVER_Y = 150.0

    def test_auto_shift_on_one_side_collision(self) -> None:
        """A foreign wire on the primary side triggers auto-shift on GateDriverBlock."""
        sch = _make_schematic()

        # Probe to find pin coordinate.  Use Device:U as a generic 4-pin
        # symbol with predictable pin positions; the GateDriverBlock
        # public API exposes ``self.driver.pin_position``.
        probe = _make_schematic()
        probe_drv = GateDriverBlock(
            probe, x=self.DRIVER_X, y=self.DRIVER_Y,
            ref="U99",
            driver_symbol="Device:R",  # 2-pin symbol, "1"/"2" pin numbers
            bootstrap_caps=None,
            bypass_caps=[],
        )
        # Use pin "1" which exists on Device:R.
        pin_pos = probe_drv.driver.pin_position("1")
        # Determine which side is primary (depends on pin_pos[0] vs DRIVER_X).
        if pin_pos[0] < self.DRIVER_X:
            primary_x = pin_pos[0] - STUB
            fallback_x = pin_pos[0] + STUB
        else:
            primary_x = pin_pos[0] + STUB
            fallback_x = pin_pos[0] - STUB

        sch.add_wire(
            (primary_x, pin_pos[1] - 10),
            (primary_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )

        GateDriverBlock(
            sch, x=self.DRIVER_X, y=self.DRIVER_Y,
            ref="U1",
            driver_symbol="Device:R",
            bootstrap_caps=None,
            bypass_caps=[],
            pin_nets={"1": "SDA"},
        )

        assert not _label_at(sch, primary_x, pin_pos[1])
        assert _label_at(sch, fallback_x, pin_pos[1])

    def test_both_sides_collide_raises(self) -> None:
        """Both sides colliding raises ``ValueError`` for GateDriverBlock."""
        sch = _make_schematic()

        probe = _make_schematic()
        probe_drv = GateDriverBlock(
            probe, x=self.DRIVER_X, y=self.DRIVER_Y,
            ref="U99",
            driver_symbol="Device:R",
            bootstrap_caps=None,
            bypass_caps=[],
        )
        pin_pos = probe_drv.driver.pin_position("1")
        if pin_pos[0] < self.DRIVER_X:
            primary_x = pin_pos[0] - STUB
            fallback_x = pin_pos[0] + STUB
        else:
            primary_x = pin_pos[0] + STUB
            fallback_x = pin_pos[0] - STUB

        sch.add_wire(
            (primary_x, pin_pos[1] - 10),
            (primary_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )
        sch.add_wire(
            (fallback_x, pin_pos[1] - 10),
            (fallback_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )

        with pytest.raises(ValueError) as exc_info:
            GateDriverBlock(
                sch, x=self.DRIVER_X, y=self.DRIVER_Y,
                ref="U1",
                driver_symbol="Device:R",
                bootstrap_caps=None,
                bypass_caps=[],
                pin_nets={"1": "MOSI"},
            )
        msg = str(exc_info.value)
        assert "MOSI" in msg
        assert "GateDriverBlock" in msg


# ---------------------------------------------------------------------------
# HalfBridge
# ---------------------------------------------------------------------------
class TestHalfBridgeGateCollision:
    """Issue #3015 regression tests for ``HalfBridge`` gate-net stubs."""

    HB_X = 150.0
    HB_Y = 100.0

    def test_no_collision_emits_normal_label(self) -> None:
        """Baseline: gate label on the primary (left) side."""
        sch = _make_schematic()
        HalfBridge(
            sch, x=self.HB_X, y=self.HB_Y,
            ref_prefix="Q",
            gate_hs_net="UHSG",
        )
        uhsg_labels = [lbl for lbl in sch.labels if lbl.text == "UHSG"]
        assert len(uhsg_labels) == 1

    def test_auto_shift_on_left_side_collision(self) -> None:
        """A foreign wire on the (left) primary side triggers auto-shift."""
        sch = _make_schematic()

        probe = _make_schematic()
        probe_hb = HalfBridge(
            probe, x=self.HB_X, y=self.HB_Y,
            ref_prefix="Q",
        )
        hs_gate = probe_hb.mosfet_hs.pin_position("G")
        # HalfBridge passes ``x_center=hs_gate[0] + 1.0`` so primary is left.
        primary_x = hs_gate[0] - STUB
        fallback_x = hs_gate[0] + STUB

        sch.add_wire(
            (primary_x, hs_gate[1] - 10),
            (primary_x, hs_gate[1] + 10),
            warn_on_collision=False,
        )

        HalfBridge(
            sch, x=self.HB_X, y=self.HB_Y,
            ref_prefix="Q",
            gate_hs_net="UHSG",
        )

        assert not _label_at(sch, primary_x, hs_gate[1])
        assert _label_at(sch, fallback_x, hs_gate[1])

    def test_both_sides_collide_raises(self) -> None:
        """Both sides colliding raises ``ValueError`` for HalfBridge gate."""
        sch = _make_schematic()

        probe = _make_schematic()
        probe_hb = HalfBridge(
            probe, x=self.HB_X, y=self.HB_Y,
            ref_prefix="Q",
        )
        hs_gate = probe_hb.mosfet_hs.pin_position("G")
        primary_x = hs_gate[0] - STUB
        fallback_x = hs_gate[0] + STUB

        sch.add_wire(
            (primary_x, hs_gate[1] - 10),
            (primary_x, hs_gate[1] + 10),
            warn_on_collision=False,
        )
        sch.add_wire(
            (fallback_x, hs_gate[1] - 10),
            (fallback_x, hs_gate[1] + 10),
            warn_on_collision=False,
        )

        with pytest.raises(ValueError) as exc_info:
            HalfBridge(
                sch, x=self.HB_X, y=self.HB_Y,
                ref_prefix="Q",
                gate_hs_net="UHSG",
            )
        msg = str(exc_info.value)
        assert "UHSG" in msg
        assert "HalfBridge" in msg


# ---------------------------------------------------------------------------
# DebugHeader
# ---------------------------------------------------------------------------
class TestDebugHeaderPinNetsCollision:
    """Issue #3015 regression tests for ``DebugHeader`` pin_nets emission."""

    HDR_X = 250.0
    HDR_Y = 50.0

    def test_auto_shift_on_one_side_collision(self) -> None:
        """A foreign wire on the primary side triggers auto-shift on DebugHeader."""
        sch = _make_schematic()

        probe = _make_schematic()
        probe_hdr = DebugHeader(
            probe, x=self.HDR_X, y=self.HDR_Y,
            interface="swd", pins=10, ref="J99",
        )
        pin_pos = probe_hdr.header.pin_position("1")
        if pin_pos[0] < self.HDR_X:
            primary_x = pin_pos[0] - STUB
            fallback_x = pin_pos[0] + STUB
        else:
            primary_x = pin_pos[0] + STUB
            fallback_x = pin_pos[0] - STUB

        sch.add_wire(
            (primary_x, pin_pos[1] - 10),
            (primary_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )

        DebugHeader(
            sch, x=self.HDR_X, y=self.HDR_Y,
            interface="swd", pins=10, ref="J1",
            pin_nets={"1": "+3V3"},
        )

        assert not _label_at(sch, primary_x, pin_pos[1])
        assert _label_at(sch, fallback_x, pin_pos[1])

    def test_both_sides_collide_raises(self) -> None:
        """Both sides colliding raises ``ValueError`` for DebugHeader."""
        sch = _make_schematic()

        probe = _make_schematic()
        probe_hdr = DebugHeader(
            probe, x=self.HDR_X, y=self.HDR_Y,
            interface="swd", pins=10, ref="J99",
        )
        pin_pos = probe_hdr.header.pin_position("1")
        if pin_pos[0] < self.HDR_X:
            primary_x = pin_pos[0] - STUB
            fallback_x = pin_pos[0] + STUB
        else:
            primary_x = pin_pos[0] + STUB
            fallback_x = pin_pos[0] - STUB

        sch.add_wire(
            (primary_x, pin_pos[1] - 10),
            (primary_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )
        sch.add_wire(
            (fallback_x, pin_pos[1] - 10),
            (fallback_x, pin_pos[1] + 10),
            warn_on_collision=False,
        )

        with pytest.raises(ValueError) as exc_info:
            DebugHeader(
                sch, x=self.HDR_X, y=self.HDR_Y,
                interface="swd", pins=10, ref="J1",
                pin_nets={"1": "+3V3"},
            )
        msg = str(exc_info.value)
        assert "+3V3" in msg
        assert "DebugHeader" in msg


# ---------------------------------------------------------------------------
# Board-realistic scenario: reproduce PR #3014's board-05 collision style
# for one of the newly covered blocks (LDOBlock here as the simplest case).
# ---------------------------------------------------------------------------
class TestBoardRealisticCollision:
    """Reproduce PR #3014's board-05 VMOTOR<->+5V geometry style on LDOBlock.

    PR #3014 exposed the silent-bridging bug on the BuckConverter FB pin
    with a foreign vertical wire crossing the FB-stub endpoint's interior.
    For the blocks added in #3015, the same geometry must trigger the
    auto-shift path (not the FB-style divert) and never leave a label on
    the foreign wire.
    """

    def test_ldo_with_long_vertical_rail_does_not_bridge(self) -> None:
        """LDO placed next to a long VMOTOR-style rail auto-shifts cleanly."""
        sch = _make_schematic()
        LDO_X = 80.0
        LDO_Y = 100.0

        # Probe to compute the would-be primary stub coordinate.
        probe = _make_schematic()
        probe_ldo = LDOBlock(
            probe, x=LDO_X, y=LDO_Y,
            ref="U99",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
        )
        vi_pos = probe_ldo.ldo.pin_position("VI")
        primary_x = vi_pos[0] - STUB  # VI is left of center

        # Pre-add a long vertical "rail" wire (board-05 C2-style) whose
        # interior crosses the primary-side stub endpoint.  The wire goes
        # from y=20 (rail) to y=120 (well past pin), and the stub endpoint
        # at vi_pos[1] sits firmly in its interior.
        sch.add_wire(
            (primary_x, 20.0),
            (primary_x, 120.0),
            warn_on_collision=False,
        )

        # Now instantiate the real LDO with a +5V label on VI.  The bug
        # would silently bridge +5V into whatever net the foreign rail
        # belongs to; the fix auto-shifts to the other side.
        block = LDOBlock(
            sch, x=LDO_X, y=LDO_Y,
            ref="U1",
            ldo_symbol="Regulator_Linear:AMS1117-3.3",
            pin_nets={"VI": "+5V"},
        )

        # No label may sit on the foreign wire's interior.
        for lbl in sch.labels:
            assert (lbl.x, lbl.y) != (primary_x, vi_pos[1]), (
                f"Regression of #3015: label {lbl.text!r} landed on the "
                f"pre-existing foreign wire at ({primary_x}, {vi_pos[1]})."
            )

        # The label exists somewhere (auto-shift placed it on the fallback).
        plus5v_labels = [lbl for lbl in sch.labels if lbl.text == "+5V"]
        assert len(plus5v_labels) == 1
        # Port alias still resolves to the pin's real coordinate.
        assert block.ports["+5V"] == vi_pos
