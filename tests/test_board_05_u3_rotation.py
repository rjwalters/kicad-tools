"""Regression guard: board-05 U3 (DRV8301, HTSSOP-56) is rotated 90° CW.

Issue #3423 (decomposition #3422, target #3412): U3's library-canonical
long-axis-vertical orientation put the half-bridge pins (29-56) on the
EAST edge while the MOSFET row sits SOUTH, forcing PWM nets to cross the
U3 body and GATE_*/ISENSE_* nets to wrap around the package -- the
unrouteable-net categories 1-3 of the #3422 taxonomy.

The fix bakes a 90° clockwise rotation (-90° in this repo's CCW-positive
convention, PR #738) directly into ``_htssop56_pad_xy``: each canonical
pad centre (x, y) maps to (-y, x) and the pad size swaps
1.55x0.30 -> 0.30x1.55 (EP: 3.61x6.35 -> 6.35x3.61).  The rotation is in
the COORDINATES, not an ``(at x y angle)`` attribute, because
``generate_htssop56`` hand-emits its S-expressions without rotation
support.

This test verifies, against the committed unrouted PCB:

1. Every U3 perimeter pad sits exactly at the (-y, x) image of its
   library-canonical position, with swapped pad sizes (the curator-
   verified transform on #3423).
2. The resulting edge order: pins 1-28 across the NORTH edge
   right-to-left, pins 29-56 across the SOUTH edge left-to-right --
   i.e. half-bridge/buck pins face the MOSFET row, logic pins face the
   MCU.  (The original issue body claimed the mirrored south-edge
   order; the curator comment corrects it.  This assertion pins the
   correct one.)
3. The companion phase-column swap: with south-edge order
   C (mid-left) / B (center) / A (mid-right), the MOSFET columns must
   be C/B/A west-to-east, i.e. Q1/Q2 carry PHASE_C and Q5/Q6 carry
   PHASE_A (net swap; placement unchanged).  Without the swap the
   Phase A and Phase C gate/sense nets cross over ~18mm.

Updating this test: if U3's package or orientation legitimately changes
again (e.g. QFN-56 migration), rewrite the expected transform here in
the same PR, with the new geometry's routing rationale in the PR body.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
UNROUTED_PCB = BOARD_DIR / "output" / "bldc_controller.kicad_pcb"

# Library-canonical (long-axis-vertical) HTSSOP-56 pad centres, per the
# KiCad footprint Package_SO:HTSSOP-56-1EP_6.1x14mm_P0.5mm_EP3.61x6.35mm:
# pins 1-28 down the left edge top-to-bottom at x=-3.75, pins 29-56 up
# the right edge bottom-to-top at x=+3.75; 0.5mm pitch from +/-6.75.


def _canonical_pad_xy(pin: int) -> tuple[float, float]:
    if 1 <= pin <= 28:
        return (-3.75, -6.75 + (pin - 1) * 0.5)
    if 29 <= pin <= 56:
        return (3.75, 6.75 - (pin - 29) * 0.5)
    raise ValueError(pin)


def _rotated_cw(xy: tuple[float, float]) -> tuple[float, float]:
    """Visual 90° CW rotation in KiCad's Y-down frame: (x, y) -> (-y, x)."""
    x, y = xy
    return (-y, x)


@pytest.fixture(scope="module")
def pcb_text() -> str:
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return UNROUTED_PCB.read_text()


def _u3_block(pcb_text: str) -> str:
    """Extract the U3 HTSSOP-56 footprint block from the PCB text."""
    start = pcb_text.index('(footprint "Package_SO:HTSSOP-56-1EP')
    end = pcb_text.index("(footprint ", start + 10)
    block = pcb_text[start:end]
    assert 'reference "U3"' in block.replace("\n", " ").replace("\t", " "), (
        "HTSSOP-56 footprint block does not carry reference U3 -- "
        "did the footprint/ref assignment change?"
    )
    return block


_PAD_RE = re.compile(
    r'\(pad "(\d+)" smd \w+\s*'
    r"\(at ([-\d.]+) ([-\d.]+)\)\s*"
    r"\(size ([\d.]+) ([\d.]+)\)",
    re.DOTALL,
)


def _u3_pads(pcb_text: str) -> dict[int, tuple[float, float, float, float]]:
    pads: dict[int, tuple[float, float, float, float]] = {}
    for m in _PAD_RE.finditer(_u3_block(pcb_text)):
        pads[int(m.group(1))] = (
            float(m.group(2)),
            float(m.group(3)),
            float(m.group(4)),
            float(m.group(5)),
        )
    return pads


class TestU3RotatedPadGeometry:
    """AC: U3 pads carry the (x, y) -> (-y, x) 90°-CW transform."""

    def test_all_56_perimeter_pads_match_cw_transform(self, pcb_text: str) -> None:
        pads = _u3_pads(pcb_text)
        missing = [p for p in range(1, 57) if p not in pads]
        assert not missing, f"U3 missing pads: {missing}"
        mismatches = []
        for pin in range(1, 57):
            ex, ey = _rotated_cw(_canonical_pad_xy(pin))
            ax, ay, sx, sy = pads[pin]
            if abs(ax - ex) > 1e-6 or abs(ay - ey) > 1e-6:
                mismatches.append((pin, (ax, ay), (ex, ey)))
        assert not mismatches, (
            f"U3 pads off the 90°-CW transform (pin, actual, expected): {mismatches[:8]!r}"
        )

    def test_pad_sizes_swapped_for_rotation(self, pcb_text: str) -> None:
        pads = _u3_pads(pcb_text)
        for pin in range(1, 57):
            _, _, sx, sy = pads[pin]
            assert (sx, sy) == (0.3, 1.55), (
                f"U3 pin {pin} pad size {sx}x{sy}; expected 0.3x1.55 "
                f"(1.55x0.30 swapped for the 90° rotation)"
            )

    def test_exposed_pad_size_swapped(self, pcb_text: str) -> None:
        pads = _u3_pads(pcb_text)
        assert 57 in pads, "U3 PowerPAD (pad 57) missing"
        x, y, sx, sy = pads[57]
        assert (x, y) == (0.0, 0.0)
        assert (sx, sy) == (6.35, 3.61), (
            f"U3 EP size {sx}x{sy}; expected 6.35x3.61 (3.61x6.35 swapped)"
        )

    def test_half_bridge_pins_face_south_in_29_to_56_order(self, pcb_text: str) -> None:
        """South edge runs 29..56 left-to-right (curator-corrected order).

        This is the geometric fact the phase-column swap is built on:
        Phase C pins (34-38) land mid-LEFT and Phase A pins (44-48)
        mid-RIGHT.  The original issue body claimed the mirror image.
        """
        pads = _u3_pads(pcb_text)
        south = [(pads[p][0], p) for p in range(29, 57)]
        assert all(pads[p][1] == 3.75 for p in range(29, 57)), (
            "pins 29-56 must all sit on the south edge (y=+3.75)"
        )
        assert south == sorted(south), (
            "south-edge pins 29-56 must increase in x left-to-right; "
            "a mirrored order means the rotation direction flipped and "
            "the C/B/A MOSFET column swap is now WRONG (A/C nets cross)"
        )

    def test_logic_pins_face_north(self, pcb_text: str) -> None:
        pads = _u3_pads(pcb_text)
        assert all(pads[p][1] == -3.75 for p in range(1, 29)), (
            "pins 1-28 must all sit on the north edge (y=-3.75)"
        )
        north = [(pads[p][0], p) for p in range(1, 29)]
        assert north == sorted(north, reverse=True), (
            "north-edge pins 1-28 must decrease in x (pin 1 at the right)"
        )


class TestPhaseColumnSwap:
    """AC: MOSFET phase columns are C/B/A to match the rotated south edge."""

    def _pad_net(self, pcb_text: str, ref: str, pad: str) -> str:
        """Return the net name on *ref*'s pad *pad* (first match)."""
        # Footprint blocks carry a fp_text reference line; find the
        # block for ``ref`` then the pad inside it.
        ref_m = re.search(
            rf'\(footprint[^\n]*\n(?:.*?\n)*?[^\n]*reference "{re.escape(ref)}"',
            pcb_text,
        )
        assert ref_m, f"footprint {ref} not found"
        start = pcb_text.rindex("(footprint", 0, ref_m.end())
        next_fp = pcb_text.find("(footprint ", start + 10)
        block = pcb_text[start : next_fp if next_fp != -1 else len(pcb_text)]
        pad_m = re.search(rf'\(pad "{re.escape(pad)}"[\s\S]*?\(net \d+ "([^"]+)"\)', block)
        assert pad_m, f"{ref} pad {pad} (with net) not found"
        return pad_m.group(1)

    def test_west_column_carries_phase_c(self, pcb_text: str) -> None:
        # Q1 HS drain tab stays +24V; its source (pad 3) is the phase node.
        assert self._pad_net(pcb_text, "Q1", "3") == "PHASE_C"
        assert self._pad_net(pcb_text, "Q1", "1") == "GATE_CH"
        assert self._pad_net(pcb_text, "Q2", "1") == "GATE_CL"

    def test_east_column_carries_phase_a(self, pcb_text: str) -> None:
        assert self._pad_net(pcb_text, "Q5", "3") == "PHASE_A"
        assert self._pad_net(pcb_text, "Q5", "1") == "GATE_AH"
        assert self._pad_net(pcb_text, "Q6", "1") == "GATE_AL"

    def test_center_column_still_phase_b(self, pcb_text: str) -> None:
        assert self._pad_net(pcb_text, "Q3", "3") == "PHASE_B"

    def test_motor_connector_pin_order_swapped(self, pcb_text: str) -> None:
        assert self._pad_net(pcb_text, "J2", "1") == "PHASE_C"
        assert self._pad_net(pcb_text, "J2", "2") == "PHASE_B"
        assert self._pad_net(pcb_text, "J2", "3") == "PHASE_A"

    def test_shunts_and_gate_resistors_swapped(self, pcb_text: str) -> None:
        assert self._pad_net(pcb_text, "R10", "1") == "ISENSE_C+"
        assert self._pad_net(pcb_text, "R12", "1") == "ISENSE_A+"
        assert self._pad_net(pcb_text, "R20", "1") == "GATE_DRV_CH"
        assert self._pad_net(pcb_text, "R22", "1") == "GATE_DRV_AH"
        assert self._pad_net(pcb_text, "C12", "2") == "PHASE_C"
        assert self._pad_net(pcb_text, "C14", "2") == "PHASE_A"
