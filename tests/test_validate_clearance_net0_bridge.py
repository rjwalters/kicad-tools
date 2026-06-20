"""Tests for net-0 stray-copper bridge detection (Issue #3816).

The base :class:`~kicad_tools.validate.rules.clearance.ClearanceRule`
pairwise loop deliberately skips net 0 (legitimate unconnected copper --
unused pads, NPTH, test points -- is common and must not flood
``clearance_pad_pad`` false positives, e.g. board 04's 31 net-0 QFP
pins).  That skip masks a genuine short class: a piece of stray net-0
copper that physically bridges two distinct *assigned* nets.

These tests cover the bounded bridge detector added to ``ClearanceRule``:

* positive -- a net-0 island bridging two assigned nets fires exactly
  one ``clearance_net0_bridge`` error naming both nets;
* negative -- a lone net-0 element near zero/one assigned net stays
  silent;
* negative -- a cluster of mutually-touching net-0 elements that touches
  no assigned net stays silent;
* regression -- the routed boards 01..06 produce zero net-0 bridge
  violations (the false-positive guard the issue mandates).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "NET_A")
  (net 2 "NET_B")
"""


def _load(tmp_path: Path, body: str) -> PCB:
    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(_HEADER + body + "\n)\n")
    return PCB.load(pcb_path)


def _bridge_violations(pcb: PCB):
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0)
    results = checker.check_clearances()
    return [v for v in results.violations if v.rule_id == "clearance_net0_bridge"]


# ---------------------------------------------------------------------------
# Positive: net-0 island bridges two assigned nets
# ---------------------------------------------------------------------------


class TestNet0BridgePositive:
    """A net-0 segment overlapping two assigned-net pads fires one short."""

    def test_net0_segment_bridging_two_nets_fires_once(self, tmp_path: Path):
        # Two assigned pads, one on NET_A (1) at x=100, one on NET_B (2)
        # at x=110.  A net-0 segment runs straight from one to the other,
        # physically connecting both -> a stray-copper short.
        body = """\
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET_A")))
  (footprint "test:pad2" (layer "F.Cu") (at 110 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "NET_B")))
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 0) (uuid "seg-stray"))"""
        pcb = _load(tmp_path, body)

        bridges = _bridge_violations(pcb)
        assert len(bridges) == 1
        v = bridges[0]
        assert v.severity == "error"
        assert v.rule_id == "clearance_net0_bridge"
        assert set(v.nets) == {"NET_A", "NET_B"}

    def test_net0_island_of_multiple_segments_fires_once(self, tmp_path: Path):
        # Two touching net-0 segments form a single island that, together,
        # reaches both assigned pads.  Exactly one violation (per island),
        # not one per touching pair.
        body = """\
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET_A")))
  (footprint "test:pad2" (layer "F.Cu") (at 120 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "NET_B")))
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 0) (uuid "seg-a"))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 0) (uuid "seg-b"))"""
        pcb = _load(tmp_path, body)

        bridges = _bridge_violations(pcb)
        assert len(bridges) == 1
        assert set(bridges[0].nets) == {"NET_A", "NET_B"}


# ---------------------------------------------------------------------------
# Negative: lone net-0 element near zero/one assigned net
# ---------------------------------------------------------------------------


class TestNet0BridgeNegativeLone:
    """A net-0 element touching < 2 assigned nets is legitimate copper."""

    def test_lone_net0_pad_near_one_net_silent(self, tmp_path: Path):
        # A single net-0 pad 0.5 mm from a NET_A pad -- touches one
        # assigned net at most.  No bridge.
        body = """\
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET_A")))
  (footprint "test:nc" (layer "F.Cu") (at 101.5 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0)))"""
        pcb = _load(tmp_path, body)
        assert _bridge_violations(pcb) == []

    def test_net0_pad_overlapping_one_net_silent(self, tmp_path: Path):
        # Even when the net-0 pad directly overlaps a single assigned
        # net, only one net is touched -> not a bridge.
        body = """\
  (footprint "test:pad" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET_A")))
  (footprint "test:nc" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0)))"""
        pcb = _load(tmp_path, body)
        assert _bridge_violations(pcb) == []


# ---------------------------------------------------------------------------
# Negative: net-0 cluster touching only itself (board-04 QFP analogue)
# ---------------------------------------------------------------------------


class TestNet0BridgeNegativeCluster:
    """Adjacent net-0 copper that touches no assigned net stays silent."""

    def test_two_touching_net0_pads_silent(self, tmp_path: Path):
        body = """\
  (footprint "test:a" (layer "F.Cu") (at 100 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0)))
  (footprint "test:b" (layer "F.Cu") (at 100.5 100)
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 0)))"""
        pcb = _load(tmp_path, body)
        assert _bridge_violations(pcb) == []

    def test_qfp_like_net0_cluster_near_one_net_silent(self, tmp_path: Path):
        # Replicate board-04's geometry: a row of mutually-adjacent net-0
        # fine-pitch pins, adjacent to at most one assigned net.  This is
        # the case that would over-fire if net 0 were treated as foreign.
        pins = []
        for i in range(6):
            x = 100 + i * 0.5
            pins.append(
                f'  (footprint "test:p{i}" (layer "F.Cu") (at {x} 100)\n'
                f'    (pad "1" smd rect (at 0 0) (size 0.3 1) (layers "F.Cu") (net 0)))'
            )
        # One assigned-net pad adjacent to the cluster on one side.
        pins.append(
            '  (footprint "test:assigned" (layer "F.Cu") (at 99 100)\n'
            '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "NET_A")))'
        )
        body = "\n".join(pins)
        pcb = _load(tmp_path, body)
        assert _bridge_violations(pcb) == []


# ---------------------------------------------------------------------------
# Regression: routed boards 01..06 produce no net-0 bridge violations
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[1]

_ROUTED_BOARDS = {
    "01-voltage-divider": "voltage_divider_routed.kicad_pcb",
    "02-charlieplex-led": "charlieplex_3x3_routed.kicad_pcb",
    "03-usb-joystick": "usb_joystick_routed.kicad_pcb",
    "04-stm32-devboard": "stm32_devboard_routed.kicad_pcb",
    "05-bldc-motor-controller": "bldc_controller_routed.kicad_pcb",
    "06-diffpair-test": "diffpair_test_routed.kicad_pcb",
}


class TestNet0BridgeBoardRegression:
    """The net-0 bridge detector must not fire on the real routed boards.

    Boards 03/04/05 carry legitimate unconnected net-0 pads (2 / 31 / 4).
    None of them bridge two assigned nets, so the detector must report
    zero ``clearance_net0_bridge`` violations -- the false-positive guard
    the issue mandates.
    """

    @pytest.mark.parametrize("board_dir,pcb_name", sorted(_ROUTED_BOARDS.items()))
    def test_routed_board_has_no_net0_bridge(self, board_dir: str, pcb_name: str):
        pcb_path = _REPO_ROOT / "boards" / board_dir / "output" / pcb_name
        if not pcb_path.exists():
            pytest.skip(f"routed board artifact missing: {pcb_path}")

        pcb = PCB.load(pcb_path)
        # 4-layer profile covers the 4-layer boards; 2-layer boards ignore
        # the extra copper layers harmlessly.
        checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4, copper_oz=1.0)
        results = checker.check_clearances()
        bridges = [v for v in results.violations if v.rule_id == "clearance_net0_bridge"]
        assert bridges == [], (
            f"{board_dir}: unexpected net-0 bridge violations: {[v.message for v in bridges]}"
        )
