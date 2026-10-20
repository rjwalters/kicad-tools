"""Board 07 ``BOOT0`` net length agrees with kicad-cli (Issue #6731).

PR #6729's first length model read ``BOOT0`` on the committed board 07
artifact as 44.9471 mm; kicad-cli 10.0.6 (Docker ``kicad/kicad:10.0``)
reports 44.9270 mm.  The 0.0201 mm was suspected to come from the
stair-stepped diagonal into the roundrect pad, but it comes from the other end
of the net:

* The In2.Cu run ends at (28.3875, 24.8375), 0.5 micron off the centre of the
  via at (28.388, 24.8375) (0.5 mm diameter).
* The run goes diagonally to (28.5, 24.95), then straight up x = 28.5 and
  leaves the via copper at y = 25.06101.
* kicad-cli clips that end at the via edge and runs it straight to the via
  centre (0.25000 mm), dropping the 0.27011 mm in-via path.  The first model
  clipped only ends exactly *at* a via centre, so it kept the extra
  0.020107 mm.

The rule that a run end anywhere inside via copper is clipped landed in
#6779 (``kicad_length``).  These tests pin it for this geometry.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.router.diffpair_length import DiffPairLengthTracker
from kicad_tools.router.kicad_length import KicadStackup, LSeg, LVia, net_length
from kicad_tools.schema.pcb import PCB

REPO = Path(__file__).resolve().parents[1]
BOARD07 = REPO / "boards" / "07-matchgroup-test" / "output" / "matchgroup_test.kicad_pcb"

#: kicad-cli 10.0.6 (Docker ``kicad/kicad:10.0``) ``length`` rule actual for
#: ``BOOT0`` on the committed board 07 artifact.
BOOT0_KICAD_CLI = 44.9270

# Via and In2.Cu run end of BOOT0, copied from the artifact.
_VX, _VY, _VD = 28.388, 24.8375, 0.5
_END = (28.3875, 24.8375)


def _boot0_via_trim() -> float:
    """In-via path minus its straight replacement, worked by hand."""
    r = _VD / 2
    y_exit = _VY + math.sqrt(r * r - (28.5 - _VX) ** 2)
    inside = math.hypot(28.5 - _END[0], 24.95 - _END[1]) + (y_exit - 24.95)
    straight = math.hypot(28.5 - _VX, y_exit - _VY)
    return inside - straight


def test_hand_trim_is_the_reported_delta():
    # The trim is the whole 0.020 mm discrepancy reported in #6731.
    assert _boot0_via_trim() == pytest.approx(0.020107, abs=1e-6)


@pytest.mark.parametrize("end", [_END, (_VX, _VY)], ids=["off_centre", "centred"])
def test_run_end_off_via_centre_is_clipped_like_a_centred_one(end):
    via = LVia(_VX, _VY, _VD, ("F.Cu", "B.Cu"))
    segs = [
        LSeg(_VX, _VY, _VX, 22.0, "F.Cu"),
        LSeg(end[0], end[1], 28.5, 24.95, "In2.Cu"),
        LSeg(28.5, 24.95, 28.5, 27.0, "In2.Cu"),
    ]
    raw_in2 = math.hypot(28.5 - end[0], 24.95 - end[1]) + 2.05
    stackup = KicadStackup.default(1.6, 4)
    span = stackup.span("F.Cu", "In2.Cu")
    got = net_length(segs, [via], [], stackup)
    if end == _END:
        expected = 2.8375 + raw_in2 - _boot0_via_trim() + span
        assert got == pytest.approx(expected, abs=1e-9)
    # Measured by kicad-cli 10.0.6 on both variants: 6.0740 mm.
    assert got == pytest.approx(6.0740, abs=1e-4)


@pytest.mark.skipif(not BOARD07.exists(), reason="board 07 artifact not present")
def test_board07_boot0_matches_kicad_cli_within_one_micron():
    pcb = PCB.load(str(BOARD07))
    net = next(n for n in pcb.nets.values() if n.name == "BOOT0")
    got = DiffPairLengthTracker.measure_net_from_pcb(pcb, net.number)
    # The acceptance bar is 1 micron.  Main reads 44.927009, which is within
    # kicad-cli's 4-decimal rounding.
    assert got == pytest.approx(BOOT0_KICAD_CLI, abs=1e-3)
