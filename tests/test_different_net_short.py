"""Unit tests for the grid-independent different-net short verifier + repair.

Issue #4470 (board-05 Phase 2).  These tests exercise
:mod:`kicad_tools.drc.different_net_short` on synthetic boards -- no routing,
no board recipe -- so they run in milliseconds and pin the correctness-critical
behaviour:

* the verifier flags a different-net via/via and via/segment copper overlap
  that a coarse grid-occupancy model would miss (the board-05 failure mode:
  two different-net vias quantized into overlapping world positions);
* the verifier is layer-aware -- a via only conflicts with copper on a layer
  its plated barrel actually spans, and same-net copper is never a short;
* the repair relocates the offending via to a clearance-safe location so the
  short is gone, and a boxed-in via is left in place and reported;
* the committed board-05 artifact (kicad-cli-clean) registers zero shorts, so
  an already-short-free board is never regressed.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.drc.different_net_short import (
    find_different_net_shorts,
    repair_different_net_shorts,
)
from kicad_tools.manufacturers import get_profile
from kicad_tools.schema.pcb import PCB

_SIZE = 0.6
_DRILL = 0.3


def _tier1_rules():
    """board-05's active profile (jlcpcb-tier1, 4-layer)."""
    return get_profile("jlcpcb-tier1").get_design_rules()


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def test_flags_via_via_different_net_overlap() -> None:
    """Two different-net through-hole vias overlapping in copper are a short."""
    pcb = PCB.create(width=40.0, height=40.0)
    # Centers 0.3 mm apart, radii 0.3 + 0.3 -> 0.3 mm copper overlap.
    pcb.add_via(20.0, 20.0, size=_SIZE, drill=_DRILL, net="NRST")
    pcb.add_via(20.3, 20.0, size=_SIZE, drill=_DRILL, net="OSC_IN")

    shorts = find_different_net_shorts(pcb)
    via_via = [s for s in shorts if s.kind == "via-via"]
    assert len(via_via) == 1
    assert via_via[0].net_pair == frozenset({"NRST", "OSC_IN"})
    assert via_via[0].gap < 0  # genuine copper overlap


def test_flags_via_segment_different_net_overlap() -> None:
    """A via landing on a foreign-net trace on a spanned layer is a short."""
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_trace((10.0, 10.0), (15.0, 10.0), width=0.2, layer="In2.Cu", net="PWM_CH")
    # Through-hole via barrel spans In2.Cu; its center sits on the trace.
    pcb.add_via(12.5, 10.0, size=_SIZE, drill=_DRILL, net="OSC_OUT")

    shorts = find_different_net_shorts(pcb)
    via_seg = [s for s in shorts if s.kind == "via-segment"]
    assert len(via_seg) == 1
    assert via_seg[0].net_pair == frozenset({"PWM_CH", "OSC_OUT"})
    assert via_seg[0].layer == "In2.Cu"
    assert via_seg[0].gap < 0


def test_same_net_overlap_is_not_a_short() -> None:
    """Same-net copper may touch freely -- never flagged."""
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_via(20.0, 20.0, size=_SIZE, drill=_DRILL, net="GND")
    pcb.add_via(20.2, 20.0, size=_SIZE, drill=_DRILL, net="GND")
    pcb.add_trace((10.0, 10.0), (15.0, 10.0), width=0.2, layer="B.Cu", net="GND")
    pcb.add_via(12.5, 10.0, size=_SIZE, drill=_DRILL, net="GND")

    assert find_different_net_shorts(pcb) == []


def test_layer_awareness_blind_via_does_not_short_unspanned_layer() -> None:
    """A blind via that does not reach a layer cannot short copper on it."""
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_trace((10.0, 10.0), (15.0, 10.0), width=0.2, layer="In2.Cu", net="PWM_CH")
    # A via whose barrel spans only F.Cu..In1.Cu -- it never reaches In2.Cu.
    pcb.add_via(12.5, 10.0, size=_SIZE, drill=_DRILL, layers=("F.Cu", "In1.Cu"), net="OSC_OUT")
    assert find_different_net_shorts(pcb) == []

    # A through-hole via on the trace (clear of the blind via) DOES reach
    # In2.Cu -> short.
    pcb.add_via(13.5, 10.0, size=_SIZE, drill=_DRILL, net="SWDIO")
    shorts = find_different_net_shorts(pcb)
    assert len(shorts) == 1
    assert shorts[0].net_pair == frozenset({"PWM_CH", "SWDIO"})


def test_clearance_threshold_flags_near_miss() -> None:
    """A positive clearance flags sub-clearance near-misses, not just overlaps."""
    pcb = PCB.create(width=40.0, height=40.0)
    # Gap = 0.6 - 0.3 - 0.3 = 0.0 edge-to-edge (tangent) -> not an overlap,
    # but a sub-clearance near-miss at any positive clearance.
    pcb.add_via(20.0, 20.0, size=_SIZE, drill=_DRILL, net="NRST")
    pcb.add_via(20.6, 20.0, size=_SIZE, drill=_DRILL, net="OSC_IN")

    assert find_different_net_shorts(pcb, clearance=0.0) == []
    near = find_different_net_shorts(pcb, clearance=0.15)
    assert len(near) == 1


def test_flags_segment_segment_overlap_same_layer() -> None:
    """Two different-net traces overlapping on the same layer are a short."""
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_trace((10.0, 10.0), (20.0, 10.0), width=0.25, layer="B.Cu", net="A")
    pcb.add_trace((15.0, 10.05), (25.0, 10.05), width=0.25, layer="B.Cu", net="B")
    shorts = find_different_net_shorts(pcb)
    seg_seg = [s for s in shorts if s.kind == "segment-segment"]
    assert len(seg_seg) == 1
    assert seg_seg[0].net_pair == frozenset({"A", "B"})


def test_segment_segment_different_layer_not_short() -> None:
    """Overlapping traces on different layers do not short."""
    pcb = PCB.create(width=40.0, height=40.0)
    pcb.add_trace((10.0, 10.0), (20.0, 10.0), width=0.25, layer="F.Cu", net="A")
    pcb.add_trace((15.0, 10.0), (25.0, 10.0), width=0.25, layer="B.Cu", net="B")
    assert find_different_net_shorts(pcb) == []


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def _board05_style_shorts() -> PCB:
    """A synthetic coarse-grid board reproducing the board-05 short signature.

    A different-net via/via overlap (NRST/OSC_IN) and a via-on-trace overlap
    (PWM_CH/OSC_OUT) -- the two shorts named in issue #4470 -- each with a
    routed escape node so the repair has a slide direction.
    """
    pcb = PCB.create(width=60.0, height=60.0)
    # NRST/OSC_IN via overlap, each with a B.Cu escape leg heading OUTWARD
    # (away from each other) so the vias can slide apart onto their own nodes.
    pcb.add_via(20.0, 20.0, size=_SIZE, drill=_DRILL, net="NRST")
    pcb.add_via(20.3, 20.0, size=_SIZE, drill=_DRILL, net="OSC_IN")
    pcb.add_trace((20.0, 20.0), (17.0, 20.0), width=0.2, layer="B.Cu", net="NRST")
    pcb.add_trace((20.3, 20.0), (23.0, 20.0), width=0.2, layer="B.Cu", net="OSC_IN")
    # PWM_CH/OSC_OUT via-on-trace overlap.  The foreign PWM_CH trace is on
    # In2.Cu; OSC_OUT's own escape leg is on F.Cu (a different layer), so
    # relocating the through-hole via off the trace resolves the In2.Cu short
    # without the escape leg itself crossing PWM_CH.
    pcb.add_trace((40.0, 40.0), (46.0, 40.0), width=0.2, layer="In2.Cu", net="PWM_CH")
    pcb.add_via(43.0, 40.0, size=_SIZE, drill=_DRILL, net="OSC_OUT")
    pcb.add_trace((43.0, 40.0), (43.0, 45.0), width=0.2, layer="F.Cu", net="OSC_OUT")
    return pcb


def test_repair_eliminates_shorts() -> None:
    """The repair relocates offending vias so no different-net short remains."""
    pcb = _board05_style_shorts()
    rules = _tier1_rules()

    before = find_different_net_shorts(pcb)
    assert before, "fixture should start with different-net shorts"

    result = repair_different_net_shorts(pcb, rules)
    assert result.changed
    assert not result.unresolved

    after = find_different_net_shorts(pcb)
    assert after == [], f"repair left {len(after)} short(s): {[s.describe() for s in after]}"

    # Every moved via genuinely cleared foreign copper by the clearance floor.
    for m in result.moved:
        for other in pcb.vias:
            if other.uuid == m.uuid or other.net_number == 0:
                continue
            gap = (
                math.hypot(other.position[0] - m.new_x, other.position[1] - m.new_y)
                - _SIZE / 2.0
                - other.size / 2.0
            )
            # Different-net vias must now clear (same-net stacking is allowed).
            if other.net_name != m.net_name:
                assert gap >= -1e-6


def test_repair_preserves_connectivity_with_stub() -> None:
    """A relocated via re-bonds to its old location via same-net copper."""
    pcb = _board05_style_shorts()
    result = repair_different_net_shorts(pcb, _tier1_rules())

    for m in result.moved:
        old, new = (m.old_x, m.old_y), (m.new_x, m.new_y)
        if math.hypot(new[0] - old[0], new[1] - old[1]) < 1e-6:
            continue  # no actual move
        bonded = False
        # Net number for the moved via.
        net_no = pcb.get_net_by_name(m.net_name)
        net_number = net_no.number if net_no else None
        if net_number is None:
            continue
        for seg in pcb.segments_in_net(net_number):
            eps = {seg.start, seg.end}
            if any(math.hypot(p[0] - old[0], p[1] - old[1]) < 1e-3 for p in eps) and any(
                math.hypot(p[0] - new[0], p[1] - new[1]) < 1e-3 for p in eps
            ):
                bonded = True
                break
        assert bonded, f"moved via on {m.net_name} not bonded old->new"


def test_repair_dry_run_does_not_mutate() -> None:
    """``dry_run`` reports moves without touching the board."""
    pcb = _board05_style_shorts()
    positions_before = [(v.position[0], v.position[1]) for v in pcb.vias]
    seg_count = len(pcb.segments)

    result = repair_different_net_shorts(pcb, _tier1_rules(), dry_run=True)
    assert result.moved  # it reports what it WOULD do

    positions_after = [(v.position[0], v.position[1]) for v in pcb.vias]
    assert positions_before == positions_after
    assert len(pcb.segments) == seg_count
    # Shorts are still present because nothing was actually moved.
    assert find_different_net_shorts(pcb)


def test_boxed_in_via_left_in_place_and_reported() -> None:
    """A via with no clearance-legal escape is left in place and reported."""
    rules = _tier1_rules()
    pcb = PCB.create(width=6.0, height=6.0)
    # Center via boxed by a tight ring of foreign-net vias on every side, so no
    # 8-direction candidate clears the clearance floor.
    pcb.add_via(3.0, 3.0, size=_SIZE, drill=_DRILL, net="VICTIM")
    ring = 0.62  # just inside overlap with the center via
    for k in range(16):
        ang = 2 * math.pi * k / 16
        pcb.add_via(
            3.0 + ring * math.cos(ang),
            3.0 + ring * math.sin(ang),
            size=_SIZE,
            drill=_DRILL,
            net=f"BLOCK{k}",
        )

    before = find_different_net_shorts(pcb)
    assert before, "center via should short its ring neighbours"

    result = repair_different_net_shorts(pcb, rules)
    # Nothing can be safely relocated -> shorts persist and are reported.
    assert result.unresolved
    assert find_different_net_shorts(pcb)


# ---------------------------------------------------------------------------
# Regression: an already-short-free board is never flagged
# ---------------------------------------------------------------------------


def test_committed_board05_is_short_free() -> None:
    """The committed (kicad-cli-clean) board-05 artifact has zero shorts.

    This is the "no regression on already-short-free boards" acceptance
    criterion: the geometric verifier must agree with kicad-cli that the
    shipped board has no different-net copper overlap.
    """
    artifact = (
        Path(__file__).resolve().parents[1]
        / "boards"
        / "05-bldc-motor-controller"
        / "output"
        / "bldc_controller_routed.kicad_pcb"
    )
    if not artifact.exists():
        pytest.skip("committed board-05 artifact not present")

    pcb = PCB.load(artifact)
    shorts = find_different_net_shorts(pcb)
    assert shorts == [], (
        f"unexpected shorts on committed board-05: {[s.describe() for s in shorts]}"
    )
